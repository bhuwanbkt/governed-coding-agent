# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-27

First release. A governed multi-agent coding agent:
- **Agents:** LangGraph agents plan, code, test and review a change in an allow-listed
  repository.
- **Control:** a policy file, budgets and an append-only audit log control every tool call.
- **Human approval:** a person approves the exact patch before a GitHub App bot pushes it and
  opens a pull request.
- **Evaluation:** measured by golden tasks with hidden tests: 22 of 22 live attempts passed, with
  0 safety violations.

### Changed (release)
- The coding run now gets a **read-only** GitHub App token. Only the publish run, after
  approval, gets push and pull-request permissions. Code executed during tests can no longer
  hold a token that could push.
- README rewritten as a front page with results, the architecture and a 5-minute quick start. The
  architecture doc is refreshed to the final design, with an index of all 14 ADRs.

### Added (release)
- `docs/threat-model.md`: assets, trust boundaries, 11 threats with their controls and residual
  risks, known limitations and hardening for untrusted code.
- Runbook: releasing, a production settings checklist, the provider daily limit.
- Release workflow template (`docs/ci/release.yml`): tags the already-deployed image with the
  version and publishes the GitHub Release from `docs/releases/`.

### Fixed (found by the first live eval)
- A provider's *daily* token limit (Groq's TPD) now hands the job off at once with a clear
  reason instead of waiting and retrying; per-minute limits are still waited out.
- Evals: attempts stopped by provider limits are *inconclusive* (not counted as passes or
  failures), the run stops when the daily limit is used up, and baselines merge partial runs.
- Retrieval eval scores at file level (headline) and symbol level (`symbol@5`) separately.
- Text files (Markdown, YAML, TOML) are chunked by a small paragraph splitter instead of
  LlamaIndex's SentenceSplitter, whose NLTK data newer NLTK refuses to open when uv installs
  packages as hard links (CI on Linux failed).
- Traces send each LLM call's prompt, task and recent turns instead of the whole growing
  history, which made span batches time out on export.

### Added (phase 6b: evaluations)
- Golden-task eval: 11 tasks over two fixture repos (solve, safety, hand-off), graded by
  hidden acceptance tests, scope rules and safety checks; no LLM judge (ADR 0014).
- `coding-agent eval golden [--offline] [--tasks] [--repeat] [--update-baseline] [--record-db]`,
  `eval retrieval [--embeddings]`, `eval validate`; JSON + Markdown reports with Langfuse links.
- Second fixture repo `evals/fixtures/shop` with real bugs to fix.
- Retrieval eval: 30 labelled questions, recall@1/5 and MRR@10 per corpus.
- CI gates (in pytest): the offline golden suite must pass 100%; retrieval must not regress
  below `evals/baseline.json`. Every task is checked to fail before and pass on its reference.
- `evals/ci/eval.yml`: manually triggered live eval workflow (copy to `.github/workflows/`).

### Added (phase 6a: observability)
- Langfuse tracing: one trace per job (coding and publishing together), with a span per graph
  step, a generation per LLM call (model, prompt version, tokens, cost) and a span per tool call
  (masked arguments, redacted result, blocked calls flagged). Scores for status, risk, cost and
  duration. Off without keys; never breaks a job (ADR 0013).
- Job page links to its trace.
- Ops page (`/ui/ops`, approvers): SLOs with met/missed, outcomes, latency (run, queue, waiting
  for a human), cost per day, top hand-off reasons, current queue and spend.
- `/metrics` in Prometheus format for a scraper, limited to `METRICS_READERS`.
- SLOs and tracing in the runbook; `coding-agent trace-check` to verify Langfuse settings.

### Fixed
- A repository URL must name one repository (`https://host/owner/repo`). A bare allowlist prefix
  such as `https://github.com/owner/` used to be queued and then fail at clone time; it's now
  refused straight away by the API (422) and the UI, with the reason. URLs with credentials, a
  query string or extra path segments are refused too.
- The new-job form no longer suggests bare prefixes; it lists the allowed prefixes as a hint.

### Added (phase 5)
- Web UI at `/ui`: a job list and a new-job form. Each job page shows the plan, review, governance
  findings, diff with its SHA-256, cost and tokens, and a live timeline. Approvers can approve or
  reject there.
- Sign-in with OpenID Connect (Zitadel): Authorization Code with PKCE, state and nonce, and
  ID-token verification via JWKS. `submitter` and `approver` roles come from the identity
  provider (ADR 0012).
- Signed, HttpOnly session cookie; CSRF tokens on every form; strict Content-Security-Policy
  with no inline scripts and no third-party code.
- Settings: `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_PROJECT_ID`,
  `OIDC_ROLES_CLAIM`, `SESSION_SECRET`, `SESSION_MAX_AGE_HOURS`.

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

[Unreleased]: https://github.com/bhuwanbkt/governed-coding-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/bhuwanbkt/governed-coding-agent/releases/tag/v1.0.0
