#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Creates the dedicated Atlas database and enables pgvector.
#
# Runs exactly once, on first initialisation of an empty data directory. It does
# NOT run against an existing volume — if you change this file, you must
# `make clean` (destroying data) or apply the change by hand.
#
# Odoo's own database is created by Odoo on first boot (docker/odoo/bootstrap.sh),
# not here, so that Odoo owns its schema end to end. See ADR-0004 for why the two
# databases are separate.
# ---------------------------------------------------------------------------
set -euo pipefail

ATLAS_DB_NAME="${ATLAS_DB_NAME:-atlas}"

echo "[atlas-initdb] creating database '${ATLAS_DB_NAME}'"
psql --username "${POSTGRES_USER}" --dbname postgres --set ON_ERROR_STOP=1 <<-SQL
	CREATE DATABASE "${ATLAS_DB_NAME}"
	    OWNER    "${POSTGRES_USER}"
	    ENCODING 'UTF8';
SQL

echo "[atlas-initdb] enabling pgvector in '${ATLAS_DB_NAME}'"
psql --username "${POSTGRES_USER}" --dbname "${ATLAS_DB_NAME}" --set ON_ERROR_STOP=1 <<-SQL
	CREATE EXTENSION IF NOT EXISTS vector;
SQL

installed_version="$(
	psql --username "${POSTGRES_USER}" --dbname "${ATLAS_DB_NAME}" \
	     --tuples-only --no-align \
	     --command "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
)"

echo "[atlas-initdb] pgvector ${installed_version} ready in '${ATLAS_DB_NAME}'"

# ADR-0004 depends on `hnsw.iterative_scan`, introduced in pgvector 0.8. Fail loudly
# now rather than silently degrading retrieval recall under ACL pre-filters later.
required_major_minor="0.8"
installed_major_minor="$(echo "${installed_version}" | cut -d. -f1,2)"
if [ "$(printf '%s\n%s\n' "${required_major_minor}" "${installed_major_minor}" | sort -V | head -n1)" != "${required_major_minor}" ]; then
	echo "[atlas-initdb] FATAL: pgvector >= ${required_major_minor} required, found ${installed_version}" >&2
	exit 1
fi
