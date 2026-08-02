"""Tests for trace-id propagation through the HTTP layer."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from atlas.config.logging import TRACE_ID_HEADER, get_trace_id
from atlas.domain.errors import AuthorizationError
from atlas.interfaces.http.errors import register_exception_handlers
from atlas.interfaces.http.middleware import TraceIdMiddleware

pytestmark = pytest.mark.unit

_LONG = "x" * 200


def _app() -> FastAPI:
    router = APIRouter()

    @router.get("/echo")
    async def echo() -> dict[str, str | None]:
        return {"trace_id": get_trace_id()}

    @router.get("/denied")
    async def denied() -> None:
        raise AuthorizationError("not your record", context={"res_model": "sale.order"})

    @router.get("/broken")
    async def broken() -> None:
        raise RuntimeError("internal detail that must not reach the client")

    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.include_router(router)
    return app


def test_a_trace_id_is_minted_and_echoed_when_the_caller_sends_none() -> None:
    client = TestClient(_app())

    response = client.get("/echo")

    assert response.status_code == 200
    minted = response.json()["trace_id"]
    assert minted
    assert response.headers[TRACE_ID_HEADER] == minted


def test_an_inbound_trace_id_is_adopted_so_one_id_spans_both_services() -> None:
    client = TestClient(_app())

    response = client.get("/echo", headers={TRACE_ID_HEADER: "from-odoo"})

    assert response.json()["trace_id"] == "from-odoo"
    assert response.headers[TRACE_ID_HEADER] == "from-odoo"


@pytest.mark.parametrize("supplied", ["", "   ", _LONG, "bad\x00value"])
def test_an_unusable_inbound_trace_id_is_replaced(supplied: str) -> None:
    """The value is echoed into headers and logs, so it cannot be trusted as-is."""
    client = TestClient(_app())

    response = client.get("/echo", headers={TRACE_ID_HEADER: supplied})

    assert response.json()["trace_id"] not in {supplied, "", None}


def test_trace_id_does_not_leak_between_requests() -> None:
    client = TestClient(_app())

    first = client.get("/echo", headers={TRACE_ID_HEADER: "first"}).json()["trace_id"]
    second = client.get("/echo").json()["trace_id"]

    assert first == "first"
    assert second != "first"


def test_a_domain_error_becomes_a_problem_document_carrying_the_trace_id() -> None:
    client = TestClient(_app())

    response = client.get("/denied", headers={TRACE_ID_HEADER: "trace-42"})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "authorization_error"
    assert body["status"] == 403
    assert body["detail"] == "not your record"
    assert body["trace_id"] == "trace-42"


def test_an_unexpected_error_does_not_leak_its_message() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)

    response = client.get("/broken", headers={TRACE_ID_HEADER: "trace-99"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["trace_id"] == "trace-99"
    assert "internal detail" not in response.text
