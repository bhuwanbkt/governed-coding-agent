"""Publishing an approved job: fresh clone -> apply the approved patch -> commit/push -> PR.

The coding workspace may be long gone (worker restart, another machine). Publishing never
reuses it: it starts from a clean clone of the base branch and applies only the patch whose
SHA-256 a human approved. If anything differs, it refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coding_agent.agent.graph import tail
from coding_agent.agent.roles import PUBLISHER_TOOLS
from coding_agent.agent.schemas import Plan, ReviewVerdict
from coding_agent.agent.toolbox import Toolbox
from coding_agent.governance.redact import redact

ACTOR = "publisher"


@dataclass(frozen=True)
class PublishRequest:
    job_id: str
    repo_url: str
    base_branch: str
    work_branch: str
    task: str
    patch: str
    approved_sha256: str
    approved_by: str
    coder_summary: str = ""
    test_output: str = ""
    prompt_refs: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    review: dict[str, Any] | None = None


@dataclass(frozen=True)
class PublishResult:
    status: str  # "succeeded" | "failed"
    pr_url: str | None = None
    error: str | None = None


def _section(title: str, model: type[Plan] | type[ReviewVerdict], data: dict[str, Any] | None) -> str:
    try:
        return f"## {title}\n\n{model.model_validate(data).as_markdown()}\n\n" if data else ""
    except ValueError:  # e.g. a "not_reviewed" placeholder
        return f"## {title}\n\n{(data or {}).get('summary', '(none)')}\n\n"


def pr_body(request: PublishRequest) -> str:
    return redact(_pr_body(request))


def _pr_body(request: PublishRequest) -> str:
    return (
        f"## Task\n\n{request.task}\n\n"
        + _section("Plan (planner agent)", Plan, request.plan)
        + f"## Agent summary\n\n{request.coder_summary or '(none)'}\n\n"
        + _section("Automated review (reviewer agent)", ReviewVerdict, request.review)
        + f"## Tests\n\n```\n{tail(request.test_output, 15)}\n```\n\n"
        f"---\nJob `{request.job_id}` · patch `{request.approved_sha256[:12]}` approved by "
        f"**{request.approved_by}** before push · prompts: {', '.join(request.prompt_refs) or 'n/a'}"
    )


async def publish(toolbox: Toolbox, request: PublishRequest) -> PublishResult:
    steps = [
        (
            "git_clone",
            {
                "repo_url": request.repo_url,
                "base_branch": request.base_branch,
                "work_branch": request.work_branch,
            },
        ),
        ("apply_patch", {"patch": request.patch, "expected_sha256": request.approved_sha256}),
        (
            "git_commit_and_push",
            {"message": f"feat(agent): {request.task.splitlines()[0][:60]}\n\nJob: {request.job_id}"},
        ),
    ]
    for tool, args in steps:
        result = await toolbox.call(tool, args, actor=ACTOR, allowed=PUBLISHER_TOOLS)
        if result.startswith("ERROR"):
            return PublishResult(status="failed", error=f"{tool}: {result}")

    pr = await toolbox.call(
        "open_pull_request",
        {
            "title": f"[agent] {request.task.splitlines()[0][:70]}",
            "body": pr_body(request),
            "base_branch": request.base_branch,
        },
        actor=ACTOR,
        allowed=PUBLISHER_TOOLS,
    )
    if pr.startswith("ERROR"):
        return PublishResult(status="failed", error=f"open_pull_request: {pr}")
    return PublishResult(
        status="succeeded", pr_url=pr.removeprefix("PR_URL=") if pr.startswith("PR_URL=") else None
    )
