"""End to end: real MCP server subprocess, real Git, real pytest runs, scripted LLM.

No network, no API key and no database: the LLM is `ScriptedChatModel`, the remote is a local
bare repo. The database-backed worker/API flow is covered in tests/integration.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from coding_agent.agent.fake_llm import ScriptedChatModel, demo_model, tool_call
from coding_agent.agent.publish import PublishRequest, PublishResult
from coding_agent.agent.runner import JobRequest, run_coding_job, run_publish_job
from coding_agent.config import Settings

TASK = "Add a divide(a, b) function that raises ValueError on zero, with tests"
JOB_ID = "00000000-0000-4000-8000-0000000000e2"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        workspaces_dir=tmp_path / "workspaces",
        allowed_test_commands="python -m pytest",
        agent_max_fix_attempts=1,
    )


def remote_branches(remote_uri: str) -> str:
    return subprocess.run(
        ["git", "branch", "--list"],
        cwd=Path(remote_uri.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def code(tmp_path: Path, remote: str, model: ScriptedChatModel) -> dict[str, Any]:
    state = asyncio.run(
        run_coding_job(
            JobRequest(repo_url=remote, task=TASK, test_command="python -m pytest -q"),
            make_settings(tmp_path),
            job_id=JOB_ID,
            model=model,
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
            ),
            make_settings(tmp_path),
        )
    )


def test_coding_stops_for_approval_with_exact_patch(tmp_path: Path, sample_remote: str) -> None:
    state = code(tmp_path, sample_remote, demo_model())

    assert state["status"] == "awaiting_approval", state.get("error")
    assert state["attempt"] == 1
    assert "def divide" in state["diff"]
    assert state["patch"].startswith("diff --git")
    assert len(state["diff_sha256"]) == 64
    assert "5 passed" in state["test_output"]
    assert str(state["prompt_refs"][0]).startswith("coder@v1#")
    assert state["work_branch"] not in remote_branches(sample_remote)  # nothing pushed yet
    assert not (tmp_path / "workspaces" / f"{JOB_ID}-code").exists()  # workspace cleaned up


def test_approved_patch_is_published_from_a_fresh_clone(tmp_path: Path, sample_remote: str) -> None:
    state = code(tmp_path, sample_remote, demo_model())
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
    state = code(tmp_path, sample_remote, demo_model())
    tampered = state["patch"].replace("cannot divide by zero", "anything goes")
    result = publish_state(tmp_path, state, patch=tampered)

    assert result.status == "failed"
    assert "does not match the approved SHA-256" in str(result.error)
    assert state["work_branch"] not in remote_branches(sample_remote)


def test_failing_tests_retry_then_hand_off(tmp_path: Path, sample_remote: str) -> None:
    broken = "def test_broken() -> None:\n    assert 1 == 2\n"
    model = ScriptedChatModel(
        [
            tool_call("write_file", {"path": "tests/test_broken.py", "content": broken}, "a1"),
            AIMessage(content="Added a test."),
            AIMessage(content="I could not fix it."),  # second attempt makes no fix
        ]
    )
    state = code(tmp_path, sample_remote, model)

    assert state["status"] == "handed_off"
    assert state["attempt"] == 2  # first attempt + 1 fix (agent_max_fix_attempts=1)
    assert "patch" not in state
    retry_prompt = model.received[-1][0].content
    assert "Previous attempt failed (attempt 2 of 2)" in str(retry_prompt)


def test_coder_cannot_use_publisher_tools(tmp_path: Path, sample_remote: str) -> None:
    model = ScriptedChatModel(
        [
            tool_call("git_commit_and_push", {"message": "sneaky"}, "x1"),
            AIMessage(content="Tried to push."),
        ]
    )
    state = code(tmp_path, sample_remote, model)

    tool_reply = model.received[1][-1]
    assert "not allowed" in str(tool_reply.content)
    assert state["status"] == "no_changes"


def test_loop_detection_hands_off(tmp_path: Path, sample_remote: str) -> None:
    model = ScriptedChatModel([tool_call("list_files", {}, f"l{i}") for i in range(4)])
    state = code(tmp_path, sample_remote, model)

    assert state["status"] == "handed_off"
    assert "too many times" in str(state["error"])


def test_clone_failure_stops_before_any_edit(tmp_path: Path) -> None:
    model = demo_model()
    state = code(tmp_path, (tmp_path / "missing.git").as_uri(), model)

    assert state["status"] == "failed"
    assert "Could not clone" in str(state["error"])
    assert model.received == []  # the LLM was never called


def test_audit_sink_receives_every_tool_call(tmp_path: Path, sample_remote: str) -> None:
    events: list[dict[str, Any]] = []

    async def sink(event: dict[str, Any]) -> None:
        events.append(event)

    asyncio.run(
        run_coding_job(
            JobRequest(repo_url=sample_remote, task=TASK, test_command="python -m pytest -q"),
            make_settings(tmp_path),
            job_id=JOB_ID,
            model=demo_model(),
            audit_sink=sink,
        )
    )
    tools = [e["tool"] for e in events]
    assert tools[:2] == ["git_clone", "list_files"]
    assert {"replace_in_file", "run_tests", "export_patch"} <= set(tools)
    assert all(e["job_id"] == JOB_ID for e in events)
