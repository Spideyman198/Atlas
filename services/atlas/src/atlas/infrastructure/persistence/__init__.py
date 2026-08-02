"""Persistence adapters for the Atlas database.

All SQL lives in this package. Table and column names appear here and in the
Alembic migrations and nowhere else, which is the containment that makes
hand-written SQL maintainable (ADR-0008).
"""

from atlas.infrastructure.persistence.pgvector_store import PgVectorStore, register_vector

__all__ = ["PgVectorStore", "register_vector"]
