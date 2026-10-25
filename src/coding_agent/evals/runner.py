"""Run an eval suite, write its report, compare with the baseline. Used by `coding-agent eval`."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from coding_agent.config import Settings
from coding_agent.evals import report
from coding_agent.evals.golden import DAILY_LIMIT_MARKER, TaskResult, offline_models, run_task, validate_task
from coding_agent.evals.retrieval import evaluate, load_queries
from coding_agent.evals.tasks import EVALS_DIR, load_golden
from coding_agent.observability.tracing import configure as configure_tracing

REPORTS_DIR = EVALS_DIR / "reports"
BASELINE = EVALS_DIR / "baseline.json"


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=EVALS_DIR.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _write(out_dir: Path, name: str, markdown: str, data: dict[str, object]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    path = out_dir / f"{name}.md"
    path.write_text(markdown, encoding="utf-8")
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):  # shows the report on the Actions run
        with Path(summary_file).open("a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    return path


def validate_all(ids: list[str] | None = None) -> int:
    """Check every solve task: hidden tests fail on the fixture and pass on the reference."""
    failures = 0
    for task in load_golden(ids=ids):
        with tempfile.TemporaryDirectory(prefix="eval-validate-") as tmp:
            problems = validate_task(task, Path(tmp))
        print(
            f"{'ok  ' if not problems else 'FAIL'} {task.id}{': ' + '; '.join(problems) if problems else ''}"
        )
        failures += bool(problems)
    return 1 if failures else 0


async def run_golden(
    settings: Settings,
    *,
    offline: bool,
    ids: list[str] | None = None,
    repeat: int = 1,
    out_dir: Path = REPORTS_DIR,
    baseline_path: Path = BASELINE,
    update_baseline: bool = False,
    record_db: bool = False,
) -> int:
    suite = "offline" if offline else "live"
    if offline:  # scripted runs are not worth tracing (and must not need Langfuse)
        settings = settings.model_copy(update={"langfuse_public_key": None, "langfuse_secret_key": None})
    model = "scripted" if offline else f"{settings.llm_provider}:{settings.llm_model}"
    run_uuid = uuid.uuid4()
    run_id = run_uuid.hex[:8]
    sha = git_sha()
    tracing = configure_tracing(settings) if not offline else None
    tasks = load_golden(ids=ids)
    results: list[TaskResult] = []
    not_run: list[str] = []
    stopped = False  # the provider's daily limit is used up: later tasks can't be measured today
    print(f"Golden eval ({suite}, {model}): {len(tasks)} task(s) x {repeat}, run {run_id}")
    for task in tasks:
        if stopped:
            not_run.append(task.id)
            continue
        for attempt in range(1, repeat + 1):
            with tempfile.TemporaryDirectory(prefix=f"eval-{task.id}-") as tmp:
                result = await run_task(
                    task,
                    settings,
                    Path(tmp),
                    models=offline_models(task) if offline else None,
                    attempt=attempt,
                    tags=(f"eval-run:{run_id}", f"suite:{suite}"),
                )
            results.append(result)
            failed = [c.name for c in result.checks if not c.passed]
            verdict = "SKIP" if result.inconclusive else ("PASS" if result.passed else "FAIL")
            print(
                f"  {verdict} {task.id}"
                f"{f' #{attempt}' if repeat > 1 else ''}: {result.status}"
                f"{' (' + ', '.join(failed) + ')' if failed else ''} "
                f"[{result.llm_calls} calls, ${result.cost_usd:.4f}, {result.duration_ms / 1000:.0f} s]",
                flush=True,
            )
            if result.inconclusive and DAILY_LIMIT_MARKER in (result.error or ""):
                print("  The LLM provider's daily limit is used up: stopping. Rerun the rest later.")
                stopped = True
                break
            if tracing is not None and not result.inconclusive:
                tracing.score(
                    result.job_id, "eval_passed", 1.0 if result.passed else 0.0, comment=", ".join(failed)
                )
    trace_urls: dict[str, str] = {}
    if tracing is not None:
        tracing.flush()
        trace_urls = {r.job_id: url for r in results if (url := tracing.trace_url(r.job_id))}

    summary = report.summarize(results, suite=suite, model=model, run_id=run_id, git_sha=sha, not_run=not_run)
    baseline = report.load_baseline(baseline_path)
    regressions, warnings = report.compare_golden(summary, baseline)
    if update_baseline:  # this run becomes the baseline: "no baseline yet" isn't news
        warnings = [w for w in warnings if "baseline yet" not in w]
    if offline and summary.pass_rate < 1.0:
        regressions.append("the offline suite is deterministic: every task must pass")
    markdown = report.golden_markdown(summary, results, regressions, warnings, trace_urls)
    path = _write(
        out_dir,
        f"golden-{suite}-{run_id}",
        markdown,
        {"summary": summary.__dict__, "results": [r.as_dict() for r in results]},
    )
    if record_db and settings.database_url is not None:
        from coding_agent.db.evals import record_eval_results

        record_eval_results(run_uuid, git_sha=sha, model=model, results=results)
    if update_baseline:
        if summary.safety_violations:
            print("Not updating the baseline: this run has safety violations.")
        else:
            suites = baseline.setdefault("golden", {})
            kinds = {t.id: t.kind for t in load_golden()}
            suites[suite] = report.golden_baseline_entry(summary, kinds, suites.get(suite))
            report.save_baseline(baseline_path, baseline)
            print(f"Baseline updated: {baseline_path}")
    print(
        f"\nPass rate {summary.pass_rate:.0%} ({sum(r.passed for r in results)}/{summary.conclusive}"
        f"{f', {summary.inconclusive} inconclusive' if summary.inconclusive else ''}), "
        f"safety violations {summary.safety_violations}, cost ${summary.total_cost_usd:.4f}"
    )
    for line in regressions:
        print(f"REGRESSION: {line}")
    for line in warnings:
        print(f"warning: {line}")
    rerun = sorted({r.task_id for r in results if r.inconclusive} | set(not_run))
    if rerun:
        print(
            f"Rerun later: uv run coding-agent eval golden --tasks {','.join(rerun)}"
            f"{' --update-baseline' if update_baseline else ''}"
        )
    print(f"Report: {path}")
    return 1 if regressions else 0


def run_retrieval(
    settings: Settings,
    *,
    embeddings: bool = False,
    out_dir: Path = REPORTS_DIR,
    baseline_path: Path = BASELINE,
    update_baseline: bool = False,
) -> int:
    embed_model = None
    if embeddings:
        from coding_agent.retrieval.search import load_embed_model

        embed_model = load_embed_model(settings.embedding_model, settings.embedding_cache_dir)
    result = evaluate(load_queries(), embed_model=embed_model)
    baseline = report.load_baseline(baseline_path)
    regressions, warnings = report.compare_retrieval(result, baseline)
    if update_baseline:
        warnings = [w for w in warnings if "no retrieval baseline" not in w]
    markdown = report.retrieval_markdown(result, regressions, warnings)
    path = _write(out_dir, f"retrieval-{result.mode}", markdown, result.as_dict())
    if update_baseline:
        baseline.setdefault("retrieval", {})[result.mode] = report.retrieval_baseline_entry(result)
        report.save_baseline(baseline_path, baseline)
        print(f"Baseline updated: {baseline_path}")
    print(
        f"Retrieval ({result.mode}, {result.queries} queries): recall@1 {result.recall_at_1:.0%}, "
        f"recall@5 {result.recall_at_5:.0%}, MRR@10 {result.mrr_at_10:.3f}"
    )
    for line in regressions:
        print(f"REGRESSION: {line}")
    for line in warnings:
        print(f"warning: {line}")
    print(f"Report: {path}")
    return 1 if regressions else 0
