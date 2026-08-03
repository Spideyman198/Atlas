"""A deliberate mistake, kept so a test can prove the type system catches it.

This file is **expected to fail** type-checking. It is excluded from the
repository's own ``mypy`` run and is checked by
``tests/unit/test_authorization_is_structural.py``, which asserts that the
error is still there.

The claim being defended is ADR-0003 §4: retrieval produces ``CandidateChunk``,
the prompt assembler accepts only ``AuthorizedChunk``, and the only thing that
converts one to the other is the authorization filter. Bypassing authorization
is therefore not a discipline problem — it does not compile.
"""

from atlas.application.retrieval import ContextAssembler
from atlas.domain.corpus import CandidateChunk


def bypass_the_authorization_step() -> None:
    """Assemble a prompt from an unauthorized chunk. Must not type-check."""
    candidate = CandidateChunk(
        chunk_id=1,
        document_id=1,
        content="something the acting user may not be allowed to see",
        score=1.0,
    )
    ContextAssembler().assemble([candidate], budget=100)
