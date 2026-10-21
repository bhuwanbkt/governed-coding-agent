# Governed Coding Agent

[![CI](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/ci.yml)
[![Deploy](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/deploy.yml/badge.svg)](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/deploy.yml)

A governed multi-agent coding agent. Give it a GitHub repo and a task: a team of agents
plans the change, writes the code, runs the tests and reviews the diff, then **waits for a
human to approve** before it pushes a branch and opens a pull request. Every tool call is
permission-checked, recorded in an append-only audit log, traced and measured.

> **Status:** Phase 4 — planner, coder, tester and reviewer agents with LlamaIndex code search,
> governed by a policy file, budgets and a GitHub App identity, behind a job API with human
> approval. See the [roadmap](#roadmap).

## Architecture

```mermaid
flowchart LR
    UI[Browser UI] --> API[FastAPI API]
    API --> DB[(Neon Postgres<br/>jobs · audit_log · approvals<br/>eval_runs · checkpoints · pgvector)]
    W[Worker<br/>LangGraph agents] --> DB
    W --> MCP[MCP workspace server<br/>files · tests · git]
    W --> LLM[Groq / Cerebras / Claude]
    W --> IDX[LlamaIndex retrieval]
    MCP --> GH[GitHub<br/>branch + PR]
    W --> LF[Langfuse traces]
```

```mermaid
flowchart TD
    A[Clone] --> I[Index repo<br/>LlamaIndex] --> P[Planner] --> C[Coder] --> T[Tester]
    T --> D1{Router}
    D1 -- tests fail, budget left --> C
    D1 -. re-plan, LLM mode .-> P
    D1 -- tests pass --> R[Reviewer]
    D1 -- out of budget --> X[Hand off to a human]
    R --> D2{Router}
    D2 -- blocking findings, round left --> C
    D2 -- approved or rounds used --> H[/Human approval/]
    H -- approve --> PB[Publisher: push + PR]
    H -- reject --> Z[Stop, nothing pushed]
```

The router is code. With `SUPERVISOR_MODE=llm` a supervisor LLM proposes the next step and the
router accepts it only if the policy allows it in that state ([ADR 0008](docs/adr/0008-deterministic-supervisor.md)).

| Agent | LLM | Tools | Output |
|---|---|---|---|
| Planner | yes | read files, `search_code`, `search_codebase` | Structured plan: files, steps, tests, risks |
| Coder | yes | read tools + `write_file`, `replace_in_file` | Code changes and a summary |
| Tester | no | `run_tests` | Pass/fail with output |
| Reviewer | yes | read tools + `git_diff` | Verdict and findings (severity, file, line) |
| Supervisor (optional) | yes | none | Proposed next step, validated by policy |

## Tech stack

| Layer | Choice |
|---|---|
| API + UI | FastAPI, Jinja2 + HTMX |
| Agents | LangGraph (supervisor + sub-agents), LangChain |
| Retrieval | LlamaIndex (code-aware chunking, BM25 + pgvector hybrid) |
| Tools | Model Context Protocol (custom workspace server + GitHub) |
| LLMs | Groq (default), Cerebras (fallback), Claude (optional) |
| Data | Neon Postgres + pgvector, Alembic migrations |
| Observability | JSON logs with correlation IDs, Langfuse, `/metrics` |
| Delivery | uv, Docker, GitHub Actions, GHCR, Northflank |

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                          # create .venv and install everything (incl. dev tools)
uv run pre-commit install        # lint, type-check and secret-scan on every commit
cp .env.example .env

docker compose up -d db          # local Postgres (pgvector)
uv run alembic upgrade head      # create the schema
uv run uvicorn coding_agent.api.main:app --reload --port 8080
```

Open http://localhost:8080/docs. Or run the whole stack in containers: `docker compose up --build`.

### Run the agent from the CLI

```bash
# Offline demo: scripted LLM, local copy of the sample repo, no keys needed
uv run coding-agent run --llm fake --repo <git-url-of-agent-sandbox-repo> \
  --task "Add a divide(a, b) function that raises ValueError on zero, with tests"

# Real run: needs GROQ_API_KEY and GITHUB_TOKEN in .env
uv run coding-agent run --repo https://github.com/<you>/agent-sandbox-repo \
  --task "Add a divide(a, b) function that raises ValueError on zero, with tests"
```

The agents clone and index the repo, plan, edit the code, run the tests (fixing failures) and
review the diff, then the CLI **stops and shows you the plan, review and diff**. Nothing is
pushed unless you type `yes`. Then it pushes `agent/<job-id>` and opens a pull request.
Add `--supervisor llm` to try LLM-proposed routing.

### Use the job API (phase 2)

Set `API_KEYS=alice:<16+ char key>,bob:<16+ char key>` and `RUN_WORKER_IN_API=true` in `.env`
(or run `uv run coding-agent worker` in a second terminal), then:

```bash
API=http://localhost:8080
# alice submits a job (202, status "queued")
curl -s -X POST $API/jobs -H "X-API-Key: $ALICE_KEY" -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/<you>/agent-sandbox-repo.git",
       "task": "Add a divide(a, b) function that raises ValueError on zero, with tests"}'

# poll until status is "awaiting_approval"; the response holds the diff and diff_sha256
curl -s $API/jobs/$JOB_ID -H "X-API-Key: $ALICE_KEY"

# bob (not alice: separation of duties) approves that exact diff
curl -s -X POST $API/jobs/$JOB_ID/decision -H "X-API-Key: $BOB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve", "diff_sha256": "<sha from the job>", "comment": "LGTM"}'

# the worker publishes; status becomes "succeeded" with pr_url. Full trail:
curl -s $API/jobs/$JOB_ID/audit -H "X-API-Key: $BOB_KEY"
```

Job states: `queued → running → awaiting_approval → approved → publishing → succeeded`
(or `no_changes`, `rejected`, `failed`).

### Tests

```bash
uv run pytest                     # everything; integration tests start a throwaway Postgres
uv run pytest -m "not integration" # fast: skip the database tests
```

Integration tests use `TEST_DATABASE_URL` if it is set (CI does this). Otherwise they start a
disposable Postgres with [Testcontainers](https://testcontainers.com/) on a random port (Docker
must be running), and skip if Docker isn't available. They never use `DATABASE_URL` from `.env`.

In VS Code, the **Testing** panel and the **Run and Debug** configs (`API`, `Pytest`, `Alembic`)
are preconfigured in `.vscode/`.

## Delivery pipeline

```
PR  ─► CI: ruff · mypy · unit + integration tests (Postgres) · migration up/down
         · pip-audit · gitleaks · Docker build            ─► required to merge
main ─► CI again ─► build image (tag = commit SHA) ─► push to GHCR
       ─► migrate Neon ─► deploy to Northflank ─► smoke test /version + /readyz
       ─► automatic rollback to the previous image if the smoke test fails
```

Setup steps for Neon, Northflank and GitHub are in [docs/setup.md](docs/setup.md).

## Governance (by design)

| Control | How it's enforced |
|---|---|
| Least privilege | Per-agent tool allowlists in the Toolbox; read-only search server with no tokens |
| **Policy as code** | [`policies/default.yaml`](src/coding_agent/governance/policies/default.yaml): protected paths (CI, deploy, secrets), diff size limits, allowed test commands. Checked on every tool call **and** on the final diff ([ADR 0011](docs/adr/0011-policy-as-code-and-budgets.md)) |
| **Bot identity** | GitHub App: per-job, one-repository, one-hour tokens; commits by `<app>[bot]` ([ADR 0010](docs/adr/0010-github-app-identity.md)) |
| Secrets | A patch containing a token or key is never packaged; logs, test output and PR bodies are redacted |
| Prompt injection | Instruction-like content is flagged and raises risk; a blocked action after it stops the job |
| Bounded autonomy | Fix/re-plan/review rounds, LLM calls, **cost per job**, **time per job**, **daily spend**, loop detection |
| Routing | An LLM may propose the next step; code decides ([ADR 0008](docs/adr/0008-deterministic-supervisor.md)) |
| Human approval | Bound to the patch SHA; **approvers list**, **expiry**, **risk level**; requester can't approve (database trigger) |
| Audit | Append-only `audit_log` (UPDATE/DELETE/TRUNCATE blocked by triggers), with policy version on every block |

## Project layout

```
src/coding_agent/
  agent/          LangGraph multi-agent workflow, roles, schemas, governed MCP Toolbox, runner
  retrieval/      LlamaIndex chunking, metadata, hybrid BM25 + vector search
  mcp_servers/    workspace server (files, tests, git) and code_index server (search)
  prompts/        versioned prompt templates + registry
  api/            FastAPI app: job routes, API-key auth, middleware (UI in phase 5)
  worker/         Postgres queue worker: claim, heartbeat, recover, code, publish
  db/             engine, job queue and audit data access
  governance/     policy file + checks, redaction, injection detection, pricing, risk, GitHub App
  observability/  structured logging (tracing/metrics later)
  cli.py          `coding-agent` command
  config.py       typed settings from env vars
evals/sample_repo/  practice repository the agent edits in demos and evals
migrations/       Alembic migrations (hand-written SQL)
tests/            unit/ and integration/
docs/             architecture, ADRs, runbook, setup
```

## Roadmap

- [x] **0 Foundation** — repo hygiene, CI/CD to Northflank, schema, health checks
- [x] **1 Core loop** — MCP workspace server, single agent, prompt registry, push + PR
- [x] **2 API + worker** — Postgres job queue, audit logging, restart recovery
- [x] **3 Multi-agent + retrieval** — planner, coder, tester, reviewer; guarded supervisor; LlamaIndex
- [x] **4 Governance** — GitHub App, policy file, budgets, masking, injection tripwires, approvals
- [ ] **5 UI + login** — GitHub OAuth, job timeline, approval screen
- [ ] **6 Evals + observability** — golden tasks, retrieval evals, Langfuse, SLOs
- [ ] **7 Release** — docs, demo video, `v1.0.0`

## Documentation

[Architecture](docs/architecture.md) · [Decisions (ADRs)](docs/adr/) · [Runbook](docs/runbook.md) ·
[Setup](docs/setup.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
