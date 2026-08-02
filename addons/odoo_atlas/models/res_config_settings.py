from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    atlas_engine_url = fields.Char(
        string="Engine URL",
        config_parameter="odoo_atlas.engine_url",
        default="http://atlas-api:8000",
        help=(
            "Where Odoo reaches the Atlas engine. The engine is an internal service and "
            "must not be published beyond the network Odoo runs on."
        ),
    )
    atlas_service_token = fields.Char(
        string="Service Token",
        config_parameter="odoo_atlas.service_token",
        help=(
            "Shared secret presented to the engine on every call, and by the engine "
            "on every callback."
        ),
    )
    atlas_request_timeout = fields.Integer(
        string="Request Timeout",
        config_parameter="odoo_atlas.request_timeout",
        default=60,
        help="Seconds Odoo waits for an answer before giving up on the engine.",
    )
