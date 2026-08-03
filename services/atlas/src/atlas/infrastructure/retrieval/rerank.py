"""Reranking, and why there is only a seam here.

Retrieval scores a query and a chunk independently — that is what makes a
bi-encoder fast enough to run over a whole corpus, and also what limits it. A
cross-encoder reads the pair together and is markedly better at ordering the
top few results.

**No cross-encoder ships by default, and that is a decision rather than an
omission.** The usual implementation pulls in `sentence-transformers` and
`torch`: roughly 2.5 GB in the image, a much larger CVE surface for M14's scans,
and per-query latency nobody has measured yet. ADR-0003's dependency policy is
to add an integration package when a milestone needs one, and the milestone that
needs this is M13 — where there will be a golden set (M12) to say whether the
reranking actually improves anything and a latency budget to say what it costs.

So the pipeline has somewhere to put a reranker, the port is fixed, and the
default does nothing. Adding one later is a class and a line in the composition
root, with no change above it.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlas.domain.corpus import CandidateChunk


class NoOpReranker:
    """Keeps the retriever's order, trimmed to the limit.

    The honest default. It is not a placeholder that pretends to rerank — it
    says plainly that nothing has re-scored these, so a reader of a trace is not
    misled into thinking a model looked at them.
    """

    name = "none"

    async def rerank(
        self,
        query: str,
        candidates: Sequence[CandidateChunk],
        *,
        limit: int,
    ) -> list[CandidateChunk]:
        return list(candidates[:limit])
