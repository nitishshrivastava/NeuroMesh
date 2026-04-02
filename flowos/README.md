# FlowOS

**Distributed Human + Machine Orchestration Platform**

FlowOS is an event-driven workflow orchestration platform that coordinates humans, machines, and AI agents across a shared Kafka event bus. Every participant — human developer, CI/CD build runner, or LangGraph AI agent — is a first-class **Agent** with a CLI interface, a Git-backed local workspace, and a well-defined event contract.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Technology Stack](#technology-stack)
- [Repository Layout](#repository-layout)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [1. Clone and configure](#1-clone-and-configure)
  - [2. Start infrastructure](#2-start-infrastructure)
  - [3. Create Kafka topics](#3-create-kafka-topics)
  - [4. Run database migrations](#4-run-database-migrations)
  - [5. Start application services](#5-start-application-services)
  - [6. Open the UI](#6-open-the-ui)
- [CLI Reference](#cli-reference)
  - [Workspace commands](#workspace-commands)
  - [Workflow commands](#workflow-commands)
  - [Work commands](#work-commands)
  - [AI commands](#ai-commands)
  - [Run commands](#run-commands)
- [Workflow DSL](#workflow-dsl)
  - [Schema reference](#schema-reference)
  - [Example workflows](#example-workflows)
- [Kafka Topics](#kafka-topics)
- [Service Ports](#service-ports)
- [Development](#development)
  - [Install dependencies](#install-dependencies)
  - [Run tests](#run-tests)
  - [Lint and format](#lint-and-format)
- [Environment Variables](#environment-variables)

---

## Architecture Overview

FlowOS separates concerns across four orthogonal planes:

| Plane | Technology | Responsibility |
|---|---|---|
| **Durable orchestration** | Temporal | Workflow state machine, retries, timeouts |
| **Local workspace state** | Git (per agent) | Reversible file-system state, checkpoints |
| **AI reasoning** | LangGraph / LangChain | Code review, design analysis, fix validation |
| **Remote compute** | Kubernetes + Argo | Build, test, and artifact production |

These planes communicate exclusively through **Apache Kafka** — the central nervous system of FlowOS. Every meaningful action (task assignment, checkpoint creation, handoff, build result, AI suggestion) is expressed as a Kafka event on a well-defined topic. No participant polls another; they all react to the shared event stream.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Apache Kafka                                │
│  flowos.workflow.events  flowos.task.events  flowos.agent.events    │
│  flowos.build.events     flowos.ai.events    flowos.workspace.events │
└───────┬──────────────────────┬──────────────────────┬──────────────┘
        │                      │                      │
   ┌────▼────┐           ┌─────▼─────┐         ┌─────▼──────┐
   │Temporal │           │  FlowOS   │         │  FlowOS UI │
   │Orchestr.│           │   API     │         │  (React)   │
   └────┬────┘           └─────┬─────┘         └────────────┘
        │                      │
   ┌────▼────┐           ┌─────▼─────┐   ┌──────────────┐
   │ Human   │           │ Machine   │   │  AI Worker   │
   │ Agent   │           │ Worker    │   │ (LangGraph)  │
   │ (CLI)   │           │ (Argo)    │   └──────────────┘
   └─────────┘           └───────────┘
```

### Key design decisions

- **Kafka fan-out**: The Orchestrator sees every event; the correct agent receives its assignment; the UI receives a real-time stream; the observability layer captures all events for metrics and replay.
- **Git-backed workspaces**: Every agent has a local Git repository. Checkpoints are Git commits. Handoffs transfer branch ownership. State is always reversible.
- **Declarative YAML DSL**: Workflows are defined in YAML, parsed by the orchestrator, and executed as Temporal workflows. No code changes are needed to add a new workflow.
- **Policy engine**: RBAC rules, branch protection, and approval gates are enforced before any state-changing action is committed.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Workflow orchestration | [Temporal](https://temporal.io) 1.25 |
| Event bus | [Apache Kafka](https://kafka.apache.org) 7.7 (Confluent) |
| AI reasoning | [LangGraph](https://langchain-ai.github.io/langgraph/) + [LangChain](https://python.langchain.com) |
| REST + WebSocket API | [FastAPI](https://fastapi.tiangolo.com) + [Uvicorn](https://www.uvicorn.org) |
| Database | [PostgreSQL](https://www.postgresql.org) 16 + [SQLAlchemy](https://www.sqlalchemy.org) 2 (async) |
| Migrations | [Alembic](https://alembic.sqlalchemy.org) |
| Object storage | [MinIO](https://min.io) (S3-compatible) |
| Build execution | [Argo Workflows](https://argoproj.github.io/argo-workflows/) on Kubernetes |
| CLI | [Click](https://click.palletsprojects.com) + [Rich](https://rich.readthedocs.io) |
| Frontend | [React](https://react.dev) + [React Flow](https://reactflow.dev) + [Vite](https://vitejs.dev) |
| Python runtime | Python 3.12 + [uv](https://docs.astral.sh/uv/) |
| Containerisation | Docker Compose (dev) / Kubernetes (prod) |

---

## Repository Layout

```
flowos/                         ← Python monorepo root
├── shared/                     ← Shared library (config, models, Kafka, DB)
│   ├── config.py               ← Pydantic-settings configuration
│   ├── models/                 ← Domain models (Workflow, Task, Agent, …)
│   ├── kafka/                  ← Kafka producer, consumer, topic registry
│   └── db/                     ← SQLAlchemy base, session factory
├── orchestrator/               ← Temporal worker (workflows + activities)
│   ├── workflows/              ← Temporal workflow implementations
│   ├── activities/             ← Temporal activity implementations
│   └── dsl/                    ← YAML DSL parser and validator
├── api/                        ← FastAPI REST + WebSocket server
│   ├── routers/                ← Route handlers (workflows, tasks, agents, …)
│   └── ws_server.py            ← WebSocket server (Kafka → browser)
├── cli/                        ← Click CLI (flowos command)
│   ├── commands/               ← Sub-command modules
│   ├── workspace_manager.py    ← Git-backed workspace operations
│   └── agent.py                ← Agent loop (poll → accept → complete)
├── workers/                    ← Machine and AI task workers
│   ├── machine_worker.py       ← Build/test task executor (Argo)
│   ├── ai_worker.py            ← AI reasoning task executor (LangGraph)
│   ├── argo_client.py          ← Argo Workflows API client
│   └── artifact_uploader.py    ← MinIO artifact upload helper
├── ai/                         ← LangGraph reasoning graphs
│   ├── graphs/                 ← code_review.py, fix_validation.py
│   ├── tools/                  ← git_tool, build_tool, search_tool, …
│   └── context_loader.py       ← Workspace context loader for AI graphs
├── policy/                     ← Policy engine (RBAC, branch protection)
│   ├── engine.py               ← Policy evaluation entry point
│   └── rules/                  ← branch_protection, approval_gates, …
├── observability/              ← Kafka → metrics + audit log consumer
│   ├── consumer.py             ← Observability Kafka consumer
│   ├── metrics.py              ← Prometheus metrics definitions
│   └── audit_log.py            ← Immutable audit log writer
├── examples/                   ← Example workflow YAML definitions
│   ├── feature_delivery_pipeline.yaml   ← 6-step end-to-end workflow
│   └── build_and_review.yaml            ← 4-step build + review workflow
├── infra/
│   ├── kafka/topics.json       ← Kafka topic definitions
│   └── postgres/init.sql       ← PostgreSQL schema initialisation
├── tests/                      ← Test suite (unit + integration + temporal)
├── docker-compose.yml          ← Infrastructure + application services
├── docker-compose.override.yml ← Local development overrides (hot-reload)
├── .env.example                ← Environment variable reference
├── pyproject.toml              ← Python project metadata + dependencies
└── Makefile                    ← Development convenience targets

ui/                             ← React frontend (separate from Python monorepo)
├── src/
│   ├── components/             ← WorkflowGraph, TaskPanel, EventTimeline, …
│   ├── hooks/                  ← useWorkflowSocket, useWorkflowGraph
│   ├── store/                  ← Zustand workflow state store
│   ├── api/                    ← REST API client
│   └── App.tsx                 ← Root application component
├── vite.config.ts
└── package.json
```

---

## Quick Start

### Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| Docker | 24.x | [docs.docker.com](https://docs.docker.com/get-docker/) |
| Docker Compose | 2.x (plugin) | bundled with Docker Desktop |
| Python | 3.12 | [python.org](https://www.python.org/downloads/) |
| uv | 0.5+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20 LTS | [nodejs.org](https://nodejs.org/) |

### 1. Clone and configure

```bash
git clone https://github.com/your-org/flowos.git
cd flowos/flowos

# Copy the environment variable template
cp .env.example .env

# Edit .env and set at minimum:
#   OPENAI_API_KEY   — required for the AI worker
#   APP_SECRET_KEY   — change for any non-development environment
```

### 2. Start infrastructure

```bash
# Start Kafka, PostgreSQL, Temporal, and MinIO
make up

# Or directly with Docker Compose:
docker compose up -d

# Check service health
make ps
```

Wait for all services to report **healthy** (typically 60–90 seconds on first run while Temporal initialises).

### 3. Create Kafka topics

```bash
make kafka-topics
```

This runs `python -m shared.kafka.admin` which reads `infra/kafka/topics.json` and creates all required topics with the correct partition counts and retention settings.

### 4. Run database migrations

```bash
make migrate
```

This runs `alembic upgrade head` to apply all schema migrations to PostgreSQL.

### 5. Start application services

```bash
# Start API, orchestrator, machine worker, AI worker, and observability consumer
docker compose --profile app up -d

# Or start everything including the UI:
docker compose --profile full up -d
```

For local development with hot-reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml --profile app up -d
```

### 6. Open the UI

| Service | URL |
|---|---|
| FlowOS UI | http://localhost:3000 |
| FlowOS API | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Temporal UI | http://localhost:8088 |
| Kafka UI | http://localhost:8080 |
| MinIO Console | http://localhost:9001 |

---

## CLI Reference

Install the CLI into your Python environment:

```bash
cd flowos
uv sync
# The 'flowos' command is now available
flowos --help
```

### Workspace commands

Workspaces are Git-backed directories that track an agent's local state for a task.

```bash
# Initialise a new workspace
flowos workspace init \
    --agent-id $(python -c "import uuid; print(uuid.uuid4())") \
    --task-id <task-uuid> \
    --branch feat/my-feature

# Show workspace status
flowos workspace status

# Create a checkpoint (Git commit with metadata)
flowos workspace checkpoint "feat: implemented authentication"

# List checkpoints
flowos workspace log

# Revert to a previous checkpoint
flowos workspace revert <checkpoint-id>

# Create a task branch
flowos workspace branch --task-id <task-uuid>

# Hand off workspace to another agent
flowos workspace handoff --handoff-id <handoff-uuid> --to-agent <agent-uuid>

# Archive the workspace when done
flowos workspace archive
```

### Workflow commands

```bash
# Start a workflow from a YAML definition
flowos workflow start \
    --definition examples/feature_delivery_pipeline.yaml \
    --input feature_branch=feat/my-feature \
    --input ticket_id=JIRA-123

# Validate a workflow definition without starting it
flowos workflow validate --definition examples/build_and_review.yaml

# Check workflow status
flowos workflow status <workflow-id>

# List running workflows
flowos workflow list --status running

# Pause a workflow
flowos workflow pause <workflow-id> --reason "waiting for dependency"

# Resume a paused workflow
flowos workflow resume <workflow-id>

# Stop a workflow
flowos workflow stop <workflow-id> --reason "cancelled by user"
```

### Work commands

```bash
# List tasks assigned to this agent
flowos work list

# Accept a task assignment
flowos work accept <task-id>

# Mark a task as complete
flowos work complete <task-id> --output artifact_url=s3://artifacts/build-123.tar.gz

# Fail a task with a reason
flowos work fail <task-id> --reason "build failed: missing dependency"

# Request a handoff to another agent
flowos work handoff <task-id> --to-agent <agent-uuid> --reason "domain expert needed"
```

### AI commands

```bash
# Run an AI code review on the current workspace
flowos ai review

# Run AI fix validation
flowos ai validate-fix --checkpoint <checkpoint-id>

# Ask the AI for a design suggestion
flowos ai suggest --context "implementing OAuth2 login"
```

### Run commands

```bash
# Run a build task directly (machine worker)
flowos run build --branch feat/my-feature --command "make build test"

# Run a test task
flowos run test --branch feat/my-feature
```

---

## Workflow DSL

Workflows are defined in YAML and parsed by the `orchestrator.dsl.parser.DSLParser`. The DSL is the declarative contract between workflow authors and the orchestrator.

### Schema reference

```yaml
name: my-workflow          # Required. Alphanumeric, hyphens, underscores, dots.
version: "1.0.0"           # Optional. Defaults to "1.0.0".
description: >             # Optional. Human-readable description.
  Multi-line description.

inputs:                    # Optional. Runtime inputs with default values.
  feature_branch: "main"   # Overridable with --input key=value at start time.
  ticket_id: null          # null = required at runtime.

outputs:                   # Optional. Outputs collected from completed steps.
  artifact_url: null
  review_status: null

steps:                     # Required. At least one step.
  - id: my-step            # Required. Unique within the workflow. [a-zA-Z0-9_-]
    name: "My Step"        # Required. Human-readable name.
    description: >         # Optional.
      What this step does.
    agent_type: human      # Required. One of: human | ai | build | deploy |
                           #   review | approval | notification | integration | analysis
    depends_on:            # Optional. List of step IDs this step waits for.
      - previous-step
    timeout_secs: 86400    # Optional. Step timeout in seconds. Default: 86400.
    retry_count: 0         # Optional. Number of retries on failure. Default: 0.
    inputs:                # Optional. Step-level inputs (supports {{ }} templates).
      plan: "{{ steps.design.outputs.plan }}"
      branch: "{{ inputs.feature_branch }}"
    tags:                  # Optional. Arbitrary string tags.
      - human
      - review

tags:                      # Optional. Workflow-level tags.
  - feature
  - delivery
```

**Template expressions** (`{{ }}`) are resolved at runtime:
- `{{ inputs.key }}` — references a workflow-level input
- `{{ steps.<step-id>.outputs.<key> }}` — references an output from a completed step

### Example workflows

| File | Description |
|---|---|
| [`examples/feature_delivery_pipeline.yaml`](examples/feature_delivery_pipeline.yaml) | Full 6-step workflow: design → implement → AI review + build → human review → publish |
| [`examples/build_and_review.yaml`](examples/build_and_review.yaml) | Simple 4-step workflow: prepare → build → AI review → human review |

Start an example workflow:

```bash
# 6-step feature delivery pipeline
flowos workflow start \
    --definition examples/feature_delivery_pipeline.yaml \
    --input feature_branch=feat/my-feature \
    --input ticket_id=JIRA-123

# 4-step build and review
flowos workflow start \
    --definition examples/build_and_review.yaml \
    --input feature_branch=feat/my-hotfix
```

---

## Kafka Topics

All FlowOS events flow through these Kafka topics:

| Topic | Events |
|---|---|
| `flowos.workflow.events` | WORKFLOW_CREATED, WORKFLOW_STARTED, WORKFLOW_COMPLETED, WORKFLOW_FAILED, WORKFLOW_PAUSED, WORKFLOW_RESUMED |
| `flowos.task.events` | TASK_CREATED, TASK_ASSIGNED, TASK_ACCEPTED, TASK_COMPLETED, TASK_FAILED, TASK_HANDOFF_REQUESTED |
| `flowos.agent.events` | AGENT_REGISTERED, AGENT_ONLINE, AGENT_OFFLINE, AGENT_BUSY, AGENT_IDLE |
| `flowos.build.events` | BUILD_STARTED, BUILD_COMPLETED, BUILD_FAILED, TEST_COMPLETED, ARTIFACT_UPLOADED |
| `flowos.ai.events` | AI_REVIEW_STARTED, AI_REVIEW_COMPLETED, AI_SUGGESTION_CREATED, AI_FIX_VALIDATED |
| `flowos.workspace.events` | WORKSPACE_CREATED, WORKSPACE_CHECKPOINT_CREATED, WORKSPACE_BRANCH_CREATED, WORKSPACE_ARCHIVED |
| `flowos.policy.events` | POLICY_EVALUATED, POLICY_VIOLATION, APPROVAL_REQUESTED, APPROVAL_GRANTED, APPROVAL_DENIED |
| `flowos.observability.events` | METRIC_RECORDED, AUDIT_LOG_ENTRY |

Consumer groups:

| Group | Subscribers |
|---|---|
| `flowos-orchestrator` | Temporal orchestrator worker |
| `flowos-agent-<id>` | Individual agent instances |
| `flowos-ui` | WebSocket server (browser fan-out) |
| `flowos-observability` | Observability consumer (metrics + audit) |

---

## Service Ports

| Service | Port | Protocol | Description |
|---|---|---|---|
| FlowOS API | 8000 | HTTP / WebSocket | REST API + WebSocket server |
| FlowOS UI | 3000 | HTTP | React frontend (nginx) |
| Kafka (external) | 9092 | TCP | Kafka broker (host access) |
| Kafka (internal) | 9093 | TCP | Kafka broker (container-to-container) |
| Kafka JMX | 9101 | TCP | JMX metrics |
| Kafka UI | 8080 | HTTP | Kafka topic management UI |
| PostgreSQL | 5432 | TCP | Database |
| Temporal gRPC | 7233 | gRPC | Temporal frontend |
| Temporal UI | 8088 | HTTP | Temporal workflow visualization |
| MinIO S3 API | 9000 | HTTP | S3-compatible object storage |
| MinIO Console | 9001 | HTTP | MinIO web console |
| Zookeeper | 2181 | TCP | Kafka coordination |
| Observability | 9090 | HTTP | Prometheus metrics endpoint |

---

## Development

### Install dependencies

```bash
cd flowos

# Install all Python dependencies (including dev extras)
make install-dev

# Or directly with uv:
uv sync --all-extras
```

### Run tests

```bash
# Full test suite with coverage
make test

# Unit tests only (fast, no external services required)
make test-unit

# Integration tests (requires Docker Compose infrastructure)
make test-integration

# Run tests without coverage (fastest)
make test-fast
```

### Lint and format

```bash
# Run all checks (lint + format check + type check)
make check

# Lint only
make lint

# Auto-fix lint issues
make lint-fix

# Format code
make format

# Type check with mypy
make typecheck
```

### Database migrations

```bash
# Apply all pending migrations
make migrate

# Rollback the last migration
make migrate-down

# Show migration history
make migrate-history

# Generate a new migration from model changes
make migrate-generate MSG="add users table"
```

### Kafka topic management

```bash
# Create all topics defined in infra/kafka/topics.json
make kafka-topics

# List all topics
make kafka-list

# Describe all topics
make kafka-describe
```

### Infrastructure management

```bash
# Start all infrastructure services
make up

# Stop all services
make down

# Stop and remove all volumes (DESTRUCTIVE — resets all data)
make down-volumes

# Show service status
make ps

# Tail logs from all services
make logs

# Tail logs from a specific service
make logs-kafka
make logs-postgres
make logs-temporal
```

---

## Environment Variables

Copy `.env.example` to `.env` and configure the following variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Yes | `localhost:9092` | Comma-separated Kafka broker addresses |
| `TEMPORAL_HOST` | Yes | `localhost` | Temporal server hostname |
| `TEMPORAL_PORT` | No | `7233` | Temporal gRPC port |
| `TEMPORAL_NAMESPACE` | No | `default` | Temporal namespace |
| `DATABASE_URL` | Yes | `postgresql+asyncpg://flowos:flowos_secret@localhost:5432/flowos` | Async database URL |
| `DATABASE_SYNC_URL` | No | `postgresql://flowos:flowos_secret@localhost:5432/flowos` | Sync database URL (Alembic) |
| `S3_ENDPOINT` | Yes | `http://localhost:9000` | S3-compatible endpoint URL |
| `S3_ACCESS_KEY` | Yes | `flowos_minio` | S3 access key ID |
| `S3_SECRET_KEY` | Yes | `flowos_minio_secret` | S3 secret access key |
| `OPENAI_API_KEY` | Yes* | — | OpenAI API key (*required for AI worker) |
| `FLOWOS_AGENT_ID` | Yes | — | Unique agent UUID |
| `FLOWOS_AGENT_TYPE` | Yes | `human` | Agent type: `human` \| `machine` \| `ai` \| `system` |
| `APP_SECRET_KEY` | Yes | `change-me-…` | JWT signing secret (change in production!) |
| `APP_ENV` | No | `development` | Runtime environment |

See [`.env.example`](.env.example) for the full list of configurable variables with descriptions and defaults.

---

## Contributing

1. Fork the repository and create a feature branch.
2. Install dependencies: `make install-dev`
3. Make your changes and add tests.
4. Run `make check` to ensure all checks pass.
5. Open a pull request.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
