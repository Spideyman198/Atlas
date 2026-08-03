"""Reading Odoo records for indexing.

A separate door from the query-time gateway, on purpose. This one runs as a
dedicated integration user and sees everything worth indexing, which is broader
than any one person's view. That is exactly why the authorization step at query
time cannot be skipped: the index is deliberately wider than the answer
(ADR-0006).

Keeping them apart means neither can be mistaken for the other. This client
cannot authorize anything, and the gateway cannot read past a user's rights.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from types import TracebackType
from typing import Any, Final, Self

import httpx

from atlas.domain.errors import DependencyUnavailableError, NotFoundError, ValidationError
from atlas.domain.ingestion import RecordBatch, SourceRecord
from atlas.domain.sources import REGISTRY, template_for

logger = logging.getLogger(__name__)

RECORDS_PATH: Final = "/atlas/api/ingest/records"
BINARY_PATH: Final = "/atlas/api/ingest/binary"
SOURCES_PATH: Final = "/atlas/api/ingest/sources"

DATABASE_HEADER: Final = "X-Odoo-Database"


class OdooHttpSourceReader:
    """Reads source records from the addon's ingestion endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        database: str,
        service_token: str,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # A far longer timeout than the query-time gateway's. Ingestion reads
        # pages of a hundred orders with their lines; nobody is waiting on it,
        # and failing a page costs a whole retry.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {service_token}",
                DATABASE_HEADER: database,
            },
        )

    async def read_records(
        self,
        source_key: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        record_ids: Sequence[int] | None = None,
    ) -> RecordBatch:
        template = template_for(source_key)
        payload: dict[str, Any] = {
            "source_key": source_key,
            "model": template.res_model,
            "fields": list(template.fields),
            "domain": [list(clause) for clause in template.domain],
            "limit": limit,
            "offset": offset,
        }
        if since is not None:
            payload["since"] = _odoo_timestamp(since)
        if record_ids is not None:
            payload["ids"] = list(record_ids)
        if template.children:
            payload["children"] = {
                "field": template.children.key,
                "model": template.children.model,
                "fields": list(template.children.fields),
                "limit": template.children.limit,
            }

        body = await self._post(RECORDS_PATH, payload)
        rows = body.get("records")
        if not isinstance(rows, list):
            message = "Odoo returned no 'records' list"
            raise DependencyUnavailableError(message, context={"source_key": source_key})

        records = [
            record
            for row in rows
            if isinstance(row, dict) and (record := _to_record(template.res_model, row)) is not None
        ]
        return RecordBatch(
            records=records,
            watermark=_parse_datetime(body.get("watermark")),
            more=bool(body.get("more")),
        )

    async def read_binary(self, source_key: str, record_id: int) -> bytes:
        """Fetch one attachment's bytes.

        Base64 over JSON rather than a binary body: it is what Odoo already
        stores, it keeps this endpoint the same shape as the others, and the
        alternative would be a second content type for one caller.
        """
        body = await self._post(BINARY_PATH, {"source_key": source_key, "id": record_id})
        encoded = body.get("content")
        if not isinstance(encoded, str):
            return b""
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            message = f"Odoo returned an unreadable attachment for {source_key}:{record_id}"
            raise ValidationError(message) from exc

    async def available_sources(self) -> Mapping[str, bool]:
        """Which sources this Odoo can serve, by source key.

        Odoo is told which models to check rather than asked what it has: the
        registry lives here, and an endpoint that enumerated every installed
        model would tell an attacker the shape of the deployment.
        """
        models = {key: REGISTRY[key].res_model for key in REGISTRY}
        body = await self._post(SOURCES_PATH, {"models": sorted(set(models.values()))})
        available = body.get("sources")
        if not isinstance(available, dict):
            return {}
        return {key: bool(available.get(model)) for key, model in models.items()}

    async def aclose(self) -> None:
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

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=dict(payload))
        except httpx.HTTPError as exc:
            message = f"Odoo is unreachable at {path}"
            raise DependencyUnavailableError(
                message, context={"path": path, "error": type(exc).__name__}
            ) from exc

        status = response.status_code
        if status == HTTPStatus.NOT_FOUND:
            message = f"Odoo cannot serve {path}: the model or addon is missing"
            raise NotFoundError(message, context={"path": path})
        if status >= HTTPStatus.BAD_REQUEST:
            message = f"Odoo refused {path} with {status}"
            raise DependencyUnavailableError(message, context={"path": path, "status": status})

        try:
            body = response.json()
        except ValueError as exc:
            message = f"Odoo answered {path} with a body that is not JSON"
            raise DependencyUnavailableError(message, context={"path": path}) from exc
        if not isinstance(body, dict):
            message = f"Odoo answered {path} with {type(body).__name__}, expected an object"
            raise DependencyUnavailableError(message, context={"path": path})
        return body


def _to_record(res_model: str, row: Mapping[str, Any]) -> SourceRecord | None:
    record_id = row.get("id")
    if not isinstance(record_id, int):
        return None
    company = row.get("company_id")
    return SourceRecord(
        res_model=res_model,
        res_id=record_id,
        values=row,
        write_date=_parse_datetime(row.get("write_date")),
        company_id=company[0] if isinstance(company, list) and company else None,
    )


def _odoo_timestamp(value: datetime) -> str:
    """Render a datetime the way Odoo's ORM will accept it.

    Odoo stores UTC and refuses a value that says so: an ISO-8601 string with an
    offset raises ``expecting only datetimes with no timezone``. So the value is
    converted to UTC and then stripped of the fact — which is exactly what
    :func:`_parse_datetime` puts back on the way in.
    """
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _parse_datetime(value: Any) -> datetime | None:
    """Parse Odoo's naive UTC timestamps into aware datetimes.

    Odoo stores and returns UTC without saying so. Leaving them naive would make
    every watermark comparison depend on the worker's local timezone, which is
    the kind of bug that only shows up after a daylight-saving change.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
