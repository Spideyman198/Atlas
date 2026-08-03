"""Deciding what to fetch before fetching it.

A question either needs live ERP data, stored documents, or both. Fetching both
every time works and costs a document search plus a tool round-trip on questions
that never needed one of them. Fetching the wrong one produces a confident answer
built on stale text.

So: rules for the cases that are genuinely obvious, and **hybrid for everything
else**. That asymmetry is the whole design. A wrong route costs latency; hybrid
costs latency too, and is never wrong for it. There is no rule here that fires
on a question a rule cannot actually recognise.

Why not ask a model to route? It would be more accurate on the ambiguous middle,
and it would add a full round-trip to every question — including the ones that
are obvious — to choose between paths that mostly overlap anyway. When M12 has
numbers showing what routing errors actually cost, that trade can be revisited
with evidence. It should not be guessed at now.

Routing decides fetching, never permission. Every path ends at the same
authorization stage (ADR-0006), so a misrouted question reaches nothing its
asker could not already see.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from atlas.domain.orchestration import Intent, Routing

logger = logging.getLogger(__name__)

#: Asking for something to change. This release reads and nothing else, and
#: saying so immediately beats letting a model discover there is no such tool.
_WRITE = re.compile(
    r"\b(create|add|make|new|update|change|edit|modify|set|delete|remove|cancel"
    r"|confirm|validate|post|send|approve|assign|archive|duplicate|import)\b"
    r"(?!\s+(?:orders?|invoices?|customers?|records?|products?)\s+(?:are|is|were|was|do|does))",
    re.IGNORECASE,
)

#: A question about a record, a total, or current state. Anything here is a
#: question a document cannot answer correctly, because the answer moved since
#: the document was written.
_STRUCTURED = re.compile(
    r"\b(how many|how much|total|sum|average|count|number of"
    r"|revenue|turnover|sales|profit|margin"
    r"|overdue|unpaid|owes?|outstanding|receivable|payable|balance"
    r"|in stock|stock|inventory|on hand|available|quantity|reorder"
    r"|order|invoice|bill|quotation|quote|purchase|delivery|shipment"
    r"|customer|supplier|vendor|partner|contact|lead|opportunity"
    r"|top|best|worst|most|least|highest|lowest|biggest|largest|ranking"
    r"|this (?:month|quarter|year|week)|last (?:month|quarter|year|week)"
    r"|since|between|status of|state of)\b",
    re.IGNORECASE,
)

#: A record reference: S00042, INV/2026/0001, SO-1234. A question containing one
#: is about that record, whatever else it says.
_REFERENCE = re.compile(r"\b[A-Z]{1,6}[/\-]?\d{3,}\b|\b[A-Z]{2,6}/\d{4}/\d+\b")

#: A question about what something says, rather than what something is.
_SEMANTIC = re.compile(
    r"\b(policy|policies|procedure|process|guideline|rule|terms|conditions"
    r"|contract|agreement|warranty|refund|return policy|sla"
    r"|manual|documentation|document|handbook|instructions|specification"
    r"|what does .{0,30} say|according to|described|explain(?:ed)?|definition"
    r"|note|notes|comment|message|discussion|wrote|said)\b",
    re.IGNORECASE,
)

#: Below this, there is no question to route. Two characters of punctuation is
#: not a question, and neither is an empty string.
_MIN_QUESTION_CHARS: Final = 3


class IntentRouter:
    """Chooses which retrieval paths run for a question."""

    def route(self, question: str) -> Routing:
        """Decide, and record why.

        Args:
            question: What was asked, verbatim.
        """
        routing = self._decide(question)
        logger.debug(
            "routed question",
            extra={"intent": routing.intent.value, "reason": routing.reason},
        )
        return routing

    def _decide(self, question: str) -> Routing:
        text = question.strip()
        if len(text) < _MIN_QUESTION_CHARS:
            return Routing(
                intent=Intent.REFUSE,
                reason="there is no question here",
                refusal="empty",
            )

        # Checked before anything else: "delete the draft orders" mentions
        # orders, and answering it as a question about orders would be a way of
        # not answering it at all.
        if _WRITE.search(text) and not _is_a_question(text):
            return Routing(
                intent=Intent.REFUSE,
                reason="asks for a change; this release only reads",
                refusal="write",
            )

        structured = bool(_STRUCTURED.search(text)) or bool(_REFERENCE.search(text))
        semantic = bool(_SEMANTIC.search(text))

        if structured and semantic:
            return Routing(Intent.HYBRID, "reads as both live data and documents")
        if structured:
            return Routing(Intent.STRUCTURED, "asks about records or current totals")
        if semantic:
            return Routing(Intent.SEMANTIC, "asks what something says")
        return Routing(Intent.HYBRID, "no confident signal; fetching both")


def _is_a_question(text: str) -> bool:
    """Whether a write word is being asked about rather than asked for.

    "Which orders were cancelled?" contains `cancel` and is a question about
    history. "Cancel order S00042" is an instruction. The distinction is not
    perfect and does not need to be: a false refusal is visible and correctable
    by rephrasing, whereas the alternative is an assistant that appears to
    attempt changes it cannot make.
    """
    lowered = text.lower().lstrip()
    return text.rstrip().endswith("?") or lowered.startswith(
        ("what", "which", "who", "when", "where", "why", "how", "is ", "are ", "do ", "did ")
    )
