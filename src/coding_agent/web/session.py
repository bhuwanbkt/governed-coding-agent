"""Who is using the UI: the signed-in user in the session cookie, plus CSRF protection.

The session cookie is signed (Starlette SessionMiddleware + itsdangerous), HttpOnly, SameSite=Lax
and Secure in production. It holds only the user's name, subject, roles and a CSRF token: no
provider tokens. Every state-changing form carries the CSRF token, compared in constant time.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status

from coding_agent.web.oidc import Identity, LoginRequest

USER_KEY = "user"
CSRF_KEY = "csrf"
LOGIN_KEY = "oidc_login"


class LoginRequiredError(Exception):
    """Raised by pages that need a signed-in user; the app redirects to /login."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


@dataclass(frozen=True)
class WebUser:
    subject: str
    name: str
    roles: frozenset[str]
    expires_at: float

    @property
    def can_submit(self) -> bool:
        return "submitter" in self.roles

    @property
    def is_approver(self) -> bool:
        return "approver" in self.roles


def current_user(request: Request) -> WebUser | None:
    data: dict[str, Any] | None = request.session.get(USER_KEY)
    if not data or float(data.get("exp", 0)) < time.time():
        return None
    return WebUser(
        subject=str(data["sub"]),
        name=str(data["name"]),
        roles=frozenset(data.get("roles", [])),
        expires_at=float(data["exp"]),
    )


def require_user(request: Request) -> WebUser:
    user = current_user(request)
    if user is None:
        raise LoginRequiredError(request.url.path)
    return user


def sign_in(request: Request, identity: Identity, max_age_seconds: float) -> None:
    """Start a fresh session (new CSRF token too) so nothing from before login carries over."""
    request.session.clear()
    request.session[USER_KEY] = {
        "sub": identity.subject,
        "name": identity.name,
        "roles": sorted(identity.roles),
        "exp": time.time() + max_age_seconds,
    }
    request.session[CSRF_KEY] = secrets.token_urlsafe(32)


def sign_out(request: Request) -> None:
    request.session.clear()


def remember_login(request: Request, login: LoginRequest, next_path: str) -> None:
    request.session[LOGIN_KEY] = {**login.as_dict(), "next": next_path}


def pop_login(request: Request) -> tuple[LoginRequest, str] | None:
    data = request.session.pop(LOGIN_KEY, None)
    if not data:
        return None
    return LoginRequest.from_dict(data), str(data.get("next") or "/ui")


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return str(token)


def check_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get(CSRF_KEY)
    if not expected or not submitted or not secrets.compare_digest(str(expected), submitted):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid or missing CSRF token; reload the page")


def safe_next(path: str | None) -> str:
    """Only local paths: an attacker-supplied ?next=https://evil.example must not redirect off-site."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/ui"
