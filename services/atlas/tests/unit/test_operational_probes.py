"""Tests for the liveness and readiness probes.

These run with no database, no network and no configuration file — which is the
point. If liveness needed any of those, it would not be a liveness probe.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas import __version__
from atlas.interfaces.http.app import _describe_pgvector, _parse_version, create_app

pytestmark = pytest.mark.unit


def test_healthz_answers_without_running_the_lifespan() -> None:
    """Liveness must not depend on the container or on settings.

    Constructing ``TestClient`` without a ``with`` block skips the lifespan, so no
    container exists and ``get_settings()`` is never called. This is the closest
    analogue to a process whose database is unreachable at boot.
    """
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "atlas-api",
        "version": __version__,
    }


def test_readyz_reports_not_ready_when_the_container_is_absent() -> None:
    client = TestClient(create_app())

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert "not initialised" in body["checks"]["database"]


def test_openapi_schema_is_generated() -> None:
    client = TestClient(create_app())

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/healthz", "/readyz"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.8.0", (0, 8, 0)),
        ("0.8", (0, 8)),
        ("1.10.2", (1, 10, 2)),
        # PostgreSQL extension versions are free-form strings, not semver.
        ("0.8.0-beta1", (0, 8, 0)),
        ("", (0,)),
    ],
)
def test_parse_version_handles_free_form_extension_versions(
    raw: str, expected: tuple[int, ...]
) -> None:
    assert _parse_version(raw) == expected


@pytest.mark.parametrize(
    ("installed", "expected_prefix"),
    [
        (None, "missing"),
        ("0.7.4", "outdated"),
        ("0.8.0", "ok"),
        ("0.8.6", "ok"),
        ("1.0.0", "ok"),
    ],
)
def test_describe_pgvector_enforces_the_minimum_required_version(
    installed: str | None, expected_prefix: str
) -> None:
    """ADR-0004 requires pgvector >= 0.8 for `hnsw.iterative_scan`."""
    assert _describe_pgvector(installed).startswith(expected_prefix)
