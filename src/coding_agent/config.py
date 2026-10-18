"""Application settings, loaded from environment variables (12-factor).

All configuration comes from env vars (or a local .env file). Secrets are typed as
SecretStr so they never show up in logs or reprs by accident.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "test", "production"]
LLMProvider = Literal["groq", "fake"]


def normalize_database_url(url: str) -> str:
    """Neon/Heroku style URLs use postgres:// or postgresql://; SQLAlchemy needs the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service -------------------------------------------------------------------
    app_env: AppEnv = "local"
    app_version: str = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr | None = None
    db_connect_timeout_seconds: int = 10  # Neon may need a moment to wake from scale-to-zero

    # --- LLM -------------------------------------------------------------------------
    llm_provider: LLMProvider = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 90
    groq_api_key: SecretStr | None = None

    # --- agent -----------------------------------------------------------------------
    github_token: SecretStr | None = None  # fine-grained PAT for phase 1; GitHub App from phase 4
    workspaces_dir: Path = Path("workspaces")
    allowed_test_commands: str = "pytest,python -m pytest"
    agent_max_fix_attempts: int = 2  # retries after the first attempt fails its tests
    agent_max_llm_turns: int = 25  # per attempt; each turn may contain several tool calls
    git_author_name: str = "governed-coding-agent"
    git_author_email: str = "agent@users.noreply.github.com"

    @field_validator("database_url", "groq_api_key", "github_token", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        return SecretStr(normalize_database_url(value.get_secret_value()))

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.app_env == "production" and self.database_url is None:
            raise ValueError("DATABASE_URL is required when APP_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
