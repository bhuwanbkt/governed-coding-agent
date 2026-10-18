# ADR 0003: Build once in CI, deploy the image by commit SHA

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

Northflank can build from the repository itself, but then the deployed artifact is not the one
CI tested, and rollbacks depend on rebuilding old commits.

## Decision

GitHub Actions builds the Docker image once per commit on `main`, tags it with the commit SHA,
pushes it to GHCR, runs migrations, and tells Northflank to deploy that exact tag. A smoke test
checks `/version` returns the new SHA and `/readyz` passes; otherwise the previous SHA's image is
redeployed automatically.

## Consequences

- Every running version maps to an exact commit; rollback is a redeploy, not a rebuild.
- Migrations run before new code, so they must be backward compatible (expand/contract).
- Requires a Northflank API token and registry credentials stored as GitHub secrets/variables.
