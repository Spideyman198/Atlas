from odoo import api, fields, models
from odoo.addons.odoo_atlas.text import summarise

# Enough of the message to identify it in a breadcrumb or a many2one.
PREVIEW_MAX_LENGTH = 80


class AtlasMessage(models.Model):
    _name = "atlas.message"
    _description = "Atlas Conversation Message"
    _order = "conversation_id, id"

    conversation_id = fields.Many2one(
        "atlas.conversation",
        required=True,
        index=True,
        ondelete="cascade",
    )
    role = fields.Selection(
        [
            ("user", "User"),
            ("assistant", "Assistant"),
            ("system", "System"),
            ("tool", "Tool"),
        ],
        required=True,
        index=True,
    )
    content = fields.Text()
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("streaming", "Streaming"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        required=True,
        default="pending",
        index=True,
    )

    # Authorization support. Both are denormalised from the conversation so that
    # every record rule on this model is a comparison against an indexed column
    # rather than a join back to atlas_conversation. This mirrors the same
    # decision on the engine side, where chunks carry a copy of the document's
    # company and visibility for the retrieval pre-filter
    # (docs/architecture/02-data-architecture.md).
    user_id = fields.Many2one(
        related="conversation_id.user_id",
        string="Owner",
        store=True,
        index=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="conversation_id.company_id",
        store=True,
        index=True,
        readonly=True,
    )

    citation_ids = fields.One2many("atlas.message.citation", "message_id", string="Citations")

    # Reported by the engine. Populated from M10 onward; the fields exist now so
    # the schema does not move under the UI later.
    tool_calls = fields.Json(
        help="Typed tool calls the model requested, as the engine reported them.",
    )
    prompt_tokens = fields.Integer()
    completion_tokens = fields.Integer()
    cost = fields.Float(string="Cost (USD)", digits=(12, 6))
    latency_ms = fields.Integer(string="Latency (ms)")
    model_used = fields.Char(string="Model")
    trace_id = fields.Char(
        index=True,
        help="Correlation id shared with the engine's structured logs for this request.",
    )

    @api.depends("role", "content")
    def _compute_display_name(self):
        labels = dict(self._fields["role"].selection)
        for message in self:
            preview = summarise(message.content, PREVIEW_MAX_LENGTH)
            role = labels.get(message.role, message.role or "")
            message.display_name = f"{role}: {preview}" if preview else role

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        for message in messages:
            message.conversation_id._register_message(message)
        return messages
