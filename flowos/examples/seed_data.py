#!/usr/bin/env python3
"""
examples/seed_data.py — FlowOS Database Seed Script

Populates the FlowOS PostgreSQL database with realistic test data for
local development and smoke testing.  Idempotent: running it multiple
times will skip records that already exist (by checking for existing
workflow/agent names).

Seed data created:
    Agents (3):
        - alice-human       — Human developer (idle)
        - gpt4-ai-agent     — AI reasoning agent (busy)
        - ci-build-agent    — Build/test runner (online)

    Workflows (3):
        - feature-auth-pipeline   — running  (project: core)
        - data-migration-v2       — pending  (project: data)
        - nightly-regression-run  — completed (project: qa)

    Tasks (9 — 3 per workflow):
        - feature-auth-pipeline:
            * design-auth-schema     (completed, assigned to alice)
            * implement-oauth-flow   (in_progress, assigned to gpt4)
            * write-auth-tests       (pending)
        - data-migration-v2:
            * analyse-schema-diff    (pending)
            * generate-migration-sql (pending)
            * validate-migration     (pending)
        - nightly-regression-run:
            * run-unit-tests         (completed, assigned to ci-build)
            * run-integration-tests  (completed, assigned to ci-build)
            * generate-report        (completed, assigned to ci-build)

    Workspaces (2):
        - alice-workspace   — for alice-human
        - ci-workspace      — for ci-build-agent

    Checkpoints (2):
        - checkpoint on design-auth-schema task (committed)
        - checkpoint on run-unit-tests task (verified)

    Handoffs (1):
        - design-auth-schema → implement-oauth-flow
          (alice-human → gpt4-ai-agent, completed)

Usage:
    # From the flowos/ directory:
    python examples/seed_data.py

    # With a custom DATABASE_URL:
    DATABASE_URL=postgresql://user:pass@host:5432/db python examples/seed_data.py

    # Via make:
    make seed
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow running from the flowos/ directory without installing the package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # flowos/
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed_data")

# ── Environment defaults ──────────────────────────────────────────────────────
# Allow overriding via environment variables so the script works both locally
# and inside Docker containers.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://flowos:flowos_secret@localhost:5432/flowos",
)
os.environ.setdefault(
    "DATABASE_SYNC_URL",
    "postgresql://flowos:flowos_secret@localhost:5432/flowos",
)
# Kafka is optional for the seed script — suppress connection errors
os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")


# ── Imports (after path/env setup) ────────────────────────────────────────────
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from shared.db.models import (
    AgentCapabilityORM,
    AgentORM,
    CheckpointORM,
    HandoffORM,
    TaskORM,
    WorkflowORM,
    WorkspaceORM,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ago(minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
    """Return a UTC datetime in the past."""
    delta = timedelta(minutes=minutes, hours=hours, days=days)
    return _utcnow() - delta


def _uid() -> str:
    return str(uuid.uuid4())


# ── Seed data definitions ─────────────────────────────────────────────────────

# Fixed UUIDs so the script is idempotent across runs
AGENT_ALICE_ID    = "aaaaaaaa-0001-0001-0001-000000000001"
AGENT_GPT4_ID     = "aaaaaaaa-0002-0002-0002-000000000002"
AGENT_CI_ID       = "aaaaaaaa-0003-0003-0003-000000000003"

WF_AUTH_ID        = "bbbbbbbb-0001-0001-0001-000000000001"
WF_MIGRATION_ID   = "bbbbbbbb-0002-0002-0002-000000000002"
WF_REGRESSION_ID  = "bbbbbbbb-0003-0003-0003-000000000003"

TASK_DESIGN_ID    = "cccccccc-0001-0001-0001-000000000001"
TASK_OAUTH_ID     = "cccccccc-0002-0002-0002-000000000002"
TASK_TESTS_ID     = "cccccccc-0003-0003-0003-000000000003"
TASK_ANALYSE_ID   = "cccccccc-0004-0004-0004-000000000004"
TASK_GENSQL_ID    = "cccccccc-0005-0005-0005-000000000005"
TASK_VALIDATE_ID  = "cccccccc-0006-0006-0006-000000000006"
TASK_UNIT_ID      = "cccccccc-0007-0007-0007-000000000007"
TASK_INTEG_ID     = "cccccccc-0008-0008-0008-000000000008"
TASK_REPORT_ID    = "cccccccc-0009-0009-0009-000000000009"

WS_ALICE_ID       = "dddddddd-0001-0001-0001-000000000001"
WS_CI_ID          = "dddddddd-0002-0002-0002-000000000002"

CP_DESIGN_ID      = "eeeeeeee-0001-0001-0001-000000000001"
CP_UNIT_ID        = "eeeeeeee-0002-0002-0002-000000000002"

HO_AUTH_ID        = "ffffffff-0001-0001-0001-000000000001"


def _build_agents() -> list[AgentORM]:
    """Build the 3 seed agent records."""
    alice = AgentORM(
        agent_id=AGENT_ALICE_ID,
        name="alice-human",
        agent_type="human",
        status="idle",
        email="alice@flowos.dev",
        display_name="Alice Chen",
        timezone="America/New_York",
        max_concurrent_tasks=3,
        tags=["senior", "backend", "auth"],
        agent_metadata={"team": "platform", "level": "senior"},
        last_heartbeat_at=_ago(minutes=5),
        registered_at=_ago(days=30),
        updated_at=_ago(minutes=5),
    )

    gpt4 = AgentORM(
        agent_id=AGENT_GPT4_ID,
        name="gpt4-ai-agent",
        agent_type="ai",
        status="busy",
        current_task_id=TASK_OAUTH_ID,
        display_name="GPT-4 Turbo Agent",
        timezone="UTC",
        kafka_group_id="ai-agent-group-1",
        max_concurrent_tasks=5,
        tags=["ai", "code-generation", "review"],
        agent_metadata={"model": "gpt-4-turbo", "provider": "openai"},
        last_heartbeat_at=_ago(minutes=1),
        registered_at=_ago(days=14),
        updated_at=_ago(minutes=1),
    )

    ci = AgentORM(
        agent_id=AGENT_CI_ID,
        name="ci-build-agent",
        agent_type="build",
        status="online",
        display_name="CI Build Runner",
        timezone="UTC",
        kafka_group_id="build-agent-group-1",
        max_concurrent_tasks=10,
        tags=["ci", "build", "test", "docker"],
        agent_metadata={"runner": "github-actions", "os": "ubuntu-22.04"},
        last_heartbeat_at=_ago(minutes=2),
        registered_at=_ago(days=60),
        updated_at=_ago(minutes=2),
    )

    return [alice, gpt4, ci]


def _build_agent_capabilities() -> list[AgentCapabilityORM]:
    """Build capability records for the seed agents."""
    caps = [
        # Alice — human developer
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_ALICE_ID,
            name="code_review",
            version="1.0",
            description="Reviews code for correctness, style, and security.",
            parameters={},
            enabled=True,
        ),
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_ALICE_ID,
            name="architecture_design",
            version="1.0",
            description="Designs system architecture and data models.",
            parameters={},
            enabled=True,
        ),
        # GPT-4 — AI agent
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_GPT4_ID,
            name="code_generation",
            version="2.0",
            description="Generates production-quality code from specifications.",
            parameters={"max_tokens": 4096, "temperature": 0.2},
            enabled=True,
        ),
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_GPT4_ID,
            name="test_generation",
            version="1.5",
            description="Generates unit and integration tests.",
            parameters={"framework": "pytest"},
            enabled=True,
        ),
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_GPT4_ID,
            name="documentation",
            version="1.0",
            description="Writes technical documentation and docstrings.",
            parameters={},
            enabled=True,
        ),
        # CI Build Agent
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_CI_ID,
            name="run_tests",
            version="3.0",
            description="Executes test suites (unit, integration, e2e).",
            parameters={"parallel": True, "timeout_secs": 3600},
            enabled=True,
        ),
        AgentCapabilityORM(
            capability_id=_uid(),
            agent_id=AGENT_CI_ID,
            name="build_docker",
            version="2.0",
            description="Builds and pushes Docker images.",
            parameters={"registry": "ghcr.io"},
            enabled=True,
        ),
    ]
    return caps


def _build_workflows() -> list[WorkflowORM]:
    """Build the 3 seed workflow records."""
    auth_definition = {
        "name": "feature-auth-pipeline",
        "version": "1.0.0",
        "description": "End-to-end OAuth 2.0 authentication feature delivery pipeline.",
        "steps": [
            {
                "id": "design",
                "name": "Design Auth Schema",
                "agent_type": "human",
                "timeout_secs": 7200,
            },
            {
                "id": "implement",
                "name": "Implement OAuth Flow",
                "agent_type": "ai",
                "depends_on": ["design"],
                "timeout_secs": 14400,
            },
            {
                "id": "test",
                "name": "Write Auth Tests",
                "agent_type": "ai",
                "depends_on": ["implement"],
                "timeout_secs": 3600,
            },
        ],
    }

    migration_definition = {
        "name": "data-migration-v2",
        "version": "2.0.0",
        "description": "Database schema migration for v2 data model.",
        "steps": [
            {"id": "analyse", "name": "Analyse Schema Diff", "agent_type": "ai"},
            {"id": "generate", "name": "Generate Migration SQL", "agent_type": "ai", "depends_on": ["analyse"]},
            {"id": "validate", "name": "Validate Migration", "agent_type": "build", "depends_on": ["generate"]},
        ],
    }

    regression_definition = {
        "name": "nightly-regression-run",
        "version": "1.0.0",
        "description": "Nightly full regression test suite.",
        "steps": [
            {"id": "unit", "name": "Run Unit Tests", "agent_type": "build"},
            {"id": "integration", "name": "Run Integration Tests", "agent_type": "build", "depends_on": ["unit"]},
            {"id": "report", "name": "Generate Report", "agent_type": "build", "depends_on": ["integration"]},
        ],
    }

    auth_wf = WorkflowORM(
        workflow_id=WF_AUTH_ID,
        name="feature-auth-pipeline",
        status="running",
        trigger="api",
        definition=auth_definition,
        owner_agent_id=AGENT_ALICE_ID,
        project="core",
        inputs={"feature_branch": "feature/oauth-v2", "target_env": "staging"},
        outputs={},
        error_message=None,
        error_details={},
        tags=["auth", "oauth", "feature"],
        created_at=_ago(hours=3),
        started_at=_ago(hours=3),
        completed_at=None,
        updated_at=_ago(minutes=15),
    )

    migration_wf = WorkflowORM(
        workflow_id=WF_MIGRATION_ID,
        name="data-migration-v2",
        status="pending",
        trigger="manual",
        definition=migration_definition,
        owner_agent_id=AGENT_ALICE_ID,
        project="data",
        inputs={"source_version": "1.x", "target_version": "2.0"},
        outputs={},
        error_message=None,
        error_details={},
        tags=["migration", "database", "v2"],
        created_at=_ago(hours=1),
        started_at=None,
        completed_at=None,
        updated_at=_ago(hours=1),
    )

    regression_wf = WorkflowORM(
        workflow_id=WF_REGRESSION_ID,
        name="nightly-regression-run",
        status="completed",
        trigger="scheduled",
        definition=regression_definition,
        owner_agent_id=AGENT_CI_ID,
        project="qa",
        inputs={"suite": "full", "branch": "main"},
        outputs={"passed": 847, "failed": 0, "skipped": 12, "duration_secs": 1842},
        error_message=None,
        error_details={},
        tags=["regression", "nightly", "ci"],
        created_at=_ago(hours=10),
        started_at=_ago(hours=10),
        completed_at=_ago(hours=7),
        updated_at=_ago(hours=7),
    )

    return [auth_wf, migration_wf, regression_wf]


def _build_tasks() -> list[TaskORM]:
    """Build the 9 seed task records (3 per workflow)."""
    # ── feature-auth-pipeline tasks ──────────────────────────────────────────
    design = TaskORM(
        task_id=TASK_DESIGN_ID,
        workflow_id=WF_AUTH_ID,
        name="design-auth-schema",
        description="Design the database schema and API contracts for OAuth 2.0 authentication.",
        task_type="human",
        status="completed",
        priority="high",
        assigned_agent_id=AGENT_ALICE_ID,
        depends_on=[],
        inputs=[{"name": "requirements_doc", "value": "docs/auth-requirements.md"}],
        outputs=[{"name": "schema_doc", "value": "docs/auth-schema.md"}, {"name": "api_spec", "value": "openapi/auth.yaml"}],
        timeout_secs=7200,
        retry_count=0,
        current_retry=0,
        tags=["design", "auth"],
        task_metadata={"story_points": "5", "sprint": "24"},
        created_at=_ago(hours=3),
        assigned_at=_ago(hours=3),
        started_at=_ago(hours=3),
        completed_at=_ago(hours=2),
        updated_at=_ago(hours=2),
    )

    oauth = TaskORM(
        task_id=TASK_OAUTH_ID,
        workflow_id=WF_AUTH_ID,
        name="implement-oauth-flow",
        description="Implement the OAuth 2.0 authorization code flow with PKCE.",
        task_type="ai",
        status="in_progress",
        priority="high",
        assigned_agent_id=AGENT_GPT4_ID,
        depends_on=[TASK_DESIGN_ID],
        inputs=[{"name": "schema_doc", "value": "docs/auth-schema.md"}, {"name": "api_spec", "value": "openapi/auth.yaml"}],
        outputs=[],
        timeout_secs=14400,
        retry_count=1,
        current_retry=0,
        tags=["implementation", "oauth", "ai"],
        task_metadata={"story_points": "13", "sprint": "24"},
        created_at=_ago(hours=2),
        assigned_at=_ago(hours=2),
        started_at=_ago(hours=2),
        completed_at=None,
        updated_at=_ago(minutes=15),
    )

    auth_tests = TaskORM(
        task_id=TASK_TESTS_ID,
        workflow_id=WF_AUTH_ID,
        name="write-auth-tests",
        description="Write comprehensive unit and integration tests for the auth module.",
        task_type="ai",
        status="pending",
        priority="normal",
        assigned_agent_id=None,
        depends_on=[TASK_OAUTH_ID],
        inputs=[],
        outputs=[],
        timeout_secs=3600,
        retry_count=0,
        current_retry=0,
        tags=["testing", "auth"],
        task_metadata={"story_points": "8", "sprint": "24"},
        created_at=_ago(hours=2),
        assigned_at=None,
        started_at=None,
        completed_at=None,
        updated_at=_ago(hours=2),
    )

    # ── data-migration-v2 tasks ───────────────────────────────────────────────
    analyse = TaskORM(
        task_id=TASK_ANALYSE_ID,
        workflow_id=WF_MIGRATION_ID,
        name="analyse-schema-diff",
        description="Analyse the differences between v1 and v2 database schemas.",
        task_type="ai",
        status="pending",
        priority="normal",
        assigned_agent_id=None,
        depends_on=[],
        inputs=[{"name": "v1_schema", "value": "schemas/v1.sql"}, {"name": "v2_schema", "value": "schemas/v2.sql"}],
        outputs=[],
        timeout_secs=1800,
        retry_count=0,
        current_retry=0,
        tags=["analysis", "migration"],
        task_metadata={"complexity": "medium"},
        created_at=_ago(hours=1),
        assigned_at=None,
        started_at=None,
        completed_at=None,
        updated_at=_ago(hours=1),
    )

    gensql = TaskORM(
        task_id=TASK_GENSQL_ID,
        workflow_id=WF_MIGRATION_ID,
        name="generate-migration-sql",
        description="Generate Alembic migration scripts from the schema diff analysis.",
        task_type="ai",
        status="pending",
        priority="normal",
        assigned_agent_id=None,
        depends_on=[TASK_ANALYSE_ID],
        inputs=[],
        outputs=[],
        timeout_secs=3600,
        retry_count=0,
        current_retry=0,
        tags=["generation", "migration", "sql"],
        task_metadata={"complexity": "high"},
        created_at=_ago(hours=1),
        assigned_at=None,
        started_at=None,
        completed_at=None,
        updated_at=_ago(hours=1),
    )

    validate = TaskORM(
        task_id=TASK_VALIDATE_ID,
        workflow_id=WF_MIGRATION_ID,
        name="validate-migration",
        description="Run the migration against a staging database and validate data integrity.",
        task_type="build",
        status="pending",
        priority="high",
        assigned_agent_id=None,
        depends_on=[TASK_GENSQL_ID],
        inputs=[],
        outputs=[],
        timeout_secs=7200,
        retry_count=2,
        current_retry=0,
        tags=["validation", "migration", "staging"],
        task_metadata={"environment": "staging"},
        created_at=_ago(hours=1),
        assigned_at=None,
        started_at=None,
        completed_at=None,
        updated_at=_ago(hours=1),
    )

    # ── nightly-regression-run tasks ─────────────────────────────────────────
    unit_tests = TaskORM(
        task_id=TASK_UNIT_ID,
        workflow_id=WF_REGRESSION_ID,
        name="run-unit-tests",
        description="Execute the full unit test suite with coverage reporting.",
        task_type="build",
        status="completed",
        priority="normal",
        assigned_agent_id=AGENT_CI_ID,
        depends_on=[],
        inputs=[{"name": "branch", "value": "main"}, {"name": "coverage_threshold", "value": "80"}],
        outputs=[{"name": "passed", "value": "412"}, {"name": "failed", "value": "0"}, {"name": "coverage_pct", "value": "87.3"}],
        timeout_secs=1800,
        retry_count=0,
        current_retry=0,
        tags=["unit-tests", "ci"],
        task_metadata={"runner": "pytest", "workers": "4"},
        created_at=_ago(hours=10),
        assigned_at=_ago(hours=10),
        started_at=_ago(hours=10),
        completed_at=_ago(hours=9, minutes=30),
        updated_at=_ago(hours=9, minutes=30),
    )

    integ_tests = TaskORM(
        task_id=TASK_INTEG_ID,
        workflow_id=WF_REGRESSION_ID,
        name="run-integration-tests",
        description="Execute integration tests against a live test database and Kafka.",
        task_type="build",
        status="completed",
        priority="normal",
        assigned_agent_id=AGENT_CI_ID,
        depends_on=[TASK_UNIT_ID],
        inputs=[{"name": "branch", "value": "main"}],
        outputs=[{"name": "passed", "value": "435"}, {"name": "failed", "value": "0"}, {"name": "skipped", "value": "12"}],
        timeout_secs=3600,
        retry_count=0,
        current_retry=0,
        tags=["integration-tests", "ci"],
        task_metadata={"runner": "pytest", "workers": "2"},
        created_at=_ago(hours=9, minutes=30),
        assigned_at=_ago(hours=9, minutes=30),
        started_at=_ago(hours=9, minutes=30),
        completed_at=_ago(hours=7, minutes=30),
        updated_at=_ago(hours=7, minutes=30),
    )

    report = TaskORM(
        task_id=TASK_REPORT_ID,
        workflow_id=WF_REGRESSION_ID,
        name="generate-report",
        description="Generate HTML test report and publish to GitHub Pages.",
        task_type="build",
        status="completed",
        priority="low",
        assigned_agent_id=AGENT_CI_ID,
        depends_on=[TASK_INTEG_ID],
        inputs=[],
        outputs=[{"name": "report_url", "value": "https://flowos.dev/reports/nightly-2026-04-02"}],
        timeout_secs=600,
        retry_count=0,
        current_retry=0,
        tags=["reporting", "ci"],
        task_metadata={"format": "html"},
        created_at=_ago(hours=7, minutes=30),
        assigned_at=_ago(hours=7, minutes=30),
        started_at=_ago(hours=7, minutes=30),
        completed_at=_ago(hours=7),
        updated_at=_ago(hours=7),
    )

    return [design, oauth, auth_tests, analyse, gensql, validate, unit_tests, integ_tests, report]


def _build_workspaces() -> list[WorkspaceORM]:
    """Build workspace records for agents that need them."""
    alice_ws = WorkspaceORM(
        workspace_id=WS_ALICE_ID,
        agent_id=AGENT_ALICE_ID,
        task_id=None,
        workflow_id=WF_AUTH_ID,
        workspace_type="local",
        status="idle",
        root_path="/workspaces/alice/feature-auth-pipeline",
        git_state={
            "branch": "feature/oauth-v2",
            "commit_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "dirty": False,
            "ahead": 3,
            "behind": 0,
        },
        snapshots=[],
        repo_url="https://github.com/flowos/core.git",
        branch_prefix="flowos",
        disk_usage_mb=245.7,
        max_disk_mb=10240.0,
        tags=["auth", "feature"],
        workspace_metadata={"created_by": "alice-human"},
        created_at=_ago(hours=3),
        updated_at=_ago(hours=2),
    )

    ci_ws = WorkspaceORM(
        workspace_id=WS_CI_ID,
        agent_id=AGENT_CI_ID,
        task_id=None,
        workflow_id=WF_REGRESSION_ID,
        workspace_type="ephemeral",
        status="archived",
        root_path="/workspaces/ci/nightly-regression-run",
        git_state={
            "branch": "main",
            "commit_sha": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
            "dirty": False,
            "ahead": 0,
            "behind": 0,
        },
        snapshots=[],
        repo_url="https://github.com/flowos/core.git",
        branch_prefix="flowos",
        disk_usage_mb=0.0,
        max_disk_mb=10240.0,
        tags=["ci", "regression"],
        workspace_metadata={"created_by": "ci-build-agent"},
        created_at=_ago(hours=10),
        updated_at=_ago(hours=7),
        archived_at=_ago(hours=7),
    )

    return [alice_ws, ci_ws]


def _build_checkpoints() -> list[CheckpointORM]:
    """Build checkpoint records for completed tasks."""
    design_cp = CheckpointORM(
        checkpoint_id=CP_DESIGN_ID,
        task_id=TASK_DESIGN_ID,
        workflow_id=WF_AUTH_ID,
        agent_id=AGENT_ALICE_ID,
        workspace_id=WS_ALICE_ID,
        checkpoint_type="completion",
        status="committed",
        sequence=1,
        git_commit_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        git_branch="feature/oauth-v2",
        git_tag=None,
        message="chore: complete auth schema design — add OAuth 2.0 tables and API spec",
        file_count=4,
        lines_added=287,
        lines_removed=12,
        s3_archive_key=None,
        task_progress=100.0,
        notes="Schema reviewed and approved by tech lead. Ready for implementation.",
        checkpoint_metadata={"reviewed_by": "tech-lead", "approved": True},
        created_at=_ago(hours=2),
        verified_at=_ago(hours=1, minutes=45),
        reverted_at=None,
    )

    unit_cp = CheckpointORM(
        checkpoint_id=CP_UNIT_ID,
        task_id=TASK_UNIT_ID,
        workflow_id=WF_REGRESSION_ID,
        agent_id=AGENT_CI_ID,
        workspace_id=WS_CI_ID,
        checkpoint_type="completion",
        status="verified",
        sequence=1,
        git_commit_sha="b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
        git_branch="main",
        git_tag="nightly-2026-04-02",
        message="ci: nightly unit test run — 412 passed, 0 failed, 87.3% coverage",
        file_count=0,
        lines_added=0,
        lines_removed=0,
        s3_archive_key="checkpoints/nightly-2026-04-02/unit-tests.tar.gz",
        task_progress=100.0,
        notes="All unit tests passed. Coverage above threshold (80%).",
        checkpoint_metadata={"coverage_pct": 87.3, "test_count": 412},
        created_at=_ago(hours=9, minutes=30),
        verified_at=_ago(hours=9, minutes=25),
        reverted_at=None,
    )

    return [design_cp, unit_cp]


def _build_handoffs() -> list[HandoffORM]:
    """Build handoff records for the auth pipeline."""
    handoff = HandoffORM(
        handoff_id=HO_AUTH_ID,
        task_id=TASK_DESIGN_ID,
        workflow_id=WF_AUTH_ID,
        source_agent_id=AGENT_ALICE_ID,
        target_agent_id=AGENT_GPT4_ID,
        handoff_type="delegation",
        status="completed",
        checkpoint_id=CP_DESIGN_ID,
        workspace_id=WS_ALICE_ID,
        context={
            "summary": "Auth schema design is complete. Handing off implementation to AI agent.",
            "deliverables": ["docs/auth-schema.md", "openapi/auth.yaml"],
            "notes": "Please implement the OAuth 2.0 authorization code flow with PKCE. "
                     "Follow the API spec exactly. Use FastAPI + SQLAlchemy async.",
            "acceptance_criteria": [
                "All endpoints in openapi/auth.yaml are implemented",
                "Unit test coverage >= 80%",
                "No hardcoded secrets",
            ],
        },
        priority="high",
        tags=["auth", "implementation"],
        expires_at=None,
        accepted_at=_ago(hours=2),
        completed_at=_ago(hours=2),
        requested_at=_ago(hours=2, minutes=5),
        updated_at=_ago(hours=2),
        handoff_metadata={"handoff_reason": "design_complete"},
    )

    return [handoff]


# ── Main seed function ─────────────────────────────────────────────────────────

def seed(database_url: str | None = None) -> dict[str, int]:
    """
    Seed the FlowOS database with test data.

    Args:
        database_url: Synchronous PostgreSQL URL.  Defaults to the
                      DATABASE_SYNC_URL environment variable.

    Returns:
        A dict mapping entity type to the number of records inserted.
    """
    from shared.config import settings

    sync_url = database_url or settings.database.sync_url
    logger.info("Connecting to database: %s", sync_url.split("@")[-1])

    engine = create_engine(sync_url, echo=False)

    # Verify connection
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version()"))
        version = result.scalar()
        logger.info("Connected to PostgreSQL: %s", version.split(",")[0] if version else "unknown")

    counts: dict[str, int] = {
        "agents": 0,
        "agent_capabilities": 0,
        "workflows": 0,
        "tasks": 0,
        "workspaces": 0,
        "checkpoints": 0,
        "handoffs": 0,
    }

    with Session(engine) as session:
        # ── Agents ────────────────────────────────────────────────────────────
        logger.info("Seeding agents...")
        agents = _build_agents()
        for agent in agents:
            existing = session.get(AgentORM, agent.agent_id)
            if existing is None:
                session.add(agent)
                counts["agents"] += 1
                logger.info("  + Agent: %s (%s)", agent.name, agent.agent_type)
            else:
                logger.info("  ~ Agent already exists: %s", agent.name)
        session.flush()

        # ── Agent capabilities ────────────────────────────────────────────────
        logger.info("Seeding agent capabilities...")
        caps = _build_agent_capabilities()
        for cap in caps:
            # Check by agent_id + name (unique constraint)
            existing = session.execute(
                select(AgentCapabilityORM).where(
                    AgentCapabilityORM.agent_id == cap.agent_id,
                    AgentCapabilityORM.name == cap.name,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(cap)
                counts["agent_capabilities"] += 1
                logger.info("  + Capability: %s → %s", cap.agent_id[:8], cap.name)
            else:
                logger.info("  ~ Capability already exists: %s → %s", cap.agent_id[:8], cap.name)
        session.flush()

        # ── Workflows ─────────────────────────────────────────────────────────
        logger.info("Seeding workflows...")
        workflows = _build_workflows()
        for wf in workflows:
            existing = session.get(WorkflowORM, wf.workflow_id)
            if existing is None:
                session.add(wf)
                counts["workflows"] += 1
                logger.info("  + Workflow: %s (%s)", wf.name, wf.status)
            else:
                logger.info("  ~ Workflow already exists: %s", wf.name)
        session.flush()

        # ── Workspaces ────────────────────────────────────────────────────────
        logger.info("Seeding workspaces...")
        workspaces = _build_workspaces()
        for ws in workspaces:
            existing = session.get(WorkspaceORM, ws.workspace_id)
            if existing is None:
                session.add(ws)
                counts["workspaces"] += 1
                logger.info("  + Workspace: %s (%s)", ws.workspace_id[:8], ws.status)
            else:
                logger.info("  ~ Workspace already exists: %s", ws.workspace_id[:8])
        session.flush()

        # ── Tasks ─────────────────────────────────────────────────────────────
        logger.info("Seeding tasks...")
        tasks = _build_tasks()
        for task in tasks:
            existing = session.get(TaskORM, task.task_id)
            if existing is None:
                session.add(task)
                counts["tasks"] += 1
                logger.info("  + Task: %s (%s)", task.name, task.status)
            else:
                logger.info("  ~ Task already exists: %s", task.name)
        session.flush()

        # ── Checkpoints ───────────────────────────────────────────────────────
        logger.info("Seeding checkpoints...")
        checkpoints = _build_checkpoints()
        for cp in checkpoints:
            existing = session.get(CheckpointORM, cp.checkpoint_id)
            if existing is None:
                session.add(cp)
                counts["checkpoints"] += 1
                logger.info("  + Checkpoint: %s (%s)", cp.checkpoint_id[:8], cp.status)
            else:
                logger.info("  ~ Checkpoint already exists: %s", cp.checkpoint_id[:8])
        session.flush()

        # ── Handoffs ──────────────────────────────────────────────────────────
        logger.info("Seeding handoffs...")
        handoffs = _build_handoffs()
        for ho in handoffs:
            existing = session.get(HandoffORM, ho.handoff_id)
            if existing is None:
                session.add(ho)
                counts["handoffs"] += 1
                logger.info(
                    "  + Handoff: %s → %s (%s)",
                    ho.source_agent_id[:8],
                    (ho.target_agent_id or "unassigned")[:8],
                    ho.status,
                )
            else:
                logger.info("  ~ Handoff already exists: %s", ho.handoff_id[:8])
        session.flush()

        # ── Commit ────────────────────────────────────────────────────────────
        session.commit()

    logger.info("")
    logger.info("═" * 60)
    logger.info("Seed complete!")
    logger.info("  Agents inserted:            %d", counts["agents"])
    logger.info("  Agent capabilities inserted: %d", counts["agent_capabilities"])
    logger.info("  Workflows inserted:          %d", counts["workflows"])
    logger.info("  Workspaces inserted:         %d", counts["workspaces"])
    logger.info("  Tasks inserted:              %d", counts["tasks"])
    logger.info("  Checkpoints inserted:        %d", counts["checkpoints"])
    logger.info("  Handoffs inserted:           %d", counts["handoffs"])
    logger.info("═" * 60)

    return counts


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Seed the FlowOS database with test data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Synchronous PostgreSQL URL "
            "(default: DATABASE_SYNC_URL env var or "
            "postgresql://flowos:flowos_secret@localhost:5432/flowos)"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing seed data before inserting (use with caution).",
    )
    args = parser.parse_args()

    if args.reset:
        logger.warning("--reset flag set: deleting existing seed data...")
        from sqlalchemy import create_engine as _ce
        from shared.config import settings as _s
        _url = args.database_url or _s.database.sync_url
        _engine = _ce(_url, echo=False)
        with Session(_engine) as _session:
            # Delete in reverse FK order
            for _id in [HO_AUTH_ID]:
                _obj = _session.get(HandoffORM, _id)
                if _obj:
                    _session.delete(_obj)
            for _id in [CP_DESIGN_ID, CP_UNIT_ID]:
                _obj = _session.get(CheckpointORM, _id)
                if _obj:
                    _session.delete(_obj)
            for _id in [TASK_DESIGN_ID, TASK_OAUTH_ID, TASK_TESTS_ID,
                        TASK_ANALYSE_ID, TASK_GENSQL_ID, TASK_VALIDATE_ID,
                        TASK_UNIT_ID, TASK_INTEG_ID, TASK_REPORT_ID]:
                _obj = _session.get(TaskORM, _id)
                if _obj:
                    _session.delete(_obj)
            for _id in [WS_ALICE_ID, WS_CI_ID]:
                _obj = _session.get(WorkspaceORM, _id)
                if _obj:
                    _session.delete(_obj)
            for _id in [WF_AUTH_ID, WF_MIGRATION_ID, WF_REGRESSION_ID]:
                _obj = _session.get(WorkflowORM, _id)
                if _obj:
                    _session.delete(_obj)
            for _id in [AGENT_ALICE_ID, AGENT_GPT4_ID, AGENT_CI_ID]:
                _obj = _session.get(AgentORM, _id)
                if _obj:
                    _session.delete(_obj)
            _session.commit()
        logger.info("Existing seed data deleted.")

    try:
        result = seed(database_url=args.database_url)
        sys.exit(0)
    except Exception as exc:
        logger.error("Seed failed: %s", exc, exc_info=True)
        sys.exit(1)
