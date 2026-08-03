"""The acceptance criterion that is a claim about the type system.

    passing a ``CandidateChunk`` to the prompt assembler is a
    ``mypy --strict`` error

A test that merely asserted the two classes were different would prove nothing.
This runs the type checker over a file that deliberately does the wrong thing
and asserts it is rejected — so the guarantee is falsifiable, and stays
falsifiable if somebody later widens the assembler's signature to be helpful.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Excluded from the repository's own mypy run — see `[tool.mypy] exclude`.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "typing" / "bypass_authorization.py"


def run_mypy(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            # Only this file's own errors matter, and following the whole
            # dependency graph would make the check cost more than it is worth.
            "--follow-imports=silent",
            "--no-error-summary",
            "--no-incremental",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_fixture_exists() -> None:
    # Guards the guard: a moved file would make the assertion below vacuous,
    # because mypy on a missing path also exits non-zero.
    assert FIXTURE.is_file()


def test_assembling_a_prompt_from_an_unauthorized_chunk_does_not_type_check() -> None:
    """The error is reported as ``list-item`` rather than ``arg-type``.

    Both mean the same thing — a sequence of the wrong element type — and which
    one mypy picks depends on how the argument happens to be written. Asserting
    the *type names* rather than the code keeps this test about the guarantee
    instead of about mypy's choice of label.
    """
    result = run_mypy(FIXTURE)

    assert result.returncode != 0, "the assembler accepted a CandidateChunk"
    assert "CandidateChunk" in result.stdout, result.stdout
    assert "AuthorizedChunk" in result.stdout, result.stdout
