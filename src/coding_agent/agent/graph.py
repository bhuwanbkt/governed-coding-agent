"""Phase 1 workflow (single coding agent) as a LangGraph state machine.

    prepare -> code -> test --pass--> approve --yes--> publish -> END
                 ^       |               |
                 |       +--fail, attempts left
                 +-------+               +--no--> END (rejected)
                         +--fail, no attempts left--> hand_off -> END

Control flow is deterministic code; the LLM is used only inside `code`. Tests always run
as a separate step the model can't skip, and nothing is pushed without human approval.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from coding_agent.agent.llm import ToolCallingModel, message_text
from coding_agent.agent.toolbox import LoopDetectedError, Toolbox, ToolNotAllowedError
from coding_agent.prompts import PromptRegistry

logger = logging.getLogger(__name__)

# Least privilege: each role sees and may call only these tools.
CODER_TOOLS = frozenset({"list_files", "read_file", "search_code", "write_file", "replace_in_file"})
SETUP_TOOLS = frozenset({"git_clone", "list_files"})
TESTER_TOOLS = frozenset({"run_tests"})
REVIEWER_TOOLS = frozenset({"git_diff"})
PUBLISHER_TOOLS = frozenset({"git_commit_and_push", "open_pull_request"})

JobStatus = Literal["running", "no_changes", "rejected", "succeeded", "handed_off", "failed"]
Approver = Callable[["ApprovalRequest"], Awaitable[bool]]


class AgentState(TypedDict, total=False):
    job_id: str
    repo_url: str
    base_branch: str
    work_branch: str
    task: str
    test_command: str
    repo_files: str
    attempt: int
    coder_summary: str
    test_exit_code: int
    test_output: str
    diff: str
    status: JobStatus
    pr_url: str | None
    error: str | None
    prompt_refs: list[str]


@dataclass(frozen=True)
class ApprovalRequest:
    job_id: str
    task: str
    diff: str
    coder_summary: str
    test_output: str


@dataclass(frozen=True)
class AgentLimits:
    max_attempts: int = 3  # first attempt + fixes
    max_llm_turns: int = 25  # per attempt


@dataclass
class AgentDeps:
    toolbox: Toolbox
    model: ToolCallingModel
    prompts: PromptRegistry
    approver: Approver
    limits: AgentLimits
    provider: str | None = None


_EXIT_CODE = re.compile(r"^EXIT_CODE=(-?\d+)")


def _exit_code(output: str) -> int:
    match = _EXIT_CODE.match(output)
    return int(match.group(1)) if match else 1


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


def build_graph(deps: AgentDeps) -> Any:
    toolbox, limits = deps.toolbox, deps.limits

    async def prepare(state: AgentState) -> dict[str, Any]:
        cloned = await toolbox.call(
            "git_clone",
            {
                "repo_url": state["repo_url"],
                "base_branch": state["base_branch"],
                "work_branch": state["work_branch"],
            },
            actor="setup",
            allowed=SETUP_TOOLS,
        )
        if cloned.startswith("ERROR"):
            return {"status": "failed", "error": f"Could not clone the repository: {cloned}"}
        files = await toolbox.call("list_files", {}, actor="setup", allowed=SETUP_TOOLS)
        return {"repo_files": files, "attempt": 0, "status": "running", "prompt_refs": []}

    def after_prepare(state: AgentState) -> Literal["code", "end"]:
        return "code" if state["status"] == "running" else "end"

    async def code(state: AgentState) -> dict[str, Any]:
        attempt = state.get("attempt", 0) + 1
        prompt = deps.prompts.render(
            "coder",
            {
                "task": state["task"],
                "test_command": state["test_command"],
                "repo_files": state["repo_files"],
                "test_output": _tail(state.get("test_output", "")) if attempt > 1 else "",
                "attempt": attempt,
                "max_attempts": limits.max_attempts,
            },
            provider=deps.provider,
        )
        logger.info("coder attempt started", extra={"attempt": attempt, "prompt": prompt.ref})
        messages: list[BaseMessage] = [
            SystemMessage(prompt.content),
            HumanMessage("Start working on the task."),
        ]
        summary = "Stopped: LLM turn budget exhausted before the coder finished."
        for _ in range(limits.max_llm_turns):
            response = await deps.model.ainvoke(messages)
            messages.append(response)
            tool_calls = response.tool_calls if isinstance(response, AIMessage) else []
            if not tool_calls:
                summary = message_text(response).strip() or "(no summary)"
                break
            for call in tool_calls:
                try:
                    result = await toolbox.call(
                        call["name"], call["args"], actor="coder", allowed=CODER_TOOLS
                    )
                except ToolNotAllowedError as exc:
                    result = f"ERROR: {exc}. Use only the tools you were given."
                messages.append(ToolMessage(content=result, tool_call_id=call.get("id") or call["name"]))
        return {
            "attempt": attempt,
            "coder_summary": summary,
            "prompt_refs": [*state.get("prompt_refs", []), prompt.ref],
        }

    async def test(state: AgentState) -> dict[str, Any]:
        output = await toolbox.call(
            "run_tests", {"command": state["test_command"]}, actor="tester", allowed=TESTER_TOOLS
        )
        return {"test_exit_code": _exit_code(output), "test_output": output}

    def after_test(state: AgentState) -> Literal["approve", "code", "hand_off"]:
        if state["test_exit_code"] == 0:
            return "approve"
        return "code" if state["attempt"] < limits.max_attempts else "hand_off"

    async def approve(state: AgentState) -> dict[str, Any]:
        diff = await toolbox.call("git_diff", {}, actor="reviewer", allowed=REVIEWER_TOOLS)
        if diff.strip() == "(no changes)":
            return {"diff": diff, "status": "no_changes"}
        approved = await deps.approver(
            ApprovalRequest(
                job_id=state["job_id"],
                task=state["task"],
                diff=diff,
                coder_summary=state.get("coder_summary", ""),
                test_output=_tail(state.get("test_output", ""), 20),
            )
        )
        return {"diff": diff, "status": "running" if approved else "rejected"}

    def after_approve(state: AgentState) -> Literal["publish", "end"]:
        return "publish" if state["status"] == "running" else "end"

    async def publish(state: AgentState) -> dict[str, Any]:
        pushed = await toolbox.call(
            "git_commit_and_push",
            {"message": f"feat(agent): {state['task'][:60]}\n\nJob: {state['job_id']}"},
            actor="publisher",
            allowed=PUBLISHER_TOOLS,
        )
        body = (
            f"## Task\n\n{state['task']}\n\n"
            f"## Agent summary\n\n{state.get('coder_summary', '')}\n\n"
            f"## Tests\n\n```\n{_tail(state.get('test_output', ''), 15)}\n```\n\n"
            f"---\nJob `{state['job_id']}` · attempts: {state['attempt']} · "
            f"prompts: {', '.join(state.get('prompt_refs', []))} · approved by a human before push"
        )
        pr = await toolbox.call(
            "open_pull_request",
            {"title": f"[agent] {state['task'][:70]}", "body": body, "base_branch": state["base_branch"]},
            actor="publisher",
            allowed=PUBLISHER_TOOLS,
        )
        pr_url = pr.removeprefix("PR_URL=") if pr.startswith("PR_URL=") else None
        if pushed.startswith("ERROR") or pr.startswith("ERROR"):
            return {"status": "failed", "error": f"{pushed}\n{pr}", "pr_url": pr_url}
        return {"status": "succeeded", "pr_url": pr_url}

    async def hand_off(state: AgentState) -> dict[str, Any]:
        return {
            "status": "handed_off",
            "error": f"Tests still failing after {state['attempt']} attempt(s); needs a human.",
        }

    graph = StateGraph(AgentState)
    graph.add_node("prepare", prepare)
    graph.add_node("code", code)
    graph.add_node("test", test)
    graph.add_node("approve", approve)
    graph.add_node("publish", publish)
    graph.add_node("hand_off", hand_off)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", after_prepare, {"code": "code", "end": END})
    graph.add_edge("code", "test")
    graph.add_conditional_edges(
        "test", after_test, {"approve": "approve", "code": "code", "hand_off": "hand_off"}
    )
    graph.add_conditional_edges("approve", after_approve, {"publish": "publish", "end": END})
    graph.add_edge("publish", END)
    graph.add_edge("hand_off", END)
    return graph.compile()


__all__ = [
    "AgentDeps",
    "AgentLimits",
    "AgentState",
    "ApprovalRequest",
    "Approver",
    "LoopDetectedError",
    "build_graph",
]
