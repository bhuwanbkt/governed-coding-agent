"""Governance policy: one versioned YAML file decides what the agents may change.

Enforced at two points:

- every tool call, by the Toolbox: writes to protected paths and test commands outside the
  allowlist are blocked before they reach the MCP server;
- the packaged diff, before human approval: size limits and protected paths. This also catches
  files created indirectly (for example by a test run) that no tool call wrote.

The policy's version and hash are recorded with every block, so a decision can be traced to the
exact rules in force at the time.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY = Path(__file__).parent / "policies" / "default.yaml"
WRITE_TOOLS = frozenset({"write_file", "replace_in_file"})
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


class PolicyError(ValueError):
    """The policy file is missing or malformed."""


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay within one path segment, `**/` matches any number of folders (or none)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def normalize(path: str) -> str:
    cleaned = posixpath.normpath(path.replace("\\", "/").strip())
    return cleaned.lstrip("/").removeprefix("./")


def changed_files(patch: str) -> list[str]:
    """Paths touched by a git patch (new path for renames; deleted files included)."""
    files: list[str] = []
    for old, new in _DIFF_HEADER.findall(patch):
        for path in (old, new):
            if path not in files:
                files.append(path)
    return files


def changed_lines(patch: str) -> int:
    """Added + removed lines in a unified diff."""
    return sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


@dataclass(frozen=True)
class Policy:
    deny_write: tuple[str, ...]
    sensitive: tuple[str, ...]
    max_files: int
    max_lines: int
    test_commands: tuple[str, ...]
    version: str
    sha256: str

    @property
    def ref(self) -> str:
        return f"policy@v{self.version}#{self.sha256[:8]}"

    @classmethod
    def from_yaml(cls, text: str) -> Policy:
        try:
            data: Any = yaml.safe_load(text) or {}
            paths, diff, tests = data.get("paths", {}), data.get("diff", {}), data.get("tests", {})
            policy = cls(
                deny_write=tuple(str(p) for p in paths.get("deny_write", [])),
                sensitive=tuple(str(p) for p in paths.get("sensitive", [])),
                max_files=int(diff.get("max_files", 20)),
                max_lines=int(diff.get("max_lines", 1000)),
                test_commands=tuple(str(c).strip() for c in tests.get("allowed_commands", [])),
                version=str(data.get("version", "0")),
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        except (yaml.YAMLError, AttributeError, TypeError, ValueError) as exc:
            raise PolicyError(f"invalid policy file: {exc}") from exc
        if policy.max_files < 1 or policy.max_lines < 1 or not policy.test_commands:
            raise PolicyError("policy needs positive diff limits and at least one test command")
        return policy

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        source = path or DEFAULT_POLICY
        try:
            return cls.from_yaml(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyError(f"cannot read policy file {source}: {exc}") from exc

    # ------------------------------------------------------------------ checks
    def _first_match(self, path: str, patterns: tuple[str, ...]) -> str | None:
        target = normalize(path)
        return next((p for p in patterns if glob_to_regex(p).match(target)), None)

    def protected(self, path: str) -> str | None:
        """The deny_write pattern `path` matches, if any."""
        return self._first_match(path, self.deny_write)

    def sensitive_match(self, path: str) -> str | None:
        return self._first_match(path, self.sensitive)

    def test_command_allowed(self, command: str) -> bool:
        command = " ".join(command.split())
        return any(command == allowed or command.startswith(allowed + " ") for allowed in self.test_commands)

    def check_tool_call(self, tool: str, args: dict[str, Any]) -> Violation | None:
        if tool in WRITE_TOOLS:
            path = str(args.get("path", ""))
            if pattern := self.protected(path):
                return Violation("paths.deny_write", f"{path!r} is protected (matches {pattern!r})")
        if tool == "run_tests" and not self.test_command_allowed(str(args.get("command", ""))):
            return Violation(
                "tests.allowed_commands",
                f"{args.get('command')!r} is not allowed (allowed: {', '.join(self.test_commands)})",
            )
        return None

    def check_patch(self, patch: str) -> list[Violation]:
        files = changed_files(patch)
        lines = changed_lines(patch)
        violations = [
            Violation("paths.deny_write", f"the change touches protected file {f!r} (matches {p!r})")
            for f in files
            if (p := self.protected(f))
        ]
        if len(files) > self.max_files:
            violations.append(
                Violation("diff.max_files", f"{len(files)} files changed (limit {self.max_files})")
            )
        if lines > self.max_lines:
            violations.append(Violation("diff.max_lines", f"{lines} lines changed (limit {self.max_lines})"))
        return violations


@lru_cache
def load_policy(path: str | None = None) -> Policy:
    return Policy.load(Path(path) if path else None)
