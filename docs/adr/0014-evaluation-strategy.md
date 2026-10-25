# ADR 0014: Evaluate with golden tasks graded by hidden tests, and a labelled retrieval set

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Unit and end-to-end tests prove the pipeline works with scripted models. They don't say whether
the *agent* does good work with a real LLM, whether a prompt or model change made it better or
worse, or whether code search finds the right code. The governance controls also need proof that
they hold when an agent tries something unsafe.

## Decision

- **Golden tasks with hidden acceptance tests.**
  - Each task has a fixture repository, a task in plain words, an expected outcome, path rules,
    tests the agent never sees, and a reference solution.
  - Grading runs the hidden tests and the repository's own tests on a fresh clone with the patch
    applied, then checks scope (allowed paths, files that must stay) and safety (no secret, no
    protected path).
  - **No LLM-as-judge.** Every verdict can be reproduced and explained.
- **Tasks are tested too.** `eval validate` (and a unit test) requires every hidden test to fail
  on the untouched fixture and pass on the reference. A task that can't fail measures nothing.
- **Three kinds of task:**
  - *solve*: the agent should succeed
  - *safety*: no unsafe patch may ever reach approval
  - *handoff*: the agent should stop rather than invent

  Safety violations are a hard failure, separate from the pass rate.
- **Two modes, one harness:**
  - *Offline*: scripted models replay the reference or the unsafe action through the real
    pipeline: MCP servers, policy, review and packaging. It's deterministic, free, must pass
    100%, and runs on every PR.
  - *Live*: the configured LLM, run on demand, from a laptop or a manually triggered workflow,
    since live runs cost money, take minutes and vary between runs.
- **Baseline with tolerance.** `evals/baseline.json` is updated deliberately and reviewed in PRs.
  Live pass rates may move 15 points before a run fails; offline and safety allow no change.
- **Retrieval as its own suite.** 30 labelled questions over the fixtures and this codebase,
  scored with recall@1/5 and MRR@10. BM25 runs in CI; hybrid search runs on demand.
- **Traceability.** Live runs are tagged in Langfuse (`eval-run:<id>`, `task:<id>`) with an
  `eval_passed` score, so a failing task links straight to its trace. Results can be stored in
  `eval_runs` to track trends over time.

## Consequences

- Eleven tasks is a smoke-level benchmark, not a leaderboard. It catches regressions and shows
  behaviour; it doesn't estimate general coding ability. Tasks are cheap to add (see
  docs/evals.md).
- Live results are noisy, especially on free-tier rate limits. `--repeat` and the tolerance
  absorb some of that; trends matter more than single runs.
- Hidden tests can be gamed only by changing the files they import, and the scope rules limit
  that. They can still miss quality problems a human would see, which is what the approval step
  is for.
