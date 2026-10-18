"""Initial schema: jobs, audit_log, approvals, eval_runs.

Governance is enforced in the database as well as in the app (defence in depth):
- audit_log is append-only: UPDATE, DELETE and TRUNCATE are rejected by triggers.
- approvals enforce separation of duties: the approver can't be the job's requester.

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_url        TEXT        NOT NULL,
    base_branch     TEXT        NOT NULL DEFAULT 'main',
    task            TEXT        NOT NULL,
    test_command    TEXT        NOT NULL DEFAULT 'pytest -q',
    model           TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'awaiting_approval', 'approved', 'rejected',
                          'publishing', 'succeeded', 'failed', 'handed_off', 'cancelled')),
    requested_by    TEXT        NOT NULL,
    work_branch     TEXT,
    pr_url          TEXT,
    error           TEXT,
    attempts        INTEGER     NOT NULL DEFAULT 0,
    tokens_in       BIGINT      NOT NULL DEFAULT 0,
    tokens_out      BIGINT      NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10, 4) NOT NULL DEFAULT 0,
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Worker queue: SELECT ... WHERE status = 'queued' ORDER BY created_at FOR UPDATE SKIP LOCKED
CREATE INDEX ix_jobs_queued ON jobs (created_at) WHERE status = 'queued';
CREATE INDEX ix_jobs_status_created ON jobs (status, created_at DESC);

CREATE TABLE audit_log (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    job_id          UUID        REFERENCES jobs (id),
    actor           TEXT        NOT NULL,          -- agent name or user login
    event_type      TEXT        NOT NULL
        CHECK (event_type IN ('tool_call', 'policy_block', 'state_change', 'approval', 'llm_call', 'system')),
    tool            TEXT,
    allowed         BOOLEAN,
    args_masked     JSONB,
    result_summary  TEXT,
    duration_ms     INTEGER,
    trace_id        TEXT
);
CREATE INDEX ix_audit_log_job_ts ON audit_log (job_id, ts);
CREATE INDEX ix_audit_log_blocked ON audit_log (ts) WHERE allowed = false;

CREATE FUNCTION audit_log_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();

CREATE TABLE approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID        NOT NULL UNIQUE REFERENCES jobs (id),
    decision        TEXT        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by      TEXT        NOT NULL,
    comment         TEXT,
    diff_sha256     TEXT        NOT NULL,          -- approval is bound to the exact diff reviewed
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION approvals_enforce_separation_of_duties() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    requester TEXT;
BEGIN
    SELECT requested_by INTO requester FROM jobs WHERE id = NEW.job_id;
    IF requester IS NOT NULL AND lower(requester) = lower(NEW.decided_by) THEN
        RAISE EXCEPTION 'separation of duties: % cannot approve their own job', NEW.decided_by
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER approvals_separation_of_duties
    BEFORE INSERT OR UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_enforce_separation_of_duties();

CREATE TABLE eval_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID        NOT NULL,          -- one evaluation run = many tasks
    git_sha         TEXT,
    model           TEXT        NOT NULL,
    task_id         TEXT        NOT NULL,
    passed          BOOLEAN     NOT NULL,
    steps           INTEGER,
    tokens_in       BIGINT,
    tokens_out      BIGINT,
    cost_usd        NUMERIC(10, 4),
    latency_ms      INTEGER,
    retrieval_hit   BOOLEAN,
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_eval_runs_run ON eval_runs (run_id);
CREATE INDEX ix_eval_runs_model_created ON eval_runs (model, created_at DESC);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS eval_runs;
DROP TABLE IF EXISTS approvals;
DROP FUNCTION IF EXISTS approvals_enforce_separation_of_duties();
DROP TABLE IF EXISTS audit_log;
DROP FUNCTION IF EXISTS audit_log_reject_mutation();
DROP TABLE IF EXISTS jobs;
"""


# Sent to the driver verbatim: no SQLAlchemy bind-param parsing or %-escaping. With
# no_parameters=True SQLAlchemy calls cursor.execute(sql) with no params, so psycopg uses the
# simple-query protocol, which allows a multi-statement script (needed for the $$ function bodies).
_RAW = {"no_parameters": True}


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL, execution_options=_RAW)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL, execution_options=_RAW)
