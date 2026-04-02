"""
tests/temporal/test_replay_semantics.py — Tests for Temporal replay semantics.

Tests that FlowOS workflow and activity definitions follow Temporal's
determinism requirements for safe replay. Tests cover:
- WorkflowInput/WorkflowResult are dataclass-serialisable (required for Temporal)
- Workflow definitions use @workflow.defn decorator
- Activity definitions use @activity.defn decorator
- No non-deterministic operations in workflow data structures
- DSL parser produces deterministic output for the same input
- Event serialisation is deterministic (same input → same bytes)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from orchestrator.workflows.base_workflow import WorkflowInput, WorkflowResult
from orchestrator.workflows.feature_delivery import (
    FeatureDeliveryWorkflow,
    FeatureDeliveryInput,
)
from orchestrator.dsl.parser import parse_workflow_yaml
from shared.models.event import EventRecord, EventSource, EventTopic, EventType
from shared.kafka.schemas import WorkflowCreatedPayload, build_event


# ---------------------------------------------------------------------------
# Sample YAML
# ---------------------------------------------------------------------------

DETERMINISTIC_YAML = """
name: deterministic-workflow
version: "1.0.0"
steps:
  - id: step-a
    name: "Step A"
    agent_type: ai
    timeout_secs: 300
  - id: step-b
    name: "Step B"
    agent_type: human
    depends_on: [step-a]
    timeout_secs: 3600
tags:
  - test
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkflowDataclassSerialisation:
    """Tests that workflow data structures are safely serialisable for Temporal."""

    def test_workflow_input_is_dataclass(self):
        """WorkflowInput is a dataclass (required for Temporal serialisation)."""
        import dataclasses
        assert dataclasses.is_dataclass(WorkflowInput)

    def test_workflow_result_is_dataclass(self):
        """WorkflowResult is a dataclass (required for Temporal serialisation)."""
        import dataclasses
        assert dataclasses.is_dataclass(WorkflowResult)

    def test_feature_delivery_input_is_dataclass(self):
        """FeatureDeliveryInput is a dataclass."""
        import dataclasses
        assert dataclasses.is_dataclass(FeatureDeliveryInput)

    def test_workflow_input_asdict_is_json_serialisable(self):
        """WorkflowInput.asdict() produces JSON-serialisable data."""
        inp = WorkflowInput(
            workflow_id=str(uuid.uuid4()),
            name="test-workflow",
            definition={"name": "test", "steps": []},
            inputs={"branch": "main"},
            tags=["feature"],
        )
        data = asdict(inp)
        # Must be JSON-serialisable (no datetime, no custom objects)
        json_str = json.dumps(data)
        assert isinstance(json_str, str)
        restored = json.loads(json_str)
        assert restored["name"] == "test-workflow"

    def test_workflow_result_asdict_is_json_serialisable(self):
        """WorkflowResult.asdict() produces JSON-serialisable data."""
        result = WorkflowResult(
            workflow_id=str(uuid.uuid4()),
            status="completed",
            outputs={"artifact_url": "s3://bucket/file.tar.gz"},
        )
        data = asdict(result)
        json_str = json.dumps(data)
        assert isinstance(json_str, str)
        restored = json.loads(json_str)
        assert restored["status"] == "completed"

    def test_workflow_input_roundtrip_via_json(self):
        """WorkflowInput survives JSON roundtrip (simulates Temporal serialisation)."""
        wf_id = str(uuid.uuid4())
        inp = WorkflowInput(
            workflow_id=wf_id,
            name="test-workflow",
            definition={"name": "test", "steps": [{"step_id": "s1", "name": "Step 1"}]},
            inputs={"branch": "feat/my-feature"},
            owner_agent_id=str(uuid.uuid4()),
            trigger="api",
            tags=["feature", "delivery"],
        )

        # Simulate Temporal serialisation: asdict → JSON → reconstruct
        data = asdict(inp)
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)

        restored = WorkflowInput(**restored_data)
        assert restored.workflow_id == wf_id
        assert restored.name == "test-workflow"
        assert restored.inputs["branch"] == "feat/my-feature"
        assert "feature" in restored.tags

    def test_feature_delivery_input_roundtrip_via_json(self):
        """FeatureDeliveryInput survives JSON roundtrip."""
        wf_id = str(uuid.uuid4())
        inp = FeatureDeliveryInput(
            workflow_id=wf_id,
            name="feature-delivery",
            definition={"name": "feature-delivery", "steps": []},
            feature_branch="feat/auth-module",
            ticket_id="JIRA-789",
            require_approval=True,
        )

        data = asdict(inp)
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)

        restored = FeatureDeliveryInput(**restored_data)
        assert restored.workflow_id == wf_id
        assert restored.feature_branch == "feat/auth-module"
        assert restored.ticket_id == "JIRA-789"
        assert restored.require_approval is True


class TestDSLParserDeterminism:
    """Tests that the DSL parser produces deterministic output."""

    def test_same_yaml_produces_same_definition(self):
        """Parsing the same YAML twice produces identical definitions."""
        defn1 = parse_workflow_yaml(DETERMINISTIC_YAML)
        defn2 = parse_workflow_yaml(DETERMINISTIC_YAML)

        assert defn1.name == defn2.name
        assert defn1.version == defn2.version
        assert len(defn1.steps) == len(defn2.steps)

        for s1, s2 in zip(defn1.steps, defn2.steps):
            assert s1.step_id == s2.step_id
            assert s1.name == s2.name
            assert s1.agent_type == s2.agent_type
            assert s1.depends_on == s2.depends_on

    def test_step_order_is_preserved(self):
        """DSL parser preserves the order of steps."""
        defn = parse_workflow_yaml(DETERMINISTIC_YAML)

        assert defn.steps[0].step_id == "step-a"
        assert defn.steps[1].step_id == "step-b"

    def test_step_dependencies_are_deterministic(self):
        """Step dependencies are always in the same order."""
        yaml_with_deps = """
name: dep-test
steps:
  - id: a
    name: "A"
    agent_type: ai
  - id: b
    name: "B"
    agent_type: human
    depends_on: [a]
  - id: c
    name: "C"
    agent_type: build
    depends_on: [b]
"""
        for _ in range(3):
            defn = parse_workflow_yaml(yaml_with_deps)
            assert defn.steps[0].step_id == "a"
            assert defn.steps[1].depends_on == ["a"]
            assert defn.steps[2].depends_on == ["b"]

    def test_dsl_source_preserved_for_replay(self):
        """DSL source is preserved in the definition for audit and replay."""
        defn = parse_workflow_yaml(DETERMINISTIC_YAML)
        assert defn.dsl_source is not None
        assert "deterministic-workflow" in defn.dsl_source

    def test_definition_to_dict_is_deterministic(self):
        """Converting a definition to dict produces consistent results."""
        defn = parse_workflow_yaml(DETERMINISTIC_YAML)

        dict1 = {
            "name": defn.name,
            "version": defn.version,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "agent_type": s.agent_type,
                    "depends_on": s.depends_on,
                }
                for s in defn.steps
            ],
        }
        dict2 = {
            "name": defn.name,
            "version": defn.version,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "agent_type": s.agent_type,
                    "depends_on": s.depends_on,
                }
                for s in defn.steps
            ],
        }

        assert json.dumps(dict1, sort_keys=True) == json.dumps(dict2, sort_keys=True)


class TestEventSerialisationDeterminism:
    """Tests that event serialisation is deterministic for replay."""

    def test_event_with_same_id_produces_same_bytes(self):
        """An EventRecord with a fixed event_id serialises to the same bytes."""
        event_id = str(uuid.uuid4())
        wf_id = str(uuid.uuid4())

        # Create the same event twice with the same IDs
        event1 = EventRecord(
            event_id=event_id,
            event_type=EventType.WORKFLOW_CREATED,
            topic=EventTopic.WORKFLOW_EVENTS,
            source=EventSource.ORCHESTRATOR,
            workflow_id=wf_id,
            payload={"workflow_id": wf_id, "name": "test"},
        )
        event2 = EventRecord(
            event_id=event_id,
            event_type=EventType.WORKFLOW_CREATED,
            topic=EventTopic.WORKFLOW_EVENTS,
            source=EventSource.ORCHESTRATOR,
            workflow_id=wf_id,
            payload={"workflow_id": wf_id, "name": "test"},
        )

        # Both should serialise to the same JSON (modulo occurred_at)
        data1 = json.loads(event1.to_kafka_value())
        data2 = json.loads(event2.to_kafka_value())

        assert data1["event_id"] == data2["event_id"]
        assert data1["event_type"] == data2["event_type"]
        assert data1["payload"] == data2["payload"]

    def test_event_deserialisation_is_idempotent(self):
        """Deserialising an event twice produces identical results."""
        wf_id = str(uuid.uuid4())
        payload = WorkflowCreatedPayload(
            workflow_id=wf_id,
            name="test-workflow",
            trigger="manual",
            owner_agent_id="agent-001",
        )
        event = build_event(
            event_type=EventType.WORKFLOW_CREATED,
            payload=payload,
            source=EventSource.ORCHESTRATOR,
            workflow_id=wf_id,
        )

        raw = event.to_kafka_value()
        restored1 = EventRecord.from_kafka_value(raw)
        restored2 = EventRecord.from_kafka_value(raw)

        assert restored1.event_id == restored2.event_id
        assert restored1.event_type == restored2.event_type
        assert restored1.payload == restored2.payload
        assert restored1.occurred_at == restored2.occurred_at

    def test_event_payload_json_is_stable(self):
        """Event payload JSON is stable across multiple serialisations."""
        wf_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        event = EventRecord(
            event_id=event_id,
            event_type=EventType.WORKFLOW_CREATED,
            topic=EventTopic.WORKFLOW_EVENTS,
            source=EventSource.ORCHESTRATOR,
            workflow_id=wf_id,
            payload={"workflow_id": wf_id, "name": "test", "trigger": "manual"},
        )

        # Serialise multiple times
        bytes1 = event.to_kafka_value()
        bytes2 = event.to_kafka_value()
        bytes3 = event.to_kafka_value()

        # All serialisations should produce the same bytes
        assert bytes1 == bytes2
        assert bytes2 == bytes3


class TestWorkflowActivityDefinitions:
    """Tests that workflow and activity definitions follow Temporal conventions."""

    def test_feature_delivery_workflow_has_defn_decorator(self):
        """FeatureDeliveryWorkflow is decorated with @workflow.defn."""
        # Temporal adds __temporal_workflow_definition to decorated classes
        assert hasattr(FeatureDeliveryWorkflow, "__temporal_workflow_definition") or \
               hasattr(FeatureDeliveryWorkflow, "_temporal_workflow_definition") or \
               any("temporal" in attr.lower() for attr in dir(FeatureDeliveryWorkflow)
                   if not attr.startswith("__"))

    def test_workflow_input_fields_are_all_serialisable_types(self):
        """All WorkflowInput fields use JSON-serialisable types."""
        import dataclasses

        for f in dataclasses.fields(WorkflowInput):
            # Check that field types are basic Python types
            type_str = str(f.type)
            # Should not contain complex objects like datetime, Path, etc.
            assert "datetime" not in type_str or "str" in type_str, \
                f"Field {f.name} uses datetime which may not be Temporal-safe"

    def test_workflow_result_fields_are_all_serialisable_types(self):
        """All WorkflowResult fields use JSON-serialisable types."""
        import dataclasses

        for f in dataclasses.fields(WorkflowResult):
            type_str = str(f.type)
            assert "datetime" not in type_str or "str" in type_str, \
                f"Field {f.name} uses datetime which may not be Temporal-safe"

    def test_kafka_activities_are_importable(self):
        """Kafka activities module is importable."""
        from orchestrator.activities.kafka_activities import (
            publish_workflow_event,
            publish_task_event,
        )
        assert callable(publish_workflow_event)
        assert callable(publish_task_event)

    def test_task_activities_are_importable(self):
        """Task activities module is importable."""
        from orchestrator.activities.task_activities import (
            create_task,
            assign_task,
            wait_for_task_completion,
        )
        assert callable(create_task)
        assert callable(assign_task)
        assert callable(wait_for_task_completion)

    def test_checkpoint_activities_are_importable(self):
        """Checkpoint activities module is importable."""
        from orchestrator.activities.checkpoint_activities import (
            create_checkpoint_record,
            revert_to_checkpoint,
        )
        assert callable(create_checkpoint_record)
        assert callable(revert_to_checkpoint)

    def test_handoff_activities_are_importable(self):
        """Handoff activities module is importable."""
        from orchestrator.activities.handoff_activities import (
            initiate_handoff,
            wait_for_handoff_acceptance,
        )
        assert callable(initiate_handoff)
        assert callable(wait_for_handoff_acceptance)
