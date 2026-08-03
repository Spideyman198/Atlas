"""Fitting a long conversation into a prompt without losing what follow-ups need."""

from __future__ import annotations

import pytest

from atlas.application.memory import ConversationMemory
from atlas.domain.chat import StopReason
from atlas.domain.errors import ProviderTimeoutError
from atlas.domain.orchestration import Turn
from atlas.infrastructure.prompts import JinjaPromptLibrary
from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

pytestmark = pytest.mark.unit


def turns(count: int, *, size: int = 20) -> tuple[Turn, ...]:
    return tuple(
        Turn(question=f"question {index} " + "x" * size, answer=f"answer {index} " + "y" * size)
        for index in range(count)
    )


def memory(chat: FakeChatProvider, **kwargs: int) -> ConversationMemory:
    return ConversationMemory(chat=chat, prompts=JinjaPromptLibrary(), **kwargs)


class TestShortConversations:
    async def test_nothing_is_summarised_when_it_all_fits(self) -> None:
        chat = FakeChatProvider()

        summary, recent = await memory(chat).compress(turns(3))

        assert summary == ""
        assert len(recent) == 3
        assert chat.call_count == 0

    async def test_an_empty_history_costs_nothing(self) -> None:
        chat = FakeChatProvider()

        summary, recent = await memory(chat).compress(())

        assert (summary, recent) == ("", ())
        assert chat.call_count == 0


class TestLongConversations:
    async def test_older_turns_become_a_summary(self) -> None:
        chat = FakeChatProvider([fake_response("Discussed Acme's overdue invoices.")])

        summary, recent = await memory(chat, budget=50).compress(turns(10, size=200))

        assert summary == "Discussed Acme's overdue invoices."
        assert len(recent) < 10

    async def test_the_most_recent_turns_survive_verbatim(self) -> None:
        """The turn before this one is what "and the second one?" refers to."""
        history = turns(10, size=200)
        chat = FakeChatProvider([fake_response("summary")])

        _summary, recent = await memory(chat, budget=50, keep_verbatim=2).compress(history)

        assert recent[-1] == history[-1]
        assert len(recent) == 2

    async def test_the_summariser_is_given_the_dropped_turns(self) -> None:
        history = turns(10, size=200)
        chat = FakeChatProvider([fake_response("summary")])

        await memory(chat, budget=50, keep_verbatim=2).compress(history)

        prompt = chat.requests[0].messages[0].content
        assert history[0].question in prompt
        assert history[-1].question not in prompt

    async def test_it_splits_on_size_rather_than_turn_count(self) -> None:
        """One turn quoting a report is worth twenty short ones."""
        chat = FakeChatProvider([fake_response("summary")])

        _summary, recent = await memory(chat, budget=200).compress(turns(6, size=10))

        assert len(recent) == 6
        assert chat.call_count == 0


class TestWhenSummarisingFails:
    async def test_a_provider_failure_does_not_fail_the_answer(self) -> None:
        """A slightly worse answer beats no answer."""
        chat = FakeChatProvider([ProviderTimeoutError("too slow")])

        summary, recent = await memory(chat, budget=50).compress(turns(10, size=200))

        assert summary == ""
        assert recent

    async def test_a_refused_summary_is_treated_the_same_way(self) -> None:
        chat = FakeChatProvider([fake_response("", stop_reason=StopReason.REFUSAL)])

        summary, _recent = await memory(chat, budget=50).compress(turns(10, size=200))

        assert summary == ""
