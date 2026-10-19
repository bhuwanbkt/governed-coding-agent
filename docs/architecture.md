# Architecture

## Components

| Component | Responsibility | Introduced |
|---|---|---|
| **API** (FastAPI) | UI, job submission, approvals, health/version endpoints | Phase 0 (ops), 2 (jobs), 5 (UI) |
| **Worker** | Claims queued jobs from Postgres, runs the LangGraph agents, heartbeats | Phase 2 |
| **Agent graph** (LangGraph) | Supervisor routes Planner → Coder → Tester → Reviewer → approval → Publisher | Phase 1–3 |
| **MCP workspace server** | Sandboxed file, search, test and git tools for one job workspace | Phase 1 |
| **Retrieval** (LlamaIndex) | Code-aware chunking, metadata, BM25 + pgvector hybrid search | Phase 3, 6 |
| **Neon Postgres** | Jobs, queue, audit log, approvals, eval runs, LangGraph checkpoints, vectors | Phase 0 |

API and worker are built from the same image and can run as separate processes, so a long
agent job never blocks HTTP traffic. On the free tier the worker runs inside the API process
(`RUN_WORKER_IN_API=true`, ADR 0007); splitting them is a configuration change.

## Job flow (phase 2)

```
POST /jobs ─► queued ─► worker claims (FOR UPDATE SKIP LOCKED) ─► running
  coding run: clone → code → test (→ fix, max retries) → export patch + SHA-256
  ─► awaiting_approval   (or no_changes / failed; nothing is pushed)
POST /jobs/{id}/decision  (approver ≠ requester, diff_sha256 must match)
  ─► approved / rejected
  publish run: fresh clone → apply_patch(expected_sha256) → commit → push agent/<id> → PR
  ─► publishing ─► succeeded (pr_url) / failed
```

Workers heartbeat every `WORKER_HEARTBEAT_SECONDS`. A job whose heartbeat is older than
`WORKER_STALE_AFTER_SECONDS` is requeued, or failed after `WORKER_MAX_CLAIMS` claims. A stale
`publishing` job is failed, never retried, because a push may already have happened.
See ADR 0006 for why approval is bound to the patch.

## Data model

| Table | Purpose | Guarantees |
|---|---|---|
| `jobs` | One row per agent job and the work queue; holds diff, patch, `diff_sha256`, test output, prompt versions | Status values constrained |
| `audit_log` | Every tool call, policy block, state change and approval | **Append-only** (triggers) |
| `approvals` | Human decision bound to the exact diff (`diff_sha256`) | **Approver ≠ requester** (trigger), one per job |
| `eval_runs` | Golden-task results per model and commit | — |

## Request lifecycle

Every HTTP request gets an `X-Request-ID` (taken from the caller if valid, otherwise generated).
It is returned in the response and attached to every log line, so one request can be traced
through the logs. Agent jobs carry `job_id` the same way.

## Health endpoints

| Endpoint | Meaning | Used by |
|---|---|---|
| `/healthz` | Process is alive; no dependency checks | Northflank liveness probe |
| `/readyz` | Database reachable | Northflank readiness probe, CD smoke test |
| `/version` | Running commit SHA | CD smoke test (confirms the new image is live) |
