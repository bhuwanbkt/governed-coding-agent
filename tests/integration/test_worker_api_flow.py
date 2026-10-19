"""Full governed flow against a real Postgres: API -> queue -> worker -> approval -> publish.

Runs in CI (Postgres service container) or locally with TEST_DATABASE_URL set. Uses the
scripted LLM and a local bare Git remote, so it needs no network or API keys.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from coding_agent.agent.fake_llm import demo_model
from coding_agent.api.main import create_app
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo
from coding_agent.db.session import get_engine
from coding_agent.worker.main import Worker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ROOT = Path(__file__).resolve().parents[2]
# Obviously fake, low-entropy test keys.
ALICE = {"X-API-Key": "alice-" + "a" * 20}
BOB = {"X-API-Key": "bob-" + "b" * 20}
TASK = "Add a divide(a, b) function that raises ValueError on zero, with tests"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("API_KEYS", f"alice:{ALICE['X-API-Key']},bob:{BOB['X-API-Key']}")
    monkeypatch.setenv("ALLOWED_REPO_PREFIXES", "file://")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("ALLOWED_TEST_COMMANDS", "python -m pytest")
    get_settings.cache_clear()
    get_engine.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    # Other tests may leave work in the shared test database; park it so it isn't claimed here.
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'cancelled' WHERE status IN ('queued', 'approved')"))


def worker() -> Worker:
    return Worker(get_settings(), worker_id="test-worker", model_factory=demo_model)


def remote_branches(remote_uri: str) -> str:
    return subprocess.run(
        ["git", "branch", "--list"],
        cwd=Path(remote_uri.removeprefix("file://")),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.usefixtures("env")
def test_submit_code_approve_publish(sample_remote: str) -> None:
    client = TestClient(create_app())

    created = client.post(
        "/jobs",
        json={"repo_url": sample_remote, "task": TASK, "test_command": "python -m pytest -q"},
        headers=ALICE,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    assert created.json()["requested_by"] == "alice"

    assert asyncio.run(worker().process_one()) is True
    job = client.get(f"/jobs/{job_id}", headers=ALICE).json()
    assert job["status"] == "awaiting_approval", job["error"]
    assert "def divide" in job["diff"]
    sha = job["diff_sha256"]
    assert job["work_branch"] not in remote_branches(sample_remote)  # nothing pushed before approval

    own = client.post(
        f"/jobs/{job_id}/decision", json={"decision": "approve", "diff_sha256": sha}, headers=ALICE
    )
    assert own.status_code == 403  # separation of duties
    wrong = client.post(
        f"/jobs/{job_id}/decision", json={"decision": "approve", "diff_sha256": "0" * 64}, headers=BOB
    )
    assert wrong.status_code == 409  # approval is bound to the exact diff
    ok = client.post(
        f"/jobs/{job_id}/decision",
        json={"decision": "approve", "diff_sha256": sha, "comment": "LGTM"},
        headers=BOB,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    again = client.post(
        f"/jobs/{job_id}/decision", json={"decision": "reject", "diff_sha256": sha}, headers=BOB
    )
    assert again.status_code == 409  # one decision per job

    assert asyncio.run(worker().process_one()) is True
    job = client.get(f"/jobs/{job_id}", headers=ALICE).json()
    assert job["status"] == "succeeded", job["error"]
    assert job["work_branch"] in remote_branches(sample_remote)

    audit = client.get(f"/jobs/{job_id}/audit", headers=BOB).json()
    tools = [e["tool"] for e in audit if e["event_type"] == "tool_call"]
    assert {
        "git_clone",
        "replace_in_file",
        "run_tests",
        "export_patch",
        "apply_patch",
        "git_commit_and_push",
    } <= set(tools)
    approvals = [e for e in audit if e["event_type"] == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["actor"] == "bob"
    states = [e["result_summary"] for e in audit if e["event_type"] == "state_change"]
    assert states == ["queued", "running", "awaiting_approval", "publishing", "succeeded"]


@pytest.mark.usefixtures("env")
def test_rejected_job_is_never_published(sample_remote: str) -> None:
    client = TestClient(create_app())
    job_id = client.post(
        "/jobs",
        json={"repo_url": sample_remote, "task": TASK, "test_command": "python -m pytest -q"},
        headers=ALICE,
    ).json()["id"]
    asyncio.run(worker().process_one())
    sha = client.get(f"/jobs/{job_id}", headers=ALICE).json()["diff_sha256"]

    rejected = client.post(
        f"/jobs/{job_id}/decision", json={"decision": "reject", "diff_sha256": sha}, headers=BOB
    )
    assert rejected.json()["status"] == "rejected"
    assert asyncio.run(worker().process_one()) is False  # nothing left to do
    assert "agent/" not in remote_branches(sample_remote)


@pytest.mark.usefixtures("env")
def test_stale_running_job_is_requeued_then_failed() -> None:
    job = repo.create_job(
        repo_url="file:///nowhere",
        task="t" * 10,
        base_branch="main",
        test_command="pytest -q",
        model="fake",
        requested_by="alice",
    )
    for expected in ("queued", "queued", "failed"):
        claimed = repo.claim_next("dead-worker", from_status="queued", to_status="running")
        assert claimed is not None and claimed.id == job.id
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = :id"),
                {"id": job.id},
            )
        recovered = dict(repo.recover_stale_jobs(stale_after_seconds=60, max_claims=3))
        assert recovered[job.id] == expected
    assert "interrupted too many times" in str(repo.get_job(job.id).error)


@pytest.mark.usefixtures("env")
def test_unknown_job_is_404() -> None:
    client = TestClient(create_app())
    assert client.get(f"/jobs/{uuid.uuid4()}", headers=ALICE).status_code == 404
