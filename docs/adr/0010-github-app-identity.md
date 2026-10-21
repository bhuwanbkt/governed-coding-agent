# ADR 0010: The agent acts as a GitHub App with per-job, one-repository tokens

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Until now the agent pushed with a fine-grained personal access token. Every commit and pull
request looked like the person who created the token; the token was long-lived; and if it leaked,
it was that person's identity that leaked. Access was managed by editing a token, not by
installing an integration on a repository.

## Decision

- The agent authenticates as a **GitHub App** installed on the repositories it may change.
- For each job (coding and publishing separately) it signs a short JWT with the App's private key,
  finds the installation on the target repository, and requests an **installation token limited
  to that one repository** with `contents: write`, `pull_requests: write` and `metadata: read`.
  GitHub expires the token after one hour.
- The token lives only in that job's MCP server environment. It is never stored; the audit log
  records that a token was issued, for which repository and installation, and when it expires.
- If the App is configured but can't issue a token (not installed, key wrong), the job fails with
  that reason. It never falls back silently to a personal token.
- Without App credentials (local development, CI), the personal `GITHUB_TOKEN` still works.

## Consequences

- Commits and PRs are authored by `<app-name>[bot]`; a human's identity appears only as the approver.
- Revoking access is uninstalling the App from a repository; rotating is generating a new App key.
- One more secret to manage (the App private key), stored only in Northflank.
- A compromised job can reach only its own repository for at most an hour.
