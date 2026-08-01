# Architecture Decision Records

An **Architecture Decision Record (ADR)** captures a single significant decision,
the context that forced it, and the consequences we accepted. We keep them in the
repository so the reasoning travels with the code instead of evaporating in chat
logs and stand-ups.

## Why we bother

Six months from now, someone (possibly you) will look at `PgVectorStore` and ask
*"why didn't they just use LangChain's built-in one?"*. Without an ADR the only
honest answer is a shrug, and the team either re-litigates the decision or
cargo-cults it. An ADR converts tribal knowledge into a reviewable artefact.

## Format

We use [Michael Nygard's template](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions):

| Section | Purpose |
| --- | --- |
| **Status** | `Proposed` / `Accepted` / `Deprecated` / `Superseded by ADR-XXXX` |
| **Context** | The forces at play. Written so a newcomer understands the pressure. |
| **Decision** | What we chose, in the active voice: *"We will …"* |
| **Consequences** | What becomes easier **and** what becomes harder. Both are mandatory. |
| **Alternatives considered** | What we rejected and the specific reason. |

## Rules

1. **ADRs are immutable.** A decision that changes gets a *new* ADR whose status is
   `Accepted`, and the old one is marked `Superseded by ADR-XXXX`. We never rewrite
   history — the wrong turns are part of the value.
2. **One decision per record.** If the title needs an "and", it is two ADRs.
3. **Numbered monotonically**, four digits, never reused.
4. **Consequences must include costs.** An ADR listing only benefits is marketing,
   not engineering.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-sidecar-service-topology.md) | Run the AI engine as a sidecar service, not inside Odoo | Accepted |
| [0003](0003-rag-framework-selection.md) | Use LlamaIndex as an infrastructure adapter behind framework-agnostic ports | Accepted |
| [0004](0004-vector-store-and-index-strategy.md) | pgvector in a dedicated database, HNSW + GIN hybrid indexes | Accepted |
| [0005](0005-model-provider-strategy.md) | Provider-agnostic model layer with split chat/embedding vendors | Accepted |
| [0006](0006-data-access-and-authorization.md) | Odoo is the authorization authority; tool-calling over text-to-SQL | Accepted |
| [0007](0007-licensing.md) | LGPL-3.0-or-later for the whole repository | Accepted |
