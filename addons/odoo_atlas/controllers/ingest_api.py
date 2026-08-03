"""The endpoints the ingestion worker reads through.

A different door from ``atlas_api.py``, deliberately. Those endpoints answer for
one user and refuse everything that user may not see. These run as a dedicated
**integration user** and read everything that user is allowed to index, which is
broader than any single person's view — and precisely why the query-time check
cannot be skipped: the index is wider than any answer drawn from it (ADR-0006).

Keeping the two apart means neither can be mistaken for the other. There is no
context token here and no way to supply one, so nothing on this path can be
tricked into answering *as* somebody.

The integration user is named by ``ATLAS_INGEST_UID`` and must hold
``odoo_atlas.group_atlas_ingest``. It is a real Odoo user with real groups, so
what it may index is decided by Odoo's own access rules — there is no ``sudo()``
here either. Give it read access to what you want indexed and nothing else.
"""

import logging
import os

import werkzeug.exceptions
from odoo import http
from odoo.addons.odoo_atlas.controllers.atlas_api import _assert_service_token
from odoo.exceptions import AccessError
from odoo.http import request

logger = logging.getLogger(__name__)

INGEST_UID_VAR = "ATLAS_INGEST_UID"
INGEST_GROUP = "odoo_atlas.group_atlas_ingest"

#: Bounds on one read. Ingestion is batched, not streamed, and an unbounded
#: page is an unbounded query against somebody's production ERP.
MAX_PAGE_SIZE = 500
MAX_FIELDS = 128
MAX_CHILDREN = 500

#: Operators the engine is allowed to send. The domains come from Atlas's own
#: registry and the caller is already authenticated, so this is defence in
#: depth — but a domain is a query language, and an allow-list is cheap.
#: A domain clause is `(field, operator, value)`.
DOMAIN_CLAUSE_PARTS = 3

ALLOWED_OPERATORS = frozenset(
    {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "not ilike"}
)


class AtlasIngestController(http.Controller):
    """Read-only endpoints for the ingestion worker."""

    @http.route(
        "/atlas/api/ingest/sources",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def sources(self, **payload):
        """Report which models this Odoo can serve to the integration user.

        A source whose module is not installed, or that the integration user may
        not read, is reported here rather than discovered as a failure halfway
        through a sync.
        """
        _act_as_integration_user()
        wanted = payload.get("models")
        if not isinstance(wanted, list):
            raise werkzeug.exceptions.BadRequest("'models' must be a list of model names")

        available = {}
        for name in wanted:
            if not isinstance(name, str):
                continue
            available[name] = name in request.env and request.env[name].has_access("read")
        return {"sources": available}

    @http.route(
        "/atlas/api/ingest/records",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def records(self, **payload):
        """Read one page of a model, oldest change first.

        Ordering by ``write_date`` is what makes the watermark work: a page ends
        at a known point in time, and the next run starts there.
        """
        _act_as_integration_user()
        model = _model(payload.get("model"))
        limit = _bounded(payload.get("limit"), MAX_PAGE_SIZE)
        offset = max(int(payload.get("offset") or 0), 0)

        fields = _known_fields(model, payload.get("fields"))
        domain = _domain(payload)

        # `active_test=False` on purpose: an archived product is still a product
        # somebody will ask about, and dropping archived records would make the
        # corpus disagree with the ERP for no reason anybody could see.
        found = model.with_context(active_test=False).search(
            domain, order="write_date, id", limit=limit + 1, offset=offset
        )
        more = len(found) > limit
        found = found[:limit]

        rows = found.read(fields)
        _label_selections(model, fields, rows)
        _expand_children(payload.get("children"), rows)

        logger.info(
            "atlas ingest read",
            extra={
                "source_key": payload.get("source_key"),
                "model": model._name,
                "returned": len(rows),
                "offset": offset,
                "more": more,
            },
        )
        return {"records": rows, "more": more}

    @http.route(
        "/atlas/api/ingest/binary",
        type="json2",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def binary(self, **payload):
        """Return one attachment's bytes, base64 encoded.

        Only ever reached for a record whose checksum says its content changed —
        the worker skips unchanged files without asking for them, which is what
        keeps a corpus of large contracts cheap to re-sync.
        """
        _act_as_integration_user()
        record_id = payload.get("id")
        if not isinstance(record_id, int) or isinstance(record_id, bool):
            raise werkzeug.exceptions.BadRequest("'id' must be an integer")

        attachment = request.env["ir.attachment"].browse(record_id).exists()
        if not attachment:
            return {"content": ""}

        # `bin_size=False` or `read` returns the file's size as a string rather
        # than the file, which is the default and is not what is wanted here.
        try:
            data = attachment.with_context(bin_size=False).read(["datas"])
        except AccessError:
            logger.info("atlas ingest refused an attachment", extra={"res_id": record_id})
            return {"content": ""}

        raw = data[0].get("datas") if data else None
        if not raw:
            return {"content": ""}
        return {"content": raw.decode() if isinstance(raw, bytes) else str(raw)}


def _act_as_integration_user():
    """Authenticate the engine and switch to the integration user.

    Two separate checks. The service token proves the caller is the engine; the
    group proves the account it is about to act as was actually designated for
    this. Neither alone is enough: a token without a designated user would let
    ingestion run as whoever happened to be uid 1.
    """
    _assert_service_token()

    raw = (os.environ.get(INGEST_UID_VAR) or "").strip()
    try:
        uid = int(raw)
    except ValueError:
        logger.error(  # noqa: TRY400 - a missing setting is not an exception to trace
            "%s is not set to a user id; ingestion is refused", INGEST_UID_VAR
        )
        raise werkzeug.exceptions.Forbidden("ingestion is not configured") from None
    if uid <= 0:
        raise werkzeug.exceptions.Forbidden("ingestion is not configured")

    request.update_env(user=uid)
    user = request.env.user
    try:
        usable = bool(user.active) and user.has_group(INGEST_GROUP)
    except Exception as exc:
        logger.warning("%s=%s does not resolve to a usable user", INGEST_UID_VAR, uid)
        raise werkzeug.exceptions.Forbidden("ingestion is not configured") from exc

    if not usable:
        logger.error(
            "%s=%s is inactive or lacks %s; ingestion is refused",
            INGEST_UID_VAR,
            uid,
            INGEST_GROUP,
        )
        raise werkzeug.exceptions.Forbidden("ingestion is not configured")
    return uid


def _model(name):
    if not isinstance(name, str) or not name:
        raise werkzeug.exceptions.BadRequest("'model' must be a non-empty string")
    if name not in request.env:
        # Not an error the worker should retry: the module simply is not here.
        raise werkzeug.exceptions.NotFound(f"model {name!r} is not installed")
    model = request.env[name]
    if not model.has_access("read"):
        raise werkzeug.exceptions.NotFound(
            f"model {name!r} is not readable by the integration user"
        )
    return model


def _known_fields(model, requested):
    """Keep the fields this model actually has.

    Atlas's templates are written against a spread of Odoo editions and module
    combinations, and a field that exists in one is missing in another. Dropping
    the unknown ones means a template stays useful on a database that does not
    have every module, instead of failing the whole source over one column.
    """
    if not isinstance(requested, list) or not requested:
        raise werkzeug.exceptions.BadRequest("'fields' must be a non-empty list")
    if len(requested) > MAX_FIELDS:
        raise werkzeug.exceptions.BadRequest(f"at most {MAX_FIELDS} fields per call")

    available = set(model._fields)
    kept = [name for name in requested if isinstance(name, str) and name in available]
    missing = [name for name in requested if isinstance(name, str) and name not in available]
    if missing:
        logger.info(
            "atlas ingest skipped unknown fields",
            extra={"model": model._name, "skipped": ", ".join(sorted(missing))},
        )
    if "id" not in kept:
        kept.insert(0, "id")
    return kept


def _domain(payload):
    """Build the search domain from the request, ids or watermark first."""
    ids = payload.get("ids")
    if isinstance(ids, list) and ids:
        clean = [item for item in ids if isinstance(item, int) and not isinstance(item, bool)]
        return [("id", "in", clean)]

    domain = []
    for clause in payload.get("domain") or []:
        if not isinstance(clause, (list, tuple)) or len(clause) != DOMAIN_CLAUSE_PARTS:
            raise werkzeug.exceptions.BadRequest("each domain clause must have three parts")
        field, operator, value = clause
        if not isinstance(field, str) or operator not in ALLOWED_OPERATORS:
            raise werkzeug.exceptions.BadRequest(f"unsupported domain clause on {field!r}")
        domain.append((field, operator, list(value) if isinstance(value, list) else value))

    since = payload.get("since")
    if isinstance(since, str) and since:
        domain.append(("write_date", ">", since))
    return domain


def _label_selections(model, fields, rows):
    """Replace selection keys with their labels, in place.

    ``state`` reads as ``sale``; a person searching says "confirmed". The label
    is what gets embedded and what lexical search matches, so this is a retrieval
    quality fix, not cosmetics.
    """
    descriptions = model.fields_get(fields, ["type", "selection"])
    labels = {
        name: dict(description.get("selection") or [])
        for name, description in descriptions.items()
        if description.get("type") == "selection"
    }
    if not labels:
        return
    for row in rows:
        for name, mapping in labels.items():
            if row.get(name) in mapping:
                row[name] = mapping[row[name]]


def _expand_children(spec, rows):
    """Replace child ids with the child rows themselves, in one extra read.

    Order lines are most of what makes an order findable — the product names
    live there, not on the header. Reading them per parent would be a query per
    order; this is one for the page.
    """
    if not isinstance(spec, dict) or not rows:
        return
    field_name = spec.get("field")
    child_model_name = spec.get("model")
    child_fields = spec.get("fields")
    if not isinstance(field_name, str) or not isinstance(child_model_name, str):
        return
    if child_model_name not in request.env or not isinstance(child_fields, list):
        return

    child_model = request.env[child_model_name]
    if not child_model.has_access("read"):
        return
    kept = [name for name in child_fields if isinstance(name, str) and name in child_model._fields]
    limit = _bounded(spec.get("limit"), MAX_CHILDREN)

    wanted = []
    for row in rows:
        ids = row.get(field_name)
        if isinstance(ids, list):
            wanted.extend(ids[:limit])
    if not wanted:
        return

    try:
        child_rows = child_model.browse(wanted).exists().read(kept)
    except AccessError:
        logger.info("atlas ingest could not read children", extra={"model": child_model_name})
        return

    by_id = {row["id"]: row for row in child_rows}
    for row in rows:
        ids = row.get(field_name)
        if isinstance(ids, list):
            row[field_name] = [by_id[child_id] for child_id in ids[:limit] if child_id in by_id]


def _bounded(value, ceiling):
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return ceiling
    return max(min(requested, ceiling), 1)
