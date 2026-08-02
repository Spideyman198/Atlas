{
    "name": "Odoo Atlas",
    "summary": "Ask questions about your Odoo data, answered under your own access rights",
    "description": """
Odoo Atlas
==========

The Odoo-side half of Atlas: conversations, messages and citations, the security
model that scopes them to their owner, and the settings that point Odoo at the
engine.

This addon is a thin adapter. It holds no AI code. Retrieval, orchestration and
model calls live in the `atlas-api` engine, which runs as a separate process and
never imports `odoo` (ADR-0002).
""",
    # Odoo series, then the project's own version. The project reaches 1.0.0 at
    # M15, at which point this becomes 19.0.1.0.0.
    "version": "19.0.0.1.0",
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
        "views/atlas_conversation_views.xml",
        "views/atlas_message_views.xml",
        "views/res_config_settings_views.xml",
        "views/atlas_menus.xml",
    ],
    "application": True,
    "installable": True,
    "auto_install": False,
}
