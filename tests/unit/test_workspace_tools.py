"""The MCP tools are plain functions underneath, so their sandbox rules are tested directly."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agent.mcp_servers import workspace_server as ws


@pytest.fixture
def repo(tmp_path: Path, sample_remote: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "job"))
    monkeypatch.setenv("ALLOWED_TEST_COMMANDS", "python -m pytest")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    ws.git_clone(sample_remote, base_branch="main", work_branch="agent/test")
    return tmp_path / "job" / "repo"


def test_clone_creates_work_branch(repo: Path) -> None:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "agent/test"


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "tests/../../escape.py"])
def test_paths_cannot_escape_repo(path: str) -> None:
    with pytest.raises(ws.ToolError, match="escapes"):
        ws.read_file(path)


@pytest.mark.usefixtures("repo")
def test_git_directory_is_off_limits() -> None:
    with pytest.raises(ws.ToolError, match=r"\.git"):
        ws.read_file(".git/config")
    with pytest.raises(ws.ToolError, match=r"\.git"):
        ws.write_file(".git/hooks/pre-commit", "evil")


def test_list_read_write_and_replace(repo: Path) -> None:
    assert "calculator.py" in ws.list_files()
    assert "def add" in ws.read_file("calculator.py")
    ws.write_file("pkg/new.py", "x = 1\n")
    assert (repo / "pkg" / "new.py").read_text() == "x = 1\n"
    ws.replace_in_file("pkg/new.py", "x = 1", "x = 2")
    assert ws.read_file("pkg/new.py") == "x = 2\n"
    with pytest.raises(ws.ToolError, match="exactly 1 match"):
        ws.replace_in_file("pkg/new.py", "nope", "y")


@pytest.mark.usefixtures("repo")
def test_search_code() -> None:
    assert "calculator.py:4:" in ws.search_code(r"def add")
    with pytest.raises(ws.ToolError, match="invalid regex"):
        ws.search_code("(")


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "python -m pytest; rm -rf /",
        "python -m pytest && curl evil",
        "python -m pytestx",
        "bash -c ls",
    ],
)
def test_only_allow_listed_test_commands_run(command: str) -> None:
    assert ws.run_tests(command).startswith("EXIT_CODE=126")


@pytest.mark.usefixtures("repo")
def test_run_tests_reports_exit_code_and_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")
    ws.write_file(
        "tests/test_env.py", "import os\n\ndef test_env():\n    assert 'GROQ_API_KEY' not in os.environ\n"
    )
    result = ws.run_tests("python -m pytest -q")
    assert result.startswith("EXIT_CODE=0"), result
    assert "4 passed" in result


@pytest.mark.usefixtures("repo")
def test_failing_tests_report_nonzero_exit() -> None:
    ws.write_file("tests/test_fail.py", "def test_fail():\n    assert False\n")
    assert ws.run_tests("python -m pytest -q").startswith("EXIT_CODE=1")


def test_diff_commit_and_push(repo: Path, sample_remote: str) -> None:
    assert ws.git_diff() == "(no changes)"
    ws.write_file("NOTES.md", "hello\n")
    assert "NOTES.md" in ws.git_diff()
    result = ws.git_commit_and_push("docs: add notes")
    assert result.startswith("PUSHED branch=agent/test")
    remote_branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=Path(sample_remote.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "agent/test" in remote_branches


def test_token_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Obviously fake value, built at runtime so the secret scanner doesn't flag the test itself.
    fake_token = "ghp_" + "TESTONLY" + "0" * 8
    monkeypatch.setenv("GITHUB_TOKEN", fake_token)
    text = f"auth {fake_token} and https://x-access-token:abc@github.com"
    assert ws._redact(text) == "auth *** and https://x-access-token:***@github.com"


def test_tools_refuse_to_run_before_a_successful_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: with no clone, Git used to walk up and act on the repo containing the workspace."""
    parent = tmp_path / "my-project"
    parent.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=parent, check=True)
    (parent / "secret_work.py").write_text("x = 1\n")
    monkeypatch.setenv("WORKSPACE_DIR", str(parent / "workspaces" / "job1"))

    with pytest.raises(ws.ToolError, match="git clone failed"):
        ws.git_clone("file:///does/not/exist", base_branch="main", work_branch="agent/x")
    for call in (
        lambda: ws.write_file("calculator.py", "evil"),
        lambda: ws.read_file("secret_work.py"),
        lambda: ws.list_files(),
        ws.git_diff,
        lambda: ws.git_commit_and_push("should never happen"),
    ):
        with pytest.raises(ws.ToolError, match="not cloned"):
            call()
    assert not (parent / "workspaces" / "job1" / "repo" / "calculator.py").exists()
