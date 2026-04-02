"""
tests/integration/test_workflow_lifecycle.py — Integration tests for workflow lifecycle.

Tests the complete workflow lifecycle from creation through completion,
using domain models and DSL parser without requiring live infrastructure.

Tests cover:
- Workflow creation from DSL
- Status transitions: PENDING → RUNNING → COMPLETED
- Task creation from workflow steps
- Workflow with multiple steps and dependencies
- Error states: FAILED, CANCELLED
- Workflow properties: is_terminal, is_active, duration_seconds
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from shared.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStep,
    WorkflowTrigger,
)
from shared.models.task import Task, TaskStatus, TaskType
from orchestrator.dsl.parser import parse_workflow_yaml, parse_workflow_file


# ---------------------------------------------------------------------------
# Sample workflow YAML
# ---------------------------------------------------------------------------

FEATURE_DELIVERY_YAML = """
name: feature-delivery
version: "1.0.0"
description: "End-to-end feature delivery workflow"

inputs:
  feature_branch: "main"
  ticket_id: "JIRA-123"

outputs:
  artifact_url: null
  review_status: null

steps:
  - id: plan
    name: "Plan Feature"
    agent_type: ai
    timeout_secs: 300
    retry_count: 2
    tags:
      - planning

  - id: implement
    name: "Implement Feature"
    agent_type: human
    depends_on:
      - plan
    timeout_secs: 86400

  - id: build
    name: "Build and Test"
    agent_type: build
    depends_on:
      - implement
    timeout_secs: 1800
    retry_count: 3

  - id: review
    name: "Code Review"
    agent_type: human
    depends_on:
      - build
    timeout_secs: 86400

tags:
  - feature
  - delivery
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkflowCreation:
    """Tests for workflow creation from DSL."""

    def test_create_workflow_from_dsl(self):
        """A workflow can be created from a DSL definition."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)
        workflow = Workflow(
            name=definition.name,
            definition=definition,
            trigger=WorkflowTrigger.MANUAL,
        )

        assert workflow.name == "feature-delivery"
        assert workflow.status == WorkflowStatus.PENDING
        assert workflow.definition is not None
        assert len(workflow.definition.steps) == 4

    def test_workflow_has_valid_id(self):
        """Created workflow has a valid UUID as workflow_id."""
        workflow = Workflow(name="test-workflow")
        uuid.UUID(workflow.workflow_id)

    def test_workflow_created_at_is_utc(self):
        """Workflow created_at is timezone-aware (UTC)."""
        workflow = Workflow(name="test-workflow")
        assert workflow.created_at.tzinfo is not None

    def test_workflow_with_temporal_ids(self):
        """Workflow stores Temporal run and workflow IDs."""
        workflow = Workflow(
            name="test-workflow",
            temporal_run_id="run-abc-123",
            temporal_workflow_id="wf-abc-123",
        )
        assert workflow.temporal_run_id == "run-abc-123"
        assert workflow.temporal_workflow_id == "wf-abc-123"

    def test_workflow_with_owner_agent(self):
        """Workflow stores the owner agent ID."""
        agent_id = str(uuid.uuid4())
        workflow = Workflow(name="test-workflow", owner_agent_id=agent_id)
        assert workflow.owner_agent_id == agent_id


class TestWorkflowStatusTransitions:
    """Tests for workflow status transitions."""

    def test_pending_to_running(self):
        """Workflow transitions from PENDING to RUNNING."""
        workflow = Workflow(name="test-workflow", status=WorkflowStatus.PENDING)
        assert workflow.status == WorkflowStatus.PENDING
        assert workflow.is_active is False

        # Simulate transition to RUNNING
        workflow = workflow.model_copy(
            update={
                "status": WorkflowStatus.RUNNING,
                "started_at": datetime.now(timezone.utc),
            }
        )
        assert workflow.status == WorkflowStatus.RUNNING
        assert workflow.is_active is True

    def test_running_to_completed(self):
        """Workflow transitions from RUNNING to COMPLETED."""
        start = datetime.now(timezone.utc)
        workflow = Workflow(
            name="test-workflow",
            status=WorkflowStatus.RUNNING,
            started_at=start,
        )

        # Simulate completion
        end = start + timedelta(seconds=120)
        workflow = workflow.model_copy(
            update={
                "status": WorkflowStatus.COMPLETED,
                "completed_at": end,
            }
        )

        assert workflow.status == WorkflowStatus.COMPLETED
        assert workflow.is_terminal is True
        assert workflow.duration_seconds == pytest.approx(120.0)

    def test_running_to_failed(self):
        """Workflow transitions from RUNNING to FAILED with error message."""
        workflow = Workflow(
            name="test-workflow",
            status=WorkflowStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )

        workflow = workflow.model_copy(
            update={
                "status": WorkflowStatus.FAILED,
                "error_message": "Build step failed: compilation error",
                "completed_at": datetime.now(timezone.utc),
            }
        )

        assert workflow.status == WorkflowStatus.FAILED
        assert workflow.is_terminal is True
        assert "compilation error" in workflow.error_message

    def test_running_to_paused_and_back(self):
        """Workflow can be paused and resumed."""
        workflow = Workflow(
            name="test-workflow",
            status=WorkflowStatus.RUNNING,
        )

        # Pause
        workflow = workflow.model_copy(update={"status": WorkflowStatus.PAUSED})
        assert workflow.status == WorkflowStatus.PAUSED
        assert workflow.is_active is True  # PAUSED is still active

        # Resume
        workflow = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
        assert workflow.status == WorkflowStatus.RUNNING

    def test_cancelled_is_terminal(self):
        """CANCELLED workflow is in a terminal state."""
        workflow = Workflow(name="test-workflow", status=WorkflowStatus.CANCELLED)
        assert workflow.is_terminal is True
        assert workflow.is_active is False


class TestWorkflowDefinitionParsing:
    """Tests for parsing workflow definitions from DSL."""

    def test_parse_feature_delivery_workflow(self):
        """Feature delivery workflow DSL parses correctly."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        assert definition.name == "feature-delivery"
        assert definition.version == "1.0.0"
        assert len(definition.steps) == 4

    def test_workflow_steps_have_correct_agent_types(self):
        """Each step has the correct agent type."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        step_by_id = {s.step_id: s for s in definition.steps}
        assert step_by_id["plan"].agent_type == "ai"
        assert step_by_id["implement"].agent_type == "human"
        assert step_by_id["build"].agent_type == "build"
        assert step_by_id["review"].agent_type == "human"

    def test_workflow_dependency_chain(self):
        """Steps have the correct dependency chain."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        step_by_id = {s.step_id: s for s in definition.steps}
        assert step_by_id["plan"].depends_on == []
        assert "plan" in step_by_id["implement"].depends_on
        assert "implement" in step_by_id["build"].depends_on
        assert "build" in step_by_id["review"].depends_on

    def test_workflow_inputs_parsed(self):
        """Workflow inputs are parsed correctly."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        assert "feature_branch" in definition.inputs
        assert definition.inputs["feature_branch"] == "main"
        assert "ticket_id" in definition.inputs

    def test_workflow_tags_parsed(self):
        """Workflow tags are parsed correctly."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        assert "feature" in definition.tags
        assert "delivery" in definition.tags

    def test_step_retry_counts(self):
        """Steps have the correct retry counts."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        step_by_id = {s.step_id: s for s in definition.steps}
        assert step_by_id["plan"].retry_count == 2
        assert step_by_id["build"].retry_count == 3
        assert step_by_id["implement"].retry_count == 0

    def test_step_timeouts(self):
        """Steps have the correct timeout values."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        step_by_id = {s.step_id: s for s in definition.steps}
        assert step_by_id["plan"].timeout_secs == 300
        assert step_by_id["build"].timeout_secs == 1800
        assert step_by_id["implement"].timeout_secs == 86400


class TestTaskCreationFromWorkflow:
    """Tests for creating tasks from workflow steps."""

    def test_create_task_from_workflow_step(self):
        """A Task can be created from a WorkflowStep."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)
        workflow_id = str(uuid.uuid4())

        plan_step = definition.steps[0]
        task = Task(
            workflow_id=workflow_id,
            step_id=plan_step.step_id,
            name=plan_step.name,
            task_type=TaskType.AI,
            timeout_secs=plan_step.timeout_secs,
            retry_count=plan_step.retry_count,
        )

        assert task.workflow_id == workflow_id
        assert task.step_id == plan_step.step_id
        assert task.name == "Plan Feature"
        assert task.task_type == TaskType.AI
        assert task.timeout_secs == 300
        assert task.retry_count == 2

    def test_tasks_created_for_all_steps(self):
        """A task can be created for each step in the workflow."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)
        workflow_id = str(uuid.uuid4())

        tasks = []
        for step in definition.steps:
            task_type_map = {
                "ai": TaskType.AI,
                "human": TaskType.HUMAN,
                "build": TaskType.BUILD,
            }
            task = Task(
                workflow_id=workflow_id,
                step_id=step.step_id,
                name=step.name,
                task_type=task_type_map.get(step.agent_type, TaskType.HUMAN),
            )
            tasks.append(task)

        assert len(tasks) == 4
        task_types = {t.task_type for t in tasks}
        assert TaskType.AI in task_types
        assert TaskType.HUMAN in task_types
        assert TaskType.BUILD in task_types


class TestWorkflowFromExampleFile:
    """Tests using the actual example pipeline YAML file."""

    def test_parse_example_pipeline_file(self, feature_delivery_yaml_path: Path):
        """The example pipeline file parses successfully."""
        definition = parse_workflow_file(str(feature_delivery_yaml_path))

        assert definition.name == "feature-delivery-pipeline"
        assert len(definition.steps) == 6

    def test_example_pipeline_creates_valid_workflow(self, feature_delivery_yaml_path: Path):
        """A valid Workflow can be created from the example pipeline."""
        definition = parse_workflow_file(str(feature_delivery_yaml_path))
        workflow = Workflow(
            name=definition.name,
            definition=definition,
            trigger=WorkflowTrigger.MANUAL,
            inputs={"feature_branch": "feat/my-feature", "ticket_id": "JIRA-456"},
        )

        assert workflow.name == "feature-delivery-pipeline"
        assert workflow.inputs["feature_branch"] == "feat/my-feature"
        assert workflow.definition is not None
        assert len(workflow.definition.steps) == 6
