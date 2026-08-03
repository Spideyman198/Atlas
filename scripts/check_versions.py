"""Fail the build when the two version declarations disagree.

The repository ships two artifacts with two version schemes:

- the engine package, `services/atlas/pyproject.toml`, semantic: `MAJOR.MINOR.PATCH`
- the Odoo addon, `addons/odoo_atlas/__manifest__.py`, which Odoo requires to
  begin with the series it targets: `19.0.MAJOR.MINOR.PATCH`

They are one product and must move together. They had already drifted — the
package said `0.1.0` while the manifest said `19.0.0.2.0`, which reads as
`0.2.0` — with nothing to catch it, because nothing was comparing them.

The release automation writes both. This exists so that a hand edit to one, or a
release that half-succeeds, fails a build rather than shipping an addon whose
declared version does not match the engine it talks to.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PACKAGE = REPOSITORY / "services" / "atlas" / "pyproject.toml"
MANIFEST = REPOSITORY / "addons" / "odoo_atlas" / "__manifest__.py"

#: The Odoo series the addon targets. Part of the manifest version by Odoo's
#: convention, not part of the project's own version.
ODOO_SERIES = "19.0"


def package_version() -> str:
    """The engine package version."""
    data = tomllib.loads(PACKAGE.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def manifest_version() -> str:
    """The addon manifest version, series included."""
    match = re.search(
        r'^\s*"version"\s*:\s*"([^"]+)"', MANIFEST.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        message = f"no version found in {MANIFEST}"
        raise SystemExit(message)
    return match.group(1)


def expected_manifest(package: str) -> str:
    """What the manifest should say for a given package version."""
    return f"{ODOO_SERIES}.{package}"


def write_manifest(version: str) -> None:
    """Set the manifest version, preserving everything around it."""
    text = MANIFEST.read_text(encoding="utf-8")
    updated = re.sub(
        r'^(\s*"version"\s*:\s*")[^"]+(")',
        rf"\g<1>{version}\g<2>",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    MANIFEST.write_text(updated, encoding="utf-8")


def main() -> int:
    """Compare the declarations, or write the manifest. Returns an exit code."""
    package = package_version()
    manifest = manifest_version()
    expected = expected_manifest(package)

    # `--write` is what the release job runs after semantic-release has bumped
    # the package: the manifest follows from it, and deriving it is safer than
    # teaching the release tool an Odoo-specific version format.
    if "--write" in sys.argv:
        if manifest != expected:
            write_manifest(expected)
            print(f"versions: addon set to {expected} from engine {package}")
        else:
            print(f"versions: already consistent at {package}")
        return 0

    if manifest == expected:
        print(f"versions: engine {package}, addon {manifest}")
        return 0

    print("Version declarations disagree:\n", file=sys.stderr)
    print(f"  {PACKAGE.relative_to(REPOSITORY)}: {package}", file=sys.stderr)
    print(f"  {MANIFEST.relative_to(REPOSITORY)}: {manifest}", file=sys.stderr)
    print(f"\nThe manifest should read {expected!r}.", file=sys.stderr)
    print(
        "Both are written by the release automation; edit them there rather than by hand.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
