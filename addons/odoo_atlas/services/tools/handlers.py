"""The five tools.

Each one compiles structured arguments into an ORM read and returns rows. None
of them writes anything, and none of them uses ``sudo()`` — so what a tool can
see is what the person who asked could see (ADR-0006).

Two of them are deliberately *not* general. ``overdue_invoices`` and
``stock_levels`` could both be expressed as ``find_records`` with the right
filters, and asking a model to get those filters right every time is how you get
an answer about the wrong ``move_type``. A fixed domain shape for the questions
people actually ask is worth more than one more parameter on a general tool.
"""

import datetime

from odoo import fields as odoo_fields
from odoo.addons.odoo_atlas.services.tools import catalog, filters
from odoo.addons.odoo_atlas.services.tools.base import Tool
from odoo.addons.odoo_atlas.services.tools.filters import FilterError

#: Invoices customers owe us. Vendor bills are a different question and are not
#: what anybody means by "overdue invoices" without saying so.
CUSTOMER_INVOICE_TYPES = ("out_invoice", "out_refund")

#: How many related records a customer summary carries. Small on purpose: this
#: is a summary, and a hundred order lines is not one.
SUMMARY_ROWS = 5


def _jsonable(value):
    """Render one ORM value as the JSON type the model will actually see.

    ``search_read`` returns dates as ``date`` objects and many2one fields as
    ``(id, name)`` tuples. Odoo's HTTP layer converts both on the way out, which
    means a tool called in process returns a different shape from the same tool
    called over the wire. Doing it here instead makes the two identical, so a
    test that reads the result is reading what ships.

    The ``(id, name)`` pair is kept rather than flattened to a name: both halves
    are valid filter values, and the id is what a follow-up call needs.
    """
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _rows(records):
    """Apply :func:`_jsonable` across ``search_read`` output."""
    return [{name: _jsonable(value) for name, value in row.items()} for row in records]


def _spec_or_fail(model_name):
    spec = catalog.spec_for(model_name)
    if spec is None:
        allowed = ", ".join(sorted(catalog.MODELS))
        message = f"{model_name!r} is not available to Atlas. Allowed models: {allowed}"
        raise FilterError(message)
    return spec


def _model_or_fail(env, spec):
    if spec.model not in env:
        message = f"{spec.model} is not installed on this database"
        raise FilterError(message)
    model = env[spec.model]
    if not model.has_access("read"):
        # Not an error the model should retry or explain away: the acting user
        # simply cannot see this, and saying so plainly is the honest answer.
        message = f"you do not have access to {spec.label.lower()}"
        raise FilterError(message)
    return model


# --- find_records -----------------------------------------------------------


def find_records(env, arguments):
    """Read records matching structured filters."""
    spec = _spec_or_fail(arguments.get("model"))
    model = _model_or_fail(env, spec)

    domain = filters.compile_filters(model, arguments.get("filters"), spec)
    fields = filters.check_fields(arguments.get("fields"), spec)
    limit = filters.check_limit(arguments.get("limit"))

    total = model.search_count(domain)
    rows = _rows(model.search_read(domain, fields, limit=limit, order=spec.order))
    return {
        "model": spec.model,
        "label": spec.label,
        "matched": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "rows": rows,
    }


FIND_RECORDS = Tool(
    name="find_records",
    description=(
        "Look up individual records in Odoo — a specific sales order, the "
        "contacts in a country, products in a category, invoices over an "
        "amount. Use this whenever the question is about particular records "
        "rather than a total, and whenever you need a record's current live "
        "values rather than what a document said at some point in the past."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["model"],
        "properties": {
            "model": {
                "type": "string",
                "enum": sorted(catalog.MODELS),
                "description": "Which Odoo model to read.",
            },
            "filters": {
                "type": "array",
                "maxItems": catalog.MAX_FILTERS,
                "description": "Conditions, combined with AND. Omit to match everything.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "operator", "value"],
                    "properties": {
                        "field": {"type": "string"},
                        "operator": {"type": "string", "enum": sorted(catalog.ALLOWED_OPERATORS)},
                        "value": {
                            "description": "A scalar, or a list for 'in' and 'not in'.",
                        },
                    },
                },
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fields to return. Omit for a sensible default set.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": catalog.MAX_ROWS,
                "description": f"Rows to return, at most {catalog.MAX_ROWS}.",
            },
        },
    },
    handler=find_records,
)


# --- aggregate --------------------------------------------------------------


def aggregate(env, arguments):
    """Group and total records — the tool for "how much" and "how many"."""
    spec = _spec_or_fail(arguments.get("model"))
    model = _model_or_fail(env, spec)

    group_by = _check_groups(arguments.get("group_by"), spec)
    measures = _check_measures(arguments.get("measures"), spec)
    domain = filters.compile_filters(model, arguments.get("filters"), spec)
    limit = filters.check_limit(arguments.get("limit"))

    # `__count` is always included: "how many" is at least as common a question
    # as "how much", and a caller should not have to know to ask for it.
    aggregates = ["__count", *[f"{measure}:sum" for measure in measures]]
    rows = model._read_group(domain, groupby=group_by, aggregates=aggregates, limit=limit)

    return {
        "model": spec.model,
        "label": spec.label,
        "group_by": group_by,
        "measures": measures,
        "returned": len(rows),
        "rows": [_group_row(group_by, aggregates, row) for row in rows],
    }


def _group_row(group_by, aggregates, row):
    """Flatten one ``_read_group`` tuple into an object with names on it.

    ``_read_group`` returns positional tuples, and a recordset for a many2one
    group. Neither survives JSON, and neither is legible to a model.
    """
    flattened = {}
    for index, name in enumerate(group_by):
        value = row[index]
        flattened[name] = value.display_name if hasattr(value, "display_name") else _jsonable(value)
    for offset, name in enumerate(aggregates):
        flattened[name.replace(":sum", "")] = _jsonable(row[len(group_by) + offset])
    return flattened


def _check_groups(group_by, spec):
    if not isinstance(group_by, list) or not group_by:
        allowed = ", ".join(sorted(spec.groupable))
        message = f"'group_by' must name at least one field. Allowed: {allowed}"
        raise FilterError(message)
    if len(group_by) > catalog.MAX_GROUPS:
        message = f"at most {catalog.MAX_GROUPS} grouping levels, got {len(group_by)}"
        raise FilterError(message)

    unknown = [name for name in group_by if _group_field(name) not in spec.groupable]
    if unknown:
        allowed = ", ".join(sorted(spec.groupable))
        message = (
            f"cannot group {spec.model} by {', '.join(map(repr, unknown))}. Allowed: {allowed}"
        )
        raise FilterError(message)
    return group_by


def _group_field(name):
    """Strip Odoo's date granularity suffix, as in ``date_order:month``."""
    return name.split(":", 1)[0] if isinstance(name, str) else name


def _check_measures(measures, spec):
    if measures is None:
        return []
    if not isinstance(measures, list):
        message = "'measures' must be a list of numeric field names"
        raise FilterError(message)
    unknown = [name for name in measures if name not in spec.measures]
    if unknown:
        allowed = ", ".join(sorted(spec.measures)) or "none on this model"
        message = (
            f"cannot total {', '.join(map(repr, unknown))} on {spec.model}. Allowed: {allowed}"
        )
        raise FilterError(message)
    return measures


AGGREGATE = Tool(
    name="aggregate",
    description=(
        "Total or count records, grouped by one or more fields — revenue by "
        "customer, orders by salesperson, stock by location, invoices by "
        "status. Use this for any question about how much, how many, which is "
        "the biggest, or a trend over time. Do not try to add up individual "
        "records yourself: this reads the whole matching set, not just the "
        "first page of it."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["model", "group_by"],
        "properties": {
            "model": {"type": "string", "enum": sorted(catalog.MODELS)},
            "group_by": {
                "type": "array",
                "minItems": 1,
                "maxItems": catalog.MAX_GROUPS,
                "items": {"type": "string"},
                "description": (
                    "Fields to group by. A date field may carry a granularity, "
                    "as in 'date_order:month'."
                ),
            },
            "measures": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Numeric fields to sum. A count is always returned.",
            },
            "filters": FIND_RECORDS.parameters["properties"]["filters"],
            "limit": {"type": "integer", "minimum": 1, "maximum": catalog.MAX_ROWS},
        },
    },
    handler=aggregate,
)


# --- stock_levels -----------------------------------------------------------


def stock_levels(env, arguments):
    """How much of a product is on hand, and where."""
    spec = _spec_or_fail("stock.quant")
    quant = _model_or_fail(env, spec)

    domain = []
    product = arguments.get("product")
    if product:
        domain.append(("product_id", "ilike", str(product)[:200]))
    location = arguments.get("location")
    if location:
        domain.append(("location_id", "ilike", str(location)[:200]))
    # Internal locations only: customer, supplier and virtual locations are
    # accounting entries, not things on a shelf, and including them makes every
    # total wrong in a way nobody notices.
    if "location_id" in quant._fields:
        domain.append(("location_id.usage", "=", "internal"))

    rows = quant._read_group(
        domain,
        groupby=["product_id"],
        aggregates=["__count", "quantity:sum", "reserved_quantity:sum"],
        limit=filters.check_limit(arguments.get("limit")),
        order="quantity:sum desc",
    )

    return {
        "model": "stock.quant",
        "label": "Stock on hand",
        "returned": len(rows),
        "rows": [
            {
                "product": product_id.display_name if product_id else None,
                "locations": count,
                "on_hand": quantity,
                "reserved": reserved,
                "available": (quantity or 0.0) - (reserved or 0.0),
            }
            for product_id, count, quantity, reserved in rows
        ],
    }


STOCK_LEVELS = Tool(
    name="stock_levels",
    description=(
        "How much of a product is physically in stock, totalled across internal "
        "locations, with how much of it is already reserved. Use this for any "
        "question about availability, what is running low, or whether an order "
        "can be fulfilled. Counts only real warehouse locations."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "product": {
                "type": "string",
                "description": "Product name or code to match. Omit for everything.",
            },
            "location": {
                "type": "string",
                "description": "Warehouse or location name to match. Omit for all of them.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": catalog.MAX_ROWS},
        },
    },
    handler=stock_levels,
    models=("stock.quant",),
)


# --- overdue_invoices -------------------------------------------------------


def overdue_invoices(env, arguments):
    """Customer invoices past their due date and not fully paid."""
    spec = _spec_or_fail("account.move")
    move = _model_or_fail(env, spec)

    as_of = arguments.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        message = "'as_of' must be a date in YYYY-MM-DD form"
        raise FilterError(message)
    cutoff = as_of or _today(env)

    domain = [
        ("move_type", "in", list(CUSTOMER_INVOICE_TYPES)),
        ("state", "=", "posted"),
        ("payment_state", "not in", ("paid", "reversed", "invoicing_legacy")),
        ("invoice_date_due", "<", cutoff),
        ("amount_residual", ">", 0),
    ]
    partner = arguments.get("partner")
    if partner:
        domain.append(("partner_id", "ilike", str(partner)[:200]))

    limit = filters.check_limit(arguments.get("limit"))
    total = move.search_count(domain)
    rows = _rows(
        move.search_read(
            domain,
            [
                "name",
                "partner_id",
                "invoice_date",
                "invoice_date_due",
                "amount_total",
                "amount_residual",
                "currency_id",
            ],
            limit=limit,
            order="invoice_date_due asc",
        )
    )
    outstanding = sum(row.get("amount_residual") or 0.0 for row in rows)

    return {
        "model": "account.move",
        "label": "Overdue customer invoices",
        "as_of": cutoff,
        "matched": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        # Only over what was returned. Saying so matters: a model given a total
        # with no caveat will present it as the whole debt.
        "outstanding_in_returned_rows": outstanding,
        "rows": rows,
    }


def _today(env):
    """Today in the acting user's timezone, not the server's.

    An invoice due today is not overdue for somebody in Brussels while the
    server thinks it is UTC and already tomorrow.
    """
    return str(odoo_fields.Date.context_today(env.user))


OVERDUE_INVOICES = Tool(
    name="overdue_invoices",
    description=(
        "Customer invoices that are past their due date and still owe money, "
        "oldest first. Use this for anything about late payment, debt, chasing "
        "customers, or cash that is owed. This is about money customers owe "
        "us — vendor bills are a different question and are not included."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "partner": {
                "type": "string",
                "description": "Customer name to match. Omit for all customers.",
            },
            "as_of": {
                "type": "string",
                "description": "Judge overdue as of this date (YYYY-MM-DD). Defaults to today.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": catalog.MAX_ROWS},
        },
    },
    handler=overdue_invoices,
    models=("account.move",),
)


# --- customer_360 -----------------------------------------------------------


def customer_360(env, arguments):
    """Everything worth knowing about one customer, in one call."""
    partner_spec = _spec_or_fail("res.partner")
    partners = _model_or_fail(env, partner_spec)

    name = arguments.get("partner")
    if not isinstance(name, str) or not name.strip():
        message = "'partner' must be the customer's name"
        raise FilterError(message)

    found = partners.search([("name", "ilike", name.strip()[:200])], limit=5)
    if not found:
        return {"label": "Customer summary", "matched": 0, "rows": []}
    if len(found) > 1:
        # Ambiguity is reported rather than guessed at. Picking the first match
        # is how a summary about the wrong company gets presented confidently.
        return {
            "label": "Customer summary",
            "matched": len(found),
            "ambiguous": True,
            "candidates": _rows(found.read(["display_name", "email", "city"])),
            "rows": [],
        }

    partner = found
    summary = {
        "label": "Customer summary",
        "matched": 1,
        "partner": _rows(
            partner.read(["display_name", "email", "phone", "city", "country_id", "vat", "user_id"])
        )[0],
    }
    summary.update(_related_totals(env, partner))
    return summary


def _related_totals(env, partner):
    """Orders, invoices and opportunities for one partner, where installed.

    Each block is optional: a database without `sale` still gets a useful
    summary, and a user who cannot read invoices simply does not see them.
    """
    blocks = {}

    if "sale.order" in env and env["sale.order"].has_access("read"):
        orders = env["sale.order"]
        domain = [("partner_id", "=", partner.id)]
        blocks["sales_orders"] = {
            "count": orders.search_count(domain),
            "recent": _rows(
                orders.search_read(
                    domain,
                    ["name", "date_order", "state", "amount_total"],
                    limit=SUMMARY_ROWS,
                    order="date_order desc",
                )
            ),
        }

    if "account.move" in env and env["account.move"].has_access("read"):
        moves = env["account.move"]
        domain = [
            ("partner_id", "=", partner.id),
            ("move_type", "in", list(CUSTOMER_INVOICE_TYPES)),
            ("state", "=", "posted"),
        ]
        unpaid = [*domain, ("amount_residual", ">", 0)]
        blocks["invoices"] = {
            "count": moves.search_count(domain),
            "unpaid_count": moves.search_count(unpaid),
            "recent": _rows(
                moves.search_read(
                    domain,
                    ["name", "invoice_date", "payment_state", "amount_total", "amount_residual"],
                    limit=SUMMARY_ROWS,
                    order="invoice_date desc",
                )
            ),
        }

    if "crm.lead" in env and env["crm.lead"].has_access("read"):
        leads = env["crm.lead"]
        domain = [("partner_id", "=", partner.id)]
        blocks["opportunities"] = {
            "count": leads.search_count(domain),
            "recent": _rows(
                leads.search_read(
                    domain,
                    ["name", "stage_id", "expected_revenue", "date_deadline"],
                    limit=SUMMARY_ROWS,
                    order="create_date desc",
                )
            ),
        }

    return blocks


CUSTOMER_360 = Tool(
    name="customer_360",
    description=(
        "A rounded picture of one customer: their contact details, recent "
        "sales orders, invoices and how much is unpaid, and open "
        "opportunities. Use this when somebody asks about a customer in "
        "general — 'tell me about', 'summarise', 'how are we doing with' — "
        "rather than about one specific record."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["partner"],
        "properties": {
            "partner": {"type": "string", "description": "The customer's name."},
        },
    },
    handler=customer_360,
)


#: The closed set. Registered in import order by the package.
TOOLS = (FIND_RECORDS, AGGREGATE, STOCK_LEVELS, OVERDUE_INVOICES, CUSTOMER_360)
