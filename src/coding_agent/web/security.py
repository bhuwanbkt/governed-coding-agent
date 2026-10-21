"""Security headers for the HTML pages.

The CSP allows only this site's own scripts and styles: no inline JavaScript, no third-party
code, no framing. Agent output and repository content are rendered as escaped text, so even if
they contain HTML, the browser shows it rather than running it.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

UI_PREFIXES = ("/ui", "/login", "/auth", "/logout", "/static")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(UI_PREFIXES):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "same-origin"
            if not request.url.path.startswith("/static"):
                response.headers["Cache-Control"] = "no-store"  # job data is not for shared caches
        return response
