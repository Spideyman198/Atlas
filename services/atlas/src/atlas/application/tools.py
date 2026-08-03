"""Running the typed tools, and telling the model what happened.

The tools themselves live in Odoo, beside the models they read. What lives here
is the part that faces the language model: fetching the catalogue to put in a
prompt, executing a call, and — the interesting bit — turning a failure into
something the model can act on.

**A rejected tool call is a result, not an exception.** If the model asks to
filter on a field that does not exist, the useful thing is to hand back "no such
field; here are the ones there are" and let it try again. Raising instead aborts
a request that was one correction away from working. Current models are good at
that correction *when told exactly what was wrong*, which is why the addon's
rejection messages are written for a reader and are passed through verbatim.

What is *not* softened: an unreachable Odoo, or a refused context. Those are not
things a model can correct, and pretending otherwise would have it apologise for
an outage.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from atlas.domain.authorization import UserContext
from atlas.domain.chat import ToolCall, ToolDefinition, ToolResult
from atlas.domain.errors import AtlasError, AuthorizationError, DependencyUnavailableError
from atlas.domain.ports.odoo_gateway import OdooGateway

logger = logging.getLogger(__name__)

#: Ceiling on the JSON handed back to the model. The tools cap their own row
#: counts, but a wide row set can still be large, and a tool result that fills
#: the context window leaves no room for the answer it was fetched for.
MAX_RESULT_CHARS: Final = 12_000


class ToolBox:
    """The catalogue and the execution path, for one Odoo."""

    def __init__(self, gateway: OdooGateway) -> None:
        self._gateway = gateway

    async def catalog(self, context: UserContext) -> list[ToolDefinition]:
        """The tools this user can be offered.

        An unreachable Odoo yields an empty catalogue rather than an error: the
        assistant can still answer from retrieved documents, and offering a tool
        that cannot run would be worse than offering none.
        """
        try:
            return await self._gateway.tool_catalog(context)
        except AuthorizationError:
            raise
        except AtlasError as exc:
            logger.warning(
                "could not fetch the tool catalogue; continuing without tools",
                extra={"trace_id": context.trace_id, "error": exc.code},
            )
            return []

    async def execute(self, context: UserContext, call: ToolCall) -> ToolResult:
        """Run one tool call and render the outcome for the model.

        Raises:
            AuthorizationError: Odoo refused the context itself.
            DependencyUnavailableError: Odoo could not be reached. Neither is
                something the model can correct, so neither is softened into a
                result it will try to reason about.
        """
        try:
            payload = await self._gateway.execute_tool(context, call.name, call.arguments)
        except (AuthorizationError, DependencyUnavailableError):
            raise
        except AtlasError as exc:
            # The model chose these arguments and can choose better ones.
            logger.info(
                "tool call rejected",
                extra={"trace_id": context.trace_id, "tool": call.name, "error": exc.code},
            )
            return ToolResult(call_id=call.id, content=exc.message, is_error=True)

        content = _render(payload)
        logger.info(
            "tool call executed",
            extra={
                "trace_id": context.trace_id,
                "tool": call.name,
                "result_chars": len(content),
            },
        )
        return ToolResult(call_id=call.id, content=content)


def _render(payload: dict[str, object]) -> str:
    """Serialise a tool result, truncating rather than overflowing a prompt.

    Truncation is announced. A silently shortened list reads to a model as the
    complete answer, and it will say so.
    """
    rendered = json.dumps(payload, default=str, ensure_ascii=False)
    if len(rendered) <= MAX_RESULT_CHARS:
        return rendered
    return (
        rendered[:MAX_RESULT_CHARS]
        + f"… [truncated at {MAX_RESULT_CHARS} characters; ask for fewer rows or fields]"
    )
