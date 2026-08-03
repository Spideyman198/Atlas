from odoo import api, fields, models
from odoo.addons.odoo_atlas.services import engine, secrets


class ResConfigSettings(models.TransientModel):
    """A read-out of how Atlas is configured, plus a way to test it.

    Nothing here is editable. Every Atlas setting on the Odoo side comes from the
    server's environment — see ``services/engine.py`` for why — so this page
    reports what the deployment decided rather than offering to overrule it.
    Presenting a writable field that a redeploy silently discards would be worse
    than presenting none.
    """

    _inherit = "res.config.settings"

    atlas_engine_url = fields.Char(
        string="Engine URL",
        readonly=True,
        compute="_compute_atlas_configuration",
    )
    atlas_request_timeout = fields.Integer(
        string="Request Timeout",
        readonly=True,
        compute="_compute_atlas_configuration",
    )
    atlas_context_token_ttl = fields.Integer(
        string="Context Token Lifetime",
        readonly=True,
        compute="_compute_atlas_configuration",
    )
    atlas_secrets_state = fields.Char(
        string="Secrets",
        readonly=True,
        compute="_compute_atlas_configuration",
    )
    atlas_secrets_ok = fields.Boolean(compute="_compute_atlas_configuration")

    @api.depends_context("uid")
    def _compute_atlas_configuration(self):
        missing = secrets.missing()
        state = (
            self.env._("Configured")
            if not missing
            else self.env._("Missing: %(names)s", names=", ".join(missing))
        )
        for record in self:
            record.atlas_engine_url = engine.base_url()
            record.atlas_request_timeout = engine.request_timeout()
            record.atlas_context_token_ttl = engine.context_token_ttl()
            record.atlas_secrets_ok = not missing
            record.atlas_secrets_state = state

    def action_atlas_test_connection(self):
        """Call the engine's liveness probe and report what came back."""
        self.ensure_one()
        reachable, detail = engine.check_health()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": (
                    self.env._("Atlas engine")
                    if reachable
                    else self.env._("Atlas engine unreachable")
                ),
                "message": detail,
                "type": "success" if reachable else "danger",
                "sticky": not reachable,
            },
        }
