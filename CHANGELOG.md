# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- A provider error during an agent's turn (e.g. Groq rejecting a malformed tool call) no longer
  crashes the job: the agent is told and retries up to twice, then the job is handed off with
  the reason. Errors now show their real cause instead of "ExceptionGroup" (job `c3059191`).
- Provider rate limits (HTTP 429, common on free tiers) are waited out with increasing backoff
  and the same request is resent; persistent rate limiting hands the job off with that reason.
- Loop detection no longer stops a coder that re-reads a file after editing it: identical calls
  are only counted while the workspace is unchanged (found in production, job `32e1226e`).
- Jobs that stop early (loop detected, time limit) now keep their LLM call, token and cost totals.

### Added (phase 4)
- GitHub App authentication: per-job installation tokens limited to one repository and one hour;
  personal token only as a local fallback (ADR 0010).
- Governance policy file (`policies/default.yaml`): protected paths, sensitive paths, diff limits,
  allowed test commands; enforced on tool calls and on the final patch (ADR 0011).
- Secret detection on the patch (hard stop) and redaction of audit events, test output,
  summaries and PR bodies.
- Prompt-injection tripwire on content agents read; a blocked action after it hands off the job.
- Budgets: list-price LLM cost per job, wall-clock time per job, daily spend across jobs.
- Approvals: approvers list, expiry (`expired` status), risk level with reasons on every job.
- Migration 0004: `risk_level`, `policy_findings`, `duration_ms`, `expired`; token and cost
  columns are now filled.
- Attack tests: CI/deploy file edits, indirect writes via tests, committed secrets, injected
  instructions, cost and time limits.

### Added (phase 3)
- Multi-agent workflow: planner, coder, tester and reviewer agents with per-role tools, prompts
  and structured outputs (`submit_plan`, `submit_review`).
- Router with `SUPERVISOR_MODE=deterministic|llm`: an LLM supervisor may propose the next step;
  a policy table and hard rules decide, and blocked proposals are audited (ADR 0008).
- LlamaIndex retrieval: code-aware chunking (`CodeSplitter`), chunk metadata, BM25 and optional
  local embeddings merged with reciprocal rank fusion, served by a new `code_index` MCP server (ADR 0009).
- Budgets: LLM calls per job, review rounds and re-plans; exhausted budgets hand off to a human.
- Migration 0003: `plan`, `review`, `routes`, `llm_calls` on jobs; shown in the API, CLI and PR body.
- Retrieval eval (hit@3 on golden queries) in the unit tests.
- Integration tests start their own disposable Postgres (Testcontainers) when
  `TEST_DATABASE_URL` isn't set; no manual database setup or port clashes.

### Changed (phase 3)
- Prompts: `coder@v2` follows the plan and reviewer findings; new `planner@v1`, `reviewer@v1`,
  `supervisor@v1`. Prompt hashes now include the partials a template uses.
- The Toolbox connects to several MCP servers; the code index server gets no tokens.

### Added (phase 2)
- Phase 2: job API (`POST /jobs`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/decision`,
  `GET /jobs/{id}/audit`) with per-user API keys and a repository allowlist.
- Postgres job queue worker (`coding-agent worker`, or embedded in the API) with heartbeats,
  stale-job recovery and graceful shutdown.
- Approval bound to the exact patch (SHA-256); publishing re-applies the approved patch to a
  fresh clone (ADR 0006). Separation of duties enforced in the API and the database.
- Every tool call, state change and decision persisted to the append-only `audit_log`.
- Migration 0002: job results, stored patch, `no_changes` status.

### Changed
- The coding run now stops at `awaiting_approval`; publishing is a separate run.
- LLM calls are paced (`LLM_MIN_INTERVAL_SECONDS`); job workspaces are cleaned up after use.
- `replace_in_file` rejects an empty `old` with guidance.

### Added (phases 0–1)
- Phase 1 core loop: single coding agent as a LangGraph workflow
  (clone -> code -> test -> fix -> human approval -> push + PR).
- Workspace MCP server: sandboxed file, search, test and git tools (path checks, test-command
  allowlist, scrubbed test environment, token redaction).
- Governed `Toolbox` MCP client: per-role tool allowlists, loop detection, audit events (ADR 0005).
- Versioned prompt registry (Jinja2 + front matter, shared safety/tool-rule partials, prompt hashes).
- Groq LLM provider with a scripted fake provider for tests and offline demos.
- `coding-agent run` CLI with a terminal approval gate; `evals/sample_repo` practice repository.
- Phase 0 foundation: uv project, FastAPI service with `/healthz`, `/readyz`, `/version`.
- Structured JSON logging with request IDs.
- Initial schema: `jobs`, `audit_log` (append-only), `approvals` (separation of duties), `eval_runs`.
- CI (lint, types, tests, migrations, security scans, Docker build) and CD to Northflank with
  smoke test and automatic rollback.
- Repo hygiene: pre-commit, Dependabot, CODEOWNERS, issue/PR templates, ADRs, runbook.
