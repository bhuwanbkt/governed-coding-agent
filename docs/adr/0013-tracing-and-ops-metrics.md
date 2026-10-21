# ADR 0013: Langfuse tracing per job; ops metrics and SLOs computed from Postgres

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Logs and the audit table show *what* happened in a job, but not easily *why*:
- which prompt version an agent saw
- what the model answered
- how many tokens each call used
- where the time went

Operating the service also needs numbers: are jobs finishing in time, is the worker keeping up,
what is it costing? The free tier has no metrics backend or alerting.

## Decision

- **Langfuse for traces** (Cloud, SDK v5, OpenTelemetry based). One trace per job, with its ID
  derived from the job ID, so the coding run and the later publish run land in the same trace.
  The UI can link to it without storing anything. Inside the trace:
  - a span per graph step
  - a *generation* per LLM call: model, prompt version, tokens, list-price cost, the error if
    the provider rejected the call
  - a span per tool call: masked arguments, redacted result, blocked calls marked WARNING
  - scores for status, risk, cost and duration
- **Manual instrumentation, not the LangChain callback.** The graph has one LLM call site and the
  Toolbox has one tool call site, so a thin `observability/tracing.py` adapter instruments both.
  That gives precise names, our own masking, and no dependency on callback internals.
- **Masking before export.** Langfuse's `mask` hook runs the same redactor as the audit log on
  every input, output and metadata value, and cuts long values (8,000 characters, 60 list items).
- **Optional and fail-safe.** Without keys the tracer is a no-op. Tests never trace (`conftest.py`
  installs a no-op tracer). Tracer errors are logged and swallowed. Flushing runs in a thread, so
  it never blocks the event loop.
- **Ops metrics from the database.** Every number the SLOs need is already in `jobs`, `approvals`
  and `audit_log`:
  - when a job was created, started and finished
  - what it cost
  - why it was handed off
  - when a human decided

  `db/ops.py` computes a snapshot over 1, 7 or 30 days. It's shown on `/ui/ops` (approvers) and
  exported at `/metrics` in Prometheus format for a future scraper (off unless `METRICS_READERS`
  is set).
- **SLOs:**
  - 95% of coding runs finish within 10 minutes
  - at most 5% of finished jobs end in `failed` (hand-offs are the agent working as designed and
    don't count)
  - p95 queue wait under 60 seconds
  - spend within the daily budget

  The targets are in code; the responses are in the runbook.

## Consequences

- Prompts and code snippets go to a third party (Langfuse Cloud), after redaction. For private
  code, self-host Langfuse or set `LANGFUSE_SAMPLE_RATE` lower. The keys are the off switch.
- The free Langfuse plan's monthly observation quota is enough for a few hundred jobs a month (a
  job is roughly 50–100 observations).
- SLO numbers are computed on request (a handful of aggregate queries over indexed columns). At
  much larger volumes they'd move to a metrics store fed by `/metrics`.
- There is no alerting; missed SLOs are visible on the ops page and to any scraper.
