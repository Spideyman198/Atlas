"""Reproducible benchmarks for retrieval.

Standalone on purpose: these scripts create and drop tables, so they are a
repository tool rather than part of the shipped `atlas` package. Nothing in
`services/atlas/` imports them.

Every script writes a timestamped JSON and CSV file to `results/`, carrying the
commit, the database versions and the host it ran on. See `README.md`.
"""
