"""Everything a published number depends on, captured with the number.

A latency figure without the machine, the database version and the commit that
produced it is not a measurement, it is an anecdote. This records the lot, and
every results file carries it.

The dirty-tree flag matters more than it looks. A benchmark run against
uncommitted changes cannot be reproduced by checking out the recorded commit,
and a table that does not say so invites somebody to try.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

import psycopg

REPOSITORY = Path(__file__).resolve().parent.parent


async def capture(connection: psycopg.AsyncConnection[Any]) -> dict[str, Any]:
    """Describe the machine, the database and the code under test."""
    return {
        "git": _git(),
        "database": await _database(connection),
        "host": _host(),
    }


def _git() -> dict[str, Any]:
    """The commit, and whether the tree matched it.

    Three sources, in order. The environment variables are what `make bench`
    sets, because the benchmark runs in a container that has no git binary — the
    first run of this recorded an empty commit, which is exactly the gap the
    field exists to close. Reading `.git` directly is the fallback for a bare
    `python -m benchmarks.recall`.
    """
    commit = os.environ.get("ATLAS_BENCH_COMMIT") or _run("git", "rev-parse", "HEAD")
    branch = os.environ.get("ATLAS_BENCH_BRANCH") or _run(
        "git", "rev-parse", "--abbrev-ref", "HEAD"
    )
    if not commit:
        commit = _head_from_files()

    declared = os.environ.get("ATLAS_BENCH_DIRTY")
    if declared is not None:
        dirty = declared not in ("", "0", "false")
    elif _run("git", "--version"):
        dirty = bool(_run("git", "status", "--porcelain"))
    else:
        # No git and nothing declared: unknowable, and saying "clean" would be
        # a claim this cannot support.
        dirty = True
    return {
        "commit": commit or "unknown",
        "branch": branch or "unknown",
        "dirty": dirty,
        # Said in words as well as a boolean, because a boolean in a JSON file
        # is easy to skim past and this one invalidates reproduction.
        "note": (
            "working tree had uncommitted changes, or cleanliness could not be "
            "determined; checking out this commit may not reproduce these numbers"
            if dirty
            else "working tree matched this commit"
        ),
    }


def _head_from_files() -> str:
    """Read the commit from `.git` without a git binary."""
    head = REPOSITORY / ".git" / "HEAD"
    if not head.exists():
        return ""
    content = head.read_text(encoding="utf-8").strip()
    if not content.startswith("ref:"):
        return content
    reference = REPOSITORY / ".git" / content.removeprefix("ref:").strip()
    if reference.exists():
        return reference.read_text(encoding="utf-8").strip()
    packed = REPOSITORY / ".git" / "packed-refs"
    if packed.exists():
        target = content.removeprefix("ref:").strip()
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + target):
                return line.split(" ", 1)[0]
    return ""


async def _database(connection: psycopg.AsyncConnection[Any]) -> dict[str, Any]:
    """PostgreSQL and pgvector versions, and the settings that move the numbers."""
    cursor = await connection.execute("SELECT version()")
    row = await cursor.fetchone()
    full_version = row[0] if row else "unknown"

    cursor = await connection.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )
    row = await cursor.fetchone()
    pgvector = row[0] if row else "not installed"

    settings: dict[str, str] = {}
    for name in (
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "effective_cache_size",
        "max_parallel_maintenance_workers",
    ):
        cursor = await connection.execute("SELECT current_setting(%s)", (name,))
        row = await cursor.fetchone()
        settings[name] = row[0] if row else "unknown"

    return {
        "postgresql": _short_version(full_version),
        "postgresql_full": full_version,
        "pgvector": pgvector,
        "settings": settings,
    }


def _host() -> dict[str, Any]:
    """CPU, memory and container context.

    Read from `/proc` where it exists, because that reports what the container
    was actually given rather than what the host owns. On Docker Desktop those
    differ, and the difference is the number being measured.
    """
    host: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": None,
        "cpu_model": None,
        "memory_gb": None,
        "containerised": Path("/.dockerenv").exists(),
    }

    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        text = cpuinfo.read_text(encoding="utf-8", errors="replace")
        host["cpu_count"] = text.count("processor\t:")
        match = re.search(r"model name\s*:\s*(.+)", text)
        if match:
            host["cpu_model"] = match.group(1).strip()

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        match = re.search(r"MemTotal:\s*(\d+) kB", meminfo.read_text(encoding="utf-8"))
        if match:
            host["memory_gb"] = round(int(match.group(1)) / (1024 * 1024), 1)

    return host


def describe(environment: dict[str, Any]) -> str:
    """One readable block, for the top of a printed report."""
    git = environment["git"]
    database = environment["database"]
    host = environment["host"]
    lines = [
        f"commit      {git['commit'][:12]} ({git['branch']})"
        + ("  [dirty tree]" if git["dirty"] else ""),
        f"postgresql  {database['postgresql']}, pgvector {database['pgvector']}",
        f"host        {host['cpu_model'] or 'unknown cpu'}, "
        f"{host['cpu_count'] or '?'} cores, {host['memory_gb'] or '?'} GB"
        + ("  [container]" if host["containerised"] else ""),
    ]
    return "\n".join(lines)


def _short_version(full: str) -> str:
    """`PostgreSQL 17.2` from the banner, which is a paragraph."""
    match = re.match(r"(PostgreSQL \S+)", full)
    return match.group(1) if match else full


def _run(*command: str) -> str:
    """A git command, or an empty string where git is unavailable."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""
