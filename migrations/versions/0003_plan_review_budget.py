"""Multi-agent results: the planner's plan, the reviewer's verdict, and LLM calls used.

Expand-only (new columns with safe defaults), so the previous release keeps working during a
deploy and a rollback needs no data migration.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
ALTER TABLE jobs
    ADD COLUMN plan      JSONB,
    ADD COLUMN review    JSONB,
    ADD COLUMN routes    JSONB   NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN llm_calls INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0);
"""

DOWNGRADE_SQL = """
ALTER TABLE jobs
    DROP COLUMN llm_calls,
    DROP COLUMN routes,
    DROP COLUMN review,
    DROP COLUMN plan;
"""

# Sent to the driver verbatim (see 0001 for why).
_RAW = {"no_parameters": True}


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL, execution_options=_RAW)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL, execution_options=_RAW)
