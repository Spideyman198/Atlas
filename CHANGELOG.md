# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From 1.0.0 the public surface — the HTTP APIs, `ATLAS_*` configuration keys, Odoo
model fields and security groups — is covered by semantic versioning. What that
covers, what it deliberately does not, and the deprecation policy are in
[docs/upgrading.md](docs/upgrading.md).

## [Unreleased]

Nothing yet.

## [1.0.1] - 2026-08-04

Two defects in the OpenAI adapter, both found by running Atlas against a live
provider for the first time. Neither is specific to one vendor: the same adapter
serves OpenAI, Azure OpenAI and any OpenAI-compatible endpoint, and both faults
were reachable from all of them. No configuration change is needed to pick these
up, and nothing in the public surface moved.

It also records the first end-to-end run against a live model provider, which
[1.0.0](#100---2026-08-04) listed as unverified, and the first release the
automation produced on its own — 1.0.0 was tagged by hand.

### Fixed

- Streamed responses dropped every tool call. The OpenAI adapter's `stream()`
  yielded text deltas and a stop reason but never the calls themselves, so the
  tool loop in `atlas.application.synthesis` saw an empty list, ended on its
  first round, and returned an empty answer to any question that needed a tool.
  The adapter now reassembles calls from the deltas and emits them on the
  terminal chunk, as the Anthropic adapter already did.

  Reassembly handles both shapes on the wire. OpenAI splits one call across many
  chunks — id and name first, then the argument JSON a few characters at a time —
  and identifies the parts by `index`. Google's compatibility endpoint sends each
  call whole and omits `index` entirely.

- Embedding responses whose items carry no `index` raised `TypeError: '<' not
  supported between instances of 'int' and 'NoneType'` and killed the sync that
  hit them. Ordering was restored by sorting on a field OpenAI always populates
  and Google leaves null. The response order now stands when any index is
  missing, which is the order every such endpoint returns items in anyway.

- `EmbeddingSettings` gained `base_url`. Chat has had one since M3b; embeddings
  did not, which left every OpenAI-compatible embedding host unreachable while
  the same host worked for chat.

- `docker-compose.yml` mapped the provider keys only from `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY`, so setting `ATLAS_CHAT__API_KEY` in `.env` had no effect and
  the container received an empty string. Both keys now accept the `ATLAS_*` name
  first and fall back to the vendor-specific one, and the `BASE_URL` variables
  are passed through.

- Odoo's healthcheck called `curl`, which the image does not ship. It failed on
  every interval and the container reported unhealthy while serving requests
  normally. It now uses `python3`.

- The release pipeline could not complete, and had never completed: 1.0.0 was
  tagged by hand. Three faults, each latent since the workflow was written and
  each surfacing only once the one before it was cleared. semantic-release
  refused to run against the detached HEAD left by a SHA checkout, so no version
  was ever written. The image tags were built from `github.repository`, which
  keeps the account's capitalisation and is not a valid lowercase OCI path. The
  Odoo image then failed to build for arm64, for the reason under *Changed*
  below. `fail-fast` is now off for the image matrix, so one image failing no
  longer cancels the other.

- CI never built the images it publishes. Both published targets are now built
  for both architectures on every push to `main`, which is where the arm64
  failure would have been caught instead of at release time.

### Changed

- The published Odoo image no longer contains a browser. `docker/odoo/Dockerfile`
  now has two targets: `runtime`, which is what gets published, and `test`, which
  adds Chrome and `websocket-client` for the tours. Compose builds `test`, so
  `make up`, `make test-odoo` and CI are unchanged and still run the tours.

  Google ships Chrome for linux/amd64 and no other Linux architecture, so
  installing it unconditionally made the arm64 half of the published manifest
  unbuildable. Both images are now genuinely multi-architecture. A deployment
  image is also a poor place to carry a browser it never runs.

  Anything that depended on a browser being present in the published Odoo image
  must use the `test` target instead. Nothing in this repository did.

### Added

- Pricing entries for the Gemini models reachable through the compatibility
  endpoint. The engine refuses to start on an unpriced model by design (M3b), so
  a new model needs one.

- Regression tests for both adapter defects, covering the fragmented-and-indexed
  shape and the whole-and-unindexed shape, plus ordering across two calls.

  The streamed-tool-call fault was invisible to the suite because the test double
  replayed a stream that contained no tool calls at all — a stub that hands over
  a finished call cannot show whether the adapter reassembles one. The double now
  fragments calls the way the wire delivers them. These tests are permanent, and
  the fidelity rule they came from is written down in
  [docs/developer-guide.md](docs/developer-guide.md).

- [ADR-0009](docs/adr/0009-defer-aggregate-ordering.md) records that the
  `aggregate` tool returns rows in no particular order under a 50-row cap while
  describing itself as suitable for "which is the biggest" questions, and defers
  the fix to a minor release because it changes behaviour rather than repairing
  an implementation defect.

## [1.0.0] - 2026-08-04

First stable release. Atlas answers questions about an Odoo Community database
in natural language, under the asking user's own access rights.

The property the design exists to hold: retrieval searches an index that is
deliberately broader than any one user's view, and nothing reaches a prompt
until Odoo has confirmed, in that request and as that user, that they may read
it. That is not a filter a future change can forget — `PromptContext` is
constructible only from `AuthorizedChunk`, and a test runs `mypy --strict` over
a fixture that tries to bypass it.

Read-only. There is no tool that changes anything, a test scans the addon for
`create`, `write`, `unlink`, `copy`, `sudo` and `execute`, and a request to
change data is refused before anything is fetched.

Verified for this release: 702 engine tests, 174 addon tests including two
browser tours, 91% coverage, four architectural import contracts, a retrieval
regression gate, and a clean supply-chain audit. Not verified: production recall
against a real corpus and an end-to-end run against a live model provider — both
need credentials and data this repository does not carry. See
[docs/performance.md](docs/performance.md) for what the measurements do and do
not establish.

### Added

CI/CD, release engineering and documentation (M14):

- A security job on every pull request: `pip-audit` for dependency
  vulnerabilities, gitleaks over the full history, a licence check against
  LGPL-3.0 compatibility, and version consistency between the engine package and
  the addon manifest. Trivy scans the built image for OS and library CVEs.
- A weekly scheduled audit, because a vulnerability disclosed against a
  dependency that has not changed will not be caught by a pull-request gate.
- `scripts/check_licences.py`, reading `importlib.metadata` directly.
  `pip-licenses` reported "UNKNOWN" for 63 of 157 distributions because it does
  not consult PEP 639 `License-Expression` — a licence checker that cannot read
  most licences is worse than none, because it looks like it is working.
- `scripts/audit_dependencies.py`, which keeps `--strict`'s guarantee without
  its false failure on Atlas itself, which has no PyPI release to audit against.
- A release workflow: semantic-release determines the version from Conventional
  Commit prefixes, the addon manifest is derived from it, and multi-architecture
  images (amd64 and arm64) are published to GHCR with provenance and an SBOM.
  The changelog is deliberately not generated — this file is prose, and a list
  of commit subjects would replace an explanation with an inventory.
- [docs/deployment.md](docs/deployment.md): installing next to a real Odoo,
  verified command by command against a running stack.
- [docs/api-reference.md](docs/api-reference.md): every endpoint on both sides
  of the boundary, with the authentication each uses.
- `scripts/check_workflows.py`, which verifies that every `uses:` reference
  resolves to a real action. GitHub resolves those at run time, so a version
  that does not exist is a red pipeline rather than a parse error — one shipped
  that way.
- `.gitleaks.toml`, allowing the redaction tests to contain the secret-shaped
  strings they exist to test against. Scoped to that one file rather than
  disabling the rules, because switching off `private-key` repository-wide to
  accommodate a fixture is how a real key gets committed later.

Security hardening and performance (M13):

- Redaction of credentials and regulated identifiers before anything crosses
  into a prompt, and again over the generated answer. Payment cards are
  Luhn-validated and IBANs mod-97 validated, which is what makes the rules
  precise enough to leave enabled in an ERP full of long digit strings. Names,
  emails, phone numbers and VAT numbers are deliberately kept — they are what
  the questions are about. Documented in [docs/security.md](docs/security.md).
- Output validation: an answer reproducing twenty consecutive words of the
  system prompt is replaced with a refusal.
- Per-user rate limiting, keyed on the context token rather than the address,
  checked before anything is fetched.
- A threat model, stating what is defended and what is not.
- `benchmarks/`: a standalone, reproducible harness — dataset generation, an
  HNSW recall sweep, a query-shape latency comparison, and the `EXPLAIN
  (ANALYZE, BUFFERS)` plans. `make bench` runs it. Each run writes a timestamped
  JSON and CSV file to `benchmarks/results/` carrying the commit, the
  PostgreSQL and pgvector versions, the host and the exact command, so a
  published table can be checked against the run that produced it.
  [benchmarks/README.md](benchmarks/README.md) documents what each number is
  worth, including the four dataset distributions that measured nothing.
- Measured figures in [docs/performance.md](docs/performance.md) replace the
  estimates that were in the data-architecture document.
- `shm_size: 2gb` on the PostgreSQL container. A parallel HNSW build asks for
  about a gigabyte of shared memory and Docker's 64 MB default fails with "No
  space left on device", which is not a disk problem.

Evaluation and observability (M12):

- A golden question set and a fixture corpus, with `make eval` scoring
  recall@k, MRR and nDCG@k and failing on a regression. Runs offline, so CI can
  gate a pull request without an API key. Documented in
  [docs/evaluation.md](docs/evaluation.md).
- The metrics are pure functions over ranked ids, tested against hand-written
  rankings where the right answer is arithmetic rather than opinion.
- Answer checks: every citation marker resolves, a grounded answer cites
  something, and figures in the text appear in the context. A proxy for
  faithfulness, and documented as one.
- `GET /metrics` in Prometheus format, with labels held to a fixed
  low-cardinality set — nothing per user, per conversation or per question.
- OpenTelemetry spans, off unless a collector is configured, carrying Atlas's
  own trace id rather than a second identity only the tracing backend knows.
- Per-answer cost, priced in the engine where the price table lives, stored on
  the message and summed per conversation.
- A `Recorder` port with a no-op default, so no use case branches on whether
  anyone is watching, and no metrics failure can reach a caller.

Chat panel (M11):

- An OWL client action: message list with a streaming renderer, composer,
  conversation sidebar with search, and loading, error and retry states.
  Documented in [docs/chat-ui.md](docs/chat-ui.md).
- `/atlas/chat/ask`, which mints the context token server-side and relays the
  engine's events to the browser. The browser never holds a token: it is a
  bearer credential, and a page holding one hands it to every script in that
  page.
- Citation chips that open the record they name. A citation with no resolvable
  target is dropped rather than rendered, because a chip that opens nothing
  looks like evidence until it is clicked.
- Suggested questions derived from which modules are installed and what the
  acting user may read, so nobody is offered a question that comes back as a
  refusal.
- The question is stored before the stream opens and the answer when it closes,
  so a dropped connection loses the answer and keeps the question.
- Google Chrome and `websocket-client` in the Odoo image, for browser tests.

Orchestration and answer synthesis (M10):

- `AnswerService`: route, gather, generate, resolve citations. One path serves
  both the streamed and the assembled answer, so the UI cannot behave
  differently from the tests. Documented in
  [docs/orchestration.md](docs/orchestration.md).
- Nothing to ground on means the model is not called. No retrieved context and
  no available tool produces a refusal, which covers the case where
  authorization emptied the context and the case where a provider cannot call
  tools. A request to change data is refused before anything is fetched.
- An intent router over four routes — structured, semantic, hybrid, refuse —
  with rules only for questions a rule can recognise and hybrid for everything
  else. A wrong route costs latency; hybrid costs latency too and cannot be
  wrong for it.
- A Jinja2 prompt library behind a port, versioned by content hash. A
  hand-maintained number gets forgotten on the edit that mattered; a hash cannot
  go stale. Every answer records the prompt identity that produced it.
- Prompt-injection resistance with a property behind it: retrieved text cannot
  forge the context fence, because no rendered variable may contain the marker.
  The system prompt's instruction to treat retrieved records as data is worth
  what the fence is hard to forge.
- Citations resolved from markers rather than accepted. A marker naming a block
  that was never in the prompt is removed from the answer — a reference a reader
  cannot follow looks like evidence.
- A bounded tool loop, five rounds, feeding results back to the model.
- Conversation memory that keeps recent turns verbatim and summarises older
  ones, split by token size rather than turn count. A failed summary drops the
  history rather than the answer.
- `POST /v1/chat`, streaming server-sent events. A failure after the first byte
  arrives as an `error` event, because the status line is long gone.

Structured query tools (M9):

- Five tools reading live Odoo through the ORM: `find_records`, `aggregate`,
  `stock_levels`, `overdue_invoices` and `customer_360`. Each runs in the acting
  user's environment, so record rules apply without the tools arranging for it.
  Documented in [docs/tools.md](docs/tools.md).
- A filter compiler. The model emits `{field, operator, value}` objects, never a
  domain — a domain can traverse relations to fields on no allow-list, and the
  set of expressible queries should be one we chose rather than one we inherited.
- Per-model allow-lists for fields, measures, groupable columns and the module
  each needs, with caps on rows, filters, list values, text length and grouping
  depth.
- A tool is only offered when its models are installed and the acting user can
  read them, so a model is never told about a call that could only ever fail.
- `ToolBox` on the engine side. A rejected argument comes back as a tool result
  the model can retry from; an unreachable Odoo does not, because no retry fixes
  it.
- Tool results are JSON before they leave the process — ISO dates, `[id, name]`
  pairs — so a tool called in a test returns what a tool called over the wire
  returns.
- A read-only scan over the tool package for `create`, `write`, `unlink`,
  `copy`, `sudo` and `execute`, plus a test that the scan would catch them.

Retrieval engine (M8):

- Hybrid retrieval: dense and lexical search over the same chunks, combined with
  reciprocal rank fusion. Their scores share no scale, so fusion uses only the
  positions — which is how an identifier and a sentence can be the same question.
  Documented in [docs/retrieval.md](docs/retrieval.md).
- A diversity pass, so the top of a result list stops being the same fact eight
  times. Similarity is over words rather than vectors, which needs no extra
  round-trip and covers the lexical half of the results too.
- `RetrievalPipeline`: retrieve, authorize, assemble. The middle stage cannot be
  skipped — `PromptContext` is constructible only from `AuthorizedChunk`, and a
  test runs `mypy --strict` over a fixture that tries to bypass it.
- Token-budgeted assembly. A chunk that does not fit is skipped rather than
  truncated, and a smaller one further down can still fit.
- Citations built from the assembled context, one per record. Nothing a model
  produces becomes a citation, so a citation cannot be hallucinated.
- `AtlasLlamaVectorStore`, `AtlasLlamaEmbedding` and `AtlasLlamaLLM`. The last
  is load-bearing rather than decorative: without it `QueryFusionRetriever`
  resolves `Settings.llm` and reaches for LlamaIndex's OpenAI integration, which
  is the second-vendor-path failure ADR-0003 inverts the dependency to prevent.
- `Reranker` port with a no-op default, and a written reason for why no
  cross-encoder ships yet.

### Fixed

- The engine package and the Odoo addon manifest had drifted apart — `0.1.0`
  against `19.0.0.2.0`, which reads as `0.2.0` — with nothing comparing them.
  They are one product shipped as two artifacts, and CI now fails when the two
  declarations disagree.
- The tools image had no `git`, so the benchmark harness recorded an empty
  commit and semantic-release could not run at all. A results file that cannot
  be traced to code is not a measurement.
- `pip` in the engine image was old enough to be a vulnerability finding in its
  own right, which meant `pip-audit` reported the toolchain rather than the
  project.
- Two HIGH vulnerabilities in the runtime image — `msgpack` and `setuptools` —
  came from pip's *vendored* copies rather than from any declared dependency,
  which is why `pip-audit` saw nothing and Trivy did. pip is now removed from
  the runtime image entirely: nothing installs anything there, the virtualenv
  arrives complete from the builder, and a runtime container without a package
  installer is one an attacker cannot `pip install` into. `alembic` is
  unaffected and migrations still run from the image.
- Filtered dense search was 32x slower than necessary and returned incomplete
  results. With a company filter matching a third of the table the planner takes
  a bitmap scan over `(company_id, visibility)` and sorts sixteen thousand rows
  rather than walking the HNSW graph — 126.95 ms. `hnsw.iterative_scan` was
  already configured and did nothing, because it governs an index scan that was
  never chosen. Forcing the index without it returns 5.1 rows for a `LIMIT` of
  8. Both are now set together, scoped to the dense search's own transaction so
  the lexical search keeps the bitmap scan it needs: 3.94 ms p50, complete
  results.
- The LlamaIndex bridge dropped chunk metadata. Everything a chunk carried of
  its own — including `record_name`, which is what a citation is labelled
  with — was lost on the way through fusion, so citations fell back to a worse
  label. Found by an evaluation run scoring zero across the board.
- Browser tests were being skipped rather than run. Odoo skips a tour when it
  cannot find a browser or `websocket-client`, and a skip is not a failure — the
  first version of the chat tours reported green having exercised nothing. A
  test now asserts both are present, so the same situation is a red build.
- The chat endpoint read the trace id from `request.state`, where the middleware
  does not put it, so every answer carried `trace_id: null` — and an answer with
  no trace id cannot be lined up against Odoo's access log, which is the one
  thing the id is for. Found by calling the running engine, not by a test.
- Enumerating the cross product of fields, operators and values against the
  filter compiler found four ways to compile a domain the database or the ORM
  then rejected: an `in` list whose elements were the wrong type, an ordering
  operator against `None`, a text operator against a boolean, and a numeric
  field compared to `True`. Each was a 500 where a rejected tool call was
  wanted. All four are now refused with a message naming what is allowed.
- The development environment never loaded demo data. Odoo 19 turned
  `--without-demo` into the default and added `--with-demo` as the opt-in, so
  the bootstrap's flag was on the wrong branch: the log said the database had
  been seeded and it came up empty.
- The engine package shipped no `py.typed`, so anything type-checking against
  Atlas treated it as untyped.
- `InMemoryVectorStore` handed every chunk the id `0`. LlamaIndex identifies
  nodes by id, so a result set of several collapsed into one on its way through
  fusion — silently, as missing results rather than an error. Ids are now unique
  across the store, the way PostgreSQL issues them.

Ingestion pipeline (M7):

- Eight sources — partners, products, attachments, CRM leads, sale and purchase
  orders, invoices, stock — declared as data in `atlas.domain.sources`. One
  renderer walks all of them, so a field nobody thought to index is one line to
  add. Documented in [docs/ingestion.md](docs/ingestion.md).
- LlamaIndex arrives, confined to `atlas.infrastructure.llamaindex`: sentence
  splitting plus PDF and DOCX readers. The containment is falsifiable — deleting
  the dependency fails exactly one test file, and an `import-linter` contract
  fails the build if it is imported anywhere else (ADR-0003).
- `SourceReader`, `DocumentLoader`, `JobQueue`, `EmbeddingCache` and
  `SourceState` ports, with in-memory doubles so retrieval work can be developed
  and tested without PostgreSQL or an Odoo.
- Re-running a sync with nothing changed makes no embedding calls: the content
  hash is checked before the provider is called. Attachments are compared by the
  checksum Odoo already holds, so an unchanged contract is never downloaded.
- The hash carries record identity as well as content, so two records that
  render identically — two contacts with the same name and nothing else filled
  in — cannot collide on the unique index and overwrite one another.
- Segment-level embedding cache keyed by `(content_hash, model)`. A changed
  order re-embeds the line that changed, not the eleven that did not.
- `ingest_jobs` claimed with `SELECT ... FOR UPDATE SKIP LOCKED`: a durable queue
  several workers can drain with no broker. Exponential backoff, a `dead` state
  deliberately distinct from `failed`, and a stale sweep that returns a crashed
  worker's job without refunding the attempt it burned.
- Incremental sync by `write_date` watermark, which only ever moves forward.
- The `atlas` command line (`sources`, `sync`, `reindex`, `worker`), the
  `atlas-worker` compose service, `POST /v1/ingest/sync`, an `ir.cron` trigger
  and an indexing wizard in Odoo. Sources are off until somebody turns them on.
- Ingestion reads as a dedicated integration user (`ATLAS_INGEST_UID`, holding
  `odoo_atlas.group_atlas_ingest`) through its own endpoints. That account sees
  more than any one person does, which is precisely why the query-time check
  cannot be skipped — and it is still an ordinary Odoo account, with no `sudo()`.

### Fixed

- `PgVectorStore.upsert_document` now removes any other document for the same
  record inside the same transaction. An edited record renders differently and so
  hashes differently, which meant the previous version's chunks lingered and the
  record was retrievable twice — once as it is and once as it was.

Odoo gateway and authorization (M6):

- The callback API the engine asks Odoo through: `/atlas/api/authorize`,
  `/atlas/api/records`, `/atlas/api/tool/execute` and `/atlas/api/status`,
  documented in [docs/api.md](docs/api.md). Every read runs as the acting user.
- Two secrets doing two jobs. `ATLAS_SERVICE_TOKEN` proves a call came from the
  engine; `ATLAS_CONTEXT_SECRET` signs the short-lived tokens naming the acting
  user and is never given to the engine, so it can replay a context Odoo issued
  but cannot mint one. An unset secret refuses every call.
- Context tokens are re-checked against the database on every use, so
  deactivating an account or removing its Atlas group takes effect on the next
  call. The companies in a token are intersected with the ones the user still
  has, so it cannot widen its own scope.
- `OdooGateway` port, an httpx adapter and an in-memory fake, held to one shared
  contract suite. Authorization is batched by model.
- `AuthorizedChunk`, produced only by `AuthorizationFilter`. Skipping the
  authorization step is a type error rather than a policy that could be
  forgotten. Every failure — including exception types nobody anticipated —
  collapses into a refusal and no chunks.
- `atlas.access.log`: who acted, which model, how many ids were asked about,
  granted and refused, the trace id and the duration. Append-only through the
  ORM, written as the acting user, and failing to write one fails the request.
- The `sudo()` prohibition from ADR-0006 enforced by a test that scans the
  addon's source. No allow-list and no exceptions.
- `/readyz` gains a gating `odoo` check: with Odoo unreachable the engine can
  clear no candidates, so every answer it could give would be a refusal.
- A *Test Connection* button on the settings page, and a hard timeout on the
  addon's engine client, so an engine outage degrades the assistant and not Odoo.

### Changed

- Atlas configuration on the Odoo side moved from `ir.config_parameter` to the
  server's environment (`ATLAS_ENGINE_URL`, `ATLAS_ENGINE_TIMEOUT`,
  `ATLAS_CONTEXT_TOKEN_TTL`). Reading a config parameter needs system rights, and
  the code that reads it runs as whichever user asked a question — so a parameter
  would have forced a `sudo()` onto the request path. The settings page now
  reports the configuration read-only instead of offering to overrule it. The
  M5 `odoo_atlas.service_token` parameter is gone; set `ATLAS_SERVICE_TOKEN`
  instead.
- The engine refuses to start without `ATLAS_ODOO__SERVICE_TOKEN`. Odoo
  authorises every retrieval, so an engine without one could retrieve candidates
  and clear none of them — an outage that looks like an assistant which knows
  nothing rather than like a missing environment variable.

Odoo addon skeleton (M5):

- `odoo_atlas`, the Odoo half of Atlas: `atlas.conversation`, `atlas.message` and
  `atlas.message.citation`, with list, form and search views, menus and window
  actions. It depends on `base` and `web` only and contains no AI code.
- A conversation is titled from its first question, leaves `draft` when it gets
  one, and carries a stored message count and provider cost.
- Citations are rows, not a JSON blob: they resolve to the live record through a
  computed reference, and keep the name the record had when it was cited so they
  still read sensibly once it is deleted.
- Two groups — Atlas user and Atlas administrator — with `ir.model.access.csv`
  and three record rules per model: ownership, administrator, and a group-less
  multi-company rule, which is global and so binds administrators too. Neither
  group is implied by `base.group_user`; access is granted per user.
- Messages and citations store their own `user_id` and `company_id`, copied from
  the conversation, so every record rule is a comparison against an indexed
  column rather than a join back to `atlas_conversation`.
- A conversation cannot change owner, administrator included. Its answers were
  computed under one user's access rights, so reassigning it would show a second
  user results drawn from records they may not read.
- Settings page for the engine URL, the service token and the request timeout.
- 40 `TransactionCase` tests including the negative access paths, run by Odoo's
  own runner: `make test-odoo` installs the addon into a database created from
  nothing, so a pass means it installs cleanly as well as behaves. CI gains an
  `addon` job that does the same.

Vector store and persistence (M4):

- The `atlas` schema: `ingest_sources`, `documents`, `chunks`, `ingest_jobs`,
  `embedding_cache`, in a hand-written Alembic migration with a working
  downgrade. `chunks.content_tsv` is a generated column, so the lexical and dense
  sides always describe the same text.
- HNSW (`vector_cosine_ops`, `m=16`, `ef_construction=64`), GIN over the tsvector,
  and the supporting btree indexes from ADR-0004, including the partial index the
  M7 job queue will poll.
- `VectorStore` port and `PgVectorStore` adapter: content-hash existence check,
  idempotent upsert that replaces a document's chunks atomically, record deletion
  via the foreign-key cascade, and dense and lexical search with company,
  visibility and model pre-filters.
- Dense search sets `hnsw.iterative_scan = relaxed_order` so a filtered ANN query
  cannot silently return too few rows, and converts cosine distance to a score so
  every search mode sorts the same direction.
- `CandidateChunk`: retrieval results are unauthorized by construction, so the
  M6 authorization filter has a type to convert from.
- Migrations ship inside the runtime image, so a deployment can apply its own
  schema. `/readyz` compares the migrated vector width against the configured
  embedding model and reports not-ready on a mismatch.
- Integration tests against real PostgreSQL, run in CI against a pgvector service
  container. They create and migrate their own database, so the migration is
  verified against an empty cluster on every run.

Vendor adapters (M3b):

- Anthropic chat adapter. Sends `output_config.effort` and adaptive thinking,
  and sends no sampling parameters — `temperature`, `top_p` and `top_k` are
  rejected with a 400 by current models, as is `thinking.budget_tokens`. Folds
  `system`-role messages into the system parameter, renders tool results as
  user-role content blocks, drops thinking blocks from the answer, and maps a
  safety refusal onto a stop reason rather than an exception.
- OpenAI chat and embedding adapters over the Chat Completions API. Azure is
  reached through the same client with a base-URL override. Tool arguments are
  parsed from JSON strings, cached tokens are subtracted from `prompt_tokens` so
  the usage fields stay disjoint, and streamed responses request usage
  explicitly.
- Voyage embedding adapter, sending `input_type` so the document/query
  distinction reaches the vendor, and requesting `output_dimension` rather than
  assuming it.
- All five adapters registered against the M3a contract suites, driven by stub
  SDK clients — substitutability is checked at build time, not asserted.
- `atlas.config.providers`: the only module naming a concrete adapter. Wraps
  every chat provider as `Accounting(Retrying(adapter))` and disables the vendor
  SDKs' own retrying so there is one backoff policy rather than two.
- Provider settings (`ATLAS_CHAT__*`, `ATLAS_EMBEDDING__*`) including the
  offline `fake`/`hash` vendors, so the stack starts with no account.
- Providers are built in the composition root, so a missing API key, an unpriced
  model, or a dimension that disagrees with the provider stops the engine at
  startup.
- Live contract suite marked `live` and key-gated, excluded from pull requests.

Provider ports and resilience (M3a):

- `ChatProvider` and `EmbeddingProvider` ports, with the domain vocabulary they
  speak: `Message`, `ToolDefinition`, `ToolCall`, `ToolResult`, `ChatRequest`,
  `ChatResponse`, `ChatChunk`, `StopReason`, `Effort`, `TokenUsage`.
- Shared contract test suites. An adapter subclasses the contract, supplies a
  provider fixture, and inherits every assertion — substitutability is enforced
  rather than assumed.
- `FakeChatProvider` (scripted responses and errors, records requests) and
  `HashEmbeddingProvider` (deterministic, L2-normalised vectors), so the suite
  runs with no network, no API key and no cost.
- Retry decorator: jittered exponential backoff, provider-supplied `retry-after`
  taking precedence, a 30-second cap, and injectable sleep so tests exercise the
  schedule without waiting. Non-retryable provider errors fail immediately.
- Accounting decorator recording latency, token usage and estimated cost against
  the request's `trace_id`.
- Pricing table using `Decimal`. Anthropic cache multipliers are derived from the
  input rate; an unpriced model raises `ConfigurationError` rather than reporting
  a silent zero.

Engine foundations (M2):

- Layered package skeleton: `domain`, `application`, `infrastructure`,
  `interfaces`, `config`.
- Error taxonomy in `atlas.domain.errors`. Errors carry a stable `code` and
  structured context but no HTTP status; the transport mapping lives at the HTTP
  boundary so the same errors are usable from the CLI and the ingestion worker.
- RFC 9457 problem documents for error responses, served as
  `application/problem+json` and carrying `code` and `trace_id`. Unexpected
  exceptions return a generic 500 without leaking the message.
- Structured JSON logging with request correlation. `TraceIdMiddleware` adopts an
  inbound `X-Request-ID` or mints one, binds it for the request, and echoes it on
  the response. Uvicorn's own loggers are rerouted so access logs are structured
  and correlated too.
- Composition root in `atlas.config.container`, owning the lifetime of
  process-wide resources.
- Settings grouped by concern with a nested env delimiter
  (`ATLAS_DATABASE__URL`, `ATLAS_DATABASE__POOL_MAX_SIZE`), plus pool sizing and
  connect timeout.
- Four `import-linter` contracts enforcing the layering, wired into `make check`
  and CI.
- [Developer guide](docs/developer-guide.md).

Development environment (M1):

- Compose stack: PostgreSQL 17 with pgvector 0.8.6, Odoo 19 CE pinned to
  `19.0-20260723`, and the Atlas engine, with health-gated startup ordering.
  Internal services publish on the loopback interface only.
- Multi-stage engine image (`builder`, `dev`, `runtime`) running as a non-root user,
  with a liveness-only container health check.
- Odoo bootstrap wrapper that initialises the database on first boot, so the stack
  is usable after a single command.
- PostgreSQL init script creating the `atlas` database, enabling `pgvector`, and
  failing if the extension is older than 0.8.
- `Makefile` and `make.ps1` with matching targets. Lint, type-check and test run in
  the `dev` image, so no local Python is required.
- Tooling configuration in the root `pyproject.toml`: ruff, mypy `--strict`, pytest
  with markers, coverage with a threshold that rises per milestone.
- `.pre-commit-config.yaml`, `.env.example`, `.dockerignore`.
- CI: lint, type-check, unit tests with coverage artefacts, compose validation,
  engine image build, and a liveness smoke test against the built image.
- `/healthz` and `/readyz` on the engine. Readiness asserts pgvector meets the
  minimum version ADR-0004 depends on.
- [Installation guide](docs/installation.md).

Planning and architecture (M0):

- Seven decision records in [`docs/adr/`](docs/adr/README.md) covering the ADR
  process, deployment topology, retrieval framework selection, vector storage and
  indexing, model providers, the data access and authorization model, and licensing.
- [Architecture overview](docs/architecture/01-overview.md): component diagrams,
  layer contracts, repository layout, and stated limits for 1.0.
- [Data architecture](docs/architecture/02-data-architecture.md): two-database
  design, entity diagrams, indexing strategy, performance notes, migration policy.
- [Request lifecycle](docs/architecture/03-request-lifecycle.md): query, ingestion
  and failure paths.
- README, [roadmap](ROADMAP.md), [contribution guide](CONTRIBUTING.md).
- LGPL-3.0-or-later licence texts, `.gitignore`, `.gitattributes` line-ending
  normalisation, `.editorconfig`.

### Changed

- CI and `make test` now run `-m "unit or contract"` rather than `-m unit`. Both
  tiers are offline, and excluding `contract` meant the suite that enforces
  adapter substitutability never gated a pull request.
- Coverage floor raised from 70% to 85% (M3a). Actual coverage is 90%.
- The engine's database setting moved from `ATLAS_DATABASE_URL` to
  `ATLAS_DATABASE__URL` when settings were grouped by concern (M2). Deployments
  setting it directly must rename the variable; `docker-compose.yml` and CI are
  already updated.
- ADR-0003 was revised during M0 review. The original proposal — own the retrieval
  orchestration with no general-purpose framework — was rejected. Atlas now uses
  LlamaIndex as an infrastructure-layer implementation of domain-owned ports,
  confined to `atlas.infrastructure.llamaindex` and enforced by an `import-linter`
  contract. Bridge adapters make LlamaIndex delegate to our provider and persistence
  layers, so there is one path to each model vendor and one database schema.
  Authorization stays in the application layer.
  See [ADR-0003](docs/adr/0003-rag-framework-selection.md).

[Unreleased]: https://github.com/Spideyman198/Atlas/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/Spideyman198/Atlas/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Spideyman198/Atlas/releases/tag/v1.0.0
