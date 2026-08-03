"""Who a request acts as.

The engine holds no credentials for anybody. What it holds is a token Odoo
minted, which the engine cannot read and cannot forge, and which says only "this
is user 7, until 12:05". Every call the engine makes back into Odoo carries one,
and Odoo decides what that user may see
(:doc:`ADR-0006 </adr/0006-data-access-and-authorization>`).

That is why :class:`UserContext` has no user id on it. The engine has no
business knowing which user it is acting for, and giving it a field to put one
in would invite code that decides things from it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserContext:
    """An opaque, short-lived assertion of who a request acts as.

    Attributes:
        token: Minted and signed by Odoo. Opaque here on purpose — the engine
            passes it back and never inspects it.
        trace_id: Correlates this request across the engine's logs, Odoo's
            access log, and the message the answer is stored on.
    """

    token: str
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.token:
            message = "a user context needs a token; an empty one authorises nothing"
            raise ValueError(message)

    def __repr__(self) -> str:
        """Render without the token.

        A context ends up in log records and exception context. The token is a
        bearer credential for the duration of its life, and the surest way to
        keep it out of a log file is to make printing one impossible.
        """
        return f"UserContext(trace_id={self.trace_id!r})"
