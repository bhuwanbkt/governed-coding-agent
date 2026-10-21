"""Ops metrics against a real Postgres: the SQL behind the ops page, /metrics and the SLOs."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from coding_agent.api.main import create_app
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo
from coding_agent.db import ops
from coding_agent.db.session import get_engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
OPS = {"X-API-Key": "ops-" + "o" * 20}  # obviously fake, low-entropy test key
ALICE = {"X-API-Key": "alice-" + "a" * 20}
SHA = "d" * 64


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEYS", f"ops:{OPS['X-API-Key']},alice:{ALICE['X-API-Key']}")
    monkeypatch.setenv("METRICS_READERS", "ops")
    get_settings.cache_clear()
    get_engine.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


def make_job(task: str, **values: object) -> repo.Job:
    job = repo.create_job(
        repo_url="file:///nowhere/repo.git",
        task=task,
        base_branch="main",
        test_command="pytest -q",
        model="fake",
        requested_by="alice",
    )
    return repo.update_job(job.id, actor="test", **values) if values else job


@pytest.mark.usefixtures("env")
def test_snapshot_counts_latency_cost_and_reasons() -> None:
    before = ops.snapshot(1, daily_budget_usd=5.0)
    reason = f"needs a human ({uuid.uuid4().hex[:8]})"
    make_job("ops: fine", status="succeeded", duration_ms=90_000, cost_usd=0.004, llm_calls=12)
    make_job("ops: stuck", status="handed_off", duration_ms=200_000, cost_usd=0.01, error=reason)
    approved = make_job(
        "ops: approved", status="awaiting_approval", duration_ms=60_000, diff="d", patch="p", diff_sha256=SHA
    )
    repo.record_decision(approved.id, decided_by="bob", approve=True, diff_sha256=SHA, comment=None)
    make_job("ops: waiting")  # queued

    after = ops.snapshot(1, daily_budget_usd=5.0)
    assert after.total - before.total == 4
    assert after.finished - before.finished == 3
    assert after.by_status.get("handed_off", 0) - before.by_status.get("handed_off", 0) == 1
    assert after.queued_now >= 1
    assert after.approvals - before.approvals == 1
    assert after.approval_wait_p50_s is not None
    assert after.duration_p95_s is not None and after.duration_p50_s is not None
    assert any(reason in r for r, _ in after.hand_off_reasons) or len(after.hand_off_reasons) == 5
    assert after.spend_today_usd >= 0.014
    assert sum(cost for _, cost, _ in after.cost_by_day) >= 0.014
    assert {slo.name for slo in after.slos} == {
        "Job duration",
        "Infrastructure failures",
        "Queue wait",
        "Daily spend",
    }


@pytest.mark.usefixtures("env")
def test_metrics_endpoint_is_limited_to_readers() -> None:
    client = TestClient(create_app())
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=ALICE).status_code == 403
    response = client.get("/metrics", headers=OPS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE gca_queue_depth gauge" in response.text
