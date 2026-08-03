"""What the tools are allowed to touch.

A closed list of models, and per model a closed list of fields. Everything a
tool does is checked against this before it reaches the ORM.

The point is not that the ORM would otherwise be unsafe — record rules still
apply, because every tool runs as the acting user (ADR-0006). The point is that
a language model chooses the arguments. Given a free hand it will eventually ask
for ``password``, or filter on a field that means something different from what
it assumed, or group a million rows. An allow-list turns "the model asked for
something strange" from an incident into a rejected tool call.

Adding a field here is a deliberate act with a diff attached. That is the
intended cost.
"""

import dataclasses

#: Operators a filter may use. Deliberately short. `child_of` and `parent_of`
#: are absent because their meaning depends on a hierarchy the model cannot see,
#: and `=like` because nobody needs case-sensitive globbing badly enough to
#: explain it to a model.
ALLOWED_OPERATORS = frozenset(
    {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "not ilike"}
)

#: Operators that take a list rather than a scalar.
LIST_OPERATORS = frozenset({"in", "not in"})

#: Operators that place a value in an order. Neither `None` nor a boolean means
#: anything to them, and `id > None` reaches PostgreSQL as `id > false`, which
#: is an error rather than an empty result.
ORDERING_OPERATORS = frozenset({">", ">=", "<", "<="})

#: Operators that only make sense against text.
TEXT_OPERATORS = frozenset({"like", "ilike", "not ilike"})

#: Bounds. A model that asks for ten thousand rows is not going to read them,
#: and the context window cannot hold them either.
MAX_ROWS = 50
MAX_FILTERS = 12
MAX_LIST_VALUES = 50
MAX_TEXT_LENGTH = 200
MAX_GROUPS = 3


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    """One model a tool may read, and the parts of it that are in bounds.

    Attributes:
        fields: Readable *and* filterable. One list rather than two, because a
            field a caller may filter on is one whose values they can already
            infer, and pretending otherwise buys nothing.
        measures: Numeric fields that may be summed or averaged.
        groupable: Fields that may be grouped by. Kept separate from ``fields``
            because grouping by a free-text column produces a row per record,
            which is a way to page through a table one group at a time.
    """

    model: str
    label: str
    fields: tuple[str, ...]
    date_field: str
    measures: tuple[str, ...] = ()
    groupable: tuple[str, ...] = ()
    order: str = "id desc"
    requires_module: str = "base"


PARTNER = ModelSpec(
    model="res.partner",
    label="Contacts and companies",
    fields=(
        "id",
        "display_name",
        "name",
        "ref",
        "email",
        "phone",
        "mobile",
        "city",
        "country_id",
        "vat",
        "function",
        "parent_id",
        "is_company",
        "customer_rank",
        "supplier_rank",
        "category_id",
        "user_id",
        "company_id",
        "create_date",
        "write_date",
    ),
    date_field="create_date",
    groupable=("country_id", "user_id", "company_id", "is_company"),
)

PRODUCT = ModelSpec(
    model="product.template",
    label="Products",
    fields=(
        "id",
        "display_name",
        "name",
        "default_code",
        "barcode",
        "categ_id",
        "list_price",
        "standard_price",
        "type",
        "uom_id",
        "active",
        "company_id",
        "write_date",
    ),
    date_field="write_date",
    measures=("list_price", "standard_price"),
    groupable=("categ_id", "type", "company_id"),
)

SALE_ORDER = ModelSpec(
    model="sale.order",
    label="Sales orders",
    fields=(
        "id",
        "name",
        "partner_id",
        "date_order",
        "state",
        "amount_total",
        "amount_untaxed",
        "currency_id",
        "user_id",
        "team_id",
        "client_order_ref",
        "company_id",
        "write_date",
    ),
    date_field="date_order",
    measures=("amount_total", "amount_untaxed"),
    groupable=("partner_id", "state", "user_id", "team_id", "company_id"),
    order="date_order desc",
    requires_module="sale",
)

SALE_ORDER_LINE = ModelSpec(
    model="sale.order.line",
    label="Sales order lines",
    fields=(
        "id",
        "order_id",
        "product_id",
        "name",
        "product_uom_qty",
        "qty_delivered",
        "price_unit",
        "price_subtotal",
        "price_total",
        "state",
        "salesman_id",
        "order_partner_id",
        "company_id",
    ),
    date_field="create_date",
    measures=("product_uom_qty", "qty_delivered", "price_subtotal", "price_total"),
    groupable=("product_id", "order_partner_id", "salesman_id", "state", "company_id"),
    requires_module="sale",
)

PURCHASE_ORDER = ModelSpec(
    model="purchase.order",
    label="Purchase orders",
    fields=(
        "id",
        "name",
        "partner_id",
        "date_order",
        "state",
        "amount_total",
        "amount_untaxed",
        "currency_id",
        "user_id",
        "partner_ref",
        "company_id",
        "write_date",
    ),
    date_field="date_order",
    measures=("amount_total", "amount_untaxed"),
    groupable=("partner_id", "state", "user_id", "company_id"),
    order="date_order desc",
    requires_module="purchase",
)

INVOICE = ModelSpec(
    model="account.move",
    label="Invoices and bills",
    fields=(
        "id",
        "name",
        "partner_id",
        "invoice_date",
        "invoice_date_due",
        "state",
        "move_type",
        "payment_state",
        "amount_total",
        "amount_residual",
        "currency_id",
        "ref",
        "company_id",
        "write_date",
    ),
    date_field="invoice_date",
    measures=("amount_total", "amount_residual"),
    groupable=("partner_id", "state", "move_type", "payment_state", "company_id"),
    order="invoice_date desc",
    requires_module="account",
)

STOCK_QUANT = ModelSpec(
    model="stock.quant",
    label="Stock on hand",
    fields=(
        "id",
        "product_id",
        "location_id",
        "lot_id",
        "quantity",
        "reserved_quantity",
        "available_quantity",
        "company_id",
        "write_date",
    ),
    date_field="write_date",
    measures=("quantity", "reserved_quantity", "available_quantity"),
    groupable=("product_id", "location_id", "company_id"),
    requires_module="stock",
)

CRM_LEAD = ModelSpec(
    model="crm.lead",
    label="Opportunities",
    fields=(
        "id",
        "name",
        "partner_id",
        "contact_name",
        "email_from",
        "phone",
        "stage_id",
        "user_id",
        "team_id",
        "expected_revenue",
        "probability",
        "date_deadline",
        "active",
        "company_id",
        "create_date",
        "write_date",
    ),
    date_field="create_date",
    measures=("expected_revenue", "probability"),
    groupable=("stage_id", "user_id", "team_id", "partner_id", "company_id"),
    requires_module="crm",
)

#: Every model a tool may reach, by model name.
MODELS = {
    spec.model: spec
    for spec in (
        PARTNER,
        PRODUCT,
        SALE_ORDER,
        SALE_ORDER_LINE,
        PURCHASE_ORDER,
        INVOICE,
        STOCK_QUANT,
        CRM_LEAD,
    )
}


def spec_for(model):
    """Return the spec for ``model``, or ``None`` if it is not in bounds.

    ``None`` rather than an exception: "no such model" is an answer a caller
    should get as a rejected tool call, not as a server error.
    """
    return MODELS.get(model)


def available_models(env):
    """The specs this database can actually serve to the acting user.

    A model whose module is not installed, or that this user may not read, is
    left out of the catalogue entirely — so the model is never told about a tool
    argument that could only ever fail.
    """
    return [
        spec for spec in MODELS.values() if spec.model in env and env[spec.model].has_access("read")
    ]
