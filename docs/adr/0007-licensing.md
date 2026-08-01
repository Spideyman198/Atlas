# ADR-0007: LGPL-3.0-or-later for the whole repository

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Core team

## Context

Odoo Community Edition is licensed **LGPL-3.0**. An Odoo addon imports Odoo's Python
modules, subclasses its `models.Model`, and is loaded into Odoo's process. Under any
reasonable reading of the LGPL, `odoo_atlas` is a work based on Odoo and must be
distributed under LGPL-3.0 or a compatible licence. Odoo's own App Store rules
require Community addons to declare an OSI licence compatible with LGPL-3.

The repository is not uniform, though:

- `addons/odoo_atlas/` — imports `odoo`. Derivative work. Constrained.
- `services/atlas/` — a standalone Python service. Imports nothing from Odoo (an
  invariant we enforce in CI, per [ADR-0002](0002-sidecar-service-topology.md)).
  Legally unconstrained.
- `docs/`, `evaluation/`, `docker/` — unconstrained.

So there is a genuine choice: license the parts differently, or pick one licence for
everything.

## Decision

The entire repository is licensed **LGPL-3.0-or-later** (SPDX: `LGPL-3.0-or-later`).

- `LICENSE` contains the LGPL-3.0 text.
- `COPYING` contains the GPL-3.0 text, which LGPL-3.0 incorporates by reference. Both
  are required for the licence to be complete.
- `addons/odoo_atlas/__manifest__.py` declares `"license": "LGPL-3"`, the value Odoo
  recognises.
- Source files carry a one-line SPDX identifier rather than a full header block.

**Why LGPL rather than AGPL.** AGPL-3.0 is the other common choice for Odoo addons
(much of the OCA uses it) and would arguably be the more "protective" pick, since
Atlas is partly a network service. It is rejected deliberately: AGPL's network clause
means any company running a modified Atlas as part of an internally-hosted ERP could
be obliged to publish their modifications. For a component meant to be **embedded in
someone else's ERP deployment**, that is a genuine adoption blocker — legal teams
routinely ban AGPL dependencies outright. LGPL keeps the copyleft where it matters
(modifications to Atlas itself must stay open) without infecting the surrounding
deployment.

**Why not split licences.** Dual-licensing the service permissively (MIT/Apache-2.0)
while the addon stays LGPL is legally clean and was considered seriously. Rejected on
practical grounds: it invites per-directory confusion, complicates contribution
terms, and the ambiguity costs more than the flexibility buys. One licence, one
answer.

**Why not Apache-2.0 for everything.** Not available to us. The addon is a derivative
of LGPL-3.0 code; we cannot relicense it permissively.

## Consequences

**Easier**

- Unambiguous compatibility with Odoo CE and with the Odoo App Store's Community
  requirements.
- Companies can deploy and even modify Atlas internally without disclosure
  obligations on their own ERP customisations — the adoption path stays open.
- Contributors know exactly what they are agreeing to; no CLA needed for an
  inbound=outbound LGPL project.

**Harder**

- **Dependency licences must stay compatible.** Anything GPL-3.0-only or AGPL pulled
  into the service would force the whole work up to that licence. M14 adds a CI
  licence-compliance check (`pip-licenses` with an allow-list) so this is caught at
  PR time rather than at release.
- LGPL is longer and less familiar than MIT, which is a small friction for casual
  contributors. Mitigated by stating the practical effect plainly in `README.md` and
  `CONTRIBUTING.md`.
- Relicensing later requires the agreement of every contributor. Accepted; the
  `-or-later` suffix at least allows moving to a future LGPL version.

## Alternatives considered

**AGPL-3.0-or-later.** The OCA-conventional choice and stronger copyleft. Rejected on
the adoption argument above — the network clause is a poor fit for a component
deployed inside a customer's own infrastructure.

**MIT / Apache-2.0.** Rejected: not legally available for the addon, which is a
derivative of LGPL-3.0 Odoo code.

**Split licensing (LGPL addon, Apache service).** Rejected for practical clarity, not
legal reasons. Revisitable if the service is ever extracted into a genuinely
standalone product.

**Proprietary / source-available (BSL, PolyForm).** Rejected: incompatible with the
addon's derivative status, and contrary to the project's purpose as an open portfolio
and community contribution.
