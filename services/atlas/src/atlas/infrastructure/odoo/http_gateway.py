"""The HTTP adapter for the Odoo gateway.

Talks to the controllers in ``addons/odoo_atlas/controllers/atlas_api.py``. The
payload shapes are documented once, in ``docs/api.md``; this module is the
client half of that contract.

Every failure mode here funnels into an exception. There is no code path that
returns a partial or empty result because Odoo was unhappy: the caller must not
be able to confuse "Odoo said no" with "Odoo did not answer", because only one
of those is safe to treat as a denial.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from types import TracebackType
from typing import Any, Final, Self

import httpx

from atlas.domain.authorization import UserContext
from atlas.domain.chat import ToolDefinition
from atlas.domain.errors import (
    AuthorizationError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)

STATUS_PATH: Final = "/atlas/api/status"
AUTHORIZE_PATH: Final = "/atlas/api/authorize"
RECORDS_PATH: Final = "/atlas/api/records"
TOOL_PATH: Final = "/atlas/api/tool/execute"
CATALOG_PATH: Final = "/atlas/api/tool/catalog"

#: Odoo resolves a session-less request's database from this header. Without it
#: a server hosting more than one database cannot route the call at all.
DATABASE_HEADER: Final = "X-Odoo-Database"


class OdooHttpGateway:
    """Reaches Odoo's Atlas endpoints over HTTP.

    Args:
        base_url: Odoo's origin.
        database: The database to address.
        service_token: The shared secret proving this is the engine.
        timeout_seconds: Hard ceiling on one call.
        max_ids_per_call: Refuse an over-large batch here rather than have Odoo
            refuse it after a round-trip.
        transport: Swapped in tests. The client is still built here, so the
            headers a test sees are the ones production sends rather than ones
            the test set up for itself.
    """

    def __init__(  # noqa: PLR0913 - keyword-only client configuration, not a parameter list
        self,
        *,
        base_url: str,
        database: str,
        service_token: str,
        timeout_seconds: float = 10.0,
        max_ids_per_call: int = 500,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._database = database
        self._max_ids_per_call = max_ids_per_call
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {service_token}",
                DATABASE_HEADER: database,
            },
        )

    async def authorize(
        self,
        context: UserContext,
        records: Mapping[str, Sequence[int]],
    ) -> dict[str, frozenset[int]]:
        requested = {model: list(ids) for model, ids in records.items() if ids}
        if not requested:
            return {}

        oversized = [model for model, ids in requested.items() if len(ids) > self._max_ids_per_call]
        if oversized:
            message = f"too many ids for {', '.join(sorted(oversized))}"
            raise ValidationError(message, context={"max_ids_per_call": self._max_ids_per_call})

        payload = self._body(context, records=requested)
        body = await self._post(AUTHORIZE_PATH, payload)
        granted = body.get("granted")
        if not isinstance(granted, dict):
            message = "Odoo returned no 'granted' mapping"
            raise DependencyUnavailableError(message)

        # Every model asked about gets an entry, so a caller can distinguish a
        # model that granted nothing from one Odoo silently dropped.
        return {model: frozenset(_int_ids(granted.get(model, []))) for model in requested}

    async def read_records(
        self,
        context: UserContext,
        model: str,
        ids: Sequence[int],
        fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        payload = self._body(context, model=model, ids=list(ids), fields=list(fields))
        body = await self._post(RECORDS_PATH, payload)
        rows = body.get("records")
        if not isinstance(rows, list):
            message = "Odoo returned no 'records' list"
            raise DependencyUnavailableError(message)
        return [row for row in rows if isinstance(row, dict)]

    async def tool_catalog(self, context: UserContext) -> list[ToolDefinition]:
        body = await self._post(CATALOG_PATH, self._body(context))
        entries = body.get("tools")
        if not isinstance(entries, list):
            message = "Odoo returned no 'tools' list"
            raise DependencyUnavailableError(message)
        return [
            ToolDefinition(
                name=str(entry["name"]),
                description=str(entry.get("description") or ""),
                parameters=dict(entry.get("parameters") or {}),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("name")
        ]

    async def execute_tool(
        self,
        context: UserContext,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = self._body(context, tool=tool, arguments=dict(arguments))
        body = await self._post(TOOL_PATH, payload)
        result = body.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def status(self) -> dict[str, Any]:
        """Confirm Odoo is reachable, has the addon, and accepts our token.

        Used by the readiness probe. It acts for nobody, so it needs no context
        token — which is the only reason the engine can run it at all.

        Raises:
            AuthorizationError: The service token was refused.
            NotFoundError: Odoo answered, but the addon is not installed.
            DependencyUnavailableError: Odoo could not be reached.
        """
        return await self._post(STATUS_PATH, {})

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _body(self, context: UserContext, **payload: Any) -> dict[str, Any]:
        return {"context_token": context.token, "trace_id": context.trace_id, **payload}

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST to Odoo and translate everything that can go wrong."""
        try:
            response = await self._client.post(path, json=dict(payload))
        except httpx.TimeoutException as exc:
            message = f"Odoo did not answer {path} in time"
            raise DependencyUnavailableError(message, context={"path": path}) from exc
        except httpx.HTTPError as exc:
            message = f"Odoo is unreachable at {path}"
            raise DependencyUnavailableError(
                message, context={"path": path, "error": type(exc).__name__}
            ) from exc

        self._raise_for_status(path, response)

        try:
            body = response.json()
        except ValueError as exc:
            message = f"Odoo answered {path} with a body that is not JSON"
            raise DependencyUnavailableError(message, context={"path": path}) from exc

        if not isinstance(body, dict):
            message = f"Odoo answered {path} with {type(body).__name__}, expected an object"
            raise DependencyUnavailableError(message, context={"path": path})
        return body

    def _raise_for_status(self, path: str, response: httpx.Response) -> None:
        """Map an HTTP status onto the error taxonomy.

        401 and 403 become :class:`AuthorizationError` rather than a transport
        failure: Odoo understood the request and declined it, and a caller that
        retried would only be refused again.
        """
        status = response.status_code
        if status < HTTPStatus.BAD_REQUEST:
            return

        detail = {"path": path, "status": status}
        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            logger.warning("odoo refused the engine's credentials", extra=detail)
            message = "Odoo refused the request's credentials"
            raise AuthorizationError(message, context=detail)
        if status == HTTPStatus.NOT_FOUND:
            message = f"Odoo has nothing at {path}"
            raise NotFoundError(message, context=detail)
        if status < HTTPStatus.INTERNAL_SERVER_ERROR:
            # Odoo's own words, when it gave any. A tool call rejected for a bad
            # argument is corrected by the model far more reliably when it is
            # told which argument and what was allowed instead.
            reason = _reason(response)
            message = f"Odoo rejected the request to {path}" + (f": {reason}" if reason else "")
            raise ValidationError(message, context=detail)

        message = f"Odoo failed on {path} with {status}"
        raise DependencyUnavailableError(message, context=detail)


def _reason(response: httpx.Response) -> str:
    """Pull Odoo's explanation out of an error body, if it left one."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("message", "detail", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _int_ids(values: Any) -> list[int]:
    """Keep the integers out of whatever Odoo sent, ignoring anything else."""
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
