from odoo import api, fields, models


class AtlasMessageCitation(models.Model):
    """A record an answer was based on.

    Citations are rows rather than a JSON blob on the message so that they can be
    searched, grouped and resolved to a real record without parsing JSON in the
    view layer — see docs/architecture/02-data-architecture.md.
    """

    _name = "atlas.message.citation"
    _description = "Atlas Message Citation"
    _order = "message_id, sequence, id"
    _rec_name = "record_name"

    message_id = fields.Many2one(
        "atlas.message",
        required=True,
        index=True,
        ondelete="cascade",
    )
    sequence = fields.Integer(default=10, help="Order the citations were presented in.")

    # A soft reference, like ir.attachment's. There is no foreign key: the cited
    # record may be deleted after the answer was given, and a dangling citation
    # is not a reason to lose the answer.
    res_model = fields.Char(string="Model", required=True, index=True)
    res_id = fields.Integer(string="Record ID", required=True)
    record_name = fields.Char(
        string="Record",
        help="Name of the cited record at the time the answer was given.",
    )
    record_ref = fields.Reference(
        selection="_selection_target_model",
        compute="_compute_record_ref",
        string="Open Record",
        help="Resolves the citation to the live record, so it can be opened from here.",
    )

    snippet = fields.Text(help="The retrieved text that supported the answer.")
    score = fields.Float(digits=(12, 6), help="Retrieval score reported by the engine.")

    # Denormalised for the record rules, exactly as on atlas.message.
    user_id = fields.Many2one(
        related="message_id.user_id",
        string="Owner",
        store=True,
        index=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="message_id.company_id",
        store=True,
        index=True,
        readonly=True,
    )

    _res_id_is_positive = models.Constraint(
        "CHECK (res_id > 0)",
        "A citation must point at a real record id.",
    )

    @api.model
    def _selection_target_model(self):
        """Every model in the registry: a citation may point at any of them.

        Read straight from the registry rather than from ``ir.model``, which
        ordinary users cannot read and which would therefore need ``sudo()`` for
        a value that is only ever used to render a link.
        """
        return [(name, name) for name in self.env.registry]

    @api.depends("res_model", "res_id")
    def _compute_record_ref(self):
        # Model-level access only. Whether this user may read this particular
        # record is decided by Odoo when the link is followed, and by the engine
        # on every retrieval (ADR-0006); this check exists so a citation to a
        # model the user has no access to at all does not break the view.
        accessible = {}
        for citation in self:
            citation.record_ref = False
            model_name = citation.res_model
            if not model_name or not citation.res_id:
                continue
            if model_name not in accessible:
                known = model_name in self.env
                accessible[model_name] = known and self.env[model_name].has_access("read")
            if accessible[model_name]:
                citation.record_ref = f"{model_name},{citation.res_id}"
