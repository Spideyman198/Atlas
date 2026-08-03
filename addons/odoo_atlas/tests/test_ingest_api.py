"""The ingestion endpoints, over real HTTP.

Ingestion reads as an integration user that sees more than any one person does.
These tests assert the two things that keeps honest: it is a real Odoo account
whose own access rules decide what gets indexed, and there is no way to make
this path answer *as* somebody else.
"""

import base64
import os
from unittest.mock import patch

from odoo.addons.odoo_atlas.controllers import ingest_api
from odoo.addons.odoo_atlas.services import secrets
from odoo.tests import HttpCase, new_test_user, tagged

SERVICE_TOKEN = "test-service-token"
CONTEXT_SECRET = "test-context-secret"


@tagged("-at_install", "post_install")
class TestAtlasIngestApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        cls.ingest_user = new_test_user(
            cls.env,
            login="atlas-ingest",
            groups="base.group_user,odoo_atlas.group_atlas_ingest",
            company_id=cls.company.id,
        )
        cls.outsider = new_test_user(
            cls.env,
            login="atlas-not-ingest",
            groups="base.group_user",
            company_id=cls.company.id,
        )
        cls.partner = cls.env["res.partner"].create({"name": "Indexable Customer"})
        cls._patch_environment(cls.ingest_user.id)

    @classmethod
    def _patch_environment(cls, uid):
        cls.startClassPatcher(
            patch.dict(
                os.environ,
                {
                    secrets.SERVICE_TOKEN_VAR: SERVICE_TOKEN,
                    secrets.CONTEXT_SECRET_VAR: CONTEXT_SECRET,
                    ingest_api.INGEST_UID_VAR: str(uid),
                },
            )
        )

    def call(self, path, payload=None, *, service_token=SERVICE_TOKEN):
        headers = {"X-Odoo-Database": self.env.cr.dbname}
        if service_token is not None:
            headers["Authorization"] = f"Bearer {service_token}"
        return self.url_open(path, method="POST", json=payload or {}, headers=headers)

    # --- authentication --------------------------------------------------

    def test_the_engine_can_list_what_this_odoo_can_serve(self):
        response = self.call(
            "/atlas/api/ingest/sources", {"models": ["res.partner", "not.a.model"]}
        )

        self.assertEqual(response.status_code, 200)
        sources = response.json()["sources"]
        self.assertTrue(sources["res.partner"])
        self.assertFalse(sources["not.a.model"])

    def test_a_wrong_service_token_is_refused(self):
        response = self.call(
            "/atlas/api/ingest/sources", {"models": []}, service_token="not-the-token"
        )

        self.assertEqual(response.status_code, 401)

    def test_ingestion_is_refused_when_no_integration_user_is_named(self):
        with patch.dict(os.environ, {ingest_api.INGEST_UID_VAR: ""}):
            response = self.call("/atlas/api/ingest/sources", {"models": []})

        self.assertEqual(response.status_code, 403)

    def test_ingestion_is_refused_when_the_named_user_lacks_the_group(self):
        # The token alone is not enough. Without this check a leaked service
        # token plus a guessed uid would read as whoever happens to be uid 1.
        with patch.dict(os.environ, {ingest_api.INGEST_UID_VAR: str(self.outsider.id)}):
            response = self.call("/atlas/api/ingest/sources", {"models": ["res.partner"]})

        self.assertEqual(response.status_code, 403)

    def test_ingestion_is_refused_when_the_named_user_is_archived(self):
        self.ingest_user.write({"active": False})
        self.env.cr.flush()
        try:
            response = self.call("/atlas/api/ingest/sources", {"models": ["res.partner"]})
        finally:
            self.ingest_user.write({"active": True})

        self.assertEqual(response.status_code, 403)

    def test_no_context_token_is_needed_or_honoured(self):
        # This path acts for nobody. Sending a context token must not make it
        # act for somebody — there is no code here that would read one.
        response = self.call(
            "/atlas/api/ingest/sources",
            {"models": ["res.partner"], "context_token": "v1.anything.at.all"},
        )

        self.assertEqual(response.status_code, 200)

    # --- reading ----------------------------------------------------------

    def test_records_come_back_with_the_fields_that_were_asked_for(self):
        response = self.call(
            "/atlas/api/ingest/records",
            {
                "source_key": "odoo.res.partner",
                "model": "res.partner",
                "fields": ["display_name", "write_date"],
                "limit": 50,
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["records"]
        self.assertTrue(rows)
        self.assertIn("display_name", rows[0])
        self.assertIn("id", rows[0])

    def test_unknown_fields_are_dropped_rather_than_failing_the_source(self):
        # Atlas's templates span module combinations. A field missing on this
        # database must not take the whole source down.
        response = self.call(
            "/atlas/api/ingest/records",
            {
                "model": "res.partner",
                "fields": ["display_name", "a_field_that_does_not_exist"],
                "limit": 5,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("a_field_that_does_not_exist", response.json()["records"][0])

    def test_a_model_that_is_not_installed_is_reported_as_missing(self):
        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "not.a.model", "fields": ["display_name"]},
        )

        self.assertEqual(response.status_code, 404)

    def test_a_model_the_integration_user_cannot_read_is_reported_as_missing(self):
        # What the integration user may read is what Atlas may index. Nothing
        # here escalates: this is an ordinary Odoo account.
        self.assertFalse(
            self.env["ir.config_parameter"].with_user(self.ingest_user).has_access("read")
        )

        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "ir.config_parameter", "fields": ["key"]},
        )

        self.assertEqual(response.status_code, 404)

    def test_paging_reports_whether_there_is_more(self):
        self.env["res.partner"].create([{"name": f"Bulk {index}"} for index in range(5)])
        self.env.cr.flush()

        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "res.partner", "fields": ["display_name"], "limit": 2},
        )

        body = response.json()
        self.assertEqual(len(body["records"]), 2)
        self.assertTrue(body["more"])

    def test_selection_values_come_back_as_their_labels(self):
        # `sale` is not what a person searches for; "Sales Order" is. The label
        # is what gets embedded, so this is retrieval quality, not cosmetics.
        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "res.partner", "fields": ["display_name", "company_type"], "limit": 1},
        )

        row = response.json()["records"][0]
        self.assertIn(row["company_type"], ("Individual", "Company"))

    def test_a_malformed_domain_is_refused(self):
        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "res.partner", "fields": ["display_name"], "domain": [["id", "; DROP", 1]]},
        )

        self.assertEqual(response.status_code, 400)

    def test_specific_ids_can_be_requested(self):
        response = self.call(
            "/atlas/api/ingest/records",
            {"model": "res.partner", "fields": ["display_name"], "ids": [self.partner.id]},
        )

        rows = response.json()["records"]
        self.assertEqual([row["id"] for row in rows], [self.partner.id])

    # --- attachments ------------------------------------------------------

    def test_an_attachment_comes_back_base64_encoded(self):
        attachment = (
            self.env["ir.attachment"]
            .with_user(self.ingest_user)
            .create(
                {"name": "terms.txt", "mimetype": "text/plain", "raw": b"Refunds within 30 days."}
            )
        )
        self.env.cr.flush()

        response = self.call("/atlas/api/ingest/binary", {"id": attachment.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(base64.b64decode(response.json()["content"]), b"Refunds within 30 days.")

    def test_an_attachment_the_integration_user_cannot_read_stays_unread(self):
        """Attachments obey ``ir.attachment``'s own rules, like everything else.

        This one belongs to somebody else and hangs off no record the
        integration user can open, so Odoo refuses it — and ingestion indexes
        the metadata it was given rather than failing the whole run.
        """
        private = (
            self.env["ir.attachment"]
            .with_user(self.outsider)
            .create({"name": "private.txt", "mimetype": "text/plain", "raw": b"Not for the index."})
        )
        self.env.cr.flush()

        response = self.call("/atlas/api/ingest/binary", {"id": private.id})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "")

    def test_an_attachment_that_does_not_exist_is_empty_not_an_error(self):
        response = self.call("/atlas/api/ingest/binary", {"id": 99_999_999})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "")
