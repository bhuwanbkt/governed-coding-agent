"""Runs the two halves of a job, each in its own fresh workspace and MCP servers:

1. `run_coding_job`: clone, index, plan, code, test, review, and package the result for human review.
2. `run_publish_job`: after approval, re-apply the approved patch and open the PR.

Used by the CLI (both halves in one process, with a terminal approval in between) and by the
worker (each half picked up from the Postgres queue).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from coding_agent.agent.graph import (
    AgentDeps,
    AgentLimits,
    AgentLLMError,
    AgentState,
    UsageMeter,
    build_graph,
)
from coding_agent.agent.llm import ModelProvider, build_models
from coding_agent.agent.publish import PublishRequest, PublishResult, publish
from coding_agent.agent.toolbox import AuditSink, InjectionSuspectedError, LoopDetectedError, Toolbox
from coding_agent.config import Settings
from coding_agent.governance.github_app import installation_token, parse_github_repo
from coding_agent.governance.policy import Policy, load_policy
from coding_agent.governance.redact import redact
from coding_agent.observability.logging import job_id_var
from coding_agent.observability.tracing import Tracer
from coding_agent.observability.tracing import configure as configure_tracing
from coding_agent.prompts import get_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobRequest:
    repo_url: str
    task: str
    base_branch: str = "main"
    test_command: str = "pytest -q"
    requested_by: str | None = None  # shown on the trace
    tags: tuple[str, ...] = ()  # extra trace tags, e.g. an eval run


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


def describe(exc: BaseException) -> str:
    """Readable error: the real cause, not "ExceptionGroup: unhandled errors in a TaskGroup"."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:500]


def _policy(settings: Settings) -> Policy:
    return load_policy(str(settings.policy_file) if settings.policy_file else None)


def _policy_ref(settings: Settings) -> str:
    try:
        return _policy(settings).ref
    except Exception:
        return "unknown"


def _trace_outcome(final: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "error",
        "risk_level",
        "llm_calls",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "duration_ms",
    )
    return {k: final.get(k) for k in keys if final.get(k) is not None}


def _score(tracing: Tracer, job_id: str, final: dict[str, Any]) -> None:
    """Scores make jobs filterable and chartable in Langfuse (status, risk, cost, duration)."""
    tracing.score(job_id, "status", str(final.get("status", "failed")))
    if final.get("risk_level"):
        tracing.score(job_id, "risk", str(final["risk_level"]))
    tracing.score(job_id, "cost_usd", float(final.get("cost_usd") or 0.0))
    tracing.score(job_id, "duration_s", round(float(final.get("duration_ms") or 0) / 1000, 1))


async def _github_token(
    settings: Settings, repo_url: str, job_id: str, audit_sink: AuditSink | None
) -> str | None:
    """A one-hour, one-repository GitHub App token when the App is configured; otherwise the
    personal token (local development, tests). Raises if the App is configured but can't issue
    a token: falling back silently would push with the wrong identity."""
    app_id, key = settings.github_app_id, settings.app_private_key()
    if app_id is None or key is None or parse_github_repo(repo_url) is None:
        return settings.github_token.get_secret_value() if settings.github_token else None
    issued = await asyncio.to_thread(installation_token, app_id, key, repo_url)
    if audit_sink is not None:
        await audit_sink(
            {
                "event_type": "system",
                "job_id": job_id,
                "actor": "credentials",
                "tool": None,
                "allowed": True,
                "args_masked": None,
                "result_summary": f"GitHub App token for {issued.repository} "
                f"(installation {issued.installation_id}, expires {issued.expires_at})",
                "duration_ms": None,
            }
        )
    return issued.token


def _server_env(settings: Settings, github_token: str | None = None) -> dict[str, str]:
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
    if github_token:
        env["GITHUB_TOKEN"] = github_token
    return env


def _index_env(settings: Settings) -> dict[str, str]:
    """The code index server only reads the clone: no Git identity, no tokens."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - fallback cache location only
        "RETRIEVAL_EMBEDDINGS": "true" if settings.retrieval_embeddings else "false",
        "EMBEDDING_MODEL": settings.embedding_model,
    }
    if settings.embedding_cache_dir is not None:
        env["EMBEDDING_CACHE_DIR"] = str(settings.embedding_cache_dir)
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = virtual_env
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
    models: ModelProvider | None = None,
    audit_sink: AuditSink | None = None,
) -> AgentState:
    """Plan, code, test and review the change.
    Ends in awaiting_approval, no_changes, handed_off or failed."""
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
    started = time.monotonic()
    meter = UsageMeter()
    tracing = configure_tracing(settings)
    with tracing.job(
        job_id,
        "job.code",
        input={"task": initial["task"], "repo_url": initial["repo_url"], "base_branch": request.base_branch},
        user=request.requested_by,
        tags=list(request.tags),
        metadata={"repo_url": initial["repo_url"], "policy": _policy_ref(settings)},
    ) as root:
        try:
            policy = _policy(settings)
            github_token = await _github_token(settings, initial["repo_url"], job_id, audit_sink)
            async with Toolbox.connect(
                workspace,
                job_id=job_id,
                env=_server_env(settings, github_token),
                audit_sink=audit_sink,
                servers=("workspace", "code_index"),
                server_env={"code_index": _index_env(settings)},
                policy=policy,
            ) as toolbox:
                graph = build_graph(
                    AgentDeps(
                        toolbox=toolbox,
                        models=models or build_models(settings),
                        prompts=get_registry(),
                        limits=AgentLimits(
                            max_fix_attempts=settings.agent_max_fix_attempts,
                            max_llm_turns=settings.agent_max_llm_turns,
                            max_llm_calls=settings.agent_max_llm_calls,
                            max_cost_usd=settings.max_cost_per_job_usd,
                            prices_per_mtok=settings.llm_prices(),
                            max_review_rounds=settings.agent_max_review_rounds,
                            max_replans=settings.agent_max_replans,
                            min_llm_interval_seconds=0.0 if models else settings.llm_min_interval_seconds,
                            rate_limit_backoff_seconds=0.0 if models else 20.0,
                            supervisor_mode=settings.supervisor_mode,
                        ),
                        provider=settings.llm_provider,
                        model_name=settings.llm_model,
                        policy=policy,
                        meter=meter,
                    )
                )
                # Handle errors inside the session: once they cross the MCP client's task group
                # they arrive wrapped in an ExceptionGroup.
                try:
                    async with asyncio.timeout(settings.job_timeout_seconds):
                        final = await graph.ainvoke(initial, config={"recursion_limit": 100})
                except (LoopDetectedError, InjectionSuspectedError, AgentLLMError) as exc:
                    final = {**initial, "status": "handed_off", "error": str(exc)}
                except TimeoutError:
                    final = {
                        **initial,
                        "status": "handed_off",
                        "error": f"Time limit reached ({settings.job_timeout_seconds} s); needs a human.",
                    }
                except Exception as exc:  # handled here, before the MCP task group wraps it
                    logger.exception("coding step crashed")
                    final = {**initial, "status": "failed", "error": describe(exc)}
                if toolbox.injection_flags:
                    findings = [{"kind": "injection", "detail": f} for f in toolbox.injection_flags[:20]]
                    final.setdefault("policy_findings", findings)
        except Exception as exc:
            logger.exception("coding crashed")
            final = {**initial, "status": "failed", "error": describe(exc)}
        finally:
            _cleanup(settings, workspace)
            job_id_var.reset(token)
        final["duration_ms"] = int((time.monotonic() - started) * 1000)
        final.update(meter.as_state())  # also when the run ended in an exception
        for key in ("coder_summary", "test_output", "error"):  # never store or show secrets
            if isinstance(final.get(key), str):
                final[key] = redact(final[key])
        root.update(output=_trace_outcome(final))
    _score(tracing, job_id, final)
    await asyncio.to_thread(tracing.flush)  # the SDK's flush blocks; keep the event loop free
    logger.info("coding finished", extra={"job_id": job_id, "status": final.get("status")})
    return cast(AgentState, final)


async def run_publish_job(
    request: PublishRequest, settings: Settings, *, audit_sink: AuditSink | None = None
) -> PublishResult:
    """Publish an approved job from a clean clone. Never reuses the coding workspace."""
    token = job_id_var.set(request.job_id)
    workspace = _workspace(settings, request.job_id, "publish")
    tracing = configure_tracing(settings)
    with tracing.job(
        request.job_id,
        "job.publish",
        input={"approved_by": request.approved_by, "approved_sha256": request.approved_sha256},
        metadata={"approved_by": request.approved_by},
    ) as root:
        try:
            github_token = await _github_token(settings, request.repo_url, request.job_id, audit_sink)
            async with Toolbox.connect(
                workspace,
                job_id=request.job_id,
                env=_server_env(settings, github_token),
                audit_sink=audit_sink,
                policy=_policy(settings),
            ) as toolbox:
                try:
                    result = await publish(toolbox, request)
                except Exception as exc:
                    logger.exception("publish step crashed")
                    result = PublishResult(status="failed", error=describe(exc))
        except Exception as exc:
            logger.exception("publish crashed")
            result = PublishResult(status="failed", error=describe(exc))
        finally:
            _cleanup(settings, workspace)
            job_id_var.reset(token)
        root.update(output={"status": result.status, "pr_url": result.pr_url, "error": result.error})
    tracing.score(request.job_id, "published", 1.0 if result.status == "succeeded" else 0.0)
    await asyncio.to_thread(tracing.flush)
    logger.info("publish finished", extra={"status": result.status, "pr_url": result.pr_url})
    return result
