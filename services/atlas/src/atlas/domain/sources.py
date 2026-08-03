"""What Atlas indexes, and how each thing becomes text.

A source template is a declaration, not code: which Odoo model, which fields,
which of them titles the document, and what label each one carries in the
rendered output. One renderer walks all of them.

Templates are data because the alternative — eight hand-written renderers —
means eight places to fix the day somebody notices that a partner's country was
never in the text. It also means the whole registry is testable without an ERP.

**Rendering decides retrieval quality.** A chunk is only findable if it contains
the words somebody would search for, so the output is labelled prose
(``Customer: Deco Addict``) rather than a field dump. The label is part of what
gets embedded and part of what lexical search matches on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from atlas.domain.corpus import Visibility
from atlas.domain.ingestion import RawDocument, SourceRecord

PDF_MIMETYPE = "application/pdf"
DOCX_MIMETYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIMETYPES = ("text/plain", "text/markdown")

#: Odoo renders a many2one as ``[id, "Display Name"]``.
_RELATION_PAIR = 2


@dataclass(frozen=True, slots=True)
class Line:
    """One labelled line of the rendered document.

    Attributes:
        kind: How to turn the raw Odoo value into text. ``auto`` handles the
            common cases — a many2one arrives as ``[id, "Name"]``, a float as a
            number — and the rest exist for values ``auto`` would get wrong.
    """

    label: str
    key: str
    kind: str = "auto"


@dataclass(frozen=True, slots=True)
class ChildLines:
    """Child records rendered as a list under the parent.

    Order lines are most of what makes an order searchable: the product names
    live there, not on the header.
    """

    key: str
    model: str
    heading: str
    fields: tuple[str, ...]
    quantity_key: str
    name_key: str = "name"
    subtotal_key: str = "price_subtotal"
    limit: int = 200


@dataclass(frozen=True, slots=True)
class SourceTemplate:
    """One indexable kind of Odoo record."""

    key: str
    res_model: str
    label: str
    title_key: str
    lines: tuple[Line, ...]
    ref_key: str | None = None
    company_key: str | None = "company_id"
    visibility: Visibility = Visibility.INTERNAL
    domain: tuple[Any, ...] = ()
    children: ChildLines | None = None
    binary_key: str | None = None
    requires_module: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> tuple[str, ...]:
        """Every field this template needs read, deduplicated and ordered."""
        names = [self.title_key, *(line.key for line in self.lines)]
        for optional in (self.ref_key, self.company_key, self.binary_key):
            if optional:
                names.append(optional)
        if self.children:
            names.append(self.children.key)
        names.append("write_date")
        seen: dict[str, None] = {}
        for name in names:
            seen.setdefault(name, None)
        return tuple(seen)

    def render(self, record: SourceRecord, *, body: str | None = None) -> RawDocument:
        """Turn one record into a document ready to hash, chunk and embed.

        Args:
            record: The record to render.
            body: Text extracted from an attachment, used instead of the
                rendered field list. The header is still prepended, so a PDF
                stays attributable to the record it hangs off.
        """
        values = record.values
        title = _text(values.get(self.title_key)) or f"{self.label} {record.res_id}"
        reference = _text(values.get(self.ref_key)) if self.ref_key else ""

        heading = f"{self.label}: {title}"
        if reference and reference != title:
            heading = f"{heading} ({reference})"

        parts = [heading]
        parts.extend(
            f"{line.label}: {rendered}"
            for line in self.lines
            if (rendered := _render_value(values.get(line.key), line.kind))
        )
        if self.children:
            parts.extend(_render_children(self.children, values.get(self.children.key)))
        if body:
            parts.append(body.strip())

        return RawDocument(
            source_key=self.key,
            title=title,
            text="\n".join(parts),
            res_model=record.res_model,
            res_id=record.res_id,
            external_ref=reference or None,
            company_id=record.company_id or _relation_id(values.get(self.company_key or "")),
            visibility=self.visibility,
            record_write_date=record.write_date,
            metadata={"label": self.label, **dict(self.metadata)},
        )


def _render_children(spec: ChildLines, raw: Any) -> list[str]:
    """Render child records as an indented list, or nothing if there are none.

    Children arrive already expanded by the source reader. Ids alone would be
    useless here: the point of reading them is the product names.
    """
    if not isinstance(raw, list) or not raw:
        return []
    rows = [row for row in raw[: spec.limit] if isinstance(row, dict)]
    if not rows:
        return []

    rendered = [f"{spec.heading}:"]
    for row in rows:
        quantity = _render_value(row.get(spec.quantity_key), "quantity")
        name = _text(row.get(spec.name_key)) or _text(row.get("product_id"))
        subtotal = _render_value(row.get(spec.subtotal_key), "auto")
        line = f"  - {quantity} x {name}" if quantity else f"  - {name}"
        rendered.append(f"{line} = {subtotal}" if subtotal else line)

    if len(raw) > spec.limit:
        rendered.append(f"  ... and {len(raw) - spec.limit} more")
    return rendered


def _is_empty(value: Any) -> bool:
    """Whether Odoo means "nothing here".

    Odoo returns an unset value of any type as ``False``, so a bare ``False`` is
    treated as empty — which is why a boolean worth rendering must declare
    ``kind="boolean"``.

    Numbers are never empty. ``0 == False`` in Python, and a naive membership
    test would have quietly dropped "Quantity on hand: 0" — which is precisely
    the stock level somebody is most likely to ask about.
    """
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return False
    return value is None or value in ("", [], {})


def _render_value(value: Any, kind: str) -> str:
    if kind == "boolean":
        return "yes" if value else "no"
    if _is_empty(value):
        return ""
    if kind == "quantity":
        whole = float(value) == int(float(value))
        return _number(value, places=0 if whole else 2)
    if kind == "money":
        return _number(value, places=2)
    return _text(value)


def _text(value: Any) -> str:  # noqa: PLR0911 - one branch per Odoo value shape
    """Render an arbitrary Odoo value as readable text."""
    if _is_empty(value):
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if _is_relation(value):
        return str(value[1])
    if isinstance(value, (list, tuple)):
        rendered = [_text(item) for item in value]
        return ", ".join(item for item in rendered if item)
    if isinstance(value, bool):
        return "yes"
    if isinstance(value, float):
        return _number(value, places=2)
    return str(value).strip()


def _number(value: Any, *, places: int) -> str:
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def _is_relation(value: Any) -> bool:
    """Whether a value is Odoo's ``[id, "Display Name"]`` many2one shape."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == _RELATION_PAIR
        and isinstance(value[0], int)
        and isinstance(value[1], str)
    )


def _relation_id(value: Any) -> int | None:
    if _is_relation(value):
        return int(value[0])
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# The registry
#
# Business modules are named here as strings and read over HTTP. Nothing imports
# them, and the addon declares no dependency on them (ADR-0002): a source whose
# module is not installed simply reports itself unavailable.
# ---------------------------------------------------------------------------

PARTNER = SourceTemplate(
    key="odoo.res.partner",
    res_model="res.partner",
    label="Contact",
    title_key="display_name",
    ref_key="ref",
    lines=(
        Line("Type", "company_type"),
        Line("Job title", "function"),
        Line("Parent company", "parent_id"),
        Line("Email", "email"),
        Line("Phone", "phone"),
        Line("Mobile", "mobile"),
        Line("Website", "website"),
        Line("Street", "street"),
        Line("City", "city"),
        Line("Postcode", "zip"),
        Line("Country", "country_id"),
        Line("Tax ID", "vat"),
        Line("Tags", "category_id"),
        Line("Notes", "comment"),
    ),
)

PRODUCT = SourceTemplate(
    key="odoo.product.template",
    res_model="product.template",
    label="Product",
    title_key="name",
    ref_key="default_code",
    visibility=Visibility.PUBLIC,
    lines=(
        Line("Internal reference", "default_code"),
        Line("Barcode", "barcode"),
        Line("Category", "categ_id"),
        Line("Sales price", "list_price", kind="money"),
        Line("Cost", "standard_price", kind="money"),
        Line("Unit", "uom_id"),
        Line("Description", "description_sale"),
    ),
)

CRM_LEAD = SourceTemplate(
    key="odoo.crm.lead",
    res_model="crm.lead",
    label="Opportunity",
    title_key="name",
    requires_module="crm",
    lines=(
        Line("Customer", "partner_id"),
        Line("Contact name", "contact_name"),
        Line("Email", "email_from"),
        Line("Phone", "phone"),
        Line("Stage", "stage_id"),
        Line("Salesperson", "user_id"),
        Line("Sales team", "team_id"),
        Line("Expected revenue", "expected_revenue", kind="money"),
        Line("Probability", "probability"),
        Line("Expected closing", "date_deadline"),
        Line("Notes", "description"),
    ),
)

SALE_ORDER = SourceTemplate(
    key="odoo.sale.order",
    res_model="sale.order",
    label="Sales Order",
    title_key="name",
    ref_key="name",
    requires_module="sale",
    lines=(
        Line("Customer", "partner_id"),
        Line("Customer reference", "client_order_ref"),
        Line("Order date", "date_order"),
        Line("Status", "state"),
        Line("Salesperson", "user_id"),
        Line("Total", "amount_total", kind="money"),
        Line("Currency", "currency_id"),
        Line("Notes", "note"),
    ),
    children=ChildLines(
        key="order_line",
        model="sale.order.line",
        heading="Order lines",
        fields=("name", "product_id", "product_uom_qty", "price_unit", "price_subtotal"),
        quantity_key="product_uom_qty",
    ),
)

PURCHASE_ORDER = SourceTemplate(
    key="odoo.purchase.order",
    res_model="purchase.order",
    label="Purchase Order",
    title_key="name",
    ref_key="name",
    requires_module="purchase",
    lines=(
        Line("Vendor", "partner_id"),
        Line("Vendor reference", "partner_ref"),
        Line("Order date", "date_order"),
        Line("Status", "state"),
        Line("Buyer", "user_id"),
        Line("Total", "amount_total", kind="money"),
        Line("Currency", "currency_id"),
    ),
    children=ChildLines(
        key="order_line",
        model="purchase.order.line",
        heading="Order lines",
        fields=("name", "product_id", "product_qty", "price_unit", "price_subtotal"),
        quantity_key="product_qty",
    ),
)

INVOICE = SourceTemplate(
    key="odoo.account.move",
    res_model="account.move",
    label="Invoice",
    title_key="name",
    ref_key="name",
    requires_module="account",
    visibility=Visibility.RESTRICTED,
    domain=(("move_type", "in", ("out_invoice", "out_refund", "in_invoice", "in_refund")),),
    lines=(
        Line("Partner", "partner_id"),
        Line("Document type", "move_type"),
        Line("Invoice date", "invoice_date"),
        Line("Due date", "invoice_date_due"),
        Line("Status", "state"),
        Line("Payment status", "payment_state"),
        Line("Total", "amount_total", kind="money"),
        Line("Amount due", "amount_residual", kind="money"),
        Line("Currency", "currency_id"),
        Line("Reference", "ref"),
        Line("Terms", "narration"),
    ),
    children=ChildLines(
        key="invoice_line_ids",
        model="account.move.line",
        heading="Invoice lines",
        fields=("name", "product_id", "quantity", "price_unit", "price_subtotal"),
        quantity_key="quantity",
    ),
)

STOCK_QUANT = SourceTemplate(
    key="odoo.stock.quant",
    res_model="stock.quant",
    label="Stock",
    title_key="display_name",
    requires_module="stock",
    lines=(
        Line("Product", "product_id"),
        Line("Location", "location_id"),
        Line("Lot or serial", "lot_id"),
        Line("Quantity on hand", "quantity", kind="quantity"),
        Line("Reserved", "reserved_quantity", kind="quantity"),
    ),
)

ATTACHMENT = SourceTemplate(
    key="odoo.ir.attachment",
    res_model="ir.attachment",
    label="Document",
    title_key="name",
    binary_key="datas",
    domain=(("mimetype", "in", (PDF_MIMETYPE, DOCX_MIMETYPE, *TEXT_MIMETYPES)),),
    lines=(
        Line("File name", "name"),
        Line("Type", "mimetype"),
        Line("Attached to", "res_model"),
        Line("Description", "description"),
    ),
)

#: Every template Atlas knows how to index, by source key.
REGISTRY: Mapping[str, SourceTemplate] = {
    template.key: template
    for template in (
        PARTNER,
        PRODUCT,
        CRM_LEAD,
        SALE_ORDER,
        PURCHASE_ORDER,
        INVOICE,
        STOCK_QUANT,
        ATTACHMENT,
    )
}


def template_for(source_key: str) -> SourceTemplate:
    """Look up a template, or say which keys exist.

    Raises:
        KeyError: No such source. The message lists the registry, because the
            usual cause is a typo in a configuration row.
    """
    try:
        return REGISTRY[source_key]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY))
        message = f"unknown ingest source {source_key!r}; known sources are {known}"
        raise KeyError(message) from exc


def source_keys() -> Sequence[str]:
    """Every known source key, sorted."""
    return sorted(REGISTRY)
