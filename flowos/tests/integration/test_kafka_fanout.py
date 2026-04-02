"""
tests/integration/test_kafka_fanout.py — Integration tests for Kafka fan-out model.

Tests the fan-out pattern where a single produced event is consumed by multiple
consumer groups. Uses mocked Kafka infrastructure to test the routing logic
without requiring a live Kafka broker.

Tests cover:
- Topic routing: events go to the correct topic
- Consumer group isolation: each group receives its own copy
- Event serialisation/deserialisation roundtrip
- Multiple handlers receiving the same event
- Wildcard handler receives all events
- Handler dispatch order
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.kafka.topics import KafkaTopic, ConsumerGroup, topic_for_event
from shared.kafka.schemas import (
    WorkflowCreatedPayload,
    TaskAssignedPayload,
    build_event,
)
from shared.kafka.producer import FlowOSProducer
from shared.kafka.consumer import FlowOSConsumer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workflow_created_event(workflow_id: str | None = None) -> EventRecord:
    """Build a WORKFLOW_CREATED event."""
    wf_id = workflow_id or str(uuid.uuid4())
    payload = WorkflowCreatedPayload(
        workflow_id=wf_id,
        name="test-workflow",
        trigger="manual",
        owner_agent_id="agent-001",
    )
    return build_event(
        event_type=EventType.WORKFLOW_CREATED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
    )


def _make_task_assigned_event(task_id: str | None = None, workflow_id: str | None = None) -> EventRecord:
    """Build a TASK_ASSIGNED event."""
    t_id = task_id or str(uuid.uuid4())
    wf_id = workflow_id or str(uuid.uuid4())
    payload = TaskAssignedPayload(
        task_id=t_id,
        workflow_id=wf_id,
        name="test-task",
        task_type="human",
        assigned_agent_id="agent-001",
    )
    return build_event(
        event_type=EventType.TASK_ASSIGNED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
        task_id=t_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTopicRouting:
    """Tests for event-to-topic routing."""

    def test_workflow_events_route_to_workflow_topic(self):
        """WORKFLOW_* events route to the workflow events topic."""
        workflow_event_types = [
            EventType.WORKFLOW_CREATED,
            EventType.WORKFLOW_STARTED,
            EventType.WORKFLOW_COMPLETED,
            EventType.WORKFLOW_FAILED,
            EventType.WORKFLOW_CANCELLED,
            EventType.WORKFLOW_PAUSED,
        ]
        for event_type in workflow_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.WORKFLOW_EVENTS, \
                f"Expected {event_type} to route to WORKFLOW_EVENTS, got {topic}"

    def test_task_events_route_to_task_topic(self):
        """TASK_* events route to the task events topic."""
        task_event_types = [
            EventType.TASK_CREATED,
            EventType.TASK_ASSIGNED,
            EventType.TASK_ACCEPTED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
        ]
        for event_type in task_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.TASK_EVENTS, \
                f"Expected {event_type} to route to TASK_EVENTS, got {topic}"

    def test_agent_events_route_to_agent_topic(self):
        """AGENT_* events route to the agent events topic."""
        agent_event_types = [
            EventType.AGENT_REGISTERED,
            EventType.AGENT_ONLINE,
            EventType.AGENT_OFFLINE,
        ]
        for event_type in agent_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.AGENT_EVENTS, \
                f"Expected {event_type} to route to AGENT_EVENTS, got {topic}"

    def test_build_events_route_to_build_topic(self):
        """BUILD_* events route to the build events topic."""
        build_event_types = [
            EventType.BUILD_TRIGGERED,
            EventType.BUILD_STARTED,
            EventType.BUILD_SUCCEEDED,
            EventType.BUILD_FAILED,
        ]
        for event_type in build_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.BUILD_EVENTS, \
                f"Expected {event_type} to route to BUILD_EVENTS, got {topic}"

    def test_workspace_events_route_to_workspace_topic(self):
        """WORKSPACE_* and CHECKPOINT_* events route to workspace topic."""
        workspace_event_types = [
            EventType.WORKSPACE_CREATED,
            EventType.CHECKPOINT_CREATED,
            EventType.CHECKPOINT_REVERTED,
            EventType.BRANCH_CREATED,
        ]
        for event_type in workspace_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.WORKSPACE_EVENTS, \
                f"Expected {event_type} to route to WORKSPACE_EVENTS, got {topic}"

    def test_policy_events_route_to_policy_topic(self):
        """POLICY_* events route to the policy events topic."""
        policy_event_types = [
            EventType.POLICY_EVALUATED,
            EventType.POLICY_VIOLATION_DETECTED,
            EventType.APPROVAL_REQUESTED,
        ]
        for event_type in policy_event_types:
            topic = topic_for_event(event_type)
            assert topic == KafkaTopic.POLICY_EVENTS, \
                f"Expected {event_type} to route to POLICY_EVENTS, got {topic}"


class TestEventSerialisation:
    """Tests for event serialisation/deserialisation roundtrip."""

    def test_workflow_created_event_roundtrip(self):
        """WORKFLOW_CREATED event survives serialisation/deserialisation."""
        event = _make_workflow_created_event()
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_id == event.event_id
        assert restored.event_type == EventType.WORKFLOW_CREATED
        assert restored.topic == EventTopic.WORKFLOW_EVENTS
        assert restored.source == EventSource.ORCHESTRATOR
        assert restored.workflow_id == event.workflow_id
        assert restored.payload["workflow_id"] == event.payload["workflow_id"]

    def test_task_assigned_event_roundtrip(self):
        """TASK_ASSIGNED event survives serialisation/deserialisation."""
        event = _make_task_assigned_event()
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_id == event.event_id
        assert restored.event_type == EventType.TASK_ASSIGNED
        assert restored.task_id == event.task_id
        assert restored.workflow_id == event.workflow_id

    def test_event_payload_preserved_in_roundtrip(self):
        """Event payload is fully preserved through serialisation."""
        wf_id = str(uuid.uuid4())
        event = _make_workflow_created_event(workflow_id=wf_id)
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.payload["workflow_id"] == wf_id
        assert restored.payload["name"] == "test-workflow"
        assert restored.payload["trigger"] == "manual"

    def test_event_metadata_preserved_in_roundtrip(self):
        """Event metadata is preserved through serialisation."""
        event = _make_workflow_created_event()
        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.occurred_at == event.occurred_at
        assert restored.schema_version == event.schema_version


class TestFanOutPattern:
    """Tests for the Kafka fan-out pattern using mocked consumers."""

    @patch("shared.kafka.consumer.Consumer")
    def test_multiple_handlers_receive_same_event(self, mock_consumer_cls):
        """Multiple handlers registered for the same event type all receive the event."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner

        consumer = FlowOSConsumer(
            group_id=ConsumerGroup.ORCHESTRATOR,
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        handler1_calls = []
        handler2_calls = []

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handler1(event: EventRecord) -> None:
            handler1_calls.append(event)

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handler2(event: EventRecord) -> None:
            handler2_calls.append(event)

        # Simulate receiving a message
        event = _make_workflow_created_event()
        msg = MagicMock()
        msg.error.return_value = None
        msg.value.return_value = event.to_kafka_value()
        consumer._process_message(msg)

        assert len(handler1_calls) == 1
        assert len(handler2_calls) == 1
        assert handler1_calls[0].event_id == event.event_id
        assert handler2_calls[0].event_id == event.event_id

    @patch("shared.kafka.consumer.Consumer")
    def test_wildcard_handler_receives_all_event_types(self, mock_consumer_cls):
        """Wildcard handler receives events of any type."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner

        consumer = FlowOSConsumer(
            group_id=ConsumerGroup.OBSERVABILITY,
            topics=[KafkaTopic.WORKFLOW_EVENTS, KafkaTopic.TASK_EVENTS],
        )

        all_events = []

        @consumer.on_all()
        def catch_all(event: EventRecord) -> None:
            all_events.append(event)

        # Send different event types
        events = [
            _make_workflow_created_event(),
            _make_task_assigned_event(),
        ]

        for event in events:
            msg = MagicMock()
            msg.error.return_value = None
            msg.value.return_value = event.to_kafka_value()
            consumer._process_message(msg)

        assert len(all_events) == 2
        event_types = {e.event_type for e in all_events}
        assert EventType.WORKFLOW_CREATED in event_types
        assert EventType.TASK_ASSIGNED in event_types

    @patch("shared.kafka.producer.Producer")
    def test_producer_routes_event_to_correct_topic(self, mock_producer_cls):
        """Producer routes each event to its canonical Kafka topic."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner

        producer = FlowOSProducer()

        # Produce a workflow event
        wf_event = _make_workflow_created_event()
        producer.produce(wf_event, poll_after=False)

        # Produce a task event
        task_event = _make_task_assigned_event()
        producer.produce(task_event, poll_after=False)

        assert mock_inner.produce.call_count == 2

        # Check topics
        calls = mock_inner.produce.call_args_list
        topics = [c[1]["topic"] for c in calls]
        assert str(KafkaTopic.WORKFLOW_EVENTS) in topics
        assert str(KafkaTopic.TASK_EVENTS) in topics

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_group_isolation(self, mock_consumer_cls):
        """Different consumer groups process events independently."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner

        # Create two consumers in different groups
        orchestrator_events = []
        ui_events = []

        consumer_orch = FlowOSConsumer(
            group_id=ConsumerGroup.ORCHESTRATOR,
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        consumer_ui = FlowOSConsumer(
            group_id=ConsumerGroup.UI_STREAM,
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        @consumer_orch.on(EventType.WORKFLOW_CREATED)
        def orch_handler(event: EventRecord) -> None:
            orchestrator_events.append(event)

        @consumer_ui.on(EventType.WORKFLOW_CREATED)
        def ui_handler(event: EventRecord) -> None:
            ui_events.append(event)

        event = _make_workflow_created_event()
        msg = MagicMock()
        msg.error.return_value = None
        msg.value.return_value = event.to_kafka_value()

        # Both consumers process the same message independently
        consumer_orch._process_message(msg)
        consumer_ui._process_message(msg)

        assert len(orchestrator_events) == 1
        assert len(ui_events) == 1
        assert orchestrator_events[0].event_id == ui_events[0].event_id


class TestConsumerGroupRegistry:
    """Tests for the ConsumerGroup registry."""

    def test_all_consumer_groups_are_defined(self):
        """All expected consumer groups are defined."""
        expected_groups = [
            ConsumerGroup.ORCHESTRATOR,
            ConsumerGroup.UI_STREAM,
            ConsumerGroup.POLICY_ENGINE,
            ConsumerGroup.OBSERVABILITY,
        ]
        for group in expected_groups:
            assert group is not None
            assert str(group)  # Should have a non-empty string value

    def test_consumer_groups_have_unique_ids(self):
        """All consumer group IDs are unique."""
        group_ids = [str(g) for g in ConsumerGroup]
        assert len(group_ids) == len(set(group_ids))

    def test_kafka_topics_all_defined(self):
        """All expected Kafka topics are defined."""
        expected_topics = [
            KafkaTopic.WORKFLOW_EVENTS,
            KafkaTopic.TASK_EVENTS,
            KafkaTopic.AGENT_EVENTS,
            KafkaTopic.BUILD_EVENTS,
            KafkaTopic.AI_EVENTS,
            KafkaTopic.WORKSPACE_EVENTS,
            KafkaTopic.POLICY_EVENTS,
            KafkaTopic.OBSERVABILITY_EVENTS,
        ]
        for topic in expected_topics:
            assert topic is not None
            assert str(topic).startswith("flowos.")
