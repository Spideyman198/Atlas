# Security policy

## Reporting a vulnerability

Report privately through [GitHub's security advisory
form](https://github.com/Spideyman198/Atlas/security/advisories/new), not as a
public issue.

Include what you can: the version, what an attacker gains, and the smallest
reproduction you have. A report with a rough reproduction is worth more than a
polished one that arrives a month later.

What to expect:

| | |
| --- | --- |
| Acknowledgement | Within 3 working days |
| Initial assessment | Within 10 working days |
| Fix or a plan with dates | Within 30 days for high severity |

This is a small project without a paid security team. Those are targets, met on
best effort, and there is no bounty. If a deadline slips you will be told why
rather than left waiting.

Credit is given in the advisory and the changelog unless you ask otherwise.

## What counts as a vulnerability

**In scope**, and treated as high severity:

- Reading data the acting user's Odoo record rules do not permit. This is the
  property the whole design exists to hold
  ([ADR-0006](docs/adr/0006-data-access-and-authorization.md)).
- Any path that lets the engine act as a user other than the one that asked, or
  that lets a caller mint or forge a context token.
- Anything that writes to Odoo. This release reads and nothing else.
- Credential disclosure: tokens or API keys in logs, in answers, or in the
  `/metrics` output.
- Remote code execution, SQL injection, or a path traversal in either process.

**Out of scope**, with reasons rather than a bare list:

- **Prompt injection that changes an answer's tone or content without crossing
  an authorization boundary.** Injection is bounded, not solved — see
  [security.md](docs/security.md). A record that talks the assistant into a
  wrong summary of records the reader may already see is a quality problem. A
  record that talks it into reading someone else's data is in scope, and would
  be a serious finding.
- **An answer derived from records the user can legitimately read.** Atlas makes
  existing access easier to use. It does not narrow it, and it is not a data
  loss prevention product.
- **Direct access to the vector index by someone holding the database
  credentials.** The index has to be searchable across users for authorization
  to have anything to filter. Protect it like the Odoo database itself.
- **Denial of service beyond the per-user rate limit.** That is a reverse
  proxy's job.
- Vulnerabilities in Odoo, PostgreSQL, pgvector or a model provider. Report
  those upstream; tell us too if Atlas's usage makes them worse.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Security fixes |
| < 1.0 | No |

One supported line at a time. When 1.1 is released, 1.0 gets security fixes for
90 days and then stops. A project this size cannot honestly promise to backport
across several branches, and a support matrix nobody can honour is worse than a
short one that is true.

## What is scanned, and what that does not prove

Every pull request runs `pip-audit` for dependency vulnerabilities, gitleaks
over the full history, Trivy against the built image, and a licence check. A
weekly job runs the same audit against `main`, because a vulnerability disclosed
against a dependency that has not changed will never be caught by a
pull-request gate.

None of that is a substitute for a review. No third-party penetration test has
been carried out. The [threat model](docs/security.md) states what is defended
and, more usefully, what is not.
