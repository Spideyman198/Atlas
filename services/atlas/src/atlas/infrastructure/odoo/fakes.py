"""An in-memory Odoo gateway.

Exists so the retrieval and orchestration milestones can be developed and tested
without an Odoo, and so the contract suite has a second implementation to hold
the HTTP adapter against. It is configuration, not a stub: give it what each user
may read and it behaves like Odoo would.

Nothing here is a security control. It is only ever wired up in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from atlas.domain.authorization import UserContext
from atlas.domain.errors import AuthorizationError, DependencyUnavailableError, NotFoundError
from atlas.domain.ingestion import RecordBatch, SourceRecord

#: Sorts records with no write date first, rather than raising on comparison.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FakeOdooGateway:
    """A gateway that answers from a dictionary.

    Args:
        readable: Which record ids each context token may read, per model.
            A token absent from this mapping reads nothing.
        records: Rows returned by :meth:`read_records`, per model and id.
        tools: Tool handlers, called with the arguments as given.
        unavailable: When true every call raises, which is how a test says
            "Odoo is down" without unplugging anything.
        failure: Raised by every call. Lets a test assert that the caller fails
            closed on failures it has never seen, including ones that are not
            part of the taxonomy at all.
    """

    def __init__(
        self,
        *,
        readable: Mapping[str, Mapping[str, Sequence[int]]] | None = None,
        records: Mapping[str, Mapping[int, dict[str, Any]]] | None = None,
        tools: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] | None = None,
        unavailable: bool = False,
        failure: Exception | None = None,
    ) -> None:
        self._readable = {
            token: {model: frozenset(ids) for model, ids in per_model.items()}
            for token, per_model in (readable or {}).items()
        }
        self._records = {model: dict(rows) for model, rows in (records or {}).items()}
        self._tools = dict(tools or {})
        self.unavailable = unavailable
        self.failure = failure
        #: Every call made, so a test can assert the batching actually batched.
        self.authorize_calls: list[dict[str, list[int]]] = []

    async def authorize(
        self,
        context: UserContext,
        records: Mapping[str, Sequence[int]],
    ) -> dict[str, frozenset[int]]:
        self._check_available()
        requested = {model: list(ids) for model, ids in records.items() if ids}
        self.authorize_calls.append(requested)
        allowed = self._allowed_for(context)
        return {
            model: frozenset(ids) & allowed.get(model, frozenset())
            for model, ids in requested.items()
        }

    async def read_records(
        self,
        context: UserContext,
        model: str,
        ids: Sequence[int],
        fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        self._check_available()
        allowed = self._allowed_for(context).get(model, frozenset())
        rows = self._records.get(model, {})
        wanted = list(fields) or ["display_name"]
        return [
            {"id": record_id, **{name: rows[record_id].get(name) for name in wanted}}
            for record_id in ids
            if record_id in allowed and record_id in rows
        ]

    async def execute_tool(
        self,
        context: UserContext,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._check_available()
        self._allowed_for(context)
        handler = self._tools.get(tool)
        if handler is None:
            message = f"unknown tool {tool!r}"
            raise NotFoundError(message)
        return handler(arguments)

    def _allowed_for(self, context: UserContext) -> Mapping[str, frozenset[int]]:
        """Reject an unknown token the way Odoo would, rather than reading nothing.

        Returning an empty grant for an unrecognised context would let a test
        pass while the real thing raised, which is the failure this fake exists
        to catch.
        """
        if context.token not in self._readable:
            message = "unknown context token"
            raise AuthorizationError(message)
        return self._readable[context.token]

    def _check_available(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.unavailable:
            message = "Odoo is unreachable"
            raise DependencyUnavailableError(message)


class FakeSourceReader:
    """An in-memory :class:`SourceReader`, for testing the ingestion pipeline.

    Records are held per source key exactly as the HTTP reader would return
    them, so the sync use case under test is the same code that runs in
    production — only the wire is missing.
    """

    def __init__(
        self,
        *,
        records: Mapping[str, Sequence[SourceRecord]] | None = None,
        files: Mapping[int, bytes] | None = None,
        available: Mapping[str, bool] | None = None,
        page_size: int | None = None,
    ) -> None:
        self._records = {key: list(rows) for key, rows in (records or {}).items()}
        self._files = dict(files or {})
        self._available = dict(available or {})
        self._page_size = page_size
        #: Every read, so a test can assert that an incremental sync narrowed by
        #: the watermark instead of re-reading the world.
        self.reads: list[dict[str, Any]] = []

    def set_records(self, source_key: str, records: Sequence[SourceRecord]) -> None:
        """Replace what a source will return on the next read."""
        self._records[source_key] = list(records)

    async def read_records(
        self,
        source_key: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        record_ids: Sequence[int] | None = None,
    ) -> RecordBatch:
        self.reads.append(
            {"source_key": source_key, "since": since, "offset": offset, "ids": record_ids}
        )
        rows = self._records.get(source_key, [])
        if record_ids is not None:
            wanted = set(record_ids)
            selected = [row for row in rows if row.res_id in wanted]
        else:
            selected = [
                row
                for row in rows
                if since is None or (row.write_date is not None and row.write_date > since)
            ]
        selected.sort(key=lambda row: (row.write_date or _EPOCH, row.res_id))

        page = self._page_size or limit
        window = selected[offset : offset + page]
        return RecordBatch(
            records=window,
            watermark=window[-1].write_date if window else None,
            more=offset + page < len(selected),
        )

    async def read_binary(self, source_key: str, record_id: int) -> bytes:  # noqa: ARG002
        return self._files.get(record_id, b"")

    async def available_sources(self) -> Mapping[str, bool]:
        return dict(self._available)
