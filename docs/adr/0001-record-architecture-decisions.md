# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

## Context

Odoo Atlas sits at the intersection of four domains with genuinely different
instincts:

- **Odoo/ERP**, where the ORM, record rules and upgrade path dominate every choice;
- **AI/RAG**, where the ecosystem churns every few months;
- **PostgreSQL**, where index and schema choices are expensive to reverse;
- **Platform engineering**, where deployment topology constrains everything above.

Decisions in this project therefore have long half-lives and cross-domain
consequences. A choice that looks like an implementation detail to the AI side
(*"we'll just query the Odoo tables directly"*) is a security incident to the ERP
side.

We also expect this repository to be read by people who did not write it —
reviewers, contributors, and hiring managers. Code shows *what*; it rarely shows
*why not*.

## Decision

We will record every architecturally significant decision as a numbered ADR in
`docs/adr/`, using the Nygard template described in [README.md](README.md).

A decision is "architecturally significant" if it is expensive to reverse, or if
it constrains other people's choices. Concretely, an ADR is required when the
change affects:

- deployment topology or the process boundary between components,
- the security or authorization model,
- the database schema, indexing strategy, or migration path,
- a dependency we would struggle to remove (framework, vendor SDK, protocol),
- a public contract (REST endpoint, Odoo model field, configuration key).

Routine choices — naming, file layout inside a module, which assertion helper to
use — do **not** get an ADR. They get a code review.

## Consequences

**Easier**

- Onboarding: a new contributor reads seven documents and understands the system's
  constraints, not just its shape.
- Code review: reviewers can challenge a decision on its recorded reasoning rather
  than on taste.
- Refactoring: when we revisit a choice, we can check whether the original forces
  still apply. Often they do not, and the ADR tells us so.

**Harder**

- Every significant PR carries a documentation cost. This is deliberate friction:
  if writing the ADR is hard, the decision is probably not understood well enough
  to merge.
- ADRs can rot if we edit them. Mitigated by the immutability rule — we supersede,
  never rewrite.

## Alternatives considered

**A wiki or Notion space.** Rejected: documentation that lives outside the
repository drifts from the code within one release cycle and is invisible during
code review. ADRs in-tree are versioned with the code they describe and show up in
`git log`.

**Long-form design docs per subsystem.** Rejected as the *primary* mechanism —
they answer "how does this work" rather than "why is it like this", and they are
rewritten rather than superseded, so the reasoning history is lost. We will still
write them (see `docs/architecture/`), but they complement ADRs rather than
replace them.

**Nothing; rely on commit messages.** Rejected: commit messages explain a change,
not a standing constraint, and nobody greps history to find out why the vector
store lives in a separate database.
