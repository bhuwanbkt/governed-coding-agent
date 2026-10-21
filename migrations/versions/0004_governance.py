"""Governance: risk level, policy findings, job duration, and an `expired` status.

- `risk_level` / `policy_findings`: what the human approver should know (injection hits, secrets,
  policy violations, size and sensitive paths).
- `duration_ms`: wall-clock time of the coding run.
- `expired`: an approval request nobody answered in time; its patch can no longer be published.
- An index for the daily budget query (cost of jobs created today).

Expand-only, so the previous release keeps working during the deploy.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES_V2 = (
    "'queued', 'running', 'awaiting_approval', 'approved', 'rejected', "
    "'publishing', 'succeeded', 'failed', 'handed_off', 'cancelled', 'no_changes'"
)
_STATUSES_V3 = _STATUSES_V2 + ", 'expired'"

UPGRADE_SQL = f"""
ALTER TABLE jobs
    ADD COLUMN risk_level      TEXT CHECK (risk_level IN ('low', 'medium', 'high')),
    ADD COLUMN policy_findings JSONB   NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN duration_ms     INTEGER CHECK (duration_ms >= 0);

ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ({_STATUSES_V3}));

-- Daily budget: SUM(cost_usd) of jobs created since midnight UTC.
CREATE INDEX ix_jobs_created_at ON jobs (created_at);
-- Approval expiry scans jobs waiting for a human.
CREATE INDEX ix_jobs_awaiting ON jobs (updated_at) WHERE status = 'awaiting_approval';
"""

DOWNGRADE_SQL = f"""
DROP INDEX IF EXISTS ix_jobs_awaiting;
DROP INDEX IF EXISTS ix_jobs_created_at;
UPDATE jobs SET status = 'rejected' WHERE status = 'expired';
ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ({_STATUSES_V2}));
ALTER TABLE jobs
    DROP COLUMN duration_ms,
    DROP COLUMN policy_findings,
    DROP COLUMN risk_level;
"""

# Sent to the driver verbatim (see 0001 for why).
_RAW = {"no_parameters": True}


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL, execution_options=_RAW)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL, execution_options=_RAW)
