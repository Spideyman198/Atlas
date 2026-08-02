"""Tests for structured logging and trace-id propagation."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import pytest

from atlas.config.logging import (
    JsonFormatter,
    bind_trace_id,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)

pytestmark = pytest.mark.unit


def _record(**kwargs: Any) -> logging.LogRecord:
    defaults: dict[str, Any] = {
        "name": "atlas.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


def _format(record: logging.LogRecord) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def test_a_record_renders_as_one_json_object() -> None:
    payload = _format(_record())

    assert payload["level"] == "INFO"
    assert payload["logger"] == "atlas.test"
    assert payload["message"] == "hello world"
    assert payload["timestamp"].endswith("+00:00")


def test_trace_id_is_attached_when_one_is_bound() -> None:
    token = bind_trace_id("abc123")
    try:
        assert _format(_record())["trace_id"] == "abc123"
    finally:
        reset_trace_id(token)


def test_no_trace_id_key_when_none_is_bound() -> None:
    assert "trace_id" not in _format(_record())


def test_extra_fields_are_merged_into_the_payload() -> None:
    record = _record()
    record.model = "sale.order"
    record.record_count = 12

    payload = _format(record)

    assert payload["model"] == "sale.order"
    assert payload["record_count"] == 12


def test_a_non_serialisable_extra_does_not_break_the_record() -> None:
    """Losing a log line to a serialisation error is worse than losing fidelity."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    record = _record()
    record.thing = Opaque()

    assert _format(record)["thing"] == "<opaque>"


def test_exception_info_is_rendered() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record(exc_info=sys.exc_info())

    assert "ValueError: boom" in _format(record)["exception"]


def test_reset_restores_the_previous_trace_id() -> None:
    outer = bind_trace_id("outer")
    try:
        inner = bind_trace_id("inner")
        assert get_trace_id() == "inner"
        reset_trace_id(inner)
        assert get_trace_id() == "outer"
    finally:
        reset_trace_id(outer)

    assert get_trace_id() is None


def test_trace_ids_are_unique() -> None:
    assert len({new_trace_id() for _ in range(100)}) == 100
