"""GitHub App authentication: a short-lived token per job, scoped to one repository.

Flow (GitHub's documented App auth):

1. Sign a JWT with the App's private key (RS256, valid for ~9 minutes).
2. Look up the App's installation on the target repository.
3. Exchange the JWT for an installation access token restricted to that one repository and to
   `contents: write` + `pull_requests: write`. GitHub expires it after one hour.

The token lives only in the job's MCP server environment and is never stored. Pushes and PRs are
made by `<app-name>[bot]`, not by a person. Without App credentials the agent falls back to the
fine-grained personal token (`GITHUB_TOKEN`), which is how local development and tests work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import jwt

API = "https://api.github.com"
PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}


class GitHubAppError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str
    installation_id: int
    repository: str


def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """(owner, name) for https://github.com/owner/name(.git); None for anything else."""
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def app_jwt(app_id: str, private_key: str, *, now: float | None = None) -> str:
    """JWT identifying the App. Backdated 60 s for clock skew; GitHub allows at most 10 minutes."""
    issued = int(now if now is not None else time.time()) - 60
    return jwt.encode(
        {"iat": issued, "exp": issued + 9 * 60, "iss": str(app_id)}, private_key, algorithm="RS256"
    )


def installation_token(
    app_id: str,
    private_key: str,
    repo_url: str,
    *,
    client: httpx.Client | None = None,
) -> InstallationToken:
    """Mint a one-hour token that can only push to and open PRs on `repo_url`."""
    repo = parse_github_repo(repo_url)
    if repo is None:
        raise GitHubAppError(f"not a github.com repository URL: {repo_url}")
    owner, name = repo
    headers = {
        "Authorization": f"Bearer {app_jwt(app_id, private_key)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    http = client or httpx.Client(base_url=API, timeout=20)
    try:
        found = http.get(f"/repos/{owner}/{name}/installation", headers=headers)
        if found.status_code == 404:
            raise GitHubAppError(f"the GitHub App is not installed on {owner}/{name}")
        found.raise_for_status()
        installation_id = int(found.json()["id"])
        created = http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json={"repositories": [name], "permissions": PERMISSIONS},
        )
        created.raise_for_status()
        body = created.json()
    except httpx.HTTPStatusError as exc:
        raise GitHubAppError(
            f"GitHub App token request failed: {exc.response.status_code} {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GitHubAppError(f"GitHub App token request failed: {type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    return InstallationToken(
        token=str(body["token"]),
        expires_at=str(body.get("expires_at", "")),
        installation_id=installation_id,
        repository=f"{owner}/{name}",
    )
