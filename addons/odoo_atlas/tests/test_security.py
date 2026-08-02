"""Negative access paths.

Atlas answers a question under the asking user's own access rights
([ADR-0006](docs/adr/0006-data-access-and-authorization.md)). A conversation is
therefore a record of what one particular user was allowed to see, and letting a
second user read it would hand them answers computed under permissions they do
not have. These tests assert that they cannot.
"""

from odoo.addons.odoo_atlas.tests.common import AtlasCase
from odoo.exceptions import AccessError, UserError
from odoo.tools import mute_logger


class TestConversationAccess(AtlasCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bobs_conversation = cls.env["atlas.conversation"].with_user(cls.bob).create({})
        cls.bobs_message = (
            cls.env["atlas.message"]
            .with_user(cls.bob)
            .create(
                {
                    "conversation_id": cls.bobs_conversation.id,
                    "role": "user",
                    "content": "What is our margin on the Vermont order?",
                }
            )
        )
        cls.bobs_citation = (
            cls.env["atlas.message.citation"]
            .with_user(cls.bob)
            .create(
                {
                    "message_id": cls.bobs_message.id,
                    "res_model": "res.partner",
                    "res_id": cls.env.ref("base.main_partner").id,
                }
            )
        )

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_read_another_users_conversation(self):
        with self.assertRaises(AccessError):
            self.bobs_conversation.with_user(self.alice).read(["name"])

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_read_another_users_messages(self):
        with self.assertRaises(AccessError):
            self.bobs_message.with_user(self.alice).read(["content"])

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_read_another_users_citations(self):
        with self.assertRaises(AccessError):
            self.bobs_citation.with_user(self.alice).read(["res_model", "res_id"])

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_write_to_another_users_conversation(self):
        with self.assertRaises(AccessError):
            self.bobs_conversation.with_user(self.alice).write({"name": "Mine now"})

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_delete_another_users_conversation(self):
        with self.assertRaises(AccessError):
            self.bobs_conversation.with_user(self.alice).unlink()

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_a_user_cannot_add_a_message_to_another_users_conversation(self):
        with self.assertRaises(AccessError):
            self.env["atlas.message"].with_user(self.alice).create(
                {
                    "conversation_id": self.bobs_conversation.id,
                    "role": "user",
                    "content": "Answer this as Bob, please.",
                }
            )

    def test_a_user_cannot_push_their_conversation_onto_someone_else(self):
        # Record rules stop a user reaching into another's conversation. Nothing
        # in them stops the reverse — handing one of your own to somebody else,
        # answers and all — so the model forbids it outright.
        conversation = self.conversation_for(self.alice)

        with self.assertRaises(UserError):
            conversation.with_user(self.alice).write({"user_id": self.bob.id})

    def test_not_even_an_administrator_can_change_the_owner(self):
        with self.assertRaises(UserError):
            self.bobs_conversation.with_user(self.manager).write({"user_id": self.alice.id})

    def test_another_users_conversation_is_invisible_to_search(self):
        found = self.env["atlas.conversation"].with_user(self.alice).search([])

        self.assertNotIn(self.bobs_conversation, found)
        self.assertFalse(found.filtered(lambda c: c.user_id != self.alice))

    def test_another_users_messages_are_invisible_to_search(self):
        found = self.env["atlas.message"].with_user(self.alice).search([])

        self.assertNotIn(self.bobs_message, found)

    def test_a_user_sees_their_own_conversation(self):
        conversation = self.conversation_for(self.alice)

        self.assertEqual(conversation.with_user(self.alice).name, conversation.name)
        self.assertIn(conversation, self.env["atlas.conversation"].with_user(self.alice).search([]))

    def test_an_administrator_sees_every_conversation_in_their_companies(self):
        conversation = self.conversation_for(self.alice)
        found = self.env["atlas.conversation"].with_user(self.manager).search([])

        self.assertIn(conversation, found)
        self.assertIn(self.bobs_conversation, found)

    def test_an_administrator_sees_every_message_and_citation(self):
        self.assertTrue(self.bobs_message.with_user(self.manager).read(["content"]))
        self.assertTrue(self.bobs_citation.with_user(self.manager).read(["res_model"]))


class TestModelAccess(AtlasCase):
    """A user outside the Atlas groups has no access at all, not merely no records."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.outsider = cls.env["res.users"].create(
            {
                "name": "Outsider",
                "login": "atlas-outsider",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
                "company_id": cls.company.id,
                "company_ids": [(6, 0, [cls.company.id])],
            }
        )

    @mute_logger("odoo.addons.base.models.ir_model")
    def test_a_user_outside_the_atlas_groups_cannot_read_conversations(self):
        with self.assertRaises(AccessError):
            self.env["atlas.conversation"].with_user(self.outsider).search([])

    @mute_logger("odoo.addons.base.models.ir_model")
    def test_a_user_outside_the_atlas_groups_cannot_create_a_conversation(self):
        with self.assertRaises(AccessError):
            self.env["atlas.conversation"].with_user(self.outsider).create({})

    @mute_logger("odoo.addons.base.models.ir_model")
    def test_a_user_outside_the_atlas_groups_cannot_read_messages(self):
        with self.assertRaises(AccessError):
            self.env["atlas.message"].with_user(self.outsider).search([])


class TestMultiCompanyAccess(AtlasCase):
    """The company rules carry no group, so they bind administrators too."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Created as the superuser: nobody in this test has the other company
        # among their allowed ones, which is the point.
        cls.foreign_conversation = cls.env["atlas.conversation"].create(
            {
                "name": "Owned elsewhere",
                "user_id": cls.alice.id,
                "company_id": cls.other_company.id,
            }
        )

    def test_a_conversation_in_another_company_is_invisible_to_its_own_owner(self):
        found = self.env["atlas.conversation"].with_user(self.alice).search([])

        self.assertNotIn(self.foreign_conversation, found)

    def test_a_conversation_in_another_company_is_invisible_to_an_administrator(self):
        found = self.env["atlas.conversation"].with_user(self.manager).search([])

        self.assertNotIn(self.foreign_conversation, found)

    @mute_logger("odoo.addons.base.models.ir_rule")
    def test_reading_a_conversation_from_another_company_is_refused(self):
        with self.assertRaises(AccessError):
            self.foreign_conversation.with_user(self.manager).read(["name"])

    def test_the_owner_sees_it_once_the_company_is_allowed(self):
        self.alice.write({"company_ids": [(4, self.other_company.id)]})
        allowed = (
            self.env["atlas.conversation"]
            .with_user(self.alice)
            .with_context(allowed_company_ids=self.alice.company_ids.ids)
        )

        self.assertIn(self.foreign_conversation, allowed.search([]))
