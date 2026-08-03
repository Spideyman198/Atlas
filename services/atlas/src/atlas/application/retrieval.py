"""The retrieval pipeline, and the assembly of what a prompt is built from.

Three stages, and the order of them *is* the security model:

    retrieve  →  authorize  →  assemble

There is no path from the index to a prompt that skips the middle one. Not
because a reviewer would catch it, but because
:class:`~atlas.domain.retrieval.PromptContext` can only be built from
:class:`~atlas.domain.corpus.AuthorizedChunk`, and the only thing in the system
that produces one of those is
:class:`~atlas.application.authorization.AuthorizationFilter`. Handing the
assembler a candidate is a ``mypy --strict`` error
(:doc:`ADR-0003 </adr/0003-rag-framework-selection>` §4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from atlas.application.authorization import AuthorizationFilter
from atlas.domain.authorization import UserContext
from atlas.domain.corpus import AuthorizedChunk
from atlas.domain.ports.retriever import Retriever
from atlas.domain.retrieval import (
    Citation,
    PromptContext,
    RetrievalRequest,
    RetrievalResult,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

#: How much of a chunk goes into a citation's snippet. Enough for somebody to
#: recognise why the record was cited, short enough that a list of eight of them
#: is still readable.
SNIPPET_CHARS: Final = 240

#: Blocks are numbered so an answer can refer to them, and the numbers line up
#: with the citations returned alongside.
_BLOCK = "[{index}] {label}\n{content}"


class ContextAssembler:
    """Turns authorized chunks into the text a prompt is grounded on.

    Accepts :class:`AuthorizedChunk` and nothing else. That signature is the
    enforcement mechanism described in the module docstring — everything else
    here is formatting.
    """

    def __init__(self, *, snippet_chars: int = SNIPPET_CHARS) -> None:
        self._snippet_chars = snippet_chars

    def assemble(self, chunks: Sequence[AuthorizedChunk], *, budget: int) -> PromptContext:
        """Fit as much context as the budget allows, best first.

        Chunks arrive ranked, so filling greedily from the front spends the
        budget on the most relevant material. A chunk that does not fit is
        skipped rather than truncated: half a sales order is a good way to make
        a model confidently state half a fact.

        Args:
            chunks: Authorized, ranked best-first.
            budget: Tokens available for context.
        """
        blocks: list[str] = []
        citations: list[Citation] = []
        seen_records: dict[tuple[str, int], int] = {}
        used = 0
        dropped = 0

        for chunk in chunks:
            label = _label(chunk)
            block = _BLOCK.format(index=len(blocks) + 1, label=label, content=chunk.content)
            cost = estimate_tokens(block)
            if used + cost > budget:
                dropped += 1
                continue

            blocks.append(block)
            used += cost

            # One citation per record, not per chunk. Three chunks of the same
            # order are one thing to go and look at, and a citation list that
            # says otherwise is noise.
            if chunk.res_model and chunk.res_id:
                key = (chunk.res_model, chunk.res_id)
                if key not in seen_records:
                    seen_records[key] = len(citations) + 1
                    citations.append(
                        Citation(
                            res_model=chunk.res_model,
                            res_id=chunk.res_id,
                            record_name=label,
                            snippet=_snippet(chunk.content, self._snippet_chars),
                            score=chunk.score,
                            sequence=len(citations) + 1,
                        )
                    )

        return PromptContext(
            text="\n\n".join(blocks),
            citations=tuple(citations),
            chunks_used=len(blocks),
            chunks_dropped=dropped,
            estimated_tokens=used,
        )


class RetrievalPipeline:
    """Retrieve, authorize, assemble. In that order, always."""

    def __init__(
        self,
        *,
        retriever: Retriever,
        authorization: AuthorizationFilter,
        assembler: ContextAssembler | None = None,
    ) -> None:
        self._retriever = retriever
        self._authorization = authorization
        self._assembler = assembler or ContextAssembler()

    async def run(self, context: UserContext, request: RetrievalRequest) -> RetrievalResult:
        """Answer-ready context for one question, as one user.

        Raises:
            AuthorizationError: Odoo declined the context, or could not be
                asked. Both mean the same thing: no context, no answer. The
                filter fails closed and this does not soften it.
        """
        candidates = await self._retriever.retrieve(request)
        authorized = await self._authorization.filter(context, candidates)

        # The over-fetch was for authorization's benefit, not the prompt's. What
        # survives is trimmed back to what was actually asked for before the
        # budget gets involved.
        wanted = authorized[: request.limit]
        prompt_context = self._assembler.assemble(wanted, budget=request.token_budget)

        logger.info(
            "retrieval pipeline",
            extra={
                "trace_id": context.trace_id,
                "candidates": len(candidates),
                "authorized": len(authorized),
                "denied": len(candidates) - len(authorized),
                "chunks_used": prompt_context.chunks_used,
                "chunks_dropped": prompt_context.chunks_dropped,
                "estimated_tokens": prompt_context.estimated_tokens,
                "citations": len(prompt_context.citations),
            },
        )
        return RetrievalResult(
            context=prompt_context,
            candidates=len(candidates),
            authorized=len(authorized),
            denied=len(candidates) - len(authorized),
            trace_id=context.trace_id,
        )


def _label(chunk: AuthorizedChunk) -> str:
    """A human-readable name for where a block came from.

    Prefers the record's title, recorded at ingestion, then its external
    reference, then the bare model and id. Something is always available, so a
    citation never reads as "source: unknown".
    """
    title = chunk.metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if chunk.external_ref:
        return chunk.external_ref
    if chunk.res_model and chunk.res_id:
        return f"{chunk.res_model} #{chunk.res_id}"
    return "Document"


def _snippet(content: str, limit: int) -> str:
    """One line of a chunk, for somebody scanning a citation list."""
    collapsed = " ".join(content.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
