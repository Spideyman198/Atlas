from odoo.addons.odoo_atlas.models.atlas_conversation import TITLE_MAX_LENGTH
from odoo.addons.odoo_atlas.tests.common import AtlasCase


class TestConversation(AtlasCase):
    def test_a_new_conversation_belongs_to_its_creator(self):
        conversation = self.conversation_for(self.alice)

        self.assertEqual(conversation.user_id, self.alice)
        self.assertEqual(conversation.company_id, self.alice.company_id)
        self.assertEqual(conversation.state, "draft")
        self.assertTrue(conversation.name)

    def test_first_question_titles_the_conversation_and_activates_it(self):
        conversation = self.conversation_for(self.alice)
        placeholder = conversation.name

        self.ask(conversation, "Which invoices are overdue?")

        self.assertEqual(conversation.name, "Which invoices are overdue?")
        self.assertNotEqual(conversation.name, placeholder)
        self.assertEqual(conversation.state, "active")

    def test_later_questions_do_not_retitle_the_conversation(self):
        conversation = self.conversation_for(self.alice)
        self.ask(conversation, "Which invoices are overdue?")

        self.ask(conversation, "And which of those are over 90 days?")

        self.assertEqual(conversation.name, "Which invoices are overdue?")

    def test_an_assistant_message_never_titles_the_conversation(self):
        conversation = self.conversation_for(self.alice)
        placeholder = conversation.name

        self.env["atlas.message"].with_user(self.alice).create(
            {
                "conversation_id": conversation.id,
                "role": "assistant",
                "content": "Three invoices are overdue.",
            }
        )

        self.assertEqual(conversation.name, placeholder)
        self.assertEqual(conversation.state, "active")

    def test_a_long_question_is_truncated_to_one_line(self):
        conversation = self.conversation_for(self.alice)
        question = "Which of our customers\n   have not ordered anything " + "at all " * 30

        self.ask(conversation, question)

        self.assertLessEqual(len(conversation.name), TITLE_MAX_LENGTH)
        self.assertTrue(conversation.name.endswith("\N{HORIZONTAL ELLIPSIS}"))
        self.assertFalse(conversation.name.endswith(" \N{HORIZONTAL ELLIPSIS}"))
        self.assertNotIn("\n", conversation.name)
        self.assertNotIn("  ", conversation.name)

    def test_a_blank_question_leaves_the_title_alone(self):
        conversation = self.conversation_for(self.alice)
        placeholder = conversation.name

        self.ask(conversation, "   \n  ")

        self.assertEqual(conversation.name, placeholder)

    def test_totals_follow_the_messages(self):
        conversation = self.conversation_for(self.alice)
        self.assertEqual(conversation.message_count, 0)
        self.assertEqual(conversation.total_cost, 0.0)

        self.ask(conversation, "Which invoices are overdue?", cost=0.000125)
        self.env["atlas.message"].with_user(self.alice).create(
            {
                "conversation_id": conversation.id,
                "role": "assistant",
                "content": "Three.",
                "cost": 0.004,
            }
        )

        self.assertEqual(conversation.message_count, 2)
        self.assertAlmostEqual(conversation.total_cost, 0.004125, places=6)

    def test_deleting_a_message_updates_the_totals(self):
        conversation = self.conversation_for(self.alice)
        message = self.ask(conversation, "Which invoices are overdue?", cost=0.5)
        self.assertEqual(conversation.message_count, 1)

        message.unlink()

        self.assertEqual(conversation.message_count, 0)
        self.assertEqual(conversation.total_cost, 0.0)

    def test_a_message_bumps_the_last_activity(self):
        conversation = self.conversation_for(self.alice)
        conversation.write({"last_activity": "2020-01-01 00:00:00"})

        self.ask(conversation, "Which invoices are overdue?")

        self.assertGreater(str(conversation.last_activity), "2020-01-01 00:00:00")

    def test_deleting_a_conversation_takes_its_messages_with_it(self):
        conversation = self.conversation_for(self.alice)
        message = self.ask(conversation, "Which invoices are overdue?")

        conversation.with_user(self.alice).unlink()

        self.assertFalse(message.exists())

    def test_archive_and_reopen(self):
        conversation = self.conversation_for(self.alice)
        self.ask(conversation, "Which invoices are overdue?")

        conversation.with_user(self.alice).action_set_archived()
        self.assertEqual(conversation.state, "archived")

        conversation.with_user(self.alice).action_set_active()
        self.assertEqual(conversation.state, "active")

    def test_a_message_is_named_by_its_role_and_content(self):
        conversation = self.conversation_for(self.alice)
        message = self.ask(conversation, "Which invoices are overdue?")

        self.assertEqual(message.display_name, "User: Which invoices are overdue?")

    def test_a_message_denormalises_owner_and_company(self):
        conversation = self.conversation_for(self.alice)

        message = self.ask(conversation, "Which invoices are overdue?")

        self.assertEqual(message.user_id, self.alice)
        self.assertEqual(message.company_id, conversation.company_id)
