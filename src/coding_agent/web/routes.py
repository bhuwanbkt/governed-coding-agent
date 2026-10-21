"""Web UI routes: sign-in, job list, job detail with a live timeline, and approvals.

The UI enforces the same rules as the JSON API, through the same data layer: separation of
duties and the diff SHA binding are checked in `record_decision` (and by a database trigger);
roles come from the identity provider.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from coding_agent.api.jobs import JobCreate
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo
from coding_agent.db import ops
from coding_agent.observability.tracing import tracer
from coding_agent.web import session as web_session
from coding_agent.web.oidc import OIDCClient, OIDCError, new_login_request

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ACTIVE_STATUSES = frozenset({"queued", "running", "approved", "publishing"})
STATUS_TONE = {
    "queued": "neutral",
    "running": "info",
    "awaiting_approval": "warn",
    "approved": "info",
    "publishing": "info",
    "succeeded": "good",
    "no_changes": "neutral",
    "rejected": "neutral",
    "expired": "neutral",
    "cancelled": "neutral",
    "handed_off": "bad",
    "failed": "bad",
}


# --------------------------------------------------------------------------- helpers
def ago(value: datetime | None) -> str:
    if value is None:
        return ""
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def secs(value: float | None) -> str:
    """12.3 -> '12 s', 754 -> '12.6 min', None -> a dash."""
    if value is None:
        return "\u2013"
    return f"{value:.0f} s" if value < 120 else f"{value / 60:.1f} min"


templates.env.filters["ago"] = ago
templates.env.filters["secs"] = secs
templates.env.globals["status_tone"] = STATUS_TONE


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    base = {
        "user": web_session.current_user(request),
        "csrf": web_session.csrf_token(request),
        "env": get_settings().app_env,
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)


def message(request: Request, title: str, text: str, status_code: int) -> HTMLResponse:
    return render(request, "message.html", {"title": title, "text": text}, status_code)


def oidc_client(request: Request) -> OIDCClient | None:
    client: OIDCClient | None = getattr(request.app.state, "oidc", None)
    return client


def diff_lines(diff: str | None) -> list[tuple[str, str]]:
    """(css class, text) per line; the template escapes the text."""
    lines: list[tuple[str, str]] = []
    for line in (diff or "").splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            kind = "meta"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append((kind, line))
    return lines


def timeline(job_id: uuid.UUID) -> list[dict[str, Any]]:
    events = []
    for event in repo.list_audit(job_id):
        args = event.get("args_masked") or {}
        target = next((str(args[k]) for k in ("path", "query", "pattern", "command") if args.get(k)), "")
        summary = (event.get("result_summary") or "").strip()
        events.append(
            {
                "time": event["ts"].strftime("%H:%M:%S"),
                "actor": event["actor"],
                "type": event["event_type"],
                "allowed": event.get("allowed"),
                "tool": event.get("tool"),
                "target": target[:120],
                "summary": summary.splitlines()[0][:200] if summary else "",
            }
        )
    return events


def get_job_or_404(job_id: uuid.UUID) -> repo.Job:
    try:
        return repo.get_job(job_id)
    except repo.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from None


# --------------------------------------------------------------------------- sign-in
@router.get("/login", name="login", response_model=None)
async def login(request: Request, next: str | None = None) -> Response:
    client = oidc_client(request)
    if client is None:
        return message(
            request,
            "Sign-in is not configured",
            "Set OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET to enable the web UI. "
            "The JSON API with API keys still works.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    login_request = new_login_request()
    web_session.remember_login(request, login_request, web_session.safe_next(next))
    try:
        url = await client.authorization_url(str(request.url_for("auth_callback")), login_request)
    except OIDCError as exc:
        return message(request, "Sign-in unavailable", str(exc), status.HTTP_502_BAD_GATEWAY)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/callback", name="auth_callback", response_model=None)
async def auth_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    client = oidc_client(request)
    pending = web_session.pop_login(request)
    if client is None or pending is None:
        return message(request, "Sign-in failed", "No sign-in is in progress. Please start again.", 400)
    if error or not code or not state:
        return message(request, "Sign-in cancelled", "The identity provider did not sign you in.", 400)
    login_request, next_path = pending
    try:
        identity = await client.finish(
            code=code, state=state, redirect_uri=str(request.url_for("auth_callback")), login=login_request
        )
    except OIDCError as exc:
        return message(request, "Sign-in failed", str(exc), status.HTTP_400_BAD_REQUEST)
    web_session.sign_in(request, identity, get_settings().session_max_age_hours * 3600)
    return RedirectResponse(next_path, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", name="logout", response_model=None)
def logout(request: Request, csrf: Annotated[str, Form()] = "") -> Response:
    web_session.check_csrf(request, csrf)
    web_session.sign_out(request)
    return message(request, "Signed out", "You have been signed out.", 200)


# --------------------------------------------------------------------------- pages
@router.get("/", response_model=None)
def home() -> Response:
    return RedirectResponse("/ui", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/ui", name="jobs_page", response_model=None)
def jobs_page(request: Request) -> Response:
    web_session.require_user(request)
    settings = get_settings()
    return render(
        request,
        "jobs.html",
        {"jobs": repo.list_jobs(50), "repo_prefixes": settings.repo_prefixes(), "form": {}, "error": None},
    )


@router.post("/ui/jobs", name="submit_job", response_model=None)
def submit_job(
    request: Request,
    repo_url: Annotated[str, Form()],
    task: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
    base_branch: Annotated[str, Form()] = "main",
    test_command: Annotated[str, Form()] = "pytest -q",
) -> Response:
    user = web_session.require_user(request)
    web_session.check_csrf(request, csrf)
    settings = get_settings()
    form = {"repo_url": repo_url, "task": task, "base_branch": base_branch, "test_command": test_command}

    def fail(text: str, code: int) -> HTMLResponse:
        context = {
            "jobs": repo.list_jobs(50),
            "repo_prefixes": settings.repo_prefixes(),
            "form": form,
            "error": text,
        }
        return render(request, "jobs.html", context, code)

    if not user.can_submit:
        return fail("Your account doesn't have the submitter role.", status.HTTP_403_FORBIDDEN)
    try:
        body = JobCreate(**form)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()
        )
        return fail(problems, 422)
    if not settings.repo_allowed(body.repo_url):
        return fail("That repository is not on the allowlist.", status.HTTP_403_FORBIDDEN)
    job = repo.create_job(
        repo_url=body.repo_url,
        task=body.task,
        base_branch=body.base_branch,
        test_command=body.test_command,
        model=f"{settings.llm_provider}:{settings.llm_model}",
        requested_by=user.name,
    )
    return RedirectResponse(f"/ui/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/ui/ops", name="ops_page", response_model=None)
def ops_page(request: Request, days: int = 7) -> Response:
    """Operations view for approvers: SLOs, throughput, latency, cost and hand-off reasons."""
    user = web_session.require_user(request)
    if not user.is_approver:
        text = "The ops page needs the approver role."
        return message(request, "Not allowed", text, status.HTTP_403_FORBIDDEN)
    days = days if days in (1, 7, 30) else 7
    snap = ops.snapshot(days, daily_budget_usd=get_settings().daily_budget_usd)
    peak = max((cost for _, cost, _ in snap.cost_by_day), default=0.0)
    return render(request, "ops.html", {"snap": snap, "days": days, "peak": peak})


def job_context(request: Request, job: repo.Job, error: str | None = None) -> dict[str, Any]:
    user = web_session.require_user(request)
    can_decide = (
        job.status == "awaiting_approval"
        and user.is_approver
        and user.name.lower() != job.requested_by.lower()
    )
    return {
        "job": job,
        "events": timeline(job.id),
        "diff": diff_lines(job.diff),
        "active": job.status in ACTIVE_STATUSES,
        "can_decide": can_decide,
        "own_job": user.name.lower() == job.requested_by.lower(),
        "trace_url": tracer().trace_url(str(job.id)),
        "error": error,
    }


@router.get("/ui/jobs/{job_id}", name="job_page", response_model=None)
def job_page(request: Request, job_id: uuid.UUID) -> Response:
    web_session.require_user(request)
    return render(request, "job.html", job_context(request, get_job_or_404(job_id)))


@router.get("/ui/jobs/{job_id}/timeline", name="job_timeline", response_model=None)
def job_timeline(request: Request, job_id: uuid.UUID) -> Response:
    """Fragment polled by the job page while the job is active. X-Poll-Stop ends the polling."""
    web_session.require_user(request)
    job = get_job_or_404(job_id)
    response = render(request, "_timeline.html", {"events": timeline(job.id), "job": job})
    if job.status not in ACTIVE_STATUSES:
        response.headers["X-Poll-Stop"] = "1"
    return response


@router.post("/ui/jobs/{job_id}/decision", name="decide", response_model=None)
def decide(
    request: Request,
    job_id: uuid.UUID,
    decision: Annotated[str, Form()],
    diff_sha256: Annotated[str, Form()],
    csrf: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
) -> Response:
    user = web_session.require_user(request)
    web_session.check_csrf(request, csrf)
    job = get_job_or_404(job_id)

    def fail(text: str, code: int) -> HTMLResponse:
        return render(request, "job.html", job_context(request, job, error=text), code)

    if decision not in ("approve", "reject"):
        return fail("Unknown decision.", status.HTTP_400_BAD_REQUEST)
    if not user.is_approver:
        return fail("Your account doesn't have the approver role.", status.HTTP_403_FORBIDDEN)
    settings = get_settings()
    try:
        repo.record_decision(
            job_id,
            decided_by=user.name,
            approve=decision == "approve",
            diff_sha256=diff_sha256,
            comment=comment.strip()[:2000] or None,
            ttl_hours=settings.approval_ttl_hours,
        )
    except repo.SeparationOfDutiesError as exc:
        return fail(str(exc), status.HTTP_403_FORBIDDEN)
    except (repo.InvalidJobStateError, repo.DiffMismatchError) as exc:
        return fail(str(exc), status.HTTP_409_CONFLICT)
    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
