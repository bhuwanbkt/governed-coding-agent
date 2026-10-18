# ADR 0004: Enforce key governance rules in the database too

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

The application enforces approvals and audit logging, but a bug, a bad migration or a manual
query could still alter history or let someone approve their own change.

## Decision

Defence in depth at the database level:
- `audit_log` rejects UPDATE, DELETE and TRUNCATE via triggers (append-only).
- `approvals` rejects a decision where `decided_by` equals the job's `requested_by`
  (case-insensitive), and allows only one decision per job.
- Each approval stores `diff_sha256`, binding it to the exact diff that was reviewed.

## Consequences

- Audit rows can't be cleaned up from the app; retention, if ever needed, is a deliberate
  migration reviewed in a PR.
- Integration tests must use a throwaway database.
