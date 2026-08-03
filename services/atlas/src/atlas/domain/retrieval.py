"""Retrieval value objects: what is asked for, and what comes back.

The types here encode the one rule the system exists to keep. A
:class:`~atlas.domain.corpus.CandidateChunk` comes out of the index; a
:class:`~atlas.domain.corpus.AuthorizedChunk` is what Odoo has cleared; and
:class:`PromptContext` — the only thing a prompt is built from — can be
assembled from the second and not the first.

That is not a convention anybody has to remember. Under ``mypy --strict`` the
shortcut does not type-check, which is the whole point of
:doc:`ADR-0003 </adr/0003-rag-framework-selection>` §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from atlas.domain.corpus import SearchFilter, Visibility

#: How many candidates to fetch per result asked for. Authorization discards an
#: unknown fraction of them (ADR-0006), and the denial rate is not knowable in
#: advance, so retrieval over-fetches and lets the filter trim. Four is the
#: ADR's figure; M13 makes it adaptive once there is a measurement to adapt to.
DEFAULT_OVER_FETCH: Final = 4

#: Results returned to the caller after authorization, before the token budget
#: has its say.
DEFAULT_LIMIT: Final = 8

#: Characters per token, used to keep assembled context inside a budget.
#: Deliberately pessimistic — three rather than the usual four — because
#: overflowing a context window truncates an answer mid-sentence, while
#: under-filling one merely leaves room unused.
CHARS_PER_TOKEN: Final = 3


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """One search, and the cheap narrowing applied to it.

    The filters here are a *pre-filter*, never an authorization decision. They
    make the index scan cheaper; whether the acting user may see any of what
    comes back is settled afterwards, by asking Odoo (ADR-0006).
    """

    query: str
    limit: int = DEFAULT_LIMIT
    company_ids: tuple[int, ...] = ()
    max_visibility: Visibility = Visibility.RESTRICTED
    res_models: tuple[str, ...] = ()
    over_fetch: int = DEFAULT_OVER_FETCH
    token_budget: int = 4000

    def __post_init__(self) -> None:
        if not self.query or not self.query.strip():
            message = "a retrieval request needs a query"
            raise ValueError(message)
        if self.limit < 1:
            message = f"limit must be at least 1, got {self.limit}"
            raise ValueError(message)
        if self.over_fetch < 1:
            message = f"over_fetch must be at least 1, got {self.over_fetch}"
            raise ValueError(message)

    @property
    def candidate_limit(self) -> int:
        """How many candidates each search mode should return."""
        return self.limit * self.over_fetch

    def search_filter(self) -> SearchFilter:
        """The pre-filter to hand the store."""
        return SearchFilter(
            company_ids=self.company_ids,
            max_visibility=self.max_visibility,
            res_models=self.res_models,
        )


@dataclass(frozen=True, slots=True)
class Citation:
    """A record the answer was built from.

    Produced from the chunks that actually entered the prompt, never by the
    model. A citation therefore cannot be hallucinated: it names something that
    was demonstrably in front of the model when it answered.
    """

    res_model: str
    res_id: int
    record_name: str
    snippet: str
    score: float
    sequence: int


@dataclass(frozen=True, slots=True)
class PromptContext:
    """The text that goes into a prompt, and what it was made of.

    Constructible only from authorized chunks — see the module docstring. The
    counters exist because "how much did retrieval actually contribute" is the
    first question asked of a bad answer, and M12 measures it.
    """

    text: str
    citations: tuple[Citation, ...] = ()
    chunks_used: int = 0
    chunks_dropped: int = 0
    estimated_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        """Whether there is anything to ground an answer on.

        An empty context is not a failure. It means the honest answer is "I
        don't have information on that", and saying so is a correct answer
        (``docs/architecture/03-request-lifecycle.md``).
        """
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What one retrieval produced, and what it cost on the way.

    ``denied`` is not an error count. It is the ordinary, expected outcome of
    asking Odoo about candidates the acting user cannot see, and watching it
    is how M13 decides whether the over-fetch factor is right.
    """

    context: PromptContext
    candidates: int = 0
    authorized: int = 0
    denied: int = 0
    trace_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Estimate a token count from character length.

    Deliberately an estimate. A real tokeniser call per chunk would cost more
    than the budgeting decision is worth, and the ratio is pessimistic so the
    error lands on the safe side of a context window.
    """
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
