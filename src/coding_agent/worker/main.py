"""Job worker.

Loop: recover stale jobs -> publish one approved job, else code one queued job -> sleep.

- Claims use `FOR UPDATE SKIP LOCKED`, so several workers can run safely (ADR 0002).
- While a job runs, a heartbeat updates `heartbeat_at`; if the worker dies, another worker's
  recovery pass requeues the job (or fails it after `worker_max_claims` interruptions).
- Every tool call is written to `audit_log` before the agent continues (fail closed).
- SIGTERM/SIGINT stop new claims; the current job finishes or is recovered by heartbeat timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Callable
from typing import Any

from coding_agent.agent.llm import ModelProvider
from coding_agent.agent.publish import PublishRequest
from coding_agent.agent.runner import JobRequest, run_coding_job, run_publish_job
from coding_agent.config import Settings
from coding_agent.db import jobs as repo
from coding_agent.observability.logging import job_id_var

logger = logging.getLogger(__name__)

ModelFactory = Callable[[], ModelProvider | None]


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str | None = None,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._model_factory = model_factory or (lambda: None)  # None -> configured LLM

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                await self.recover_stale()
                did_work = await self.process_one()
            except Exception:
                logger.exception("worker iteration failed")
                did_work = False
            if not did_work:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
        logger.info("worker stopped", extra={"worker_id": self.worker_id})

    async def recover_stale(self) -> None:
        recovered = await asyncio.to_thread(
            repo.recover_stale_jobs,
            stale_after_seconds=self.settings.worker_stale_after_seconds,
            max_claims=self.settings.worker_max_claims,
        )
        for job_id, status in recovered:
            logger.warning("recovered stale job", extra={"job_id": str(job_id), "status": status})

    async def process_one(self) -> bool:
        """Run at most one unit of work. Finishing approved jobs comes before starting new ones."""
        job = await asyncio.to_thread(
            repo.claim_next, self.worker_id, from_status="approved", to_status="publishing"
        )
        if job is not None:
            await self._with_heartbeat(job, self._publish)
            return True
        job = await asyncio.to_thread(
            repo.claim_next, self.worker_id, from_status="queued", to_status="running"
        )
        if job is not None:
            await self._with_heartbeat(job, self._code)
            return True
        return False

    # ------------------------------------------------------------------ steps
    async def _code(self, job: repo.Job) -> None:
        state = await run_coding_job(
            JobRequest(
                repo_url=job.repo_url,
                task=job.task,
                base_branch=job.base_branch,
                test_command=job.test_command,
            ),
            self.settings,
            job_id=str(job.id),
            models=self._model_factory(),
            audit_sink=self._audit_sink,
        )
        status = state.get("status", "failed")
        await asyncio.to_thread(
            repo.update_job,
            job.id,
            actor=self.worker_id,
            status=status if status != "running" else "failed",
            work_branch=state.get("work_branch"),
            coder_summary=state.get("coder_summary"),
            test_output=(state.get("test_output") or "")[-20_000:] or None,
            diff=state.get("diff") or None,
            patch=state.get("patch"),
            diff_sha256=state.get("diff_sha256"),
            prompt_refs=state.get("prompt_refs", []),
            plan=state.get("plan"),
            review=state.get("review"),
            routes=state.get("routes", []),
            llm_calls=state.get("llm_calls", 0),
            error=state.get("error"),
        )

    async def _publish(self, job: repo.Job) -> None:
        if not job.patch or not job.diff_sha256 or not job.work_branch:
            await asyncio.to_thread(
                repo.update_job,
                job.id,
                actor=self.worker_id,
                status="failed",
                error="no approved patch stored",
            )
            return
        approval = await asyncio.to_thread(repo.get_approval, job.id)
        if (
            approval is None
            or approval["decision"] != "approved"
            or approval["diff_sha256"] != job.diff_sha256
        ):
            await asyncio.to_thread(
                repo.update_job,
                job.id,
                actor=self.worker_id,
                status="failed",
                error="approval record missing",
            )
            return
        result = await run_publish_job(
            PublishRequest(
                job_id=str(job.id),
                repo_url=job.repo_url,
                base_branch=job.base_branch,
                work_branch=job.work_branch,
                task=job.task,
                patch=job.patch,
                approved_sha256=approval["diff_sha256"],
                approved_by=approval["decided_by"],
                coder_summary=job.coder_summary or "",
                test_output=job.test_output or "",
                prompt_refs=job.prompt_refs,
                plan=job.plan,
                review=job.review,
            ),
            self.settings,
            audit_sink=self._audit_sink,
        )
        await asyncio.to_thread(
            repo.update_job,
            job.id,
            actor=self.worker_id,
            status=result.status,
            pr_url=result.pr_url,
            error=result.error,
        )

    # ------------------------------------------------------------------ helpers
    async def _audit_sink(self, event: dict[str, Any]) -> None:
        await asyncio.to_thread(repo.insert_audit_event, event)

    async def _with_heartbeat(self, job: repo.Job, step: Callable[[repo.Job], Any]) -> None:
        token = job_id_var.set(str(job.id))

        async def beat() -> None:
            while True:
                await asyncio.sleep(self.settings.worker_heartbeat_seconds)
                await asyncio.to_thread(repo.heartbeat, job.id, self.worker_id)

        heartbeat_task = asyncio.create_task(beat())
        try:
            await step(job)
        except Exception as exc:
            logger.exception("job step crashed")
            await asyncio.to_thread(
                repo.update_job,
                job.id,
                actor=self.worker_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            job_id_var.reset(token)


async def run_worker_forever(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Entry point for `coding-agent worker`, and for the worker embedded in the API process
    (which passes its own `stop` event and leaves signal handling to uvicorn)."""
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
    await Worker(settings).run(stop)
