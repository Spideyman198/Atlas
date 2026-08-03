# Orchestration

How a question becomes an answer, and what stops it becoming a plausible one.

```
   question
      │
      ▼
  ┌─────────────────┐   refuse ──────────────────────────────┐
  │ 1. ROUTE        │   empty question, or asks for a change │
  └────────┬────────┘                                        │
           │ structured / semantic / hybrid                  │
           ▼                                                 │
  ┌─────────────────┐                                        │
  │ 2. GATHER       │  documents (retrieve → authorize)      │
  │                 │  tools (offered, not yet called)       │
  └────────┬────────┘                                        │
           │                                                 │
     nothing at all? ───────────────────────────────────────▶│
           │                                                 ▼
           ▼                                            refusal
  ┌─────────────────┐
  │ 3. GENERATE     │  system + context + question
  │                 │  tool calls execute and loop back
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ 4. RESOLVE      │  markers → citations; invented ones removed
  └────────┬────────┘
           ▼
        answer
```

## Refusal when there is nothing to ground on

If no documents were retrieved and no tool is available, the model is not
called. Not called and ignored — not called at all.

There is no prompt wording that turns a generation from nothing into something
other than a guess. The failure mode this prevents is specific: asked "how much
does Acme owe us?" with an empty corpus, a model will produce a figure, an
invoice count and a date, in the register of someone reading it off a screen.
Nobody reading that output can tell it apart from an answer.

This is the M10 acceptance criterion, and it is enforced in
`application/synthesis.py` rather than hoped for in a template. The tests script
the provider to fabricate, so the refusal can only come from the orchestrator —
a test where the model behaves well proves nothing about the orchestrator.

Two cases fall under it that are easy to miss:

- **Authorization emptied the context.** The documents exist; they are not this
  user's to read. Identical treatment to finding nothing, and the answer must
  not reflect them.
- **A live question with no tools.** A provider that cannot call tools, asked
  what is in stock, has nothing but its own imagination. Refused.

What is *not* a refusal: an empty document search when tools are available. A
tool is grounding.

## Routing

Rules for the questions a rule can genuinely recognise; hybrid for everything
else.

| Route | Fetches | Fires on |
| --- | --- | --- |
| `structured` | Tools | totals, stock, debt, a record reference like `S00042` |
| `semantic` | Documents | policy, contract, manual, "what does it say" |
| `hybrid` | Both | signals for both, or no confident signal |
| `refuse` | Nothing | an empty question, or a request to change data |

The asymmetry is the design. A wrong route costs latency; hybrid costs latency
too and cannot be wrong for it, so no rule fires on a question it cannot
actually recognise. "Tell me about Acme" is hybrid, not a guess.

Routing decides what is fetched, never what is permitted. Every path ends at the
same authorization stage ([ADR-0006](adr/0006-data-access-and-authorization.md)),
so a misrouted question reaches nothing its asker could not already see.

Asking a model to route would be more accurate on the ambiguous middle and would
add a round-trip to every question, including the obvious ones, to choose
between paths that mostly overlap. M12 will have numbers; the trade should be
made then rather than guessed at now.

### Write requests

"Delete order S00042" is refused before anything is fetched. The alternative is
an assistant that searches, finds the order, and then explains it cannot do the
thing — which reads as a system that nearly did it.

The distinction between asking *for* a change and asking *about* one is
imperfect on purpose: "which orders were cancelled?" is a question, "cancel the
order" is not, and the test is whether it reads as a question. A false refusal is
visible and fixed by rephrasing. The failure in the other direction is not.

## Prompts

Templates live in `infrastructure/prompts/templates/` and are rendered behind a
port, so the application layer never imports Jinja.

**Versions are content hashes.** A hand-maintained version number gets forgotten
on the one edit that mattered, and then the logs claim two different answers came
from the same prompt. `system@84298b4212b1` cannot go stale: change a word and it
changes. Every answer records the identity of the prompt that produced it.

The system prompt carries requirements, not preferences — grounding, citing,
treating retrieved text as data, read-only. Each has a test asserting it is still
there, so losing one in an edit fails a test rather than surfacing as a wrong
answer months later.

## Prompt injection

Retrieved content is quoted material. It goes inside a fence:

```
<atlas:context>
[1] Sales Order S00001
...
</atlas:context>
```

and the system prompt says that everything between the markers is the contents of
a database field, that some of it may look like instructions addressed to the
assistant, and that instructions come only from the person asking.

That instruction is worth exactly as much as the fence is hard to forge. So **no
rendered variable may contain the marker**: the prompt library replaces any
occurrence with a visible `[removed: context marker in retrieved text]` before
rendering, recursively through the containers a template variable is built from.
A document cannot close the fence and start issuing orders.

Two other properties, both asserted rather than assumed:

- Jinja does not evaluate the *contents* of a variable, so `{{ 7 * 6 }}` in a
  customer's notes stays literal.
- The question is sanitised on the same terms. The person asking is more trusted
  than a document, but not unlimited.

This is defence in depth, not a solved problem. A sufficiently persuasive
paragraph inside a legitimately retrieved record can still influence an answer.
What it cannot do is escape its quoting or forge a block that was never there.

## Citations

The model writes `[2]`. The orchestrator decides whether block 2 existed.

Citations are built from the blocks that demonstrably entered the prompt, and a
marker naming a block that was not there is **removed from the answer text**. A
reference a reader cannot follow is worse than no reference: it looks like
evidence.

Citations come back ordered by block number, matching the markers in the text, so
the list under an answer can be scanned by number rather than read through.

## Tool loop

The model may call tools, read the results, and call more. Bounded at five
rounds — a model that keeps calling tools never answers, and the bound turns that
into a slightly worse answer rather than a request that never returns.

A rejected tool call comes back as a *result*, not an exception: the addon's
rejection messages name what was wrong and what was allowed instead, and current
models correct themselves well when told. An unreachable Odoo is not softened
that way; no retry fixes it. See [tools.md](tools.md).

## Conversation memory

Recent turns go in verbatim; older ones are replaced by a summary once they cross
a token budget. The split is by size rather than turn count, because one turn
quoting a sales report is worth twenty short ones.

Summarising costs a model call, on the turn that crosses the budget rather than
every turn. If it fails, the history is dropped and the answer proceeds: a
slightly worse answer beats no answer, which is what refusing over a failed
summary would produce.

The history budget is deliberately smaller than the retrieval budget. History
competes with the context that grounds the answer, and grounding wins.

## Endpoint

```
POST /v1/chat
{
  "question": "which invoices are overdue?",
  "context_token": "v1....",
  "history": [{"question": "...", "answer": "..."}],
  "conversation_id": 12
}
```

Responds `text/event-stream`. Streaming is not decoration: a grounded answer
means a search, an authorization round-trip, and often two model calls with a
tool execution between them.

| Event | Payload |
| --- | --- |
| `delta` | `{"text": "..."}` — text as it is generated |
| `done` | the whole answer, citations, intent, tools called, usage, prompt version |
| `error` | `{"message": "..."}` |

**A 200 does not mean it worked.** Once the first byte is out the status line is
gone, so a failure arrives as an `error` event. A client must read events rather
than trust the status.

The context token is minted by Odoo and passed straight through; the engine never
inspects it (ADR-0006). `X-Request-ID` is adopted as the trace id when supplied,
so one id spans the addon, the engine and Odoo's access log.

## Known limitations

- **Relevance is the model's judgement.** Retrieval returns its best candidates
  whether or not they are any good, and fusion scores are not calibrated, so
  there is no threshold to filter on. Asked about a refund policy with only
  customer records indexed, the orchestrator proceeds and the prompt is what
  makes the model say it does not have the information. M12 measures how often
  that holds.
- Answers are not persisted here. Storing a turn on `atlas.conversation` is
  M11's job.
- No cross-encoder rerank, so context is fusion order plus diversity.
