from odoo import api, fields, models
from odoo.addons.odoo_atlas.services import context_token, engine, suggestions
from odoo.addons.odoo_atlas.text import summarise
from odoo.exceptions import UserError

# A conversation is titled from its first user message. Long enough to stay
# recognisable in a list, short enough not to wrap.
TITLE_MAX_LENGTH = 60


class AtlasConversation(models.Model):
    _name = "atlas.conversation"
    _description = "Atlas Conversation"
    _order = "last_activity desc, id desc"

    name = fields.Char(
        required=True,
        default=lambda self: self.env._("New conversation"),
        help="Taken from the first question asked in this conversation. Editable afterwards.",
    )
    user_id = fields.Many2one(
        "res.users",
        string="Owner",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.user,
        help="The user whose access rights every answer in this conversation is subject to.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("archived", "Archived"),
        ],
        required=True,
        default="draft",
        index=True,
        help="Draft until the first message is sent, archived when the user is done with it.",
    )
    last_activity = fields.Datetime(
        default=fields.Datetime.now,
        index=True,
        readonly=True,
    )

    message_ids = fields.One2many("atlas.message", "conversation_id", string="Messages")
    message_count = fields.Integer(compute="_compute_message_totals", store=True)
    total_cost = fields.Float(
        string="Cost (USD)",
        compute="_compute_message_totals",
        store=True,
        digits=(12, 6),
        help=(
            "Provider spend across every message. Held in USD rather than as a Monetary "
            "field because model providers bill in USD whatever the company currency is."
        ),
    )

    @api.depends("message_ids.cost")
    def _compute_message_totals(self):
        for conversation in self:
            conversation.message_count = len(conversation.message_ids)
            conversation.total_cost = sum(conversation.message_ids.mapped("cost"))

    def write(self, values):
        # A conversation cannot change hands. Its answers were computed under one
        # user's access rights (ADR-0006), so handing it to a second user would
        # show them results assembled from records they may not read. The record
        # rules stop a user reaching into someone else's conversation; this stops
        # the reverse, pushing one of their own into somebody else's list.
        if "user_id" in values:
            reassigned = self.filtered(lambda c: c.user_id.id != values["user_id"])
            if reassigned:
                raise UserError(
                    self.env._(
                        "A conversation cannot change owner. Its answers were computed under "
                        "the access rights of %(owner)s and do not carry over to anyone else.",
                        owner=reassigned[0].user_id.display_name,
                    )
                )
        return super().write(values)

    def _register_message(self, message):
        """Bring the conversation up to date after ``message`` was added to it.

        Called from :meth:`atlas.message.create`. A conversation leaves ``draft``
        on its first message, and takes its title from the first question asked.
        """
        self.ensure_one()
        values = {"last_activity": fields.Datetime.now()}
        if self.state == "draft":
            values["state"] = "active"
        if message.role == "user":
            questions = self.message_ids.filtered(lambda m: m.role == "user")
            title = summarise(message.content, TITLE_MAX_LENGTH)
            if title and len(questions) == 1:
                values["name"] = title
        self.write(values)

    def _atlas_context_token(self):
        """Mint the token the engine will present when acting for this owner.

        Internal, and not reachable over RPC: the token is minted server-side
        immediately before the addon calls the engine, and travels only on that
        call. Nothing in the browser ever sees one.
        """
        self.ensure_one()
        return context_token.mint(
            self.env(user=self.user_id),
            ttl_seconds=engine.context_token_ttl(),
        )

    @api.model
    def atlas_suggestions(self):
        """Questions to offer somebody looking at an empty panel.

        Derived from what this database has installed and what this user may
        read, so nobody is offered a question that comes back as a refusal.
        """
        return suggestions.for_user(self.env)

    def atlas_transcript(self):
        """This conversation's messages and their citations, for the UI.

        One call rather than three: the panel needs messages and citations
        together, and fetching them separately means rendering a message with
        its citations missing for however long the second request takes.

        Record rules decide what comes back. A conversation that is not this
        user's raises before any of it is read.
        """
        self.ensure_one()
        self.check_access("read")
        return [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content or "",
                "status": message.status,
                "citations": [
                    {
                        "id": citation.id,
                        "sequence": citation.sequence,
                        "res_model": citation.res_model,
                        "res_id": citation.res_id,
                        "record_name": citation.record_name or citation.res_model,
                        "snippet": citation.snippet or "",
                    }
                    for citation in message.citation_ids.sorted("sequence")
                ],
            }
            for message in self.message_ids.sorted("id")
            # System and tool messages are the machinery, not the conversation.
            # Showing them would turn a transcript into a debug log.
            if message.role in ("user", "assistant")
        ]

    def action_set_archived(self):
        self.write({"state": "archived"})

    def action_set_active(self):
        self.write({"state": "active"})
