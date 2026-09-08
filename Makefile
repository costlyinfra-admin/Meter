# Meter — root orchestration.
# One entry point for installing, testing, and linting both packages.
# Backend = Python (backend/), Web = React+TypeScript (web/).

BACKEND := backend
WEB := web
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help install install-backend install-web test test-backend test-web test-sdk \
        lint lint-backend lint-web format db-migrate db-seed db-reset api web clean

help:
	@echo "Meter make targets:"
	@echo "  make install     - install backend (venv) + web (npm) dependencies"
	@echo "  make test        - run backend + web test suites"
	@echo "  make lint        - lint backend (ruff) + web (eslint)"
	@echo "  make format      - auto-format backend (ruff) + web (prettier)"
	@echo "  make db-migrate  - apply DB migrations (needs DATABASE_URL + Postgres)"
	@echo "  make db-seed     - apply migrations and seed one demo tenant"
	@echo "  make db-reset    - wipe the demo tenant and re-seed it fresh"
	@echo "  make api         - run the backend API (uvicorn, port 8000)"
	@echo "  make web         - run the web dev server (vite, port 5173)"
	@echo "  make demo        - one-command seeded demo (throwaway DB + API + web)"
	@echo "  make clean       - remove virtualenv, node_modules, build caches"

# ---- install -------------------------------------------------------------
install: install-backend install-web

install-backend:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e "$(BACKEND)[dev]"

install-web:
	cd $(WEB) && npm install

# ---- test ----------------------------------------------------------------
test: test-backend test-web

test-backend:
	cd $(BACKEND) && .venv/bin/python -m pytest

test-web:
	cd $(WEB) && npm test

# Metering SDKs (M7): Python uses the backend venv's pytest; Node uses node --test.
test-sdk:
	$(VENV)/bin/pip install -q -e "sdk/python[dev]"
	cd sdk/python && ../../$(VENV)/bin/pytest
	cd sdk/node && node --test

# ---- lint ----------------------------------------------------------------
lint: lint-backend lint-web

lint-backend:
	cd $(BACKEND) && .venv/bin/python -m ruff check .

lint-web:
	cd $(WEB) && npm run lint

# ---- format --------------------------------------------------------------
format:
	cd $(BACKEND) && .venv/bin/python -m ruff format .
	cd $(WEB) && npm run format

# ---- database ------------------------------------------------------------
# Both need DATABASE_URL pointing at a Postgres instance (see README).
db-migrate:
	cd $(BACKEND) && .venv/bin/python -m meter.migrations

db-seed:
	cd $(BACKEND) && .venv/bin/python -m seed

db-reset:
	cd $(BACKEND) && .venv/bin/python -m seed --reset

# Scheduled inference-cost ingest (run on a cadence in production).
ingest:
	cd $(BACKEND) && .venv/bin/python -m meter.inference

# ---- run -----------------------------------------------------------------
# Needs DATABASE_URL + APP_SECRET_KEY in the environment (see .env.example).
api:
	cd $(BACKEND) && .venv/bin/python -m uvicorn --factory meter.api:create_app --reload --port 8000

web:
	cd $(WEB) && npm run dev

# One-command demo: throwaway seeded Postgres + API + web. See docs/demo-script.md.
demo:
	./scripts/demo.sh

# ---- clean ---------------------------------------------------------------
clean:
	rm -rf $(VENV) $(WEB)/node_modules $(WEB)/dist
	find $(BACKEND) -type d -name __pycache__ -prune -exec rm -rf {} +
	find $(BACKEND) -type d -name .pytest_cache -prune -exec rm -rf {} +
