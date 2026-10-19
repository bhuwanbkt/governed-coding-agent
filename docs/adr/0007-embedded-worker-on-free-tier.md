# ADR 0007: Run the worker inside the API process on the free tier

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

API and worker are separate concerns and normally separate services, so a long agent job never
competes with HTTP traffic and each scales independently. The Northflank free tier allows only
two services, and one is already used by another project.

## Decision

The worker is a normal async loop (`coding-agent worker`) that can also start inside the API's
lifespan when `RUN_WORKER_IN_API=true`. Production on the free tier uses the embedded mode;
nothing in the code assumes it, and switching to a dedicated worker service is a configuration
change (same image, command `coding-agent worker`, `RUN_WORKER_IN_API=false` on the API).

## Consequences

- One service, $0. Job concurrency is one job per process.
- Agent work runs off the event loop (subprocesses, threads for DB calls), so the API stays
  responsive, but a heavy job can still slow requests.
- Recovery doesn't depend on the mode: heartbeats and stale-job recovery work either way.
