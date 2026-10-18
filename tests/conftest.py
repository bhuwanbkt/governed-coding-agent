from __future__ import annotations

from collections.abc import Iterator

import pytest

from coding_agent.config import get_settings
from coding_agent.db.session import get_engine


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
