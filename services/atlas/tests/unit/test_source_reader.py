"""Tests for the ingestion reader.

Driven through ``httpx.MockTransport``, so the request the addon would actually
receive is the one being asserted on.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from atlas.domain.errors import DependencyUnavailableError, NotFoundError, ValidationError
from atlas.infrastructure.odoo.source_reader import DATABASE_HEADER, OdooHttpSourceReader

pytestmark = pytest.mark.unit

SERVICE_TOKEN = "service-token"


def build_reader(handler: Any) -> OdooHttpSourceReader:
    return OdooHttpSourceReader(
        base_url="http://odoo:8069",
        database="odoo",
        service_token=SERVICE_TOKEN,
        transport=httpx.MockTransport(handler),
    )


def responder(payload: dict[str, Any], status_code: int = 200) -> Any:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handle


async def test_a_read_names_the_model_and_the_fields_the_template_wants() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["database"] = request.headers.get(DATABASE_HEADER)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"records": [], "more": False})

    async with build_reader(handle) as reader:
        await reader.read_records("odoo.res.partner", limit=25)

    assert seen["path"] == "/atlas/api/ingest/records"
    assert seen["auth"] == f"Bearer {SERVICE_TOKEN}"
    assert seen["database"] == "odoo"
    assert seen["body"]["model"] == "res.partner"
    assert "display_name" in seen["body"]["fields"]
    assert "write_date" in seen["body"]["fields"]
    assert seen["body"]["limit"] == 25


async def test_an_order_asks_for_its_line_items() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"records": [], "more": False})

    async with build_reader(handle) as reader:
        await reader.read_records("odoo.sale.order")

    assert seen["children"]["field"] == "order_line"
    assert seen["children"]["model"] == "sale.order.line"


async def test_a_watermark_is_sent_in_the_format_odoo_accepts() -> None:
    """Odoo stores UTC and refuses a value that says so.

    An ISO-8601 string with an offset raises `expecting only datetimes with no
    timezone` inside the ORM, which surfaces as a 500 and a failed sync.
    """
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"records": [], "more": False})

    async with build_reader(handle) as reader:
        await reader.read_records("odoo.res.partner", since=datetime(2026, 8, 1, 12, 0, tzinfo=UTC))

    assert seen["since"] == "2026-08-01 12:00:00"


async def test_a_watermark_in_another_zone_is_converted_to_utc() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"records": [], "more": False})

    brussels = timezone(timedelta(hours=2))
    async with build_reader(handle) as reader:
        await reader.read_records(
            "odoo.res.partner", since=datetime(2026, 8, 1, 14, 0, tzinfo=brussels)
        )

    assert seen["since"] == "2026-08-01 12:00:00"


async def test_records_come_back_with_naive_timestamps_made_aware() -> None:
    """Odoo stores UTC and says nothing about it.

    Leaving the timestamps naive would make every watermark comparison depend on
    the worker's local timezone — a bug that surfaces once a year.
    """
    payload = {
        "records": [{"id": 4, "display_name": "Deco Addict", "write_date": "2026-08-01 12:00:00"}],
        "more": False,
    }

    async with build_reader(responder(payload)) as reader:
        batch = await reader.read_records("odoo.res.partner")

    assert len(batch.records) == 1
    written = batch.records[0].write_date
    assert written is not None
    assert written.tzinfo is not None
    assert written == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


async def test_a_company_is_read_out_of_odoos_relation_pair() -> None:
    payload = {"records": [{"id": 4, "company_id": [1, "My Company"]}], "more": False}

    async with build_reader(responder(payload)) as reader:
        batch = await reader.read_records("odoo.res.partner")

    assert batch.records[0].company_id == 1


async def test_a_row_without_an_id_is_skipped_rather_than_crashing() -> None:
    payload = {"records": [{"display_name": "no id here"}, {"id": 4}], "more": False}

    async with build_reader(responder(payload)) as reader:
        batch = await reader.read_records("odoo.res.partner")

    assert [record.res_id for record in batch.records] == [4]


async def test_more_is_carried_through_so_the_caller_pages() -> None:
    async with build_reader(responder({"records": [{"id": 1}], "more": True})) as reader:
        batch = await reader.read_records("odoo.res.partner")

    assert batch.more is True


async def test_a_missing_model_is_reported_as_not_found() -> None:
    """Not a retryable failure: the module simply is not installed."""
    async with build_reader(responder({}, 404)) as reader:
        with pytest.raises(NotFoundError):
            await reader.read_records("odoo.crm.lead")


async def test_an_unreachable_odoo_is_a_dependency_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with build_reader(handle) as reader:
        with pytest.raises(DependencyUnavailableError):
            await reader.read_records("odoo.res.partner")


async def test_a_body_without_records_is_a_dependency_failure() -> None:
    async with build_reader(responder({"unexpected": True})) as reader:
        with pytest.raises(DependencyUnavailableError):
            await reader.read_records("odoo.res.partner")


async def test_an_attachment_comes_back_decoded() -> None:
    encoded = base64.b64encode(b"Refunds within 30 days.").decode()

    async with build_reader(responder({"content": encoded})) as reader:
        content = await reader.read_binary("odoo.ir.attachment", 9)

    assert content == b"Refunds within 30 days."


async def test_an_absent_attachment_is_empty_bytes() -> None:
    async with build_reader(responder({"content": ""})) as reader:
        assert await reader.read_binary("odoo.ir.attachment", 9) == b""


async def test_an_undecodable_attachment_is_rejected() -> None:
    async with build_reader(responder({"content": "not base64 !!"})) as reader:
        with pytest.raises(ValidationError):
            await reader.read_binary("odoo.ir.attachment", 9)


async def test_available_sources_maps_models_back_to_source_keys() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"sources": {"res.partner": True, "crm.lead": False}})

    async with build_reader(handle) as reader:
        available = await reader.available_sources()

    # Odoo is told which models to check rather than asked what it has.
    assert "res.partner" in seen["models"]
    assert available["odoo.res.partner"] is True
    assert available["odoo.crm.lead"] is False
