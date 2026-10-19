"""Coding workflow (single coding agent) as a LangGraph state machine.

    prepare -> code -> test --pass--> package --> END (awaiting_approval | no_changes)
                 ^       |
                 +-------+ fail, attempts left
                         +-- fail, no attempts left --> hand_off --> END

The run stops at a human approval point. `package` stores the exact patch and its SHA-256;
publishing (publish.py) happens later, only for an approved job, by re-applying that patch.
Control flow is deterministic code; the LLM is used only inside `code`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
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
PACKAGER_TOOLS = frozenset({"git_diff", "export_patch"})
PUBLISHER_TOOLS = frozenset({"git_clone", "apply_patch", "git_commit_and_push", "open_pull_request"})

JobStatus = Literal["running", "awaiting_approval", "no_changes", "handed_off", "failed"]


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
    patch: str
    diff_sha256: str
    status: JobStatus
    pr_url: str | None
    error: str | None
    prompt_refs: list[str]


@dataclass(frozen=True)
class AgentLimits:
    max_attempts: int = 3  # first attempt + fixes
    max_llm_turns: int = 25  # per attempt
    min_llm_interval_seconds: float = 0.0  # pacing for rate-limited providers


@dataclass
class AgentDeps:
    toolbox: Toolbox
    model: ToolCallingModel
    prompts: PromptRegistry
    limits: AgentLimits
    provider: str | None = None


_EXIT_CODE = re.compile(r"^EXIT_CODE=(-?\d+)")


def _exit_code(output: str) -> int:
    match = _EXIT_CODE.match(output)
    return int(match.group(1)) if match else 1


def tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


def build_graph(deps: AgentDeps) -> Any:
    toolbox, limits = deps.toolbox, deps.limits
    last_llm_call = 0.0

    async def paced_llm_call(messages: list[BaseMessage]) -> BaseMessage:
        nonlocal last_llm_call
        wait = limits.min_llm_interval_seconds - (time.monotonic() - last_llm_call)
        if wait > 0:
            await asyncio.sleep(wait)
        last_llm_call = time.monotonic()
        return await deps.model.ainvoke(messages)

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
                "test_output": tail(state.get("test_output", "")) if attempt > 1 else "",
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
            response = await paced_llm_call(messages)
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

    def after_test(state: AgentState) -> Literal["package", "code", "hand_off"]:
        if state["test_exit_code"] == 0:
            return "package"
        return "code" if state["attempt"] < limits.max_attempts else "hand_off"

    async def package(state: AgentState) -> dict[str, Any]:
        """Freeze the result for review: a readable diff plus the exact patch and its hash."""
        diff = await toolbox.call("git_diff", {}, actor="packager", allowed=PACKAGER_TOOLS)
        if diff.strip() == "(no changes)":
            return {"diff": "", "status": "no_changes"}
        patch = await toolbox.call("export_patch", {}, actor="packager", allowed=PACKAGER_TOOLS)
        if patch.startswith("ERROR"):
            return {"status": "failed", "error": patch}
        return {
            "diff": diff,
            "patch": patch,
            "diff_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "status": "awaiting_approval",
        }

    async def hand_off(state: AgentState) -> dict[str, Any]:
        return {
            "status": "handed_off",
            "error": f"Tests still failing after {state['attempt']} attempt(s); needs a human.",
        }

    graph = StateGraph(AgentState)
    graph.add_node("prepare", prepare)
    graph.add_node("code", code)
    graph.add_node("test", test)
    graph.add_node("package", package)
    graph.add_node("hand_off", hand_off)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", after_prepare, {"code": "code", "end": END})
    graph.add_edge("code", "test")
    graph.add_conditional_edges(
        "test", after_test, {"package": "package", "code": "code", "hand_off": "hand_off"}
    )
    graph.add_edge("package", END)
    graph.add_edge("hand_off", END)
    return graph.compile()


__all__ = ["AgentDeps", "AgentLimits", "AgentState", "LoopDetectedError", "build_graph", "tail"]
