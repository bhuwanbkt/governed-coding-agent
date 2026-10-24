"""Command-line entry points.

coding-agent run --repo <url> --task "..."   one job in this terminal, with a terminal approval
coding-agent worker                          process jobs from the Postgres queue (phase 2)
coding-agent trace-check                     send one test trace to Langfuse and print its link
coding-agent eval golden|retrieval|validate  evaluations (docs/evals.md)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from coding_agent.agent.graph import tail
from coding_agent.agent.publish import PublishRequest
from coding_agent.agent.runner import JobRequest, run_coding_job, run_publish_job
from coding_agent.agent.schemas import Plan, ReviewVerdict
from coding_agent.config import get_settings
from coding_agent.observability.logging import configure_logging

_RULE = "=" * 72


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="coding-agent", description="Governed coding agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one coding job against a repository")
    run.add_argument("--repo", required=True, help="Git URL (or local Git folder) of the target repository")
    run.add_argument("--task", required=True, help="what the agent should change")
    run.add_argument("--base", default="main", help="base branch (default: main)")
    run.add_argument("--test-command", default="pytest -q", help="allow-listed test command")
    run.add_argument("--llm", choices=["groq", "fake"], help="override LLM_PROVIDER")
    run.add_argument("--model", help="override LLM_MODEL")
    run.add_argument(
        "--supervisor", choices=["deterministic", "llm"], help="override SUPERVISOR_MODE for this run"
    )
    sub.add_parser("worker", help="process queued and approved jobs from the database")
    sub.add_parser("trace-check", help="send one test trace to Langfuse and print its link")
    evals = sub.add_parser("eval", help="run an evaluation suite (see docs/evals.md)")
    suites = evals.add_subparsers(dest="suite", required=True)
    golden = suites.add_parser("golden", help="golden coding tasks with hidden acceptance tests")
    golden.add_argument(
        "--offline", action="store_true", help="scripted models replay reference solutions (no LLM)"
    )
    golden.add_argument("--tasks", help="comma-separated task ids (default: all)")
    golden.add_argument("--repeat", type=int, default=1, help="attempts per task (default 1)")
    golden.add_argument("--update-baseline", action="store_true", help="save this run as the baseline")
    golden.add_argument("--record-db", action="store_true", help="also store results in eval_runs")
    golden.add_argument("--verbose", action="store_true", help="show the agent's log lines")
    retrieval = suites.add_parser("retrieval", help="code search quality: recall@k and MRR")
    retrieval.add_argument("--embeddings", action="store_true", help="hybrid search (downloads a model)")
    retrieval.add_argument("--update-baseline", action="store_true", help="save this run as the baseline")
    validate = suites.add_parser("validate", help="check that hidden tests fail before and pass after")
    validate.add_argument("--tasks", help="comma-separated task ids (default: all)")
    return parser.parse_args(argv)


async def _run_interactive(args: argparse.Namespace) -> int:
    settings = get_settings()
    overrides = {
        k: v
        for k, v in {
            "llm_provider": args.llm,
            "llm_model": args.model,
            "supervisor_mode": args.supervisor,
        }.items()
        if v
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    request = JobRequest(
        repo_url=args.repo, task=args.task, base_branch=args.base, test_command=args.test_command
    )
    state = await run_coding_job(request, settings)
    status = state.get("status")
    if status != "awaiting_approval":
        print(f"\n{_RULE}\nResult: {status}")
        if state.get("error"):
            print(f"Details: {state['error']}")
        for finding in state.get("policy_findings", []):
            print(f"  - [{finding.get('kind')}] {finding.get('detail')}")
        return 0 if status == "no_changes" else 1

    print(f"\n{_RULE}\nAPPROVAL REQUIRED  job {state['job_id']}\n{_RULE}")
    print(f"Task: {state['task']}\n")
    if state.get("plan"):
        print(f"Plan (planner):\n{Plan.model_validate(state['plan']).as_markdown()}\n")
    print(f"Agent summary (coder):\n{state.get('coder_summary', '')}\n")
    review = state.get("review") or {}
    if review.get("verdict") in ("approve", "request_changes"):
        print(f"Review (reviewer agent):\n{ReviewVerdict.model_validate(review).as_markdown()}\n")
    elif review:
        print(f"Review (reviewer agent): {review.get('summary')}\n")
    print(
        f"LLM calls: {state.get('llm_calls', 0)}  tokens: {state.get('tokens_in', 0)} in / "
        f"{state.get('tokens_out', 0)} out  list-price cost: ${state.get('cost_usd', 0.0):.4f}"
    )
    print(f"Risk: {state.get('risk_level', 'n/a')}")
    for finding in state.get("policy_findings", []):
        print(f"  - [{finding.get('kind')}] {finding.get('detail')}")
    print()
    print(f"Tests:\n{tail(state.get('test_output', ''), 20)}\n\nDiff:\n{state.get('diff', '')}")
    print(f"Patch SHA-256: {state['diff_sha256']}\n{_RULE}")
    answer = await asyncio.to_thread(
        input, "Push this branch and open a pull request? Type 'yes' to approve: "
    )
    if answer.strip().lower() != "yes":
        print(f"\n{_RULE}\nResult: rejected (nothing was pushed)")
        return 0

    result = await run_publish_job(
        PublishRequest(
            job_id=state["job_id"],
            repo_url=state["repo_url"],
            base_branch=state["base_branch"],
            work_branch=state["work_branch"],
            task=state["task"],
            patch=state["patch"],
            approved_sha256=state["diff_sha256"],
            approved_by=f"{getpass.getuser()} (CLI)",
            coder_summary=state.get("coder_summary", ""),
            test_output=state.get("test_output", ""),
            prompt_refs=state.get("prompt_refs", []),
            plan=state.get("plan"),
            review=state.get("review"),
        ),
        settings,
    )
    print(f"\n{_RULE}\nResult: {result.status}")
    if result.pr_url:
        print(f"Pull request: {result.pr_url}")
    if result.error:
        print(f"Details: {result.error}")
    return 0 if result.status == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    configure_logging(get_settings().log_level)
    if args.command == "worker":
        from coding_agent.worker.main import run_worker_forever

        asyncio.run(run_worker_forever(get_settings()))
        return 0
    if args.command == "trace-check":
        return _trace_check()
    if args.command == "eval":
        return _eval(args)
    return asyncio.run(_run_interactive(args))


def _eval(args: argparse.Namespace) -> int:
    from coding_agent.evals import runner

    settings = get_settings()
    if not getattr(args, "verbose", False):  # the eval prints its own progress; job logs are noise
        logging.getLogger().setLevel(logging.WARNING)
    ids = [t.strip() for t in args.tasks.split(",") if t.strip()] if getattr(args, "tasks", None) else None
    if args.suite == "validate":
        return runner.validate_all(ids)
    if args.suite == "retrieval":
        return runner.run_retrieval(
            settings, embeddings=args.embeddings, update_baseline=args.update_baseline
        )
    if args.repeat < 1:
        print("--repeat must be at least 1")
        return 2
    return asyncio.run(
        runner.run_golden(
            settings,
            offline=args.offline,
            ids=ids,
            repeat=args.repeat,
            update_baseline=args.update_baseline,
            record_db=args.record_db,
        )
    )


def _trace_check() -> int:
    """Verify the Langfuse settings end to end: keys, host, export and the trace link."""
    import uuid

    from coding_agent.observability.tracing import configure

    active = configure(get_settings())
    if not active.enabled:
        print("Tracing is off: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and LANGFUSE_BASE_URL).")
        return 1
    job_id = f"trace-check-{uuid.uuid4()}"
    with active.job(job_id, "trace-check", input={"check": "hello"}, tags=["trace-check"]) as root:
        with active.observe("tool.example", as_type="tool", input={"path": "README.md"}) as tool:
            tool.update(output="ok")
        root.update(output={"status": "ok"})
    active.score(job_id, "status", "ok")
    active.flush()
    url = active.trace_url(job_id)
    print(f"Sent a test trace. Open it in Langfuse: {url or '(link unavailable: check the keys and host)'}")
    return 0 if url else 1


if __name__ == "__main__":
    sys.exit(main())
