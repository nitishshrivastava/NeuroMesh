"""
tests/conftest.py — Shared pytest fixtures for FlowOS test suite.

Provides common fixtures used across unit, integration, and temporal tests:
- Sample domain model instances (Workflow, Task, Agent, etc.)
- Mock Kafka producer/consumer
- Temporary workspace directories
- Sample EventRecord builders
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Domain model fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def task_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def agent_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def sample_workflow(workflow_id: str):
    """Return a minimal valid Workflow instance."""
    from shared.models.workflow import Workflow, WorkflowStatus, WorkflowTrigger

    return Workflow(
        workflow_id=workflow_id,
        name="test-workflow",
        status=WorkflowStatus.PENDING,
        trigger=WorkflowTrigger.MANUAL,
    )


@pytest.fixture
def sample_task(task_id: str, workflow_id: str):
    """Return a minimal valid Task instance."""
    from shared.models.task import Task, TaskStatus, TaskType

    return Task(
        task_id=task_id,
        workflow_id=workflow_id,
        name="test-task",
        task_type=TaskType.HUMAN,
        status=TaskStatus.PENDING,
    )


@pytest.fixture
def sample_agent(agent_id: str):
    """Return a minimal valid Agent instance."""
    from shared.models.agent import Agent, AgentStatus, AgentType

    return Agent(
        agent_id=agent_id,
        name="test-agent",
        agent_type=AgentType.HUMAN,
        status=AgentStatus.IDLE,
    )


@pytest.fixture
def sample_event(workflow_id: str):
    """Return a minimal valid EventRecord."""
    from shared.models.event import (
        EventRecord,
        EventSource,
        EventTopic,
        EventType,
    )

    return EventRecord(
        event_type=EventType.WORKFLOW_CREATED,
        topic=EventTopic.WORKFLOW_EVENTS,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        payload={"workflow_id": workflow_id, "name": "test-workflow", "trigger": "manual"},
    )


# ---------------------------------------------------------------------------
# Temporary workspace fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Return a temporary directory for workspace tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Mock Kafka producer fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_producer():
    """Return a MagicMock that mimics FlowOSProducer."""
    producer = MagicMock()
    producer.produce = MagicMock()
    producer.produce_sync = MagicMock()
    producer.produce_batch = MagicMock()
    producer.flush = MagicMock()
    producer.close = MagicMock()
    return producer


# ---------------------------------------------------------------------------
# Simple YAML DSL fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_workflow_yaml() -> str:
    return """
name: simple-test-workflow
version: "1.0.0"
description: "A simple test workflow"

inputs:
  feature_branch: "main"

outputs:
  result: null

steps:
  - id: plan
    name: "Plan Step"
    agent_type: ai
    timeout_secs: 300
    retry_count: 1
    tags:
      - planning

  - id: implement
    name: "Implement Step"
    agent_type: human
    depends_on:
      - plan
    timeout_secs: 3600

tags:
  - test
"""


@pytest.fixture
def feature_delivery_yaml_path() -> Path:
    """Return path to the example feature delivery pipeline YAML."""
    here = Path(__file__).parent.parent
    return here / "examples" / "feature_delivery_pipeline.yaml"
