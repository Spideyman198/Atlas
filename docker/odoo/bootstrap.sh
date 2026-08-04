#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Initialises the Odoo database on first boot, then hands control to the
# official Odoo entrypoint.
#
# Without this, `docker compose up` leaves the user on Odoo's database-manager page
# and the stack is not usable until someone completes a form.
#
# Idempotent: on every subsequent start this costs one query against pg_database
# and then execs straight through.
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
	#
	# Odoo 19 does not install demo data unless asked: `--without-demo` is the
	# default and `--with-demo` is the opt-in. So the flag goes on the *true*
	# branch. Earlier releases had it the other way round, which is how this
	# came to be inverted — the database came up empty while the log said it
	# had been seeded.
	demo_args=()
	if [ "${ODOO_LOAD_DEMO_DATA:-true}" = "true" ]; then
		demo_args+=(--with-demo)
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

# Confine the server to the database this script bootstrapped.
#
# The engine's pgvector database lives in the same PostgreSQL cluster, so Odoo
# can see it, and `dbfilter = .*` in odoo.conf matched it along with every
# throwaway test database. Serving one is not a degraded experience but a 500:
# it has no `ir_module_module`, so the registry fails to build and every request
# ends in `KeyError: 'ir.http'`. A session pinned to it stays broken until the
# cookie is cleared, because the database is remembered per browser session.
#
# Passed here rather than written into odoo.conf because the name is
# configurable and that file is static and mounted read-only.
exec /entrypoint.sh "$@" --db-filter="^${DB_NAME}$"
