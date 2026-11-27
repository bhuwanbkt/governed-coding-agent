# ADR 0015: The worker doesn't poll an idle database; the API wakes it

- **Status:** Accepted
- **Date:** 2026-11-27

## Context

Postgres is the job queue (ADR 0002), and the worker found work by polling it. Every 3 seconds
it sent about five queries: recover stale jobs, expire old approvals, look for an approved job,
read today's spend, look for a queued job. That's roughly 1,700 queries an hour with an empty
queue. Northflank's readiness probe also queried the database on every `/readyz` call.

Neon's free plan suspends the compute after about five minutes without queries, and suspended
time isn't billed. Constant polling meant the compute never suspended. In November 2026 it used
up the month's free compute allowance before the month ended. The database stopped accepting
connections, every worker iteration failed, `/readyz` returned 503, and no jobs (and so no
traces) ran until the allowance reset.

## Decision

- **Wake, don't poll.** On the free tier the worker runs inside the API process (ADR 0007). The
  routes that create work, `POST /jobs`, `POST /ui/jobs` and both decision routes, call
  `notify_worker()`. That's an in-memory, thread-safe signal (`worker/wake.py`), and the worker
  checks the queue straight away. While idle, it sends no queries.
- **Sleep until something is due.** After each pass the worker asks the database for the next
  housekeeping deadline (`seconds_until_housekeeping`): when a running job's heartbeat would go
  stale, or a pending approval would expire. It sleeps until the earliest of:
  - that deadline
  - the next UTC midnight, if today's budget is spent and jobs are waiting
  - a backstop, `WORKER_IDLE_SECONDS` (default 6 hours; 0 = never)
  With nothing running and nothing awaiting approval, only the backstop remains.
- **A standalone worker still polls.** `coding-agent worker` in its own process can't receive
  the in-memory signal, so it keeps `WORKER_POLL_SECONDS`. Housekeeping there also runs only
  when due.
- **Back off on failure.** A failed iteration waits `WORKER_POLL_SECONDS × 2ⁿ`, up to
  `WORKER_ERROR_BACKOFF_MAX_SECONDS` (default 300). It logs one traceback, then a reminder every
  10 minutes, then "worker recovered". A wake signal still cuts the wait short.
- **Health checks don't touch the database.** The platform's readiness probe uses `/healthz`.
  `/readyz` stays for people and for the deploy smoke test, and it reuses a success for
  `READYZ_CACHE_SECONDS` (default 60).

## Alternatives considered

- **Longer poll interval or exponential idle backoff.** This is simpler, but every check still
  wakes Neon, and a new job waits for the next check.
- **Postgres `LISTEN/NOTIFY`.** It doesn't work through Neon's pooled endpoint (PgBouncer in
  transaction mode), and it needs a connection held open.
- **A paid Neon plan.** That fixes the symptom but keeps a design that wastes compute.

## Consequences

- An idle service sends no database queries. Neon suspends about five minutes after the last
  one, and the free allowance covers actual use.
- A submitted or approved job still starts within a second. The first query after a suspension
  is slower (about 0.5–1 s) while Neon resumes.
- A job inserted into the database directly, bypassing the API, waits for the backstop. So does
  a job meant for a second API replica whose worker didn't get the signal. Neither happens in
  this deployment; a multi-replica deployment would need a shared signal or shorter
  `WORKER_IDLE_SECONDS`.
