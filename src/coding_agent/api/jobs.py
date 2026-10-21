"""Job endpoints: submit work, inspect results, decide on the diff, read the audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from coding_agent.api.auth import CurrentUser
from coding_agent.config import get_settings
from coding_agent.db import jobs as repo

router = APIRouter(prefix="/jobs", tags=["jobs"])

Task = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=4000)]
Branch = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._/-]{1,100}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class JobCreate(BaseModel):
    repo_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=500)]
    task: Task
    base_branch: Branch = "main"
    test_command: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = "pytest -q"


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    repo_url: str
    base_branch: str
    task: str
    requested_by: str
    work_branch: str | None
    pr_url: str | None
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class JobDetail(JobSummary):
    test_command: str
    model: str
    coder_summary: str | None
    test_output: str | None
    diff: str | None
    diff_sha256: str | None = Field(
        description="Send this back with your decision to approve this exact diff"
    )
    prompt_refs: list[str]
    plan: dict[str, Any] | None = Field(description="The planner agent's plan")
    review: dict[str, Any] | None = Field(
        description="The reviewer agent's verdict and findings; advisory, the human decides"
    )
    routes: list[dict[str, Any]] = Field(description="Routing decisions between agents, with reasons")
    llm_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float = Field(description="List-price LLM cost (USD); $0 is billed on a free tier")
    duration_ms: int | None
    risk_level: str | None = Field(description="low | medium | high: how carefully to review")
    policy_findings: list[dict[str, Any]] = Field(
        description="Injection hits, secrets, policy violations and risk reasons"
    )


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    diff_sha256: Sha256
    comment: Annotated[str, StringConstraints(max_length=2000)] | None = None


class AuditEvent(BaseModel):
    id: int
    ts: datetime
    actor: str
    event_type: str
    tool: str | None
    allowed: bool | None
    args_masked: dict[str, Any] | None
    result_summary: str | None
    duration_ms: int | None


def _get(job_id: uuid.UUID) -> repo.Job:
    try:
        return repo.get_job(job_id)
    except repo.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from None


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobSummary)
def submit_job(body: JobCreate, user: CurrentUser) -> repo.Job:
    """Queue a coding job. A worker picks it up; poll `GET /jobs/{id}` for progress."""
    settings = get_settings()
    if not settings.repo_allowed(body.repo_url):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "repository is not on the allowlist")
    return repo.create_job(
        repo_url=body.repo_url,
        task=body.task,
        base_branch=body.base_branch,
        test_command=body.test_command,
        model=f"{settings.llm_provider}:{settings.llm_model}",
        requested_by=user,
    )


@router.get("", response_model=list[JobSummary])
def list_jobs(_: CurrentUser, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[repo.Job]:
    return repo.list_jobs(limit)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: uuid.UUID, _: CurrentUser) -> repo.Job:
    return _get(job_id)


@router.post("/{job_id}/decision", response_model=JobDetail)
def decide(job_id: uuid.UUID, body: Decision, user: CurrentUser) -> repo.Job:
    """Approve or reject the diff of a job awaiting approval. You can't decide on your own job,
    only listed approvers may decide (if APPROVERS is set), and requests expire."""
    settings = get_settings()
    try:
        return repo.record_decision(
            job_id,
            decided_by=user,
            approve=body.decision == "approve",
            diff_sha256=body.diff_sha256,
            comment=body.comment,
            approvers=settings.approver_set(),
            ttl_hours=settings.approval_ttl_hours,
        )
    except repo.JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from None
    except (repo.SeparationOfDutiesError, repo.NotAnApproverError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except (repo.InvalidJobStateError, repo.DiffMismatchError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/{job_id}/audit", response_model=list[AuditEvent])
def audit_trail(job_id: uuid.UUID, _: CurrentUser) -> list[dict[str, Any]]:
    _get(job_id)
    return repo.list_audit(job_id)
