from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from coding_agent.config import get_settings
from coding_agent.db.session import get_engine

SAMPLE_REPO = Path(__file__).resolve().parents[1] / "evals" / "sample_repo"


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
