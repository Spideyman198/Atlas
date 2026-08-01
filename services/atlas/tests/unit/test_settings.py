"""Tests for configuration loading.

Configuration is fail-fast by design: these tests pin that behaviour so a future
refactor cannot quietly turn a boot-time error into a runtime one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.config.settings import Settings, get_settings

pytestmark = pytest.mark.unit

_VALID_DATABASE_URL = "postgresql://atlas:secret@localhost:5432/atlas"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Keep the cached singleton from leaking configuration between tests."""
    get_settings.cache_clear()


def test_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE_URL", _VALID_DATABASE_URL)
    monkeypatch.setenv("ATLAS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ATLAS_ENV", "production")

    settings = get_settings()

    assert settings.database_url == _VALID_DATABASE_URL
    assert settings.log_level == "DEBUG"
    assert settings.env == "production"
    assert settings.service_name == "atlas-api"


def test_settings_are_cached_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE_URL", _VALID_DATABASE_URL)

    assert get_settings() is get_settings()


def test_settings_are_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE_URL", _VALID_DATABASE_URL)
    settings = get_settings()

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_an_invalid_log_level_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=_VALID_DATABASE_URL, log_level="CHATTY")  # type: ignore[arg-type]
