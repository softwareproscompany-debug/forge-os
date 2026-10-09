# ForgeOS — root Makefile
# Requires: docker + docker compose plugin, python3 + pytest for `test`.

COMPOSE := docker compose

.PHONY: up down logs seed migrate test build-web help

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS=":.*##"} {printf "  %-10s %s\n", $$1, $$2}'

up: ## Build and start the full stack (postgres, redis, api, worker, web)
	$(COMPOSE) up --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Tail logs for all services (Ctrl-C to stop)
	$(COMPOSE) logs -f

seed: ## Load the idempotent Acme Demo Co fixture (needs the stack up)
	# Reuses the api service env (DATABASE_URL etc.); the entrypoint runs
	# `alembic upgrade head` first, so the schema is guaranteed to exist.
	$(COMPOSE) run --rm -v ./infra/seed.py:/seed.py:ro api python3 /seed.py

migrate: ## Apply pending Alembic migrations (forge-db) without starting api
	# Runs only the entrypoint's migration step, then exits (`true` = no-op CMD).
	$(COMPOSE) run --rm --entrypoint /entrypoint.sh api true

test: ## Run pytest across packages + api
	python3 -m pytest packages/forge-db packages/forge-llm packages/forge-channels apps/api -q

build-web: ## Build only the web (nginx) image
	$(COMPOSE) build web
