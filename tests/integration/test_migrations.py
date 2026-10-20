"""Runs against a real Postgres (CI service container, docker compose, or a Neon branch).

Skipped when DATABASE_URL is not set. Use a throwaway database: rows written here can't be
deleted from audit_log by design.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from coding_agent.db.session import get_engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def migrated_db(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


def _insert_job(requested_by: str) -> uuid.UUID:
    job_id = uuid.uuid4()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, repo_url, task, model, requested_by) "
                "VALUES (:id, 'https://github.com/example/repo', 'test task', 'fake', :by)"
            ),
            {"id": job_id, "by": requested_by},
        )
    return job_id


@pytest.mark.usefixtures("migrated_db")
def test_audit_log_is_append_only() -> None:
    job_id = _insert_job("alice")
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (job_id, actor, event_type, tool, allowed) "
                "VALUES (:j, 'coder', 'tool_call', 'write_file', true)"
            ),
            {"j": job_id},
        )
    for statement in (
        "UPDATE audit_log SET actor = 'x' WHERE job_id = :j",
        "DELETE FROM audit_log WHERE job_id = :j",
    ):
        with pytest.raises(DBAPIError, match="append-only"), get_engine().begin() as conn:
            conn.execute(text(statement), {"j": job_id})


@pytest.mark.usefixtures("migrated_db")
def test_requester_cannot_approve_own_job() -> None:
    job_id = _insert_job("alice")
    insert = text(
        "INSERT INTO approvals (job_id, decision, decided_by, diff_sha256) "
        "VALUES (:j, 'approved', :by, 'sha')"
    )
    with pytest.raises(DBAPIError, match="separation of duties"), get_engine().begin() as conn:
        conn.execute(insert, {"j": job_id, "by": "ALICE"})
    with get_engine().begin() as conn:
        conn.execute(insert, {"j": job_id, "by": "bob"})
