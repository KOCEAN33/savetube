SHELL := bash
ENV := set -a; [ -f .env ] && . ./.env; set +a;
# check and web run from the repo root, so pydantic-settings finds .env in the
# working directory. lint runs in backend/, because ruff caches in the working directory.
PY := uv run --project backend

.PHONY: check lint web

# The DB-gated tests skip when SAVETUBE_TEST_DATABASE_URL is unset, so the gate
# refuses to run without it.
check:
	@$(ENV) [ -n "$$SAVETUBE_TEST_DATABASE_URL" ] || { echo "SAVETUBE_TEST_DATABASE_URL is unset (see .env.sample)"; exit 1; }; \
	$(PY) pytest -q backend/tests

lint:
	cd backend && uv run ruff check
	cd backend && uv run ruff format --check

web:
	$(PY) savetube web
