"""Store eval results in `eval_runs` (created in migration 0001), one row per task attempt."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

from coding_agent.db.session import get_engine

if TYPE_CHECKING:
    from coding_agent.evals.golden import TaskResult


def record_eval_results(run_id: uuid.UUID, *, git_sha: str, model: str, results: list[TaskResult]) -> None:
    rows = [
        {
            "run_id": run_id,
            "git_sha": git_sha,
            "model": model,
            "task_id": r.task_id,
            "passed": r.passed,
            "steps": r.llm_calls,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "cost_usd": r.cost_usd,
            "latency_ms": r.duration_ms,
            "details": json.dumps(
                {
                    "kind": r.kind,
                    "attempt": r.attempt,
                    "job_id": r.job_id,
                    "status": r.status,
                    "checks": [{"name": c.name, "passed": c.passed} for c in r.checks],
                    "risk_level": r.risk_level,
                }
            ),
        }
        for r in results
    ]
    if not rows:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_runs (run_id, git_sha, model, task_id, passed, steps, tokens_in, "
                "tokens_out, cost_usd, latency_ms, details) VALUES (:run_id, :git_sha, :model, :task_id, "
                ":passed, :steps, :tokens_in, :tokens_out, :cost_usd, :latency_ms, CAST(:details AS JSONB))"
            ),
            rows,
        )
