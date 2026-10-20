# ADR 0008: Route between agents with code; an LLM supervisor may only propose

- **Status:** Accepted
- **Date:** 2026-10-20

## Context

Phase 3 splits the work across a planner, coder, tester and reviewer. Something has to decide
what happens after each step: fix the code, re-plan, review, finish, or hand off to a human.
A common pattern is an LLM "supervisor" that picks the next agent. But some steps must never be
skipped (tests must pass before review and approval), every LLM hop costs a rate-limited call,
and routing done by an LLM can only be measured statistically, not unit-tested.

## Decision

- The LangGraph graph routes. At two decision points (after tests, after review) a function
  computes the **options the policy allows in the current state** and a default.
- `SUPERVISOR_MODE=deterministic` (default, production): the default is taken.
- `SUPERVISOR_MODE=llm`: a supervisor agent proposes one option with a reason. The proposal is
  accepted only if it is in the allowed options; otherwise it is recorded as a `policy_block` in
  the audit log and the default is used. Hard rules apply after that (nothing reaches review or
  approval with failing tests). Every decision is audited with its source (`rule`, `llm`, `policy`).
- The static `TRANSITIONS` table lists every step reachable from each decision point; options
  outside it are a programming error.
- Budgets bound every loop: fix attempts, re-plans, reviewer rounds, turns per agent and LLM
  calls per job. When one runs out the job is handed to a human.

## Consequences

- Guarantees are code, not prompts: a misbehaving or injected supervisor can choose among safe
  options, never skip a gate.
- Both modes run the same graph, so phase 6 evaluations can compare them on success rate, LLM
  calls and cost per job, and the production choice can be made with data.
- The LLM mode adds up to one call per decision point.
