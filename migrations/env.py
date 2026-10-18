"""Alembic environment: reads DATABASE_URL from app settings.

For Neon, run migrations with the *direct* (non-pooler) connection string; DDL and
advisory locks are not reliable through PgBouncer transaction pooling.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from coding_agent.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # migrations are hand-written SQL; no ORM autogenerate


def _url() -> str:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL must be set to run migrations")
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, connect_args={"prepare_threshold": None})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
