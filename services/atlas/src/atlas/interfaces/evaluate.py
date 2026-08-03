"""``make eval``: score retrieval against the golden set and gate on the result.

Runs offline by default. The corpus is a file, the embedder is deterministic and
the store is in memory, so the same commit produces the same number on any
machine and CI can fail a pull request on it without an API key or a bill.

That comes at a cost worth stating plainly: **the offline embedder is not
semantic.** It hashes tokens into buckets, so documents sharing words land near
each other and dense retrieval genuinely contributes, but "owes" and
"outstanding" share no bucket. Golden questions that turn on meaning rather than
overlap score badly offline and are supposed to — the floors are set to what
this configuration actually achieves, so a regression is visible even though the
absolute numbers are not a claim about production quality.

``--live`` runs the same golden set through the configured provider and store.
That is the number that says something about semantics. It costs money, so it is
not what CI runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from atlas.application.evaluation import RetrievalEvaluator
from atlas.config.container import Container
from atlas.config.logging import configure_logging
from atlas.domain.corpus import ChunkInput, Document
from atlas.domain.evaluation import GoldenQuestion, RetrievalReport, Thresholds
from atlas.domain.ports.embedding import EmbeddingProvider
from atlas.domain.ports.vector_store import VectorStore
from atlas.infrastructure.llamaindex import LlamaIndexHybridRetriever
from atlas.infrastructure.persistence.fakes import InMemoryVectorStore
from atlas.infrastructure.providers.fakes import FakeChatProvider, TokenEmbeddingProvider

logger = logging.getLogger(__name__)

#: Where the fixtures live, relative to the repository root.
EVALUATION_DIR = Path(__file__).resolve().parents[3] / "evaluation"

#: Cut-off for the offline gate. Four rather than the eight retrieval actually
#: uses, because the fixture corpus is twelve documents: at k=8 recall is 1.000
#: for any ranking that is not actively broken, and a floor under a saturated
#: metric catches nothing. At k=4 the number can move in both directions.
OFFLINE_K = 4

#: Cut-off for a live run, matching what retrieval serves in production.
LIVE_K = 8

#: Floors for the offline gate, taken from an actual run and rounded down by
#: roughly a tenth so an incidental change to chunking does not fail a build.
#: Raise them when the number improves. Lowering one is a decision that needs a
#: sentence in the commit message saying why.
#:
#: Measured at OFFLINE_K: recall 0.778, MRR 0.694, nDCG 0.655.
OFFLINE_THRESHOLDS = Thresholds(recall_at_k=0.70, mrr=0.62, ndcg_at_k=0.58)


@dataclass(frozen=True, slots=True)
class Fixture:
    """One document in the evaluation corpus."""

    key: str
    res_model: str
    res_id: int
    title: str
    text: str


def load_corpus(path: Path) -> list[Fixture]:
    """Read the corpus fixture."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Fixture(
            key=entry["key"],
            res_model=entry["res_model"],
            res_id=int(entry["res_id"]),
            title=entry["title"],
            text=entry["text"].strip(),
        )
        for entry in payload["documents"]
    ]


def load_golden(path: Path) -> list[GoldenQuestion]:
    """Read the labelled question set."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        GoldenQuestion(
            id=entry["id"],
            question=entry["question"],
            relevant=tuple(entry["relevant"]),
            note=(entry.get("note") or "").strip(),
        )
        for entry in payload["questions"]
    ]


async def build_corpus(
    store: VectorStore, embedder: EmbeddingProvider, fixtures: list[Fixture]
) -> None:
    """Embed and write the fixture corpus into ``store``.

    One chunk per document. The corpus is written to be chunk-sized, so
    splitting it would only measure the splitter — and the golden set labels
    documents precisely so it does not have to care how they were split.
    """
    for fixture in fixtures:
        content = f"{fixture.title}\n{fixture.text}"
        result = await embedder.embed([content])
        document = Document(
            source_key=f"eval.{fixture.res_model}",
            source_hash=f"eval:{fixture.key}",
            title=fixture.title,
            embedding_model=embedder.model_id,
            embedding_dimensions=embedder.dimensions,
            res_model=fixture.res_model,
            res_id=fixture.res_id,
            external_ref=fixture.title,
            company_id=1,
        )
        await store.upsert_document(
            document,
            [
                ChunkInput(
                    ordinal=0,
                    content=content,
                    embedding=result.vectors[0],
                    token_count=len(content) // 4,
                    # What ties a retrieved chunk back to a golden label. Without
                    # it the scorer falls back to `res_model:res_id`, which works
                    # but makes the fixture keys unusable in a report.
                    metadata={"golden_key": fixture.key, "record_name": fixture.title},
                )
            ],
        )


def render(report: RetrievalReport, *, mode: str) -> str:
    """The metrics table, and the questions worth looking at.

    The aggregate is three numbers nobody can act on. The list underneath is the
    part that tells somebody which question broke, which is why it is printed
    even on a passing run.
    """
    lines = [
        "",
        f"Retrieval evaluation ({mode}, k={report.k})",
        "=" * 58,
        f"{'questions':<24}{report.questions:>10}",
        f"{'recall@k':<24}{report.recall:>10.3f}",
        f"{'MRR':<24}{report.mrr:>10.3f}",
        f"{'nDCG@k':<24}{report.ndcg:>10.3f}",
        f"{'fully retrieved':<24}{report.perfect:>7}/{report.questions}",
        "",
        f"{'question':<26}{'recall':>8}{'MRR':>8}{'nDCG':>8}  missed from top-k",
        "-" * 72,
    ]
    for result in report.worst:
        if result.missed:
            rank = result.rank_of_first_hit
            where = f" (best at {rank})" if rank else " (not found)"
            missed = ", ".join(result.missed) + where
        else:
            missed = "-"
        lines.append(
            f"{result.question.id:<26}"
            f"{result.recall:>8.2f}{result.reciprocal_rank:>8.2f}{result.ndcg:>8.2f}  {missed}"
        )
    lines.append("")
    return "\n".join(lines)


async def evaluate_offline(
    questions: list[GoldenQuestion], fixtures: list[Fixture], k: int
) -> RetrievalReport:
    """Score the golden set with no network and no key."""
    embedder = TokenEmbeddingProvider()
    store = InMemoryVectorStore()
    await build_corpus(store, embedder, fixtures)

    retriever = LlamaIndexHybridRetriever(
        store=store,
        embedder=embedder,
        # Never asked anything: fusion is reciprocal-rank, which needs no model.
        # It is passed so LlamaIndex cannot reach for a vendor of its own.
        chat=FakeChatProvider(),
    )
    return await RetrievalEvaluator(retriever, k=k).run(questions)


async def evaluate_live(questions: list[GoldenQuestion], k: int) -> RetrievalReport:
    """Score the golden set against the configured provider and store.

    Reads whatever corpus is already indexed rather than writing the fixture:
    the point of a live run is to measure retrieval over real content.
    """
    async with await Container.create() as container:
        return await RetrievalEvaluator(container.retriever, k=k).run(questions)


def build_parser() -> argparse.ArgumentParser:
    """The command line, kept separate so a test can parse without running."""
    parser = argparse.ArgumentParser(
        prog="atlas-eval",
        description="Score retrieval against the golden question set.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the configured provider and store instead of the offline fixtures.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=None,
        help=(
            f"Cut-off for recall@k and nDCG@k. Defaults to {OFFLINE_K} offline "
            f"and {LIVE_K} live; see OFFLINE_K for why they differ."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print the metrics as JSON instead of a table.",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Report the numbers without failing on a regression.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation and return a process exit code.

    Returns 1 when a floor is not cleared, which is what makes this a gate
    rather than a report.
    """
    args = build_parser().parse_args(argv)
    configure_logging(level="WARNING", json_output=False)

    questions = load_golden(EVALUATION_DIR / "golden.yaml")
    mode = "live" if args.live else "offline"
    k = args.k if args.k is not None else (LIVE_K if args.live else OFFLINE_K)

    if args.live:
        report = asyncio.run(evaluate_live(questions, k))
    else:
        fixtures = load_corpus(EVALUATION_DIR / "corpus.yaml")
        report = asyncio.run(evaluate_offline(questions, fixtures, k))

    payload: dict[str, Any] = {"mode": mode, **report.as_dict()}
    _write(json.dumps(payload, indent=2) if args.as_json else render(report, mode=mode))

    if args.no_gate or args.live:
        # A live run scores whatever happens to be indexed, which is not a
        # controlled input. Gating on it would fail the build for a change to
        # somebody's data.
        return 0

    failures = OFFLINE_THRESHOLDS.failures(report)
    if failures:
        _write("Retrieval regression:", stream=sys.stderr)
        for failure in failures:
            _write(f"  {failure}", stream=sys.stderr)
        _write(
            "\nIf this is an improvement the floors have not caught up with, "
            "raise them in interfaces/evaluate.py. If it is not, the table above "
            "names the questions that lost ground.",
            stream=sys.stderr,
        )
        return 1

    return 0


def _write(message: str, *, stream: TextIO | None = None) -> None:
    """Write a line for a human.

    The engine logs structurally; a command line answers a person, and routing
    this through the logger would bury a metrics table in JSON.
    """
    print(message, file=stream or sys.stdout)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
