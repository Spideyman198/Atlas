"""The endpoint the chat panel talks to.

Two things are being defended. That the browser never needs a context token to
get an answer — it sends a session cookie and Odoo does the rest — and that what
happened is written down afterwards, including when it went wrong.

The engine is replaced by a scripted generator throughout. This is a test of the
relay, not of the engine; M10 covers what the engine does with a question.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

from odoo.addons.odoo_atlas.controllers import chat
from odoo.addons.odoo_atlas.services import engine, suggestions
from odoo.tests import HttpCase, new_test_user, tagged


def scripted(*events):
    """A stand-in for :func:`engine.stream_answer` that replays ``events``."""

    def stream_answer(question, token, *, history=None, conversation_id=None):
        stream_answer.calls.append(
            {
                "question": question,
                "token": token,
                "history": history,
                "conversation_id": conversation_id,
            }
        )
        yield from events

    stream_answer.calls = []
    return stream_answer


#: Odoo salts the token with a distant expiry when no limit is given.
_CSRF_SALT = 60 * 60 * 24 * 365


def _csrf_token_for(env, session_id):
    secret = env["ir.config_parameter"].sudo().get_param("database.secret")
    max_ts = int(time.time() + _CSRF_SALT)
    message = f"{session_id[:42]}{max_ts}".encode()
    digest = hmac.new(secret.encode("ascii"), message, hashlib.sha1).hexdigest()
    return f"{digest}o{max_ts}"


ANSWER = (
    ("delta", {"text": "Order S00001 "}),
    ("delta", {"text": "is confirmed. [1]"}),
    (
        "done",
        {
            "text": "Order S00001 is confirmed. [1]",
            "refused": False,
            "intent": "structured",
            "tools_called": ["find_records"],
            "trace_id": "trace-xyz",
            "usage": {"input_tokens": 120, "output_tokens": 18},
            "citations": [
                {
                    "sequence": 1,
                    "res_model": "res.partner",
                    "res_id": 1,
                    "record_name": "Acme",
                    "snippet": "Acme placed order S00001.",
                }
            ],
        },
    ),
)


@tagged("post_install", "-at_install")
class TestChatController(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="atlas-chat-user",
            password="atlas-chat-user",
            groups="base.group_user,odoo_atlas.group_atlas_user",
        )

    def ask(self, question, conversation_id=None, *, stream=None):
        """Post a question the way the panel does, and return the raw body."""
        payload = {"question": question}
        if conversation_id:
            payload["conversation_id"] = conversation_id

        with patch.object(engine, "stream_answer", stream or scripted(*ANSWER)):
            response = self.url_open(
                "/atlas/chat/ask",
                data={"payload": json.dumps(payload), "csrf_token": self.csrf_token()},
            )
        self.assertEqual(response.status_code, 200)
        return response.text

    def csrf_token(self):
        """A token for the session this test holds.

        Built the way `Request.csrf_token` builds one. There is no `request` in
        a test, and posting without a token would exercise a path the panel
        never takes — the panel always sends one, so the tests do too.
        """
        return _csrf_token_for(self.env, self.session.sid)

    def events(self, body):
        """Parse a server-sent event body into ``(kind, data)`` pairs."""
        parsed = []
        for block in body.split("\n\n"):
            kind = data = None
            for line in block.splitlines():
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
            if kind:
                parsed.append((kind, data))
        return parsed

    def conversations(self):
        return self.env["atlas.conversation"].with_user(self.user).search([])

    def test_a_question_is_answered_over_the_stream(self):
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        kinds = [kind for kind, _data in self.events(self.ask("what is the status of S00001?"))]

        self.assertEqual(kinds, ["open", "delta", "delta", "done"])

    def test_the_first_event_names_the_conversation(self):
        """A new conversation has no id until the server makes one, and the
        panel needs it before the second question."""
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        events = self.events(self.ask("what is the status of S00001?"))

        self.assertTrue(events[0][1]["conversation_id"])

    def test_the_question_is_stored_before_the_answer_arrives(self):
        """A reload mid-answer should still show what was asked."""
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        def slow(_question, _token, *, history=None, conversation_id=None):  # noqa: ARG001
            stored = (
                self.env["atlas.message"]
                .with_user(self.user)
                .search([("role", "=", "user")])
                .mapped("content")
            )
            self.assertIn("what is the status of S00001?", stored)
            yield "done", {"text": "It is confirmed."}

        self.ask("what is the status of S00001?", stream=slow)

    def test_the_answer_is_stored_with_its_citations(self):
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        self.ask("what is the status of S00001?")

        answer = (
            self.env["atlas.message"]
            .with_user(self.user)
            .search([("role", "=", "assistant")], limit=1)
        )
        self.assertEqual(answer.content, "Order S00001 is confirmed. [1]")
        self.assertEqual(answer.status, "done")
        self.assertEqual(answer.trace_id, "trace-xyz")
        self.assertEqual(answer.prompt_tokens, 120)
        self.assertEqual(answer.completion_tokens, 18)
        self.assertEqual(len(answer.citation_ids), 1)
        self.assertEqual(answer.citation_ids.record_name, "Acme")

    def test_a_citation_with_no_target_is_dropped(self):
        """A chip that opens nothing is worse than one that is not there."""
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        self.ask(
            "what is the status of S00001?",
            stream=scripted(
                ("done", {"text": "Confirmed.", "citations": [{"sequence": 1, "snippet": "x"}]})
            ),
        )

        answer = (
            self.env["atlas.message"]
            .with_user(self.user)
            .search([("role", "=", "assistant")], limit=1)
        )
        self.assertFalse(answer.citation_ids)

    def test_a_failed_answer_is_recorded_as_one(self):
        self.authenticate("atlas-chat-user", "atlas-chat-user")

        events = self.events(
            self.ask(
                "what is the status of S00001?",
                stream=scripted(("error", {"message": "The assistant is unavailable."})),
            )
        )

        self.assertEqual(events[-1][0], "error")
        answer = (
            self.env["atlas.message"]
            .with_user(self.user)
            .search([("role", "=", "assistant")], limit=1)
        )
        self.assertEqual(answer.status, "error")

    def test_the_second_question_continues_the_same_conversation(self):
        self.authenticate("atlas-chat-user", "atlas-chat-user")
        first = self.events(self.ask("what is the status of S00001?"))[0][1]["conversation_id"]

        second = self.events(self.ask("and the next one?", first))[0][1]["conversation_id"]

        self.assertEqual(first, second)
        self.assertEqual(len(self.conversations()), 1)

    def test_earlier_turns_are_sent_to_the_engine(self):
        self.authenticate("atlas-chat-user", "atlas-chat-user")
        conversation_id = self.events(self.ask("what is the status of S00001?"))[0][1][
            "conversation_id"
        ]

        stream = scripted(*ANSWER)
        with patch.object(engine, "stream_answer", stream):
            self.url_open(
                "/atlas/chat/ask",
                data={
                    "payload": json.dumps(
                        {"question": "and the next one?", "conversation_id": conversation_id}
                    ),
                    "csrf_token": self.csrf_token(),
                },
            )

        self.assertEqual(
            stream.calls[0]["history"],
            [
                {
                    "question": "what is the status of S00001?",
                    "answer": "Order S00001 is confirmed. [1]",
                }
            ],
        )

    def test_a_turn_that_failed_is_not_sent_as_history(self):
        """A later answer should not be built on one that never arrived."""
        self.authenticate("atlas-chat-user", "atlas-chat-user")
        conversation_id = self.events(
            self.ask("first question?", stream=scripted(("error", {"message": "down"})))
        )[0][1]["conversation_id"]

        stream = scripted(*ANSWER)
        with patch.object(engine, "stream_answer", stream):
            self.url_open(
                "/atlas/chat/ask",
                data={
                    "payload": json.dumps(
                        {"question": "second question?", "conversation_id": conversation_id}
                    ),
                    "csrf_token": self.csrf_token(),
                },
            )

        self.assertEqual(stream.calls[0]["history"], [])


@tagged("post_install", "-at_install")
class TestChatControllerRefusals(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="atlas-chat-owner",
            password="atlas-chat-owner",
            groups="base.group_user,odoo_atlas.group_atlas_user",
        )
        cls.other = new_test_user(
            cls.env,
            login="atlas-chat-other",
            password="atlas-chat-other",
            groups="base.group_user,odoo_atlas.group_atlas_user",
        )

    def post(self, payload, login="atlas-chat-owner"):
        self.authenticate(login, login)
        with patch.object(engine, "stream_answer", scripted(*ANSWER)):
            return self.url_open(
                "/atlas/chat/ask",
                data={
                    "payload": json.dumps(payload),
                    "csrf_token": _csrf_token_for(self.env, self.session.sid),
                },
            )

    def test_an_empty_question_is_refused(self):
        response = self.post({"question": "   "})

        self.assertIn("event: error", response.text)

    def test_an_oversized_question_is_refused(self):
        response = self.post({"question": "x" * (chat.MAX_QUESTION_LENGTH + 1)})

        self.assertIn("event: error", response.text)
        self.assertIn("too long", response.text)

    def test_a_malformed_payload_is_refused(self):
        self.authenticate("atlas-chat-owner", "atlas-chat-owner")

        response = self.url_open(
            "/atlas/chat/ask",
            data={
                "payload": "not json",
                "csrf_token": _csrf_token_for(self.env, self.session.sid),
            },
        )

        self.assertIn("event: error", response.text)

    def test_another_users_conversation_is_refused(self):
        """Record rules already stop the read. This checks the refusal is a
        refusal rather than a traceback."""
        theirs = self.env["atlas.conversation"].with_user(self.other).create({})

        response = self.post(
            {"question": "what did they ask?", "conversation_id": theirs.id},
            login="atlas-chat-owner",
        )

        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)

    def test_an_anonymous_caller_gets_nowhere(self):
        self.url_open("/web/session/logout")

        response = self.url_open(
            "/atlas/chat/ask", data={"payload": json.dumps({"question": "hello?"})}
        )

        self.assertNotIn("event: done", response.text)


class TestSuggestions(HttpCase):
    """What a first-time user is offered before they have asked anything."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="atlas-suggest",
            groups="base.group_user,odoo_atlas.group_atlas_user",
        )

    def test_there_is_always_something_to_click(self):
        offered = self.env["atlas.conversation"].with_user(self.user).atlas_suggestions()

        self.assertTrue(offered)
        self.assertLessEqual(len(offered), suggestions.MAX_SUGGESTIONS)

    def test_nothing_is_offered_that_this_database_cannot_answer(self):
        """`sale` and `stock` are not installed on the test database, so their
        questions would come back as "you do not have access to that"."""
        offered = self.env["atlas.conversation"].with_user(self.user).atlas_suggestions()

        for model_name, question in suggestions.CANDIDATES:
            if model_name not in self.env:
                self.assertNotIn(question, offered)

    def test_a_model_the_user_cannot_read_is_not_offered(self):
        stranger = new_test_user(self.env, login="atlas-no-partner-access")
        with patch.object(type(self.env["res.partner"]), "has_access", lambda *_: False):
            offered = self.env["atlas.conversation"].with_user(stranger).atlas_suggestions()

        for _model, question in suggestions.CANDIDATES:
            if "customers" in question or "contacts" in question:
                self.assertNotIn(question, offered)
