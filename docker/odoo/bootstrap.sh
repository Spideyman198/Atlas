#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Initialises the Odoo database on first boot, then hands control to the
# official Odoo entrypoint.
#
# Without this, `docker compose up` drops the user on Odoo's database-manager
# page and the stack is not actually usable until someone clicks through a form.
# The whole point of M1 is that one command yields a working system.
#
# Idempotent: on every subsequent start this costs one query against
# pg_database and then execs straight through.
# ---------------------------------------------------------------------------
set -euo pipefail

DB_NAME="${ODOO_DB_NAME:-odoo}"
DB_HOST="${HOST:-postgres}"
DB_PORT="${PORT:-5432}"
DB_USER="${USER:-odoo}"
export PGPASSWORD="${PASSWORD:-}"

database_exists() {
	psql --host "${DB_HOST}" --port "${DB_PORT}" --username "${DB_USER}" \
	     --dbname postgres --tuples-only --no-align \
	     --command "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" \
	 | grep -q 1
}

if database_exists; then
	echo "[atlas-bootstrap] database '${DB_NAME}' exists — skipping initialisation"
else
	init_modules="${ODOO_INIT_MODULES:-base}"
	echo "[atlas-bootstrap] initialising '${DB_NAME}' with modules: ${init_modules}"

	# Demo data gives us realistic partners, products, orders and invoices to
	# develop retrieval against. Opt out with ODOO_LOAD_DEMO_DATA=false.
	demo_args=()
	if [ "${ODOO_LOAD_DEMO_DATA:-true}" != "true" ]; then
		demo_args+=(--without-demo=all)
	fi

	# Connection arguments must be passed explicitly. The official entrypoint
	# injects them from HOST/PORT/USER/PASSWORD, but we run before it — without
	# these, Odoo falls back to a local Unix socket and fails immediately.
	# The password travels via PGPASSWORD (exported above) rather than
	# --db_password, keeping it out of the container's process list.
	odoo --db_host "${DB_HOST}" \
	     --db_port "${DB_PORT}" \
	     --db_user "${DB_USER}" \
	     --database "${DB_NAME}" \
	     --init "${init_modules}" \
	     --stop-after-init \
	     "${demo_args[@]}"

	echo "[atlas-bootstrap] initialisation of '${DB_NAME}' complete"
fi

# Hand over to the image's own entrypoint, which injects the database connection
# arguments and starts the server.
exec /entrypoint.sh "$@"
