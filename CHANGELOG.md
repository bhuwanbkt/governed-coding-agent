# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
