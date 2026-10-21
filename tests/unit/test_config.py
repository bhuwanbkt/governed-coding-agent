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


OIDC = {"oidc_issuer": "https://idp.example.test", "oidc_client_id": "c1", "oidc_client_secret": "x" * 32}
PROD = {
    "app_env": "production",
    "database_url": "postgresql://u:p@h/db",
    "allowed_repo_prefixes": "https://x/",
}


def test_oidc_settings_must_be_set_together() -> None:
    with pytest.raises(ValidationError, match="together"):
        Settings(_env_file=None, oidc_issuer="https://idp.example.test")  # type: ignore[call-arg]
    assert Settings(_env_file=None, **OIDC).oidc_enabled  # type: ignore[call-arg]
    assert not Settings(_env_file=None, oidc_issuer=" ").oidc_enabled  # type: ignore[call-arg]


def test_production_ui_needs_a_strong_session_secret() -> None:
    with pytest.raises(ValidationError, match="SESSION_SECRET is required"):
        Settings(_env_file=None, **PROD, **OIDC)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(_env_file=None, **PROD, **OIDC, session_secret="short")  # type: ignore[call-arg]
    settings = Settings(_env_file=None, **PROD, **OIDC, session_secret="s" * 48)  # type: ignore[call-arg]
    assert "s" * 48 not in repr(settings)
    assert "x" * 32 not in repr(settings)
