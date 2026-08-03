"""What a question is, what an answer is, and what happens in between.

The orchestrator's vocabulary. Three ideas here are worth stating plainly,
because the rest of the module is shaped by them.

**A refusal is an answer.** "I don't have information on that" is correct output,
not a failure — so it is a field on :class:`Answer` rather than an exception.
Anything that forces refusal down an error path eventually gets caught by
something well-meaning and turned back into a guess.

**Citations are not produced by the model.** They come from the blocks that
demonstrably went into the prompt. The model emits markers — ``[1]``, ``[2]`` —
and the orchestrator resolves them. A marker naming a block that was never there
is dropped, so a citation cannot be invented.

**Intent decides what to fetch, not what to say.** Routing wrong costs a slower
or emptier answer; it does not let anything through that authorization would
otherwise have stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atlas.domain.retrieval import Citation
from atlas.domain.usage import TokenUsage


class Intent(StrEnum):
    """Which retrieval paths run for a question.

    Attributes:
        STRUCTURED: Live ERP data. Tools, no document search.
        SEMANTIC: Documents, notes, policies. Search, no tools.
        HYBRID: Both, or not confidently either. The default when unsure,
            because it costs more and is never wrong for it.
        REFUSE: Decided without fetching anything — an empty question, or a
            request to change data, which this release does not do.
    """

    STRUCTURED = "structured"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    REFUSE = "refuse"

    @property
    def uses_tools(self) -> bool:
        """Whether the model should be offered tools."""
        return self in (Intent.STRUCTURED, Intent.HYBRID)

    @property
    def uses_retrieval(self) -> bool:
        """Whether documents should be searched."""
        return self in (Intent.SEMANTIC, Intent.HYBRID)


@dataclass(frozen=True, slots=True)
class Routing:
    """The router's decision, and why it made it.

    The reason is not decoration. When an answer is wrong, the first question is
    whether the right things were fetched, and a routing decision with no
    recorded reason cannot be argued with.
    """

    intent: Intent
    reason: str
    #: Set when the router alone decided to refuse, so the orchestrator can say
    #: something specific instead of the generic "nothing to go on".
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange, as conversation memory holds it."""

    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    """A question, and everything needed to answer it as one person.

    Attributes:
        question: What was asked, verbatim.
        history: Earlier turns, oldest first. May be summarised already.
        conversation_id: Correlates this answer with the Odoo record it is
            stored on.
        intent: Force a route instead of letting the router decide. For tests
            and for the "search documents only" affordance in the UI.
    """

    question: str
    history: tuple[Turn, ...] = ()
    conversation_id: int | None = None
    intent: Intent | None = None


@dataclass(frozen=True, slots=True)
class Answer:
    """A finished answer, and the evidence for it.

    Attributes:
        text: What to show. Citation markers are left in place: they are how a
            reader tells which sentence rests on which record.
        citations: Resolved from the markers the model actually used, ordered by
            block number so the list matches the markers in the text. A citation
            here was in front of the model when it answered.
        refused: True when the honest answer was that there was nothing to go
            on. ``text`` still holds something readable to show.
        intent: What the router decided, for logs and for M12.
        tools_called: Tool names, in call order. Repeats are kept: calling the
            same tool four times is a fact worth seeing.
    """

    text: str
    citations: tuple[Citation, ...] = ()
    refused: bool = False
    intent: Intent = Intent.HYBRID
    tools_called: tuple[str, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    prompt_version: str = ""
    trace_id: str | None = None

    @property
    def is_grounded(self) -> bool:
        """Whether the answer rests on something citable.

        A grounded answer is not necessarily a correct one. It is one where the
        reader can go and check.
        """
        return bool(self.citations)


class EventKind(StrEnum):
    """What a streamed event carries."""

    DELTA = "delta"
    CITATIONS = "citations"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    """One increment of an answer being streamed.

    Citations arrive as their own event at the end rather than being folded into
    the final delta, because a client renders them somewhere else on the page.
    """

    kind: EventKind
    text: str = ""
    citations: tuple[Citation, ...] = ()
    answer: Answer | None = None

    @classmethod
    def delta(cls, text: str) -> AnswerEvent:
        """Text as it is generated."""
        return cls(kind=EventKind.DELTA, text=text)

    @classmethod
    def done(cls, answer: Answer) -> AnswerEvent:
        """The terminal event, carrying the assembled answer."""
        return cls(kind=EventKind.DONE, answer=answer, citations=answer.citations)

    @classmethod
    def error(cls, message: str) -> AnswerEvent:
        """A failure the client should show rather than retry silently."""
        return cls(kind=EventKind.ERROR, text=message)
