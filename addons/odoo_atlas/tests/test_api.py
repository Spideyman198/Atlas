"""The endpoints, over real HTTP.

These are the tests M6 is judged on. They drive the actual routes through
Odoo's HTTP stack, with real tokens, and assert the thing the whole design
claims: a user cannot obtain a record through Atlas that they could not open in
the Odoo UI.
"""

import os
from unittest.mock import patch

from odoo.addons.odoo_atlas.services import context_token, secrets
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

SERVICE_TOKEN = "test-service-token"
CONTEXT_SECRET = "test-context-secret"


@tagged("-at_install", "post_install")
class TestAtlasApi(HttpCase):
    """Post-install because these need the routing map, which is built once.

    At install time the controller's routes may not be registered yet, and the
    requests would 404 for a reason that has nothing to do with what is being
    tested.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(
            patch.dict(
                os.environ,
                {
                    secrets.SERVICE_TOKEN_VAR: SERVICE_TOKEN,
                    secrets.CONTEXT_SECRET_VAR: CONTEXT_SECRET,
                },
            )
        )

        cls.company = cls.env.ref("base.main_company")
        cls.alice = new_test_user(
            cls.env,
            login="api-alice",
            groups="base.group_user,odoo_atlas.group_atlas_user",
            company_id=cls.company.id,
        )
        cls.bob = new_test_user(
            cls.env,
            login="api-bob",
            groups="base.group_user,odoo_atlas.group_atlas_user",
            company_id=cls.company.id,
        )

        cls.alices_conversation = (
            cls.env["atlas.conversation"].with_user(cls.alice).create({"name": "Alice asks"})
        )
        cls.bobs_conversation = (
            cls.env["atlas.conversation"].with_user(cls.bob).create({"name": "Bob asks"})
        )

    # --- helpers ---------------------------------------------------------

    def token_for(self, user):
        return context_token.mint(self.env(user=user))

    def call(self, path, payload=None, *, service_token=SERVICE_TOKEN):
        """POST to an Atlas endpoint the way the engine does.

        The database goes in a header rather than a session cookie: these calls
        are stateless by design, and a server hosting more than one database
        could not route them otherwise.
        """
        headers = {"X-Odoo-Database": self.env.cr.dbname}
        if service_token is not None:
            headers["Authorization"] = f"Bearer {service_token}"
        # `method` is explicit because an empty JSON body is falsy, and
        # `url_open` only infers POST from a truthy one.
        return self.url_open(path, method="POST", json=payload or {}, headers=headers)

    def authorize(self, user, records, **kwargs):
        return self.call(
            "/atlas/api/authorize",
            {"context_token": self.token_for(user), "records": records, **kwargs},
        )

    def logs_for(self, user):
        return self.env["atlas.access.log"].search([("user_id", "=", user.id)])

    # --- service token ---------------------------------------------------

    def test_status_reports_the_addon_when_the_token_is_right(self):
        response = self.call("/atlas/api/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["addon"], "odoo_atlas")
        self.assertEqual(body["database"], self.env.cr.dbname)

    def test_a_wrong_service_token_is_refused(self):
        response = self.call("/atlas/api/status", service_token="not-the-token")

        self.assertEqual(response.status_code, 401)

    def test_a_missing_service_token_is_refused(self):
        response = self.call("/atlas/api/status", service_token=None)

        self.assertEqual(response.status_code, 401)

    def test_an_unconfigured_service_token_refuses_everything(self):
        # The failure mode that matters: forgetting to set the secret must close
        # the door, not leave it open.
        with patch.dict(os.environ, {secrets.SERVICE_TOKEN_VAR: ""}):
            response = self.call("/atlas/api/status", service_token="")

        self.assertEqual(response.status_code, 401)

    def test_the_service_token_is_checked_before_the_context_token(self):
        # An unauthenticated caller must not be able to make Odoo do work, not
        # even signature verification, by sending rubbish.
        response = self.call(
            "/atlas/api/authorize",
            {"context_token": "nonsense", "records": {"atlas.conversation": [1]}},
            service_token="not-the-token",
        )

        self.assertEqual(response.status_code, 401)

    # --- context token ---------------------------------------------------

    def test_a_forged_context_token_is_refused(self):
        response = self.call(
            "/atlas/api/authorize",
            {"context_token": "v1.forged.deadbeef", "records": {"atlas.conversation": [1]}},
        )

        self.assertEqual(response.status_code, 403)

    def test_a_missing_context_token_is_refused(self):
        response = self.call("/atlas/api/authorize", {"records": {"atlas.conversation": [1]}})

        self.assertEqual(response.status_code, 403)

    def test_a_user_who_lost_atlas_access_is_refused(self):
        token = self.token_for(self.alice)
        self.alice.write({"group_ids": [(3, self.env.ref("odoo_atlas.group_atlas_user").id)]})
        self.env.cr.flush()

        response = self.call(
            "/atlas/api/authorize",
            {"context_token": token, "records": {"atlas.conversation": [1]}},
        )

        self.assertEqual(response.status_code, 403)

    # --- authorize: the property the project exists for ------------------

    def test_a_user_is_granted_their_own_records(self):
        response = self.authorize(self.alice, {"atlas.conversation": [self.alices_conversation.id]})

        self.assertEqual(response.status_code, 200)
        granted = response.json()["granted"]
        self.assertEqual(granted["atlas.conversation"], [self.alices_conversation.id])

    def test_a_restricted_user_cannot_retrieve_a_restricted_record(self):
        # The acceptance criterion. Alice asks about Bob's conversation, which a
        # record rule keeps from her, and Odoo refuses it.
        response = self.authorize(
            self.alice,
            {"atlas.conversation": [self.alices_conversation.id, self.bobs_conversation.id]},
        )

        granted = response.json()["granted"]
        self.assertEqual(granted["atlas.conversation"], [self.alices_conversation.id])
        self.assertNotIn(self.bobs_conversation.id, granted["atlas.conversation"])

    def test_a_model_the_user_cannot_touch_grants_nothing(self):
        # Model-level refusal rather than record-level: config parameters are
        # administrator-only, so the search itself raises inside the controller.
        self.assertFalse(self.env["ir.config_parameter"].with_user(self.alice).has_access("read"))

        response = self.authorize(self.alice, {"ir.config_parameter": [1, 2, 3]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["granted"]["ir.config_parameter"], [])

    def test_a_model_that_does_not_exist_grants_nothing(self):
        response = self.authorize(self.alice, {"atlas.not.a.model": [1]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["granted"]["atlas.not.a.model"], [])

    def test_every_model_asked_about_appears_in_the_answer(self):
        # A caller must be able to tell "asked and refused" from "never asked".
        response = self.authorize(
            self.alice,
            {
                "atlas.conversation": [self.bobs_conversation.id],
                "ir.config_parameter": [1],
            },
        )

        granted = response.json()["granted"]
        self.assertEqual(set(granted), {"atlas.conversation", "ir.config_parameter"})

    def test_an_over_large_batch_is_refused(self):
        response = self.authorize(self.alice, {"atlas.conversation": list(range(1, 2000))})

        self.assertEqual(response.status_code, 400)

    def test_a_malformed_body_is_refused(self):
        response = self.call(
            "/atlas/api/authorize",
            {"context_token": self.token_for(self.alice), "records": {"atlas.conversation": "all"}},
        )

        self.assertEqual(response.status_code, 400)

    # --- records ---------------------------------------------------------

    def test_records_returns_only_what_the_user_may_read(self):
        response = self.call(
            "/atlas/api/records",
            {
                "context_token": self.token_for(self.alice),
                "model": "atlas.conversation",
                "ids": [self.alices_conversation.id, self.bobs_conversation.id],
                "fields": ["name"],
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["records"]
        self.assertEqual([row["id"] for row in rows], [self.alices_conversation.id])
        self.assertEqual(rows[0]["name"], "Alice asks")

    def test_records_defaults_to_the_name_alone(self):
        response = self.call(
            "/atlas/api/records",
            {
                "context_token": self.token_for(self.alice),
                "model": "atlas.conversation",
                "ids": [self.alices_conversation.id],
            },
        )

        row = response.json()["records"][0]
        self.assertEqual(set(row), {"id", "display_name"})

    # --- tools -----------------------------------------------------------

    def test_an_unknown_tool_is_refused(self):
        response = self.call(
            "/atlas/api/tool/execute",
            {"context_token": self.token_for(self.alice), "tool": "no_such_tool"},
        )

        self.assertEqual(response.status_code, 404)

    def test_a_tool_call_without_a_name_is_refused(self):
        response = self.call(
            "/atlas/api/tool/execute", {"context_token": self.token_for(self.alice)}
        )

        self.assertEqual(response.status_code, 400)

    # --- audit -----------------------------------------------------------

    def test_every_authorization_is_logged_against_the_acting_user(self):
        before = len(self.logs_for(self.alice))

        self.authorize(
            self.alice,
            {"atlas.conversation": [self.alices_conversation.id, self.bobs_conversation.id]},
        )

        entries = self.logs_for(self.alice)
        self.assertEqual(len(entries), before + 1)
        entry = entries[0]
        self.assertEqual(entry.operation, "authorize")
        self.assertEqual(entry.res_model, "atlas.conversation")
        self.assertEqual(entry.requested_count, 2)
        self.assertEqual(entry.granted_count, 1)
        self.assertEqual(entry.denied_count, 1)
        self.assertIn(str(self.bobs_conversation.id), entry.denied_ids)

    def test_the_log_records_the_trace_id_the_engine_sent(self):
        self.authorize(
            self.alice,
            {"atlas.conversation": [self.alices_conversation.id]},
            trace_id="trace-abc123",
        )

        entry = self.logs_for(self.alice)[0]
        self.assertEqual(entry.trace_id, "trace-abc123")

    def test_a_refused_call_writes_no_log_entry(self):
        before = len(self.env["atlas.access.log"].search([]))

        self.call("/atlas/api/status", service_token="not-the-token")

        self.assertEqual(len(self.env["atlas.access.log"].search([])), before)

    def test_a_user_cannot_read_another_users_log_entries(self):
        self.authorize(self.bob, {"atlas.conversation": [self.bobs_conversation.id]})

        visible = self.env["atlas.access.log"].with_user(self.alice).search([])

        self.assertFalse(visible.filtered(lambda entry: entry.user_id != self.alice))

    def test_the_log_cannot_be_edited_after_the_fact(self):
        self.authorize(self.alice, {"atlas.conversation": [self.alices_conversation.id]})
        entry = self.logs_for(self.alice)[0]

        with self.assertRaises(AccessError), mute_logger("odoo.addons.base.models.ir_model"):
            entry.with_user(self.alice).write({"granted_count": 999})
