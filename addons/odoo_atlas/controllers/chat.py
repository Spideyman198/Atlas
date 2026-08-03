"""The endpoint the chat UI talks to.

The browser never holds a context token. It is a bearer credential for the
duration of its life (ADR-0006), and handing one to a page would let anything
running in that page act as the user against the engine directly. So the browser
asks Odoo, Odoo mints the token, calls the engine, and relays what comes back.

    browser ──POST /atlas/chat/ask──▶ Odoo ──POST /v1/chat──▶ engine
              session cookie              mints the token

**The response streams, and that costs a second cursor.** Odoo commits the
request transaction when the handler returns, which is before werkzeug iterates
the response body. Anything the generator writes therefore needs a cursor of its
own — the request's is closed by then. Everything the generator needs is
captured before the first yield, because ``request`` is not usable inside it
either.

The question is persisted before the stream opens, so a reload shows what was
asked. The answer is persisted when the stream ends. A connection dropped in
between loses the answer and keeps the question, which is the right way round.
"""

import json
import logging

from odoo import api, http
from odoo.addons.odoo_atlas.services import engine
from odoo.exceptions import AccessError
from odoo.http import request
from odoo.modules.registry import Registry

logger = logging.getLogger(__name__)

#: Matches the engine's own limit. Rejecting here as well means a paste of a
#: whole document gets an answer in the UI rather than a stack trace.
MAX_QUESTION_LENGTH = 4000

#: Turns sent to the engine as history. The engine summarises what does not fit,
#: but there is no reason to put a hundred turns on the wire to have it discard
#: most of them.
MAX_HISTORY_TURNS = 20


class AtlasChatController(http.Controller):
    """What the chat UI calls. Sessions only — the engine uses its own routes."""

    @http.route("/atlas/chat/ask", type="http", auth="user", methods=["POST"])
    def ask(self, payload="", **_kwargs):
        """Answer a question, streaming the result back as it arrives.

        Args:
            payload: JSON with ``question`` and an optional ``conversation_id``.
                Sent as a form field rather than a JSON body so that Odoo's CSRF
                check, which reads form parameters, still applies.
        """
        try:
            parsed = json.loads(payload or "{}")
        except ValueError:
            return _error_response("That request could not be read.")

        question = (parsed.get("question") or "").strip()
        if not question:
            return _error_response("Ask a question first.")
        if len(question) > MAX_QUESTION_LENGTH:
            return _error_response(
                f"That question is too long. Keep it under {MAX_QUESTION_LENGTH} characters."
            )

        try:
            conversation = _resolve_conversation(parsed.get("conversation_id"))
        except AccessError:
            # Record rules already stopped the read. Saying which conversation
            # exists would answer a question the caller should not be asking.
            return _error_response("That conversation is not available.")

        conversation.env["atlas.message"].create(
            {
                "conversation_id": conversation.id,
                "role": "user",
                "content": question,
                "status": "done",
            }
        )

        # Captured now: none of this is reachable once the generator runs.
        state = {
            "database": request.db,
            "uid": request.env.uid,
            "conversation_id": conversation.id,
            "question": question,
            "history": _history(conversation),
            "token": conversation._atlas_context_token(),
        }

        return http.Response(
            _stream(state),
            mimetype="text/event-stream",
            headers=[
                ("Cache-Control", "no-cache"),
                # Without this nginx buffers the whole stream and delivers it as
                # one block, which defeats the point of streaming it.
                ("X-Accel-Buffering", "no"),
            ],
        )


def _stream(state):
    """Relay the engine's events, then record what was said.

    Every event is passed straight through, including the conversation id, which
    the client needs on the first turn of a new conversation.
    """
    yield _event("open", {"conversation_id": state["conversation_id"]})

    text = []
    final = None
    failure = None

    for kind, data in engine.stream_answer(
        state["question"],
        state["token"],
        history=state["history"],
        conversation_id=state["conversation_id"],
    ):
        if kind == "delta":
            text.append(data.get("text") or "")
        elif kind == "done":
            final = data
        elif kind == "error":
            failure = data.get("message") or "The assistant could not answer."
        yield _event(kind, data)

    try:
        _persist(state, "".join(text), final, failure)
    except Exception:
        # The user has already read the answer; losing the transcript is worth a
        # log line, not an error event that contradicts what is on their screen.
        logger.exception("could not store the answer for conversation %s", state["conversation_id"])


def _persist(state, text, final, failure):
    """Write the answer on a cursor of this generator's own.

    The request's cursor was committed and closed when the handler returned, so
    this opens a new one and commits it explicitly.
    """
    with Registry(state["database"]).cursor() as cr:
        env = api.Environment(cr, state["uid"], {})
        payload = final or {}
        message = env["atlas.message"].create(
            {
                "conversation_id": state["conversation_id"],
                "role": "assistant",
                "content": text or payload.get("text") or failure or "",
                "status": "error" if failure else "done",
                "tool_calls": payload.get("tools_called") or False,
                "trace_id": payload.get("trace_id") or False,
                "prompt_tokens": (payload.get("usage") or {}).get("input_tokens", 0),
                "completion_tokens": (payload.get("usage") or {}).get("output_tokens", 0),
                # Priced by the engine, which is where the price table lives.
                # Odoo storing a copy of that table would give two of them to
                # keep in step, and they would diverge on the first repricing.
                "cost": payload.get("cost_usd") or 0.0,
                "model_used": payload.get("model") or False,
            }
        )
        citations = [
            {
                "message_id": message.id,
                "sequence": citation.get("sequence") or index,
                "res_model": citation.get("res_model") or "",
                "res_id": citation.get("res_id") or 0,
                "record_name": citation.get("record_name") or "",
                "snippet": citation.get("snippet") or "",
            }
            for index, citation in enumerate(payload.get("citations") or [], start=1)
            # A citation without a target cannot be opened, and a chip that goes
            # nowhere is worse than one that is not there.
            if citation.get("res_model") and citation.get("res_id")
        ]
        if citations:
            env["atlas.message.citation"].create(citations)
        cr.commit()


def _resolve_conversation(conversation_id):
    """Return the conversation to answer in, creating one if needed.

    Raises:
        AccessError: The conversation is not this user's. Record rules raise it;
            this only names the case.
    """
    conversations = request.env["atlas.conversation"]
    if conversation_id:
        conversation = conversations.browse(int(conversation_id))
        conversation.check_access("read")
        if conversation.user_id != request.env.user:
            message = "a conversation belongs to the user who started it"
            raise AccessError(message)
        return conversation
    return conversations.create({})


def _history(conversation):
    """Earlier turns as the engine wants them: oldest first, paired.

    Only completed exchanges. A question whose answer failed is not history a
    later answer should be built on.
    """
    turns = []
    pending = None
    for message in conversation.message_ids.sorted("id"):
        if message.role == "user":
            pending = message.content or ""
        elif message.role == "assistant" and pending is not None and message.status == "done":
            turns.append({"question": pending, "answer": message.content or ""})
            pending = None
    return turns[-MAX_HISTORY_TURNS:]


def _event(kind, data):
    """One server-sent event.

    JSON on a single line: a newline inside the payload would end the event
    early, and answers contain plenty of them.
    """
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _error_response(message):
    """A refusal shaped like the stream, so the client has one thing to parse."""
    return http.Response(
        _event("error", {"message": message}),
        mimetype="text/event-stream",
        headers=[("Cache-Control", "no-cache")],
    )
