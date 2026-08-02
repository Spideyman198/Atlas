from odoo.addons.odoo_atlas.tests.common import AtlasCase
from odoo.tools import mute_logger
from psycopg2 import IntegrityError


class TestCitation(AtlasCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Cited Customer"})

    def cite(self, message, **values):
        return (
            self.env["atlas.message.citation"]
            .with_user(message.user_id)
            .create({"message_id": message.id, **values})
        )

    def answer(self, conversation, content="Three invoices are overdue."):
        return (
            self.env["atlas.message"]
            .with_user(conversation.user_id)
            .create({"conversation_id": conversation.id, "role": "assistant", "content": content})
        )

    def test_a_citation_resolves_to_the_record_it_names(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)

        citation = self.cite(
            message,
            res_model="res.partner",
            res_id=self.partner.id,
            record_name=self.partner.name,
            snippet="Cited Customer has three overdue invoices.",
            score=0.87,
        )

        self.assertEqual(citation.record_ref, self.partner)
        self.assertEqual(citation.display_name, "Cited Customer")

    def test_a_citation_denormalises_owner_and_company(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)

        citation = self.cite(message, res_model="res.partner", res_id=self.partner.id)

        self.assertEqual(citation.user_id, self.alice)
        self.assertEqual(citation.company_id, conversation.company_id)

    def test_no_reference_when_the_user_cannot_reach_the_model_at_all(self):
        # Premise: config parameters are administrator-only. If that ever stops
        # being true this assertion fails first, rather than the test passing
        # for the wrong reason.
        restricted = self.env["ir.config_parameter"].with_user(self.alice)
        self.assertFalse(restricted.has_access("read"))

        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)
        citation = self.cite(message, res_model="ir.config_parameter", res_id=1)

        self.assertFalse(citation.with_user(self.alice).record_ref)

    def test_no_reference_when_the_model_no_longer_exists(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)

        citation = self.cite(message, res_model="atlas.model.that.was.removed", res_id=1)

        self.assertFalse(citation.record_ref)

    def test_a_citation_must_point_at_a_real_record_id(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)

        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.cite(message, res_model="res.partner", res_id=0)

    def test_deleting_a_message_takes_its_citations_with_it(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)
        citation = self.cite(message, res_model="res.partner", res_id=self.partner.id)

        message.with_user(self.alice).unlink()

        self.assertFalse(citation.exists())

    def test_a_citation_survives_the_record_it_points_at(self):
        conversation = self.conversation_for(self.alice)
        message = self.answer(conversation)
        citation = self.cite(
            message,
            res_model="res.partner",
            res_id=self.partner.id,
            record_name=self.partner.name,
        )

        self.partner.unlink()

        self.assertTrue(citation.exists())
        self.assertEqual(citation.record_name, "Cited Customer")
