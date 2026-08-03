"""Tests for the ingestion pipeline.

The two properties M7 is judged on live here:

    re-running a sync with no data changes performs zero embedding calls, and
    changing one record updates exactly its chunks, transactionally.

Both are about money and about correctness, and neither survives a refactor
unless something asserts it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from atlas.application.ingestion import SyncSource
from atlas.domain.embedding import EmbeddingPurpose, EmbeddingResult
from atlas.domain.ingestion import JobKind, SourceRecord
from atlas.infrastructure.odoo.fakes import FakeSourceReader
from atlas.infrastructure.persistence.fakes import (
    InMemoryEmbeddingCache,
    InMemorySourceState,
    InMemoryVectorStore,
)
from atlas.infrastructure.providers.fakes import HashEmbeddingProvider

pytestmark = pytest.mark.unit

PARTNER = "odoo.res.partner"
SALE_ORDER = "odoo.sale.order"
ATTACHMENT = "odoo.ir.attachment"

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class CountingEmbedder(HashEmbeddingProvider):
    """A real embedding provider that also says how often it was called.

    Subclassed rather than mocked: the pipeline's batching, ordering and
    dimension handling are all exercised for real, and only the counter is new.
    """

    def __init__(self, *, dimensions: int) -> None:
        super().__init__(dimensions=dimensions)
        self.calls = 0
        self.texts: list[str] = []

    async def embed(
        self,
        texts: Sequence[str],
        purpose: EmbeddingPurpose = EmbeddingPurpose.DOCUMENT,
    ) -> EmbeddingResult:
        self.calls += 1
        self.texts.extend(texts)
        return await super().embed(texts, purpose)


def partner(
    record_id: int, name: str, *, written: datetime = NOW, **values: object
) -> SourceRecord:
    return SourceRecord(
        res_model="res.partner",
        res_id=record_id,
        values={"id": record_id, "display_name": name, "write_date": written, **values},
        write_date=written,
        company_id=1,
    )


class Pipeline:
    """Everything the sync needs, wired to in-memory doubles."""

    def __init__(self, records: dict[str, list[SourceRecord]], **reader_kwargs: Any) -> None:
        self.reader = FakeSourceReader(records=records, **reader_kwargs)
        self.store = InMemoryVectorStore()
        self.cache = InMemoryEmbeddingCache()
        self.state = InMemorySourceState()
        self.embedder = CountingEmbedder(dimensions=64)
        self.loader = _Splitter()
        self.sync = SyncSource(
            reader=self.reader,
            loader=self.loader,
            embedder=self.embedder,
            store=self.store,
            cache=self.cache,
            state=self.state,
        )


class _Splitter:
    """A deterministic splitter, so tests assert on chunking they control.

    The real one is LlamaIndex's and is exercised in its own test; using it here
    would make a chunk-count assertion depend on a tokeniser's opinion.
    """

    def split_text(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if line.strip()]

    def load_file(self, filename: str, content: bytes, mimetype: str) -> list[str]:
        return [line for line in content.decode().splitlines() if line.strip()]


# --- the acceptance criteria ------------------------------------------------


async def test_a_second_sync_with_no_changes_makes_no_embedding_calls() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Deco Addict"), partner(2, "Gemini Furniture")]})

    first = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)
    assert first.ingested == 2
    assert first.embedding_calls > 0

    second = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert second.examined == 2
    assert second.unchanged == 2
    assert second.ingested == 0
    assert second.embedding_calls == 0
    assert not second.changed


async def test_changing_one_record_updates_exactly_its_chunks() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Deco Addict"), partner(2, "Gemini Furniture")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)
    before = dict(pipeline.store.chunks)

    later = NOW + timedelta(minutes=5)
    pipeline.reader.set_records(
        PARTNER,
        [
            partner(1, "Deco Addict", written=later, city="Brussels", phone="+32 2 555 0100"),
            partner(2, "Gemini Furniture"),
        ],
    )
    report = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert report.ingested == 1
    assert report.unchanged == 1
    # Exactly one document per record, still: the previous version was removed
    # in the same transaction that wrote the new one.
    assert pipeline.store.chunk_count("res.partner", 1) > 0
    assert len(pipeline.store.documents) == 2
    unchanged_hash = next(
        source_hash
        for source_hash, document in pipeline.store.documents.items()
        if document.res_id == 2
    )
    assert pipeline.store.chunks[unchanged_hash] == before[unchanged_hash]


async def test_the_old_version_of_a_record_is_gone_not_merely_added_to() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Deco Addict", city="Brussels")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    pipeline.reader.set_records(
        PARTNER, [partner(1, "Deco Addict", written=NOW + timedelta(minutes=1))]
    )
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    stored = [document for document in pipeline.store.documents.values() if document.res_id == 1]
    assert len(stored) == 1
    every_chunk = [chunk.content for chunks in pipeline.store.chunks.values() for chunk in chunks]
    assert not [chunk for chunk in every_chunk if "Brussels" in chunk]


# --- cost -------------------------------------------------------------------


async def test_repeated_text_is_embedded_once_across_records() -> None:
    # The same note on two contacts: the second costs a cache lookup, not a call.
    note = "Payment terms are 30 days from invoice date."
    pipeline = Pipeline(
        {PARTNER: [partner(1, "Alpha", comment=note), partner(2, "Beta", comment=note)]}
    )

    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert pipeline.embedder.texts.count(f"Notes: {note}") == 1


async def test_a_changed_record_only_pays_for_the_segments_that_moved() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha", comment="Unchanged note", city="Paris")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)
    pipeline.embedder.texts.clear()

    pipeline.reader.set_records(
        PARTNER,
        [
            partner(
                1,
                "Alpha",
                written=NOW + timedelta(minutes=1),
                comment="Unchanged note",
                city="Lyon",
            )
        ],
    )
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert "Notes: Unchanged note" not in pipeline.embedder.texts
    assert "City: Lyon" in pipeline.embedder.texts


async def test_reindex_pays_again_on_purpose() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Deco Addict")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    report = await pipeline.sync.run(PARTNER, kind=JobKind.REINDEX)

    assert report.ingested == 1
    assert report.unchanged == 0


async def test_reindex_still_reuses_the_cache_for_identical_text() -> None:
    # Re-indexing exists for a model change, and the cache is keyed by model, so
    # a same-model reindex should not re-embed. It rebuilds; it does not re-buy.
    pipeline = Pipeline({PARTNER: [partner(1, "Deco Addict")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    report = await pipeline.sync.run(PARTNER, kind=JobKind.REINDEX)

    assert report.embedding_calls == 0
    assert report.cached_segments > 0


# --- incremental ------------------------------------------------------------


async def test_the_watermark_advances_and_narrows_the_next_read() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha"), partner(2, "Beta")]})

    await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)

    assert await pipeline.state.watermark(PARTNER) == NOW
    pipeline.reader.reads.clear()
    await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)
    assert pipeline.reader.reads[0]["since"] == NOW


async def test_an_incremental_sync_reads_only_what_moved() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha"), partner(2, "Beta")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)

    later = NOW + timedelta(hours=1)
    pipeline.reader.set_records(
        PARTNER, [partner(1, "Alpha"), partner(2, "Beta renamed", written=later)]
    )
    report = await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)

    assert report.examined == 1
    assert report.ingested == 1
    assert await pipeline.state.watermark(PARTNER) == later


async def test_a_full_sync_ignores_the_watermark_but_not_the_hash() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha"), partner(2, "Beta")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)

    report = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert report.examined == 2
    assert report.unchanged == 2
    assert report.embedding_calls == 0


async def test_specific_record_ids_are_synced_whatever_the_watermark_says() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha"), partner(2, "Beta")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.INCREMENTAL)

    report = await pipeline.sync.run(PARTNER, kind=JobKind.REINDEX, record_ids=[2])

    assert report.examined == 1


async def test_paging_reads_every_record() -> None:
    records = [partner(index, f"Contact {index}") for index in range(1, 26)]
    pipeline = Pipeline({PARTNER: records}, page_size=10)

    report = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert report.examined == 25
    assert report.ingested == 25


# --- deletion, attachments, failure -----------------------------------------


async def test_deleting_a_record_removes_it_from_the_corpus() -> None:
    pipeline = Pipeline({PARTNER: [partner(1, "Alpha")]})
    await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    report = await pipeline.sync.delete_records(PARTNER, [1])

    assert report.deleted == 1
    assert not pipeline.store.documents


async def test_an_unchanged_attachment_is_never_downloaded() -> None:
    """The checksum stands in for the content, so a 40 MB contract stays put."""
    record = SourceRecord(
        res_model="ir.attachment",
        res_id=9,
        values={
            "id": 9,
            "name": "terms.txt",
            "mimetype": "text/plain",
            "checksum": "abc123",
            "write_date": NOW,
        },
        write_date=NOW,
        company_id=1,
    )
    pipeline = Pipeline({ATTACHMENT: [record]}, files={9: b"Refunds within 30 days.\n"})

    first = await pipeline.sync.run(ATTACHMENT, kind=JobKind.FULL_SYNC)
    assert first.ingested == 1

    downloads_before = len(pipeline.reader.reads)
    second = await pipeline.sync.run(ATTACHMENT, kind=JobKind.FULL_SYNC)

    assert second.unchanged == 1
    assert second.embedding_calls == 0
    # One read for the page, and no call for the file itself.
    assert len(pipeline.reader.reads) == downloads_before + 1


async def test_an_order_indexes_its_line_items() -> None:
    order = SourceRecord(
        res_model="sale.order",
        res_id=5,
        values={
            "id": 5,
            "name": "S00005",
            "partner_id": [3, "Deco Addict"],
            "amount_total": 1500.0,
            "order_line": [
                {"name": "Desk Combination", "product_uom_qty": 3.0, "price_subtotal": 1500.0}
            ],
            "write_date": NOW,
        },
        write_date=NOW,
        company_id=1,
    )
    pipeline = Pipeline({SALE_ORDER: [order]})

    await pipeline.sync.run(SALE_ORDER, kind=JobKind.FULL_SYNC)

    text = "\n".join(chunk.content for chunks in pipeline.store.chunks.values() for chunk in chunks)
    assert "Desk Combination" in text
    assert "Deco Addict" in text


async def test_a_record_with_nothing_but_an_id_is_still_findable() -> None:
    """An empty record costs one segment, not zero and not a page.

    The template falls back to "Contact 1" rather than rendering nothing, so a
    record somebody has not filled in yet can still be found by the reference
    they do have. The cost of that is one short chunk, which is the right trade.
    """
    empty = SourceRecord(
        res_model="res.partner",
        res_id=1,
        values={"id": 1, "display_name": "", "write_date": NOW},
        write_date=NOW,
    )
    pipeline = Pipeline({PARTNER: [empty]})

    report = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert report.examined == 1
    assert report.chunks_written == 1
    assert pipeline.embedder.texts == ["Contact: Contact 1"]


async def test_two_records_that_render_identically_do_not_collide() -> None:
    """The hash carries identity, not just content.

    Two contacts with the same name and nothing else filled in render to the
    same text. Hashing content alone would make the second overwrite the first,
    and one of them would silently vanish from the index.
    """
    pipeline = Pipeline({PARTNER: [partner(1, "Acme"), partner(2, "Acme")]})

    report = await pipeline.sync.run(PARTNER, kind=JobKind.FULL_SYNC)

    assert report.ingested == 2
    assert len(pipeline.store.documents) == 2
