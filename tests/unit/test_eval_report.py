"""Eval summaries, baseline comparison and the Markdown report."""

from __future__ import annotations

from pathlib import Path

from coding_agent.evals import report
from coding_agent.evals.golden import Check, TaskResult
from coding_agent.evals.retrieval import QueryResult, RetrievalReport


def result(
    task_id: str, kind: str, passed: bool, *, safe: bool = True, seconds: int = 60, inconclusive: bool = False
) -> TaskResult:
    checks = [Check("outcome", passed), Check("safety", safe, "" if safe else "secret (github_token)")]
    return TaskResult(
        task_id=task_id,
        kind=kind,
        attempt=1,
        job_id=f"job-{task_id}",
        status="awaiting_approval",
        passed=passed and safe and not inconclusive,
        inconclusive=inconclusive,
        checks=checks,
        llm_calls=10,
        cost_usd=0.002,
        duration_ms=seconds * 1000,
    )


RESULTS = [
    result("a", "solve", True, seconds=30),
    result("b", "solve", False, seconds=90),
    result("c", "safety", True),
    result("d", "handoff", True),
]


def summary(results: list[TaskResult] = RESULTS, suite: str = "live") -> report.GoldenSummary:
    return report.summarize(results, suite=suite, model="groq:m", run_id="r1", git_sha="abc")


def test_summary_rates_and_costs() -> None:
    s = summary()
    assert s.pass_rate == 0.75
    assert s.by_kind == {"handoff": 1.0, "safety": 1.0, "solve": 0.5}
    assert s.by_task["b"] == 0.0
    assert s.safety_violations == 0
    assert s.total_cost_usd == 0.008
    assert s.duration_p50_s == 60.0 and s.duration_p95_s == 90.0


def test_safety_violation_is_always_a_regression() -> None:
    unsafe = summary([*RESULTS, result("e", "safety", True, safe=False)])
    regressions, _ = report.compare_golden(unsafe, {})
    assert regressions and "safety violation" in regressions[0]


BASE_TASKS = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 0.5}  # mean 0.875


def test_live_pass_rate_has_a_tolerance_offline_has_none() -> None:
    baseline = {"golden": {"live": {"by_task": BASE_TASKS}, "offline": {"by_task": BASE_TASKS}}}
    regressions, warnings = report.compare_golden(summary(), baseline)  # 0.75 vs 0.875 - 0.15
    assert regressions == []
    assert warnings == ["b: 0% (baseline 100%)"]
    offline_regressions, _ = report.compare_golden(summary(suite="offline"), baseline)
    assert offline_regressions and "below the baseline" in offline_regressions[0]


def test_a_subset_run_is_compared_on_the_same_tasks() -> None:
    baseline = {"golden": {"live": {"by_task": {**BASE_TASKS, "b": 0.0}}}}
    regressions, warnings = report.compare_golden(summary([RESULTS[1]]), baseline)  # b only: 0% vs 0%
    assert regressions == [] and warnings == []


def test_provider_limits_are_inconclusive_not_failures() -> None:
    results = [*RESULTS[:2], result("c", "solve", False, inconclusive=True)]
    s = summary(results)
    assert (s.attempts, s.inconclusive, s.conclusive) == (3, 1, 2)
    assert s.pass_rate == 0.5  # a passed, b failed; c not counted
    assert "c" not in s.by_task
    _, warnings = report.compare_golden(s, {})
    assert "inconclusive" in warnings[0]
    assert "⏸️ inconclusive" in report.golden_markdown(s, results, [], [])


def test_baseline_merges_partial_runs_for_the_same_model() -> None:
    kinds = {"a": "solve", "b": "solve", "c": "safety", "d": "handoff", "e": "solve"}
    previous = {"model": "groq:m", "by_task": {"a": 0.0, "e": 1.0, "gone": 1.0}}
    entry = report.golden_baseline_entry(summary(), kinds, previous)
    assert entry["by_task"] == {"a": 1.0, "b": 0.0, "c": 1.0, "d": 1.0, "e": 1.0}  # "gone" dropped
    assert entry["pass_rate"] == 0.8
    assert entry["by_kind"]["solve"] == 0.667
    other_model = report.golden_baseline_entry(summary(), kinds, {**previous, "model": "other"})
    assert "e" not in other_model["by_task"]


def test_missing_baseline_is_a_warning_not_a_failure() -> None:
    regressions, warnings = report.compare_golden(summary(), {})
    assert regressions == [] and "no live baseline" in warnings[0]


def retrieval(recall5: float, mrr: float) -> RetrievalReport:
    return RetrievalReport(
        mode="bm25",
        queries=2,
        recall_at_1=0.5,
        recall_at_5=recall5,
        mrr_at_10=mrr,
        symbol_at_5=recall5,
        by_corpus={"project": {"recall_at_1": 0.5, "recall_at_5": recall5, "mrr_at_10": mrr}},
        results=[QueryResult("project", "q1", 1, ["a.py:1"]), QueryResult("project", "q2", None, ["b.py:1"])],
    )


def test_retrieval_regression_beyond_tolerance() -> None:
    baseline = {"retrieval": {"bm25": {"recall_at_5": 0.9, "mrr_at_10": 0.7}}}
    assert report.compare_retrieval(retrieval(0.89, 0.69), baseline)[0] == []
    regressions, _ = report.compare_retrieval(retrieval(0.8, 0.7), baseline)
    assert regressions and regressions[0].startswith("recall_at_5")


def test_markdown_reports(tmp_path: Path) -> None:
    golden = report.golden_markdown(summary(), RESULTS, ["drop"], [], {"job-a": "https://lf.example/t/1"})
    assert "**75%** (3/4)" in golden
    assert "❌ drop" in golden and "[trace](https://lf.example/t/1)" in golden
    text = report.retrieval_markdown(retrieval(0.5, 0.5), [], [])
    assert "Not in the top 5" in text and "q2" in text

    path = tmp_path / "baseline.json"
    kinds = {"a": "solve", "b": "solve", "c": "safety", "d": "handoff"}
    report.save_baseline(path, {"golden": {"live": report.golden_baseline_entry(summary(), kinds)}})
    assert report.load_baseline(path)["golden"]["live"]["pass_rate"] == 0.75
