"""Keeping a conversation inside a context window.

A long conversation cannot all go into every prompt, and dropping the oldest
turns loses exactly the thing follow-up questions depend on — "and the second
one?" refers back to a list from six turns ago.

So recent turns are kept verbatim and older ones are replaced by a summary. The
split is by token budget rather than turn count, because one turn quoting a
sales report is worth twenty short ones.

The summary is produced by the same chat provider as the answers, which costs a
model call. It happens on the turn that crosses the budget, not on every turn,
and the result is handed back to the caller to store: this class does not own a
database, and a conversation's history belongs to Odoo.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from atlas.domain.chat import ChatRequest, Message, Role
from atlas.domain.errors import AtlasError
from atlas.domain.orchestration import Turn
from atlas.domain.ports.chat import ChatProvider
from atlas.domain.ports.prompts import PromptLibrary
from atlas.domain.retrieval import estimate_tokens

logger = logging.getLogger(__name__)

#: Tokens of conversation history a prompt may carry before older turns are
#: summarised. Small next to the context budget on purpose: history competes
#: with retrieved context, and the retrieved context is what grounds the answer.
DEFAULT_BUDGET: Final = 1500

#: Turns always kept verbatim, however long they are. A summary of the turn
#: immediately before this one is a good way to lose the pronoun it resolves.
KEEP_VERBATIM: Final = 2

#: Ceiling on the summary itself. Long enough for the facts, short enough that
#: it cannot grow into the thing it exists to avoid.
SUMMARY_TOKENS: Final = 400


class ConversationMemory:
    """Splits history into a summary plus recent turns."""

    def __init__(
        self,
        *,
        chat: ChatProvider,
        prompts: PromptLibrary,
        budget: int = DEFAULT_BUDGET,
        keep_verbatim: int = KEEP_VERBATIM,
    ) -> None:
        self._chat = chat
        self._prompts = prompts
        self._budget = budget
        self._keep_verbatim = keep_verbatim

    async def compress(self, history: Sequence[Turn]) -> tuple[str, tuple[Turn, ...]]:
        """Fit ``history`` into the budget.

        Returns:
            The summary of what was dropped — empty when nothing was — and the
            turns to send verbatim.
        """
        if not history:
            return "", ()

        recent = list(history)
        older: list[Turn] = []
        while len(recent) > self._keep_verbatim and _cost(recent) > self._budget:
            older.append(recent.pop(0))

        if not older:
            return "", tuple(recent)

        summary = await self._summarise(older)
        return summary, tuple(recent)

    async def _summarise(self, turns: Sequence[Turn]) -> str:
        """Compress old turns, or drop them if the model cannot be reached.

        A failed summary is not a failed request. The alternative — refusing to
        answer because the history could not be compressed — trades a slightly
        worse answer for no answer at all.
        """
        prompt = self._prompts.render(
            "summarise",
            turns=[{"question": turn.question, "answer": turn.answer} for turn in turns],
        )
        request = ChatRequest(
            messages=(Message(role=Role.USER, content=prompt.text),),
            max_output_tokens=SUMMARY_TOKENS,
        )
        try:
            response = await self._chat.complete(request)
        except AtlasError as exc:
            logger.warning(
                "could not summarise conversation history; continuing without it",
                extra={"turns": len(turns), "error": exc.code},
            )
            return ""

        if response.is_refusal:
            # Rare, but a conversation about something a classifier dislikes
            # would otherwise fail on the summary rather than on the answer.
            logger.info("summariser declined; continuing without history")
            return ""

        logger.info(
            "summarised conversation history",
            extra={"turns": len(turns), "summary_chars": len(response.content)},
        )
        return response.content.strip()


def _cost(turns: Sequence[Turn]) -> int:
    return sum(estimate_tokens(turn.question) + estimate_tokens(turn.answer) for turn in turns)
