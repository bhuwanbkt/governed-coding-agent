"""Job results, the approved patch, and a `no_changes` status.

Approval is bound to the exact patch the reviewer saw: the worker stores the patch and its
SHA-256, the approval records that SHA, and publishing re-applies that same patch to a fresh
clone. Nothing that wasn't reviewed can be pushed, even if the original workspace is gone.

Expand-only (new nullable columns, wider CHECK), so the previous release keeps working.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES_V1 = (
    "'queued', 'running', 'awaiting_approval', 'approved', 'rejected', "
    "'publishing', 'succeeded', 'failed', 'handed_off', 'cancelled'"
)
_STATUSES_V2 = _STATUSES_V1 + ", 'no_changes'"

UPGRADE_SQL = f"""
ALTER TABLE jobs
    ADD COLUMN coder_summary TEXT,
    ADD COLUMN test_output   TEXT,
    ADD COLUMN diff          TEXT,
    ADD COLUMN patch         TEXT,
    ADD COLUMN diff_sha256   TEXT,
    ADD COLUMN prompt_refs   JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ({_STATUSES_V2}));

-- Worker queues: approved jobs are published first, then queued jobs are coded.
CREATE INDEX ix_jobs_approved ON jobs (created_at) WHERE status = 'approved';
-- Stale-job recovery scans jobs a worker is (supposedly) still holding.
CREATE INDEX ix_jobs_in_flight ON jobs (heartbeat_at) WHERE status IN ('running', 'publishing');
"""

DOWNGRADE_SQL = f"""
DROP INDEX IF EXISTS ix_jobs_in_flight;
DROP INDEX IF EXISTS ix_jobs_approved;
UPDATE jobs SET status = 'succeeded' WHERE status = 'no_changes';
ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ({_STATUSES_V1}));
ALTER TABLE jobs
    DROP COLUMN prompt_refs,
    DROP COLUMN diff_sha256,
    DROP COLUMN patch,
    DROP COLUMN diff,
    DROP COLUMN test_output,
    DROP COLUMN coder_summary;
"""

# Sent to the driver verbatim (see 0001 for why).
_RAW = {"no_parameters": True}


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL, execution_options=_RAW)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL, execution_options=_RAW)
