"""The contract every OdooGateway adapter must satisfy.

Two implementations: the HTTP adapter that talks to the addon, and the in-memory
fake the retrieval milestones develop against. They have to agree, because a
fake that is more permissive than the real thing turns every test written
against it into a false negative for the one property this project has.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from atlas.domain.authorization import UserContext
from atlas.domain.errors import AuthorizationError, DependencyUnavailableError, NotFoundError
from atlas.domain.ports.odoo_gateway import OdooGateway
from atlas.infrastructure.odoo.fakes import FakeOdooGateway
from atlas.infrastructure.odoo.http_gateway import OdooHttpGateway

pytestmark = pytest.mark.contract

ALICE = UserContext(token="alice-token", trace_id="trace-1")
STRANGER = UserContext(token="a-token-odoo-never-minted")

#: What Alice may read. Both implementations are set up from this one fixture,
#: so neither can quietly disagree about the scenario.
READABLE: dict[str, dict[str, list[int]]] = {
    ALICE.token: {"sale.order": [1, 2], "res.partner": [7]},
}
ROWS: dict[str, dict[int, dict[str, Any]]] = {
    "sale.order": {
        1: {"name": "SO001", "amount_total": 100.0},
        2: {"name": "SO002", "amount_total": 200.0},
        3: {"name": "SO003", "amount_total": 300.0},
    },
}


def odoo_like_handler(request: httpx.Request) -> httpx.Response:
    """A stand-in for the addon's controllers, applying the same rules they do."""
    body = json.loads(request.content or b"{}")
    allowed = READABLE.get(body.get("context_token", ""))
    if allowed is None:
        return httpx.Response(403, json={"message": "invalid context"})

    path = request.url.path
    if path == "/atlas/api/authorize":
        granted = {
            model: sorted(set(ids) & set(allowed.get(model, [])))
            for model, ids in body["records"].items()
        }
        return httpx.Response(200, json={"granted": granted})

    if path == "/atlas/api/records":
        model = body["model"]
        wanted = body.get("fields") or ["display_name"]
        permitted = set(allowed.get(model, []))
        rows = [
            {"id": record_id, **{name: ROWS[model][record_id].get(name) for name in wanted}}
            for record_id in body["ids"]
            if record_id in permitted and record_id in ROWS.get(model, {})
        ]
        return httpx.Response(200, json={"records": rows})

    if path == "/atlas/api/tool/execute":
        if body["tool"] != "echo":
            return httpx.Response(404, json={"message": "unknown tool"})
        return httpx.Response(200, json={"result": dict(body.get("arguments") or {})})

    return httpx.Response(404, json={"message": "no such endpoint"})


class OdooGatewayContract:
    """Behaviour required of every gateway adapter."""

    @pytest.fixture
    def gateway(self) -> OdooGateway:
        raise NotImplementedError

    def test_it_satisfies_the_protocol(self, gateway: OdooGateway) -> None:
        assert isinstance(gateway, OdooGateway)

    async def test_it_grants_only_what_the_user_may_read(self, gateway: OdooGateway) -> None:
        granted = await gateway.authorize(ALICE, {"sale.order": [1, 2, 3]})

        assert granted["sale.order"] == frozenset({1, 2})

    async def test_it_answers_for_every_model_asked_about(self, gateway: OdooGateway) -> None:
        """Asked-and-refused must be distinguishable from never-asked."""
        granted = await gateway.authorize(ALICE, {"sale.order": [3], "res.partner": [7]})

        assert set(granted) == {"sale.order", "res.partner"}
        assert granted["sale.order"] == frozenset()

    async def test_it_grants_nothing_for_a_model_the_user_cannot_touch(
        self, gateway: OdooGateway
    ) -> None:
        granted = await gateway.authorize(ALICE, {"ir.config_parameter": [1, 2]})

        assert granted["ir.config_parameter"] == frozenset()

    async def test_an_empty_request_is_not_a_round_trip(self, gateway: OdooGateway) -> None:
        assert await gateway.authorize(ALICE, {}) == {}
        assert await gateway.authorize(ALICE, {"sale.order": []}) == {}

    async def test_an_unknown_context_is_refused_rather_than_emptied(
        self, gateway: OdooGateway
    ) -> None:
        """The difference between "denied" and "unrecognised" is load-bearing.

        An adapter that answered "nothing granted" for a token Odoo never minted
        would let a caller treat a broken context as a normal empty result.
        """
        with pytest.raises(AuthorizationError):
            await gateway.authorize(STRANGER, {"sale.order": [1]})

    async def test_it_reads_only_records_the_user_may_read(self, gateway: OdooGateway) -> None:
        rows = await gateway.read_records(ALICE, "sale.order", [1, 3], ["name"])

        assert [row["id"] for row in rows] == [1]
        assert rows[0]["name"] == "SO001"

    async def test_reading_nothing_is_allowed(self, gateway: OdooGateway) -> None:
        assert await gateway.read_records(ALICE, "sale.order", [], ["name"]) == []

    async def test_an_unknown_tool_is_reported_as_missing(self, gateway: OdooGateway) -> None:
        with pytest.raises(NotFoundError):
            await gateway.execute_tool(ALICE, "no_such_tool", {})

    async def test_a_known_tool_returns_its_result(self, gateway: OdooGateway) -> None:
        result = await gateway.execute_tool(ALICE, "echo", {"hello": "world"})

        assert result == {"hello": "world"}


class TestFakeOdooGateway(OdooGatewayContract):
    @pytest.fixture
    def gateway(self) -> OdooGateway:
        return FakeOdooGateway(
            readable=READABLE,
            records=ROWS,
            tools={"echo": dict},
        )


class TestOdooHttpGateway(OdooGatewayContract):
    @pytest.fixture
    async def gateway(self) -> Any:
        adapter = OdooHttpGateway(
            base_url="http://odoo:8069",
            database="odoo",
            service_token="service-token",
            transport=httpx.MockTransport(odoo_like_handler),
        )
        try:
            yield adapter
        finally:
            await adapter.aclose()


async def test_the_fake_and_the_adapter_agree_that_odoo_can_be_down() -> None:
    """Both must raise rather than return an empty grant when Odoo is unusable."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OdooHttpGateway(
        base_url="http://odoo:8069",
        database="odoo",
        service_token="service-token",
        transport=httpx.MockTransport(refuse),
    )
    fake = FakeOdooGateway(readable=READABLE, unavailable=True)

    try:
        for gateway in (adapter, fake):
            with pytest.raises(DependencyUnavailableError):
                await gateway.authorize(ALICE, {"sale.order": [1]})
    finally:
        await adapter.aclose()
