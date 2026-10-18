# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. You'll get a response within 7 days.

## Security model (summary)

This service runs an LLM that edits code and runs tests, so it is designed assuming the model
and the target repository's content may be adversarial:

- The agent can act only on allow-listed repositories, inside a per-job workspace.
- Each agent role has a fixed tool allowlist; unauthorised calls are blocked and audited.
- Only allow-listed test commands can run; secrets are stripped from the test environment.
- Nothing is pushed without human approval; the approver can't be the requester.
- GitHub access uses a GitHub App with short-lived, per-repository tokens.
- The audit log is append-only at the database level.

The full threat model lives in `docs/threat-model.md` (added in phase 4).
