"""The registry the typed tools land in.

Empty at M6, deliberately. The tools themselves — ``find_records``,
``aggregate``, ``stock_levels`` and the rest of the closed set named in
``docs/adr/0006-data-access-and-authorization.md`` — arrive in M9. Each will be a
function taking the acting user's environment and already-validated arguments.

The registry exists now so that the boundary exists now. A tool is reachable
only through ``/atlas/api/tool/execute``, which has already proved the caller is
the engine and switched the environment to the acting user. Adding a tool then
means adding an entry here, never adding another route — which is what keeps the
number of ways into Odoo at one.
"""

_TOOLS = {}


def register(name, handler):
    """Register ``handler`` under ``name``.

    Raises:
        ValueError: The name is already taken. Silently replacing a tool would
            let one addon shadow another's, which is not a thing to discover at
            runtime.
    """
    if name in _TOOLS:
        message = f"tool {name!r} is already registered"
        raise ValueError(message)
    _TOOLS[name] = handler
    return handler


def get(name):
    """Return the handler for ``name``, or ``None`` if there is no such tool."""
    return _TOOLS.get(name)


def names():
    """Every registered tool name, sorted."""
    return tuple(sorted(_TOOLS))
