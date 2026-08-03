"""Fail the build on a known vulnerability in a dependency.

A thin wrapper around `pip-audit`, for one reason: `--strict` fails when any
distribution cannot be audited, and Atlas itself cannot be — it has no PyPI
release to compare against. Running without `--strict` fixes that and gives up
the guarantee, because a third-party package that silently fails to resolve then
passes too.

This keeps the guarantee and drops the false failure: every skip is an error
except the project's own distribution.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

#: Distributions allowed to be unauditable. Only the project itself: it is built
#: from this repository and its dependencies are audited individually.
EXPECTED_SKIPS = {"atlas"}


def run_audit() -> dict[str, Any]:
    """Run pip-audit over the current environment and return its report."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--progress-spinner",
            "off",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not completed.stdout.strip():
        message = f"pip-audit produced no report: {completed.stderr.strip()}"
        raise SystemExit(message)
    return dict(json.loads(completed.stdout))


def main() -> int:
    """Audit, and report anything vulnerable or unexpectedly unaudited."""
    report = run_audit()

    vulnerable = [
        (dependency["name"], dependency["version"], vulnerability)
        for dependency in report.get("dependencies", [])
        for vulnerability in dependency.get("vulns", [])
    ]
    unexpected = [
        skipped
        for skipped in report.get("fixes", []) + report.get("skipped", [])
        if str(skipped.get("name", "")).lower() not in EXPECTED_SKIPS
    ]

    if vulnerable:
        print("Known vulnerabilities:\n", file=sys.stderr)
        for name, version, vulnerability in vulnerable:
            fixes = ", ".join(vulnerability.get("fix_versions", [])) or "none published"
            print(
                f"  {name} {version}: {vulnerability.get('id')} — fixed in {fixes}",
                file=sys.stderr,
            )

    if unexpected:
        print("\nDistributions that could not be audited:\n", file=sys.stderr)
        for skipped in unexpected:
            print(f"  {skipped.get('name')}: {skipped.get('reason')}", file=sys.stderr)
        print(
            "\nAn unauditable dependency is not a pass. Either it is misnamed, or "
            "it is not on PyPI and needs checking by hand.",
            file=sys.stderr,
        )

    if vulnerable or unexpected:
        return 1

    audited = len(report.get("dependencies", []))
    print(f"pip-audit: {audited} distributions, no known vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
