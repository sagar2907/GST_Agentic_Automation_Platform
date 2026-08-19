.PHONY: help setup test lint fmt reconcile experiments report repro clean

help:
	@echo "setup       install dependencies and start Postgres"
	@echo "test        full offline suite (no key, no network)"
	@echo "lint        ruff check and format check"
	@echo "reconcile   Tier 1 report over a generated cycle"
	@echo "experiments regenerate results/ offline"
	@echo "report      render docs/report.md to PDF"
	@echo "repro       everything reproducible from a clean clone"

setup:
	docker compose up -d
	uv sync --group dev

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .

reconcile:
	uv run gst-recon reconcile

experiments:
	uv run gst-recon experiment all --mode fake

report:
	uv run python scripts/render_report.py

# Everything below runs with no API key and no network. Live experiments are
# deliberately excluded: they cost quota and cannot be reproduced by a reader.
repro: lint test reconcile experiments report

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
