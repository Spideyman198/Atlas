"""The acceptance criterion for the chat panel, driven through a browser.

    a first-time user completes a question-to-cited-answer round trip
    without instructions

Everything below the browser is already covered by ``test_chat_controller``.
What this adds is the part no Python test can reach: whether the panel a person
actually sees leads them from an empty screen to an answer and then to the
record behind it, using nothing but what is on the page.

The engine is scripted. This is a test of the panel, not of the model — an
assertion about what a language model says would fail for reasons that have
nothing to do with the interface.
"""

import importlib.util
import shutil
from unittest.mock import patch

from odoo.addons.odoo_atlas.services import engine
from odoo.tests import HttpCase, new_test_user, tagged

#: What Odoo looks for, in its order of preference (``odoo/tests/common.py``).
BROWSER_BINARIES = ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable")


@tagged("post_install", "-at_install")
class TestTheBrowserTestsCanRun(HttpCase):
    """A guard against a green suite that ran no browser tests at all.

    Odoo *skips* a browser test when the browser or the websocket client is
    missing, and a skip is not a failure: the suite reports success having
    exercised none of the interface. That is how the tours below first appeared
    to pass. These two assertions turn the same situation into a red build.
    """

    def test_a_browser_is_installed(self):
        found = [name for name in BROWSER_BINARIES if shutil.which(name)]

        self.assertTrue(
            found,
            "no browser on PATH, so every tour would be skipped rather than run. "
            f"Odoo looks for: {', '.join(BROWSER_BINARIES)}",
        )

    def test_the_websocket_client_is_installed(self):
        self.assertIsNotNone(
            importlib.util.find_spec("websocket"),
            "websocket-client is missing, so HttpCase would skip every tour rather than run it.",
        )


@tagged("post_install", "-at_install")
class TestChatTour(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Named so the citation the scripted answer points at is worth opening,
        # and so the tour can recognise it on screen.
        cls.partner = cls.env["res.partner"].create({"name": "Acme Corporation"})
        cls.user = new_test_user(
            cls.env,
            login="atlas-tour",
            password="atlas-tour",
            groups="base.group_user,odoo_atlas.group_atlas_user",
        )

    def scripted_engine(self):
        """An answer that cites a record the user can actually open."""
        partner_id = self.partner.id

        def stream_answer(_question, _token, *, history=None, conversation_id=None):  # noqa: ARG001
            yield "delta", {"text": "Acme Corporation "}
            yield "delta", {"text": "has one open order. [1]"}
            yield (
                "done",
                {
                    "text": "Acme Corporation has one open order. [1]",
                    "refused": False,
                    "intent": "structured",
                    "tools_called": ["find_records"],
                    "trace_id": "tour-trace",
                    "usage": {"input_tokens": 100, "output_tokens": 12},
                    "citations": [
                        {
                            "sequence": 1,
                            "res_model": "res.partner",
                            "res_id": partner_id,
                            "record_name": "Acme Corporation",
                            "snippet": "Acme Corporation, Brussels.",
                        }
                    ],
                },
            )

        return stream_answer

    def test_a_first_time_user_gets_from_a_question_to_the_cited_record(self):
        with patch.object(engine, "stream_answer", self.scripted_engine()):
            self.start_tour(
                "/odoo/action-odoo_atlas.atlas_chat_action",
                "atlas_chat_tour",
                login="atlas-tour",
            )

        conversation = self.env["atlas.conversation"].with_user(self.user).search([], limit=1)
        self.assertTrue(conversation, "the round trip left no conversation behind")
        self.assertEqual(conversation.message_count, 2)

    def test_a_follow_up_continues_the_same_conversation(self):
        with patch.object(engine, "stream_answer", self.scripted_engine()):
            self.start_tour(
                "/odoo/action-odoo_atlas.atlas_chat_action",
                "atlas_chat_followup_tour",
                login="atlas-tour",
            )

        conversations = self.env["atlas.conversation"].with_user(self.user).search([])
        self.assertEqual(len(conversations), 1, "a follow-up started a second conversation")
        self.assertEqual(conversations.message_count, 4)
