"""Command-line runner (phase 1). The API, worker and UI replace this from phase 2.

    uv run coding-agent run --repo https://github.com/<you>/agent-sandbox-repo \\
        --task "Add a divide(a, b) function that raises ValueError on zero, with tests"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from coding_agent.agent.graph import ApprovalRequest
from coding_agent.agent.runner import JobRequest, run_job
from coding_agent.config import get_settings
from coding_agent.observability.logging import configure_logging

_RULE = "=" * 72


async def terminal_approver(request: ApprovalRequest) -> bool:
    """Human approval gate: show the diff and evidence, require an explicit 'yes'."""
    print(f"\n{_RULE}\nAPPROVAL REQUIRED  job {request.job_id}\n{_RULE}")
    print(f"Task: {request.task}\n\nAgent summary:\n{request.coder_summary}\n")
    print(f"Tests:\n{request.test_output}\n\nDiff:\n{request.diff}\n{_RULE}")
    answer = await asyncio.to_thread(
        input, "Push this branch and open a pull request? Type 'yes' to approve: "
    )
    return answer.strip().lower() == "yes"


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="coding-agent", description="Governed coding agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one coding job against a repository")
    run.add_argument("--repo", required=True, help="Git URL of the target repository")
    run.add_argument("--task", required=True, help="what the agent should change")
    run.add_argument("--base", default="main", help="base branch (default: main)")
    run.add_argument("--test-command", default="pytest -q", help="allow-listed test command")
    run.add_argument("--llm", choices=["groq", "fake"], help="override LLM_PROVIDER")
    run.add_argument("--model", help="override LLM_MODEL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    settings = get_settings()
    overrides = {k: v for k, v in {"llm_provider": args.llm, "llm_model": args.model}.items() if v}
    if overrides:
        settings = settings.model_copy(update=overrides)
    configure_logging(settings.log_level)

    request = JobRequest(
        repo_url=args.repo, task=args.task, base_branch=args.base, test_command=args.test_command
    )
    final = asyncio.run(run_job(request, settings, terminal_approver))

    print(f"\n{_RULE}\nResult: {final.get('status')}")
    if final.get("pr_url"):
        print(f"Pull request: {final['pr_url']}")
    if final.get("error"):
        print(f"Details: {final['error']}")
    return 0 if final.get("status") in {"succeeded", "no_changes", "rejected"} else 1


if __name__ == "__main__":
    sys.exit(main())
