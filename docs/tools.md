# Tools

How a question about live data gets answered without text-to-SQL.

Retrieval answers questions about documents. It cannot answer "where is order
S00042" — the answer changes hourly, and an embedding of last week's state is
worse than no answer. Those questions go to a tool, which reads Odoo through the
ORM, as the person who asked.

```
   model                          Odoo
     │  {"name": "find_records",
     │   "arguments": {...}}
     ├──────────────────────────────▶ 1. is this tool in the registry?
     │                                2. is the model in the allow-list?
     │                                3. may this user read it?
     │                                4. compile filters → domain
     │                                5. search_read, as this user
     │◀───────────────────────────────── rows, or a rejection in words
     │
     └─ retry with better arguments, or answer
```

Steps 2 to 4 are this document. Step 5 is
[ADR-0006](adr/0006-data-access-and-authorization.md): every tool runs in the
acting user's environment, so record rules apply without the tools doing
anything to make that happen. There is no `sudo()` anywhere in the addon, and a
test scans for it.

## The five tools

| Tool | Answers | Backed by |
| --- | --- | --- |
| `find_records` | Which records match these conditions | `search_read` |
| `aggregate` | How much, how many, which is biggest | `_read_group` |
| `stock_levels` | What is on hand and where | `stock.quant` grouped by product |
| `overdue_invoices` | Who owes us money and since when | `account.move`, fixed domain |
| `customer_360` | Everything about one customer | partner plus related blocks |

`overdue_invoices` and `stock_levels` are both expressible as `find_records`
with the right filters. They exist anyway. Asking a model to get
`move_type in ('out_invoice', 'out_refund')` and
`payment_state not in ('paid', 'reversed')` right every single time is how you
get a confident answer about vendor bills. A fixed domain for a question people
actually ask is worth more than one more parameter on a general tool.

A tool is only offered if its models are installed **and** the acting user can
read them. A user without sales rights is never told `overdue_invoices` exists,
so it cannot be called and then refused — which reads to a model as an obstacle
to route around.

### Known limitation: `aggregate` does not order its rows

`aggregate` passes no ordering to `_read_group` and caps the result at 50 rows.
Groups come back in Odoo's default order for the grouping field, and anything
past the cap is dropped with nothing in the result to say so.

For "how much" and "how many" this does not matter. For "which is the biggest" —
which the tool's own description invites — it does: above 50 groups the largest
can be truncated before the model sees it, and the answer is then wrong with no
signal attached. Below the cap the model has every candidate but must compare
them itself, which it does not always get right.

Treat a superlative over a large grouping as unverified until this is fixed.
[ADR-0009](adr/0009-defer-aggregate-ordering.md) records why the fix waits for a
minor release: adding an order parameter changes a published tool schema, which
a patch release may not do.

## Why the model does not emit domains

An Odoo domain is a small programming language: nested boolean operators, dotted
traversal across relations, and operators whose meaning depends on the model.

```python
[("partner_id.user_id.login", "ilike", "admin")]
```

That is a valid domain. It walks two relations to a field nobody put on any
allow-list. Record rules would still apply to the *records* returned, but the
query shape is unbounded, and "the model can only ask things we chose" is a
property worth having on its own.

So the model does not emit domains. It emits objects:

```json
{"field": "amount_total", "operator": ">=", "value": 1000}
```

and `services/tools/filters.py` compiles them, after checking that the field is
on the model's allow-list, the operator is one of eleven, and the value's type
suits the field. Anything else is refused. The set of expressible queries is one
we chose rather than one we inherited.

## The allow-list

`services/tools/catalog.py` names eight models, and for each of them:

- `fields` — readable and filterable
- `measures` — numeric fields that may be summed
- `groupable` — fields that may be grouped by, kept separate because grouping by
  a free-text column returns one group per record, which is a way to page
  through a table sideways
- `requires_module` — so an uninstalled module means the tool is absent, not
  broken

Adding a field is a deliberate act with a diff attached. That is the intended
cost. The alternative — reflecting over the model and exposing whatever is
there — exposes `password`, internal state fields, and computed fields whose
meaning differs from their name.

Caps are in the same file: 50 rows, 12 filters, 50 values in an `in` list, 200
characters of text, 3 grouping levels. A model that asks for ten thousand rows
will not read them and the context window cannot hold them.

## Rejected inputs

Every rule below was added because a test produced the failure, not because
somebody imagined it.

| Input | What happened without the rule |
| --- | --- |
| `partner_id.user_id.login` | Traversal to a field on no allow-list |
| `id in ["Brussels"]` | `invalid input syntax for type integer` from PostgreSQL |
| `id > None` | Reaches PostgreSQL as `id > false`; `operator does not exist` |
| `is_company ilike "Brussels"` | `ValueError` inside the ORM's domain optimiser |
| `list_price = True` | Silently means `1`, and matches the wrong rows quietly |
| `create_date = 20260801` | Matches nothing, which reads as "there are none" |

The last one is the pattern worth noticing. A rejected call costs a retry. A
call that quietly matches nothing costs a wrong answer delivered confidently,
and nobody finds out.

`tests/test_tool_filters.py` enumerates the cross product of fields, operators
and values — including the ones a model produces by mistake — compiles each one,
and executes everything that compiled. The invariant: either a clause names an
allow-listed field with an allow-listed operator and the ORM accepts it, or
nothing compiles at all. A domain the ORM rejects is a 500 where a rejection was
wanted. Three of the six rows above came from that test.

## Error handling

A rejected tool call comes back to the model as a tool *result*, not an
exception:

```
'colour' is not filterable on res.partner. Allowed: category_id, city,
country_id, create_date, display_name, email, ...
```

Current models correct themselves well when told exactly what was wrong and what
was allowed instead, and raising instead would abort a request that was one
retry away from working. The messages are written to be read.

Two things are not softened, because no retry fixes them: an unreachable Odoo,
and a context Odoo refuses. Those propagate.

## Results

Tool results are JSON, and are JSON *before* they leave the process — dates
render as ISO strings and many2one fields as `[id, name]` pairs, rather than
relying on the HTTP layer's encoder. A tool called in a test therefore returns
exactly what a tool called over the wire returns.

The `[id, name]` pair is kept rather than flattened to a name: both halves are
valid filter values, and the id is what a follow-up call needs.

Row counts are reported honestly. `find_records` returns `matched` alongside
`returned` and a `truncated` flag, because a model given the first page with no
caveat will present it as the whole answer. `overdue_invoices` names its total
`outstanding_in_returned_rows` for the same reason.

## Read-only

Nothing in `services/tools/` calls `create`, `write`, `unlink`, `copy`,
`sudo` or `execute`. `tests/test_tools.py` scans the package for all six and
fails on any of them, and a further test checks the scan would actually catch
them. Write operations are a post-1.0 milestone with explicit human
confirmation, not something that arrives behind a helpful-looking argument.

## Adding a tool

1. Add a `ModelSpec` to `catalog.py` if the model is new.
2. Write a handler taking `(env, arguments)` and returning a JSON-able dict.
   Use `env` as given — never `sudo()`, never `with_user`.
3. Wrap it in a `Tool` with a JSON Schema that sets
   `"additionalProperties": false`, and a description that says *when to use it*,
   not just what it does. A description that only states a capability
   under-triggers the tool.
4. Add it to `TOOLS` in `handlers.py`.
5. Add tests: the happy path, one refusal, and one showing two users get
   different answers.
