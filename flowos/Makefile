# FlowOS — Distributed Human + Machine Orchestration Platform
# Makefile for local development and CI operations

.PHONY: help up down migrate test lint format check install clean logs ps

# Default target
.DEFAULT_GOAL := help

# ─────────────────────────────────────────────────────────────────────────────
# Variables
# ─────────────────────────────────────────────────────────────────────────────
COMPOSE         := docker compose
COMPOSE_FILE    := docker-compose.yml
COMPOSE_OVERRIDE := docker-compose.override.yml
PYTHON          := python3
UV              := uv
ALEMBIC         := alembic
PYTEST          := pytest
RUFF            := ruff

# Colours for output
BOLD   := \033[1m
RESET  := \033[0m
GREEN  := \033[32m
YELLOW := \033[33m
RED    := \033[31m
CYAN   := \033[36m

# ─────────────────────────────────────────────────────────────────────────────
# Help
# ─────────────────────────────────────────────────────────────────────────────
help: ## Show this help message
	@echo ""
	@echo "$(BOLD)FlowOS — Development Commands$(RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "$(CYAN)%-20s$(RESET) %s\n", "Target", "Description"} \
	      /^[a-zA-Z_-]+:.*?##/ { printf "$(GREEN)%-20s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure
# ─────────────────────────────────────────────────────────────────────────────
up: ## Start all services (Kafka, Postgres, Temporal, etc.)
	@echo "$(BOLD)$(GREEN)▶ Starting FlowOS infrastructure...$(RESET)"
	$(COMPOSE) -f $(COMPOSE_FILE) -f $(COMPOSE_OVERRIDE) up -d --build
	@echo "$(BOLD)$(GREEN)✔ Services started. Run 'make ps' to check status.$(RESET)"
	@echo ""
	@echo "$(YELLOW)Waiting for services to become healthy...$(RESET)"
	@sleep 5
	@$(COMPOSE) -f $(COMPOSE_FILE) ps

down: ## Stop and remove all services
	@echo "$(BOLD)$(RED)▶ Stopping FlowOS infrastructure...$(RESET)"
	$(COMPOSE) -f $(COMPOSE_FILE) -f $(COMPOSE_OVERRIDE) down --remove-orphans
	@echo "$(BOLD)$(RED)✔ Services stopped.$(RESET)"

down-volumes: ## Stop services and remove all volumes (DESTRUCTIVE)
	@echo "$(BOLD)$(RED)▶ Stopping FlowOS infrastructure and removing volumes...$(RESET)"
	$(COMPOSE) -f $(COMPOSE_FILE) -f $(COMPOSE_OVERRIDE) down --remove-orphans --volumes
	@echo "$(BOLD)$(RED)✔ Services and volumes removed.$(RESET)"

restart: down up ## Restart all services

ps: ## Show running service status
	$(COMPOSE) -f $(COMPOSE_FILE) ps

logs: ## Tail logs from all services (Ctrl+C to stop)
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f

logs-%: ## Tail logs from a specific service (e.g. make logs-kafka)
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f $*

# ─────────────────────────────────────────────────────────────────────────────
# Database Migrations
# ─────────────────────────────────────────────────────────────────────────────
migrate: ## Run Alembic database migrations (upgrade to head)
	@echo "$(BOLD)$(GREEN)▶ Running database migrations...$(RESET)"
	$(ALEMBIC) -c shared/db/alembic.ini upgrade head
	@echo "$(BOLD)$(GREEN)✔ Migrations complete.$(RESET)"

migrate-down: ## Rollback the last Alembic migration
	@echo "$(BOLD)$(YELLOW)▶ Rolling back last migration...$(RESET)"
	$(ALEMBIC) -c shared/db/alembic.ini downgrade -1
	@echo "$(BOLD)$(YELLOW)✔ Rollback complete.$(RESET)"

migrate-history: ## Show Alembic migration history
	$(ALEMBIC) -c shared/db/alembic.ini history --verbose

migrate-current: ## Show current Alembic migration revision
	$(ALEMBIC) -c shared/db/alembic.ini current

migrate-generate: ## Auto-generate a new migration (usage: make migrate-generate MSG="add users table")
	@echo "$(BOLD)$(GREEN)▶ Generating migration: $(MSG)$(RESET)"
	$(ALEMBIC) -c shared/db/alembic.ini revision --autogenerate -m "$(MSG)"

# ─────────────────────────────────────────────────────────────────────────────
# Kafka Topics
# ─────────────────────────────────────────────────────────────────────────────
kafka-topics: ## Create all Kafka topics defined in infra/kafka/topics.json
	@echo "$(BOLD)$(GREEN)▶ Creating Kafka topics...$(RESET)"
	$(PYTHON) -m shared.kafka.admin
	@echo "$(BOLD)$(GREEN)✔ Kafka topics created.$(RESET)"

kafka-list: ## List all Kafka topics
	$(COMPOSE) -f $(COMPOSE_FILE) exec kafka \
	    kafka-topics --bootstrap-server localhost:9092 --list

kafka-describe: ## Describe all Kafka topics
	$(COMPOSE) -f $(COMPOSE_FILE) exec kafka \
	    kafka-topics --bootstrap-server localhost:9092 --describe

# ─────────────────────────────────────────────────────────────────────────────
# Testing
# ─────────────────────────────────────────────────────────────────────────────
test: ## Run the full test suite
	@echo "$(BOLD)$(GREEN)▶ Running tests...$(RESET)"
	$(PYTEST) tests/ -v --tb=short --cov=shared --cov=packages \
	    --cov-report=term-missing --cov-report=html:htmlcov
	@echo "$(BOLD)$(GREEN)✔ Tests complete.$(RESET)"

test-fast: ## Run tests without coverage (faster)
	$(PYTEST) tests/ -v --tb=short -x

test-unit: ## Run only unit tests
	$(PYTEST) tests/unit/ -v --tb=short

test-integration: ## Run only integration tests
	$(PYTEST) tests/integration/ -v --tb=short

test-watch: ## Run tests in watch mode (requires pytest-watch)
	ptw tests/ -- -v --tb=short

# ─────────────────────────────────────────────────────────────────────────────
# Linting & Formatting
# ─────────────────────────────────────────────────────────────────────────────
lint: ## Run ruff linter on all Python files
	@echo "$(BOLD)$(GREEN)▶ Running linter...$(RESET)"
	$(RUFF) check shared/ packages/ tests/
	@echo "$(BOLD)$(GREEN)✔ Lint complete.$(RESET)"

lint-fix: ## Run ruff linter and auto-fix issues
	$(RUFF) check --fix shared/ packages/ tests/

format: ## Format all Python files with ruff
	@echo "$(BOLD)$(GREEN)▶ Formatting code...$(RESET)"
	$(RUFF) format shared/ packages/ tests/
	@echo "$(BOLD)$(GREEN)✔ Formatting complete.$(RESET)"

format-check: ## Check formatting without modifying files
	$(RUFF) format --check shared/ packages/ tests/

typecheck: ## Run mypy type checking
	@echo "$(BOLD)$(GREEN)▶ Running type checks...$(RESET)"
	mypy shared/ packages/
	@echo "$(BOLD)$(GREEN)✔ Type checks complete.$(RESET)"

check: lint format-check typecheck ## Run all checks (lint + format + typecheck)
	@echo "$(BOLD)$(GREEN)✔ All checks passed.$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Installation
# ─────────────────────────────────────────────────────────────────────────────
install: ## Install all Python dependencies using uv
	@echo "$(BOLD)$(GREEN)▶ Installing dependencies...$(RESET)"
	$(UV) sync --all-extras
	@echo "$(BOLD)$(GREEN)✔ Dependencies installed.$(RESET)"

install-dev: ## Install dependencies including dev extras
	$(UV) sync --all-extras --dev

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────
clean: ## Remove Python cache files and build artifacts
	@echo "$(BOLD)$(YELLOW)▶ Cleaning up...$(RESET)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".coverage" -delete 2>/dev/null || true
	@echo "$(BOLD)$(YELLOW)✔ Cleanup complete.$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Development Shortcuts
# ─────────────────────────────────────────────────────────────────────────────
dev: up kafka-topics migrate ## Full local dev bootstrap: start services, create topics, run migrations
	@echo ""
	@echo "$(BOLD)$(GREEN)════════════════════════════════════════════$(RESET)"
	@echo "$(BOLD)$(GREEN)  FlowOS local dev environment is ready!    $(RESET)"
	@echo "$(BOLD)$(GREEN)════════════════════════════════════════════$(RESET)"
	@echo ""
	@echo "  Kafka UI:       http://localhost:8080"
	@echo "  Temporal UI:    http://localhost:8088"
	@echo "  API:            http://localhost:8000"
	@echo "  Postgres:       localhost:5432"
	@echo ""

shell-%: ## Open a shell in a running service container (e.g. make shell-postgres)
	$(COMPOSE) -f $(COMPOSE_FILE) exec $* /bin/bash || \
	$(COMPOSE) -f $(COMPOSE_FILE) exec $* /bin/sh
