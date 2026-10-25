SHELL := /bin/bash
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

.PHONY: all setup test lint format demo eval clean run-env env-up seed

all: setup test

setup:
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating virtual environment..."; \
		/opt/homebrew/bin/python3.11 -m venv $(VENV) || python3 -m venv $(VENV); \
	fi
	@echo "Installing dependencies..."
	@$(PIP) install --upgrade pip
	@$(PIP) install -e ".[dev]"
	@$(VENV)/bin/playwright install chromium
	@if [ ! -f ".env" ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi
	@echo "Setup complete!"

test:
	@echo "Running unit and integration tests..."
	@$(PYTEST) -v tests/

lint:
	@echo "Running linter..."
	@$(RUFF) check .
	@echo "Running type checker..."
	@$(VENV)/bin/mypy core env

format:
	@echo "Formatting code..."
	@$(RUFF) format .

seed:
	@echo "Seeding mock company environment data..."
	@$(PYTHON) -m env.seed.generator

env-up:
	@echo "Starting unified mock company environment on http://localhost:8001..."
	@$(PYTHON) -m env.server

run-env: env-up

demo:
	@echo "Running demo scenario..."
	@$(PYTHON) -m core.cli "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal ERP system, and notify me once done."

eval:
	@echo "Running evaluation suite..."
	@$(PYTHON) -m evals.runner

clean:
	@rm -rf $(VENV) .pytest_cache .ruff_cache build dist *.egg-info
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@echo "Cleaned environment."
