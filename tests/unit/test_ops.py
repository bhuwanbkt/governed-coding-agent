"""SLO evaluation and the Prometheus rendering (pure functions; the SQL is tested against
Postgres in tests/integration/test_ops_db.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from coding_agent.db.ops import Snapshot, evaluate_slos, prometheus

BASE = Snapshot(
    days=7,
    by_status={"succeeded": 18, "handed_off": 1, "failed": 1},
    finished=20,
    duration_p50_s=150.0,
    duration_p95_s=420.0,
    within_duration_slo=19,
    queue_wait_p50_s=2.0,
    queue_wait_p95_s=8.5,
    approval_wait_p50_s=600.0,
    approval_wait_p95_s=3600.0,
    approvals=18,
    avg_cost_usd=0.0047,
    avg_llm_calls=16.0,
    cost_by_day=[(date(2026, 10, 20), 0.05, 12)],
    hand_off_reasons=[("Tests still failing", 1)],
    queued_now=0,
    running_now=1,
    awaiting_now=2,
    spend_today_usd=0.02,
    daily_budget_usd=5.0,
)


def by_name(snapshot: Snapshot) -> dict[str, bool | None]:
    return {slo.name: slo.met for slo in evaluate_slos(snapshot)}


def test_healthy_window_meets_every_slo() -> None:
    assert by_name(BASE) == {
        "Job duration": True,
        "Infrastructure failures": True,
        "Queue wait": True,
        "Daily spend": True,
    }


def test_missed_slos_are_reported() -> None:
    bad = replace(
        BASE,
        by_status={"succeeded": 15, "failed": 5},
        within_duration_slo=15,
        queue_wait_p95_s=95.0,
        spend_today_usd=6.0,
    )
    assert set(by_name(bad).values()) == {False}


def test_hand_offs_are_not_infrastructure_failures() -> None:
    many_hand_offs = replace(BASE, by_status={"succeeded": 10, "handed_off": 10})
    assert by_name(many_hand_offs)["Infrastructure failures"] is True


def test_empty_window_has_no_data_instead_of_a_verdict() -> None:
    empty = replace(BASE, by_status={}, finished=0, within_duration_slo=0, queue_wait_p95_s=None)
    verdicts = by_name(empty)
    assert verdicts["Job duration"] is None and verdicts["Queue wait"] is None
    assert verdicts["Daily spend"] is True


def test_prometheus_text_format() -> None:
    text = prometheus(replace(BASE, slos=evaluate_slos(BASE)))
    assert "# TYPE gca_jobs gauge" in text
    assert 'gca_jobs{window_days="7",status="succeeded"} 18.0' in text
    assert 'gca_job_duration_seconds{window_days="7",quantile="0.95"} 420.0' in text
    assert "gca_queue_depth 0.0" in text
    assert 'gca_slo_met{window_days="7",slo="Queue wait"} 1.0' in text
    assert text.endswith("\n")


def test_missing_values_are_left_out() -> None:
    text = prometheus(replace(BASE, duration_p50_s=None, approval_wait_p95_s=None))
    assert 'quantile="0.5"} ' in text  # queue wait p50 still there
    assert 'gca_job_duration_seconds{window_days="7",quantile="0.5"}' not in text
