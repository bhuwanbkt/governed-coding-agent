"""Which repository URLs a job may target, checked before a job is queued.

The allowlist (`ALLOWED_REPO_PREFIXES`) says *whose* repositories are allowed; this module says
what a repository URL must *look like*. A bare prefix such as `https://github.com/owner/` passes
the allowlist but isn't a repository, so without this check the job would fail later, at clone
time. URLs with credentials, query strings or extra path segments are refused too.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
EXAMPLE = "https://github.com/<owner>/<repo>.git"


def repo_url_problem(url: str) -> str | None:
    """None if `url` names one repository; otherwise a short, user-facing reason."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"not a valid URL; use {EXAMPLE}"
    if parts.scheme == "file":  # local bare repositories: development, tests and evals
        if parts.netloc or not parts.path or parts.path.endswith("/") or parts.path == "/":
            return "a file:// URL must point to a repository folder, e.g. file:///path/to/repo.git"
        return None
    if parts.scheme != "https":
        return f"only https:// repository URLs are supported; use {EXAMPLE}"
    if parts.username or parts.password:
        return "don't put credentials in the URL; the agent authenticates with its GitHub App"
    if not parts.hostname:
        return f"the URL has no host; use {EXAMPLE}"
    if parts.query or parts.fragment:
        return "remove the ?query or #fragment from the URL"
    segments = parts.path.strip("/").split("/")
    if len(segments) != 2 or not all(segments):
        return f"the URL must name one repository (owner and repository); use {EXAMPLE}"
    owner, name = segments[0], segments[1].removesuffix(".git")
    if not _SEGMENT.match(owner) or not _SEGMENT.match(name) or name in {".", ".."}:
        return "the owner or repository name contains characters that aren't allowed"
    return None
