"""Mask secrets and personal data before anything is stored or shown.

Two levels:

- `find_secrets`: high-confidence credentials only (known token formats, private keys). Used to
  stop a job whose patch would commit a secret; false positives there would block real work.
- `redact`: everything above plus looser patterns (password assignments, credentials in URLs,
  emails, phone numbers). Used on audit events, test output, summaries and PR bodies, where
  over-masking is cheap and a leak is not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# (kind, pattern) — high confidence: specific token formats
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("llm_api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

# Looser patterns, for masking only
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<password>[^/\s@]+)@", re.I
)
_ASSIGNMENT = re.compile(
    r"(?P<key>\b[\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|token)[\w.-]*)"
    r"(?P<sep>\s*[:=]\s*)(?P<quote>['\"]?)(?P<value>[^\s'\"]{6,})(?P=quote)",
    re.I,
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w+])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)")


def find_secrets(text: str) -> list[str]:
    """Kinds of high-confidence secrets present in `text` (empty list if none)."""
    return [kind for kind, pattern in SECRET_PATTERNS if pattern.search(text)]


def redact(text: str) -> str:
    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m['scheme']}{m['user']}:[REDACTED]@", text)
    text = _ASSIGNMENT.sub(
        lambda m: (
            m.group(0)
            if m["value"].startswith("[REDACTED")
            else f"{m['key']}{m['sep']}{m['quote']}[REDACTED]{m['quote']}"
        ),
        text,
    )
    text = _EMAIL.sub("[REDACTED:email]", text)
    return _PHONE.sub("[REDACTED:phone]", text)


def redact_value(value: Any) -> Any:
    """Redact strings inside nested dicts/lists (e.g. tool arguments)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value
