from odoo import api, fields, models
from odoo.addons.odoo_atlas.services import engine


class AtlasIngestWizard(models.TransientModel):
    """Turn sources on and start indexing, in one place.

    The list exists as records already, so this wizard is not strictly
    necessary — but "which of these should Atlas index, and go" is one decision,
    and making somebody tick eight rows and then find a button in a different
    menu is how a first run gets abandoned halfway.
    """

    _name = "atlas.ingest.wizard"
    _description = "Configure Atlas Indexing"

    source_ids = fields.Many2many(
        "atlas.ingest.source",
        string="Sources",
        default=lambda self: self._default_sources(),
        domain=[("available", "=", True)],
        help="Only sources this Odoo can actually serve are offered.",
    )
    kind = fields.Selection(
        [
            ("incremental", "Only what changed"),
            ("full_sync", "Everything, skipping unchanged content"),
            ("reindex", "Everything, re-embedding it all"),
        ],
        default="full_sync",
        required=True,
        help=(
            "A first run wants 'everything'. 'Re-embedding' is for after an "
            "embedding model change and is the only one that costs full price."
        ),
    )
    unavailable_note = fields.Char(compute="_compute_unavailable_note")

    @api.model
    def _default_sources(self):
        return self.env["atlas.ingest.source"].search([("active", "=", True)]).ids

    @api.depends_context("uid")
    def _compute_unavailable_note(self):
        missing = self.env["atlas.ingest.source"].search([("available", "=", False)])
        note = (
            self.env._(
                "%(count)s source(s) are not available on this database: %(names)s",
                count=len(missing),
                names=", ".join(missing.mapped("source_key")),
            )
            if missing
            else ""
        )
        for record in self:
            record.unavailable_note = note

    def action_refresh(self):
        """Re-read the registry from the engine, then come back to this wizard."""
        self.env["atlas.ingest.source"].action_refresh()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "form",
            "target": "new",
        }

    def action_start(self):
        """Enable the chosen sources, disable the rest, and queue the run."""
        self.ensure_one()
        every = self.env["atlas.ingest.source"].search([])
        every.write({"active": False})
        self.source_ids.write({"active": True})

        if not self.source_ids:
            return _notify(
                self.env,
                self.env._("Nothing to index"),
                self.env._("No sources were selected, so nothing was queued."),
                "warning",
            )

        queued, detail = engine.request_sync(self.source_ids.mapped("source_key"), self.kind)
        self.source_ids.write(
            {
                "last_queued": fields.Datetime.now(),
                "last_result": (detail or f"queued {len(queued)} job(s) as {self.kind}")[:200],
            }
        )
        if detail:
            return _notify(self.env, self.env._("Atlas engine unreachable"), detail, "danger")
        return _notify(
            self.env,
            self.env._("Indexing started"),
            self.env._("%(count)s job(s) queued.", count=len(queued)),
            "success",
        )


def _notify(_env, title, message, kind):
    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {"title": title, "message": message, "type": kind, "sticky": kind == "danger"},
    }
