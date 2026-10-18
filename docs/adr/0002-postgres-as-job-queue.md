# ADR 0002: Use Postgres (Neon) as the job queue

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Agent jobs run for minutes and must survive restarts. The usual choices are a dedicated broker
(Redis, RabbitMQ, SQS) or the existing database. The project runs on free tiers and handles one
job at a time.

## Decision

Store jobs in the `jobs` table and have workers claim them with
`SELECT ... WHERE status = 'queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`,
plus a heartbeat column so stuck jobs can be detected and re-queued.

## Consequences

- No extra infrastructure or cost; jobs, audit log and approvals commit in one transaction.
- Throughput is bounded by Postgres, which is far above this system's needs.
- If volume grows to many concurrent workers, revisit with a dedicated queue.
