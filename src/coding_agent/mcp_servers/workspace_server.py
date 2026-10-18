"""Workspace MCP server: sandboxed tools for working on ONE cloned repository.

The agent process starts this server as a stdio subprocess per job. Every tool is confined
to ``$WORKSPACE_DIR/repo``:

- paths are resolved and rejected if they escape the repo or touch ``.git``
- only allow-listed test commands run, with no shell and a scrubbed environment
- the GitHub token is passed to git via an in-memory header, never written to disk,
  and redacted from every output

Run standalone for debugging:
    WORKSPACE_DIR=/tmp/ws python -m coding_agent.mcp_servers.workspace_server
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

server = MCPServer("workspace")

MAX_READ_CHARS = 60_000
MAX_OUTPUT_CHARS = 12_000
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}
DEFAULT_TEST_COMMANDS = "pytest,python -m pytest"
# Only these variables reach the target repo's test process. Secrets never do.
SAFE_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT")
_SHELL_METACHARS = set(";&|`$<>\n")


class ToolError(MCPToolError):
    """Invalid tool use. MCP returns its message to the model as an error result."""


# --------------------------------------------------------------------------- config (read per call)
def _workspace() -> Path:
    return Path(os.environ.get("WORKSPACE_DIR", "workspaces/default")).resolve()


def _repo() -> Path:
    return _workspace() / "repo"


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _allowed_test_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("ALLOWED_TEST_COMMANDS", DEFAULT_TEST_COMMANDS)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# --------------------------------------------------------------------------- helpers
def _require_repo() -> Path:
    """Refuse to work until the clone exists. Without this, Git commands run in a folder with no
    .git would walk up the directory tree and act on whatever repository contains the workspace."""
    repo = _repo()
    if not (repo / ".git").is_dir():
        raise ToolError("repository is not cloned yet; git_clone must succeed first")
    return repo


def _safe_path(rel: str) -> Path:
    repo = _require_repo()
    candidate = (repo / rel).resolve()
    if candidate != repo and repo not in candidate.parents:
        raise ToolError(f"path escapes the repository: {rel}")
    if ".git" in candidate.relative_to(repo).parts:
        raise ToolError("access to .git is not allowed")
    return candidate


def _redact(text: str) -> str:
    token = _token()
    if token:
        text = text.replace(token, "***")
    return re.sub(r"(x-access-token:)[^@\s]+@", r"\1***@", text)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars truncated] ...\n{text[-half:]}"


def _is_github(url: str) -> bool:
    return urlparse(url).hostname in {"github.com", "www.github.com"}


def _git_env(remote_url: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Never let Git discover a repository above the job workspace (defence in depth).
    env["GIT_CEILING_DIRECTORIES"] = str(_workspace())
    token = _token()
    if token and remote_url and _is_github(remote_url):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
        )
    return env


def _git(*args: str, cwd: Path | None = None, remote_url: str | None = None, timeout: int = 300) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed binary, no shell, args are not interpolated
        ["git", *args],  # noqa: S607
        cwd=cwd or _require_repo(),
        env=_git_env(remote_url),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    out = _redact((proc.stdout + proc.stderr).strip())
    if proc.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {out}")
    return out


def _origin_url() -> str:
    return _git("remote", "get-url", "origin")


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        files.extend(Path(dirpath) / f for f in sorted(filenames))
    return files


# --------------------------------------------------------------------------- repo setup
@server.tool()
def git_clone(repo_url: str, base_branch: str = "main", work_branch: str = "agent/change") -> str:
    """Clone the repository into the job workspace and create a fresh working branch."""
    workspace = _workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    repo = _repo()
    if (repo / ".git").is_dir():
        return f"Repository already cloned; on branch {_git('rev-parse', '--abbrev-ref', 'HEAD')}"
    if repo.exists():
        raise ToolError("workspace already contains a non-Git 'repo' folder; use a fresh job workspace")
    _git(
        "clone", "--depth", "50", "--branch", base_branch, repo_url, str(repo),
        cwd=workspace, remote_url=repo_url,
    )  # fmt: skip
    _git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "governed-coding-agent"))
    _git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "agent@users.noreply.github.com"))
    _git("checkout", "-b", work_branch)
    return f"Cloned (base {base_branch}) and switched to branch {work_branch}"


# --------------------------------------------------------------------------- file tools
@server.tool()
def list_files(subdir: str = ".", max_files: int = 300) -> str:
    """List repository files as relative paths, skipping vendored and build directories."""
    root = _safe_path(subdir)
    files = [str(p.relative_to(_repo())) for p in _iter_files(root)]
    if len(files) > max_files:
        return "\n".join(files[:max_files]) + f"\n... ({len(files) - max_files} more files)"
    return "\n".join(files) or "(no files)"


@server.tool()
def read_file(path: str) -> str:
    """Read a UTF-8 text file from the repository."""
    p = _safe_path(path)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    return _truncate(p.read_text(encoding="utf-8", errors="replace"), MAX_READ_CHARS)


@server.tool()
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file with the given full content."""
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


@server.tool()
def replace_in_file(path: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of `old` with `new`. Prefer this for small edits."""
    p = _safe_path(path)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise ToolError(f"expected exactly 1 match for `old`, found {count}; include more context")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"Edited {path}"


@server.tool()
def search_code(pattern: str, max_results: int = 50) -> str:
    """Regex search across repository text files. Returns `path:line: text` lines."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc
    repo = _repo()
    hits: list[str] = []
    for fp in _iter_files(repo):
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{fp.relative_to(repo)}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    return "\n".join(hits)
    return "\n".join(hits) or "(no matches)"


# --------------------------------------------------------------------------- tests
@server.tool()
def run_tests(command: str = "pytest -q", timeout_seconds: int = 300) -> str:
    """Run an allow-listed test command. First line of the result is EXIT_CODE=<n>."""
    cmd = command.strip()
    prefixes = _allowed_test_prefixes()
    if not any(cmd == p or cmd.startswith(p + " ") for p in prefixes) or _SHELL_METACHARS & set(cmd):
        return f"EXIT_CODE=126\nCommand not allowed: {cmd!r}. Allowed: {', '.join(prefixes)}"
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}
    try:
        proc = subprocess.run(  # noqa: S603 - allow-listed command, no shell
            _resolve_interpreter(shlex.split(cmd)),
            cwd=_repo(),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"EXIT_CODE=124\nTests timed out after {timeout_seconds}s"
    except FileNotFoundError as exc:
        return f"EXIT_CODE=127\n{exc}"
    return f"EXIT_CODE={proc.returncode}\n{_truncate(_redact(proc.stdout + proc.stderr))}"


def _resolve_interpreter(argv: list[str]) -> list[str]:
    """Run Python tools with this server's interpreter, not whatever `python` is first on PATH."""
    if argv[0] in {"python", "python3"}:
        return [sys.executable, *argv[1:]]
    if argv[0] == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    return argv


# --------------------------------------------------------------------------- git
@server.tool()
def git_diff() -> str:
    """Show all changes (including new files) on the working branch."""
    _git("add", "-A")
    diff = _git("diff", "--cached")
    if not diff:
        return "(no changes)"
    return _truncate(f"{_git('diff', '--cached', '--stat')}\n\n{diff}")


@server.tool()
def git_commit_and_push(message: str) -> str:
    """Stage all changes, commit with `message`, and push the working branch to origin."""
    _git("add", "-A")
    if not _git("status", "--porcelain"):
        return "NOTHING_TO_COMMIT"
    _git("commit", "-m", message)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    sha = _git("rev-parse", "HEAD")
    _git("push", "-u", "origin", branch, remote_url=_origin_url())
    return f"PUSHED branch={branch} sha={sha}"


@server.tool()
def open_pull_request(title: str, body: str, base_branch: str = "main") -> str:
    """Open a GitHub pull request from the working branch. Returns PR_URL=<url>."""
    origin = _origin_url()
    if not _is_github(origin):
        return "SKIPPED: origin is not a GitHub repository"
    token = _token()
    if not token:
        raise ToolError("GITHUB_TOKEN is not set")
    owner, name = urlparse(origin).path.strip("/").removesuffix(".git").split("/")[:2]
    head = _git("rev-parse", "--abbrev-ref", "HEAD")
    response = httpx.post(
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "head": head, "base": base_branch},
        timeout=30,
    )
    if response.status_code >= 300:
        raise ToolError(f"GitHub API {response.status_code}: {_redact(response.text[:500])}")
    return f"PR_URL={response.json()['html_url']}"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
