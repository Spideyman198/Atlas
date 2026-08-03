from odoo import fields, models

#: Denied ids are kept for diagnosis, not for accounting, and one request may be
#: refused hundreds. Storing a sample keeps the table useful without letting a
#: single noisy query dominate it; ``denied_count`` stays exact either way.
DENIED_IDS_SAMPLE = 50


class AtlasAccessLog(models.Model):
    """What the engine asked Odoo for, on whose behalf, and what it was given.

    Separate from ``atlas.message`` because one message can trigger several
    authorization checks across several models, and because the log has to
    outlive the conversation it came from
    (``docs/architecture/02-data-architecture.md``).

    Append-only through the ORM: no group has write access, so a row cannot be
    edited after the fact. Administrators may delete, which is how retention is
    handled until it is automated.
    """

    _name = "atlas.access.log"
    _description = "Atlas Access Log"
    _order = "create_date desc, id desc"
    _rec_name = "trace_id"

    user_id = fields.Many2one(
        "res.users",
        string="Acting User",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.user,
        help="The user whose access rights the request was answered under.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    trace_id = fields.Char(
        index=True,
        help="Correlation id shared with the engine's logs and the answering message.",
    )
    operation = fields.Selection(
        [
            ("authorize", "Authorize"),
            ("records", "Read Records"),
            ("tool", "Tool Call"),
        ],
        required=True,
        index=True,
    )
    res_model = fields.Char(string="Model", index=True)
    tool_name = fields.Char(string="Tool")
    requested_count = fields.Integer()
    granted_count = fields.Integer()
    denied_count = fields.Integer()
    denied_ids = fields.Text(
        help="A sample of the ids the acting user was refused, for diagnosis.",
    )
    duration_ms = fields.Integer(string="Duration (ms)")

    def _record(self, operation, *, trace_id=None, duration_ms=0, **values):
        """Write one audit row, as the acting user.

        The user and company come from the environment rather than from the
        caller, so a client cannot attribute its own request to somebody else.

        Nothing is caught here. An access that could not be audited is exactly
        what the log exists to make impossible, so a failure to write one fails
        the request that caused it.
        """
        return self.create(
            {
                "operation": operation,
                "trace_id": trace_id,
                "duration_ms": duration_ms,
                **values,
            }
        )

    def _record_authorization(self, model, requested, granted, *, trace_id=None, duration_ms=0):
        """Write the audit row for one model's worth of an authorize call."""
        denied = [record_id for record_id in requested if record_id not in granted]
        return self._record(
            "authorize",
            trace_id=trace_id,
            duration_ms=duration_ms,
            res_model=model,
            requested_count=len(requested),
            granted_count=len(granted),
            denied_count=len(denied),
            denied_ids=format_denied_ids(denied),
        )


def format_denied_ids(denied):
    """Render denied ids for storage, truncated with a count of the remainder."""
    if not denied:
        return False
    sample = ", ".join(str(record_id) for record_id in denied[:DENIED_IDS_SAMPLE])
    remaining = len(denied) - DENIED_IDS_SAMPLE
    return f"{sample} (+{remaining} more)" if remaining > 0 else sample
