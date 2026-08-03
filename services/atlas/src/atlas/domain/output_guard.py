"""Checking what the model produced before anyone reads it.

The prompt tells the model not to reveal its instructions. That instruction is
worth something, and it is not worth everything: a sufficiently determined
paragraph inside a retrieved record sometimes talks a model into quoting its
system prompt back. The prompt is a defence; this is the check that it held.

**What is looked for is verbatim quoting, not topic.** An answer that says "I
was told to answer only from the provided context" is the assistant explaining
itself, which is fine and useful. An answer that reproduces twenty consecutive
words of the system prompt is extraction. Matching on runs of words separates
the two without needing to guess at intent.

Deliberately not attempted here: judging whether an answer *followed* injected
instructions. That is a question about meaning, and answering it needs another
model — which would then be a second thing that can be talked into the wrong
answer. The defences against injection are structural and live upstream: the
fence retrieved text cannot forge, and authorization it cannot cross.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Consecutive words that count as quoting rather than coincidence. Twenty is
#: comfortably longer than any phrase two texts share by accident and shorter
#: than a paragraph, so a partial leak is still caught.
DEFAULT_RUN_LENGTH: Final = 20

_WORDS = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True, slots=True)
class OutputCheck:
    """What was found in a generated answer.

    Attributes:
        leaked_instructions: The answer reproduces a long run of the system
            prompt. Treated as a failure, not a warning: an answer that is
            partly the prompt is not an answer.
    """

    leaked_instructions: bool = False

    @property
    def safe(self) -> bool:
        """Whether this answer can be shown as it is."""
        return not self.leaked_instructions


def inspect(answer: str, *, instructions: str, run_length: int = DEFAULT_RUN_LENGTH) -> OutputCheck:
    """Check a generated answer against the instructions that produced it.

    Args:
        answer: What the model wrote.
        instructions: The system prompt it was given.
        run_length: Consecutive words that count as quoting.
    """
    return OutputCheck(
        leaked_instructions=shares_a_run(answer, instructions, run_length=run_length)
    )


def shares_a_run(text: str, source: str, *, run_length: int = DEFAULT_RUN_LENGTH) -> bool:
    """Whether ``text`` contains ``run_length`` consecutive words from ``source``.

    Compared on lowercased word sequences, so reformatting, punctuation and
    line breaks do not hide a quotation — a model asked to reveal its prompt
    tends to reflow it.
    """
    haystack = _WORDS.findall(source.lower())
    needle = _WORDS.findall(text.lower())
    if len(needle) < run_length or len(haystack) < run_length:
        return False

    known = {
        tuple(haystack[index : index + run_length])
        for index in range(len(haystack) - run_length + 1)
    }
    return any(
        tuple(needle[index : index + run_length]) in known
        for index in range(len(needle) - run_length + 1)
    )
