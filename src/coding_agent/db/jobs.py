"""Data access for jobs, approvals and the audit log (SQLAlchemy Core, plain SQL).

The `jobs` table doubles as the work queue (ADR 0002). Workers claim rows with
`FOR UPDATE SKIP LOCKED`, keep them alive with heartbeats, and stale rows are recovered.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import DBAPIError

from coding_agent.db.session import get_engine

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "rejected", "handed_off", "no_changes", "cancelled"})

_JSON_COLUMNS = frozenset({"prompt_refs", "plan", "review", "routes"})

# Columns `update_job` may set. Anything else is a programming error.
_UPDATABLE = frozenset(
    {
        "status",
        "work_branch",
        "pr_url",
        "error",
        "coder_summary",
        "test_output",
        "diff",
        "patch",
        "diff_sha256",
        "prompt_refs",
        "plan",
        "review",
        "routes",
        "llm_calls",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "finished_at",
    }
)


class JobNotFoundError(LookupError):
    pass


class InvalidJobStateError(RuntimeError):
    pass


class DiffMismatchError(RuntimeError):
    pass


class SeparationOfDutiesError(PermissionError):
    pass


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    repo_url: str
    base_branch: str
    task: str
    test_command: str
    model: str
    status: str
    requested_by: str
    work_branch: str | None
    pr_url: str | None
    error: str | None
    attempts: int
    coder_summary: str | None
    test_output: str | None
    diff: str | None
    patch: str | None
    diff_sha256: str | None
    prompt_refs: list[str]
    plan: dict[str, Any] | None
    review: dict[str, Any] | None
    routes: list[dict[str, Any]]
    llm_calls: int
    locked_by: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RowMapping) -> Job:
        values = {f.name: row[f.name] for f in fields(cls)}
        for column in _JSON_COLUMNS:
            if isinstance(values[column], str):  # drivers without JSONB decoding
                values[column] = json.loads(values[column])
        values["prompt_refs"] = list(values["prompt_refs"] or [])
        values["routes"] = list(values["routes"] or [])
        return cls(**values)


# --------------------------------------------------------------------------- jobs
def create_job(
    *, repo_url: str, task: str, base_branch: str, test_command: str, model: str, requested_by: str
) -> Job:
    with get_engine().begin() as conn:
        row = (
            conn.execute(
                text(
                    "INSERT INTO jobs (repo_url, task, base_branch, test_command, model, requested_by) "
                    "VALUES (:repo_url, :task, :base_branch, :test_command, :model, :requested_by) "
                    "RETURNING *"
                ),
                {
                    "repo_url": repo_url,
                    "task": task,
                    "base_branch": base_branch,
                    "test_command": test_command,
                    "model": model,
                    "requested_by": requested_by,
                },
            )
            .mappings()
            .one()
        )
        job = Job.from_row(row)
        _insert_audit(conn, job.id, actor=requested_by, event_type="state_change", result="queued")
    return job


def get_job(job_id: uuid.UUID) -> Job:
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
    if row is None:
        raise JobNotFoundError(str(job_id))
    return Job.from_row(row)


def list_jobs(limit: int = 50) -> list[Job]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM jobs ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}
        ).mappings()
        return [Job.from_row(r) for r in rows]


def claim_next(worker_id: str, *, from_status: str, to_status: str) -> Job | None:
    """Atomically take the oldest job in `from_status`. Concurrent workers never get the same row."""
    with get_engine().begin() as conn:
        row = (
            conn.execute(
                text(
                    """
                WITH next AS (
                    SELECT id FROM jobs
                    WHERE status = :from_status
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs AS j
                SET status = :to_status, locked_by = :worker, locked_at = now(), heartbeat_at = now(),
                    attempts = j.attempts + 1, started_at = COALESCE(j.started_at, now()), updated_at = now()
                FROM next WHERE j.id = next.id
                RETURNING j.*
                """
                ),
                {"from_status": from_status, "to_status": to_status, "worker": worker_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        job = Job.from_row(row)
        _insert_audit(conn, job.id, actor=worker_id, event_type="state_change", result=to_status)
    return job


def heartbeat(job_id: uuid.UUID, worker_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET heartbeat_at = now() WHERE id = :id AND locked_by = :worker"),
            {"id": job_id, "worker": worker_id},
        )


def update_job(job_id: uuid.UUID, *, actor: str, **values: Any) -> Job:
    """Set result fields; a status change is also written to the audit log."""
    unknown = set(values) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update columns: {sorted(unknown)}")
    if values.get("status") in TERMINAL_STATUSES:
        values.setdefault("finished_at", datetime.now().astimezone())
    params: dict[str, Any] = {"id": job_id}
    assignments = ["updated_at = now()"]
    for column, value in values.items():
        if column in _JSON_COLUMNS:
            assignments.append(f"{column} = CAST(:{column} AS JSONB)")
            params[column] = None if value is None else json.dumps(value, default=str)
        else:
            assignments.append(f"{column} = :{column}")
            params[column] = value
    if "status" in values and values["status"] not in {"running", "publishing"}:
        assignments.append("locked_by = NULL")
    with get_engine().begin() as conn:
        row = (
            conn.execute(
                text(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = :id RETURNING *"),  # noqa: S608
                params,
            )
            .mappings()
            .first()
        )
        if row is None:
            raise JobNotFoundError(str(job_id))
        if "status" in values:
            _insert_audit(conn, job_id, actor=actor, event_type="state_change", result=str(values["status"]))
    return Job.from_row(row)


def recover_stale_jobs(*, stale_after_seconds: int, max_claims: int) -> list[tuple[uuid.UUID, str]]:
    """Requeue coding jobs whose worker stopped heart-beating; fail ones interrupted too often.

    Interrupted publishes are failed rather than retried: the branch may already be pushed,
    so a human should check before anything is pushed again.
    """
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                UPDATE jobs SET
                    status = CASE
                        WHEN status = 'publishing' THEN 'failed'
                        WHEN attempts >= :max_claims THEN 'failed'
                        ELSE 'queued' END,
                    error = CASE
                        WHEN status = 'publishing'
                            THEN 'Publish was interrupted; check the branch before retrying.'
                        WHEN attempts >= :max_claims THEN 'Worker was interrupted too many times.'
                        ELSE error END,
                    finished_at = CASE
                        WHEN status = 'publishing' OR attempts >= :max_claims THEN now()
                        ELSE finished_at END,
                    locked_by = NULL,
                    updated_at = now()
                WHERE status IN ('running', 'publishing')
                  AND heartbeat_at < now() - make_interval(secs => CAST(:stale AS double precision))
                RETURNING id, status
                """
            ),
            {"stale": stale_after_seconds, "max_claims": max_claims},
        ).all()
        for job_id, status in rows:
            _insert_audit(conn, job_id, actor="recovery", event_type="state_change", result=status)
    return [(r[0], r[1]) for r in rows]


# --------------------------------------------------------------------------- approvals
def record_decision(
    job_id: uuid.UUID, *, decided_by: str, approve: bool, diff_sha256: str, comment: str | None
) -> Job:
    """Record a human decision on the exact diff that was reviewed (one decision per job)."""
    with get_engine().begin() as conn:
        row = (
            conn.execute(text("SELECT * FROM jobs WHERE id = :id FOR UPDATE"), {"id": job_id})
            .mappings()
            .first()
        )
        if row is None:
            raise JobNotFoundError(str(job_id))
        job = Job.from_row(row)
        if job.status != "awaiting_approval":
            raise InvalidJobStateError(f"job is {job.status}, not awaiting_approval")
        if job.diff_sha256 != diff_sha256:
            raise DiffMismatchError("diff_sha256 does not match the diff awaiting approval")
        if job.requested_by.lower() == decided_by.lower():
            raise SeparationOfDutiesError("the requester of a job cannot approve it")
        decision = "approved" if approve else "rejected"
        try:
            conn.execute(
                text(
                    "INSERT INTO approvals (job_id, decision, decided_by, comment, diff_sha256) "
                    "VALUES (:job_id, :decision, :decided_by, :comment, :sha)"
                ),
                {
                    "job_id": job_id,
                    "decision": decision,
                    "decided_by": decided_by,
                    "comment": comment,
                    "sha": diff_sha256,
                },
            )
        except DBAPIError as exc:  # the database trigger is the second line of defence
            if "separation of duties" in str(exc):
                raise SeparationOfDutiesError("the requester of a job cannot approve it") from exc
            raise
        finish = "" if approve else ", finished_at = now()"
        updated = (
            conn.execute(
                text(
                    f"UPDATE jobs SET status = :status, updated_at = now(){finish} "  # noqa: S608 - constant
                    "WHERE id = :id RETURNING *"
                ),
                {"id": job_id, "status": decision},
            )
            .mappings()
            .one()
        )
        _insert_audit(
            conn,
            job_id,
            actor=decided_by,
            event_type="approval",
            result=f"{decision} diff {diff_sha256[:12]}" + (f": {comment[:200]}" if comment else ""),
        )
    return Job.from_row(updated)


def get_approval(job_id: uuid.UUID) -> dict[str, Any] | None:
    with get_engine().connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT decision, decided_by, comment, diff_sha256, decided_at "
                    "FROM approvals WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


# --------------------------------------------------------------------------- audit log
def insert_audit_event(event: dict[str, Any]) -> None:
    """Persist one Toolbox audit event (tool_call / policy_block)."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (job_id, actor, event_type, tool, allowed, args_masked, "
                "result_summary, duration_ms, trace_id) VALUES (:job_id, :actor, :event_type, :tool, "
                ":allowed, CAST(:args AS JSONB), :result, :duration_ms, :trace_id)"
            ),
            {
                "job_id": uuid.UUID(str(event["job_id"])),
                "actor": event["actor"],
                "event_type": event["event_type"],
                "tool": event.get("tool"),
                "allowed": event.get("allowed"),
                "args": json.dumps(event.get("args_masked") or {}, default=str),
                "result": event.get("result_summary"),
                "duration_ms": int(event.get("duration_ms") or 0),
                "trace_id": event.get("trace_id"),
            },
        )


def list_audit(job_id: uuid.UUID, limit: int = 500) -> list[dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, ts, actor, event_type, tool, allowed, args_masked, result_summary, duration_ms "
                "FROM audit_log WHERE job_id = :id ORDER BY id LIMIT :limit"
            ),
            {"id": job_id, "limit": limit},
        ).mappings()
        return [dict(r) for r in rows]


def _insert_audit(conn: Connection, job_id: uuid.UUID, *, actor: str, event_type: str, result: str) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (job_id, actor, event_type, result_summary) "
            "VALUES (:job_id, :actor, :event_type, :result)"
        ),
        {"job_id": job_id, "actor": actor, "event_type": event_type, "result": result},
    )
