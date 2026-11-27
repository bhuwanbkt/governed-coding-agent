"""Job worker.

Loop: recover stale jobs and expire old approvals (when due) -> publish one approved job, else
code one queued job (unless today's LLM budget is spent) -> sleep.

- While idle, the worker embedded in the API doesn't poll: the routes that create work wake it
  (`worker/wake.py`), and it otherwise sleeps until the next housekeeping deadline, the next UTC
  midnight when the budget is spent, or the `worker_idle_seconds` backstop. So an idle service
  sends no queries and Neon can scale to zero (ADR 0015). A standalone worker polls every
  `worker_poll_seconds`.
- Failed iterations back off exponentially, log one traceback, then a reminder every 10 minutes.

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
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from coding_agent.agent.llm import ModelProvider
from coding_agent.agent.publish import PublishRequest
from coding_agent.agent.runner import JobRequest, run_coding_job, run_publish_job
from coding_agent.config import Settings
from coding_agent.db import jobs as repo
from coding_agent.observability.logging import job_id_var
from coding_agent.worker.wake import WakeSignal

logger = logging.getLogger(__name__)

ModelFactory = Callable[[], ModelProvider | None]

FAILURE_REMINDER_SECONDS = 600.0


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str | None = None,
        model_factory: ModelFactory | None = None,
        wake: WakeSignal | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._model_factory = model_factory or (lambda: None)  # None -> configured LLM
        self._wake = wake  # None -> standalone worker: poll every worker_poll_seconds
        self._budget_warned = False
        self._budget_blocked = False
        self._housekeeping_at = 0.0  # monotonic time; 0 = due now
        self._failures = 0
        self._failing_since = 0.0
        self._last_failure_log = 0.0

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "worker started",
            extra={"worker_id": self.worker_id, "mode": "woken" if self._wake else "polling"},
        )
        if self._wake is not None:
            self._wake.bind()
        try:
            while not stop.is_set():
                try:
                    if time.monotonic() >= self._housekeeping_at:
                        await self.housekeep()
                    did_work = await self.process_one()
                except Exception:
                    self._record_failure()
                    await self._sleep(stop, self._error_backoff())
                    continue
                self._record_success()
                if did_work:
                    self._housekeeping_at = 0.0  # a job changed state: recompute deadlines
                    continue
                await self._sleep(stop, self.idle_wait())
        finally:
            if self._wake is not None:
                self._wake.unbind()
        logger.info("worker stopped", extra={"worker_id": self.worker_id})

    def idle_wait(self) -> float | None:
        """Seconds to sleep with nothing to do; None = until woken."""
        now = time.monotonic()
        waits: list[float] = []
        if self._wake is None:
            waits.append(self.settings.worker_poll_seconds)
        elif self.settings.worker_idle_seconds > 0:
            waits.append(self.settings.worker_idle_seconds)
        if self._housekeeping_at != float("inf"):
            waits.append(max(self._housekeeping_at - now, 1.0))
        if self._budget_blocked:
            waits.append(_seconds_to_utc_midnight() + 1.0)
        return min(waits) if waits else None

    async def _sleep(self, stop: asyncio.Event, seconds: float | None) -> None:
        if self._wake is not None:
            await self._wake.wait(stop, seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    def _error_backoff(self) -> float:
        backoff = self.settings.worker_poll_seconds * 2 ** min(self._failures - 1, 16)
        return float(min(backoff, self.settings.worker_error_backoff_max_seconds))

    def _record_failure(self) -> None:
        now = time.monotonic()
        self._failures += 1
        if self._failures == 1:
            self._failing_since = self._last_failure_log = now
            logger.exception("worker iteration failed; retrying with backoff")
        elif now - self._last_failure_log >= FAILURE_REMINDER_SECONDS:
            self._last_failure_log = now
            logger.warning(
                "worker still failing",
                extra={"minutes": round((now - self._failing_since) / 60, 1), "attempts": self._failures},
                exc_info=True,
            )
        else:
            logger.debug("worker iteration failed again", exc_info=True)

    def _record_success(self) -> None:
        if self._failures:
            logger.info(
                "worker recovered",
                extra={"minutes": round((time.monotonic() - self._failing_since) / 60, 1)},
            )
            self._failures = 0

    async def housekeep(self) -> None:
        """Recover stale jobs, expire old approvals, and work out when that's next needed."""
        await self.recover_stale()
        seconds = await asyncio.to_thread(
            repo.seconds_until_housekeeping,
            stale_after_seconds=self.settings.worker_stale_after_seconds,
            ttl_hours=self.settings.approval_ttl_hours,
        )
        now = time.monotonic()
        self._housekeeping_at = float("inf") if seconds is None else now + max(seconds, 1.0)

    async def recover_stale(self) -> None:
        recovered = await asyncio.to_thread(
            repo.recover_stale_jobs,
            stale_after_seconds=self.settings.worker_stale_after_seconds,
            max_claims=self.settings.worker_max_claims,
        )
        for job_id, status in recovered:
            logger.warning("recovered stale job", extra={"job_id": str(job_id), "status": status})
        expired = await asyncio.to_thread(
            repo.expire_stale_approvals, ttl_hours=self.settings.approval_ttl_hours
        )
        for job_id in expired:
            logger.warning("approval request expired", extra={"job_id": str(job_id)})

    async def process_one(self) -> bool:
        """Run at most one unit of work. Finishing approved jobs comes before starting new ones."""
        job = await asyncio.to_thread(
            repo.claim_next, self.worker_id, from_status="approved", to_status="publishing"
        )
        if job is not None:
            await self._with_heartbeat(job, self._publish)
            return True
        if not await self._within_daily_budget():
            return False
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
                requested_by=job.requested_by,
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
            tokens_in=state.get("tokens_in", 0),
            tokens_out=state.get("tokens_out", 0),
            cost_usd=state.get("cost_usd", 0.0),
            risk_level=state.get("risk_level"),
            policy_findings=state.get("policy_findings", []),
            duration_ms=state.get("duration_ms"),
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
    async def _within_daily_budget(self) -> bool:
        """New coding jobs wait in the queue once today's list-price LLM spend reaches the limit.
        Approved jobs are still published (publishing uses no LLM)."""
        spent = await asyncio.to_thread(repo.spend_today)
        self._budget_blocked = spent >= self.settings.daily_budget_usd
        if not self._budget_blocked:
            self._budget_warned = False
            return True
        if not self._budget_warned:
            logger.warning(
                "daily LLM budget reached; new jobs wait until tomorrow (UTC)",
                extra={"spent_usd": round(spent, 4), "budget_usd": self.settings.daily_budget_usd},
            )
            self._budget_warned = True
        return False

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


async def run_worker_forever(
    settings: Settings, stop: asyncio.Event | None = None, *, wake: WakeSignal | None = None
) -> None:
    """Entry point for `coding-agent worker`, and for the worker embedded in the API process
    (which passes its own `stop` event and wake signal, and leaves signal handling to uvicorn)."""
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
    await Worker(settings, wake=wake).run(stop)


def _seconds_to_utc_midnight() -> float:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()
