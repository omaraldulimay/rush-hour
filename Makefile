PY := .venv/bin/python

.PHONY: install lint test up down seed local local-stop local-status local-reset

install:
	python3.12 -m venv .venv
	$(PY) -m pip install -r requirements-dev.txt

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

test:
	$(PY) -m pytest

up:
	docker compose up --build -d

down:
	docker compose down -v

# Seed a running stack (the compose "migrate" service already seeds on `make up`).
seed:
	docker compose run --rm migrate

# Everything on this machine without Docker (postgres, redis, prometheus, services, Control Room).
# State lives in .local/. See docs/local.md.
local:
	$(PY) -m scripts.local.stack up

local-stop:
	$(PY) -m scripts.local.stack stop

local-status:
	$(PY) -m scripts.local.stack status

# Stop and wipe the local database, Redis and Prometheus data.
local-reset:
	$(PY) -m scripts.local.stack reset
