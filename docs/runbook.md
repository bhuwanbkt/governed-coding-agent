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

## Rotating secrets

| Secret | Where to rotate | Then update |
|---|---|---|
| Neon password | Neon → Roles → reset | Northflank `DATABASE_URL`, GitHub `NEON_MIGRATION_DATABASE_URL` |
| Northflank API token | Northflank → Team → API tokens | GitHub secret `NORTHFLANK_API_KEY` |
| GHCR pull token | GitHub → Developer settings → tokens | Northflank registry credentials |

Redeploy after rotating runtime secrets.
