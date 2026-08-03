"""The closed set of typed tools, and the registry that holds them.

A tool is reachable only through ``/atlas/api/tool/execute``, which has already
proved the caller is the engine and switched the environment to the acting user.
Adding a tool means adding an entry here, never adding another route — which is
what keeps the number of ways into Odoo at one.

Three rules hold for every tool, and are tested rather than asserted:

**Read-only.** Nothing here creates, writes or deletes. ``test_tools.py`` scans
this package and fails the build if that stops being true. Write operations are
a post-1.0 milestone with explicit human confirmation, not something to smuggle
in behind a helpful-looking argument.

**As the acting user.** No ``sudo()``, so record rules decide what a tool can
see. A tool cannot answer a question its caller could not have answered by
clicking around Odoo themselves.

**No SQL and no raw domains.** Arguments are structured objects compiled against
per-model allow-lists (:mod:`~.filters`).
"""

from odoo.addons.odoo_atlas.services.tools.base import Tool
from odoo.addons.odoo_atlas.services.tools.handlers import TOOLS

_TOOLS = {tool.name: tool for tool in TOOLS}


def register(tool):
    """Add a tool to the registry.

    Raises:
        ValueError: The name is taken. Silently replacing a tool would let one
            addon shadow another's, which is not a thing to discover at runtime.
    """
    if tool.name in _TOOLS:
        message = f"tool {tool.name!r} is already registered"
        raise ValueError(message)
    _TOOLS[tool.name] = tool
    return tool


def get(name):
    """Return the tool called ``name``, or ``None``."""
    return _TOOLS.get(name)


def names():
    """Every registered tool name, sorted."""
    return tuple(sorted(_TOOLS))


def catalog_for(env):
    """The tools this database can serve to the acting user, as JSON schemas.

    This is the single definition the engine hands to the model. Keeping it here
    rather than in the engine means adding a tool is one diff in one place, and
    that a schema can never describe a tool this Odoo does not have.
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in sorted(_TOOLS.values(), key=lambda item: item.name)
        if tool.available_in(env)
    ]


__all__ = ["Tool", "catalog_for", "get", "names", "register"]
