# Ingestion

How ERP records become searchable text, and what that costs.

This is the cold path. It runs on a schedule, nobody waits for it, and almost
every design decision in it is about not paying twice for the same embedding.

## The shape of it

```
ir.cron ──▶ addon ──▶ POST /v1/ingest/sync ──▶ ingest_jobs (queued, returns)
                                                    │
                                          atlas-worker claims one
                                                    │
   ┌────────────────────────────────────────────────┘
   ▼
read a page of records, as the integration user
   ▼
render to text through the source template
   ▼
hash  ──▶ already stored?  ──▶ yes: skip. No provider call. No download.
   ▼ no
split into segments
   ▼
per segment: cached?  ──▶ yes: reuse the vector
   ▼ no
embed the outstanding ones in one batched call
   ▼
replace the record's document and chunks, in one transaction
   ▼
advance the watermark
```

Two properties fall out of that ordering, and both are tested:

- **An idle sync makes no provider calls.** The hash check happens before the
  embedding call, not after. This is the difference between a cron job that
  costs cents a day and one that costs dollars an hour.
- **Changing one record updates exactly that record's chunks.** Delete and
  replace inside one transaction, so retrieval never sees a half-rewritten
  document or two versions of the same order.

## What gets indexed

Eight sources, declared in `atlas.domain.sources` as data rather than code —
which model, which fields, what label each one carries in the output.

| Source | Odoo model | Needs |
| --- | --- | --- |
| `odoo.res.partner` | `res.partner` | base |
| `odoo.product.template` | `product.template` | base |
| `odoo.ir.attachment` | `ir.attachment` | base |
| `odoo.crm.lead` | `crm.lead` | `crm` |
| `odoo.sale.order` | `sale.order` | `sale` |
| `odoo.purchase.order` | `purchase.order` | `purchase` |
| `odoo.account.move` | `account.move` | `account` |
| `odoo.stock.quant` | `stock.quant` | `stock` |

A source whose module is not installed reports itself unavailable rather than
failing a sync halfway through. The addon declares no dependency on any of them
(ADR-0002): the models are read by name over HTTP.

Sources are **off by default**. Indexing costs money, so somebody has to say
which of it is worth spending.

### Rendering decides what is findable

A chunk is only retrievable if it contains the words somebody would search for,
so the output is labelled prose rather than a field dump:

```
Sales Order: S00005
Customer: Deco Addict
Order date: 2026-08-01
Status: Sales Order
Total: 4,500.00
Order lines:
  - 3 x Desk Combination = 1,500.00
  - 1 x Office Chair = 120.50
```

Three details that matter more than they look:

- **Selection values are labels, not keys.** Odoo stores `sale`; a person
  searches for "Sales Order". Odoo resolves the label before the row leaves it.
- **Order lines are indexed with the order.** The product names live on the
  lines, not the header, and they are most of what makes an order findable.
- **Zero is rendered.** `0 == False` in Python, and a naive emptiness check drops
  "Quantity on hand: 0" — which is exactly the stock level somebody asks about.

Fields a model does not have are dropped rather than failing the source, so a
template written against one module combination still works on another.

## What makes it cheap

**The content hash** carries the record's identity as well as its text.
Identity, because the column is unique and two contacts with the same name and
nothing else filled in would otherwise collide and overwrite one another. Text,
normalised for whitespace, so reformatting a template does not invalidate a
corpus that says exactly the same thing.

**Attachments are compared by checksum**, which Odoo already keeps. An unchanged
40 MB contract is skipped before it is ever downloaded; a changed one is
re-read.

**The embedding cache is keyed by segment and model**, not by document.
Boilerplate is everywhere in an ERP — the same delivery paragraph on every
order, the same description on every product variant — so a changed order
re-embeds the line that changed and not the eleven that did not. The model is
part of the key because two models produce incompatible vector spaces, and
sharing a cache between them would poison a corpus silently.

**The watermark** is the newest `write_date` already indexed. A 15-minute cron
reads the handful of records that moved. It only ever moves forward, so a full
sync finishing on older records cannot undo an incremental run's progress.

## The queue

`ingest_jobs` in PostgreSQL, claimed with `SELECT ... FOR UPDATE SKIP LOCKED`.
The row lock *is* the claim, released by the same commit that records the
outcome, so there is no window where a job is taken but not recorded as taken.
Two workers polling at the same instant get different jobs.

No broker, no scheduler, no second datastore. It is the single best argument for
already having PostgreSQL.

Failures back off exponentially. After `ATLAS_INGESTION__MAX_ATTEMPTS` tries a
job becomes **dead** — deliberately not `failed`, so "still trying" and "gave up"
are different queries. A worker that dies holding a job has it returned by the
stale sweep, and the attempt it burned is *not* refunded: a job that reliably
kills workers must still reach `dead` rather than crash-looping the pool.

## Who it reads as

Ingestion reads as a dedicated **integration user**, named by `ATLAS_INGEST_UID`
and holding `odoo_atlas.group_atlas_ingest`. It is an ordinary Odoo account: what
it may read is what Atlas may index, decided by Odoo's own access rules. There is
no `sudo()` on this path either.

That account sees more than any one person does — which is exactly why the
authorization step at query time cannot be skipped. **The index is deliberately
wider than any answer drawn from it** ([ADR-0006](adr/0006-data-access-and-authorization.md)).

Give it read access to what you want indexed and nothing else.

## Running it

From Odoo: **Atlas → Configuration → Configure Indexing** picks the sources and
starts the first run. The `ir.cron` job (off by default) queues an incremental
sync every 15 minutes.

From the command line, inside the engine container:

```bash
atlas sources
```

```bash
atlas sync --now --source odoo.res.partner
```

```bash
atlas reindex --source odoo.res.partner
```

```bash
atlas worker
```

`--now` runs inline and prints what it cost, which is the question an operator
actually has:

```
odoo.res.partner: examined 412, unchanged 409, ingested 3, chunks 7,
embedding calls 1, cached segments 4
```

`reindex` ignores the content hash and rebuilds every document. It is what to run
after changing embedding model, and the only mode that is expensive on purpose —
though it still reuses the cache when the model has not actually changed.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_INGESTION__PAGE_SIZE` | `100` | Records read from Odoo per round-trip |
| `ATLAS_INGESTION__CHUNK_SIZE` | `512` | Tokens per segment |
| `ATLAS_INGESTION__CHUNK_OVERLAP` | `64` | Tokens neighbouring segments share |
| `ATLAS_INGESTION__WORKER_POLL_SECONDS` | `5` | Idle poll interval |
| `ATLAS_INGESTION__MAX_ATTEMPTS` | `5` | Attempts before a job is dead |
| `ATLAS_INGESTION__RETRY_BACKOFF_SECONDS` | `30` | Base of the exponential backoff |
| `ATLAS_INGESTION__STALE_JOB_SECONDS` | `900` | Before a claimed job is assumed abandoned |
| `ATLAS_INGEST_UID` | — | Odoo side. The integration user's id |

Chunk size and overlap are retrieval strategy, not implementation detail, which
is why they are settings rather than constants in the adapter
([ADR-0003](adr/0003-rag-framework-selection.md)). An overlap at least as large
as the chunk never terminates, and is refused at start-up.

## Where LlamaIndex is

`atlas.infrastructure.llamaindex`, and nowhere else — an `import-linter`
contract fails the build otherwise. It supplies the sentence splitter and the
PDF and DOCX readers. Algorithms only; never transport and never storage.

The containment is falsifiable rather than asserted: delete LlamaIndex and the
only tests that fail are `test_document_loader.py`. Everything above the adapter
runs on fakes.

## Known limits

- **Deletions are pushed, not detected.** Odoo tells the engine what went away;
  nothing diffs id sets on a schedule. A missed delete is a citation that
  resolves to nothing, not a leak.
- **Chunks with no Odoo record behind them are dropped at query time** until the
  visibility-and-group check for uploads lands. Failing closed, but it means a
  source that is not record-backed is not yet useful.
- **Token counts on chunks are estimated** at four characters per token. Nothing
  correctness-bearing reads them; M10's context budgeting will want better.
- **Retrieval quality is still unmeasured.** M12 owns the golden set.
