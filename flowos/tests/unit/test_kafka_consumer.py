"""
tests/unit/test_kafka_consumer.py — Unit tests for FlowOS Kafka consumer.

Tests cover:
- FlowOSConsumer initialisation
- Handler registration: on(), on_all(), register(), register_wildcard()
- Handler dispatch: correct handler called for event type
- Wildcard handler: receives all events
- Error handling strategies: SKIP, RAISE
- stop() method: graceful shutdown
- Consumer group configuration
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from unittest.mock import MagicMock, patch, call

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.kafka.consumer import FlowOSConsumer, ErrorStrategy
from shared.kafka.topics import ConsumerGroup, KafkaTopic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_bytes(
    event_type: EventType = EventType.WORKFLOW_CREATED,
    topic: EventTopic = EventTopic.WORKFLOW_EVENTS,
    workflow_id: str | None = None,
) -> bytes:
    wf_id = workflow_id or str(uuid.uuid4())
    event = EventRecord(
        event_type=event_type,
        topic=topic,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
        payload={"workflow_id": wf_id, "name": "test"},
    )
    return event.to_kafka_value()


def _make_mock_message(
    event_type: EventType = EventType.WORKFLOW_CREATED,
    topic: EventTopic = EventTopic.WORKFLOW_EVENTS,
    workflow_id: str | None = None,
) -> MagicMock:
    """Create a mock confluent-kafka Message."""
    msg = MagicMock()
    msg.error.return_value = None
    msg.value.return_value = _make_event_bytes(event_type, topic, workflow_id)
    msg.topic.return_value = str(topic)
    msg.partition.return_value = 0
    msg.offset.return_value = 0
    return msg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFlowOSConsumerInit:
    """Tests for FlowOSConsumer initialisation."""

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_initialises_with_group_and_topics(self, mock_consumer_cls):
        """Consumer initialises with group_id and topics."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id=ConsumerGroup.ORCHESTRATOR,
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )
        assert consumer._group_id == str(ConsumerGroup.ORCHESTRATOR)
        assert str(KafkaTopic.WORKFLOW_EVENTS) in consumer._topics

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_accepts_string_group_id(self, mock_consumer_cls):
        """Consumer accepts a plain string as group_id."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="my-custom-group",
            topics=[KafkaTopic.TASK_EVENTS],
        )
        assert consumer._group_id == "my-custom-group"

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_not_running_on_init(self, mock_consumer_cls):
        """Consumer is not running immediately after initialisation."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )
        assert consumer._running is False

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_default_error_strategy_is_skip(self, mock_consumer_cls):
        """Consumer defaults to SKIP error strategy."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )
        assert consumer._error_strategy == ErrorStrategy.SKIP

    @patch("shared.kafka.consumer.Consumer")
    def test_consumer_accepts_multiple_topics(self, mock_consumer_cls):
        """Consumer can subscribe to multiple topics."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS, KafkaTopic.TASK_EVENTS, KafkaTopic.AGENT_EVENTS],
        )
        assert len(consumer._topics) == 3


class TestFlowOSConsumerHandlerRegistration:
    """Tests for handler registration methods."""

    @patch("shared.kafka.consumer.Consumer")
    def test_on_decorator_registers_handler(self, mock_consumer_cls):
        """@consumer.on() registers a handler for the given event type."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handle_created(event: EventRecord) -> None:
            pass

        assert str(EventType.WORKFLOW_CREATED) in consumer._handlers
        assert handle_created in consumer._handlers[str(EventType.WORKFLOW_CREATED)]

    @patch("shared.kafka.consumer.Consumer")
    def test_on_decorator_registers_multiple_event_types(self, mock_consumer_cls):
        """@consumer.on() can register a handler for multiple event types."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        @consumer.on(EventType.WORKFLOW_CREATED, EventType.WORKFLOW_STARTED)
        def handle_workflow(event: EventRecord) -> None:
            pass

        assert handle_workflow in consumer._handlers[str(EventType.WORKFLOW_CREATED)]
        assert handle_workflow in consumer._handlers[str(EventType.WORKFLOW_STARTED)]

    @patch("shared.kafka.consumer.Consumer")
    def test_on_all_registers_wildcard_handler(self, mock_consumer_cls):
        """@consumer.on_all() registers a wildcard handler."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        @consumer.on_all()
        def handle_all(event: EventRecord) -> None:
            pass

        assert "__all__" in consumer._handlers
        assert handle_all in consumer._handlers["__all__"]

    @patch("shared.kafka.consumer.Consumer")
    def test_register_programmatic_handler(self, mock_consumer_cls):
        """register() programmatically registers a handler."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        def my_handler(event: EventRecord) -> None:
            pass

        consumer.register(EventType.WORKFLOW_CREATED, my_handler)
        assert my_handler in consumer._handlers[str(EventType.WORKFLOW_CREATED)]

    @patch("shared.kafka.consumer.Consumer")
    def test_register_wildcard_programmatic(self, mock_consumer_cls):
        """register_wildcard() programmatically registers a wildcard handler."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        def my_handler(event: EventRecord) -> None:
            pass

        consumer.register_wildcard(my_handler)
        assert my_handler in consumer._handlers["__all__"]

    @patch("shared.kafka.consumer.Consumer")
    def test_on_decorator_returns_original_function(self, mock_consumer_cls):
        """@consumer.on() returns the original function unchanged."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handle_created(event: EventRecord) -> None:
            pass

        # The decorator should return the original function
        assert callable(handle_created)
        assert handle_created.__name__ == "handle_created"


class TestFlowOSConsumerDispatch:
    """Tests for message dispatch to handlers."""

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_dispatches_to_correct_handler(self, mock_consumer_cls):
        """_process_message() dispatches to the handler registered for the event type."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        received_events = []

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handle_created(event: EventRecord) -> None:
            received_events.append(event)

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        consumer._process_message(msg)

        assert len(received_events) == 1
        assert received_events[0].event_type == EventType.WORKFLOW_CREATED

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_calls_wildcard_handler(self, mock_consumer_cls):
        """_process_message() calls wildcard handlers for all event types."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        received_events = []

        @consumer.on_all()
        def handle_all(event: EventRecord) -> None:
            received_events.append(event)

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        consumer._process_message(msg)

        assert len(received_events) == 1

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_calls_both_specific_and_wildcard(self, mock_consumer_cls):
        """_process_message() calls both specific and wildcard handlers."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        specific_calls = []
        wildcard_calls = []

        @consumer.on(EventType.WORKFLOW_CREATED)
        def handle_specific(event: EventRecord) -> None:
            specific_calls.append(event)

        @consumer.on_all()
        def handle_all(event: EventRecord) -> None:
            wildcard_calls.append(event)

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        consumer._process_message(msg)

        assert len(specific_calls) == 1
        assert len(wildcard_calls) == 1

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_skips_unregistered_event_type(self, mock_consumer_cls):
        """_process_message() does not fail for unregistered event types."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )

        # Register handler for TASK_ASSIGNED but send WORKFLOW_CREATED
        task_calls = []

        @consumer.on(EventType.TASK_ASSIGNED)
        def handle_task(event: EventRecord) -> None:
            task_calls.append(event)

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        consumer._process_message(msg)  # Should not raise

        assert len(task_calls) == 0

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_with_skip_strategy_continues_on_error(self, mock_consumer_cls):
        """With SKIP strategy, handler errors are logged but don't stop the consumer."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
            error_strategy=ErrorStrategy.SKIP,
        )

        @consumer.on(EventType.WORKFLOW_CREATED)
        def failing_handler(event: EventRecord) -> None:
            raise ValueError("Handler error!")

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        # Should not raise with SKIP strategy
        consumer._process_message(msg)

    @patch("shared.kafka.consumer.Consumer")
    def test_process_message_with_raise_strategy_propagates_error(self, mock_consumer_cls):
        """With RAISE strategy, handler errors propagate to the caller."""
        mock_inner = MagicMock()
        mock_consumer_cls.return_value = mock_inner
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
            error_strategy=ErrorStrategy.RAISE,
        )

        @consumer.on(EventType.WORKFLOW_CREATED)
        def failing_handler(event: EventRecord) -> None:
            raise ValueError("Handler error!")

        msg = _make_mock_message(EventType.WORKFLOW_CREATED)
        with pytest.raises(ValueError, match="Handler error!"):
            consumer._process_message(msg)


class TestFlowOSConsumerStop:
    """Tests for FlowOSConsumer stop() method."""

    @patch("shared.kafka.consumer.Consumer")
    def test_stop_sets_stop_event(self, mock_consumer_cls):
        """stop() sets the internal stop event."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )
        consumer.stop()
        assert consumer._stop_event.is_set()

    @patch("shared.kafka.consumer.Consumer")
    def test_stop_signals_the_stop_event_so_run_loop_exits(self, mock_consumer_cls):
        """stop() signals the stop event so the run loop will exit on next iteration."""
        mock_consumer_cls.return_value = MagicMock()
        consumer = FlowOSConsumer(
            group_id="test-group",
            topics=[KafkaTopic.WORKFLOW_EVENTS],
        )
        # Before stop: stop event is not set
        assert not consumer._stop_event.is_set()
        consumer.stop()
        # After stop: stop event is set (run loop will exit)
        assert consumer._stop_event.is_set()


class TestErrorStrategy:
    """Tests for ErrorStrategy constants."""

    def test_error_strategy_constants(self):
        """ErrorStrategy has the expected constant values."""
        assert ErrorStrategy.SKIP == "skip"
        assert ErrorStrategy.RAISE == "raise"
        assert ErrorStrategy.DLQ == "dlq"
