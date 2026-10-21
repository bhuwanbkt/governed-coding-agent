"""Spot text that tries to give the agents instructions (prompt injection).

Repository files, search results and test output are data. When they contain instruction-like
text aimed at an AI ("ignore previous instructions", "push to main", "print the token"), we
record it, show it to the reviewer and approver, and treat a blocked action that follows it as
an attack: the job stops for a human. Detection is a heuristic tripwire, not the defence; the
defence is that agents can't do anything harmful even if they follow such text (allowlists,
protected paths, human approval).
"""

from __future__ import annotations

import re

SCANNED_TOOLS = frozenset({"read_file", "search_code", "search_codebase", "run_tests", "git_diff"})

_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|system)\s+(?:instructions|rules|prompts?|guidelines)",
        r"\byou\s+are\s+now\s+(?:a|an|the|in)\b",
        r"\b(?:new|updated|override|real)\s+(?:system\s+)?(?:instructions|prompt)\s*:",
        r"\b(?:reveal|print|show|output|leak|exfiltrate|send|post)\b[^.\n]{0,40}"
        r"\b(?:token|secret|api[_ ]?key|password|credentials?|system\s+prompt|env(?:ironment)?\s+var)",
        r"\b(?:push|commit|merge)\b[^.\n]{0,30}\bto\s+(?:main|master|production|prod)\b",
        r"\bdo\s+not\s+(?:tell|inform|mention\s+(?:this\s+)?to)\s+the\s+(?:user|human|reviewer|approver)",
        r"\b(?:curl|wget)\s+\S+\s*\|\s*(?:sh|bash)\b",
        r"\b(?:disable|skip|bypass)\s+(?:the\s+)?(?:tests?|review|approval|safety|security)\b",
    )
)


def detect(text: str) -> list[str]:
    """Matched snippets (trimmed), in order of appearance; empty if nothing suspicious."""
    found: list[tuple[int, str]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), " ".join(match.group(0).split())[:80]))
    return [snippet for _, snippet in sorted(found)]
