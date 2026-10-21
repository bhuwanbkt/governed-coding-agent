# ADR 0011: Governance policy as a versioned file; budgets bound cost and time

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Phases 1–3 enforced *which tools* each agent may call. They did not limit *what* an allowed tool
does (the coder could write `.github/workflows/*`), how big a change may be, how much a job may
spend, or how long it may run. Secrets could reach logs, and text in a repository could try to
steer the agents.

## Decision

- **Policy file** (`governance/policies/default.yaml`, override with `POLICY_FILE`): protected
  paths, sensitive paths, diff size limits and allowed test commands. It is reviewed in pull
  requests like code; its version and hash (`policy@v1#…`) are recorded with every block.
- **Two enforcement points:** the Toolbox checks every tool call's arguments; the packager checks
  the final patch (protected paths, size), which also catches files created indirectly, e.g. by
  a test run.
- **Secrets:** a patch containing a high-confidence secret (known token formats, private keys) is
  never packaged; the job is handed to a human. Everything stored or shown (audit log, test
  output, summaries, PR bodies) is passed through a redactor first.
- **Prompt injection:** content agents read is scanned for instruction-like text. Hits are
  audited and raise the job's risk level. A blocked action after such content stops the job.
  Detection is a tripwire; the actual protection is that agents can't do harmful things
  (allowlists, protected paths, human approval) even if they follow injected text.
- **Budgets:** list-price LLM cost per job (`MAX_COST_PER_JOB_USD`), wall-clock time per job
  (`JOB_TIMEOUT_SECONDS`), and spend per UTC day across jobs (`DAILY_BUDGET_USD`; new jobs wait).
- **Approvals:** optional approvers list (`APPROVERS`), and requests expire after
  `APPROVAL_TTL_HOURS`. Each job carries a risk level (low/medium/high) with reasons.

## Consequences

- Policy changes are visible, reviewable and traceable to a version.
- Test commands still run with the agent's network access; isolating them (no network, separate
  sandbox such as gVisor or Firecracker) is a deployment concern not solved here.
- Injection detection has false negatives by nature; it is one layer, not the defence.
- Prices in the cost table are list prices and need updating when providers change them.
- Requiring two approvers for high-risk changes needs a schema change (one approval per job
  today) and is left for later.
