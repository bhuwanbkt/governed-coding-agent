"""Governance rules that live in the database and API: approvers list, approval expiry, the daily
budget, and the new job fields. Runs against a real Postgres (see tests/conftest.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from coding_agent.agent.fake_llm import demo_models
from coding_agent.api.main import create_app
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo
from coding_agent.db.session import get_engine
from coding_agent.worker.main import Worker

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
# Obviously fake, low-entropy test keys.
ALICE = {"X-API-Key": "alice-" + "a" * 20}
BOB = {"X-API-Key": "bob-" + "b" * 20}
CAROL = {"X-API-Key": "carol-" + "c" * 20}
SHA = "a" * 64


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    keys = [
        f"{name}:{headers['X-API-Key']}"
        for name, headers in (("alice", ALICE), ("bob", BOB), ("carol", CAROL))
    ]
    monkeypatch.setenv("API_KEYS", ",".join(keys))
    monkeypatch.setenv("ALLOWED_REPO_PREFIXES", "file://")
    monkeypatch.setenv("APPROVERS", "carol")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    get_engine.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with get_engine().begin() as conn:  # park leftovers from other tests
        conn.execute(
            text(
                "UPDATE jobs SET status = 'cancelled' "
                "WHERE status IN ('queued', 'approved', 'awaiting_approval')"
            )
        )


def awaiting_job() -> repo.Job:
    job = repo.create_job(
        repo_url="file:///nowhere",
        task="governance test job",
        base_branch="main",
        test_command="pytest -q",
        model="fake",
        requested_by="alice",
    )
    return repo.update_job(
        job.id,
        actor="test",
        status="awaiting_approval",
        diff="diff",
        patch="patch",
        diff_sha256=SHA,
        risk_level="medium",
        policy_findings=[{"kind": "risk", "detail": "120 lines changed"}],
        tokens_in=1200,
        tokens_out=300,
        cost_usd=0.0004,
        duration_ms=4321,
    )


@pytest.mark.usefixtures("env")
def test_only_listed_approvers_can_decide_and_job_shows_governance_fields() -> None:
    job = awaiting_job()
    client = TestClient(create_app())
    decision = {"decision": "approve", "diff_sha256": SHA}

    denied = client.post(f"/jobs/{job.id}/decision", json=decision, headers=BOB)
    assert denied.status_code == 403
    assert "approvers list" in denied.json()["detail"]

    detail = client.get(f"/jobs/{job.id}", headers=ALICE).json()
    assert detail["risk_level"] == "medium"
    assert detail["policy_findings"] == [{"kind": "risk", "detail": "120 lines changed"}]
    assert (detail["tokens_in"], detail["tokens_out"], detail["duration_ms"]) == (1200, 300, 4321)
    assert detail["cost_usd"] == pytest.approx(0.0004)

    approved = client.post(f"/jobs/{job.id}/decision", json=decision, headers=CAROL)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


@pytest.mark.usefixtures("env")
def test_unanswered_approval_requests_expire() -> None:
    job = awaiting_job()
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET updated_at = now() - interval '48 hours' WHERE id = :id"), {"id": job.id}
        )
    client = TestClient(create_app())

    late = client.post(
        f"/jobs/{job.id}/decision", json={"decision": "approve", "diff_sha256": SHA}, headers=CAROL
    )
    assert late.status_code == 409
    assert "approval window closed" in late.json()["detail"]

    assert job.id in repo.expire_stale_approvals(ttl_hours=24)
    expired = repo.get_job(job.id)
    assert expired.status == "expired"
    assert expired.finished_at is not None
    states = [e["result_summary"] for e in repo.list_audit(job.id) if e["event_type"] == "state_change"]
    assert states[-1] == "expired"


@pytest.mark.usefixtures("env")
def test_daily_budget_pauses_new_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    spent = repo.create_job(
        repo_url="file:///nowhere",
        task="an expensive job earlier today",
        base_branch="main",
        test_command="pytest -q",
        model="fake",
        requested_by="alice",
    )
    repo.update_job(spent.id, actor="test", status="succeeded", cost_usd=10.0)
    queued = repo.create_job(
        repo_url="file:///nowhere",
        task="waits for tomorrow",
        base_branch="main",
        test_command="pytest -q",
        model="fake",
        requested_by="alice",
    )
    monkeypatch.setenv("DAILY_BUDGET_USD", "1")
    get_settings.cache_clear()
    try:
        assert repo.spend_today() >= 10.0
        worker = Worker(get_settings(), worker_id="budget-test", model_factory=demo_models)
        assert asyncio.run(worker.process_one()) is False
        assert repo.get_job(queued.id).status == "queued"
    finally:
        # Move the spend to an earlier day so later tests in this database aren't throttled.
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE jobs SET created_at = now() - interval '3 days' WHERE id = :id"),
                {"id": spent.id},
            )
            conn.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :id"), {"id": queued.id})
