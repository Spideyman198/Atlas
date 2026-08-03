"""What the model is told when a tool call goes wrong.

The distinction under test: a bad *argument* comes back as a result the model
can read and retry from, while a bad *Odoo* comes back as an exception. Getting
this backwards produces either an assistant that gives up on a typo, or one that
apologises for a network outage as though it had asked for the wrong field.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest

from atlas.application.tools import MAX_RESULT_CHARS, ToolBox
from atlas.domain.authorization import UserContext
from atlas.domain.chat import ToolCall
from atlas.domain.errors import AuthorizationError, DependencyUnavailableError, ValidationError
from atlas.infrastructure.odoo.fakes import FakeOdooGateway

CONTEXT = UserContext(token="alice-token", trace_id="trace-1")


def gateway(**tools: Any) -> FakeOdooGateway:
    return FakeOdooGateway(readable={"alice-token": {}}, tools=tools)


def call(name: str = "find_records", **arguments: Any) -> ToolCall:
    return ToolCall(id="call-1", name=name, arguments=arguments)


class TestCatalog:
    async def test_it_offers_what_odoo_offers(self) -> None:
        box = ToolBox(gateway(find_records=lambda _: {}, aggregate=lambda _: {}))

        names = [tool.name for tool in await box.catalog(CONTEXT)]

        assert names == ["aggregate", "find_records"]

    async def test_an_unreachable_odoo_yields_no_tools_rather_than_an_error(self) -> None:
        """An answer from retrieved documents beats no answer at all."""
        box = ToolBox(FakeOdooGateway(readable={"alice-token": {}}, unavailable=True))

        assert await box.catalog(CONTEXT) == []

    async def test_a_refused_context_still_raises(self) -> None:
        """Nobody should be served under a token Odoo would not accept."""
        box = ToolBox(FakeOdooGateway(readable={"someone-else": {}}))

        with pytest.raises(AuthorizationError):
            await box.catalog(CONTEXT)


class TestExecute:
    async def test_a_result_comes_back_as_json(self) -> None:
        box = ToolBox(gateway(find_records=lambda _: {"rows": [{"id": 4}], "matched": 1}))

        result = await box.execute(CONTEXT, call())

        assert not result.is_error
        assert json.loads(result.content) == {"rows": [{"id": 4}], "matched": 1}
        assert result.call_id == "call-1"

    async def test_the_call_id_survives(self) -> None:
        """A tool result the provider cannot match to its call is rejected."""
        box = ToolBox(gateway(find_records=lambda _: {}))

        result = await box.execute(
            CONTEXT, ToolCall(id="abc123", name="find_records", arguments={})
        )

        assert result.call_id == "abc123"

    async def test_a_bad_argument_comes_back_as_a_readable_result(self) -> None:
        def refuse(_arguments: Any) -> dict[str, Any]:
            message = "'colour' is not filterable on res.partner. Allowed: city, name"
            raise ValidationError(message)

        box = ToolBox(gateway(find_records=refuse))

        result = await box.execute(CONTEXT, call(field="colour"))

        assert result.is_error
        # Verbatim, because the words are what the model corrects from.
        assert "'colour' is not filterable" in result.content
        assert "Allowed: city, name" in result.content

    async def test_an_unknown_tool_is_a_result_too(self) -> None:
        """The model invented the name; it can pick a real one on the retry."""
        box = ToolBox(gateway(find_records=lambda _: {}))

        result = await box.execute(CONTEXT, call(name="drop_tables"))

        assert result.is_error

    async def test_an_unreachable_odoo_raises(self) -> None:
        """Not something the model can fix by choosing different arguments."""
        box = ToolBox(FakeOdooGateway(readable={"alice-token": {}}, unavailable=True))

        with pytest.raises(DependencyUnavailableError):
            await box.execute(CONTEXT, call())

    async def test_a_refused_context_raises(self) -> None:
        box = ToolBox(FakeOdooGateway(readable={"someone-else": {}}))

        with pytest.raises(AuthorizationError):
            await box.execute(CONTEXT, call())


class TestRendering:
    async def test_a_large_result_is_truncated(self) -> None:
        rows = [{"id": index, "name": "x" * 200} for index in range(500)]
        box = ToolBox(gateway(find_records=lambda _: {"rows": rows}))

        result = await box.execute(CONTEXT, call())

        assert len(result.content) < len(json.dumps({"rows": rows}))

    async def test_truncation_is_announced(self) -> None:
        """Truncation has to be visible.

        A silently cut list reads as the complete answer, and gets presented as
        one.
        """
        rows = [{"id": index, "name": "x" * 200} for index in range(500)]
        box = ToolBox(gateway(find_records=lambda _: {"rows": rows}))

        result = await box.execute(CONTEXT, call())

        assert "truncated" in result.content
        assert str(MAX_RESULT_CHARS) in result.content

    async def test_a_result_that_fits_is_left_alone(self) -> None:
        box = ToolBox(gateway(find_records=lambda _: {"rows": [{"id": 1}]}))

        result = await box.execute(CONTEXT, call())

        assert "truncated" not in result.content

    async def test_values_json_cannot_hold_are_rendered_rather_than_raising(self) -> None:
        """Dates arrive from the ORM as objects.

        Failing here would turn a working query into an error the model cannot
        do anything about.
        """
        box = ToolBox(gateway(find_records=lambda _: {"date": datetime.date(2026, 8, 1)}))

        result = await box.execute(CONTEXT, call())

        assert "2026-08-01" in result.content
