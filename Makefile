PY := .venv/bin/python

.PHONY: install lint test up down seed

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
