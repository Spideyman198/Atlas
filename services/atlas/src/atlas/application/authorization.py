"""Stage 2 of retrieval: ask Odoo which candidates the user may actually read.

This is the step the whole design exists to protect
(:doc:`ADR-0006 </adr/0006-data-access-and-authorization>`). The index is
deliberately broader than any one user's view — ingestion reads as an
integration user so that it can see everything worth indexing — so the only
thing standing between a warehouse intern and the pricing on somebody else's
deal is that nothing reaches a prompt until Odoo has confirmed, in this request
and as this user, that they may read it.

Two properties, both tested:

**It fails closed.** Every way this can go wrong ends in
:class:`~atlas.domain.errors.AuthorizationError` and no chunks. An Odoo that is
down, slow, or returning nonsense results in a refused answer, never an
unfiltered one.

**It cannot be skipped.** The only way to obtain an
:class:`~atlas.domain.corpus.AuthorizedChunk` is to call this filter. The prompt
assembler takes those and nothing else, so bypassing the check is not a policy
that could be forgotten — it is a type error.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence

from atlas.domain.authorization import UserContext
from atlas.domain.corpus import AuthorizedChunk, CandidateChunk
from atlas.domain.errors import AtlasError, AuthorizationError
from atlas.domain.ports.odoo_gateway import OdooGateway

logger = logging.getLogger(__name__)


class AuthorizationFilter:
    """Turns candidates into authorized chunks, or into nothing at all."""

    def __init__(self, gateway: OdooGateway) -> None:
        self._gateway = gateway

    async def filter(
        self,
        context: UserContext,
        candidates: Sequence[CandidateChunk],
    ) -> list[AuthorizedChunk]:
        """Return the candidates Odoo confirms the acting user may read.

        Order is preserved, so a caller's ranking survives the filter.

        Raises:
            AuthorizationError: Odoo declined the context, or could not be asked.
                Both mean the same thing to a caller: authorize nothing.
        """
        if not candidates:
            return []

        by_model: dict[str, list[int]] = defaultdict(list)
        unbacked = 0
        for candidate in candidates:
            if candidate.res_model and candidate.res_id:
                by_model[candidate.res_model].append(candidate.res_id)
            else:
                unbacked += 1

        if unbacked:
            # Chunks with no Odoo record behind them — uploaded PDFs, manuals —
            # are authorized by visibility tier and owning group instead
            # (ADR-0006). No source produces them until M7, and until the check
            # that covers them exists, dropping them is the only honest option.
            logger.info(
                "dropped chunks with no record to authorize",
                extra={"trace_id": context.trace_id, "count": unbacked},
            )

        if not by_model:
            return []

        granted = await self._ask(context, by_model)

        authorized = [
            _authorize(candidate)
            for candidate in candidates
            if candidate.res_model
            and candidate.res_id
            and candidate.res_id in granted.get(candidate.res_model, frozenset())
        ]

        logger.info(
            "authorization filter applied",
            extra={
                "trace_id": context.trace_id,
                "models": len(by_model),
                "candidates": len(candidates),
                "authorized": len(authorized),
                "denied": len(candidates) - len(authorized),
            },
        )
        return authorized

    async def _ask(
        self,
        context: UserContext,
        by_model: dict[str, list[int]],
    ) -> dict[str, frozenset[int]]:
        """One batched call to Odoo, with every failure collapsed into a denial.

        The blanket ``except`` is the point rather than an oversight. Any
        unexpected failure here has to mean "authorize nothing"; letting a new
        exception type escape uncaught would be a leak waiting for the day
        somebody adds one.
        """
        try:
            return await self._gateway.authorize(context, by_model)
        except AuthorizationError:
            raise
        except AtlasError as exc:
            logger.warning(
                "authorization gateway failed, denying everything",
                extra={"trace_id": context.trace_id, "error": exc.code},
            )
            message = "could not confirm access with Odoo"
            raise AuthorizationError(message, context={"cause": exc.code}) from exc
        except Exception as exc:
            logger.exception(
                "authorization gateway raised an unexpected error, denying everything",
                extra={"trace_id": context.trace_id},
            )
            message = "could not confirm access with Odoo"
            raise AuthorizationError(message, context={"cause": type(exc).__name__}) from exc


def _authorize(candidate: CandidateChunk) -> AuthorizedChunk:
    """Promote one candidate. The only place this conversion happens."""
    return AuthorizedChunk(
        chunk_id=candidate.chunk_id,
        document_id=candidate.document_id,
        content=candidate.content,
        score=candidate.score,
        res_model=candidate.res_model,
        res_id=candidate.res_id,
        external_ref=candidate.external_ref,
        metadata=candidate.metadata,
    )
