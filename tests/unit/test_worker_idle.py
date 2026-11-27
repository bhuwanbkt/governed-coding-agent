"""The worker leaves an idle database alone: woken by the API, never polling while idle (ADR 0015)."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from coding_agent.config import Settings, get_settings
from coding_agent.db import jobs as repo
from coding_agent.worker import main as worker_main
from coding_agent.worker.main import Worker
from coding_agent.worker.wake import WakeSignal


class FakeQueue:
    """Stands in for the job repository and counts the queries the worker sends."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.queued = 0
        self.until_housekeeping: float | None = None
        self.spent = 0.0
        self.fail = False

    def _q(self, name: str) -> None:
        self.queries.append(name)
        if self.fail:
            raise ConnectionError("database unreachable")

    def recover_stale_jobs(self, **_: Any) -> list[Any]:
        self._q("recover")
        return []

    def expire_stale_approvals(self, **_: Any) -> list[Any]:
        self._q("expire")
        return []

    def seconds_until_housekeeping(self, **_: Any) -> float | None:
        self._q("deadline")
        return self.until_housekeeping

    def spend_today(self) -> float:
        self._q("spend")
        return self.spent

    def claim_next(self, worker_id: str, *, from_status: str, to_status: str) -> None:
        self._q(f"claim:{from_status}")
        if from_status == "queued" and self.queued:
            self.queued -= 1
            self.queries.append("ran-job")
        return None  # the "job" is recorded above; nothing to run


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeQueue]:
    fake = FakeQueue()
    for name in (
        "recover_stale_jobs",
        "expire_stale_approvals",
        "seconds_until_housekeeping",
        "spend_today",
        "claim_next",
    ):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    yield fake


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


async def _settle() -> None:
    for _ in range(20):  # let the worker's thread hops finish
        await asyncio.sleep(0.01)


def test_woken_worker_sends_no_queries_while_idle(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    cfg = settings(monkeypatch, WORKER_IDLE_SECONDS="0", WORKER_POLL_SECONDS="0.01")

    async def scenario() -> None:
        wake = WakeSignal()
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(cfg, worker_id="w", wake=wake).run(stop))
        await _settle()
        after_start = list(queue.queries)
        assert after_start[:3] == ["recover", "expire", "deadline"]  # once, at startup
        await asyncio.sleep(0.3)  # 30 poll intervals of a standalone worker
        assert queue.queries == after_start  # silence while idle

        queue.queued = 1
        wake.notify()  # what POST /jobs does
        await _settle()
        assert "ran-job" in queue.queries
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_wake_from_another_thread(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    """Routes are sync functions run in FastAPI's thread pool."""
    cfg = settings(monkeypatch, WORKER_IDLE_SECONDS="0")

    async def scenario() -> None:
        wake = WakeSignal()
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(cfg, worker_id="w", wake=wake).run(stop))
        await _settle()
        queue.queued = 1
        threading.Thread(target=wake.notify).start()
        await _settle()
        assert "ran-job" in queue.queries
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_notify_before_the_worker_starts_is_kept() -> None:
    async def scenario() -> None:
        wake = WakeSignal()
        wake.notify()
        wake.bind()
        await asyncio.wait_for(wake.wait(asyncio.Event(), timeout=None), 1)  # returns at once

    asyncio.run(scenario())


def test_standalone_worker_still_polls(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    cfg = settings(monkeypatch, WORKER_POLL_SECONDS="0.02")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(cfg, worker_id="w").run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert queue.queries.count("claim:queued") >= 5
    assert queue.queries.count("recover") == 1  # housekeeping only when due


def test_idle_wait_follows_deadlines(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    cfg = settings(monkeypatch, WORKER_IDLE_SECONDS="21600")
    w = Worker(cfg, worker_id="w", wake=WakeSignal())

    async def housekeep(seconds: float | None) -> float | None:
        queue.until_housekeeping = seconds
        await w.housekeep()
        return w.idle_wait()

    assert asyncio.run(housekeep(None)) == 21600  # nothing in flight: only the backstop
    wait = asyncio.run(housekeep(120.0))  # a job's heartbeat goes stale in 2 minutes
    assert wait is not None and 119 <= wait <= 120
    assert asyncio.run(housekeep(-5.0)) == 1.0  # overdue: soon, but never a busy loop

    no_backstop = Worker(settings(monkeypatch, WORKER_IDLE_SECONDS="0"), worker_id="w", wake=WakeSignal())
    queue.until_housekeeping = None
    asyncio.run(no_backstop.housekeep())
    assert no_backstop.idle_wait() is None  # sleep until woken


def test_spent_budget_waits_for_utc_midnight(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    cfg = settings(monkeypatch, WORKER_IDLE_SECONDS="0", DAILY_BUDGET_USD="1")
    w = Worker(cfg, worker_id="w", wake=WakeSignal())
    queue.spent = 5.0
    asyncio.run(w.housekeep())
    assert asyncio.run(w.process_one()) is False
    wait = w.idle_wait()
    assert wait is not None and 1 <= wait <= 86_401
    assert abs(wait - (worker_main._seconds_to_utc_midnight() + 1)) < 5


def test_failures_back_off_and_log_one_traceback(
    monkeypatch: pytest.MonkeyPatch, queue: FakeQueue, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = settings(monkeypatch, WORKER_POLL_SECONDS="3", WORKER_ERROR_BACKOFF_MAX_SECONDS="300")
    w = Worker(cfg, worker_id="w")
    waits: list[float] = []
    with caplog.at_level(logging.INFO, logger="coding_agent.worker.main"):
        for _ in range(10):
            try:
                raise ConnectionError("database unreachable")
            except ConnectionError:
                w._record_failure()  # called from the loop's except block, like here
            waits.append(w._error_backoff())
        w._record_success()
    assert waits[:5] == [3, 6, 12, 24, 48]
    assert max(waits) == 300
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None and errors[0].exc_info[0] is ConnectionError
    assert any(r.getMessage() == "worker recovered" for r in caplog.records)


def test_worker_survives_an_unreachable_database(monkeypatch: pytest.MonkeyPatch, queue: FakeQueue) -> None:
    cfg = settings(monkeypatch, WORKER_POLL_SECONDS="0.01", WORKER_ERROR_BACKOFF_MAX_SECONDS="0.05")
    queue.fail = True

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(cfg, worker_id="w", wake=WakeSignal()).run(stop))
        await asyncio.sleep(0.3)
        failed_attempts = queue.queries.count("recover")
        assert 2 <= failed_attempts <= 12  # backing off, not hammering
        queue.fail = False
        await asyncio.sleep(0.2)
        assert queue.queries[-1].startswith("claim:")  # recovered without a restart
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "env",
    [{"WORKER_IDLE_SECONDS": "-1"}, {"WORKER_POLL_SECONDS": "0"}, {"READYZ_CACHE_SECONDS": "-1"}],
)
def test_worker_settings_are_validated(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        settings(monkeypatch, **env)
