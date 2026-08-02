# ---------------------------------------------------------------------------
# Odoo Atlas — developer entrypoints.
#
# Every target runs inside Docker. There is deliberately no "install a local
# Python 3.12 first" step: the toolchain lives in the `dev` stage of
# docker/atlas/Dockerfile, so a clean machine with Docker can lint, type-check
# and test immediately.
#
# Windows without GNU make:  .\make.ps1 <target>   (mirrors these targets)
# ---------------------------------------------------------------------------
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
# --no-deps keeps the tools container from dragging PostgreSQL up with it.
TOOLS   := $(COMPOSE) --profile tools run --rm --no-deps atlas-tools

.PHONY: help
help: ## List available targets
	@echo "Odoo Atlas — available targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- environment -----------------------------------------------------------

.PHONY: init
init: ## Create .env from the template if it does not exist
	@test -f .env && echo ".env already exists — leaving it alone" \
	  || (cp .env.example .env && echo "created .env from .env.example")

# --- stack -----------------------------------------------------------------

.PHONY: up
up: ## Build and start the full stack in the background
	$(COMPOSE) up --build --detach
	@echo ""
	@echo "  Odoo        http://localhost:$${ODOO_PORT:-8069}   (admin / admin)"
	@echo "  Atlas API   http://127.0.0.1:$${ATLAS_API_PORT:-8000}/docs"
	@echo ""
	@echo "  First boot initialises the Odoo database and can take a few minutes."
	@echo "  Follow it with: make logs"

.PHONY: down
down: ## Stop the stack, keeping all data
	$(COMPOSE) down

.PHONY: restart
restart: ## Restart every service
	$(COMPOSE) restart

.PHONY: ps
ps: ## Show service status and health
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow logs from every service
	$(COMPOSE) logs --follow --tail=100

.PHONY: build
build: ## Rebuild images without starting anything
	$(COMPOSE) build

.PHONY: rebuild
rebuild: ## Rebuild images from scratch, ignoring the layer cache
	$(COMPOSE) build --no-cache

# --- shells ----------------------------------------------------------------

.PHONY: shell-atlas
shell-atlas: ## Open a shell in the Atlas engine container
	$(COMPOSE) exec atlas-api /bin/bash

.PHONY: shell-odoo
shell-odoo: ## Open a shell in the Odoo container
	$(COMPOSE) exec odoo /bin/bash

.PHONY: psql-atlas
psql-atlas: ## Open psql against the Atlas (pgvector) database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-odoo} -d $${ATLAS_DB_NAME:-atlas}

.PHONY: psql-odoo
psql-odoo: ## Open psql against the Odoo database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-odoo} -d $${ODOO_DB_NAME:-odoo}

# --- quality ---------------------------------------------------------------

.PHONY: lint
lint: ## Run ruff (lint + format check)
	$(TOOLS) ruff check .
	$(TOOLS) ruff format --check .

.PHONY: format
format: ## Apply ruff formatting and safe autofixes
	$(TOOLS) ruff check --fix .
	$(TOOLS) ruff format .

.PHONY: type
type: ## Run mypy in strict mode
	$(TOOLS) mypy

.PHONY: imports
imports: ## Verify the architectural layering contracts
	$(TOOLS) lint-imports

.PHONY: test
test: ## Run the unit test suite with coverage
	$(TOOLS) pytest -m unit --cov --cov-report=term-missing

.PHONY: check
check: lint type imports test ## Run everything CI runs

# --- teardown --------------------------------------------------------------

.PHONY: clean
clean: ## Stop the stack and DELETE all data (databases, filestore)
	@echo "This destroys the Odoo and Atlas databases and the Odoo filestore."
	@read -r -p "Type 'yes' to continue: " answer && [ "$$answer" = "yes" ]
	$(COMPOSE) down --volumes --remove-orphans
