# Architecture

## Components

| Component | Responsibility | Introduced |
|---|---|---|
| **API** (FastAPI) | UI, job submission, approvals, health/version endpoints | Phase 0 (ops), 2 (jobs), 5 (UI) |
| **Worker** | Claims queued jobs from Postgres, runs the LangGraph agents, heartbeats | Phase 2 |
| **Agent graph** (LangGraph) | Planner → Coder → Tester → Reviewer → approval; a code router (optionally LLM-proposed, policy-checked) picks fix / re-plan / review / hand-off | Phase 1–3 |
| **MCP workspace server** | Sandboxed file, search, test and git tools for one job workspace | Phase 1 |
| **MCP code index server** | `build_index`, `search_codebase` over the clone; read-only, no tokens | Phase 3 |
| **Retrieval** (LlamaIndex) | Code-aware chunking, metadata, BM25 + optional local embeddings (RRF); pgvector persistence later | Phase 3, 6 |
| **Neon Postgres** | Jobs, queue, audit log, approvals, eval runs, LangGraph checkpoints, vectors | Phase 0 |

API and worker are built from the same image and can run as separate processes, so a long
agent job never blocks HTTP traffic. On the free tier the worker runs inside the API process
(`RUN_WORKER_IN_API=true`, ADR 0007); splitting them is a configuration change.

## Job flow

```
POST /jobs ─► queued ─► worker claims (FOR UPDATE SKIP LOCKED) ─► running
  coding run: clone → index → plan → code → test (→ fix / re-plan) → review (→ one more round)
              → export patch + SHA-256
  ─► awaiting_approval   (or no_changes / handed_off / failed; nothing is pushed)
POST /jobs/{id}/decision  (approver ≠ requester, diff_sha256 must match)
  ─► approved / rejected
  publish run: fresh clone → apply_patch(expected_sha256) → commit → push agent/<id> → PR
  ─► publishing ─► succeeded (pr_url) / failed
```

Workers heartbeat every `WORKER_HEARTBEAT_SECONDS`. A job whose heartbeat is older than
`WORKER_STALE_AFTER_SECONDS` is requeued, or failed after `WORKER_MAX_CLAIMS` claims. A stale
`publishing` job is failed, never retried, because a push may already have happened.
See ADR 0006 for why approval is bound to the patch.

## Agents and routing (phase 3)

| Step | Actor in the audit log | LLM | Tools |
|---|---|---|---|
| Clone | `setup` | no | `git_clone`, `list_files` |
| Index | `indexer` | no | `build_index` |
| Plan | `planner` | yes | read tools, `search_codebase`, `submit_plan` |
| Code | `coder` | yes | read tools, `write_file`, `replace_in_file` |
| Test | `tester` | no | `run_tests` |
| Review | `reviewer` | yes | read tools, `git_diff`, `submit_review` |
| Route | `router` / `supervisor` | only in `llm` mode | `choose_next_step` |
| Package | `packager` | no | `git_diff`, `export_patch` |

Routing decisions are stored on the job (`routes`) and in the audit log with their source:
`rule` (deterministic), `llm` (accepted proposal) or `policy` (proposal rejected or overridden).
Limits: `AGENT_MAX_FIX_ATTEMPTS`, `AGENT_MAX_REPLANS`, `AGENT_MAX_REVIEW_ROUNDS`,
`AGENT_MAX_LLM_TURNS` (per coder run) and `AGENT_MAX_LLM_CALLS` (per job). See ADR 0008 and 0009.

## Data model

| Table | Purpose | Guarantees |
|---|---|---|
| `jobs` | One row per agent job and the work queue; holds plan, review, routes, diff, patch, `diff_sha256`, test output, prompt versions, LLM calls | Status values constrained |
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
