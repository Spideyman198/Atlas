"""Fixtures for tests that need a real PostgreSQL with pgvector.

Skipped unless ``ATLAS_TEST_DATABASE_URL`` is set, so the offline suite stays
runnable with no services. Locally that points at the compose PostgreSQL; in CI
it points at a service container.

The tests run against a **dedicated database**, created if absent and migrated
with Alembic, so a test run can never touch a development corpus. Tables are
truncated between tests rather than the database recreated — recreating it would
re-run the HNSW index build for every test.
"""

from __future__ import annotations

import os
from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg_pool import AsyncConnectionPool

from atlas.infrastructure.persistence import PgVectorStore, register_vector

_TEST_DATABASE = "atlas_integration_test"
_SERVICE_URL = os.environ.get("ATLAS_TEST_DATABASE_URL")

requires_database = pytest.mark.skipif(
    not _SERVICE_URL,
    reason="ATLAS_TEST_DATABASE_URL is not set",
)


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def _create_database(admin_url: str) -> None:
    """Create the test database if it does not exist.

    Autocommit because CREATE DATABASE cannot run inside a transaction block.
    """
    with psycopg.connect(admin_url, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_TEST_DATABASE,))
        if cursor.fetchone() is None:
            cursor.execute(f'CREATE DATABASE "{_TEST_DATABASE}"')


def _migrate(url: str) -> None:
    service_root = Path(__file__).resolve().parents[2]
    config = Config(str(service_root / "alembic.ini"))
    config.set_main_option("script_location", str(service_root / "migrations"))
    # `-x url=` is how env.py takes an override without reading settings, which
    # keeps the test database out of the process-wide configuration.
    config.cmd_opts = Namespace(x=[f"url={url}"])
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated test database, built once per session."""
    if not _SERVICE_URL:
        pytest.skip("ATLAS_TEST_DATABASE_URL is not set")

    _create_database(_SERVICE_URL)
    url = _with_database(_SERVICE_URL, _TEST_DATABASE)
    _migrate(url)
    return url


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    """A pool whose connections understand the `vector` type."""
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=4,
        open=False,
        configure=register_vector,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def store(pool: AsyncConnectionPool) -> PgVectorStore:
    """An empty store. Truncating cascades from documents to chunks."""
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("TRUNCATE documents, ingest_jobs, embedding_cache CASCADE")
    return PgVectorStore(pool)
