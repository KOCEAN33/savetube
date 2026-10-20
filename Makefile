SHELL := bash
ENV := set -a; [ -f .env ] && . ./.env; set +a;

.PHONY: check lint web

# The DB-gated tests skip when SAVETUBE_TEST_DATABASE_URL is unset, so the gate
# refuses to run without it.
check:
	@$(ENV) [ -n "$$SAVETUBE_TEST_DATABASE_URL" ] || { echo "SAVETUBE_TEST_DATABASE_URL is unset (see .env.sample)"; exit 1; }; \
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

web:
	uv run savetube web
