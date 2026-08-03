"""The Odoo gateway port.

Odoo is the authorization authority, and this is how the engine asks it
(:doc:`ADR-0006 </adr/0006-data-access-and-authorization>`). Three operations,
each executed by Odoo as the user the context names:

``authorize``   which of these records may they read — stage 2 of retrieval
``read_records``  give me these fields of the ones they may read
``execute_tool``  run this typed tool for them

Every implementation must fail closed. A gateway that cannot reach Odoo raises;
it never answers "everything is fine" or returns an empty grant that a caller
could mistake for "nothing matched". The difference between "denied" and
"unknown" is the difference between a correct answer and a leak.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from atlas.domain.authorization import UserContext


@runtime_checkable
class OdooGateway(Protocol):
    """Odoo, as the engine is allowed to see it."""

    async def authorize(
        self,
        context: UserContext,
        records: Mapping[str, Sequence[int]],
    ) -> dict[str, frozenset[int]]:
        """Return, per model, the subset of ids the acting user may read.

        Batched by model rather than one call per record: a question can retrieve
        forty candidates across four models, and forty round-trips would put the
        authorization step above the whole retrieval budget.

        Args:
            context: Who the request acts as.
            records: Candidate ids per Odoo model name.

        Returns:
            A mapping with an entry for every model asked about. A model that
            granted nothing maps to an empty set, so a caller can tell "asked
            and refused" from "never asked".

        Raises:
            AuthorizationError: Odoo refused the context itself — an expired
                token, a user who lost access.
            DependencyUnavailableError: Odoo could not be reached, or failed.
                Callers must treat this as "authorize nothing", never as
                "authorize everything".
        """
        ...

    async def read_records(
        self,
        context: UserContext,
        model: str,
        ids: Sequence[int],
        fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Read named fields of the records the acting user may see.

        Records they may not see are absent from the result rather than reported
        as denied. The caller already knows what it asked for; telling it which
        ids were refused would leak the existence of records it cannot read.
        """
        ...

    async def execute_tool(
        self,
        context: UserContext,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run one typed tool inside Odoo, as the acting user.

        The tool set is closed and lives on Odoo's side (M9). The engine names a
        tool and passes arguments; it never sends a domain, and never sends SQL.

        Raises:
            NotFoundError: No such tool.
            ValidationError: The arguments were rejected.
        """
        ...
