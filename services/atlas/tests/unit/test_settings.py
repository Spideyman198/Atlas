"""Tests for configuration loading.

Configuration is fail-fast by design; these tests pin that behaviour so a later
refactor cannot quietly turn a boot-time error into a runtime one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.config.settings import DatabaseSettings, Settings, get_settings

pytestmark = pytest.mark.unit

_URL = "postgresql://atlas:secret@localhost:5432/atlas"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Keep the cached singleton from leaking configuration between tests."""
    get_settings.cache_clear()


def test_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE__URL", _URL)
    monkeypatch.setenv("ATLAS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ATLAS_ENV", "production")

    settings = get_settings()

    assert settings.database.url == _URL
    assert settings.log_level == "DEBUG"
    assert settings.env == "production"
    assert settings.service_name == "atlas-api"
    assert settings.is_production


def test_nested_database_settings_have_usable_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_DATABASE__URL", _URL)

    database = get_settings().database

    # Zero minimum lets the process start before PostgreSQL is reachable.
    assert database.pool_min_size == 0
    assert database.pool_max_size >= 1
    assert database.connect_timeout_seconds > 0


def test_nested_settings_are_overridable_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_DATABASE__URL", _URL)
    monkeypatch.setenv("ATLAS_DATABASE__POOL_MAX_SIZE", "32")

    assert get_settings().database.pool_max_size == 32


def test_settings_are_cached_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE__URL", _URL)

    assert get_settings() is get_settings()


def test_settings_are_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE__URL", _URL)
    settings = get_settings()

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_an_invalid_log_level_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        Settings(database=DatabaseSettings(url=_URL), log_level="CHATTY")  # type: ignore[arg-type]


def test_a_non_positive_pool_size_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(url=_URL, pool_max_size=0)
