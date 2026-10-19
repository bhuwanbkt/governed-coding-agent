"""Runs the two halves of a job, each in its own fresh workspace and MCP server:

1. `run_coding_job`: clone, code, test, and package the result for human review.
2. `run_publish_job`: after approval, re-apply the approved patch and open the PR.

Used by the CLI (both halves in one process, with a terminal approval in between) and by the
worker (each half picked up from the Postgres queue).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from coding_agent.agent.graph import CODER_TOOLS, AgentDeps, AgentLimits, AgentState, build_graph
from coding_agent.agent.llm import ToolCallingModel, build_chat_model
from coding_agent.agent.publish import PublishRequest, PublishResult, publish
from coding_agent.agent.toolbox import AuditSink, LoopDetectedError, Toolbox
from coding_agent.config import Settings
from coding_agent.observability.logging import job_id_var
from coding_agent.prompts import get_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobRequest:
    repo_url: str
    task: str
    base_branch: str = "main"
    test_command: str = "pytest -q"


def new_job_id() -> str:
    return str(uuid.uuid4())


def work_branch_for(job_id: str) -> str:
    return f"agent/{job_id.replace('-', '')[:12]}"


def normalize_repo_url(repo_url: str) -> str:
    """Accept a local folder (handy for demos) by turning it into an absolute file:// URL."""
    if "://" not in repo_url and not repo_url.startswith("git@"):
        local = Path(repo_url).expanduser()
        if local.exists():
            return local.resolve().as_uri()
    return repo_url


def _server_env(settings: Settings) -> dict[str, str]:
    env = {
        "ALLOWED_TEST_COMMANDS": settings.allowed_test_commands,
        "GIT_AUTHOR_NAME": settings.git_author_name,
        "GIT_AUTHOR_EMAIL": settings.git_author_email,
        "GIT_COMMITTER_NAME": settings.git_author_name,
        "GIT_COMMITTER_EMAIL": settings.git_author_email,
        "PATH": os.environ.get("PATH", ""),
    }
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = virtual_env
    if settings.github_token is not None:
        env["GITHUB_TOKEN"] = settings.github_token.get_secret_value()
    return env


def _workspace(settings: Settings, job_id: str, phase: str) -> Path:
    return Path(settings.workspaces_dir).resolve() / f"{job_id}-{phase}"


def _cleanup(settings: Settings, workspace: Path) -> None:
    if not settings.keep_workspaces:
        shutil.rmtree(workspace, ignore_errors=True)


async def run_coding_job(
    request: JobRequest,
    settings: Settings,
    *,
    job_id: str | None = None,
    model: ToolCallingModel | None = None,
    audit_sink: AuditSink | None = None,
) -> AgentState:
    """Code and test the change. Ends in awaiting_approval, no_changes, handed_off or failed."""
    job_id = job_id or new_job_id()
    token = job_id_var.set(job_id)
    workspace = _workspace(settings, job_id, "code")
    initial: AgentState = {
        "job_id": job_id,
        "repo_url": normalize_repo_url(request.repo_url),
        "base_branch": request.base_branch,
        "work_branch": work_branch_for(job_id),
        "task": request.task.strip(),
        "test_command": request.test_command,
    }
    logger.info("coding started", extra={"repo_url": request.repo_url})
    final: dict[str, Any]
    try:
        async with Toolbox.connect(
            workspace, job_id=job_id, env=_server_env(settings), audit_sink=audit_sink
        ) as toolbox:
            chat_model = model or build_chat_model(settings, toolbox.schemas(sorted(CODER_TOOLS)))
            graph = build_graph(
                AgentDeps(
                    toolbox=toolbox,
                    model=chat_model,
                    prompts=get_registry(),
                    limits=AgentLimits(
                        max_attempts=1 + settings.agent_max_fix_attempts,
                        max_llm_turns=settings.agent_max_llm_turns,
                        min_llm_interval_seconds=0.0 if model else settings.llm_min_interval_seconds,
                    ),
                    provider=settings.llm_provider,
                )
            )
            # Handle errors inside the session: once they cross the MCP client's task group
            # they arrive wrapped in an ExceptionGroup.
            try:
                final = await graph.ainvoke(initial, config={"recursion_limit": 50})
            except LoopDetectedError as exc:
                final = {**initial, "status": "handed_off", "error": str(exc)}
    except Exception as exc:
        logger.exception("coding crashed")
        final = {**initial, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _cleanup(settings, workspace)
        job_id_var.reset(token)
    logger.info("coding finished", extra={"job_id": job_id, "status": final.get("status")})
    return cast(AgentState, final)


async def run_publish_job(
    request: PublishRequest, settings: Settings, *, audit_sink: AuditSink | None = None
) -> PublishResult:
    """Publish an approved job from a clean clone. Never reuses the coding workspace."""
    token = job_id_var.set(request.job_id)
    workspace = _workspace(settings, request.job_id, "publish")
    try:
        async with Toolbox.connect(
            workspace, job_id=request.job_id, env=_server_env(settings), audit_sink=audit_sink
        ) as toolbox:
            try:
                result = await publish(toolbox, request)
            except Exception as exc:
                logger.exception("publish step crashed")
                result = PublishResult(status="failed", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        logger.exception("publish crashed")
        result = PublishResult(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        _cleanup(settings, workspace)
        job_id_var.reset(token)
    logger.info("publish finished", extra={"status": result.status, "pr_url": result.pr_url})
    return result
