"""Fail the build when a workflow references an action that does not exist.

GitHub resolves `uses:` at run time, so a mistyped version is not a syntax error
— it is a red pipeline on somebody else's pull request. `trivy-action@0.28.0`
shipped that way: the tag was invented, and nothing between writing it and
running it looked.

Checks that each pinned reference resolves to an action definition. Needs
network access, which is why it is a separate target rather than part of
`make lint`.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
WORKFLOWS = REPOSITORY / ".github" / "workflows"

#: `uses: owner/repo@ref`, ignoring local (`./`) and docker (`docker://`) forms.
USES = re.compile(r"^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([A-Za-z0-9_.\-/]+)\s*$")

#: An action is defined by one of these. Both spellings are in use — GitHub
#: accepts either, and `trivy-action` uses the one nobody expects.
DEFINITIONS = ("action.yml", "action.yaml")

TIMEOUT_SECONDS = 15


def references() -> set[tuple[str, str, Path]]:
    """Every pinned action reference across the workflows."""
    found: set[tuple[str, str, Path]] = set()
    for workflow in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = USES.match(line)
            if match:
                found.add((match.group(1), match.group(2), workflow))
    return found


def resolves(repository: str, ref: str) -> bool:
    """Whether an action definition exists at this repository and ref."""
    for definition in DEFINITIONS:
        url = f"https://raw.githubusercontent.com/{repository}/{ref}/{definition}"
        request = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                if response.status == 200:  # noqa: PLR2004 - HTTP OK
                    return True
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError) as error:
            message = f"could not reach GitHub to check {repository}@{ref}: {error}"
            raise SystemExit(message) from error
    return False


def main() -> int:
    """Check every reference. Returns a process exit code."""
    broken: list[tuple[str, str, Path]] = []
    checked = sorted(references())

    for repository, ref, workflow in checked:
        if not resolves(repository, ref):
            broken.append((repository, ref, workflow))

    if not broken:
        print(f"workflows: {len(checked)} action references, all resolve")
        return 0

    print("Action references that do not resolve:\n", file=sys.stderr)
    for repository, ref, workflow in broken:
        print(
            f"  {repository}@{ref}  ({workflow.relative_to(REPOSITORY)})",
            file=sys.stderr,
        )
    print(
        "\nCheck the tag against the action's releases. A version that does not "
        "exist fails at run time, not at parse time.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
