# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |
| < 1.0 | No |

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. You'll get a response within 7 days.

## Security model (summary)

This service runs an LLM that edits code and runs tests. It is designed on the assumption that
the model, the task text and the target repository's content may be adversarial:

- **Allow-listed repositories only.** The agent works on repositories that match
  `ALLOWED_REPO_PREFIXES`, inside a per-job workspace.
- **Checked tool calls.** Each agent role has a fixed tool allowlist, and a versioned policy file
  protects CI, deploy and secret paths. Blocked calls are audited.
- **Restricted tests.** Only allow-listed test commands run, without a shell and with the
  service's environment stripped.
- **Human approval of the exact patch.** Nothing is pushed until someone approves that patch,
  and the approver can't be the requester (enforced by the database).
- **Scoped credentials.** GitHub access uses a GitHub App:
  - the coding run gets a read-only token
  - only the publish run, after approval, gets a write token
  - both are limited to one repository and expire after one hour
- **Redaction.** Secrets are kept out of patches, and logs, traces, test output and PR bodies
  are redacted.
- **Tamper-proof audit.** The audit log is append-only at the database level.

Full threat model, including **known limitations** (tests run in the service's container) and
the hardening recommended for untrusted code: [docs/threat-model.md](docs/threat-model.md).
Accepted dependency advisories are listed in
[docs/security-exceptions.md](docs/security-exceptions.md).
