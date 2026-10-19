# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
