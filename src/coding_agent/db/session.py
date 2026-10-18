"""SQLAlchemy engine configured for Neon.

Notes for Neon:
- Use the *pooled* connection string (host contains `-pooler`) for the app.
- Neon's pooler is PgBouncer in transaction mode, which does not support server-side
  prepared statements, so psycopg's automatic prepare is disabled (`prepare_threshold=None`).
- `pool_pre_ping` + `pool_recycle` handle connections dropped while Neon scales to zero.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from coding_agent.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    if settings.database_url is None:
        raise DatabaseNotConfiguredError("DATABASE_URL is not set")
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "prepare_threshold": None,
            "connect_timeout": settings.db_connect_timeout_seconds,
            "application_name": "governed-coding-agent",
        },
    )


def ping() -> None:
    """Raise if the database is unreachable."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
