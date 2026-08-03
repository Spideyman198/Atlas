"""Tests for the source registry and its rendering.

Rendering decides what is findable. A field that silently renders to nothing is
a question nobody can ask, and it fails no test unless one is written for it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atlas.domain.corpus import Visibility
from atlas.domain.ingestion import RawDocument, SourceRecord
from atlas.domain.sources import (
    INVOICE,
    PARTNER,
    REGISTRY,
    SALE_ORDER,
    STOCK_QUANT,
    source_keys,
    template_for,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)


def record(res_model: str, res_id: int, **values: object) -> SourceRecord:
    return SourceRecord(
        res_model=res_model,
        res_id=res_id,
        values={"id": res_id, **values},
        write_date=NOW,
        company_id=1,
    )


def test_every_template_is_registered_under_its_own_key() -> None:
    for key, template in REGISTRY.items():
        assert template.key == key


def test_an_unknown_source_names_the_ones_that_exist() -> None:
    with pytest.raises(KeyError, match=r"odoo.res.partner"):
        template_for("odoo.not.a.thing")


def test_every_template_asks_for_write_date() -> None:
    """The watermark is a `write_date` comparison; a template without it cannot sync."""
    for key in source_keys():
        assert "write_date" in REGISTRY[key].fields


def test_a_templates_fields_are_deduplicated() -> None:
    # `name` is both the title and the reference on a sales order.
    assert len(SALE_ORDER.fields) == len(set(SALE_ORDER.fields))


def test_rendering_labels_every_line() -> None:
    document = PARTNER.render(
        record(
            "res.partner",
            3,
            display_name="Deco Addict",
            email="deco@example.com",
            country_id=[21, "Belgium"],
        )
    )

    assert document.text.startswith("Contact: Deco Addict")
    assert "Email: deco@example.com" in document.text
    assert "Country: Belgium" in document.text


def test_empty_fields_are_left_out_rather_than_rendered_blank() -> None:
    document = PARTNER.render(record("res.partner", 3, display_name="Deco Addict", phone=False))

    assert "Phone" not in document.text


def test_a_zero_quantity_is_rendered_not_dropped() -> None:
    """`0 == False` in Python, and a naive emptiness test loses the answer.

    "Which products are out of stock" is the question most likely to be asked of
    a stock source, and it is unanswerable if zero renders as nothing.
    """
    document = STOCK_QUANT.render(
        record(
            "stock.quant",
            8,
            display_name="Desk / WH",
            product_id=[4, "Desk"],
            quantity=0.0,
            reserved_quantity=0.0,
        )
    )

    assert "Quantity on hand: 0" in document.text


def test_a_reference_is_shown_beside_the_title_when_it_differs() -> None:
    document = PARTNER.render(record("res.partner", 3, display_name="Deco Addict", ref="C0042"))

    assert document.text.startswith("Contact: Deco Addict (C0042)")
    assert document.external_ref == "C0042"


def test_a_reference_equal_to_the_title_is_not_repeated() -> None:
    document = SALE_ORDER.render(record("sale.order", 5, name="S00005"))

    assert document.text.startswith("Sales Order: S00005")
    assert "(S00005)" not in document.text


def test_child_lines_are_rendered_with_quantities() -> None:
    document = SALE_ORDER.render(
        record(
            "sale.order",
            5,
            name="S00005",
            order_line=[
                {"name": "Desk Combination", "product_uom_qty": 3.0, "price_subtotal": 1500.0},
                {"name": "Office Chair", "product_uom_qty": 1.0, "price_subtotal": 120.5},
            ],
        )
    )

    assert "Order lines:" in document.text
    assert "  - 3 x Desk Combination = 1,500.00" in document.text
    assert "  - 1 x Office Chair = 120.50" in document.text


def test_child_lines_are_capped_and_say_so() -> None:
    assert SALE_ORDER.children is not None
    limit = SALE_ORDER.children.limit
    lines = [{"name": f"Item {n}", "product_uom_qty": 1.0} for n in range(limit + 5)]
    document = SALE_ORDER.render(record("sale.order", 5, name="S00005", order_line=lines))

    assert "... and 5 more" in document.text


def test_a_title_falls_back_to_the_record_id() -> None:
    document = PARTNER.render(record("res.partner", 3, display_name=""))

    assert document.title == "Contact 3"


def test_invoices_are_restricted_by_default() -> None:
    """Visibility is a cheap pre-filter, never the authorization decision."""
    assert INVOICE.visibility is Visibility.RESTRICTED


# --- hashing ---------------------------------------------------------------


def base_document(**overrides: object) -> RawDocument:
    values: dict[str, object] = {
        "source_key": "odoo.res.partner",
        "title": "Deco Addict",
        "text": "Contact: Deco Addict",
        "res_model": "res.partner",
        "res_id": 3,
    }
    values.update(overrides)
    return RawDocument(**values)  # type: ignore[arg-type]


def test_the_same_content_hashes_the_same() -> None:
    assert base_document().source_hash() == base_document().source_hash()


def test_changed_content_changes_the_hash() -> None:
    changed = base_document(text="Contact: Deco Addict\nCity: Brussels")

    assert changed.source_hash() != base_document().source_hash()


def test_reformatting_alone_does_not_change_the_hash() -> None:
    """A template that changes its whitespace must not re-embed the corpus."""
    spaced = base_document(text="Contact:   Deco Addict  ")

    assert spaced.source_hash() == base_document().source_hash()


def test_identical_text_on_different_records_hashes_differently() -> None:
    """The unique index would otherwise let one record overwrite the other."""
    other = base_document(res_id=4)

    assert other.source_hash() != base_document().source_hash()


def test_a_fingerprint_stands_in_for_the_content() -> None:
    """An attachment is compared by checksum, so it need not be downloaded."""
    first = base_document(text="whatever the header says", content_fingerprint="abc")
    second = base_document(text="a completely different header", content_fingerprint="abc")

    assert first.source_hash() == second.source_hash()


def test_a_changed_fingerprint_changes_the_hash() -> None:
    first = base_document(text="same header", content_fingerprint="abc")
    second = base_document(text="same header", content_fingerprint="def")

    assert first.source_hash() != second.source_hash()
