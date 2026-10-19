"""FastAPI application entry point: ops endpoints, job API, and (optionally) the embedded worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from coding_agent.api.jobs import router as jobs_router
from coding_agent.api.middleware import RequestContextMiddleware
from coding_agent.config import get_settings
from coding_agent.db.session import DatabaseNotConfiguredError, ping
from coding_agent.observability.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """On the free tier one service runs both the API and the worker (ADR 0007)."""
        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        if settings.run_worker_in_api:
            from coding_agent.worker.main import run_worker_forever

            worker_task = asyncio.create_task(run_worker_forever(settings, stop))
            logger.info("embedded worker started")
        yield
        if worker_task is not None:
            stop.set()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(worker_task, timeout=25)

    app = FastAPI(
        title="Governed Coding Agent",
        version=settings.app_version,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(jobs_router)

    @app.exception_handler(DatabaseNotConfiguredError)
    async def no_database(_: Request, __: DatabaseNotConfiguredError) -> JSONResponse:
        return JSONResponse({"detail": "database is not configured (DATABASE_URL)"}, status_code=503)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches dependencies."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"], response_model=None)
    def readyz() -> dict[str, Any] | JSONResponse:
        """Readiness: dependencies needed to serve traffic are reachable."""
        try:
            ping()
        except DatabaseNotConfiguredError:
            if settings.app_env == "production":
                return JSONResponse({"status": "unavailable", "database": "not_configured"}, status_code=503)
            return {"status": "ok", "database": "not_configured"}
        except Exception:
            logger.exception("readiness check failed: database unreachable")
            return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    @app.get("/version", tags=["ops"])
    def version() -> dict[str, str]:
        """Which build is running. CD uses this to confirm the new image is live."""
        return {"version": settings.app_version, "env": settings.app_env}

    logger.info("app started", extra={"version": settings.app_version, "env": settings.app_env})
    return app


app = create_app()
