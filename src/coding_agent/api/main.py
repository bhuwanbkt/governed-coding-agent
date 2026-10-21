"""FastAPI application entry point: ops endpoints, job API, web UI, and (optionally) the embedded worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from coding_agent.api.jobs import router as jobs_router
from coding_agent.api.metrics import router as metrics_router
from coding_agent.api.middleware import RequestContextMiddleware
from coding_agent.config import get_settings
from coding_agent.db.session import DatabaseNotConfiguredError, ping
from coding_agent.observability.logging import configure_logging
from coding_agent.observability.tracing import configure as configure_tracing
from coding_agent.observability.tracing import tracer
from coding_agent.web.oidc import OIDCClient, OIDCConfig
from coding_agent.web.routes import router as web_router
from coding_agent.web.security import SecurityHeadersMiddleware
from coding_agent.web.session import LoginRequiredError

STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings)

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
        await asyncio.to_thread(tracer().flush)  # send buffered spans before the process exits

    app = FastAPI(
        title="Governed Coding Agent",
        version=settings.app_version,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    # Middleware runs outermost-last: request context wraps everything, then security headers.
    app.add_middleware(
        SessionMiddleware,
        # Without SESSION_SECRET (dev only; production refuses to start) sessions last until restart.
        secret_key=(
            settings.session_secret.get_secret_value()
            if settings.session_secret is not None
            else secrets.token_urlsafe(32)
        ),
        session_cookie="gca_session",
        max_age=int(settings.session_max_age_hours * 3600),
        same_site="lax",
        https_only=settings.app_env == "production",
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(jobs_router)
    app.include_router(metrics_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.oidc = None
    if settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret:
        app.state.oidc = OIDCClient(
            OIDCConfig(
                issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret.get_secret_value(),
                project_id=settings.oidc_project_id,
                roles_claim=settings.oidc_roles_claim,
            )
        )

    @app.exception_handler(LoginRequiredError)
    async def login_required(_: Request, exc: LoginRequiredError) -> RedirectResponse:
        return RedirectResponse(f"/login?{urlencode({'next': exc.next_path})}", status_code=303)

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
