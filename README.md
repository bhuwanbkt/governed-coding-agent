# Governed Coding Agent

[![CI](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/ci.yml)
[![Deploy](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/deploy.yml/badge.svg)](https://github.com/bhuwanbkt/governed-coding-agent/actions/workflows/deploy.yml)

A governed multi-agent coding agent. Give it a GitHub repo and a task: a team of agents
plans the change, writes the code, runs the tests and reviews the diff, then **waits for a
human to approve** before it pushes a branch and opens a pull request. Every tool call is
permission-checked, recorded in an append-only audit log, traced and measured.

> **Status:** Phase 1 (core loop) — a single governed coding agent runs from the CLI.
> See the [roadmap](#roadmap).

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
    A[Clone + index repo] --> S{Supervisor}
    S --> P[Planner] --> C[Coder] --> T[Tester]
    T -- tests fail --> C
    T -- tests pass --> R[Reviewer]
    R -- changes needed --> C
    R -- looks good --> H[/Human approval/]
    S -- limits hit --> X[Human handoff]
    H -- approve --> PB[Publisher: push + PR]
    H -- reject --> Z[Stop, nothing pushed]
```

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

### Run the agent (phase 1 CLI)

```bash
# Offline demo: scripted LLM, local copy of the sample repo, no keys needed
uv run coding-agent run --llm fake --repo <git-url-of-agent-sandbox-repo> \
  --task "Add a divide(a, b) function that raises ValueError on zero, with tests"

# Real run: needs GROQ_API_KEY and GITHUB_TOKEN in .env
uv run coding-agent run --repo https://github.com/<you>/agent-sandbox-repo \
  --task "Add a divide(a, b) function that raises ValueError on zero, with tests"
```

The agent clones the repo into `workspaces/<job-id>/`, edits code, runs the tests (retrying with
the failure output if they fail), then **stops and shows you the diff**. Nothing is pushed unless
you type `yes`. Then it pushes `agent/<job-id>` and opens a pull request.

### Tests

```bash
uv run pytest                                    # unit tests
TEST_DATABASE_URL=postgresql://agent:agent@localhost:5432/agent \
  uv run pytest -m integration                   # against the local Postgres
```

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

- Scoped tool permissions per agent; least privilege
- Bounded autonomy: step limits, timeouts, loop detection, human handoff
- Human approval before any push, with **separation of duties** (enforced in the database)
- **Append-only audit log** (UPDATE/DELETE/TRUNCATE blocked by database triggers)
- Secret/PII masking, repo allowlist, prompt-injection controls, cost budgets

## Project layout

```
src/coding_agent/
  agent/          LangGraph workflow, governed MCP Toolbox, LLM factory, job runner
  mcp_servers/    workspace MCP server (sandboxed files, tests, git)
  prompts/        versioned prompt templates + registry
  api/            FastAPI app, middleware (UI arrives in phase 5)
  db/             engine and data access
  observability/  structured logging (tracing/metrics/masking later)
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
- [ ] **2 API + worker** — Postgres job queue, audit logging, restart recovery
- [ ] **3 Multi-agent + retrieval** — supervisor + 5 agents, LlamaIndex
- [ ] **4 Governance** — permissions, guards, approvals, budgets, masking
- [ ] **5 UI + login** — GitHub OAuth, job timeline, approval screen
- [ ] **6 Evals + observability** — golden tasks, retrieval evals, Langfuse, SLOs
- [ ] **7 Release** — docs, demo video, `v1.0.0`

## Documentation

[Architecture](docs/architecture.md) · [Decisions (ADRs)](docs/adr/) · [Runbook](docs/runbook.md) ·
[Setup](docs/setup.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
