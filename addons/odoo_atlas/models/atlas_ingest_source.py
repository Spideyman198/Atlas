from odoo import api, fields, models
from odoo.addons.odoo_atlas.services import engine


class AtlasIngestSource(models.Model):
    """A kind of record Atlas keeps in its index.

    Odoo's copy of a registry that lives in the engine. It is a mirror, not the
    definition: :meth:`action_refresh` re-reads it, and nothing here invents a
    source the engine has never heard of. Two lists that both claim to be the
    registry is exactly how one of them ends up wrong.

    Enabling a source here says "index this". What actually reaches the index is
    still whatever the integration user is allowed to read.
    """

    _name = "atlas.ingest.source"
    _description = "Atlas Ingest Source"
    _order = "sequence, source_key"
    _rec_name = "name"

    source_key = fields.Char(required=True, index=True, readonly=True)
    name = fields.Char(required=True, readonly=True)
    res_model = fields.Char(string="Model", readonly=True)
    requires_module = fields.Char(readonly=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(
        default=False,
        help="Sources are off until somebody turns them on: indexing costs money.",
    )
    available = fields.Boolean(
        readonly=True,
        help="Whether Odoo can serve this source to the integration user.",
    )
    last_queued = fields.Datetime(readonly=True)
    last_result = fields.Char(readonly=True)

    _source_key_uniq = models.Constraint(
        "UNIQUE (source_key)",
        "A source can only be listed once.",
    )

    @api.model
    def action_refresh(self):
        """Re-read the registry from the engine and mirror it here.

        Rows are added and updated, never deleted: a source the engine has
        dropped stays visible with ``available`` false, so somebody can see that
        it went away rather than wondering where it went.
        """
        sources, detail = engine.list_sources()
        if not sources:
            return _notify(self.env, "Atlas engine", detail or "No sources reported.", "warning")

        existing = {record.source_key: record for record in self.search([])}
        created = 0
        for entry in sources:
            key = entry.get("key")
            if not key:
                continue
            values = {
                "source_key": key,
                "name": key.removeprefix("odoo.").replace(".", " ").title(),
                "res_model": entry.get("model"),
                "requires_module": entry.get("requires_module") or "base",
                "available": bool(entry.get("available")),
            }
            if record := existing.get(key):
                record.write(values)
            else:
                self.create(values)
                created += 1

        message = f"{len(sources)} sources, {created} new."
        return _notify(self.env, "Atlas sources refreshed", message, "success")

    def action_sync_now(self):
        """Queue an incremental sync for the selected sources."""
        return self._queue("incremental")

    def action_full_sync(self):
        """Queue a full read. Unchanged content is still skipped."""
        return self._queue("full_sync")

    def action_reindex(self):
        """Queue a re-embed of everything. Use after changing embedding model."""
        return self._queue("reindex")

    def _queue(self, kind):
        """Ask the engine to queue work, and record what it said."""
        targets = self.filtered(lambda source: source.active) or self
        queued, detail = engine.request_sync(targets.mapped("source_key"), kind)

        result = detail or f"queued {len(queued)} job(s) as {kind}"
        targets.write({"last_queued": fields.Datetime.now(), "last_result": result[:200]})

        if detail:
            return _notify(self.env, "Atlas engine unreachable", detail, "danger")
        return _notify(self.env, "Ingestion queued", result, "success")

    @api.model
    def _cron_sync(self):
        """Queue an incremental sync for every enabled source.

        Called by ``ir.cron``. Queueing rather than syncing is the point: a full
        run takes minutes, and holding an Odoo cron thread open for all of them
        is exactly the coupling ADR-0002 exists to avoid.
        """
        enabled = self.search([("active", "=", True)])
        if not enabled:
            return
        enabled._queue("incremental")


def _notify(_env, title, message, kind):
    """Show the outcome to whoever pressed the button."""
    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {"title": title, "message": message, "type": kind, "sticky": kind == "danger"},
    }
