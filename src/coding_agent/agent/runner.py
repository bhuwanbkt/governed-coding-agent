"""Runs one agent job end to end: workspace, MCP server, model, graph. Used by the CLI now and
by the worker process from phase 2."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from coding_agent.agent.graph import CODER_TOOLS, AgentDeps, AgentLimits, AgentState, Approver, build_graph
from coding_agent.agent.llm import ToolCallingModel, build_chat_model
from coding_agent.agent.toolbox import LoopDetectedError, Toolbox
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
        "PATH": os.environ.get("PATH", ""),
    }
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = virtual_env
    if settings.github_token is not None:
        env["GITHUB_TOKEN"] = settings.github_token.get_secret_value()
    return env


async def run_job(
    request: JobRequest,
    settings: Settings,
    approver: Approver,
    *,
    job_id: str | None = None,
    model: ToolCallingModel | None = None,
) -> AgentState:
    """Run the workflow and return the final state. `model` overrides the configured LLM (tests)."""
    job_id = job_id or uuid.uuid4().hex[:12]
    token = job_id_var.set(job_id)
    workspace = Path(settings.workspaces_dir).resolve() / job_id
    initial: AgentState = {
        "job_id": job_id,
        "repo_url": normalize_repo_url(request.repo_url),
        "base_branch": request.base_branch,
        "work_branch": f"agent/{job_id}",
        "task": request.task,
        "test_command": request.test_command,
    }
    logger.info("job started", extra={"repo_url": request.repo_url, "workspace": str(workspace)})
    final: dict[str, Any]
    try:
        async with Toolbox.connect(workspace, job_id=job_id, env=_server_env(settings)) as toolbox:
            chat_model = model or build_chat_model(settings, toolbox.schemas(sorted(CODER_TOOLS)))
            graph = build_graph(
                AgentDeps(
                    toolbox=toolbox,
                    model=chat_model,
                    prompts=get_registry(),
                    approver=approver,
                    limits=AgentLimits(
                        max_attempts=1 + settings.agent_max_fix_attempts,
                        max_llm_turns=settings.agent_max_llm_turns,
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
        logger.exception("job crashed")
        final = {**initial, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        job_id_var.reset(token)
    logger.info("job finished", extra={"status": final.get("status"), "pr_url": final.get("pr_url")})
    return cast(AgentState, final)
