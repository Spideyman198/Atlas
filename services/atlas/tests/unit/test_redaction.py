"""What is stripped before a prompt leaves the process, and what is not.

Half of this file asserts that redaction *does not* fire. That half is the more
important one: a rule that shreds order references and customer names produces
an assistant nobody can use, and the first thing anyone would do is switch it
off. A redactor that is safe to leave on is worth more than one that catches
marginally more.
"""

from __future__ import annotations

import pytest

from atlas.domain.redaction import PLACEHOLDER, redact

pytestmark = pytest.mark.unit


class TestCredentials:
    @pytest.mark.parametrize(
        ("secret", "kind"),
        [
            ("sk-ant-api03-abcdefghijklmnop1234567890", "api key"),
            ("ghp_abcdefghijklmnopqrstuvwxyz012345", "api key"),
            ("AKIAIOSFODNN7EXAMPLE", "api key"),
            ("xoxb-123456789012-abcdefghijkl", "api key"),
            ("glpat-abcdefghijklmnopqrst", "api key"),
        ],
    )
    def test_a_provider_key_is_removed(self, secret: str, kind: str) -> None:
        result = redact(f"Ops note: the key is {secret} please rotate it.")

        assert secret not in result.text
        assert PLACEHOLDER.format(kind=kind) in result.text

    def test_a_bearer_token_is_removed(self) -> None:
        result = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc")

        assert "eyJhbGci" not in result.text
        assert result.counts == {"bearer token": 1}

    def test_a_private_key_block_is_removed_whole(self) -> None:
        note = (
            "Deploy key below.\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxyz\nabcdef\n"
            "-----END RSA PRIVATE KEY-----\n"
            "Regards."
        )

        result = redact(note)

        assert "MIIEow" not in result.text
        assert "Regards." in result.text

    @pytest.mark.parametrize(
        "line",
        [
            "password: hunter2000",
            "Password = Tr0ub4dor&3",
            "passphrase: correct-horse-battery",
            "secret: s0mething-long-enough",
        ],
    )
    def test_a_password_in_prose_is_removed(self, line: str) -> None:
        result = redact(f"Handover note. {line}. Do not share.")

        assert "hunter2000" not in result.text
        assert result.counts.get("password") == 1

    def test_the_label_is_required(self) -> None:
        """A bare token is not distinguishable from a product code."""
        result = redact("The replacement part is Tr0ub4dor and it ships Monday.")

        assert not result.redacted


class TestPaymentCards:
    @pytest.mark.parametrize(
        "number",
        [
            "4111111111111111",
            "4111 1111 1111 1111",
            "5500-0000-0000-0004",
            "378282246310005",
        ],
    )
    def test_a_valid_card_is_removed(self, number: str) -> None:
        result = redact(f"Customer paid by card {number} on the 4th.")

        assert number not in result.text
        assert result.counts == {"payment card": 1}

    @pytest.mark.parametrize(
        "number",
        [
            "4111111111111112",  # one digit off: fails Luhn
            "1234567890123",
            "9876543210987654",
        ],
    )
    def test_a_long_number_that_is_not_a_card_is_left_alone(self, number: str) -> None:
        """The check that makes this usable in an ERP.

        Order references, EAN-13s and VAT numbers are all long digit strings.
        Without Luhn this rule would shred the corpus.
        """
        result = redact(f"Reference {number} was despatched.")

        assert number in result.text
        assert not result.redacted


class TestBankAccounts:
    def test_a_valid_iban_is_removed(self) -> None:
        result = redact("Pay to GB82 WEST 1234 5698 7654 32 by Friday.")

        assert "WEST" not in result.text
        assert result.counts == {"bank account": 1}

    def test_a_string_shaped_like_an_iban_but_failing_mod_97_stays(self) -> None:
        result = redact("Pay to GB82 WEST 1234 5698 7654 99 by Friday.")

        assert "WEST" in result.text


class TestNationalIdentifiers:
    def test_a_labelled_ssn_is_removed(self) -> None:
        result = redact("Employee file. SSN: 123-45-6789. Start date March.")

        assert "123-45-6789" not in result.text
        assert result.counts == {"national id": 1}

    def test_an_unlabelled_nine_digit_pattern_is_left_alone(self) -> None:
        """`123-45-6789` on its own matches part numbers and phone extensions."""
        result = redact("Part 123-45-6789 is discontinued.")

        assert "123-45-6789" in result.text


class TestWhatIsDeliberatelyKept:
    """The half of the design that makes the other half acceptable.

    Atlas exists to answer questions about customers. Redacting the answers
    would produce an assistant that can only discuss records in the abstract.
    """

    @pytest.mark.parametrize(
        "content",
        [
            "Acme Corporation, contact Marie Dubois, purchasing manager.",
            "Email marie.dubois@acme.example for the delivery schedule.",
            "Call +32 2 555 0134 or 0475 12 34 56.",
            "Ship to Rue de la Loi 16, 1000 Brussels, Belgium.",
            "VAT BE0477472701 on every invoice.",
            "Sales order S00042 totals 12,480.00 EUR.",
            "Invoice INV/2026/0117 is overdue by 40 days.",
        ],
    )
    def test_the_substance_of_a_question_survives(self, content: str) -> None:
        result = redact(content)

        assert result.text == content
        assert not result.redacted


class TestTheResult:
    def test_a_removal_is_visible_rather_than_a_gap(self) -> None:
        """A removal is named, not blanked.

        A sentence that stops mid-way reads as corruption. A named placeholder
        tells a reader something was removed and what kind.
        """
        result = redact("The card 4111111111111111 was declined.")

        assert result.text == "The card [redacted: payment card] was declined."

    def test_several_secrets_are_counted_separately(self) -> None:
        result = redact("key sk-ant-api03-abcdefghijklmnop1234567890 and card 4111111111111111")

        assert result.counts == {"api key": 1, "payment card": 1}
        assert result.total == 2

    def test_empty_text_is_handled(self) -> None:
        assert redact("").text == ""

    def test_clean_text_is_returned_unchanged(self) -> None:
        content = "Order S00042 for Acme Corporation is confirmed."

        assert redact(content).text is content or redact(content).text == content


class TestItIsActuallyWiredIn:
    """A redactor nothing calls is a redactor that redacts nothing.

    Both entry points are covered: retrieved context and live tool results. A
    note field with a key in it reaches a provider whether it arrived through
    search or through a read.
    """

    async def test_retrieved_context_is_redacted_before_it_reaches_a_prompt(self) -> None:
        from tests.unit.test_synthesis import CONTEXT, chunk, service

        from atlas.domain.orchestration import AnswerRequest
        from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

        chat = FakeChatProvider([fake_response("Noted. [1]")])
        hostile = chunk("Ops note: key sk-ant-api03-abcdefghijklmnop1234567890 rotate it.")

        await service(chat=chat, chunks=(hostile,)).answer(
            CONTEXT, AnswerRequest(question="what does the note say?")
        )

        prompt = chat.requests[0].messages[-1].content
        assert "sk-ant-api03" not in prompt
        assert "[redacted: api key]" in prompt

    async def test_a_tool_result_is_redacted_too(self) -> None:
        from tests.unit.test_toolbox import CONTEXT, call, gateway

        from atlas.application.tools import ToolBox

        box = ToolBox(gateway(find_records=lambda _: {"rows": [{"note": "card 4111111111111111"}]}))

        result = await box.execute(CONTEXT, call())

        assert "4111111111111111" not in result.content
        assert "payment card" in result.content

    async def test_what_the_question_is_about_still_reaches_the_prompt(self) -> None:
        """The wiring must not quietly strip the answer along with the secrets."""
        from tests.unit.test_synthesis import CONTEXT, chunk, service

        from atlas.domain.orchestration import AnswerRequest
        from atlas.infrastructure.providers.fakes import FakeChatProvider, fake_response

        chat = FakeChatProvider([fake_response("Acme. [1]")])
        record = chunk("Acme Corporation, VAT BE0477472701, order S00042 for 12,480.00 EUR.")

        await service(chat=chat, chunks=(record,)).answer(
            CONTEXT, AnswerRequest(question="what does the policy say?")
        )

        prompt = chat.requests[0].messages[-1].content
        assert "Acme Corporation" in prompt
        assert "BE0477472701" in prompt
        assert "S00042" in prompt
