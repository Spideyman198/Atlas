"""Writing results in a form somebody can check a published table against.

JSON and CSV for the same run. JSON carries the environment; CSV carries only
the rows, because a spreadsheet is where a table gets compared against the one
in the documentation and nested objects do not survive that.

Filenames are timestamped in UTC and sortable, so a directory listing is a
history.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DIRECTORY = Path(__file__).resolve().parent / "results"


def write(
    kind: str,
    *,
    rows: list[dict[str, Any]],
    environment: dict[str, Any],
    parameters: dict[str, Any],
) -> dict[str, Path]:
    """Write one run as JSON and CSV. Returns the paths written.

    Args:
        kind: `recall` or `latency`. Becomes part of the filename.
        rows: The measurements, one dict per row.
        environment: From `environment.capture`.
        parameters: What the run was asked for, including the exact command.
    """
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-{kind}"

    json_path = DIRECTORY / f"{stem}.json"
    json_path.write_text(
        json.dumps(
            {
                "kind": kind,
                "recorded_at": datetime.now(UTC).isoformat(),
                "parameters": parameters,
                "environment": environment,
                "rows": rows,
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )

    csv_path = DIRECTORY / f"{stem}.csv"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    return {"json": json_path, "csv": csv_path}


def percentile(values: list[float], point: int) -> float:
    """The nth percentile, nearest-rank.

    Nearest-rank rather than interpolated: with forty samples an interpolated
    p95 invents a number between two measurements, and the honest answer is one
    of the measurements.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * point / 100), len(ordered) - 1)
    return ordered[index]
