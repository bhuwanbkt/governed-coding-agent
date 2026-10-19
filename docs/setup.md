# Setup guide

One-time setup for local development, Neon, GitHub and Northflank. Do the steps in order.

## 1. Local (your Mac)

```bash
cd ~/Documents/governed-coding-agent
uv lock                                # creates uv.lock (commit it; CI and Docker use --locked)
uv sync                                # .venv with app + dev tools
uv run pre-commit install
cp .env.example .env
docker compose up -d db && uv run alembic upgrade head
uv run pytest                          # unit tests should pass
```

Open the folder in VS Code and accept the recommended extensions. Select the interpreter
`.venv/bin/python` if VS Code doesn't pick it automatically.

## 2. Neon (database)

1. Create a Neon project, e.g. `governed-coding-agent` (separate from other projects).
2. Create a database `agent`.
3. Copy two connection strings from **Connect**:
   - **Pooled** (host contains `-pooler`) → used by the running app.
   - **Direct** (no `-pooler`) → used only for migrations.
   Both must end with `?sslmode=require`.

## 3. GitHub repository settings

**Settings → Secrets and variables → Actions**

| Kind | Name | Value |
|---|---|---|
| Secret | `NEON_MIGRATION_DATABASE_URL` | Neon **direct** connection string |
| Secret | `NORTHFLANK_API_KEY` | Northflank API token (step 4) |
| Variable | `NF_PROJECT_ID` | Northflank project ID (step 4) |
| Variable | `NF_SERVICE_ID` | Northflank service ID (step 4) |
| Variable | `NF_CREDENTIALS_ID` | Northflank registry credentials ID (leave empty if the image is public) |
| Variable | `PROD_URL` | Public URL of the Northflank service, no trailing slash |

**Settings → Environments → New environment `production`**
Optionally add yourself as a *required reviewer* to get a manual "approve deploy" gate.

**Settings → Branches → Add rule for `main`** (after the first push)
- Require a pull request before merging
- Require status checks: `Lint & type-check`, `Tests (unit + integration)`, `Security scans`, `Docker build`
- Require branches to be up to date; do not allow bypassing

**Settings → Code security** → enable Dependabot alerts, secret scanning and private
vulnerability reporting.

## 4. Northflank (hosting)

The first push to `main` builds and publishes the image to GHCR, so do it before this step:

```bash
git init -b main && git add . && git commit -m "chore: phase 0 foundation"
git remote add origin https://github.com/bhuwanbkt/governed-coding-agent.git
git push -u origin main
```

The deploy job will stop at the Northflank step on this first run; that's expected.

1. **Image access.** Either make the package public (GitHub → your profile → Packages →
   `governed-coding-agent` → Package settings → Change visibility), or create a classic GitHub
   token with `read:packages` and add it in Northflank → **Registry credentials**; copy its ID
   into `NF_CREDENTIALS_ID`.
2. **Service.** In your existing free project: **Create new → Service → Deployment**
   (deploy an external image) using `ghcr.io/bhuwanbkt/governed-coding-agent:latest`.
   - Port `8080`, HTTP, public.
   - Health checks: liveness `GET /healthz`, readiness `GET /readyz`, port 8080.
3. **Runtime secrets** (service → Environment, or a secret group):
   - `APP_ENV=production`
   - `DATABASE_URL=` Neon **pooled** connection string
   - `LOG_LEVEL=INFO`
4. **API token.** Team settings → API → create a token allowed to update deployments for this
   project. Save it as the `NORTHFLANK_API_KEY` GitHub secret.
5. Copy the project ID, service ID and public URL into the GitHub variables from step 3.
6. GitHub → Actions → "Deploy to production" → **Re-run all jobs**. It should finish with
   "Version <sha> is live and ready."

## 5. Verify

```bash
curl https://<your-service-url>/healthz    # {"status":"ok"}
curl https://<your-service-url>/readyz     # {"status":"ok","database":"ok"}
curl https://<your-service-url>/version    # {"version":"<commit sha>","env":"production"}
```

From now on, every change goes through a pull request, and merging to `main` deploys it.

## 6. Phase 1: run the agent locally

1. **Groq API key:** console.groq.com -> API Keys -> create -> add `GROQ_API_KEY=...` to `.env`.
2. **Test repository:** create an empty **public** repo `agent-sandbox-repo` on GitHub, then push
   the practice project into it:
   ```bash
   cp -r evals/sample_repo /tmp/agent-sandbox-repo && cd /tmp/agent-sandbox-repo
   git init -b main && git add . && git commit -m "chore: sample project"
   git remote add origin https://github.com/<you>/agent-sandbox-repo.git && git push -u origin main
   ```
3. **GitHub token for the agent:** GitHub -> Settings -> Developer settings -> Personal access
   tokens -> **Fine-grained tokens** -> Generate:
   - Repository access: **Only select repositories** -> `agent-sandbox-repo`
   - Permissions: **Contents: Read and write**, **Pull requests: Read and write**
   - Add it to `.env` as `GITHUB_TOKEN=...` (replaced by a GitHub App in phase 4)
4. Run:
   ```bash
   uv run coding-agent run --repo https://github.com/<you>/agent-sandbox-repo.git \
     --task "Add a divide(a, b) function that raises ValueError on zero, with tests"
   ```

## 7. Phase 2: job API and worker in production

Add these to the Northflank service (Environment / secret group) **before merging phase 2**.
The app refuses to start in production without `ALLOWED_REPO_PREFIXES`, and the deploy would
roll back.

| Variable | Value |
|---|---|
| `ALLOWED_REPO_PREFIXES` | `https://github.com/bhuwanbkt/` (comma-separated list) |
| `API_KEYS` | `alice:<key>,bob:<key>`; keys 16+ chars, e.g. `openssl rand -hex 24` |
| `RUN_WORKER_IN_API` | `true` (free tier: worker runs inside the API, ADR 0007) |
| `GROQ_API_KEY` | a separate production Groq key |
| `GITHUB_TOKEN` | fine-grained token scoped to `agent-sandbox-repo` |

Keep each person's key private; the user name before `:` is what the audit log records.

## Coming in later phases

| Phase | Setup needed |
|---|---|
| 4 | GitHub App instead of the personal token |
| 6 | Langfuse project keys, optional Cerebras / Anthropic keys |
