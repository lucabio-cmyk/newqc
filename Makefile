# QConnect-AI monorepo convenience targets.
# Usage: make <target>

PY ?= python3
PIP ?= pip

.PHONY: help install install-shared install-cloud install-edge \
        test test-shared test-cloud test-edge lint format \
        cloud-up cloud-down edge-up edge-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: install-shared install-cloud install-edge ## Install all projects (editable shared)

install-shared: ## Install the shared library in editable mode
	$(PIP) install -e qconnect-ai-shared[dev]

install-cloud: ## Install cloud backend requirements
	$(PIP) install -r qconnect-ai-cloud/requirements.txt

install-edge: ## Install edge requirements
	$(PIP) install -r qconnect-ai-edge/requirements.txt

test: test-shared test-cloud test-edge ## Run all test suites

test-shared: ## Run shared library tests
	$(PY) -m pytest qconnect-ai-shared/tests -q

test-cloud: ## Run cloud tests
	$(PY) -m pytest qconnect-ai-cloud/cloud/tests -q

test-edge: ## Run edge tests
	$(PY) -m pytest qconnect-ai-edge/tests -q

lint: ## Lint all Python with ruff + black --check
	ruff check .
	black --check .

format: ## Auto-format with black
	black .

cloud-up: ## Start the cloud stack
	cd qconnect-ai-cloud && docker compose up -d

cloud-down: ## Stop the cloud stack
	cd qconnect-ai-cloud && docker compose down

edge-up: ## Start the edge stack
	cd qconnect-ai-edge && docker compose up -d

edge-down: ## Stop the edge stack
	cd qconnect-ai-edge && docker compose down

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
