# Contributing

## Workflow

1. Open (or pick) an issue; every change traces back to one.
2. Branch from `main`: `feat/<short-name>`, `fix/<short-name>`, `chore/<short-name>`.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/): `feat: add loop detection`.
4. Open a PR using the template. CI must be green; `main` is protected and squash-merged.
5. Merging to `main` deploys to production automatically.

## Local checks

```bash
uv sync
uv run pre-commit install
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest
```

## Database migrations

- Add a new file in `migrations/versions/` (hand-written SQL, numbered `0002_...`).
- Migrations run **before** the new code is deployed, so they must keep working with the
  previous release (expand → migrate → contract). Never rename or drop a column in one step.
- Always include a working `downgrade()`; CI runs upgrade → downgrade → upgrade.

## Releases

Tag `vX.Y.Z` on `main` after moving the "Unreleased" CHANGELOG entries under the new version.
