# Evaluation and observability

How the assistant is measured, and what each number is worth.

## The golden set

`services/atlas/evaluation/` holds a corpus of twelve documents and nine
labelled questions. Both are files, not an Odoo export, so `make eval` produces
the same number twice on any machine. A real ERP is a better benchmark and a
worse gate: the corpus moves under you, and a metric that drops because somebody
confirmed an order says nothing about retrieval.

The corpus is deliberately confusable. Two customers share a first word, two
orders differ only in status, and the refund policy exists both as a document
and as a sentence inside an unrelated note. Retrieval that scores well on a
corpus of unrelated paragraphs has not been tested.

Labels are **document keys**, not chunk ids. Chunking is an implementation
detail that moves, and a golden set needing relabelling every time the chunk
size changes is one nobody maintains.

Every question carries a `note` saying why those documents and not others. When
a metric drops, the first question is whether retrieval got worse or the label
was wrong, and a set with no reasoning recorded cannot answer that. A test
enforces that the note is there.

## Three metrics, because each hides what the others show

| Metric | Answers | Blind to |
| --- | --- | --- |
| recall@k | Did the right documents get found at all | Where they ranked |
| MRR | How far down the first hit was | Everything after it |
| nDCG@k | Graded, position-weighted, comparable across questions | Nothing, but nobody can read it off |

Recall matters most: a chunk that never enters the candidate set cannot be
authorized, cannot be assembled and cannot be cited. Everything downstream is
bounded by it.

They are pure functions over ranked identifiers, tested against hand-written
rankings where the right answer is arithmetic. A test asserting "recall went up"
against a real retriever proves nothing about whether recall is computed
correctly.

## What the offline gate can and cannot measure

```bash
make eval
```

Runs with a file corpus, a deterministic embedder and an in-memory store: no
services, no API key, no bill. That is the only reason CI can gate on it — a
metric that costs money to produce is one that eventually stops being produced.

**The offline embedder is not semantic.** It hashes tokens into buckets, so
documents sharing words land near each other and dense retrieval genuinely
contributes, but "owes" and "outstanding" share no bucket. Golden questions that
turn on meaning score badly offline and are supposed to. The floors are set to
what this configuration actually achieves, so a regression is visible even
though the absolute numbers are not a claim about production quality.

`make eval-live` runs the same questions through the configured provider and the
real corpus. That is the number that says something about semantics. It costs
money, so it is not what CI runs, and it does not gate: it scores whatever
happens to be indexed, which is not a controlled input.

### The cut-off is 4, not 8

Retrieval serves eight chunks in production. The gate uses four, because the
fixture corpus is twelve documents: at k=8 recall is 1.000 for any ranking that
is not actively broken, and a floor under a saturated metric catches nothing.

### The floors

Absolute, not "no worse than last time". A relative gate ratchets downward one
acceptable-looking commit at a time and nobody notices until the number is half
what it was.

Raise them when the number improves. Lowering one needs a sentence in the commit
message saying why.

## The report is written for whoever has to fix it

```
question                    recall     MRR    nDCG  missed from top-k
------------------------------------------------------------------------
order-by-reference            0.00    0.17    0.00  order-s00042 (best at 6)
broken-furniture              0.50    0.50    0.39  policy-warranty (best at 2)
```

Worst first, and printed on a passing run too. The aggregate says something got
worse; this says which question to go and look at.

`(best at 6)` distinguishes "ranked just outside the cut-off" from "not found at
all" — the same number in every metric, and very different problems.

## Answer checks

`AnswerAuditor` looks at a finished answer beside the context it was built from
and checks what can be checked mechanically:

- every citation marker names a block that was really in the prompt
- a grounded answer carries at least one citation
- figures in the text appear in the context, compared after normalising
  `12,480.00` against `12480.0` — a check that fails on formatting is a check
  somebody switches off

**This is a proxy for faithfulness, not a measurement of it.** A model can
restate a number correctly and draw a wrong conclusion from it, and nothing here
notices. Judging that needs a human or a second model, and a gate whose verdict
is itself a generation fails for reasons unrelated to the change under test.

An ungrounded answer passes trivially — there was no context, so there is
nothing to be unfaithful to. Whether it should have refused is the
orchestrator's business and is tested there.

## Metrics

`GET /metrics`, Prometheus exposition format, on by default.

What is counted comes from the questions somebody asks when the assistant is
behaving badly, rather than from what is easy to instrument:

| Question | Series |
| --- | --- |
| Is it answering, refusing, or failing? | `atlas_answers_total{outcome,intent}` |
| Is authorization removing most of what retrieval finds? | `atlas_chunks_total{stage}` |
| Is it slow because of the model, Odoo, or the database? | one histogram per stage |
| What is it costing? | `atlas_tokens_total{provider,model,kind}` |

The gap between `retrieved` and `authorized` is the denial rate — the number
that says whether the over-fetch factor is set right.

**Labels are low-cardinality by rule.** Nothing is labelled by user, by
conversation or by question: those multiply series without bound, and the one
thing worse than no metrics is a metrics backend that fell over. A test asserts
the label names stay inside a fixed set.

The endpoint is deliberately outside the OpenAPI schema. It is scraped by a
collector, not called by a client, and listing it as an API invites somebody to
build on the format.

### Measurement never breaks the thing measured

Every recorder method swallows its own failures and logs at debug. A metrics
backend is not worth a failed answer, and a malformed label — a programming
mistake, not a runtime condition — should surface as a missing series rather
than a 500 on somebody's question.

The application layer records through a port (`domain/observability.py`) whose
methods are named after events, not instruments: `answer_finished`, not
`increment_counter`. The adapter decides whether that becomes a counter, a
histogram or a span. Naming them after instruments would put that decision in
the use case, where it would have to change to add a second backend.

`NullRecorder` is the default everywhere, so no use case branches on whether
anyone is watching.

## Traces

Off unless `ATLAS_OBSERVABILITY__OTLP_ENDPOINT` is set. With no endpoint the SDK
installs nothing and every span is a no-op. An engine that cannot start because
a collector is missing would be a poor trade for observability.

A trace answers what a metric cannot: for this one bad answer, where did the
time go and what did each stage see.

**The trace id is Atlas's, not OpenTelemetry's.** Spans carry `atlas.trace_id`,
because that is the id the addon logged, the engine logged, and Odoo's access
log recorded. A second identity known only to the tracing backend would mean
correlating two id spaces by timestamp.

The OTLP exporter pulls in grpc and is an optional extra: `pip install
atlas[otlp]`.

## Cost

The engine prices each answer and reports it on the `done` event; the addon
stores it on the message. `atlas.conversation.total_cost` sums them, and both
the conversation list and form show it.

Pricing lives in the engine because that is where the price table is. Odoo
holding a copy would give two tables to keep in step, and they would diverge on
the first repricing.

A model nobody has priced reports zero rather than failing. A missing cost
figure is a reporting gap, not a reason to withhold an answer somebody is
already reading.

The figure is an estimate. Providers round and occasionally reprice, and retry
traffic can be billed differently. Accurate enough to rank conversations by
spend and to catch a runaway loop, which is what it is for.
