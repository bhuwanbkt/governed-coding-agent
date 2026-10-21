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

## 8. Phase 3: multi-agent + retrieval

Nothing new is required. Optional Northflank variables:

| Variable | Default | When to change |
|---|---|---|
| `SUPERVISOR_MODE` | `deterministic` | `llm` to let a supervisor LLM propose routing (policy-checked) |
| `AGENT_MAX_LLM_CALLS` | `40` | Lower to cap cost; jobs that hit it are handed off |
| `RETRIEVAL_EMBEDDINGS` | `false` | `true` for hybrid search; check memory first (model ~100 MB + ONNX runtime) |

## 9. Phase 4: GitHub App and governance settings

### Create the GitHub App (about 10 minutes)

1. GitHub → your profile picture → **Settings** → **Developer settings** → **GitHub Apps** →
   **New GitHub App**.
2. Fill in:
   - **GitHub App name:** something unique, e.g. `bhuwanbkt-governed-agent`. Commits and PRs
     will show `<name>[bot]`.
   - **Homepage URL:** your repository URL.
   - **Webhook:** untick **Active** (the agent doesn't receive events).
   - **Repository permissions:** **Contents: Read and write**, **Pull requests: Read and write**
     (Metadata: Read-only is added automatically). Nothing else.
   - **Where can this GitHub App be installed?** Only on this account.
3. **Create GitHub App**. On the next page note the **App ID**.
4. Scroll to **Private keys** → **Generate a private key**. A `.pem` file downloads. Treat it like
   a password: don't commit it, don't paste it into chats.
5. Left menu → **Install App** → **Install** → **Only select repositories** →
   `agent-sandbox-repo` → **Install**.

### Add it to Northflank

Service → **Environment** (secret):

| Variable | Value |
|---|---|
| `GITHUB_APP_ID` | the App ID from step 3 |
| `GITHUB_APP_PRIVATE_KEY` | the full contents of the `.pem` file (multi-line is fine in Northflank; on one line, write newlines as `\n`) |
| `GIT_AUTHOR_NAME` | optional: `<app-name>[bot]` |
| `APPROVERS` | `bob` (only listed users may approve; empty = anyone except the requester) |

Optional: `MAX_COST_PER_JOB_USD` (default 0.50), `DAILY_BUDGET_USD` (5.00),
`JOB_TIMEOUT_SECONDS` (1200), `APPROVAL_TTL_HOURS` (24).

Once the App works, remove `GITHUB_TOKEN` from Northflank and delete that personal token in
GitHub: the agent no longer needs a person's credentials in production.

**Check:** run a job. Its audit log starts with a `credentials` event ("GitHub App token for
bhuwanbkt/agent-sandbox-repo …"), and the pull request is opened by `<app-name>[bot]`.

## 10. Phase 5: web UI and sign-in with Zitadel

### In Zitadel (about 10 minutes)

1. Create a project for this app only (e.g. `governed-coding-agent`). In its settings, tick
   **Return user roles during authentication** and **Only authorized users can authenticate**,
   then save.
2. **Roles:** add the keys `submitter` and `approver` exactly.
3. **New application:** type **Web**, authentication method **Code**.
   - Redirect URIs: `https://<your-app>.code.run/auth/callback` and
     `http://localhost:8080/auth/callback`
   - Post-logout URI: `https://<your-app>.code.run/ui`
   - Turn on **Development Mode** so the `http://localhost` URI is accepted.
   - Copy the Client ID and Client Secret. The secret is shown only once; keep it in a password
     manager.
4. Application → **Token Settings:** tick **User roles inside ID Token** and **User Info inside
   ID Token**.
5. **Users:** create a second user (e.g. `reviewer`). In the project's **Role Assignments**, give
   yourself `submitter` and the reviewer `approver`. With different people in each role,
   separation of duties gets exercised.

### Configure the app

Set these locally in `.env` and in Northflank as secrets:

| Variable | Value |
|---|---|
| `OIDC_ISSUER` | Your instance URL, e.g. `https://<instance>.zitadel.cloud` (no trailing slash) |
| `OIDC_CLIENT_ID` | From the application |
| `OIDC_CLIENT_SECRET` | From the application |
| `OIDC_PROJECT_ID` | The project's ID (project page header) |
| `SESSION_SECRET` | `openssl rand -base64 48`, at least 32 characters, different per environment |

Open `/ui`, sign in, and submit a job as the submitter. Then sign in as the reviewer (use a
private window) and approve it. The JSON API with `X-API-Key` still works as before.

Troubleshooting:

- **"redirect_uri missing"** in Zitadel: the URI must match exactly, including `https` and
  `/auth/callback`.
- **No role badges after sign-in:** check step 4 and the role assignments, then sign out and in
  again.
- **"issuer does not match":** remove the trailing slash from `OIDC_ISSUER`.

## Coming in later phases

| Phase | Setup needed |
|---|---|
| 6 | Langfuse project keys, optional Cerebras / Anthropic keys |
