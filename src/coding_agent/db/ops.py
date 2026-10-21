"""Operational metrics computed from the jobs, approvals and audit tables.

The free tier has no metrics backend, and everything needed is already in Postgres: when a job
was created, claimed and finished, what it cost, why it was handed off, and when a human decided.
`snapshot()` turns that into the numbers behind the ops page, `/metrics` and the SLOs
(docs/runbook.md#slos).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from sqlalchemy import text

from coding_agent.db.session import get_engine

# A coding run "produced a result" when it reached one of these (or later) states.
RESULT_STATUSES = (
    "awaiting_approval",
    "approved",
    "publishing",
    "succeeded",
    "rejected",
    "expired",
    "no_changes",
)
JOB_DURATION_SLO_SECONDS = 600  # 95% of coding runs finish within 10 minutes
JOB_DURATION_SLO_TARGET = 0.95
FAILURE_RATE_SLO = 0.05  # at most 5% of finished jobs fail on infrastructure (hand-offs don't count)
QUEUE_WAIT_SLO_SECONDS = 60.0  # p95 time from submit to a worker picking the job up


@dataclass(frozen=True)
class Slo:
    name: str
    objective: str
    actual: str
    met: bool | None  # None: not enough data in the window


@dataclass(frozen=True)
class Snapshot:
    days: int
    by_status: dict[str, int]
    finished: int  # coding runs with a recorded duration
    duration_p50_s: float | None
    duration_p95_s: float | None
    within_duration_slo: int
    queue_wait_p50_s: float | None
    queue_wait_p95_s: float | None
    approval_wait_p50_s: float | None
    approval_wait_p95_s: float | None
    approvals: int
    avg_cost_usd: float | None
    avg_llm_calls: float | None
    cost_by_day: list[tuple[date, float, int]]
    hand_off_reasons: list[tuple[str, int]]
    queued_now: int
    running_now: int
    awaiting_now: int
    spend_today_usd: float
    daily_budget_usd: float
    slos: list[Slo] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.by_status.values())

    def rate(self, *statuses: str) -> float | None:
        if not self.finished:
            return None
        return sum(self.by_status.get(s, 0) for s in statuses) / self.finished


STATUS_SQL = """
SELECT status, count(*) AS n FROM jobs
WHERE created_at >= now() - make_interval(days => :days)
GROUP BY status
"""
RUNS_SQL = """
SELECT count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) / 1000.0 AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) / 1000.0 AS p95,
       count(*) FILTER (WHERE duration_ms <= :slo_ms) AS within_slo,
       avg(cost_usd) AS avg_cost,
       avg(llm_calls) AS avg_calls
FROM jobs
WHERE created_at >= now() - make_interval(days => :days) AND duration_ms IS NOT NULL
"""
QUEUE_SQL = """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY wait) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY wait) AS p95
FROM (
    SELECT extract(epoch FROM started_at - created_at) AS wait FROM jobs
    WHERE created_at >= now() - make_interval(days => :days) AND started_at IS NOT NULL
) AS waits
"""
APPROVAL_SQL = """
WITH waiting AS (
    SELECT job_id, min(ts) AS since FROM audit_log
    WHERE event_type = 'state_change' AND result_summary = 'awaiting_approval'
    GROUP BY job_id
), waits AS (
    SELECT extract(epoch FROM a.decided_at - w.since) AS wait
    FROM approvals AS a JOIN waiting AS w ON w.job_id = a.job_id
    WHERE a.decided_at >= now() - make_interval(days => :days)
)
SELECT count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY wait) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY wait) AS p95
FROM waits
"""
COST_SQL = """
SELECT (created_at AT TIME ZONE 'UTC')::date AS day, COALESCE(sum(cost_usd), 0) AS cost, count(*) AS n
FROM jobs WHERE created_at >= now() - make_interval(days => :days)
GROUP BY 1 ORDER BY 1
"""
HAND_OFF_SQL = """
SELECT left(regexp_replace(COALESCE(error, '(no reason)'), '\\s+', ' ', 'g'), 90) AS reason, count(*) AS n
FROM jobs
WHERE created_at >= now() - make_interval(days => :days) AND status = 'handed_off'
GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5
"""
NOW_SQL = """
SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
       count(*) FILTER (WHERE status IN ('running', 'publishing')) AS running,
       count(*) FILTER (WHERE status = 'awaiting_approval') AS awaiting,
       COALESCE(sum(cost_usd) FILTER (
           WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
       ), 0) AS spend_today
FROM jobs
"""


def _seconds(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def snapshot(days: int, *, daily_budget_usd: float) -> Snapshot:
    params = {"days": days, "slo_ms": JOB_DURATION_SLO_SECONDS * 1000}
    with get_engine().connect() as conn:
        by_status = {str(r.status): int(r.n) for r in conn.execute(text(STATUS_SQL), params)}
        runs = conn.execute(text(RUNS_SQL), params).one()
        queue = conn.execute(text(QUEUE_SQL), params).one()
        approval = conn.execute(text(APPROVAL_SQL), params).one()
        cost_by_day = [(r.day, float(r.cost), int(r.n)) for r in conn.execute(text(COST_SQL), params)]
        reasons = [(str(r.reason), int(r.n)) for r in conn.execute(text(HAND_OFF_SQL), params)]
        now = conn.execute(text(NOW_SQL)).one()

    snap = Snapshot(
        days=days,
        by_status=by_status,
        finished=int(runs.n),
        duration_p50_s=_seconds(runs.p50),
        duration_p95_s=_seconds(runs.p95),
        within_duration_slo=int(runs.within_slo),
        queue_wait_p50_s=_seconds(queue.p50),
        queue_wait_p95_s=_seconds(queue.p95),
        approval_wait_p50_s=_seconds(approval.p50),
        approval_wait_p95_s=_seconds(approval.p95),
        approvals=int(approval.n),
        avg_cost_usd=None if runs.avg_cost is None else float(runs.avg_cost),
        avg_llm_calls=None if runs.avg_calls is None else round(float(runs.avg_calls), 1),
        cost_by_day=cost_by_day,
        hand_off_reasons=reasons,
        queued_now=int(now.queued),
        running_now=int(now.running),
        awaiting_now=int(now.awaiting),
        spend_today_usd=float(now.spend_today),
        daily_budget_usd=daily_budget_usd,
    )
    return replace(snap, slos=evaluate_slos(snap))


def evaluate_slos(s: Snapshot) -> list[Slo]:
    """Each SLO with its objective, the measured value and whether it's met (None = no data)."""
    slos: list[Slo] = []
    if s.finished:
        share = s.within_duration_slo / s.finished
        slos.append(
            Slo(
                "Job duration",
                f"≥ {JOB_DURATION_SLO_TARGET:.0%} of coding runs finish within "
                f"{JOB_DURATION_SLO_SECONDS // 60} min",
                f"{share:.0%} of {s.finished}",
                share >= JOB_DURATION_SLO_TARGET,
            )
        )
        failed = s.by_status.get("failed", 0) / s.finished
        slos.append(
            Slo(
                "Infrastructure failures",
                f"≤ {FAILURE_RATE_SLO:.0%} of finished jobs end in failed (hand-offs excluded)",
                f"{failed:.1%}",
                failed <= FAILURE_RATE_SLO,
            )
        )
    else:
        slos.append(Slo("Job duration", "≥ 95% of coding runs finish within 10 min", "no data", None))
        slos.append(Slo("Infrastructure failures", "≤ 5% of finished jobs end in failed", "no data", None))
    if s.queue_wait_p95_s is not None:
        slos.append(
            Slo(
                "Queue wait",
                f"p95 time from submit to pickup ≤ {QUEUE_WAIT_SLO_SECONDS:.0f} s",
                f"{s.queue_wait_p95_s:.0f} s",
                s.queue_wait_p95_s <= QUEUE_WAIT_SLO_SECONDS,
            )
        )
    else:
        slos.append(Slo("Queue wait", "p95 time from submit to pickup ≤ 60 s", "no data", None))
    slos.append(
        Slo(
            "Daily spend",
            f"LLM spend today ≤ budget (${s.daily_budget_usd:.2f})",
            f"${s.spend_today_usd:.4f}",
            s.spend_today_usd <= s.daily_budget_usd,
        )
    )
    return slos


def prometheus(s: Snapshot) -> str:
    """The snapshot in Prometheus text format (gauges over the window, plus current state)."""
    lines: list[str] = []

    def gauge(name: str, help_text: str, samples: list[tuple[dict[str, str], float | None]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            if value is None:
                continue
            label = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{label}}} {value}" if label else f"{name} {value}")

    window = {"window_days": str(s.days)}
    gauge(
        "gca_jobs",
        "Jobs created in the window, by status.",
        [({**window, "status": k}, float(v)) for k, v in sorted(s.by_status.items())],
    )
    gauge(
        "gca_job_duration_seconds",
        "Coding run duration quantiles in the window.",
        [
            ({**window, "quantile": "0.5"}, s.duration_p50_s),
            ({**window, "quantile": "0.95"}, s.duration_p95_s),
        ],
    )
    gauge(
        "gca_queue_wait_seconds",
        "Time from submit to pickup, quantiles in the window.",
        [
            ({**window, "quantile": "0.5"}, s.queue_wait_p50_s),
            ({**window, "quantile": "0.95"}, s.queue_wait_p95_s),
        ],
    )
    gauge(
        "gca_approval_wait_seconds",
        "Time from awaiting_approval to a human decision, quantiles in the window.",
        [
            ({**window, "quantile": "0.5"}, s.approval_wait_p50_s),
            ({**window, "quantile": "0.95"}, s.approval_wait_p95_s),
        ],
    )
    gauge("gca_job_cost_usd_avg", "Average list-price LLM cost per finished job.", [(window, s.avg_cost_usd)])
    gauge("gca_queue_depth", "Jobs waiting for a worker now.", [({}, float(s.queued_now))])
    gauge("gca_jobs_running", "Jobs being coded or published now.", [({}, float(s.running_now))])
    gauge("gca_jobs_awaiting_approval", "Jobs waiting for a human now.", [({}, float(s.awaiting_now))])
    gauge("gca_spend_today_usd", "List-price LLM spend since midnight UTC.", [({}, s.spend_today_usd)])
    gauge("gca_daily_budget_usd", "Configured daily budget.", [({}, s.daily_budget_usd)])
    gauge(
        "gca_slo_met",
        "1 if the SLO is met in the window, 0 if missed (absent without data).",
        [({**window, "slo": slo.name}, None if slo.met is None else float(slo.met)) for slo in s.slos],
    )
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
