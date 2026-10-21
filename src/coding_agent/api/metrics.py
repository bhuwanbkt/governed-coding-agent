"""Prometheus-format metrics, computed from the database (see db/ops.py).

Off unless METRICS_READERS names users from API_KEYS; a scraper then sends one of their keys in
X-API-Key. Without a reader list the endpoint answers 404, so it doesn't reveal it exists.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from coding_agent.api.auth import CurrentUser
from coding_agent.config import get_settings
from coding_agent.db import ops

router = APIRouter(tags=["ops"])


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(user: CurrentUser, days: int = Query(7, ge=1, le=90)) -> PlainTextResponse:
    settings = get_settings()
    readers = settings.metrics_reader_set()
    if not readers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if user not in readers:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a metrics reader (METRICS_READERS)")
    snap = ops.snapshot(days, daily_budget_usd=settings.daily_budget_usd)
    return PlainTextResponse(ops.prometheus(snap), media_type="text/plain; version=0.0.4")
