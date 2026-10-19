.PHONY: up down test lint check recon spike

up:
	docker compose up -d redis

down:
	docker compose down

test:
	uv run pytest tests -v

lint:
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

check:
	uv run ruff check src tests scripts

recon:
	uv run python scripts/quota_recon.py

spike:
	uv run python scripts/spike_checkpointer.py
