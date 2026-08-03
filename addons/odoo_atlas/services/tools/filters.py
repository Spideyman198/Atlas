"""Turning structured filters into Odoo domains, safely.

The model never emits a domain. It emits objects:

    {"field": "amount_total", "operator": ">=", "value": 1000}

and this module compiles them, after checking the field is in the allow-list,
the operator is one we accept, and the value has a type that makes sense for
that field. Everything else is refused (ADR-0006 §2).

Why not just let the model write a domain? Because a domain is a small
programming language with recursion, dotted field traversal, and operators whose
semantics depend on the model. ``[('partner_id.user_id.login', 'ilike', 'a')]``
is a valid domain that walks two relations to a field nobody put on any
allow-list. Accepting objects and compiling them means the set of expressible
queries is one we chose rather than one we inherited.

Record rules still apply on top of all this — every tool runs as the acting
user. This is the layer that stops a *well-formed* query being a strange one.
"""

from odoo.addons.odoo_atlas.services.tools import catalog


class FilterError(ValueError):
    """A filter was malformed, out of bounds, or of the wrong type.

    Carries a message meant to be read by the model that produced it: current
    models correct themselves well when told exactly what was wrong and what
    was allowed instead.
    """


def compile_filters(model, filters, spec):
    """Compile structured filters into an Odoo domain.

    Args:
        model: The Odoo model, for reading field types.
        filters: A list of ``{"field", "operator", "value"}`` objects.
        spec: The :class:`~.catalog.ModelSpec` bounding what is allowed.

    Returns:
        A domain, ANDed together. An empty filter list compiles to an empty
        domain, which matches everything the record rules allow — bounded, so
        not dangerous.

    Raises:
        FilterError: Anything not expressible within the allow-list.
    """
    if filters is None:
        return []
    if not isinstance(filters, list):
        message = "'filters' must be a list of {field, operator, value} objects"
        raise FilterError(message)
    if len(filters) > catalog.MAX_FILTERS:
        message = f"at most {catalog.MAX_FILTERS} filters per call, got {len(filters)}"
        raise FilterError(message)

    return [_clause(model, entry, spec) for entry in filters]


def _clause(model, entry, spec):
    if not isinstance(entry, dict):
        message = "each filter must be an object with field, operator and value"
        raise FilterError(message)

    field = entry.get("field")
    operator = entry.get("operator", "=")
    value = entry.get("value")

    _check_field(field, spec)
    _check_operator(operator)

    description = model._fields.get(field)
    if description is None:
        # In the allow-list but absent from this database: a field that came and
        # went between Odoo versions. Refuse it as out of bounds rather than
        # letting the ORM raise something less legible.
        message = f"field {field!r} does not exist on {spec.model}"
        raise FilterError(message)

    return (field, operator, _check_value(field, operator, value, description))


def _check_field(field, spec):
    if not isinstance(field, str) or not field:
        message = "'field' must be a non-empty string"
        raise FilterError(message)
    if "." in field:
        # The whole reason this module exists. A dotted path walks relations to
        # somewhere nobody allow-listed.
        message = (
            f"{field!r} traverses a relation; only direct fields of {spec.model} can be filtered on"
        )
        raise FilterError(message)
    if field not in spec.fields:
        allowed = ", ".join(sorted(spec.fields))
        message = f"{field!r} is not filterable on {spec.model}. Allowed: {allowed}"
        raise FilterError(message)


def _check_operator(operator):
    if operator not in catalog.ALLOWED_OPERATORS:
        allowed = ", ".join(sorted(catalog.ALLOWED_OPERATORS))
        message = f"operator {operator!r} is not allowed. Allowed: {allowed}"
        raise FilterError(message)


def _check_value(field, operator, value, description):
    """Check the value suits both the operator and the field's type."""
    if operator in catalog.LIST_OPERATORS:
        items = _check_list_shape(field, value)
        # Every element, not just the list itself. Checking only the shape let
        # `id in ["Brussels"]` compile, and PostgreSQL then rejected it as
        # `invalid input syntax for type integer` — a 500 where a rejected tool
        # call was wanted. Found by enumerating the cross product in
        # tests/test_tool_filters.py, not by imagining it.
        return [_check_scalar(field, item, description) for item in items]

    if isinstance(value, (list, tuple, dict)):
        message = f"{field!r} with operator {operator!r} takes a single value, not a list"
        raise FilterError(message)

    if operator in catalog.ORDERING_OPERATORS and (value is None or isinstance(value, bool)):
        # `id > None` reaches PostgreSQL as `id > false` and raises
        # `operator does not exist: integer > boolean`. A rule about the
        # operator rather than the field's type, because it holds for every
        # type and does not need the catalogue to be exhaustive to work.
        message = f"{field!r} with operator {operator!r} needs a value to compare against"
        raise FilterError(message)

    if operator in catalog.TEXT_OPERATORS:
        # Only against something made of text. `is_company ilike 'Brussels'`
        # compiles happily and then raises inside the ORM's domain optimiser —
        # a 500 where a rejected tool call was wanted. A many2one counts,
        # because Odoo matches those by display name.
        if description.type not in _TEXTUAL and description.type != "many2one":
            message = f"{field!r} is not text; operator {operator!r} does not apply to it"
            raise FilterError(message)
        return _check_text(field, value)
    return _check_scalar(field, value, description)


#: Field types that hold numbers, and ones that hold text. A `many2one` is in
#: neither: Odoo accepts an id *or* a name for one, and the name form is genuinely
#: useful to a model that knows a customer by name and not by id.
_NUMERIC = frozenset({"integer", "float", "monetary"})
_TEXTUAL = frozenset({"char", "text", "selection", "html"})
_TEMPORAL = frozenset({"date", "datetime"})


def _check_scalar(field, value, description):
    """Check one value against the field's declared type.

    The aim is not to reimplement the ORM's coercion. It is to refuse the
    handful of mismatches that either blow up in the database or, worse, match
    nothing quietly — which reads to a model as "there are none" and sends it
    off answering a different question.

    Two rules hold for every type and are settled here; the rest is dispatched
    to a per-type check so that adding a type means adding a function rather
    than another branch to a ladder nobody can hold in their head.
    """
    if value is None:
        # Odoo's way of asking "is this unset", and valid for every type.
        return value

    kind = description.type
    if isinstance(value, bool):
        if kind in _NUMERIC:
            # `True` is an int in Python and would silently mean 1.
            message = f"{field!r} is numeric; true/false is not a value for it"
            raise FilterError(message)
        return value

    return _CHECKS.get(kind, _check_other)(field, value, kind)


def _check_numeric(field, value, kind):
    if isinstance(value, str):
        message = f"{field!r} takes a number, not text"
        raise FilterError(message)
    if kind == "integer" and isinstance(value, float):
        message = f"{field!r} takes a whole number"
        raise FilterError(message)
    return value


def _check_relation(field, value, _kind):
    """A many2one takes a record id or a display name, and Odoo resolves both."""
    if isinstance(value, float):
        message = f"{field!r} takes a record id or a name"
        raise FilterError(message)
    if isinstance(value, str):
        return _check_text(field, value)
    return value


def _check_textual(field, value, _kind):
    if isinstance(value, (int, float)):
        message = f"{field!r} is text; {value!r} is not a value for it"
        raise FilterError(message)
    return _check_text(field, value)


def _check_temporal(field, value, _kind):
    if not isinstance(value, str):
        message = f"{field!r} is a date; give it as text, such as '2026-08-01'"
        raise FilterError(message)
    return _check_text(field, value)


def _check_boolean(field, value, _kind):
    # `True` and `False` already returned above, so anything arriving here is
    # the wrong type by definition.
    message = f"{field!r} is true or false; {value!r} is not a value for it"
    raise FilterError(message)


def _check_other(field, value, _kind):
    """Anything the catalogue allows that has no rule of its own.

    Reached for binary, reference and the rest. Bounded rather than validated:
    the field is on an allow-list, so the worst case is a query that matches
    nothing.
    """
    if isinstance(value, str):
        return _check_text(field, value)
    return value


#: Per-type checks, by Odoo field type.
_CHECKS = {
    **dict.fromkeys(_NUMERIC, _check_numeric),
    **dict.fromkeys(_TEXTUAL, _check_textual),
    **dict.fromkeys(_TEMPORAL, _check_temporal),
    "many2one": _check_relation,
    "boolean": _check_boolean,
}


def _check_list_shape(field, value):
    if not isinstance(value, list):
        message = f"{field!r} with 'in' or 'not in' takes a list"
        raise FilterError(message)
    if not value:
        message = f"{field!r} with 'in' or 'not in' needs at least one value"
        raise FilterError(message)
    if len(value) > catalog.MAX_LIST_VALUES:
        message = f"at most {catalog.MAX_LIST_VALUES} values, got {len(value)}"
        raise FilterError(message)
    for item in value:
        if isinstance(item, (list, tuple, dict)):
            message = f"{field!r} list values must be scalars"
            raise FilterError(message)
    return value


def _check_text(field, value):
    if not isinstance(value, str):
        message = f"{field!r} with this operator takes a string"
        raise FilterError(message)
    if len(value) > catalog.MAX_TEXT_LENGTH:
        message = f"values are limited to {catalog.MAX_TEXT_LENGTH} characters"
        raise FilterError(message)
    return value


def check_fields(requested, spec):
    """Narrow a requested field list to what this model may return.

    Unknown fields are refused rather than dropped. Silently omitting one makes
    a model conclude the record has no such value, which is a worse answer than
    being told the field is not available.
    """
    if requested is None:
        return _default_fields(spec)
    if not isinstance(requested, list) or not requested:
        message = "'fields' must be a non-empty list of field names"
        raise FilterError(message)

    unknown = [name for name in requested if name not in spec.fields]
    if unknown:
        allowed = ", ".join(sorted(spec.fields))
        message = f"cannot read {', '.join(map(repr, unknown))} on {spec.model}. Allowed: {allowed}"
        raise FilterError(message)
    return ["id", *[name for name in requested if name != "id"]]


def _default_fields(spec):
    """A sensible default: what identifies the record, plus its measures."""
    preferred = [
        name
        for name in ("id", "name", "display_name", "partner_id", "state", spec.date_field)
        if name in spec.fields
    ]
    preferred.extend(name for name in spec.measures if name in spec.fields)
    seen = dict.fromkeys(preferred)
    return list(seen)


def check_limit(requested):
    """Clamp a row count into bounds, defaulting to the maximum."""
    try:
        limit = int(requested)
    except (TypeError, ValueError):
        return catalog.MAX_ROWS
    return max(min(limit, catalog.MAX_ROWS), 1)
