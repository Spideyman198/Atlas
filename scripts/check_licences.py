"""Fail the build if a dependency's licence is incompatible with LGPL-3.0.

Atlas is LGPL-3.0-or-later ([ADR-0007](../docs/adr/0007-licensing.md)). That
constrains what it may depend on: permissive and weak-copyleft licences are
fine, strong copyleft is not, and anything with a field-of-use restriction is
not open source at all whatever its README says.

Reads `importlib.metadata` directly rather than shelling out to `pip-licenses`.
Two reasons. Packaging has moved to PEP 639 `License-Expression`, and
pip-licenses 5.0.0 reported "UNKNOWN" for 63 of 157 installed distributions
because it does not consult it — a licence checker that cannot read most
licences is worse than none, because it looks like it is working. And the
fallback order matters enough to want it visible here rather than behind a flag.

Every declaration is considered, not the first one found: `License-Expression`
(PEP 639), the legacy `License` field, and the `License ::` trove classifiers.
A distribution passes if any of them names an acceptable licence, and fails if
any names a denied one.

Taking only the first was tried and rejected. `python-dateutil` puts "Dual
License" in the legacy field and the two actual licences in its classifiers,
and `python-discovery` puts the entire MIT licence text there. Both look like
unknown licences to a checker that stops at the first field and both are fine.
"""

from __future__ import annotations

import sys
from importlib import metadata

#: Licences a dependency may carry. Matched case-insensitively as substrings,
#: because the same licence is spelled a dozen ways across PyPI — "MIT", "MIT
#: License", "MIT-0", "Expat".
ALLOWED = (
    "mit",
    "expat",
    "bsd",
    "apache",
    "isc",
    "python software foundation",
    "psf",
    "mozilla public license",
    "mpl-2.0",
    "mpl 2.0",
    "unlicense",
    "public domain",
    "zlib",
    "lgpl",  # weak copyleft: linking is fine, and it is Atlas's own licence
    "cc0",
    "postgresql",
    "artistic",
    "historical permission notice",
    "hpnd",
    "0bsd",
    "wtfpl",
)

#: Licences that would make the combined work undistributable under LGPL, or
#: that are not open source at all. Checked before ALLOWED.
DENIED = {
    "agpl": "network copyleft; using it would force Atlas to be AGPL",
    "sspl": "not an open-source licence (field-of-use restriction)",
    "commons clause": "not an open-source licence (sale restriction)",
    "elastic license": "not an open-source licence",
    "business source": "source-available, not open source",
}

#: The project itself, which is LGPL by design and not a dependency of itself.
SELF = {"atlas"}

#: Distributions whose metadata declares no licence, checked by hand against the
#: project's own repository. Each entry needs the licence and where it was read.
KNOWN_UNDECLARED = {
    # Ships LICENSE files but declares neither field nor classifier.
    "ast_serialize": "MIT (LICENSE in the sdist)",
    "griffecli": "ISC (LICENSE in the sdist)",
    "griffelib": "ISC (LICENSE in the sdist)",
    "librt": "MIT (LICENSE in the sdist)",
}


#: A legacy `License` field longer than this is the licence *text*, not its
#: name. Several distributions paste the whole of MIT in there.
_MAX_NAME_LENGTH = 64


def declarations(distribution: metadata.Distribution) -> list[str]:
    """Every licence this distribution declares, in any of the three places."""
    meta = distribution.metadata
    found: list[str] = []

    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        found.append(expression)

    legacy = (meta.get("License") or "").strip()
    if legacy and legacy.upper() != "UNKNOWN":
        first = legacy.splitlines()[0].strip()
        found.append(first if len(first) <= _MAX_NAME_LENGTH else legacy)

    found.extend(
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License ::")
    )
    return found


def _exempt(name: str) -> bool:
    """Whether this distribution is checked somewhere other than its metadata."""
    key = name.lower().replace("_", "-")
    exempt = SELF | {entry.lower().replace("_", "-") for entry in KNOWN_UNDECLARED}
    return key in exempt


def _denial(declared: list[str]) -> str | None:
    """A reason to reject, if any declaration names an incompatible licence.

    Denial wins over permission: a package offered under "MIT or SSPL" is one
    somebody has to make a deliberate choice about.
    """
    for value in declared:
        for marker, reason in DENIED.items():
            if marker in value:
                return reason
        if "gpl" in value and "lgpl" not in value:
            return "strong copyleft; incompatible with distributing Atlas under LGPL"
    return None


def classify(name: str, declared: list[str]) -> str | None:
    """Return a reason to reject, or ``None`` if the licence is acceptable."""
    if _exempt(name):
        return None

    lowered = [value.lower() for value in declared]
    denial = _denial(lowered)
    if denial:
        return denial

    if any(marker in value for value in lowered for marker in ALLOWED):
        return None
    if not lowered:
        return "no licence declared; check the project and add it to KNOWN_UNDECLARED"
    return "licence not on the allow-list; add it deliberately or replace the package"


def main() -> int:
    """Check every installed distribution. Returns a process exit code."""
    problems: list[tuple[str, str, str]] = []
    checked = 0

    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if not name:
            continue
        checked += 1
        declared = declarations(distribution)
        reason = classify(name, declared)
        if reason:
            summary = "; ".join(declared)[:60] or "(none)"
            problems.append((name, summary, reason))

    if not problems:
        print(f"licences: {checked} distributions, all compatible with LGPL-3.0")
        return 0

    print("Incompatible or undeclared licences:\n", file=sys.stderr)
    for name, licence, reason in sorted(problems):
        print(f"  {name:34} {licence:34} {reason}", file=sys.stderr)
    print(
        "\nIf one of these is acceptable, add it to ALLOWED or KNOWN_UNDECLARED "
        "with a sentence saying why. Do not widen the list to make a build pass.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
