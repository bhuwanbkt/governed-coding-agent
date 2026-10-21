"""End to end: real MCP server subprocesses (workspace + code index), real Git, real pytest runs,
real LlamaIndex retrieval, scripted LLMs per agent role.

No network, no API key and no database: the remote is a local bare repo. The database-backed
worker/API flow is covered in tests/integration.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from coding_agent.agent.fake_llm import (
    ScriptedChatModel,
    ScriptedModels,
    demo_coder,
    demo_models,
    submit_review,
    tool_call,
)
from coding_agent.agent.publish import PublishRequest, PublishResult
from coding_agent.agent.roles import READ_TOOLS
from coding_agent.agent.runner import JobRequest, run_coding_job, run_publish_job
from coding_agent.config import Settings

TASK = "Add a divide(a, b) function that raises ValueError on zero, with tests"
JOB_ID = "00000000-0000-4000-8000-0000000000e2"
BROKEN_TEST = "def test_broken() -> None:\n    assert 1 == 2\n"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "llm_provider": "fake",
        "workspaces_dir": tmp_path / "workspaces",
        "allowed_test_commands": "python -m pytest",
        "agent_max_fix_attempts": 1,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def remote_branches(remote_uri: str) -> str:
    return subprocess.run(
        ["git", "branch", "--list"],
        cwd=Path(remote_uri.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def run(
    tmp_path: Path,
    remote: str,
    models: ScriptedModels,
    events: list[dict[str, Any]] | None = None,
    **settings: Any,
) -> dict[str, Any]:
    async def sink(event: dict[str, Any]) -> None:
        if events is not None:
            events.append(event)

    state = asyncio.run(
        run_coding_job(
            JobRequest(repo_url=remote, task=TASK, test_command="python -m pytest -q"),
            make_settings(tmp_path, **settings),
            job_id=JOB_ID,
            models=models,
            audit_sink=sink,
        )
    )
    return dict(state)


def publish_state(tmp_path: Path, state: dict[str, Any], *, patch: str | None = None) -> PublishResult:
    return asyncio.run(
        run_publish_job(
            PublishRequest(
                job_id=state["job_id"],
                repo_url=state["repo_url"],
                base_branch=state["base_branch"],
                work_branch=state["work_branch"],
                task=state["task"],
                patch=state["patch"] if patch is None else patch,
                approved_sha256=state["diff_sha256"],
                approved_by="reviewer",
                plan=state.get("plan"),
                review=state.get("review"),
            ),
            make_settings(tmp_path),
        )
    )


def broken_coder() -> ScriptedChatModel:
    return ScriptedChatModel(
        [
            tool_call("write_file", {"path": "tests/test_broken.py", "content": BROKEN_TEST}, "a1"),
            AIMessage(content="Added a test."),
            AIMessage(content="I could not fix it."),
        ]
    )


# --------------------------------------------------------------------------- happy path
def test_plan_code_test_review_then_stop_for_approval(tmp_path: Path, sample_remote: str) -> None:
    events: list[dict[str, Any]] = []
    state = run(tmp_path, sample_remote, demo_models(), events)

    assert state["status"] == "awaiting_approval", state.get("error")
    assert state["plan"]["files_to_change"][0]["path"] == "calculator.py"
    assert state["review"]["verdict"] == "approve"
    assert state["round"] == 1
    assert "def divide" in state["diff"]
    assert state["patch"].startswith("diff --git")
    assert len(state["diff_sha256"]) == 64
    assert "5 passed" in state["test_output"]
    assert [r.split("#")[0] for r in state["prompt_refs"]] == ["planner@v1", "coder@v2", "reviewer@v1"]
    assert [(r["point"], r["choice"]) for r in state["routes"]] == [
        ("after_test", "review"),
        ("after_review", "package"),
    ]
    assert state["llm_calls"] == 7  # planner 2, coder 4, reviewer 1
    assert state["index_summary"].startswith("INDEXED")
    assert state["work_branch"] not in remote_branches(sample_remote)  # nothing pushed yet
    assert not (tmp_path / "workspaces" / f"{JOB_ID}-code").exists()  # workspace cleaned up

    tools = [e["tool"] for e in events if e["event_type"] == "tool_call"]
    assert tools[:3] == ["git_clone", "list_files", "build_index"]
    assert {"search_codebase", "replace_in_file", "run_tests", "git_diff", "export_patch"} <= set(tools)
    planner_search = next(e for e in events if e.get("tool") == "search_codebase")
    assert planner_search["actor"] == "planner"
    assert planner_search["result_summary"].startswith("1. ")  # real hits; ranking is in test_retrieval
    assert all(e["job_id"] == JOB_ID for e in events)


def test_each_agent_sees_only_its_own_tools(tmp_path: Path, sample_remote: str) -> None:
    models = demo_models()
    run(tmp_path, sample_remote, models)

    assert models.tools_seen["planner"] == sorted({*READ_TOOLS, "submit_plan"})
    assert models.tools_seen["reviewer"] == sorted({*READ_TOOLS, "git_diff", "submit_review"})
    assert "write_file" in models.tools_seen["coder"]
    assert not {"git_commit_and_push", "run_tests", "export_patch"} & set(models.tools_seen["coder"])


def test_approved_patch_is_published_from_a_fresh_clone(tmp_path: Path, sample_remote: str) -> None:
    state = run(tmp_path, sample_remote, demo_models())
    result = publish_state(tmp_path, state)

    assert result.status == "succeeded", result.error
    assert result.pr_url is None  # local remote: PR step is skipped, push still happens
    assert state["work_branch"] in remote_branches(sample_remote)
    shown = subprocess.run(
        ["git", "show", f"{state['work_branch']}:calculator.py"],
        cwd=Path(sample_remote.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "def divide" in shown


def test_tampered_patch_is_refused(tmp_path: Path, sample_remote: str) -> None:
    state = run(tmp_path, sample_remote, demo_models())
    tampered = state["patch"].replace("cannot divide by zero", "anything goes")
    result = publish_state(tmp_path, state, patch=tampered)

    assert result.status == "failed"
    assert "does not match the approved SHA-256" in str(result.error)
    assert state["work_branch"] not in remote_branches(sample_remote)


# --------------------------------------------------------------------------- loops and limits
def test_failing_tests_retry_then_hand_off(tmp_path: Path, sample_remote: str) -> None:
    coder = broken_coder()
    state = run(tmp_path, sample_remote, ScriptedModels(coder=coder))

    assert state["status"] == "handed_off"
    assert state["round"] == 2  # first round + 1 fix (agent_max_fix_attempts=1)
    assert "patch" not in state
    assert state["review"] is None  # failing code never reaches the reviewer
    retry_prompt = str(coder.received[-1][0].content)
    assert "Previous attempt failed (round 2)" in retry_prompt
    assert [r["choice"] for r in state["routes"]] == ["code", "hand_off"]


def test_reviewer_findings_go_back_to_the_coder(tmp_path: Path, sample_remote: str) -> None:
    finding = {
        "severity": "medium",
        "file": "calculator.py",
        "line": 15,
        "issue": "divide() has no docstring, unlike the module's style",
        "suggestion": "Add a one-line docstring",
    }
    coder = demo_coder()
    coder._responses.extend(  # second round: address the finding
        [
            tool_call(
                "replace_in_file",
                {
                    "path": "calculator.py",
                    "old": "def divide(a: float, b: float) -> float:\n",
                    "new": 'def divide(a: float, b: float) -> float:\n    """Divide a by b."""\n',
                },
                "r2",
            ),
            AIMessage(content="Added the docstring."),
        ]
    )
    reviewer = ScriptedChatModel([submit_review("request_changes", [finding]), submit_review("approve")])
    state = run(tmp_path, sample_remote, ScriptedModels(coder=coder, reviewer=reviewer))

    assert state["status"] == "awaiting_approval", state.get("error")
    assert state["round"] == 2
    assert state["review_rounds"] == 1
    assert state["review"]["verdict"] == "approve"
    assert '"""Divide a by b."""' in state["diff"]
    second_round_prompt = str(coder.received[-1][0].content)
    assert "The Reviewer requested changes (round 2)" in second_round_prompt
    assert "no docstring" in second_round_prompt
    assert [r["choice"] for r in state["routes"]] == ["review", "code", "review", "package"]


def test_unresolved_review_still_reaches_the_human_flagged(tmp_path: Path, sample_remote: str) -> None:
    finding = {"severity": "high", "file": "calculator.py", "issue": "made-up problem"}
    reviewer = ScriptedChatModel([submit_review("request_changes", [finding])] * 2)
    state = run(
        tmp_path,
        sample_remote,
        ScriptedModels(coder=demo_coder(), reviewer=reviewer),
        agent_max_review_rounds=0,
    )

    assert state["status"] == "awaiting_approval"  # the human decides, with the objection attached
    assert state["review"]["verdict"] == "request_changes"


def test_llm_call_budget_hands_off(tmp_path: Path, sample_remote: str) -> None:
    state = run(tmp_path, sample_remote, demo_models(), agent_max_llm_calls=3)

    assert state["status"] == "handed_off"
    assert "budget exhausted" in str(state["error"])
    assert state["llm_calls"] == 3


def test_loop_detection_hands_off(tmp_path: Path, sample_remote: str) -> None:
    coder = ScriptedChatModel([tool_call("list_files", {}, f"l{i}") for i in range(4)])
    state = run(tmp_path, sample_remote, ScriptedModels(coder=coder))

    assert state["status"] == "handed_off"
    assert "too many times" in str(state["error"])
    assert state["llm_calls"] == 5  # planner 1 + coder 4: usage survives the early stop


def test_rereading_a_file_after_each_edit_is_not_a_loop(tmp_path: Path, sample_remote: str) -> None:
    """Regression (found in production): the coder read calculator.py after every edit, the 4th
    identical read_file tripped loop detection and the job was handed off."""

    def edit(n: int) -> AIMessage:
        return tool_call(
            "replace_in_file",
            {"path": "calculator.py", "old": "    return a * b\n", "new": f"    return a * b  # {n}\n"},
            f"e{n}",
        )

    read = {"path": "calculator.py"}
    coder = ScriptedChatModel(
        [
            tool_call("read_file", read, "r1"),
            edit(1),
            tool_call("read_file", read, "r2"),
            tool_call("replace_in_file", {"path": "calculator.py", "old": "  # 1\n", "new": "  # 2\n"}, "e2"),
            tool_call("read_file", read, "r3"),
            tool_call("replace_in_file", {"path": "calculator.py", "old": "  # 2\n", "new": "  # 3\n"}, "e3"),
            tool_call("read_file", read, "r4"),
            AIMessage(content="Edited calculator.py three times."),
        ]
    )
    state = run(tmp_path, sample_remote, ScriptedModels(coder=coder))

    assert state["status"] == "awaiting_approval", state.get("error")
    assert "# 3" in state["diff"]


# --------------------------------------------------------------------------- permissions
def test_coder_cannot_use_publisher_tools(tmp_path: Path, sample_remote: str) -> None:
    coder = ScriptedChatModel(
        [tool_call("git_commit_and_push", {"message": "sneaky"}, "x1"), AIMessage(content="Tried to push.")]
    )
    events: list[dict[str, Any]] = []
    state = run(tmp_path, sample_remote, ScriptedModels(coder=coder), events)

    assert "not allowed" in str(coder.received[1][-1].content)
    assert state["status"] == "no_changes"
    assert any(e["event_type"] == "policy_block" and e["actor"] == "coder" for e in events)


def test_reviewer_cannot_edit_files(tmp_path: Path, sample_remote: str) -> None:
    reviewer = ScriptedChatModel(
        [
            tool_call("write_file", {"path": "calculator.py", "content": "hacked"}, "w1"),
            submit_review("approve"),
        ]
    )
    events: list[dict[str, Any]] = []
    state = run(tmp_path, sample_remote, ScriptedModels(coder=demo_coder(), reviewer=reviewer), events)

    assert "not allowed" in str(reviewer.received[1][-1].content)
    assert "hacked" not in state["diff"]
    assert any(e["event_type"] == "policy_block" and e["actor"] == "reviewer" for e in events)


def test_clone_failure_stops_before_any_llm_call(tmp_path: Path) -> None:
    models = demo_models()
    state = run(tmp_path, (tmp_path / "missing.git").as_uri(), models)

    assert state["status"] == "failed"
    assert "Could not clone" in str(state["error"])
    assert all(model.received == [] for model in models.scripts.values())


# --------------------------------------------------------------------------- LLM supervisor
def choose(next_step: str) -> AIMessage:
    return tool_call("choose_next_step", {"next": next_step, "reason": "scripted"}, "route")


def test_llm_supervisor_proposal_outside_policy_is_blocked(tmp_path: Path, sample_remote: str) -> None:
    supervisor = ScriptedChatModel([choose("package")])  # tests are failing: not allowed
    events: list[dict[str, Any]] = []
    state = run(
        tmp_path,
        sample_remote,
        ScriptedModels(coder=broken_coder(), supervisor=supervisor),
        events,
        supervisor_mode="llm",
    )

    first = state["routes"][0]
    assert (first["choice"], first["source"]) == ("code", "policy")
    blocks = [e for e in events if e["event_type"] == "policy_block" and e["actor"] == "supervisor"]
    assert "'package' is not allowed" in blocks[0]["result_summary"]
    assert state["status"] == "handed_off"
    assert "patch" not in state


def test_llm_supervisor_valid_choice_is_followed(tmp_path: Path, sample_remote: str) -> None:
    supervisor = ScriptedChatModel([choose("hand_off")])
    state = run(
        tmp_path,
        sample_remote,
        ScriptedModels(coder=broken_coder(), supervisor=supervisor),
        supervisor_mode="llm",
    )

    assert state["round"] == 1  # supervisor stopped after the first failure
    assert (state["routes"][0]["choice"], state["routes"][0]["source"]) == ("hand_off", "llm")
    assert state["status"] == "handed_off"


def test_llm_supervisor_can_ask_for_a_replan(tmp_path: Path, sample_remote: str) -> None:
    supervisor = ScriptedChatModel([choose("plan")])
    state = run(
        tmp_path,
        sample_remote,
        ScriptedModels(coder=broken_coder(), supervisor=supervisor),
        supervisor_mode="llm",
    )

    assert state["replans"] == 1
    assert [r.split("@")[0] for r in state["prompt_refs"]].count("planner") == 2
    assert state["status"] == "handed_off"


# --------------------------------------------------------------------------- model errors
class FlakyModel:
    """Fails the way Groq does when the model emits a malformed tool call, then behaves."""

    def __init__(self, inner: ScriptedChatModel, failures: int) -> None:
        self.inner, self.failures = inner, failures

    async def ainvoke(self, input: list[Any], /) -> Any:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Error code: 400 - {'error': {'code': 'tool_use_failed'}}")
        return await self.inner.ainvoke(input)


def test_model_error_is_retried_with_guidance(tmp_path: Path, sample_remote: str) -> None:
    """Regression (found in production): a provider 400 during the coder's turn crashed the job
    with an unreadable ExceptionGroup."""
    coder = demo_coder()
    models = ScriptedModels()
    models.scripts["coder"] = FlakyModel(coder, failures=1)  # type: ignore[assignment]
    state = run(tmp_path, sample_remote, models)

    assert state["status"] == "awaiting_approval", state.get("error")
    first_retry = coder.received[0]
    assert "could not be processed" in str(first_retry[-1].content)


def test_model_that_keeps_failing_hands_off_with_a_clear_reason(tmp_path: Path, sample_remote: str) -> None:
    models = ScriptedModels()
    models.scripts["coder"] = FlakyModel(demo_coder(), failures=99)  # type: ignore[assignment]
    state = run(tmp_path, sample_remote, models)

    assert state["status"] == "handed_off"
    assert "coder: the model failed 3 times in a row" in str(state["error"])
    assert "tool_use_failed" in str(state["error"])
    assert "ExceptionGroup" not in str(state["error"])
