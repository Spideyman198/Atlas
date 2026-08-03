{
    "name": "Odoo Atlas",
    "summary": "Ask questions about your Odoo data, answered under your own access rights",
    "description": """
Odoo Atlas
==========

The Odoo-side half of Atlas: conversations, messages and citations, the security
model that scopes them to their owner, the audit log of what the engine asked
for, and the endpoints it asks through.

This addon is a thin adapter. It holds no AI code. Retrieval, orchestration and
model calls live in the `atlas-api` engine, which runs as a separate process and
never imports `odoo` (ADR-0002). What lives here is the authorization boundary:
every read the engine causes runs as the user who asked, under Odoo's own record
rules (ADR-0006).
""",
    # Odoo series, then the project's own version. The project reaches 1.0.0 at
    # M15, at which point this becomes 19.0.1.0.0.
    "version": "19.0.0.2.0",
    "category": "Productivity",
    "author": "Odoo Atlas contributors",
    "website": "https://github.com/Spideyman198/Atlas",
    "license": "LGPL-3",
    # `base` for users, companies and the settings form; `web` for the backend
    # views. The business modules Atlas will index (sale, purchase, stock,
    # account) are deliberately absent: nothing here imports them, and declaring
    # a dependency to obtain demo data would be a lie the installer has to pay
    # for. Ingestion reads them by name at M7, which needs no dependency.
    "depends": ["base", "web"],
    # Groups are defined before the access rules that reference them.
    "data": [
        "security/atlas_security.xml",
        "security/ir.model.access.csv",
        "data/atlas_cron.xml",
        "views/atlas_chat_views.xml",
        "views/atlas_conversation_views.xml",
        "views/atlas_message_views.xml",
        "views/atlas_access_log_views.xml",
        "views/atlas_ingest_views.xml",
        "views/res_config_settings_views.xml",
        "views/atlas_menus.xml",
    ],
    # The chat panel only exists inside the backend, so it loads with the
    # backend bundle rather than being fetched separately when the action opens.
    "assets": {
        "web.assets_backend": [
            "odoo_atlas/static/src/chat/chat_action.scss",
            "odoo_atlas/static/src/chat/chat_action.js",
            "odoo_atlas/static/src/chat/chat_action.xml",
        ],
        "web.assets_tests": [
            "odoo_atlas/static/tests/tours/**/*",
        ],
    },
    "application": True,
    "installable": True,
    "auto_install": False,
}
