# ADR-0009: Defer ordering for the `aggregate` tool to a minor release

- **Status:** Accepted
- **Date:** 2026-08-04
- **Deciders:** Core team

## Context

The `aggregate` tool groups records and returns one row per group. Its
description, which the model reads when choosing a tool, says:

> Use this for any question about how much, how many, which is the biggest, or a
> trend over time.

The handler passes no `order` to `_read_group` and caps the result at
`catalog.MAX_ROWS`, currently 50:

```python
rows = model._read_group(domain, groupby=group_by, aggregates=aggregates, limit=limit)
```

Rows therefore come back in whatever order Odoo's default ordering on the
grouping field produces, and anything past the fiftieth group is discarded. For
"how much" and "how many" that is harmless — the model reads the row it wants.
For "which is the biggest" it is not:

- With 50 groups or fewer, every candidate is present and the model has to scan
  and compare them itself.
- With more than 50, the largest group can be dropped before the model ever sees
  it. The answer is then confidently wrong, with no signal that anything was
  truncated.

This surfaced during the first live-provider run. Asked which customer had the
most sale orders, the engine called `aggregate`, received all five groups
including the correct answer at 17, and reported a different customer with 2. The
tool returned complete and correct data; the model misread it. The demo database
has five groups, so truncation was not involved — but the same question against a
database with more customers than the cap would fail for a second, independent
reason that no amount of model quality can recover from.

Two distinct problems sit here, and only one of them is ours:

1. The model did not pick the maximum from rows it was given. That is model
   behaviour, addressable — if at all — through the prompt or the tool
   description.
2. The tool advertises an ordering-sensitive capability while providing no
   ordering and silently truncating. That is ours.

Fixing (2) properly means letting the caller order by a measure — a new property
on the tool's parameter schema, and a change in what an unchanged call returns.
The tool schema is part of the public surface frozen at 1.0.0, and
[docs/upgrading.md](../upgrading.md) covers it under semantic versioning. A patch
release may not change it.

## Decision

We will leave the `aggregate` tool's schema and behaviour unchanged in the 1.0.x
series, and record the limitation here rather than fix it quietly.

Ordering will be added in a minor release, as an optional `order_by` naming a
measure or grouping field, with the row cap kept and a signal in the result when
truncation occurred. Silent truncation is the part that turns a limitation into a
wrong answer, so a caller must be able to tell that it happened.

Until then, the tool's description stays honest about what it does. Removing the
"which is the biggest" phrasing is itself a change to what the model chooses and
belongs with the fix, not ahead of it.

## Consequences

**Easier.** The 1.0.x line stays a pure bug-fix series: 1.0.1 ships two adapter
repairs with no behavioural change anywhere, and anyone upgrading within the
series can do so without re-reading their tool traces. The limitation is written
down, so the next person to see a wrong superlative finds the explanation instead
of re-deriving it.

**Harder.** Superlative questions over more than 50 groups stay wrong until the
minor release, and wrong in the worst way — a plausible answer with no warning
attached. That is a real defect being knowingly carried. Anyone running Atlas
against a database with many customers, products or salespeople is exposed to it
now, and the only mitigation available is not to trust "which is the biggest"
without checking. Deferring also means the eventual fix arrives with a schema
change, so it cannot be back-ported to 1.0.x for people who cannot take a minor
upgrade.

## Alternatives considered

**Add `order_by` in 1.0.1.** Rejected. It adds a property to a published tool
schema and changes the rows an unchanged call receives. Both are minor-version
changes under the policy this project committed to eight days ago, and a patch
release that quietly redefines a tool is exactly the failure the policy exists to
prevent.

**Order by the first measure by default, without a schema change.** Rejected on
the same grounds and more so: it changes results for every existing caller while
leaving the schema looking identical, which is the least inspectable form of a
breaking change. It also guesses at intent — the useful order for "revenue by
customer" is not the useful order for "orders by month".

**Raise `MAX_ROWS` so truncation is rarer.** Rejected. It makes the failure less
frequent and no less silent, moves more data into the prompt on every call, and
leaves the model still scanning an unordered list. The cap is not the root cause.

**Fix the description only, dropping "which is the biggest".** Rejected as a
standalone step. It would steer the model away from the tool for exactly the
questions the tool should eventually answer well, and the wording needs to be
decided together with the capability it describes.
