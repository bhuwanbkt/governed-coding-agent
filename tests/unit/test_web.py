"""Web UI: sign-in flow, sessions, CSRF, roles, escaping and security headers.

The identity provider is a fake OIDC client (the real one is covered in test_oidc.py) and the
data layer is replaced with in-memory fakes, so no database or network is needed."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.api.main import create_app
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo
from coding_agent.web.oidc import Identity, LoginRequest, OIDCError

SHA = "c" * 64
NOW = datetime.now(UTC)
BASE_JOB = repo.Job(
    id=uuid.UUID("00000000-0000-4000-8000-00000000a001"),
    repo_url="https://github.com/bhuwanbkt/agent-sandbox-repo.git",
    base_branch="main",
    task="Add a divide function",
    test_command="pytest -q",
    model="fake",
    status="awaiting_approval",
    requested_by="bhuwan",
    work_branch="agent/a001",
    pr_url=None,
    error=None,
    attempts=1,
    coder_summary=None,
    test_output=None,
    diff="diff --git a/x.py b/x.py\n+print('<b>hi</b>')\n",
    patch="patch",
    diff_sha256=SHA,
    prompt_refs=[],
    plan=None,
    review=None,
    routes=[],
    llm_calls=3,
    tokens_in=100,
    tokens_out=50,
    cost_usd=0.001,
    risk_level="low",
    policy_findings=[],
    duration_ms=1000,
    locked_by=None,
    heartbeat_at=None,
    created_at=NOW,
    started_at=NOW,
    finished_at=None,
    updated_at=NOW,
)


class FakeOIDC:
    """Stands in for OIDCClient: hands back a fixed identity, but still checks `state`."""

    def __init__(self) -> None:
        self.identity = Identity(subject="u1", name="bhuwan", email=None, roles=frozenset())

    async def authorization_url(self, redirect_uri: str, login: LoginRequest) -> str:
        return f"https://idp.example.test/authorize?state={login.state}&redirect_uri={redirect_uri}"

    async def finish(self, *, code: str, state: str, redirect_uri: str, login: LoginRequest) -> Identity:
        if state != login.state:
            raise OIDCError("This login link doesn't match the one you started. Please sign in again.")
        return self.identity


class FakeRepo:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, repo.Job] = {BASE_JOB.id: BASE_JOB}
        self.created: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.decision_error: Exception | None = None

    def list_jobs(self, limit: int = 50) -> list[repo.Job]:
        return list(self.jobs.values())[:limit]

    def get_job(self, job_id: uuid.UUID) -> repo.Job:
        if job_id not in self.jobs:
            raise repo.JobNotFoundError(str(job_id))
        return self.jobs[job_id]

    def create_job(self, **values: Any) -> repo.Job:
        self.created.append(values)
        job = replace(BASE_JOB, id=uuid.uuid4(), status="queued", **values)
        self.jobs[job.id] = job
        return job

    def list_audit(self, job_id: uuid.UUID, limit: int = 500) -> list[dict[str, Any]]:
        return [
            {
                "ts": NOW,
                "actor": "coder",
                "event_type": "tool_call",
                "allowed": True,
                "tool": "read_file",
                "args_masked": {"path": "<img src=x onerror=alert(1)>"},
                "result_summary": "ok",
            }
        ]

    def record_decision(self, job_id: uuid.UUID, **values: Any) -> repo.Job:
        if self.decision_error is not None:
            raise self.decision_error
        self.decisions.append({"job_id": job_id, **values})
        job = replace(self.jobs[job_id], status="approved" if values["approve"] else "rejected")
        self.jobs[job_id] = job
        return job


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fake = FakeRepo()
    for name in ("list_jobs", "get_job", "create_job", "list_audit", "record_decision"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    return fake


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, fake_repo: FakeRepo) -> FastAPI:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("API_KEYS", "alice:" + "a" * 26)
    monkeypatch.setenv("ALLOWED_REPO_PREFIXES", "https://github.com/bhuwanbkt/")
    get_settings.cache_clear()
    application = create_app()
    application.state.oidc = FakeOIDC()
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False)


def sign_in(client: TestClient, app: FastAPI, *roles: str, name: str = "bhuwan", next: str = "/ui") -> Any:
    app.state.oidc.identity = Identity(subject=f"sub-{name}", name=name, email=None, roles=frozenset(roles))
    start = client.get("/login", params={"next": next})
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get("/auth/callback", params={"code": "c", "state": state})


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "page has no CSRF field"
    return match.group(1)


# --------------------------------------------------------------------------- sign-in
def test_pages_redirect_to_login_when_signed_out(client: TestClient) -> None:
    response = client.get("/ui/jobs/" + str(BASE_JOB.id))
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=%2Fui%2Fjobs%2F{BASE_JOB.id}"


def test_login_redirects_to_provider_with_our_callback(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://idp.example.test/authorize?")
    assert "redirect_uri=http://testserver/auth/callback" in location


def test_sign_in_lands_on_next_page_and_shows_roles(client: TestClient, app: FastAPI) -> None:
    done = sign_in(client, app, "submitter", next=f"/ui/jobs/{BASE_JOB.id}")
    assert done.status_code == 303
    assert done.headers["location"] == f"/ui/jobs/{BASE_JOB.id}"
    cookie = done.headers["set-cookie"]
    assert "gca_session=" in cookie and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()

    page = client.get("/ui")
    assert page.status_code == 200
    assert "bhuwan" in page.text and ">submitter<" in page.text
    assert "New job" in page.text


def test_open_redirects_are_refused(client: TestClient, app: FastAPI) -> None:
    done = sign_in(client, app, "submitter", next="https://evil.example.test/")
    assert done.headers["location"] == "/ui"
    done = sign_in(client, app, "submitter", next="//evil.example.test/")
    assert done.headers["location"] == "/ui"


def test_callback_with_forged_state_or_no_login_fails(client: TestClient) -> None:
    assert client.get("/auth/callback", params={"code": "c", "state": "s"}).status_code == 400
    client.get("/login")
    forged = client.get("/auth/callback", params={"code": "c", "state": "forged"})
    assert forged.status_code == 400
    assert "doesn&#39;t match" in forged.text or "doesn't match" in forged.text
    assert client.get("/ui").status_code == 303  # still signed out


def test_provider_error_is_shown_as_cancelled(client: TestClient) -> None:
    client.get("/login")
    response = client.get("/auth/callback", params={"error": "access_denied"})
    assert response.status_code == 400
    assert "cancelled" in response.text


def test_login_without_oidc_configured_explains(client: TestClient, app: FastAPI) -> None:
    app.state.oidc = None
    response = client.get("/login")
    assert response.status_code == 503
    assert "OIDC_ISSUER" in response.text


def test_logout_needs_csrf_and_clears_the_session(client: TestClient, app: FastAPI) -> None:
    sign_in(client, app, "submitter")
    token = csrf_from(client.get("/ui").text)
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf": token}).status_code == 200
    assert client.get("/ui").status_code == 303


# --------------------------------------------------------------------------- headers & escaping
def test_security_headers_on_ui_pages(client: TestClient, app: FastAPI) -> None:
    sign_in(client, app, "submitter")
    headers = client.get("/ui").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    static = client.get("/static/app.js")
    assert static.status_code == 200 and "cache-control" not in static.headers


def test_agent_and_repo_content_is_escaped(client: TestClient, app: FastAPI, fake_repo: FakeRepo) -> None:
    evil = replace(BASE_JOB, task="<script>alert(1)</script>")
    fake_repo.jobs[evil.id] = evil
    sign_in(client, app, "approver", name="reviewer")
    page = client.get(f"/ui/jobs/{evil.id}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page  # timeline target from the audit log
    assert "&lt;b&gt;hi&lt;/b&gt;" in page  # diff line


def test_json_api_ignores_the_session_cookie(client: TestClient, app: FastAPI) -> None:
    sign_in(client, app, "submitter")
    body = {"repo_url": BASE_JOB.repo_url, "task": "Add a divide function"}
    assert client.post("/jobs", json=body).status_code == 401


# --------------------------------------------------------------------------- submitting
def test_submit_requires_csrf(client: TestClient, app: FastAPI, fake_repo: FakeRepo) -> None:
    sign_in(client, app, "submitter")
    response = client.post("/ui/jobs", data={"repo_url": BASE_JOB.repo_url, "task": "Add a thing"})
    assert response.status_code == 403
    assert fake_repo.created == []


def test_submitter_can_create_a_job(client: TestClient, app: FastAPI, fake_repo: FakeRepo) -> None:
    sign_in(client, app, "submitter")
    token = csrf_from(client.get("/ui").text)
    response = client.post(
        "/ui/jobs", data={"csrf": token, "repo_url": BASE_JOB.repo_url, "task": "Add a multiply function"}
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/jobs/")
    [created] = fake_repo.created
    assert created["requested_by"] == "bhuwan"
    assert created["task"] == "Add a multiply function"


def test_submit_without_role_or_outside_allowlist_is_refused(
    client: TestClient, app: FastAPI, fake_repo: FakeRepo
) -> None:
    sign_in(client, app, "approver", name="reviewer")
    token = csrf_from(client.get("/ui").text)
    no_role = client.post("/ui/jobs", data={"csrf": token, "repo_url": BASE_JOB.repo_url, "task": "Do it"})
    assert no_role.status_code == 403
    assert "submitter role" in no_role.text

    sign_in(client, app, "submitter")
    token = csrf_from(client.get("/ui").text)
    other = "https://github.com/someone-else/repo.git"
    outside = client.post("/ui/jobs", data={"csrf": token, "repo_url": other, "task": "Do it please"})
    assert outside.status_code == 403
    assert "allowlist" in outside.text
    assert fake_repo.created == []


def test_bare_prefix_is_refused_with_a_clear_message(
    client: TestClient, app: FastAPI, fake_repo: FakeRepo
) -> None:
    sign_in(client, app, "submitter")
    page = client.get("/ui").text
    assert "<datalist" not in page  # the form no longer suggests bare prefixes
    response = client.post(
        "/ui/jobs",
        data={"csrf": csrf_from(page), "repo_url": "https://github.com/bhuwanbkt/", "task": "Add a function"},
    )
    assert response.status_code == 422
    assert "must name one repository" in response.text
    assert "Value error" not in response.text
    assert 'value="https://github.com/bhuwanbkt/"' in response.text  # the form keeps what was typed
    assert fake_repo.created == []


# --------------------------------------------------------------------------- approving
def test_requester_sees_no_decision_buttons(client: TestClient, app: FastAPI) -> None:
    sign_in(client, app, "submitter", "approver", name="bhuwan")  # same person who requested
    page = client.get(f"/ui/jobs/{BASE_JOB.id}").text
    assert "Approve and publish" not in page
    assert "separation of duties" in page


def test_approver_approves_the_exact_diff(client: TestClient, app: FastAPI, fake_repo: FakeRepo) -> None:
    sign_in(client, app, "approver", name="reviewer")
    page = client.get(f"/ui/jobs/{BASE_JOB.id}").text
    assert "Approve and publish" in page
    response = client.post(
        f"/ui/jobs/{BASE_JOB.id}/decision",
        data={"csrf": csrf_from(page), "decision": "approve", "diff_sha256": SHA, "comment": " lgtm "},
    )
    assert response.status_code == 303
    [decision] = fake_repo.decisions
    assert decision["decided_by"] == "reviewer"
    assert decision["approve"] is True
    assert decision["diff_sha256"] == SHA
    assert decision["comment"] == "lgtm"


def test_decision_errors_map_to_clear_responses(
    client: TestClient, app: FastAPI, fake_repo: FakeRepo
) -> None:
    sign_in(client, app, "approver", name="reviewer")
    token = csrf_from(client.get(f"/ui/jobs/{BASE_JOB.id}").text)
    form = {"csrf": token, "decision": "approve", "diff_sha256": SHA}

    fake_repo.decision_error = repo.SeparationOfDutiesError("the requester cannot approve their own job")
    assert client.post(f"/ui/jobs/{BASE_JOB.id}/decision", data=form).status_code == 403
    fake_repo.decision_error = repo.DiffMismatchError("the diff changed since you looked")
    conflict = client.post(f"/ui/jobs/{BASE_JOB.id}/decision", data=form)
    assert conflict.status_code == 409
    assert "the diff changed" in conflict.text
    assert client.post(f"/ui/jobs/{BASE_JOB.id}/decision", data={**form, "csrf": "x"}).status_code == 403


def test_non_approver_cannot_decide(client: TestClient, app: FastAPI, fake_repo: FakeRepo) -> None:
    sign_in(client, app, "submitter", name="someone")
    token = csrf_from(client.get(f"/ui/jobs/{BASE_JOB.id}").text)
    response = client.post(
        f"/ui/jobs/{BASE_JOB.id}/decision", data={"csrf": token, "decision": "approve", "diff_sha256": SHA}
    )
    assert response.status_code == 403
    assert fake_repo.decisions == []


# --------------------------------------------------------------------------- live timeline
def test_timeline_fragment_stops_polling_when_job_is_done(
    client: TestClient, app: FastAPI, fake_repo: FakeRepo
) -> None:
    sign_in(client, app, "submitter")
    running = replace(BASE_JOB, id=uuid.uuid4(), status="running")
    fake_repo.jobs[running.id] = running

    live = client.get(f"/ui/jobs/{running.id}/timeline")
    assert live.status_code == 200 and "X-Poll-Stop" not in live.headers
    assert "read_file" in live.text
    done = client.get(f"/ui/jobs/{BASE_JOB.id}/timeline")
    assert done.headers["X-Poll-Stop"] == "1"


def test_unknown_job_is_404(client: TestClient, app: FastAPI) -> None:
    sign_in(client, app, "submitter")
    assert client.get(f"/ui/jobs/{uuid.uuid4()}").status_code == 404
