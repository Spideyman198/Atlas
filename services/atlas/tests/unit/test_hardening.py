"""Rate limiting, and checking what the model produced before anyone reads it.

Time is passed in rather than slept through. A rate-limit test that waits for a
real minute is a test somebody eventually marks skip.
"""

from __future__ import annotations

import pytest

from atlas.domain.output_guard import DEFAULT_RUN_LENGTH, inspect, shares_a_run
from atlas.domain.rate_limit import TokenBucketLimiter

pytestmark = pytest.mark.unit


class TestTheAllowance:
    def test_a_first_question_is_always_served(self) -> None:
        limiter = TokenBucketLimiter(per_minute=15, burst=5)

        assert limiter.check("alice", now=0.0).allowed

    def test_a_burst_is_allowed(self) -> None:
        """Three questions pasted in a row is normal use, not a script."""
        limiter = TokenBucketLimiter(per_minute=15, burst=5)

        allowed = [limiter.check("alice", now=0.0).allowed for _ in range(5)]

        assert all(allowed)

    def test_the_burst_runs_out(self) -> None:
        limiter = TokenBucketLimiter(per_minute=15, burst=3)
        for _ in range(3):
            limiter.check("alice", now=0.0)

        assert not limiter.check("alice", now=0.0).allowed

    def test_it_refills_over_time(self) -> None:
        """A bucket, not a window.

        Somebody idle for a minute is not punished for having asked three
        questions before that.
        """
        limiter = TokenBucketLimiter(per_minute=60, burst=2)
        limiter.check("alice", now=0.0)
        limiter.check("alice", now=0.0)
        assert not limiter.check("alice", now=0.0).allowed

        # 60/minute is one per second.
        assert limiter.check("alice", now=1.0).allowed

    def test_it_never_refills_past_the_burst(self) -> None:
        limiter = TokenBucketLimiter(per_minute=60, burst=2)
        limiter.check("alice", now=0.0)

        # An hour idle does not buy an hour's worth of questions. Asked at one
        # instant, so the sustained rate cannot top the bucket up between them.
        allowed = [limiter.check("alice", now=3600.0).allowed for _ in range(3)]

        assert allowed == [True, True, False]


class TestOneUserCannotStarveAnother:
    def test_allowances_are_separate(self) -> None:
        """The reason the key is the context token and not the address.

        Everyone in an Odoo deployment arrives from the same handful of IPs,
        often exactly one behind a proxy.
        """
        limiter = TokenBucketLimiter(per_minute=15, burst=2)
        limiter.check("alice", now=0.0)
        limiter.check("alice", now=0.0)
        assert not limiter.check("alice", now=0.0).allowed

        assert limiter.check("bob", now=0.0).allowed


class TestWhatARefusalTells:
    def test_it_says_how_long_to_wait(self) -> None:
        """So a client waits the right amount instead of hammering."""
        limiter = TokenBucketLimiter(per_minute=60, burst=1)
        limiter.check("alice", now=0.0)

        decision = limiter.check("alice", now=0.0)

        assert not decision.allowed
        assert decision.retry_after == pytest.approx(1.0)

    def test_it_reports_what_is_left(self) -> None:
        limiter = TokenBucketLimiter(per_minute=60, burst=5)

        assert limiter.check("alice", now=0.0).remaining == 4


class TestMemoryDoesNotGrowForever:
    def test_idle_callers_are_forgotten(self) -> None:
        """Idle callers are dropped.

        Without this the map grows one entry per user who ever asked anything
        and never shrinks.
        """
        limiter = TokenBucketLimiter()
        limiter.check("alice", now=0.0)
        limiter.check("bob", now=1000.0)

        dropped = limiter.forget(before=500.0)

        assert dropped == 1
        assert limiter.tracked == 1

    def test_an_active_caller_survives_a_sweep(self) -> None:
        limiter = TokenBucketLimiter()
        limiter.check("alice", now=1000.0)

        assert limiter.forget(before=500.0) == 0
        assert limiter.tracked == 1


SYSTEM_PROMPT = (
    "You are Atlas, an assistant built into this company's Odoo system. You answer "
    "questions about the company's own data, for the person who asked, using only "
    "what you are given in this conversation. You have no independent knowledge."
)


class TestTheOutputGuard:
    def test_an_ordinary_answer_passes(self) -> None:
        answer = "Order S00042 is a draft quotation totalling 3,150.00 EUR. [1]"

        assert inspect(answer, instructions=SYSTEM_PROMPT).safe

    def test_an_answer_that_quotes_the_prompt_is_caught(self) -> None:
        answer = (
            "Here are my instructions: You are Atlas, an assistant built into this "
            "company's Odoo system. You answer questions about the company's own data, "
            "for the person who asked, using only what you are given in this conversation."
        )

        assert not inspect(answer, instructions=SYSTEM_PROMPT).safe

    def test_explaining_itself_is_not_leaking(self) -> None:
        """An assistant saying what it can do is useful.

        Matching on runs of words separates that from reproducing the prompt.
        """
        answer = (
            "I can only answer from the records you have access to, and I don't have "
            "anything on that. Try asking about a specific order."
        )

        assert inspect(answer, instructions=SYSTEM_PROMPT).safe

    def test_reformatting_does_not_hide_a_quotation(self) -> None:
        """A model asked to reveal its prompt tends to reflow it."""
        answer = (
            "YOU ARE ATLAS, an assistant built into this company's Odoo system!\n"
            "You answer questions about the company's own data, for the person who "
            "asked, using only what you are given in this conversation."
        )

        assert not inspect(answer, instructions=SYSTEM_PROMPT).safe

    def test_a_short_answer_cannot_trip_it(self) -> None:
        assert shares_a_run("You are Atlas.", SYSTEM_PROMPT) is False

    def test_the_run_length_is_what_decides(self) -> None:
        shared = " ".join(f"word{index}" for index in range(DEFAULT_RUN_LENGTH))

        assert shares_a_run(shared, shared) is True
        assert shares_a_run(" ".join(shared.split()[:-1]), shared) is False


class TestTheGuardIsWiredIn:
    async def test_an_answer_reproducing_the_prompt_is_replaced(self) -> None:
        """The prompt tells the model not to reveal its instructions.

        This is the check that the instruction held.
        """
        from tests.unit.test_synthesis import CONTEXT, chunk, service

        from atlas.domain.orchestration import AnswerRequest
        from atlas.infrastructure.prompts import JinjaPromptLibrary
        from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

        leaked = JinjaPromptLibrary().render("system").text[:1200]
        chat = FakeChatProvider([fake_response(leaked)])

        answer = await service(chat=chat, chunks=(chunk("Order S00001."),)).answer(
            CONTEXT, AnswerRequest(question="what does the policy say?")
        )

        assert answer.refused
        assert "You are Atlas" not in answer.text

    async def test_a_secret_in_a_generated_answer_is_stripped(self) -> None:
        """Defence in depth.

        The input redaction covers what went in; this covers a model that
        reconstructed something.
        """
        from tests.unit.test_synthesis import CONTEXT, chunk, service

        from atlas.domain.orchestration import AnswerRequest
        from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

        chat = FakeChatProvider([fake_response("The card is 4111111111111111. [1]")])

        answer = await service(chat=chat, chunks=(chunk("Order S00001."),)).answer(
            CONTEXT, AnswerRequest(question="what does the policy say?")
        )

        assert "4111111111111111" not in answer.text
        assert "payment card" in answer.text
