"""End to end: real MCP server subprocess, real Git, real pytest runs, scripted LLM.

No network and no API key: the LLM is `ScriptedChatModel`, the remote is a local bare repo.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from coding_agent.agent.fake_llm import ScriptedChatModel, demo_model, tool_call
from coding_agent.agent.graph import ApprovalRequest
from coding_agent.agent.runner import JobRequest, run_job
from coding_agent.config import Settings

TASK = "Add a divide(a, b) function that raises ValueError on zero, with tests"


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        workspaces_dir=tmp_path / "workspaces",
        allowed_test_commands="python -m pytest",
        agent_max_fix_attempts=1,
    )


def _remote_branches(remote_uri: str) -> str:
    return subprocess.run(
        ["git", "branch", "--list"],
        cwd=Path(remote_uri.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _run(
    tmp_path: Path, remote: str, model: ScriptedChatModel, answer: bool
) -> tuple[dict[str, object], list[ApprovalRequest]]:
    seen: list[ApprovalRequest] = []

    async def approver(request: ApprovalRequest) -> bool:
        seen.append(request)
        return answer

    final = asyncio.run(
        run_job(
            JobRequest(repo_url=remote, task=TASK, test_command="python -m pytest -q"),
            _settings(tmp_path),
            approver,
            job_id="e2e",
            model=model,
        )
    )
    return dict(final), seen


def test_happy_path_pushes_branch_after_approval(tmp_path: Path, sample_remote: str) -> None:
    final, approvals = _run(tmp_path, sample_remote, demo_model(), answer=True)

    assert final["status"] == "succeeded", final.get("error")
    assert final["test_exit_code"] == 0
    assert final["attempt"] == 1
    assert len(approvals) == 1
    assert "def divide" in approvals[0].diff
    assert "5 passed" in approvals[0].test_output
    assert "agent/e2e" in _remote_branches(sample_remote)
    assert final["pr_url"] is None  # local remote: PR step is skipped, push still happens
    assert len(final["prompt_refs"]) == 1  # type: ignore[arg-type]
    assert str(final["prompt_refs"][0]).startswith("coder@v1#")


def test_rejection_pushes_nothing(tmp_path: Path, sample_remote: str) -> None:
    final, approvals = _run(tmp_path, sample_remote, demo_model(), answer=False)

    assert final["status"] == "rejected"
    assert len(approvals) == 1
    assert "agent/e2e" not in _remote_branches(sample_remote)


def test_failing_tests_retry_then_hand_off(tmp_path: Path, sample_remote: str) -> None:
    broken = "def test_broken() -> None:\n    assert 1 == 2\n"
    model = ScriptedChatModel(
        [
            tool_call("write_file", {"path": "tests/test_broken.py", "content": broken}, "a1"),
            AIMessage(content="Added a test."),
            # second attempt: the model gets the failure output but makes no fix
            AIMessage(content="I could not fix it."),
        ]
    )
    final, approvals = _run(tmp_path, sample_remote, model, answer=True)

    assert final["status"] == "handed_off"
    assert final["attempt"] == 2  # first attempt + 1 fix (agent_max_fix_attempts=1)
    assert approvals == []
    assert "agent/e2e" not in _remote_branches(sample_remote)
    retry_prompt = model.received[-1][0].content
    assert "Previous attempt failed (attempt 2 of 2)" in str(retry_prompt)


def test_coder_cannot_use_publisher_tools(tmp_path: Path, sample_remote: str) -> None:
    model = ScriptedChatModel(
        [
            tool_call("git_commit_and_push", {"message": "sneaky"}, "x1"),
            AIMessage(content="Tried to push."),
        ]
    )
    final, _ = _run(tmp_path, sample_remote, model, answer=False)

    tool_reply = model.received[1][-1]
    assert "not allowed" in str(tool_reply.content)
    assert "agent/e2e" not in _remote_branches(sample_remote)
    assert final["status"] == "no_changes"


@pytest.mark.parametrize("repeats", [4])
def test_loop_detection_hands_off(tmp_path: Path, sample_remote: str, repeats: int) -> None:
    model = ScriptedChatModel([tool_call("list_files", {}, f"l{i}") for i in range(repeats)])
    final, _ = _run(tmp_path, sample_remote, model, answer=True)

    assert final["status"] == "handed_off"
    assert "too many times" in str(final["error"])


def test_clone_failure_stops_before_any_edit(tmp_path: Path) -> None:
    """Regression: a failed clone must end the job, not let the coder write files elsewhere."""
    model = demo_model()
    final, approvals = _run(tmp_path, (tmp_path / "missing.git").as_uri(), model, answer=True)

    assert final["status"] == "failed"
    assert "Could not clone" in str(final["error"])
    assert model.received == []  # the LLM was never called
    assert approvals == []
