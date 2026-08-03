"""The endpoints the engine calls back into.

This is the boundary that makes the whole design work. Retrieval happens in the
engine over an index that is deliberately broader than any one user's view; what
stops that index leaking is that nothing reaches a prompt until Odoo has
confirmed, *in this request and as this user*, that they may read it
(``docs/adr/0006-data-access-and-authorization.md``).

Three things follow, and none of them is optional:

**Every read runs as the acting user.** ``request.update_env(user=...)`` switches
the environment before any model is touched, and there is no ``sudo()`` anywhere
in this package — a test asserts it, so adding one fails the build.

**Two secrets, two jobs.** The service token proves the caller is the engine. The
context token proves which user the engine is acting for. The engine holds the
first and cannot mint the second, so it cannot promote itself to an arbitrary
user. Both come from the environment; see ``services/secrets.py``.

**Failures are indistinguishable.** A bad service token, an expired context
token and a user who lost their Atlas group all produce the same refusal. The
detail goes to the log, not to the caller, so a forger learns nothing about
which part of their attempt was nearly right.
"""

import logging
import time

import werkzeug.exceptions
from odoo import http, release
from odoo.addons.odoo_atlas.services import context_token, secrets, tools
from odoo.addons.odoo_atlas.services.tools.filters import FilterError
from odoo.exceptions import AccessError
from odoo.http import request
from odoo.tools import consteq

logger = logging.getLogger(__name__)

#: Bounds on one call. The engine over-fetches candidates by design (ADR-0006),
#: so these are generous, but an unbounded request is an unbounded query.
MAX_MODELS_PER_CALL = 32
MAX_IDS_PER_MODEL = 500
MAX_FIELDS_PER_CALL = 64

#: Sent by the engine as `Authorization: Bearer <service token>`.
_BEARER_PREFIX = "Bearer "


class AtlasApiController(http.Controller):
    """Service-to-service endpoints. Not for browsers, and not for users."""

    @http.route(
        "/atlas/api/status",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def status(self, **_payload):
        """Confirm the addon is installed and the service token is the right one.

        The engine's readiness probe calls this. It needs no context token
        because it acts for nobody: a probe that had to name a user would need
        the engine to hold one, which is the thing this design avoids.

        Deliberately says nothing about the database beyond its name — a probe
        that leaked configuration to an unauthenticated caller would be a poor
        trade for slightly better diagnostics.
        """
        _assert_service_token()
        return {
            "addon": "odoo_atlas",
            "version": release.version,
            "database": request.db,
            "tools": list(tools.names()),
        }

    @http.route(
        "/atlas/api/tool/catalog",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def tool_catalog(self, **payload):
        """The tools this database can serve the acting user, as JSON schemas.

        The engine hands these to the model verbatim. Keeping the definitions
        on this side means there is one description of each tool rather than
        two that can drift, and that a schema can never describe a tool this
        Odoo does not have — a database without `sale` simply offers fewer.
        """
        _authenticate(payload)
        return {"tools": tools.catalog_for(request.env)}

    @http.route(
        "/atlas/api/authorize",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def authorize(self, **payload):
        """Return, per model, which of the given ids the acting user may read.

        This is stage 2 of retrieval. The engine sends every candidate it found;
        Odoo answers with the subset that survives model access and record
        rules. Ids it does not return are denied, and the engine has no way to
        ask why.
        """
        started = time.perf_counter()
        acting = _authenticate(payload)
        trace_id = _trace_id(payload)
        requested = _requested_records(payload)

        granted = {}
        access_log = request.env["atlas.access.log"]
        for model, ids in requested.items():
            model_started = time.perf_counter()
            allowed = _readable_ids(model, ids)
            granted[model] = allowed
            access_log._record_authorization(
                model,
                ids,
                allowed,
                trace_id=trace_id,
                duration_ms=_elapsed_ms(model_started),
            )

        logger.info(
            "atlas authorize",
            extra={
                "atlas_uid": acting,
                "trace_id": trace_id,
                "models": len(requested),
                "requested": sum(len(ids) for ids in requested.values()),
                "granted": sum(len(ids) for ids in granted.values()),
                "duration_ms": _elapsed_ms(started),
            },
        )
        return {"granted": granted}

    @http.route(
        "/atlas/api/records",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def records(self, **payload):
        """Read named fields of records the acting user may see.

        Used to put a name and a few facts beside a citation. Records the user
        cannot read are absent from the answer rather than reported as denied,
        for the same reason as :meth:`authorize`.
        """
        started = time.perf_counter()
        acting = _authenticate(payload)
        trace_id = _trace_id(payload)
        model = _model_name(payload.get("model"))
        ids = _record_ids(payload.get("ids"))
        fields = _field_names(payload.get("fields"))

        allowed = _readable_ids(model, ids)
        try:
            rows = request.env[model].browse(sorted(allowed)).read(fields)
        except AccessError:
            # A field the user may not read. Refusing the call is right: the
            # engine asked for something it should not have, and answering with
            # a partial row would hide that.
            logger.info("atlas records refused a field", extra={"model": model})
            rows = []

        request.env["atlas.access.log"]._record(
            "records",
            trace_id=trace_id,
            duration_ms=_elapsed_ms(started),
            res_model=model,
            requested_count=len(ids),
            granted_count=len(rows),
            denied_count=len(ids) - len(rows),
        )
        logger.info(
            "atlas records",
            extra={
                "atlas_uid": acting,
                "trace_id": trace_id,
                "model": model,
                "requested": len(ids),
                "granted": len(rows),
                "duration_ms": _elapsed_ms(started),
            },
        )
        return {"records": rows}

    @http.route(
        "/atlas/api/tool/execute",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def tool_execute(self, **payload):
        """Run one typed tool as the acting user.

        The tool set itself lands in M9. What exists here is the boundary the
        tools will run behind — the same authentication, the same acting-user
        environment and the same audit row as the other two endpoints — so that
        adding a tool is adding an entry to a registry, not another way into
        Odoo.
        """
        started = time.perf_counter()
        acting = _authenticate(payload)
        trace_id = _trace_id(payload)
        name = payload.get("tool")
        if not isinstance(name, str) or not name:
            raise werkzeug.exceptions.BadRequest("'tool' must be a non-empty string")

        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise werkzeug.exceptions.BadRequest("'arguments' must be an object")

        tool = tools.get(name)
        if tool is None:
            logger.info("atlas unknown tool", extra={"atlas_uid": acting, "tool": name})
            raise werkzeug.exceptions.NotFound(f"unknown tool {name!r}")

        try:
            result = tool.run(request.env, arguments)
        except FilterError as exc:
            # The model chose these arguments, and is good at correcting itself
            # when told exactly what was wrong. A 400 with the reason is worth
            # far more here than a generic refusal.
            logger.info("atlas tool rejected arguments", extra={"tool": name})
            raise werkzeug.exceptions.BadRequest(str(exc)) from exc
        except AccessError as exc:
            # Odoo refused a record the arguments reached. Not an error to
            # explain away: the acting user cannot see it, and that is the
            # answer (ADR-0006).
            logger.info("atlas tool refused by access rules", extra={"tool": name})
            raise werkzeug.exceptions.Forbidden("not permitted") from exc
        request.env["atlas.access.log"]._record(
            "tool",
            trace_id=trace_id,
            duration_ms=_elapsed_ms(started),
            tool_name=name,
        )
        return {"result": result}


def _authenticate(payload):
    """Establish who this request acts as, or refuse it.

    Order matters. The service token is checked first, so an unauthenticated
    caller cannot make Odoo do work — not even signature verification — by
    sending rubbish. Only then is the environment switched to the acting user,
    and only then is that user re-checked against the database.

    Returns:
        The acting user's id, for logging.
    """
    _assert_service_token()

    try:
        claim = context_token.verify(payload.get("context_token"))
    except secrets.SecretNotConfiguredError as exc:
        logger.exception("atlas context secret is not configured")
        raise werkzeug.exceptions.Forbidden("invalid context") from exc
    except context_token.ContextTokenError as exc:
        logger.info("atlas context token refused: %s", exc)
        raise werkzeug.exceptions.Forbidden("invalid context") from exc

    # From here on, every model access in this request is the named user's.
    request.update_env(user=claim.uid)
    try:
        companies = context_token.assert_usable(request.env, claim)
    except context_token.ContextTokenError as exc:
        logger.info("atlas context token refused: %s", exc)
        raise werkzeug.exceptions.Forbidden("invalid context") from exc

    request.update_env(context={**request.env.context, "allowed_company_ids": list(companies)})
    return claim.uid


def _assert_service_token():
    """Confirm the caller is the engine.

    An unconfigured secret refuses every call. The alternative — treating
    "no token set" as "no check needed" — turns a forgotten environment variable
    into an open door onto the whole ERP.
    """
    try:
        expected = secrets.service_token()
    except secrets.SecretNotConfiguredError as exc:
        logger.exception("atlas service token is not configured")
        raise werkzeug.exceptions.Unauthorized("not authorised") from exc

    header = request.httprequest.headers.get("Authorization") or ""
    presented = header[len(_BEARER_PREFIX) :] if header.startswith(_BEARER_PREFIX) else ""
    if not presented or not consteq(presented, expected):
        logger.warning(
            "atlas service token refused",
            extra={"remote_addr": request.httprequest.remote_addr},
        )
        raise werkzeug.exceptions.Unauthorized("not authorised")


def _readable_ids(model, ids):
    """The subset of ``ids`` the acting user may read, as a sorted list.

    ``search`` is what applies record rules, which is why ADR-0006 specifies it
    rather than a cheaper existence check. ``active_test=False`` is deliberate:
    an archived record is one the user may still open, and treating archiving as
    a denial would quietly drop citations to closed orders.
    """
    if not ids or model not in request.env:
        return []
    try:
        found = request.env[model].with_context(active_test=False).search([("id", "in", list(ids))])
    except AccessError:
        # No model-level read at all. Every id is denied, and that is an answer,
        # not an error: the engine asked about a model this user cannot touch.
        return []
    return sorted(found.ids)


def _requested_records(payload):
    """Validate and normalise the ``records`` mapping of an authorize call."""
    records = payload.get("records")
    if not isinstance(records, dict) or not records:
        raise werkzeug.exceptions.BadRequest("'records' must be a non-empty object")
    if len(records) > MAX_MODELS_PER_CALL:
        raise werkzeug.exceptions.BadRequest(f"at most {MAX_MODELS_PER_CALL} models per call")
    return {_model_name(model): _record_ids(ids) for model, ids in records.items()}


def _model_name(model):
    if not isinstance(model, str) or not model:
        raise werkzeug.exceptions.BadRequest("model names must be non-empty strings")
    return model


def _record_ids(ids):
    if not isinstance(ids, list):
        raise werkzeug.exceptions.BadRequest("record ids must be a list")
    if len(ids) > MAX_IDS_PER_MODEL:
        raise werkzeug.exceptions.BadRequest(f"at most {MAX_IDS_PER_MODEL} ids per model")
    if not all(isinstance(record_id, int) and not isinstance(record_id, bool) for record_id in ids):
        raise werkzeug.exceptions.BadRequest("record ids must be integers")
    return ids


def _field_names(fields):
    """Validate the requested field list, defaulting to the record's name only.

    Defaulting to everything would ship binary columns and every private note a
    model happens to carry into the engine's memory, for a caller that asked for
    none of it.
    """
    if fields is None:
        return ["display_name"]
    if not isinstance(fields, list) or not all(isinstance(name, str) and name for name in fields):
        raise werkzeug.exceptions.BadRequest("'fields' must be a list of field names")
    if len(fields) > MAX_FIELDS_PER_CALL:
        raise werkzeug.exceptions.BadRequest(f"at most {MAX_FIELDS_PER_CALL} fields per call")
    return fields or ["display_name"]


def _trace_id(payload):
    trace_id = payload.get("trace_id")
    return trace_id if isinstance(trace_id, str) and trace_id else None


def _elapsed_ms(started):
    return int((time.perf_counter() - started) * 1000)
