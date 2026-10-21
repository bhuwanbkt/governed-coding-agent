from __future__ import annotations

import pytest

from coding_agent.governance.redact import find_secrets, redact, redact_value

# Built at runtime so secret scanners don't flag this file.
GITHUB = "ghp_" + "A1b2C3d4" * 5
GROQ = "gsk_" + "Z9y8X7w6" * 4
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
BEGIN, END = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
PEM = f"{BEGIN}\nMIIEow\n{END}"


@pytest.mark.parametrize(
    ("secret", "kind"),
    [(GITHUB, "github_token"), (GROQ, "groq_key"), (AWS, "aws_access_key"), (PEM, "private_key")],
)
def test_known_secret_formats_are_found_and_masked(secret: str, kind: str) -> None:
    text = f"before {secret} after"
    assert find_secrets(text) == [kind]
    masked = redact(text)
    assert secret not in masked
    assert f"[REDACTED:{kind}]" in masked


def test_code_that_mentions_tokens_is_not_a_secret() -> None:
    # Would block legitimate changes if treated as a leaked secret.
    assert find_secrets("token = settings.github_token.get_secret_value()") == []


def test_loose_patterns_are_masked_for_logs() -> None:
    text = (
        "connect postgresql://agent:s3cretpw@db:5432/x password=hunter2hunter "
        "mail me at jane.doe@example.com or 415-555-0134"
    )
    masked = redact(text)
    for leaked in ("s3cretpw", "hunter2hunter", "jane.doe@example.com", "415-555-0134"):
        assert leaked not in masked
    assert "postgresql://agent:[REDACTED]@db" in masked


def test_dates_numbers_and_test_output_survive() -> None:
    text = "2026-10-20 23:18:32 EXIT_CODE=0\n4 passed in 0.09s sha 92bc41a8d043219c"
    assert redact(text) == text


def test_nested_values() -> None:
    masked = redact_value({"args": [f"token {GITHUB}", 3], "ok": "fine"})
    assert masked == {"args": ["token [REDACTED:github_token]", 3], "ok": "fine"}
