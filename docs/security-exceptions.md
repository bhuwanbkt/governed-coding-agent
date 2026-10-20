# Security exceptions

Known vulnerabilities we accept for now, each with a reason and a review date. CI's dependency
scan (`pip-audit`) ignores exactly these IDs and nothing else; every other finding fails the build.
Remove an entry as soon as a fixed version is available and the lock file is updated.

| ID | Package | Why it doesn't affect us | Added | Review by |
|---|---|---|---|---|
| PYSEC-2026-3740 (CVE-2026-81726, GHSA-8mgp-746c-j5xp) | nltk 3.10.3 | Path traversal in NLTK's model-artifact APIs (`TransitionParser`, `AveragedPerceptron`, `PerceptronTagger`, maxent parameters) when given caller-controlled paths. NLTK is only a transitive dependency of `llama-index-core` (sentence splitting). We never call those APIs or load NLTK models from user input, and the code index server runs read-only on a cloned repository with no tokens. No fixed release yet. | 2026-10-20 | 2026-11-20 |

## Process

1. CI fails on a new finding. Upgrade the package if a fix exists: `uv lock --upgrade-package <name>`.
2. If there is no fix, check whether the vulnerable code path is reachable from this service.
3. Only if it isn't: add a row here and the ID to `--ignore-vuln` in `.github/workflows/ci.yml`, in the
   same pull request, so the exception is reviewed like any other change.
