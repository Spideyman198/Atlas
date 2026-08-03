"""The addon's client for the Atlas engine, and where it is configured from.

Deliberately small and deliberately timid. Odoo has to survive the engine being
down: an outage degrades the assistant, never the ERP
(``docs/architecture/03-request-lifecycle.md``). Every call carries a hard
timeout and reports failure as a value rather than an exception that reaches a
view, and nothing here retries — a user waiting on a reply would rather be told
than kept waiting three times as long.

**Configuration comes from the environment, not from ``ir.config_parameter``.**
Two reasons. Reading a config parameter needs system rights, and the code that
will call the engine in M10 runs as whichever user asked the question — so a
parameter would force a ``sudo()`` onto the request path, which this addon does
not do. And the engine's address is deployment topology: it is fixed by the same
thing that decides both containers exist, so an administrator editing it in a
form would be editing the wrong file.

Answers stream. :func:`stream_answer` yields the engine's events as they arrive
rather than collecting them, because a grounded answer takes seconds and a user
watching a blank panel assumes it has hung.
"""

import json
import logging
import os
from http import HTTPStatus

import requests

ENGINE_URL_VAR = "ATLAS_ENGINE_URL"
ENGINE_TIMEOUT_VAR = "ATLAS_ENGINE_TIMEOUT"
# A variable name, not a secret; the bandit rule matches the identifier.
CONTEXT_TOKEN_TTL_VAR = "ATLAS_CONTEXT_TOKEN_TTL"  # noqa: S105

DEFAULT_BASE_URL = "http://atlas-api:8000"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONTEXT_TOKEN_TTL = 900

#: Liveness, not readiness. Readiness reports the engine's own view of
#: PostgreSQL, which is not the question an administrator is asking when they
#: press "Test Connection".
HEALTH_PATH = "/healthz"

#: Short on purpose: the settings page has to answer while somebody is looking
#: at it, and a slow engine is a failed connection test as far as they care.
HEALTH_TIMEOUT_SECONDS = 5

logger = logging.getLogger(__name__)


def base_url():
    """Where the engine is, without a trailing slash."""
    return (os.environ.get(ENGINE_URL_VAR) or DEFAULT_BASE_URL).strip().rstrip("/")


def request_timeout():
    """Seconds to wait for the engine before giving up on it."""
    return _positive_int(os.environ.get(ENGINE_TIMEOUT_VAR), DEFAULT_TIMEOUT_SECONDS)


def context_token_ttl():
    """How long a minted user context token stays valid."""
    return _positive_int(os.environ.get(CONTEXT_TOKEN_TTL_VAR), DEFAULT_CONTEXT_TOKEN_TTL)


def check_health():
    """Ask the engine whether it is alive.

    Returns:
        A ``(reachable, detail)`` pair. ``detail`` is one sentence meant to be
        shown to an administrator, so it names what failed without pasting a
        traceback into a dialog.
    """
    url = f"{base_url()}{HEALTH_PATH}"
    try:
        response = requests.get(url, timeout=HEALTH_TIMEOUT_SECONDS)
    except requests.Timeout:
        return False, f"No answer from {url} within {HEALTH_TIMEOUT_SECONDS} seconds."
    except requests.RequestException as exc:
        logger.warning("engine health check failed: %s", exc)
        return False, f"Could not reach {url}: {type(exc).__name__}."

    if response.status_code != HTTPStatus.OK:
        return False, f"{url} answered {response.status_code}."

    try:
        body = response.json()
    except ValueError:
        return False, f"{url} answered 200, but not with JSON."

    return True, f"Engine reachable at {base_url()} (version {body.get('version') or 'unknown'})."


def list_sources():
    """Ask the engine which sources it knows and which this Odoo can serve.

    The registry lives in the engine, not here. Duplicating it in the addon
    would give two lists to keep in step, and they would diverge the first time
    somebody added a source without thinking about Odoo.

    Returns:
        A ``(sources, detail)`` pair. ``sources`` is empty when the engine could
        not be reached, and ``detail`` says why.
    """
    url = f"{base_url()}/v1/ingest/sources"
    try:
        response = requests.get(url, timeout=HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        logger.warning("could not list ingest sources: %s", exc)
        return [], f"Could not reach {url}: {type(exc).__name__}."
    except ValueError:
        return [], f"{url} answered with something that is not JSON."

    sources = body.get("sources")
    if not isinstance(sources, list):
        return [], f"{url} answered without a source list."
    return [entry for entry in sources if isinstance(entry, dict)], ""


def request_sync(source_keys, kind="incremental", *, record_ids=None, deleted_ids=None):
    """Ask the engine to queue an ingestion run.

    Returns a ``(queued, detail)`` pair rather than raising. This is called from
    a cron and from a button, and neither should turn an engine outage into a
    traceback in somebody's face — the assistant degrades, the ERP does not
    (``docs/architecture/03-request-lifecycle.md``).
    """
    url = f"{base_url()}/v1/ingest/sync"
    payload = {"sources": list(source_keys), "kind": kind}
    if record_ids:
        payload["record_ids"] = list(record_ids)
    if deleted_ids:
        payload["deleted_ids"] = list(deleted_ids)

    try:
        response = requests.post(url, json=payload, timeout=request_timeout())
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        logger.warning("could not queue ingestion: %s", exc)
        return {}, f"Could not reach {url}: {type(exc).__name__}."
    except ValueError:
        return {}, f"{url} answered with something that is not JSON."

    queued = body.get("queued")
    if not isinstance(queued, dict):
        return {}, f"{url} answered without a job list."
    return queued, ""


def stream_answer(question, context_token_value, *, history=None, conversation_id=None):
    """Ask the engine a question and yield its events as they arrive.

    Args:
        question: What the user typed, verbatim.
        context_token_value: Minted by Odoo for the acting user. Travels on this
            call and nowhere else; the browser never holds one.
        history: Earlier turns as ``{"question", "answer"}`` dicts, oldest first.
        conversation_id: The ``atlas.conversation`` this belongs to.

    Yields:
        ``(kind, data)`` pairs, where ``kind`` is ``delta``, ``done`` or
        ``error``. A transport failure is yielded as an ``error`` event rather
        than raised: the caller is already streaming to a browser by then and
        has no status code left to fail with.
    """
    url = f"{base_url()}/v1/chat"
    payload = {
        "question": question,
        "context_token": context_token_value,
        "history": list(history or []),
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    try:
        response = requests.post(url, json=payload, timeout=request_timeout(), stream=True)
        response.raise_for_status()
        yield from _decode_events(response)
    except requests.Timeout:
        logger.warning("engine did not answer within %ss", request_timeout())
        yield "error", {"message": "The assistant took too long to answer. Try again."}
    except requests.RequestException as exc:
        logger.warning("could not reach the engine for an answer: %s", exc)
        yield "error", {"message": "The assistant is unavailable right now."}


def _decode_events(response):
    """Turn a server-sent event stream into ``(kind, data)`` pairs.

    A minimal reader on purpose. The engine emits one ``event:`` and one
    ``data:`` line per block, and pulling in a dependency to parse two line
    prefixes would be a poor trade for an addon that has to install cleanly next
    to whatever else is in the deployment.
    """
    kind = None
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.strip()
        if not line:
            kind = None
            continue
        if line.startswith(":"):
            # A comment. The engine sends one to open the stream.
            continue
        if line.startswith("event:"):
            kind = line[len("event:") :].strip()
            continue
        if line.startswith("data:") and kind:
            try:
                yield kind, json.loads(line[len("data:") :].strip())
            except ValueError:
                logger.warning("engine sent an event that was not JSON: %r", line[:120])


def _positive_int(raw, default):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
