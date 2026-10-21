"""API-key authentication. Each key maps to a named user (API_KEYS="alice:<key>,bob:<key>"),
so every job and approval is attributed to a person. This guards the JSON API only (scripts, CI,
evals); people use the web UI, which signs in with OIDC (ADR 0012). Without API_KEYS the JSON API
refuses every request."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from coding_agent.config import get_settings


def current_user(x_api_key: Annotated[str | None, Header()] = None) -> str:
    users = get_settings().api_users()
    if not users:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API keys are not configured (API_KEYS)")
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-API-Key header")
    user = None
    for key, name in users.items():  # constant-time comparison for every configured key
        if hmac.compare_digest(key.encode(), x_api_key.encode()):
            user = name
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    return user


CurrentUser = Annotated[str, Depends(current_user)]
