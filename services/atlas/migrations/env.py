"""Alembic environment.

Reads the database URL from Atlas settings rather than ``alembic.ini``, so there
is one source of truth for where the Atlas database lives and no credentials in a
committed file.

Offline mode is supported so ``alembic upgrade --sql`` can emit a script for a
DBA to review before it touches a production database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from atlas.config.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No autogenerate: migrations are hand-written (ADR-0008), so there is no model
# metadata to diff against.
target_metadata = None


def _database_url() -> str:
    """Prefer an explicit ``-x url``, then settings.

    The URL is rewritten to the ``postgresql+psycopg`` dialect. Settings hold a
    plain libpq URL because that is what the runtime pool consumes; SQLAlchemy
    would otherwise default to the psycopg2 driver, which is not installed.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    url = str(override) if override else get_settings().database.url
    return _as_psycopg3(url)


def _as_psycopg3(url: str) -> str:
    """Point a libpq URL at the psycopg 3 dialect, leaving explicit ones alone."""
    if "+" in url.split("://", 1)[0]:
        return url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_migrations_offline() -> None:
    """Emit SQL without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
