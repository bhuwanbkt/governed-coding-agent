"""Application settings, loaded from environment variables (12-factor).

All configuration comes from env vars (or a local .env file). Secrets are typed as
SecretStr so they never show up in logs or reprs by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "test", "production"]


def normalize_database_url(url: str) -> str:
    """Neon/Heroku style URLs use postgres:// or postgresql://; SQLAlchemy needs the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnv = "local"
    app_version: str = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr | None = None
    db_connect_timeout_seconds: int = 10  # Neon may need a moment to wake from scale-to-zero

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_url(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return normalize_database_url(value) if value else None
        return value

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.app_env == "production" and self.database_url is None:
            raise ValueError("DATABASE_URL is required when APP_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
