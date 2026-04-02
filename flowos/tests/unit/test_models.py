"""
tests/unit/test_models.py — Unit tests for FlowOS domain models.

Tests cover:
- Workflow model: creation, validation, lifecycle properties
- Task model: creation, validation, lifecycle properties, retry bounds
- Agent model: creation, capabilities
- Checkpoint model: creation, status transitions
- Handoff model: creation, validation
- EventRecord model: creation, serialisation, deserialisation
- WorkflowDefinition: step dependency validation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStep,
    WorkflowTrigger,
)
from shared.models.task import (
    Task,
    TaskAttempt,
    TaskInput,
    TaskOutput,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from shared.models.agent import (
    Agent,
    AgentCapability,
    AgentStatus,
    AgentType,
)
from shared.models.checkpoint import (
    Checkpoint,
    CheckpointStatus,
    CheckpointType,
)
from shared.models.handoff import (
    Handoff,
    HandoffStatus,
    HandoffType,
)
from shared.models.event import (
    EventRecord,
    EventSeverity,
    EventSource,
    EventTopic,
    EventType,
)


# ===========================================================================
# Workflow model tests
# ===========================================================================


class TestWorkflowModel:
    """Tests for the Workflow domain model."""

    def test_create_minimal_workflow(self):
        """A workflow can be created with only required fields."""
        wf = Workflow(name="my-workflow")
        assert wf.name == "my-workflow"
        assert wf.status == WorkflowStatus.PENDING
        assert wf.trigger == WorkflowTrigger.MANUAL
        assert isinstance(wf.workflow_id, str)
        uuid.UUID(wf.workflow_id)  # must be valid UUID

    def test_workflow_id_is_valid_uuid(self):
        """workflow_id is auto-generated as a valid UUID."""
        wf = Workflow(name="test")
        assert uuid.UUID(wf.workflow_id)

    def test_workflow_id_validation_rejects_non_uuid(self):
        """workflow_id must be a valid UUID string."""
        with pytest.raises(ValidationError, match="workflow_id"):
            Workflow(workflow_id="not-a-uuid", name="test")

    def test_workflow_name_cannot_be_empty(self):
        """Workflow name must have at least 1 character."""
        with pytest.raises(ValidationError):
            Workflow(name="")

    def test_workflow_is_terminal_states(self):
        """is_terminal returns True for COMPLETED, FAILED, CANCELLED."""
        for status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
            wf = Workflow(name="test", status=status)
            assert wf.is_terminal is True

    def test_workflow_is_not_terminal_for_active_states(self):
        """is_terminal returns False for PENDING, RUNNING, PAUSED."""
        for status in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
            wf = Workflow(name="test", status=status)
            assert wf.is_terminal is False

    def test_workflow_is_active(self):
        """is_active returns True for RUNNING and PAUSED."""
        for status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
            wf = Workflow(name="test", status=status)
            assert wf.is_active is True

    def test_workflow_is_not_active_for_terminal_states(self):
        """is_active returns False for terminal states."""
        for status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
            wf = Workflow(name="test", status=status)
            assert wf.is_active is False

    def test_workflow_duration_seconds_none_when_not_started(self):
        """duration_seconds is None when started_at is not set."""
        wf = Workflow(name="test")
        assert wf.duration_seconds is None

    def test_workflow_duration_seconds_calculated(self):
        """duration_seconds is calculated from started_at to completed_at."""
        from datetime import timedelta

        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
        wf = Workflow(name="test", started_at=start, completed_at=end)
        assert wf.duration_seconds == 30.0

    def test_workflow_all_triggers(self):
        """All WorkflowTrigger values are valid."""
        for trigger in WorkflowTrigger:
            wf = Workflow(name="test", trigger=trigger)
            assert wf.trigger == trigger

    def test_workflow_with_inputs_and_outputs(self):
        """Workflow accepts arbitrary inputs and outputs dicts."""
        wf = Workflow(
            name="test",
            inputs={"branch": "main", "ticket": "JIRA-123"},
            outputs={"artifact_url": "s3://bucket/artifact.tar.gz"},
        )
        assert wf.inputs["branch"] == "main"
        assert wf.outputs["artifact_url"] == "s3://bucket/artifact.tar.gz"


# ===========================================================================
# WorkflowDefinition / WorkflowStep tests
# ===========================================================================


class TestWorkflowDefinition:
    """Tests for WorkflowDefinition and WorkflowStep models."""

    def test_create_workflow_definition_with_steps(self):
        """WorkflowDefinition can be created with a list of steps."""
        step = WorkflowStep(name="Plan", agent_type="ai")
        defn = WorkflowDefinition(name="my-workflow", steps=[step])
        assert defn.name == "my-workflow"
        assert len(defn.steps) == 1
        assert defn.steps[0].name == "Plan"

    def test_step_dependency_validation_rejects_unknown_dep(self):
        """Steps cannot depend on step_ids that don't exist."""
        step = WorkflowStep(
            step_id="step-a",
            name="Step A",
            agent_type="human",
            depends_on=["nonexistent-step-id"],
        )
        with pytest.raises(ValidationError, match="unknown step_id"):
            WorkflowDefinition(name="test", steps=[step])

    def test_step_dependency_validation_accepts_valid_dep(self):
        """Steps can depend on other steps in the same definition."""
        step_a = WorkflowStep(step_id="step-a", name="Step A", agent_type="ai")
        step_b = WorkflowStep(
            step_id="step-b",
            name="Step B",
            agent_type="human",
            depends_on=["step-a"],
        )
        defn = WorkflowDefinition(name="test", steps=[step_a, step_b])
        assert defn.steps[1].depends_on == ["step-a"]

    def test_workflow_step_defaults(self):
        """WorkflowStep has sensible defaults."""
        step = WorkflowStep(name="My Step", agent_type="build")
        assert step.timeout_secs == 0
        assert step.retry_count == 0
        assert step.depends_on == []
        assert step.tags == []
        assert step.inputs == {}

    def test_workflow_step_retry_count_bounded(self):
        """retry_count cannot exceed 10."""
        with pytest.raises(ValidationError):
            WorkflowStep(name="test", agent_type="ai", retry_count=11)

    def test_workflow_definition_empty_steps(self):
        """WorkflowDefinition can have zero steps."""
        defn = WorkflowDefinition(name="empty-workflow")
        assert defn.steps == []


# ===========================================================================
# Task model tests
# ===========================================================================


class TestTaskModel:
    """Tests for the Task domain model."""

    def _make_task(self, **kwargs) -> Task:
        defaults = dict(
            workflow_id=str(uuid.uuid4()),
            name="test-task",
        )
        defaults.update(kwargs)
        return Task(**defaults)

    def test_create_minimal_task(self):
        """Task can be created with only required fields."""
        wf_id = str(uuid.uuid4())
        task = Task(workflow_id=wf_id, name="my-task")
        assert task.name == "my-task"
        assert task.workflow_id == wf_id
        assert task.status == TaskStatus.PENDING
        assert task.task_type == TaskType.HUMAN
        assert task.priority == TaskPriority.NORMAL

    def test_task_id_is_valid_uuid(self):
        """task_id is auto-generated as a valid UUID."""
        task = self._make_task()
        uuid.UUID(task.task_id)

    def test_task_workflow_id_must_be_uuid(self):
        """workflow_id must be a valid UUID."""
        with pytest.raises(ValidationError):
            Task(workflow_id="not-a-uuid", name="test")

    def test_task_is_terminal_states(self):
        """is_terminal returns True for COMPLETED, FAILED, CANCELLED, SKIPPED, REVERTED."""
        terminal_statuses = [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
            TaskStatus.REVERTED,
        ]
        for status in terminal_statuses:
            task = self._make_task(status=status)
            assert task.is_terminal is True, f"Expected {status} to be terminal"

    def test_task_is_active_states(self):
        """is_active returns True for ACCEPTED, IN_PROGRESS, CHECKPOINTED."""
        active_statuses = [
            TaskStatus.ACCEPTED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.CHECKPOINTED,
        ]
        for status in active_statuses:
            task = self._make_task(status=status)
            assert task.is_active is True, f"Expected {status} to be active"

    def test_task_retry_bounds_validation(self):
        """current_retry cannot exceed retry_count."""
        with pytest.raises(ValidationError, match="current_retry"):
            Task(
                workflow_id=str(uuid.uuid4()),
                name="test",
                retry_count=2,
                current_retry=3,
            )

    def test_task_retry_bounds_valid(self):
        """current_retry equal to retry_count is valid."""
        task = Task(
            workflow_id=str(uuid.uuid4()),
            name="test",
            retry_count=3,
            current_retry=3,
        )
        assert task.current_retry == 3

    def test_task_duration_seconds_none_when_not_started(self):
        """duration_seconds is None when started_at is not set."""
        task = self._make_task()
        assert task.duration_seconds is None

    def test_task_with_inputs_and_outputs(self):
        """Task accepts TaskInput and TaskOutput lists."""
        inp = TaskInput(name="branch", value="main", required=True)
        out = TaskOutput(name="artifact_url", value="s3://bucket/file.tar.gz")
        task = self._make_task(inputs=[inp], outputs=[out])
        assert task.inputs[0].name == "branch"
        assert task.inputs[0].value == "main"
        assert task.outputs[0].name == "artifact_url"

    def test_task_attempt_duration(self):
        """TaskAttempt.duration_seconds is calculated correctly."""
        from datetime import timedelta

        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 10, 0, 45, tzinfo=timezone.utc)
        attempt = TaskAttempt(attempt_number=1, agent_id="agent-1", started_at=start, ended_at=end)
        assert attempt.duration_seconds == 45.0

    def test_task_all_types(self):
        """All TaskType values are valid."""
        for task_type in TaskType:
            task = self._make_task(task_type=task_type)
            assert task.task_type == task_type

    def test_task_all_priorities(self):
        """All TaskPriority values are valid."""
        for priority in TaskPriority:
            task = self._make_task(priority=priority)
            assert task.priority == priority


# ===========================================================================
# Agent model tests
# ===========================================================================


class TestAgentModel:
    """Tests for the Agent domain model."""

    def test_create_minimal_agent(self):
        """Agent can be created with required fields."""
        agent = Agent(name="my-agent", agent_type=AgentType.HUMAN)
        assert agent.name == "my-agent"
        assert agent.agent_type == AgentType.HUMAN
        assert agent.status == AgentStatus.OFFLINE

    def test_agent_id_is_valid_uuid(self):
        """agent_id is auto-generated as a valid UUID."""
        agent = Agent(name="test", agent_type=AgentType.AI)
        uuid.UUID(agent.agent_id)

    def test_agent_all_types(self):
        """All AgentType values are valid."""
        for agent_type in AgentType:
            agent = Agent(name="test", agent_type=agent_type)
            assert agent.agent_type == agent_type

    def test_agent_all_statuses(self):
        """All AgentStatus values are valid."""
        for status in AgentStatus:
            agent = Agent(name="test", agent_type=AgentType.HUMAN, status=status)
            assert agent.status == status

    def test_agent_capability(self):
        """AgentCapability can be created and attached to an agent."""
        cap = AgentCapability(name="code_review", version="1.0", enabled=True)
        agent = Agent(name="ai-agent", agent_type=AgentType.AI, capabilities=[cap])
        assert len(agent.capabilities) == 1
        assert agent.capabilities[0].name == "code_review"

    def test_agent_name_cannot_be_empty(self):
        """Agent name must not be empty."""
        with pytest.raises(ValidationError):
            Agent(name="", agent_type=AgentType.HUMAN)


# ===========================================================================
# Checkpoint model tests
# ===========================================================================


class TestCheckpointModel:
    """Tests for the Checkpoint domain model."""

    def _make_checkpoint(self, **kwargs) -> Checkpoint:
        defaults = dict(
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            agent_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()),
            git_sha="abc123def456" * 3,  # 36 chars
            message="checkpoint: 50% complete",
        )
        defaults.update(kwargs)
        return Checkpoint(**defaults)

    def test_create_minimal_checkpoint(self):
        """Checkpoint can be created with required fields."""
        ckpt = self._make_checkpoint()
        assert ckpt.status == CheckpointStatus.CREATING
        assert ckpt.checkpoint_type == CheckpointType.MANUAL

    def test_checkpoint_id_is_valid_uuid(self):
        """checkpoint_id is auto-generated as a valid UUID."""
        ckpt = self._make_checkpoint()
        uuid.UUID(ckpt.checkpoint_id)

    def test_checkpoint_all_statuses(self):
        """All CheckpointStatus values are valid."""
        for status in CheckpointStatus:
            ckpt = self._make_checkpoint(status=status)
            assert ckpt.status == status

    def test_checkpoint_all_types(self):
        """All CheckpointType values are valid."""
        for ckpt_type in CheckpointType:
            ckpt = self._make_checkpoint(checkpoint_type=ckpt_type)
            assert ckpt.checkpoint_type == ckpt_type


# ===========================================================================
# Handoff model tests
# ===========================================================================


class TestHandoffModel:
    """Tests for the Handoff domain model."""

    def _make_handoff(self, **kwargs) -> Handoff:
        defaults = dict(
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            source_agent_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            handoff_type=HandoffType.DELEGATION,
            reason="Delegating to AI for code review",
        )
        defaults.update(kwargs)
        return Handoff(**defaults)

    def test_create_minimal_handoff(self):
        """Handoff can be created with required fields."""
        handoff = self._make_handoff()
        assert handoff.status == HandoffStatus.REQUESTED
        assert handoff.handoff_type == HandoffType.DELEGATION

    def test_handoff_id_is_valid_uuid(self):
        """handoff_id is auto-generated as a valid UUID."""
        handoff = self._make_handoff()
        uuid.UUID(handoff.handoff_id)

    def test_handoff_source_and_target_agent_must_differ(self):
        """from_agent_id and to_agent_id must be different."""
        same_id = str(uuid.uuid4())
        with pytest.raises(ValidationError, ):
            self._make_handoff(source_agent_id=same_id, target_agent_id=same_id)

    def test_handoff_all_types(self):
        """All HandoffType values are valid."""
        for handoff_type in HandoffType:
            handoff = self._make_handoff(handoff_type=handoff_type)
            assert handoff.handoff_type == handoff_type

    def test_handoff_all_statuses(self):
        """All HandoffStatus values are valid (with required fields per status)."""
        for status in HandoffStatus:
            extra = {}
            if status == HandoffStatus.REJECTED:
                extra["rejection_reason"] = "Not available"
            if status == HandoffStatus.FAILED:
                extra["failure_reason"] = "Workspace sync failed"
            handoff = self._make_handoff(status=status, **extra)
            assert handoff.status == status


# ===========================================================================
# EventRecord model tests
# ===========================================================================


class TestEventRecordModel:
    """Tests for the EventRecord domain model."""

    def _make_event(self, **kwargs) -> EventRecord:
        defaults = dict(
            event_type=EventType.WORKFLOW_CREATED,
            topic=EventTopic.WORKFLOW_EVENTS,
            source=EventSource.ORCHESTRATOR,
            payload={"workflow_id": str(uuid.uuid4()), "name": "test"},
        )
        defaults.update(kwargs)
        return EventRecord(**defaults)

    def test_create_minimal_event(self):
        """EventRecord can be created with required fields."""
        event = self._make_event()
        assert event.event_type == EventType.WORKFLOW_CREATED
        assert event.topic == EventTopic.WORKFLOW_EVENTS
        assert event.source == EventSource.ORCHESTRATOR
        assert event.severity == EventSeverity.INFO

    def test_event_id_is_valid_uuid(self):
        """event_id is auto-generated as a valid UUID."""
        event = self._make_event()
        uuid.UUID(event.event_id)

    def test_event_id_validation_rejects_non_uuid(self):
        """event_id must be a valid UUID."""
        with pytest.raises(ValidationError, match="event_id"):
            self._make_event(event_id="not-a-uuid")

    def test_event_serialisation_to_kafka_bytes(self):
        """to_kafka_value() returns valid JSON bytes."""
        import json

        event = self._make_event()
        raw = event.to_kafka_value()
        assert isinstance(raw, bytes)
        parsed = json.loads(raw)
        assert parsed["event_type"] == "WORKFLOW_CREATED"
        assert parsed["source"] == "orchestrator"

    def test_event_deserialisation_from_kafka_bytes(self):
        """from_kafka_value() reconstructs the original EventRecord."""
        event = self._make_event()
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)
        assert restored.event_id == event.event_id
        assert restored.event_type == event.event_type
        assert restored.source == event.source

    def test_event_roundtrip_preserves_payload(self):
        """Payload survives serialisation/deserialisation roundtrip."""
        wf_id = str(uuid.uuid4())
        event = self._make_event(payload={"workflow_id": wf_id, "name": "roundtrip-test"})
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)
        assert restored.payload["workflow_id"] == wf_id
        assert restored.payload["name"] == "roundtrip-test"

    def test_event_occurred_at_is_utc(self):
        """occurred_at is always timezone-aware (UTC)."""
        event = self._make_event()
        assert event.occurred_at.tzinfo is not None

    def test_event_all_sources(self):
        """All EventSource values are valid."""
        for source in EventSource:
            event = self._make_event(source=source)
            assert event.source == source

    def test_event_with_correlation_and_causation_ids(self):
        """EventRecord accepts correlation_id and causation_id."""
        corr_id = str(uuid.uuid4())
        caus_id = str(uuid.uuid4())
        event = self._make_event(correlation_id=corr_id, causation_id=caus_id)
        assert event.correlation_id == corr_id
        assert event.causation_id == caus_id

    def test_event_task_events_topic(self):
        """Task events use the TASK_EVENTS topic."""
        event = EventRecord(
            event_type=EventType.TASK_ASSIGNED,
            topic=EventTopic.TASK_EVENTS,
            source=EventSource.ORCHESTRATOR,
            payload={"task_id": str(uuid.uuid4())},
        )
        assert event.topic == EventTopic.TASK_EVENTS

    def test_event_is_frozen(self):
        """EventRecord is immutable (frozen Pydantic model)."""
        event = self._make_event()
        with pytest.raises(Exception):  # ValidationError or TypeError
            event.event_type = EventType.WORKFLOW_COMPLETED  # type: ignore[misc]
