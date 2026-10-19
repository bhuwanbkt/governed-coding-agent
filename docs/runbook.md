# Runbook

Operational procedures for production (Northflank + Neon). Grows with each phase.

## Where to look first

| Signal | Where |
|---|---|
| Is it up? | `GET /healthz`, `GET /readyz` on the production URL |
| Which version is live? | `GET /version` (commit SHA) |
| Logs | Northflank → service → Logs. Filter by `request_id` or `job_id` |
| Last deploy | GitHub → Actions → "Deploy to production" (summary shows image + rollback target) |

## Deploy failed

1. Open the failed "Deploy to production" run.
2. **Failed before "Deploy to Northflank"** (CI, build, migration): production is untouched.
   Fix forward with a new PR.
3. **Smoke test failed:** the workflow redeploys the previous image automatically. Confirm
   `/version` shows the previous SHA, then investigate logs for the failed SHA.
4. **Migration succeeded but the app failed:** the old image still works because migrations
   are backward compatible. Fix forward; don't downgrade the database in production.

## Manual rollback

GitHub → Actions → "Deploy to production" → **Run workflow** on the last good commit, or in
Northflank set the service image to `ghcr.io/<owner>/governed-coding-agent:<good-sha>`.

## `/readyz` returns 503

- `database: unreachable` — check Neon status and the project's compute (free tier scales to
  zero; first connection can take a few seconds). Verify the `DATABASE_URL` secret uses the
  **pooled** host (`-pooler`) and `sslmode=require`.
- `database: not_configured` — `DATABASE_URL` secret missing on the Northflank service.

## Job stuck or failed

Check `GET /jobs/{id}` (status, `error`, `attempts`) and `GET /jobs/{id}/audit` (last tool call).

- **`running` for a long time:** if the worker died, its heartbeat stops and the job is
  requeued automatically after `WORKER_STALE_AFTER_SECONDS` (default 300 s). After
  `WORKER_MAX_CLAIMS` claims it is marked `failed`.
- **`publishing` then `failed` ("interrupted"):** check the sandbox repo for a pushed
  `agent/<id>` branch or open PR before resubmitting; publishing is never retried automatically.
- **`failed` with a `git apply` error (patch failed):** the base branch moved after review. Resubmit the
  task; the new diff needs a new approval.
- **Groq 429 / rate limit:** retried with backoff; raise `LLM_MIN_INTERVAL_SECONDS` if frequent.
- **`403 repository is not on the allowlist`:** add the prefix to `ALLOWED_REPO_PREFIXES`.
- **API returns 503 "API keys are not configured":** set `API_KEYS` on the service.

Never edit `audit_log`; it is append-only by design (triggers reject UPDATE/DELETE).

## Rotating secrets

| Secret | Where to rotate | Then update |
|---|---|---|
| Neon password | Neon → Roles → reset | Northflank `DATABASE_URL`, GitHub `NEON_MIGRATION_DATABASE_URL` |
| Northflank API token | Northflank → Team → API tokens | GitHub secret `NORTHFLANK_API_KEY` |
| GHCR pull token | GitHub → Developer settings → tokens | Northflank registry credentials |
| Groq key | console.groq.com → API Keys | Northflank `GROQ_API_KEY` |
| Agent GitHub token | GitHub → Developer settings → Fine-grained tokens | Northflank `GITHUB_TOKEN` |
| A user's API key | generate a new key | Northflank `API_KEYS` (replace that user's entry) |

Redeploy after rotating runtime secrets.
