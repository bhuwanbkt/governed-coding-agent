"""Wake-up signal from the API to the embedded worker.

When the worker runs inside the API process (`RUN_WORKER_IN_API`), it doesn't poll the database
while it has nothing to do: that would keep Neon's compute awake around the clock (ADR 0015).
Instead the routes that create work - submitting a job, approving one - call `notify_worker()`,
and the worker checks the queue straight away.

Routes are plain `def` functions that FastAPI runs in a thread pool, so `notify` is thread-safe:
it hands the event to the worker's event loop with `call_soon_threadsafe`. A notification sent
before the worker has started is remembered and delivered when it binds.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading


class WakeSignal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._pending = False

    def bind(self) -> None:
        """Attach to the running event loop. Called by the worker when it starts."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._event = asyncio.Event()
            if self._pending:
                self._event.set()
                self._pending = False

    def unbind(self) -> None:
        with self._lock:
            self._loop = None
            self._event = None

    @property
    def bound(self) -> bool:
        return self._event is not None

    def notify(self) -> None:
        """Wake the worker. Safe to call from any thread, and before the worker has started."""
        with self._lock:
            loop, event = self._loop, self._event
            if loop is None or event is None:
                self._pending = True
                return
        with contextlib.suppress(RuntimeError):  # loop already closed during shutdown
            loop.call_soon_threadsafe(event.set)

    async def wait(self, stop: asyncio.Event, timeout: float | None) -> None:
        """Sleep until notified, `stop` is set, or `timeout` seconds pass (None = no timeout)."""
        event = self._event
        if event is None:
            raise RuntimeError("WakeSignal.wait() called before bind()")
        waiters = [asyncio.ensure_future(event.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            event.clear()


_signal = WakeSignal()


def wake_signal() -> WakeSignal:
    return _signal


def notify_worker() -> None:
    """Tell the embedded worker there's new work. Harmless when no worker is listening."""
    _signal.notify()
