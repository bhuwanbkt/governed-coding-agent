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
SupervisorMode = Literal["deterministic", "llm"]


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
    github_token: SecretStr | None = None  # fine-grained PAT: fallback when no GitHub App is set
    # GitHub App (phase 4): per-job, one-repository, one-hour tokens. Both or neither.
    github_app_id: str | None = None
    github_app_private_key: SecretStr | None = None  # PEM; "\n" escapes are accepted
    workspaces_dir: Path = Path("workspaces")
    allowed_test_commands: str = "pytest,python -m pytest"
    agent_max_fix_attempts: int = 2  # retries after the first attempt fails its tests
    agent_max_llm_turns: int = 25  # per attempt; each turn may contain several tool calls
    llm_min_interval_seconds: float = 1.0  # pacing between LLM calls (free tiers rate-limit)
    keep_workspaces: bool = False  # keep job folders after a job for debugging
    agent_max_llm_calls: int = 40  # whole job, all agents; hitting it hands the job to a human
    agent_max_review_rounds: int = 1  # extra coder rounds the reviewer agent may request
    agent_max_replans: int = 1  # times the planner may be asked to re-plan after failing tests
    # deterministic: graph code routes between agents (default, production)
    # llm: a supervisor LLM proposes the next step; a policy table and hard rules validate it
    supervisor_mode: SupervisorMode = "deterministic"

    # --- retrieval (LlamaIndex) ----------------------------------------------------------
    retrieval_embeddings: bool = False  # hybrid BM25 + embeddings; off = BM25 only (less memory)
    embedding_model: str = "BAAI/bge-small-en-v1.5"  # runs locally via fastembed, no API key
    embedding_cache_dir: Path | None = None  # where the embedding model is downloaded/cached
    retrieval_top_k: int = 6

    # --- governance (phase 4) -----------------------------------------------------------
    policy_file: Path | None = None  # default: the packaged governance/policies/default.yaml
    job_timeout_seconds: int = 1200  # coding run wall-clock limit; then handed to a human
    max_cost_per_job_usd: float = 0.50  # list-price LLM cost per job
    daily_budget_usd: float = 5.00  # all jobs per UTC day; new jobs wait when reached
    llm_price_input_per_mtok: float | None = None  # override the built-in price table
    llm_price_output_per_mtok: float | None = None
    # "bob,carol": only these users may approve. Empty = anyone except the requester.
    approvers: str = ""
    approval_ttl_hours: float = 24.0  # unanswered approvals expire after this

    # --- web UI + OIDC login (phase 5) --------------------------------------------------
    # Any OpenID Connect provider (Zitadel, Okta, Entra ID, Keycloak). Unset = UI login disabled.
    oidc_issuer: str | None = None  # e.g. https://my-instance.zitadel.cloud
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_project_id: str | None = None  # Zitadel: adds the project audience scope + roles claim
    oidc_roles_claim: str | None = None  # other providers: claim holding roles, e.g. "roles"
    session_secret: SecretStr | None = None  # signs the session cookie; required in production
    session_max_age_hours: float = 8.0
    git_author_name: str = "governed-coding-agent"
    git_author_email: str = "agent@users.noreply.github.com"

    # --- API & governance ------------------------------------------------------------
    # "alice:<key>,bob:<key>": each key maps to a user, so approvals have a real identity.
    api_keys: SecretStr | None = None
    # Comma-separated URL prefixes the agent may work on, e.g. "https://github.com/bhuwanbkt/".
    allowed_repo_prefixes: str = ""

    # --- worker ----------------------------------------------------------------------
    run_worker_in_api: bool = False  # free tier: one service runs both API and worker
    worker_poll_seconds: float = 3.0
    worker_heartbeat_seconds: float = 15.0
    worker_stale_after_seconds: int = 300
    worker_max_claims: int = 3  # a job interrupted this many times is marked failed

    @field_validator(
        "database_url",
        "groq_api_key",
        "github_token",
        "api_keys",
        "github_app_id",
        "github_app_private_key",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_project_id",
        "oidc_roles_claim",
        "session_secret",
        mode="before",
    )
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
        if self.app_env == "production":
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required when APP_ENV=production")
            if not self.repo_prefixes():
                raise ValueError("ALLOWED_REPO_PREFIXES is required when APP_ENV=production")
        self.api_users()  # fail fast on a malformed API_KEYS value
        if (self.github_app_id is None) != (self.github_app_private_key is None):
            raise ValueError("set both GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY, or neither")
        oidc = (self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret)
        if any(v is not None for v in oidc) and not all(v is not None for v in oidc):
            raise ValueError("set OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET together, or none")
        if self.oidc_enabled and self.app_env == "production" and self.session_secret is None:
            raise ValueError("SESSION_SECRET is required for the web UI in production")
        if self.session_secret is not None and len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        if self.max_cost_per_job_usd <= 0 or self.daily_budget_usd <= 0 or self.job_timeout_seconds <= 0:
            raise ValueError("budgets and the job timeout must be positive")
        return self

    def app_private_key(self) -> str | None:
        """The App's PEM key, with literal "\\n" sequences (common in env vars) turned into newlines."""
        if self.github_app_private_key is None:
            return None
        return self.github_app_private_key.get_secret_value().replace("\\n", "\n")

    @property
    def oidc_enabled(self) -> bool:
        return self.oidc_issuer is not None and self.oidc_client_id is not None

    def approver_set(self) -> frozenset[str]:
        return frozenset(a.strip() for a in self.approvers.split(",") if a.strip())

    def llm_prices(self) -> tuple[float, float]:
        from coding_agent.governance.pricing import price_for

        return price_for(self.llm_model, (self.llm_price_input_per_mtok, self.llm_price_output_per_mtok))

    def api_users(self) -> dict[str, str]:
        """Map API key -> user name, parsed from API_KEYS ("user:key,user:key")."""
        if self.api_keys is None:
            return {}
        users: dict[str, str] = {}
        for entry in self.api_keys.get_secret_value().split(","):
            if not entry.strip():
                continue
            user, sep, key = entry.strip().partition(":")
            if not sep or not user or len(key) < 16:
                raise ValueError("API_KEYS entries must look like user:key with keys of 16+ characters")
            users[key] = user
        return users

    def repo_prefixes(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.allowed_repo_prefixes.split(",") if p.strip())

    def repo_allowed(self, repo_url: str) -> bool:
        prefixes = self.repo_prefixes()
        return not prefixes or repo_url.startswith(prefixes)


@lru_cache
def get_settings() -> Settings:
    return Settings()
