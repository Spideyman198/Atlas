"""The secrets Atlas needs, read from the environment rather than the database.

There are two, and they are deliberately different secrets:

``ATLAS_SERVICE_TOKEN``
    Shared with the engine. Proves a request on ``/atlas/api/...`` came from the
    engine, and not from anything else that can reach the port.

``ATLAS_CONTEXT_SECRET``
    Odoo's alone. Signs the short-lived tokens that name the acting user. The
    engine must not be able to mint one: if it could, it could ask Odoo to read
    records as any user it chose, which is exactly the property
    ``docs/adr/0006-data-access-and-authorization.md`` exists to protect.

They come from the environment and not from ``ir.config_parameter`` for two
reasons. A secret in the database is readable by every system administrator, is
carried in every backup, and appears in database dumps shared for support. And
reading one on the request path would mean a ``sudo()`` there, which this addon
does not do at all — see ``addons/odoo_atlas/tests/test_no_sudo.py``.
"""

import os

# The names of the variables, not the values. The bandit rule matches on the
# identifier and cannot tell the difference.
SERVICE_TOKEN_VAR = "ATLAS_SERVICE_TOKEN"  # noqa: S105
CONTEXT_SECRET_VAR = "ATLAS_CONTEXT_SECRET"  # noqa: S105


class SecretNotConfiguredError(Exception):
    """A required secret is absent from the environment.

    Raised rather than returning an empty value, so that a deployment which
    forgot to set one refuses every call instead of accepting every call.
    """


def _required(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        message = f"{name} is not set in the Odoo server's environment"
        raise SecretNotConfiguredError(message)
    return value


def service_token():
    """The shared secret the engine presents on every call."""
    return _required(SERVICE_TOKEN_VAR)


def context_secret():
    """The signing key for user context tokens. Never leaves Odoo."""
    return _required(CONTEXT_SECRET_VAR)


def missing():
    """Names of the secrets that are not configured, for the settings page."""
    return tuple(
        name
        for name in (SERVICE_TOKEN_VAR, CONTEXT_SECRET_VAR)
        if not (os.environ.get(name) or "").strip()
    )
