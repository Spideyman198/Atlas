"""Short-lived signed tokens naming the user a request acts as.

The engine never holds anyone's credentials. It holds a token Odoo minted, which
says only "this is user 7, in companies 1 and 3, until 12:05". That is the whole
point of the design in ``docs/adr/0006-data-access-and-authorization.md``: the
service cannot impersonate an arbitrary user, it can only present a token Odoo
itself issued.

Verification re-reads the user on every use rather than trusting the payload, so
deactivating an account or removing its Atlas access takes effect on the next
call instead of whenever the token happens to expire.

The token is not encrypted and is not meant to be. It carries no secret — a user
id and a company list — and its integrity, not its confidentiality, is what
matters.
"""

import base64
import binascii
import dataclasses
import json
import time

from odoo.addons.odoo_atlas.services import secrets
from odoo.exceptions import AccessError, MissingError
from odoo.tools import consteq, hmac

#: Mixed into every signature. A token minted for this purpose cannot be
#: replayed against another use of the same secret. Not itself a secret — the
#: bandit rule matches the identifier, not the value.
TOKEN_SCOPE = "odoo_atlas.context_token"  # noqa: S105

#: Bumped when the payload shape changes, so an old token is rejected outright
#: rather than misread under the new shape.
TOKEN_VERSION = "v1"  # noqa: S105

DEFAULT_TTL_SECONDS = 900

#: Re-checked on every call. Losing this group revokes engine access at once.
ATLAS_USER_GROUP = "odoo_atlas.group_atlas_user"


class ContextTokenError(Exception):
    """The token is absent, malformed, expired, or fails to verify.

    One exception type for every failure on purpose. Telling a caller *why*
    their token was refused tells an attacker which part of a forgery was
    nearly right; the specifics go to the log instead.
    """


@dataclasses.dataclass(frozen=True)
class Claim:
    """What a verified token asserts. Not yet checked against the database."""

    uid: int
    company_ids: tuple[int, ...]
    expires_at: int


def mint(env, *, ttl_seconds=DEFAULT_TTL_SECONDS):
    """Mint a token naming ``env.user`` and the companies they have active.

    Called from Odoo's side of the boundary only. Nothing exposes this over RPC:
    a token is minted when the addon is about to call the engine, and travels
    only on that call.
    """
    payload = {
        "uid": env.user.id,
        "cid": sorted(env.companies.ids),
        "exp": int(time.time()) + max(int(ttl_seconds), 1),
    }
    encoded = _encode(payload)
    return f"{TOKEN_VERSION}.{encoded}.{_sign(encoded)}"


def verify(token):
    """Check a token's signature and expiry and return what it claims.

    This settles whether Odoo minted the token and whether it is still valid.
    Whether the user it names may actually do anything is
    :func:`assert_usable`, which needs a database.

    Raises:
        ContextTokenError: The token is malformed, unsigned by us, or expired.
        secrets.SecretNotConfiguredError: The signing key is absent, in which case no
            token can be trusted and every call must be refused.
    """
    version, _, remainder = (token or "").partition(".")
    encoded, _, signature = remainder.partition(".")
    if version != TOKEN_VERSION or not encoded or not signature:
        raise ContextTokenError("malformed context token")

    if not consteq(_sign(encoded), signature):
        raise ContextTokenError("context token signature does not verify")

    # Decoded only after the signature check, so malformed input from an
    # unauthenticated caller never reaches the JSON parser.
    try:
        payload = json.loads(base64.urlsafe_b64decode(_pad(encoded)))
        claim = Claim(
            uid=int(payload["uid"]),
            company_ids=tuple(int(company) for company in payload["cid"]),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ContextTokenError("unreadable context token payload") from exc

    if claim.expires_at <= time.time():
        raise ContextTokenError("context token has expired")
    if not claim.uid or not claim.company_ids:
        raise ContextTokenError("context token names no user or no company")
    return claim


def assert_usable(env, claim):
    """Confirm the user a token names may still use Atlas, and narrow companies.

    Runs in an environment already switched to that user, so every read here is
    a user reading their own record — no ``sudo()``, and no reliance on the
    payload being current.

    Returns:
        The companies from the token that the user still has, which becomes
        ``allowed_company_ids`` for the request. Record rules read it, so a
        token cannot widen its own scope after the fact.

    Raises:
        ContextTokenError: The user is gone, archived, or no longer an Atlas user.
    """
    user = env.user
    try:
        active = user.active
        is_atlas_user = user.has_group(ATLAS_USER_GROUP)
        current = set(user.company_ids.ids)
    except (AccessError, MissingError) as exc:
        # The user was deleted between minting and use, or cannot read even
        # their own record. Either way there is nobody to act as.
        raise ContextTokenError("context token names an unusable user") from exc

    if not active:
        raise ContextTokenError("context token names an inactive user")
    if not is_atlas_user:
        raise ContextTokenError("context token names a user without Atlas access")

    allowed = tuple(company for company in claim.company_ids if company in current)
    if not allowed:
        raise ContextTokenError("context token names no company the user still has")
    return allowed


def _sign(encoded):
    """HMAC-SHA256 over the payload segment, keyed by the Odoo-only secret.

    ``secret`` is passed explicitly so that :func:`odoo.tools.hmac` never reads
    ``database.secret`` from ``ir.config_parameter`` — which would need a
    ``sudo()`` on the request path.
    """
    return hmac(None, TOKEN_SCOPE, encoded, secret=secrets.context_secret())


def _encode(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pad(encoded):
    return encoded + "=" * (-len(encoded) % 4)
