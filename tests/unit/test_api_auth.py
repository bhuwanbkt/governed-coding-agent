from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coding_agent.api.main import create_app
from coding_agent.config import Settings, get_settings

# Obviously fake, low-entropy test keys (built at runtime so secret scanners stay quiet).
ALICE_KEY = "alice-" + "a" * 20
BOB_KEY = "bob-" + "b" * 20
WRONG_KEY = "wrong-" + "w" * 20
BODY = {"repo_url": "https://github.com/bhuwanbkt/agent-sandbox-repo.git", "task": "Add a divide function"}


def test_api_keys_parse_to_users() -> None:
    settings = Settings(_env_file=None, api_keys=f"alice:{ALICE_KEY}, bob:{BOB_KEY}")  # type: ignore[call-arg]
    assert settings.api_users() == {ALICE_KEY: "alice", BOB_KEY: "bob"}


@pytest.mark.parametrize("raw", ["alice", "alice:short", ":" + "n" * 20])
def test_malformed_api_keys_fail_fast(raw: str) -> None:
    with pytest.raises(ValidationError, match="API_KEYS"):
        Settings(_env_file=None, api_keys=raw)  # type: ignore[call-arg]


def test_repo_allowlist() -> None:
    settings = Settings(_env_file=None, allowed_repo_prefixes="https://github.com/bhuwanbkt/")  # type: ignore[call-arg]
    assert settings.repo_allowed("https://github.com/bhuwanbkt/agent-sandbox-repo.git")
    assert not settings.repo_allowed("https://github.com/someone-else/repo.git")
    assert Settings(_env_file=None).repo_allowed("anything")  # type: ignore[call-arg]  # empty = allow all (dev)


def test_production_requires_repo_allowlist() -> None:
    with pytest.raises(ValidationError, match="ALLOWED_REPO_PREFIXES"):
        Settings(_env_file=None, app_env="production", database_url="postgresql://u:p@h/db")  # type: ignore[call-arg]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("API_KEYS", f"alice:{ALICE_KEY}")
    monkeypatch.setenv("ALLOWED_REPO_PREFIXES", "https://github.com/bhuwanbkt/")
    get_settings.cache_clear()
    return TestClient(create_app())


def test_requests_without_a_key_are_rejected(client: TestClient) -> None:
    assert client.post("/jobs", json=BODY).status_code == 401
    assert client.post("/jobs", json=BODY, headers={"X-API-Key": WRONG_KEY}).status_code == 401


def test_repo_outside_allowlist_is_forbidden(client: TestClient) -> None:
    body = {**BODY, "repo_url": "https://github.com/someone-else/repo.git"}
    response = client.post("/jobs", json=body, headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 403


def test_invalid_body_is_rejected(client: TestClient) -> None:
    response = client.post("/jobs", json={**BODY, "task": "  "}, headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 422


def test_bare_allowlist_prefix_is_not_a_repository(client: TestClient) -> None:
    body = {**BODY, "repo_url": "https://github.com/bhuwanbkt/"}  # passes the allowlist, isn't a repo
    response = client.post("/jobs", json=body, headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 422
    assert "one repository" in response.text


def test_without_database_the_api_says_so(client: TestClient) -> None:
    response = client.post("/jobs", json=BODY, headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 503
    assert "DATABASE_URL" in response.json()["detail"]


def test_no_configured_keys_means_api_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", "")
    get_settings.cache_clear()
    response = TestClient(create_app()).get("/jobs", headers={"X-API-Key": ALICE_KEY})
    assert response.status_code == 503
