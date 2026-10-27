from __future__ import annotations

import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from coding_agent.config import Settings
from coding_agent.governance.github_app import (
    GitHubAppError,
    app_jwt,
    installation_token,
    parse_github_repo,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
REPO = "https://github.com/bhuwanbkt/agent-sandbox-repo.git"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (REPO, ("bhuwanbkt", "agent-sandbox-repo")),
        ("https://github.com/o/r", ("o", "r")),
        ("https://gitlab.com/o/r.git", None),
        ("file:///tmp/remote.git", None),
        ("https://github.com/o", None),
    ],
)
def test_parse_github_repo(url: str, expected: tuple[str, str] | None) -> None:
    assert parse_github_repo(url) == expected


def test_app_jwt_is_short_lived_and_signed() -> None:
    token = app_jwt("12345", PEM, now=1_000_000)
    claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 10 * 60


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="https://api.github.com", transport=handler)


def test_installation_token_is_scoped_to_one_repo() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"].startswith("Bearer ")
        if request.url.path == "/repos/bhuwanbkt/agent-sandbox-repo/installation":
            return httpx.Response(200, json={"id": 77})
        if request.url.path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2026-10-21T01:00:00Z"})
        return httpx.Response(404)

    issued = installation_token("12345", PEM, REPO, client=_client(httpx.MockTransport(handle)))

    assert issued.token == "ghs_test"
    assert issued.installation_id == 77
    assert issued.repository == "bhuwanbkt/agent-sandbox-repo"
    body = json.loads(seen[1].content)
    assert body["repositories"] == ["agent-sandbox-repo"]
    assert body["permissions"]["contents"] == "write"
    assert set(body["permissions"]) == {"contents", "pull_requests", "metadata"}


def test_coding_runs_get_a_read_only_token() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2026-10-21T01:00:00Z"})

    installation_token("12345", PEM, REPO, write=False, client=_client(httpx.MockTransport(handle)))
    assert json.loads(seen[1].content)["permissions"] == {"contents": "read", "metadata": "read"}


def test_app_not_installed_is_a_clear_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(GitHubAppError, match="not installed"):
        installation_token("1", PEM, REPO, client=_client(transport))


def test_non_github_urls_are_refused() -> None:
    with pytest.raises(GitHubAppError, match=r"not a github\.com"):
        installation_token("1", PEM, "https://gitlab.com/o/r.git")


def test_settings_accept_escaped_newlines_and_require_both_values() -> None:
    escaped = PEM.replace("\n", "\\n")
    settings = Settings(_env_file=None, github_app_id="1", github_app_private_key=escaped)  # type: ignore[call-arg]
    assert settings.app_private_key() == PEM
    with pytest.raises(ValueError, match="GITHUB_APP_ID"):
        Settings(_env_file=None, github_app_id="1")  # type: ignore[call-arg]
