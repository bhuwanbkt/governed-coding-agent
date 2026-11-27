from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coding_agent.api.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_healthz_is_ok_without_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_unconfigured_database_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    response = _client().get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "not_configured"


def test_readyz_fails_when_database_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Port 1 on localhost refuses connections immediately.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "2")
    response = _client().get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}


def test_version_and_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("APP_VERSION", "abc1234")
    response = _client().get("/version", headers={"X-Request-ID": "req-42"})
    assert response.json() == {"version": "abc1234", "env": "test"}
    assert response.headers["X-Request-ID"] == "req-42"


def test_invalid_request_id_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    response = _client().get("/version", headers={"X-Request-ID": "bad id\nwith newline"})
    assert response.headers["X-Request-ID"] != "bad id\nwith newline"
    assert len(response.headers["X-Request-ID"]) == 32


def test_readyz_reuses_a_recent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frequent probes mustn't keep an idle Neon database awake."""
    from coding_agent.api import main

    calls: list[int] = []
    monkeypatch.setattr(main, "ping", lambda: calls.append(1))
    monkeypatch.setenv("READYZ_CACHE_SECONDS", "60")
    client = _client()
    for _ in range(5):
        assert client.get("/readyz").json() == {"status": "ok", "database": "ok"}
    assert len(calls) == 1


def test_readyz_does_not_cache_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from coding_agent.api import main

    calls: list[int] = []

    def down() -> None:
        calls.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(main, "ping", down)
    client = _client()
    assert client.get("/readyz").status_code == 503
    assert client.get("/readyz").status_code == 503
    assert len(calls) == 2
