"""
tests/integration/test_task_handoff.py — Integration tests for task handoff.

Tests the complete handoff lifecycle from request through acceptance and completion,
using domain models and mocked Kafka infrastructure.

Tests cover:
- Handoff creation and validation
- Handoff status transitions
- HandoffContext creation
- Handoff with checkpoint reference
- Handoff type classification
- Kafka event emission for handoffs (mocked)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from shared.models.handoff import (
    Handoff,
    HandoffContext,
    HandoffStatus,
    HandoffType,
)
from shared.models.task import Task, TaskStatus, TaskType
from shared.models.checkpoint import Checkpoint, CheckpointStatus, CheckpointType
from shared.models.event import EventRecord, EventSource, EventTopic, EventType
from shared.kafka.schemas import (
    TaskHandoffRequestedPayload,
    TaskHandoffAcceptedPayload,
    build_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handoff(
    source_agent_id: str | None = None,
    target_agent_id: str | None = None,
    handoff_type: HandoffType = HandoffType.DELEGATION,
    **kwargs,
) -> Handoff:
    """Create a test Handoff instance."""
    return Handoff(
        task_id=str(uuid.uuid4()),
        workflow_id=str(uuid.uuid4()),
        source_agent_id=source_agent_id or str(uuid.uuid4()),
        target_agent_id=target_agent_id or str(uuid.uuid4()),
        handoff_type=handoff_type,
        **kwargs,
    )


def _make_task(status: TaskStatus = TaskStatus.IN_PROGRESS) -> Task:
    """Create a test Task instance."""
    return Task(
        workflow_id=str(uuid.uuid4()),
        name="test-task",
        task_type=TaskType.HUMAN,
        status=status,
    )


def _make_checkpoint() -> Checkpoint:
    """Create a test Checkpoint instance."""
    return Checkpoint(
        task_id=str(uuid.uuid4()),
        workflow_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        git_sha="abc123def456" * 3,
        message="Pre-handoff checkpoint",
        checkpoint_type=CheckpointType.PRE_HANDOFF,
        status=CheckpointStatus.COMMITTED,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandoffCreation:
    """Tests for Handoff creation and validation."""

    def test_create_minimal_handoff(self):
        """Handoff can be created with required fields."""
        handoff = _make_handoff()
        assert handoff.status == HandoffStatus.REQUESTED
        assert handoff.handoff_type == HandoffType.DELEGATION
        assert handoff.handoff_id is not None
        assert handoff.source_agent_id != handoff.target_agent_id

    def test_handoff_id_is_valid_uuid(self):
        """handoff_id is a valid UUID."""
        handoff = _make_handoff()
        uuid.UUID(handoff.handoff_id)

    def test_handoff_source_and_target_must_differ(self):
        """source_agent_id and target_agent_id must be different."""
        from pydantic import ValidationError

        same_id = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            _make_handoff(source_agent_id=same_id, target_agent_id=same_id)

    def test_handoff_with_context(self):
        """Handoff can include a HandoffContext briefing."""
        context = HandoffContext(
            summary="Implemented authentication module",
            remaining_work="Need to add unit tests",
            known_issues=["Token refresh not implemented"],
            relevant_files=["src/auth.py", "tests/test_auth.py"],
        )
        handoff = _make_handoff(context=context)

        assert handoff.context is not None
        assert handoff.context.summary == "Implemented authentication module"
        assert "Token refresh not implemented" in handoff.context.known_issues

    def test_handoff_with_checkpoint_reference(self):
        """Handoff can reference a checkpoint."""
        checkpoint = _make_checkpoint()
        handoff = _make_handoff(checkpoint_id=checkpoint.checkpoint_id)

        assert handoff.checkpoint_id == checkpoint.checkpoint_id

    def test_handoff_all_types(self):
        """All HandoffType values create valid handoffs."""
        for handoff_type in HandoffType:
            handoff = _make_handoff(handoff_type=handoff_type)
            assert handoff.handoff_type == handoff_type


class TestHandoffStatusTransitions:
    """Tests for Handoff status transitions."""

    def test_requested_to_pending_acceptance(self):
        """Handoff transitions from REQUESTED to PENDING_ACCEPTANCE."""
        handoff = _make_handoff()
        assert handoff.status == HandoffStatus.REQUESTED

        handoff = handoff.model_copy(update={"status": HandoffStatus.PENDING_ACCEPTANCE})
        assert handoff.status == HandoffStatus.PENDING_ACCEPTANCE

    def test_pending_to_accepted(self):
        """Handoff transitions from PENDING_ACCEPTANCE to ACCEPTED."""
        handoff = _make_handoff(status=HandoffStatus.PENDING_ACCEPTANCE)
        accepted_at = datetime.now(timezone.utc)

        handoff = handoff.model_copy(
            update={
                "status": HandoffStatus.ACCEPTED,
                "accepted_at": accepted_at,
            }
        )

        assert handoff.status == HandoffStatus.ACCEPTED
        assert handoff.accepted_at is not None

    def test_accepted_to_completed(self):
        """Handoff transitions from ACCEPTED to COMPLETED."""
        handoff = _make_handoff(status=HandoffStatus.ACCEPTED)
        completed_at = datetime.now(timezone.utc)

        handoff = handoff.model_copy(
            update={
                "status": HandoffStatus.COMPLETED,
                "completed_at": completed_at,
            }
        )

        assert handoff.status == HandoffStatus.COMPLETED

    def test_rejected_requires_rejection_reason(self):
        """REJECTED status requires a rejection_reason."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_handoff(status=HandoffStatus.REJECTED)

    def test_rejected_with_reason(self):
        """REJECTED handoff with reason is valid."""
        handoff = _make_handoff(
            status=HandoffStatus.REJECTED,
            rejection_reason="Target agent is unavailable",
        )
        assert handoff.status == HandoffStatus.REJECTED
        assert handoff.rejection_reason == "Target agent is unavailable"

    def test_expired_handoff(self):
        """Handoff can be marked as EXPIRED."""
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        handoff = _make_handoff(
            status=HandoffStatus.EXPIRED,
            expires_at=expires_at,
        )
        assert handoff.status == HandoffStatus.EXPIRED


class TestHandoffTypes:
    """Tests for HandoffType classification."""

    def test_ai_to_human_handoff(self):
        """AI_TO_HUMAN handoff type is valid."""
        handoff = _make_handoff(handoff_type=HandoffType.AI_TO_HUMAN)
        assert handoff.handoff_type == HandoffType.AI_TO_HUMAN

    def test_human_to_ai_handoff(self):
        """HUMAN_TO_AI handoff type is valid."""
        handoff = _make_handoff(handoff_type=HandoffType.HUMAN_TO_AI)
        assert handoff.handoff_type == HandoffType.HUMAN_TO_AI

    def test_escalation_handoff(self):
        """ESCALATION handoff type is valid."""
        handoff = _make_handoff(handoff_type=HandoffType.ESCALATION)
        assert handoff.handoff_type == HandoffType.ESCALATION

    def test_emergency_handoff(self):
        """EMERGENCY handoff type is valid."""
        handoff = _make_handoff(handoff_type=HandoffType.EMERGENCY)
        assert handoff.handoff_type == HandoffType.EMERGENCY


class TestHandoffKafkaEvents:
    """Tests for Kafka events emitted during handoff lifecycle."""

    def test_task_handoff_requested_event_structure(self):
        """TASK_HANDOFF_REQUESTED event has correct structure."""
        handoff = _make_handoff()
        payload = TaskHandoffRequestedPayload(
            handoff_id=handoff.handoff_id,
            task_id=handoff.task_id,
            workflow_id=handoff.workflow_id,
            from_agent_id=handoff.source_agent_id,
            to_agent_id=handoff.target_agent_id,
            reason="Test handoff",
        )
        event = build_event(
            event_type=EventType.TASK_HANDOFF_REQUESTED,
            payload=payload,
            source=EventSource.CLI,
            workflow_id=handoff.workflow_id,
            task_id=handoff.task_id,
        )

        assert event.event_type == EventType.TASK_HANDOFF_REQUESTED
        assert event.topic == EventTopic.TASK_EVENTS
        assert event.payload["handoff_id"] == handoff.handoff_id
        assert event.payload["from_agent_id"] == handoff.source_agent_id
        assert event.payload["to_agent_id"] == handoff.target_agent_id

    def test_task_handoff_accepted_event_structure(self):
        """TASK_HANDOFF_ACCEPTED event has correct structure."""
        handoff = _make_handoff()
        payload = TaskHandoffAcceptedPayload(
            handoff_id=handoff.handoff_id,
            task_id=handoff.task_id,
            workflow_id=handoff.workflow_id,
            new_agent_id=handoff.target_agent_id,
        )
        event = build_event(
            event_type=EventType.TASK_HANDOFF_ACCEPTED,
            payload=payload,
            source=EventSource.CLI,
            workflow_id=handoff.workflow_id,
            task_id=handoff.task_id,
        )

        assert event.event_type == EventType.TASK_HANDOFF_ACCEPTED
        assert event.payload["handoff_id"] == handoff.handoff_id
        assert event.payload["new_agent_id"] == handoff.target_agent_id

    def test_handoff_event_roundtrip(self):
        """Handoff events survive serialisation/deserialisation."""
        handoff = _make_handoff()
        payload = TaskHandoffRequestedPayload(
            handoff_id=handoff.handoff_id,
            task_id=handoff.task_id,
            workflow_id=handoff.workflow_id,
            from_agent_id=handoff.source_agent_id,
            to_agent_id=handoff.target_agent_id,
            reason="Test handoff",
        )
        event = build_event(
            event_type=EventType.TASK_HANDOFF_REQUESTED,
            payload=payload,
            source=EventSource.CLI,
        )

        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_type == EventType.TASK_HANDOFF_REQUESTED
        assert restored.payload["handoff_id"] == handoff.handoff_id


class TestHandoffContext:
    """Tests for HandoffContext briefing document."""

    def test_create_minimal_context(self):
        """HandoffContext can be created with just a summary."""
        ctx = HandoffContext(summary="Work completed so far")
        assert ctx.summary == "Work completed so far"
        assert ctx.remaining_work is None
        assert ctx.known_issues == []

    def test_context_with_all_fields(self):
        """HandoffContext accepts all optional fields."""
        ctx = HandoffContext(
            summary="Implemented auth module",
            remaining_work="Add unit tests",
            known_issues=["Token refresh not implemented", "Rate limiting missing"],
            suggested_approach="Use pytest fixtures for test setup",
            relevant_files=["src/auth.py", "src/token.py"],
            references=["https://docs.example.com/auth"],
            custom_data={"estimated_hours": 4},
        )

        assert ctx.summary == "Implemented auth module"
        assert len(ctx.known_issues) == 2
        assert len(ctx.relevant_files) == 2
        assert ctx.custom_data["estimated_hours"] == 4

    def test_context_known_issues_list(self):
        """HandoffContext known_issues is a list of strings."""
        ctx = HandoffContext(
            summary="Test",
            known_issues=["Issue 1", "Issue 2", "Issue 3"],
        )
        assert len(ctx.known_issues) == 3
        assert all(isinstance(issue, str) for issue in ctx.known_issues)
