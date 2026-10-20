from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.config import get_settings
from coding_agent.db.session import get_engine

SAMPLE_REPO = Path(__file__).resolve().parents[1] / "evals" / "sample_repo"
POSTGRES_IMAGE = "pgvector/pgvector:pg16"  # same image as docker-compose.yml


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test builds settings from its own env, never from a developer's local .env."""
    monkeypatch.setenv("APP_ENV", "test")
    _clear_caches()
    yield
    _clear_caches()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Postgres for the integration tests, chosen in this order:

    1. TEST_DATABASE_URL, if set (CI points it at its own Postgres service).
    2. Otherwise a throwaway container started by Testcontainers on a random free port,
       removed after the run. Needs Docker running; no port clashes with a local Postgres.
    3. Otherwise (no Docker) the integration tests are skipped.

    Never the app's DATABASE_URL from .env: tests only touch a database chosen for them.
    """
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(
            POSTGRES_IMAGE, username="agent", password="agent", dbname="agent", driver=None
        )
        container.start()
    except Exception as exc:
        pytest.skip(f"no TEST_DATABASE_URL and no Docker for a throwaway Postgres ({type(exc).__name__})")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def sample_remote(tmp_path: Path) -> str:
    """A bare Git repo seeded with evals/sample_repo, usable as a clone/push remote."""
    src = tmp_path / "src"
    shutil.copytree(SAMPLE_REPO, src, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(remote))
    return remote.as_uri()
