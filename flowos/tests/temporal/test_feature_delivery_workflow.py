"""
tests/temporal/test_feature_delivery_workflow.py — Tests for FeatureDeliveryWorkflow.

Tests the FeatureDeliveryWorkflow Temporal workflow definition, input/output
data structures, and workflow configuration without requiring a live Temporal
server.

Tests cover:
- WorkflowInput and WorkflowResult data structures
- FeatureDeliveryInput fields and defaults
- Workflow class registration and decorators
- Workflow signal and query handlers
- DSL-to-workflow input conversion
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

from orchestrator.workflows.base_workflow import WorkflowInput, WorkflowResult
from orchestrator.workflows.feature_delivery import (
    FeatureDeliveryWorkflow,
    FeatureDeliveryInput,
)
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

steps:
  - id: plan
    name: "Plan Feature"
    agent_type: ai
    timeout_secs: 300
    retry_count: 2

  - id: implement
    name: "Implement Feature"
    agent_type: human
    depends_on: [plan]
    timeout_secs: 86400

  - id: build
    name: "Build and Test"
    agent_type: build
    depends_on: [implement]
    timeout_secs: 1800
    retry_count: 3

  - id: review
    name: "Code Review"
    agent_type: human
    depends_on: [build]
    timeout_secs: 86400

tags:
  - feature
  - delivery
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkflowInput:
    """Tests for WorkflowInput data structure."""

    def test_create_workflow_input(self):
        """WorkflowInput can be created with required fields."""
        wf_id = str(uuid.uuid4())
        inp = WorkflowInput(
            workflow_id=wf_id,
            name="feature-delivery",
            definition={"name": "feature-delivery", "steps": []},
        )

        assert inp.workflow_id == wf_id
        assert inp.name == "feature-delivery"
        assert inp.trigger == "manual"

    def test_workflow_input_defaults(self):
        """WorkflowInput has sensible defaults."""
        inp = WorkflowInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={},
        )

        assert inp.inputs == {}
        assert inp.owner_agent_id is None
        assert inp.project is None
        assert inp.trigger == "manual"
        assert inp.tags == []
        assert inp.correlation_id is None

    def test_workflow_input_serialisable(self):
        """WorkflowInput can be serialised to a dict."""
        inp = WorkflowInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={"name": "test"},
            inputs={"branch": "main"},
            tags=["feature"],
        )
        data = asdict(inp)

        assert isinstance(data, dict)
        assert data["name"] == "test"
        assert data["inputs"]["branch"] == "main"
        assert "feature" in data["tags"]

    def test_workflow_input_with_all_fields(self):
        """WorkflowInput accepts all optional fields."""
        wf_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        corr_id = str(uuid.uuid4())

        inp = WorkflowInput(
            workflow_id=wf_id,
            name="test",
            definition={},
            inputs={"branch": "main"},
            owner_agent_id=agent_id,
            project="my-project",
            trigger="api",
            tags=["feature", "delivery"],
            correlation_id=corr_id,
        )

        assert inp.owner_agent_id == agent_id
        assert inp.project == "my-project"
        assert inp.trigger == "api"
        assert inp.correlation_id == corr_id


class TestWorkflowResult:
    """Tests for WorkflowResult data structure."""

    def test_create_workflow_result(self):
        """WorkflowResult can be created with required fields."""
        wf_id = str(uuid.uuid4())
        result = WorkflowResult(
            workflow_id=wf_id,
            status="completed",
        )

        assert result.workflow_id == wf_id
        assert result.status == "completed"

    def test_workflow_result_defaults(self):
        """WorkflowResult has sensible defaults."""
        result = WorkflowResult(
            workflow_id=str(uuid.uuid4()),
            status="completed",
        )

        assert result.outputs == {}
        assert result.error_message is None
        assert result.error_details == {}

    def test_workflow_result_with_error(self):
        """WorkflowResult can represent a failed workflow."""
        result = WorkflowResult(
            workflow_id=str(uuid.uuid4()),
            status="failed",
            error_message="Build step failed: compilation error",
            error_details={"step": "build", "exit_code": 1},
        )

        assert result.status == "failed"
        assert "compilation error" in result.error_message
        assert result.error_details["exit_code"] == 1

    def test_workflow_result_with_outputs(self):
        """WorkflowResult can carry output parameters."""
        result = WorkflowResult(
            workflow_id=str(uuid.uuid4()),
            status="completed",
            outputs={
                "artifact_url": "s3://bucket/artifact.tar.gz",
                "review_status": "approved",
            },
        )

        assert result.outputs["artifact_url"] == "s3://bucket/artifact.tar.gz"
        assert result.outputs["review_status"] == "approved"


class TestFeatureDeliveryInput:
    """Tests for FeatureDeliveryInput data structure."""

    def test_create_feature_delivery_input(self):
        """FeatureDeliveryInput can be created with required fields."""
        wf_id = str(uuid.uuid4())
        inp = FeatureDeliveryInput(
            workflow_id=wf_id,
            name="feature-delivery",
            definition={"name": "feature-delivery", "steps": []},
        )

        assert inp.workflow_id == wf_id
        assert inp.feature_branch == "main"
        assert inp.require_approval is False
        assert inp.auto_assign is True

    def test_feature_delivery_input_defaults(self):
        """FeatureDeliveryInput has sensible defaults."""
        inp = FeatureDeliveryInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={},
        )

        assert inp.feature_branch == "main"
        assert inp.ticket_id is None
        assert inp.ai_agent_id is None
        assert inp.human_agent_id is None
        assert inp.build_agent_id is None
        assert inp.require_approval is False
        assert inp.auto_assign is True

    def test_feature_delivery_input_with_agents(self):
        """FeatureDeliveryInput accepts agent IDs."""
        ai_id = str(uuid.uuid4())
        human_id = str(uuid.uuid4())
        build_id = str(uuid.uuid4())

        inp = FeatureDeliveryInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={},
            ai_agent_id=ai_id,
            human_agent_id=human_id,
            build_agent_id=build_id,
        )

        assert inp.ai_agent_id == ai_id
        assert inp.human_agent_id == human_id
        assert inp.build_agent_id == build_id

    def test_feature_delivery_input_with_ticket(self):
        """FeatureDeliveryInput accepts a ticket ID."""
        inp = FeatureDeliveryInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={},
            ticket_id="JIRA-456",
            feature_branch="feat/my-feature",
        )

        assert inp.ticket_id == "JIRA-456"
        assert inp.feature_branch == "feat/my-feature"

    def test_feature_delivery_input_is_workflow_input(self):
        """FeatureDeliveryInput is a subclass of WorkflowInput."""
        inp = FeatureDeliveryInput(
            workflow_id=str(uuid.uuid4()),
            name="test",
            definition={},
        )

        assert isinstance(inp, WorkflowInput)


class TestFeatureDeliveryWorkflowClass:
    """Tests for FeatureDeliveryWorkflow class structure."""

    def test_workflow_class_is_importable(self):
        """FeatureDeliveryWorkflow can be imported."""
        assert FeatureDeliveryWorkflow is not None

    def test_workflow_has_run_method(self):
        """FeatureDeliveryWorkflow has a run() method."""
        assert hasattr(FeatureDeliveryWorkflow, "run")
        assert callable(FeatureDeliveryWorkflow.run)

    def test_workflow_has_signal_handlers(self):
        """FeatureDeliveryWorkflow has signal handlers for pause/resume/cancel."""
        # Check for signal handler methods
        assert hasattr(FeatureDeliveryWorkflow, "pause") or \
               hasattr(FeatureDeliveryWorkflow, "signal_pause") or \
               any("pause" in name.lower() for name in dir(FeatureDeliveryWorkflow))

    def test_workflow_class_name(self):
        """FeatureDeliveryWorkflow has the correct class name."""
        assert FeatureDeliveryWorkflow.__name__ == "FeatureDeliveryWorkflow"


class TestDSLToWorkflowInput:
    """Tests for converting DSL definitions to workflow inputs."""

    def test_dsl_to_workflow_input_conversion(self):
        """A DSL definition can be converted to a WorkflowInput."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)
        wf_id = str(uuid.uuid4())

        # Convert definition to dict for WorkflowInput
        definition_dict = {
            "name": definition.name,
            "version": definition.version,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "agent_type": s.agent_type,
                    "depends_on": s.depends_on,
                    "timeout_secs": s.timeout_secs,
                    "retry_count": s.retry_count,
                }
                for s in definition.steps
            ],
        }

        inp = FeatureDeliveryInput(
            workflow_id=wf_id,
            name=definition.name,
            definition=definition_dict,
            inputs=definition.inputs,
            tags=definition.tags,
            feature_branch=definition.inputs.get("feature_branch", "main"),
            ticket_id=definition.inputs.get("ticket_id"),
        )

        assert inp.workflow_id == wf_id
        assert inp.name == "feature-delivery"
        assert inp.feature_branch == "main"
        assert inp.ticket_id == "JIRA-123"
        assert "feature" in inp.tags

    def test_dsl_steps_preserved_in_definition_dict(self):
        """DSL steps are preserved when converting to WorkflowInput."""
        definition = parse_workflow_yaml(FEATURE_DELIVERY_YAML)

        definition_dict = {
            "name": definition.name,
            "steps": [
                {"step_id": s.step_id, "name": s.name, "agent_type": s.agent_type}
                for s in definition.steps
            ],
        }

        inp = WorkflowInput(
            workflow_id=str(uuid.uuid4()),
            name=definition.name,
            definition=definition_dict,
        )

        assert len(inp.definition["steps"]) == 4
        step_names = [s["name"] for s in inp.definition["steps"]]
        assert "Plan Feature" in step_names
        assert "Build and Test" in step_names

    def test_example_pipeline_to_workflow_input(self, feature_delivery_yaml_path: Path):
        """The example pipeline file can be converted to a WorkflowInput."""
        definition = parse_workflow_file(str(feature_delivery_yaml_path))

        inp = FeatureDeliveryInput(
            workflow_id=str(uuid.uuid4()),
            name=definition.name,
            definition={"name": definition.name, "steps": []},
            inputs=definition.inputs,
            feature_branch=definition.inputs.get("feature_branch", "main"),
        )

        assert inp.name == "feature-delivery-pipeline"
        assert inp.feature_branch == "main"
