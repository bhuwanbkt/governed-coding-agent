# ADR 0006: Bind approval to the exact patch, publish by re-applying it

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

From phase 2 the agent runs in a worker, and a human may approve hours later through the API.
By then the coding workspace may be gone (restart, scale-to-zero, another worker). We also
need certainty that what gets pushed is exactly what the human reviewed.

## Decision

Split each job into two runs:

1. **Coding run:** clone, code, test, then `export_patch` produces the full binary-safe patch.
   The job stores the readable diff, the patch and its SHA-256, and moves to
   `awaiting_approval`. Nothing is pushed.
2. **Decision:** the approver sends `diff_sha256` with their decision. It must match the stored
   hash; the approver can't be the requester (checked in the API and by a database trigger);
   one decision per job.
3. **Publish run:** a fresh clone of the base branch, then `apply_patch(patch, expected_sha256)`
   (refuses on any mismatch), commit, push to `agent/<id>`, open the PR.

## Consequences

- Restart-safe: publishing needs only the database row, not a workspace.
- Tamper-evident: a changed patch, or approval of a stale diff, is refused.
- If the base branch moved and the patch no longer applies, publishing fails cleanly and a
  human resubmits. Rebasing automatically would push code nobody reviewed.
- Patches are stored in Postgres (capped at 1 MB per job).
