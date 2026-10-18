"""FastAPI application entry point.

Phase 0 exposes only operational endpoints; job, approval and UI routes arrive in later phases.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from coding_agent.api.middleware import RequestContextMiddleware
from coding_agent.config import get_settings
from coding_agent.db.session import DatabaseNotConfiguredError, ping
from coding_agent.observability.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Governed Coding Agent",
        version=settings.app_version,
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)

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
