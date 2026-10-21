"""Multi-agent coding workflow as a LangGraph state machine.

    prepare -> index -> plan -> code -> test -> [after_test] --pass--> review -> [after_review]
                         ^       ^                  |                                  |
                         |       +---- fix ---------+         changes requested -------+
                         +------ re-plan -----------+                                  |
                                                    +--> hand_off -> END       package -> END

Agents (LLM inside a node): planner, coder, reviewer. Deterministic steps: setup, indexer,
tester, packager. The run stops at a human approval point; publishing happens later (publish.py).

Routing at the two decision points ([after_test], [after_review]) is made by `decide()`:

- `SUPERVISOR_MODE=deterministic` (default): fixed rules pick the next step.
- `SUPERVISOR_MODE=llm`: a supervisor LLM proposes the next step; the proposal must be one of the
  options the policy allows in the current state, and hard rules (e.g. nothing reaches review or
  approval with failing tests) override it. Rejected proposals are audited as `policy_block`.

Budgets bound every loop: fix attempts, re-plans, review rounds, turns per agent, LLM calls and
LLM cost per job. When a budget runs out the job is handed to a human instead of looping (ADR 0008).
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
from pydantic import BaseModel, ValidationError

from coding_agent.agent.llm import ModelProvider, ToolCallingModel, message_text
from coding_agent.agent.roles import (
    CODER,
    INDEXER_TOOLS,
    PACKAGER_TOOLS,
    PLANNER,
    REVIEWER,
    REVIEWER_TOOLS,
    SETUP_TOOLS,
    SUPERVISOR,
    TESTER_TOOLS,
    Role,
)
from coding_agent.agent.schemas import Plan, ReviewVerdict, submit_tool_schema
from coding_agent.agent.toolbox import LoopDetectedError, Toolbox, ToolNotAllowedError
from coding_agent.governance.policy import Policy
from coding_agent.governance.pricing import cost_usd
from coding_agent.governance.redact import find_secrets
from coding_agent.governance.risk import assess
from coding_agent.prompts import PromptRegistry

logger = logging.getLogger(__name__)

JobStatus = Literal["running", "awaiting_approval", "no_changes", "handed_off", "failed"]
SupervisorMode = Literal["deterministic", "llm"]

# The policy table: every step the router may ever take from each decision point.
# Which of these are offered at runtime depends on the state (tests, budgets).
TRANSITIONS = {
    "after_test": frozenset({"review", "code", "plan", "hand_off"}),
    "after_review": frozenset({"code", "package"}),
}
MAX_REVIEW_DIFF_CHARS = 30_000


class AgentState(TypedDict, total=False):
    job_id: str
    repo_url: str
    base_branch: str
    work_branch: str
    task: str
    test_command: str
    repo_files: str
    index_summary: str
    plan: dict[str, Any] | None
    round: int  # coder runs so far
    fix_attempts: int
    replans: int
    review_rounds: int
    coder_context: str  # why the coder runs again: "", "tests_failed" or "review"
    coder_summary: str
    test_exit_code: int
    test_output: str
    review: dict[str, Any] | None
    review_feedback: str
    next_step: str
    routes: list[dict[str, str]]
    halt: str | None  # set when a budget runs out or an agent can't produce its output
    llm_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    risk_level: str
    policy_findings: list[dict[str, str]]
    duration_ms: int
    diff: str
    patch: str
    diff_sha256: str
    status: JobStatus
    pr_url: str | None
    error: str | None
    prompt_refs: list[str]


@dataclass(frozen=True)
class AgentLimits:
    max_fix_attempts: int = 2  # coder retries after failing tests
    max_llm_turns: int = 25  # per coder run
    max_llm_calls: int = 40  # whole job, all agents
    max_cost_usd: float = float("inf")  # list-price LLM cost, whole job
    prices_per_mtok: tuple[float, float] = (0.0, 0.0)
    max_review_rounds: int = 1
    max_replans: int = 1
    min_llm_interval_seconds: float = 0.0  # pacing for rate-limited providers
    supervisor_mode: SupervisorMode = "deterministic"


@dataclass
class AgentDeps:
    toolbox: Toolbox
    models: ModelProvider
    prompts: PromptRegistry
    limits: AgentLimits
    provider: str | None = None
    policy: Policy | None = None


class BudgetExhaustedError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRun:
    output: BaseModel | None
    text: str
    prompt_ref: str


_EXIT_CODE = re.compile(r"^EXIT_CODE=(-?\d+)")


def _exit_code(output: str) -> int:
    match = _EXIT_CODE.match(output)
    return int(match.group(1)) if match else 1


def tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _not_reviewed(reason: str) -> dict[str, Any]:
    return {"verdict": "not_reviewed", "summary": reason, "findings": []}


def build_graph(deps: AgentDeps) -> Any:
    toolbox, limits = deps.toolbox, deps.limits
    calls = 0
    last_call = 0.0
    tokens = {"in": 0, "out": 0}

    def spent() -> float:
        return cost_usd(tokens["in"], tokens["out"], limits.prices_per_mtok)

    def usage() -> dict[str, Any]:
        return {
            "llm_calls": calls,
            "tokens_in": tokens["in"],
            "tokens_out": tokens["out"],
            "cost_usd": round(spent(), 6),
        }

    # ------------------------------------------------------------------ LLM plumbing
    async def llm(model: ToolCallingModel, messages: list[BaseMessage]) -> BaseMessage:
        nonlocal calls, last_call
        if calls >= limits.max_llm_calls:
            raise BudgetExhaustedError(f"LLM call budget exhausted ({limits.max_llm_calls} calls)")
        if spent() >= limits.max_cost_usd:
            raise BudgetExhaustedError(
                f"LLM cost budget exhausted (${spent():.4f} of ${limits.max_cost_usd:.2f})"
            )
        wait = limits.min_llm_interval_seconds - (time.monotonic() - last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        last_call = time.monotonic()
        calls += 1
        response = await model.ainvoke(messages)
        metadata = getattr(response, "usage_metadata", None) or {}
        tokens["in"] += int(metadata.get("input_tokens", 0) or 0)
        tokens["out"] += int(metadata.get("output_tokens", 0) or 0)
        return response

    async def run_agent(role: Role, variables: dict[str, Any], kickoff: str) -> AgentRun:
        """Tool-calling loop for one agent. Agents with a structured output finish by calling
        their submit tool; the coder finishes by replying without tool calls."""
        prompt = deps.prompts.render(role.prompt, variables, provider=deps.provider)
        tools = toolbox.schemas(sorted(role.tools))
        if role.output is not None and role.submit_tool:
            tools.append(submit_tool_schema(role.submit_tool, role.submit_description, role.output))
        model = deps.models.for_role(role.name, tools)
        max_turns = limits.max_llm_turns if role is CODER else role.max_turns
        logger.info("agent started", extra={"role": role.name, "prompt": prompt.ref})
        messages: list[BaseMessage] = [SystemMessage(prompt.content), HumanMessage(kickoff)]
        text = ""
        nudged = False
        for _ in range(max_turns):
            response = await llm(model, messages)
            messages.append(response)
            tool_calls = response.tool_calls if isinstance(response, AIMessage) else []
            if not tool_calls:
                text = message_text(response).strip()
                if role.output is None:
                    return AgentRun(None, text or "(no summary)", prompt.ref)
                if nudged:
                    break
                nudged = True
                messages.append(HumanMessage(f"Finish by calling `{role.submit_tool}` with your answer."))
                continue
            for call in tool_calls:
                call_id = call.get("id") or call["name"]
                if role.output is not None and call["name"] == role.submit_tool:
                    try:
                        output = role.output.model_validate(call["args"])
                    except ValidationError as exc:
                        error = exc.errors(include_url=False)
                        messages.append(
                            ToolMessage(
                                content=f"ERROR: invalid arguments {error}. Fix them and call "
                                f"{role.submit_tool} again.",
                                tool_call_id=call_id,
                            )
                        )
                        continue
                    await toolbox.record(role.name, "system", f"{role.submit_tool}: {_brief(output)}")
                    return AgentRun(output, text, prompt.ref)
                try:
                    result = await toolbox.call(
                        call["name"], call["args"], actor=role.name, allowed=role.tools
                    )
                except ToolNotAllowedError as exc:
                    result = f"ERROR: {exc}. Use only the tools you were given."
                messages.append(ToolMessage(content=result, tool_call_id=call_id))
        if role.output is None:
            return AgentRun(None, "Stopped: LLM turn budget exhausted before the coder finished.", prompt.ref)
        return AgentRun(None, text, prompt.ref)

    # ------------------------------------------------------------------ routing
    async def decide(
        point: str, options: dict[str, str], default: str, state: AgentState, situation: str
    ) -> dict[str, Any]:
        """Pick the next step. Deterministic by default; in LLM mode the supervisor proposes and
        the policy (allowed options + hard rules) decides."""
        if not set(options) <= TRANSITIONS[point] or default not in options:
            raise ValueError(f"options {sorted(options)} break the policy table for {point}")
        proposed, reason, source = default, "rule", "deterministic"
        if limits.supervisor_mode == "llm" and len(options) > 1:
            try:
                run = await run_agent(
                    SUPERVISOR,
                    {"task": state["task"], "situation": situation, "options": options},
                    "Choose the next step.",
                )
                if run.output is not None:
                    proposed = str(getattr(run.output, "next", ""))
                    reason = str(getattr(run.output, "reason", ""))
                    source = "llm"
                else:
                    reason = "supervisor gave no valid answer; rule used"
            except BudgetExhaustedError:
                reason = "LLM budget exhausted; rule used"
        choice = proposed
        if choice not in options:
            await toolbox.record(
                "supervisor",
                "policy_block",
                f"{point}: proposed {proposed!r} is not allowed now (options: {', '.join(options)}); "
                f"using {default!r}",
                allowed=False,
            )
            choice, source = default, "policy"
        # Hard rules: nothing reaches review or approval with failing tests.
        if choice in ("review", "package") and state.get("test_exit_code") != 0:
            choice, source = default, "policy"
        await toolbox.record(
            "supervisor" if source == "llm" else "router",
            "system",
            f"{point} -> {choice} ({source}): {reason}",
        )
        route = {"point": point, "choice": choice, "source": source, "reason": reason}
        return {"next_step": choice, "routes": [*state.get("routes", []), route], **usage()}

    def situation(state: AgentState) -> str:
        review = state.get("review")
        review_text = (
            ReviewVerdict.model_validate(review).as_markdown()
            if review and review.get("verdict") in ("approve", "request_changes")
            else "(not reviewed yet)"
        )
        passed = state.get("test_exit_code") == 0
        return (
            f"- Coder rounds so far: {state.get('round', 0)}\n"
            f"- Fix attempts used: {state.get('fix_attempts', 0)} of {limits.max_fix_attempts}\n"
            f"- Re-plans used: {state.get('replans', 0)} of {limits.max_replans}\n"
            f"- Review rounds used: {state.get('review_rounds', 0)} of {limits.max_review_rounds}\n"
            f"- LLM calls used: {calls} of {limits.max_llm_calls} (cost ${spent():.4f})\n"
            f"- Tests: {'passing' if passed else 'failing'}\n\n"
            f'<untrusted kind="test-output">\n{tail(state.get("test_output", ""), 15)}\n</untrusted>\n\n'
            f"Review:\n{review_text}"
        )

    # ------------------------------------------------------------------ nodes
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
        return {
            "repo_files": files,
            "status": "running",
            "round": 0,
            "fix_attempts": 0,
            "replans": 0,
            "review_rounds": 0,
            "coder_context": "",
            "routes": [],
            "prompt_refs": [],
            "llm_calls": 0,
            "halt": None,
            "plan": None,
            "review": None,
        }

    async def index(state: AgentState) -> dict[str, Any]:
        try:
            result = await toolbox.call("build_index", {}, actor="indexer", allowed=INDEXER_TOOLS)
        except ToolNotAllowedError as exc:  # code_index server not running
            result = f"ERROR: {exc}"
        if result.startswith("ERROR"):
            logger.warning("code index unavailable", extra={"error": result[:300]})
            return {"index_summary": "unavailable; use search_code and read_file instead"}
        return {"index_summary": result}

    async def plan(state: AgentState) -> dict[str, Any]:
        replanning = state.get("replans", 0) > 0 and state.get("plan") is not None
        previous = (
            Plan.model_validate(state["plan"]).as_markdown() if replanning and state.get("plan") else ""
        )
        try:
            run = await run_agent(
                PLANNER,
                {
                    "task": state["task"],
                    "test_command": state["test_command"],
                    "repo_files": state["repo_files"],
                    "index_summary": state.get("index_summary", "unavailable"),
                    "replan_reason": tail(state.get("test_output", "")) if replanning else "",
                    "previous_plan": previous,
                },
                "Explore the repository, then submit your plan.",
            )
        except BudgetExhaustedError as exc:
            return {"halt": str(exc), **usage()}
        refs = [*state.get("prompt_refs", []), run.prompt_ref]
        if run.output is None:
            return {
                "halt": "The planner did not produce a valid plan.",
                "prompt_refs": refs,
                **usage(),
            }
        return {"plan": run.output.model_dump(), "coder_context": "", "prompt_refs": refs, **usage()}

    async def code(state: AgentState) -> dict[str, Any]:
        round_ = state.get("round", 0) + 1
        context = state.get("coder_context", "")
        plan_md = Plan.model_validate(state["plan"]).as_markdown() if state.get("plan") else "(no plan)"
        try:
            run = await run_agent(
                CODER,
                {
                    "task": state["task"],
                    "test_command": state["test_command"],
                    "repo_files": state["repo_files"],
                    "plan": plan_md,
                    "round": round_,
                    "test_output": tail(state.get("test_output", "")) if context == "tests_failed" else "",
                    "review_findings": state.get("review_feedback", "") if context == "review" else "",
                },
                "Start working on the task.",
            )
        except BudgetExhaustedError as exc:
            return {"round": round_, "halt": str(exc), **usage()}
        return {
            "round": round_,
            "coder_summary": run.text,
            "prompt_refs": [*state.get("prompt_refs", []), run.prompt_ref],
            **usage(),
        }

    async def test(state: AgentState) -> dict[str, Any]:
        output = await toolbox.call(
            "run_tests", {"command": state["test_command"]}, actor="tester", allowed=TESTER_TOOLS
        )
        return {"test_exit_code": _exit_code(output), "test_output": output}

    async def after_test(state: AgentState) -> dict[str, Any]:
        passed = state.get("test_exit_code") == 0
        options: dict[str, str]
        if state.get("halt"):
            options = {"hand_off": "Stop and hand the job to a human."}
        elif passed:
            options = {"review": "Tests pass: ask the Reviewer to check the change."}
        elif state.get("fix_attempts", 0) < limits.max_fix_attempts:
            options = {"code": "Send the test failure back to the Coder to fix."}
            if state.get("replans", 0) < limits.max_replans:
                options["plan"] = "The approach looks wrong: ask the Planner for a new plan."
            options["hand_off"] = "Stop and hand the job to a human."
        else:
            options = {"hand_off": "Out of fix attempts: hand the job to a human."}
        decision = await decide("after_test", options, next(iter(options)), state, situation(state))
        if decision["next_step"] == "code":
            decision.update(fix_attempts=state.get("fix_attempts", 0) + 1, coder_context="tests_failed")
        elif decision["next_step"] == "plan":
            decision.update(
                fix_attempts=state.get("fix_attempts", 0) + 1, replans=state.get("replans", 0) + 1
            )
        return decision

    async def review(state: AgentState) -> dict[str, Any]:
        diff = await toolbox.call("git_diff", {}, actor="reviewer", allowed=REVIEWER_TOOLS)
        if diff.strip() == "(no changes)":
            return {"review": None}
        plan_md = Plan.model_validate(state["plan"]).as_markdown() if state.get("plan") else "(no plan)"
        try:
            run = await run_agent(
                REVIEWER,
                {
                    "task": state["task"],
                    "plan": plan_md,
                    "test_output": tail(state.get("test_output", ""), 30),
                    "diff": diff[:MAX_REVIEW_DIFF_CHARS],
                },
                "Review the change, then submit your verdict.",
            )
        except BudgetExhaustedError:
            return {"review": _not_reviewed("Skipped: the LLM call budget ran out."), **usage()}
        refs = [*state.get("prompt_refs", []), run.prompt_ref]
        if run.output is None:
            verdict = _not_reviewed("The reviewer did not return a valid verdict.")
        else:
            verdict = run.output.model_dump()
        return {"review": verdict, "prompt_refs": refs, **usage()}

    async def after_review(state: AgentState) -> dict[str, Any]:
        review_ = state.get("review")
        finish = "Finish: send the change and this review to the human approver."
        options: dict[str, str] = {"package": finish}
        default = "package"
        if (
            review_
            and review_.get("verdict") == "request_changes"
            and state.get("review_rounds", 0) < limits.max_review_rounds
        ):
            options = {"code": "Send the findings back to the Coder for another round.", "package": finish}
            if ReviewVerdict.model_validate(review_).blocking():
                default = "code"
        decision = await decide("after_review", options, default, state, situation(state))
        if decision["next_step"] == "code" and review_:
            decision.update(
                review_rounds=state.get("review_rounds", 0) + 1,
                coder_context="review",
                review_feedback=ReviewVerdict.model_validate(review_).as_markdown(),
            )
        return decision

    async def package(state: AgentState) -> dict[str, Any]:
        """Freeze the result for review: a readable diff plus the exact patch and its hash.
        Last gate before a human sees it: no secrets in the patch, and the diff must satisfy
        the policy (protected paths, size limits), however the files were changed."""
        diff = await toolbox.call("git_diff", {}, actor="packager", allowed=PACKAGER_TOOLS)
        if diff.strip() == "(no changes)":
            return {"diff": "", "status": "no_changes", **usage()}
        patch = await toolbox.call("export_patch", {}, actor="packager", allowed=PACKAGER_TOOLS)
        if patch.startswith("ERROR"):
            return {"status": "failed", "error": patch, **usage()}
        findings: list[dict[str, str]] = [
            {"kind": "injection", "detail": flag} for flag in toolbox.injection_flags[:20]
        ]
        if secrets := find_secrets(patch):
            await toolbox.record(
                "packager", "policy_block", f"patch contains secret-like values: {secrets}", allowed=False
            )
            findings.append({"kind": "secret", "detail": f"patch contains: {', '.join(secrets)}"})
            return {
                "status": "handed_off",
                "error": f"The change contains what looks like a secret ({', '.join(secrets)}); "
                "it was not packaged. A human must check the repository.",
                "policy_findings": findings,
                **usage(),
            }
        policy = deps.policy
        if policy is not None and (violations := policy.check_patch(patch)):
            for violation in violations:
                await toolbox.record("packager", "policy_block", f"{policy.ref}: {violation}", allowed=False)
            findings.extend({"kind": "violation", "detail": str(v)} for v in violations)
            return {
                "status": "handed_off",
                "error": "The change breaks the policy: " + "; ".join(str(v) for v in violations),
                "policy_findings": findings,
                **usage(),
            }
        risk_level, reasons = (
            assess(patch, policy, injection_flags=toolbox.injection_flags, review=state.get("review"))
            if policy is not None
            else ("low", [])
        )
        findings.extend({"kind": "risk", "detail": reason} for reason in reasons)
        await toolbox.record("packager", "system", f"risk {risk_level}: {'; '.join(reasons) or 'no flags'}")
        return {
            "diff": diff,
            "patch": patch,
            "diff_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "status": "awaiting_approval",
            "risk_level": risk_level,
            "policy_findings": findings,
            **usage(),
        }

    async def hand_off(state: AgentState) -> dict[str, Any]:
        reason = state.get("halt") or (
            f"Tests still failing after {state.get('round', 0)} coder round(s); needs a human."
        )
        return {"status": "handed_off", "error": reason, **usage()}

    # ------------------------------------------------------------------ wiring
    def after_prepare(state: AgentState) -> str:
        return "index" if state["status"] == "running" else "end"

    def unless_halted(target: str) -> Any:
        def route(state: AgentState) -> str:
            return "hand_off" if state.get("halt") else target

        return route

    def by_next_step(state: AgentState) -> str:
        return state["next_step"]

    graph = StateGraph(AgentState)
    for name, node in {
        "prepare": prepare,
        "index": index,
        "plan": plan,
        "code": code,
        "test": test,
        "after_test": after_test,
        "review": review,
        "after_review": after_review,
        "package": package,
        "hand_off": hand_off,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", after_prepare, {"index": "index", "end": END})
    graph.add_edge("index", "plan")
    graph.add_conditional_edges("plan", unless_halted("code"), {"code": "code", "hand_off": "hand_off"})
    graph.add_conditional_edges("code", unless_halted("test"), {"test": "test", "hand_off": "hand_off"})
    graph.add_edge("test", "after_test")
    graph.add_conditional_edges(
        "after_test",
        by_next_step,
        {"review": "review", "code": "code", "plan": "plan", "hand_off": "hand_off"},
    )
    graph.add_edge("review", "after_review")
    graph.add_conditional_edges("after_review", by_next_step, {"code": "code", "package": "package"})
    graph.add_edge("package", END)
    graph.add_edge("hand_off", END)
    return graph.compile()


def _brief(output: BaseModel) -> str:
    if isinstance(output, Plan):
        return f"{output.summary} ({len(output.files_to_change)} file(s), {len(output.steps)} step(s))"
    if isinstance(output, ReviewVerdict):
        return f"{output.verdict}: {output.summary} ({len(output.findings)} finding(s))"
    return output.model_dump_json()[:200]


__all__ = [
    "TRANSITIONS",
    "AgentDeps",
    "AgentLimits",
    "AgentState",
    "BudgetExhaustedError",
    "LoopDetectedError",
    "build_graph",
    "tail",
]
