from __future__ import annotations

import pytest
from pydantic import ValidationError

from coding_agent.config import Settings, normalize_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ],
)
def test_database_url_is_normalized_to_psycopg(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_empty_database_url_means_not_configured() -> None:
    settings = Settings(_env_file=None, database_url="  ")  # type: ignore[call-arg]
    assert settings.database_url is None


def test_database_url_is_secret_in_repr() -> None:
    settings = Settings(_env_file=None, database_url="postgresql://u:hunter2@host/db")  # type: ignore[call-arg]
    assert "hunter2" not in repr(settings)


def test_production_requires_database_url() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        Settings(_env_file=None, app_env="production", database_url=None)  # type: ignore[call-arg]
