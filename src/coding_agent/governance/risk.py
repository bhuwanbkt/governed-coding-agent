"""Risk level for the human approver: how carefully should this change be reviewed?"""

from __future__ import annotations

from typing import Any, Literal

from coding_agent.governance.policy import Policy, changed_files, changed_lines

RiskLevel = Literal["low", "medium", "high"]


def assess(
    patch: str, policy: Policy, *, injection_flags: list[str], review: dict[str, Any] | None
) -> tuple[RiskLevel, list[str]]:
    files = changed_files(patch)
    lines = changed_lines(patch)
    high: list[str] = []
    medium: list[str] = []
    if injection_flags:
        high.append(f"suspected prompt injection in content the agents read ({len(injection_flags)} hit(s))")
    sensitive = [f for f in files if policy.sensitive_match(f)]
    if sensitive:
        high.append(f"touches sensitive paths: {', '.join(sensitive[:5])}")
    if lines > policy.max_lines // 2:
        high.append(f"large change: {lines} lines")
    elif lines > 100:
        medium.append(f"{lines} lines changed")
    if len(files) > 5:
        medium.append(f"{len(files)} files changed")
    verdict = (review or {}).get("verdict")
    if verdict != "approve":
        medium.append(f"automated review: {verdict or 'none'}")
    if high:
        return "high", high + medium
    if medium:
        return "medium", medium
    return "low", []
