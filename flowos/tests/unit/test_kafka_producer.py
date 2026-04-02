"""
tests/unit/test_kafka_producer.py — Unit tests for FlowOS Kafka producer.

Tests cover:
- FlowOSProducer initialisation
- produce() method: topic routing, key derivation, headers
- produce_sync() method: delivery confirmation
- produce_batch() method: batch production
- Error handling: closed producer, buffer errors
- Context manager usage
- _derive_key() and _build_headers() helpers
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch, call

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.kafka.producer import FlowOSProducer, _default_delivery_callback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: EventType = EventType.WORKFLOW_CREATED,
    topic: EventTopic = EventTopic.WORKFLOW_EVENTS,
    workflow_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> EventRecord:
    wf_id = workflow_id or str(uuid.uuid4())
    return EventRecord(
        event_type=event_type,
        topic=topic,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
        task_id=task_id,
        agent_id=agent_id,
        payload={"workflow_id": wf_id, "name": "test"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFlowOSProducerInit:
    """Tests for FlowOSProducer initialisation."""

    @patch("shared.kafka.producer.Producer")
    def test_producer_initialises_with_default_config(self, mock_producer_cls):
        """Producer initialises successfully with default settings."""
        mock_producer_cls.return_value = MagicMock()
        producer = FlowOSProducer()
        assert producer is not None
        mock_producer_cls.assert_called_once()

    @patch("shared.kafka.producer.Producer")
    def test_producer_accepts_extra_config(self, mock_producer_cls):
        """Producer merges extra_config into the base config."""
        mock_producer_cls.return_value = MagicMock()
        extra = {"message.max.bytes": 1000000}
        producer = FlowOSProducer(extra_config=extra)
        # Verify the config passed to Producer includes the extra key
        call_kwargs = mock_producer_cls.call_args[0][0]
        assert call_kwargs.get("message.max.bytes") == 1000000

    @patch("shared.kafka.producer.Producer")
    def test_producer_accepts_custom_delivery_callback(self, mock_producer_cls):
        """Producer stores a custom delivery callback."""
        mock_producer_cls.return_value = MagicMock()
        custom_cb = MagicMock()
        producer = FlowOSProducer(delivery_callback=custom_cb)
        assert producer._delivery_callback is custom_cb

    @patch("shared.kafka.producer.Producer")
    def test_producer_not_closed_on_init(self, mock_producer_cls):
        """Producer is not closed immediately after initialisation."""
        mock_producer_cls.return_value = MagicMock()
        producer = FlowOSProducer()
        assert producer._closed is False


class TestFlowOSProducerProduce:
    """Tests for FlowOSProducer.produce()."""

    @patch("shared.kafka.producer.Producer")
    def test_produce_calls_underlying_producer(self, mock_producer_cls):
        """produce() calls the underlying confluent-kafka Producer.produce()."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce(event)

        mock_inner.produce.assert_called_once()
        call_kwargs = mock_inner.produce.call_args[1]
        assert call_kwargs["topic"] == str(EventTopic.WORKFLOW_EVENTS)

    @patch("shared.kafka.producer.Producer")
    def test_produce_uses_workflow_id_as_partition_key(self, mock_producer_cls):
        """produce() uses workflow_id as the partition key when available."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        wf_id = str(uuid.uuid4())
        event = _make_event(workflow_id=wf_id)
        producer.produce(event)

        call_kwargs = mock_inner.produce.call_args[1]
        key = call_kwargs["key"]
        assert key == wf_id.encode("utf-8")

    @patch("shared.kafka.producer.Producer")
    def test_produce_uses_task_id_when_no_workflow_id(self, mock_producer_cls):
        """produce() falls back to task_id when workflow_id is absent."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        task_id = str(uuid.uuid4())
        event = EventRecord(
            event_type=EventType.TASK_ASSIGNED,
            topic=EventTopic.TASK_EVENTS,
            source=EventSource.ORCHESTRATOR,
            task_id=task_id,
            payload={"task_id": task_id},
        )
        producer.produce(event)

        call_kwargs = mock_inner.produce.call_args[1]
        key = call_kwargs["key"]
        assert key == task_id.encode("utf-8")

    @patch("shared.kafka.producer.Producer")
    def test_produce_includes_event_type_header(self, mock_producer_cls):
        """produce() includes event_type in Kafka message headers."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce(event)

        call_kwargs = mock_inner.produce.call_args[1]
        # Headers are list of (str, bytes) tuples
        header_dict = {k: v for k, v in call_kwargs["headers"]}
        assert "event_type" in header_dict
        assert header_dict["event_type"] == b"WORKFLOW_CREATED"

    @patch("shared.kafka.producer.Producer")
    def test_produce_raises_when_closed(self, mock_producer_cls):
        """produce() raises RuntimeError when the producer is closed."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()
        producer._closed = True

        event = _make_event()
        with pytest.raises(RuntimeError, match="closed"):
            producer.produce(event)

    @patch("shared.kafka.producer.Producer")
    def test_produce_polls_after_produce(self, mock_producer_cls):
        """produce() calls poll(0) after producing to trigger delivery callbacks."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce(event, poll_after=True)

        mock_inner.poll.assert_called_with(0)

    @patch("shared.kafka.producer.Producer")
    def test_produce_skips_poll_when_poll_after_false(self, mock_producer_cls):
        """produce() skips poll() when poll_after=False."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce(event, poll_after=False)

        mock_inner.poll.assert_not_called()

    @patch("shared.kafka.producer.Producer")
    def test_produce_with_custom_partition_key(self, mock_producer_cls):
        """produce() uses the provided partition_key override."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce(event, partition_key="custom-key")

        call_kwargs = mock_inner.produce.call_args[1]
        assert call_kwargs["key"] == b"custom-key"


class TestFlowOSProducerProduceSync:
    """Tests for FlowOSProducer.produce_sync()."""

    @patch("shared.kafka.producer.Producer")
    def test_produce_sync_flushes_after_produce(self, mock_producer_cls):
        """produce_sync() calls flush() to wait for delivery."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0  # 0 messages remaining
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        producer.produce_sync(event)

        mock_inner.flush.assert_called_once()

    @patch("shared.kafka.producer.Producer")
    def test_produce_sync_raises_on_timeout(self, mock_producer_cls):
        """produce_sync() raises RuntimeError when flush times out."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 1  # 1 message still in queue
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        event = _make_event()
        with pytest.raises(RuntimeError, match="Timed out"):
            producer.produce_sync(event, timeout=5.0)

    @patch("shared.kafka.producer.Producer")
    def test_produce_sync_raises_when_closed(self, mock_producer_cls):
        """produce_sync() raises RuntimeError when producer is closed."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()
        producer._closed = True

        event = _make_event()
        with pytest.raises(RuntimeError, match="closed"):
            producer.produce_sync(event)


class TestFlowOSProducerBatch:
    """Tests for FlowOSProducer.produce_batch()."""

    @patch("shared.kafka.producer.Producer")
    def test_produce_batch_produces_all_events(self, mock_producer_cls):
        """produce_batch() produces all events in the list."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        events = [_make_event() for _ in range(5)]
        producer.produce_batch(events)

        assert mock_inner.produce.call_count == 5

    @patch("shared.kafka.producer.Producer")
    def test_produce_batch_flushes_by_default(self, mock_producer_cls):
        """produce_batch() flushes after producing all events by default."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        events = [_make_event() for _ in range(3)]
        producer.produce_batch(events, flush=True)

        mock_inner.flush.assert_called_once()

    @patch("shared.kafka.producer.Producer")
    def test_produce_batch_skips_flush_when_disabled(self, mock_producer_cls):
        """produce_batch() skips flush when flush=False."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        events = [_make_event() for _ in range(2)]
        producer.produce_batch(events, flush=False)

        mock_inner.flush.assert_not_called()

    @patch("shared.kafka.producer.Producer")
    def test_produce_batch_raises_when_closed(self, mock_producer_cls):
        """produce_batch() raises RuntimeError when producer is closed."""
        mock_inner = MagicMock()
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()
        producer._closed = True

        with pytest.raises(RuntimeError, match="closed"):
            producer.produce_batch([_make_event()])


class TestFlowOSProducerLifecycle:
    """Tests for FlowOSProducer lifecycle methods."""

    @patch("shared.kafka.producer.Producer")
    def test_flush_delegates_to_inner_producer(self, mock_producer_cls):
        """flush() delegates to the underlying producer's flush()."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        producer.flush()
        mock_inner.flush.assert_called_once()

    @patch("shared.kafka.producer.Producer")
    def test_close_marks_producer_as_closed(self, mock_producer_cls):
        """close() marks the producer as closed."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0
        mock_producer_cls.return_value = mock_inner
        producer = FlowOSProducer()

        producer.close()
        assert producer._closed is True

    @patch("shared.kafka.producer.Producer")
    def test_context_manager_closes_on_exit(self, mock_producer_cls):
        """Using producer as context manager closes it on exit."""
        mock_inner = MagicMock()
        mock_inner.flush.return_value = 0
        mock_producer_cls.return_value = mock_inner

        with FlowOSProducer() as producer:
            assert producer._closed is False

        assert producer._closed is True


class TestDefaultDeliveryCallback:
    """Tests for the _default_delivery_callback function."""

    def test_callback_logs_error_on_failure(self, caplog):
        """Callback logs an error when err is not None."""
        import logging

        mock_msg = MagicMock()
        mock_msg.topic.return_value = "flowos.workflow.events"
        mock_msg.partition.return_value = 0
        mock_msg.offset.return_value = 100

        with caplog.at_level(logging.ERROR):
            _default_delivery_callback(Exception("Delivery failed"), mock_msg)

        assert any("delivery failed" in r.message.lower() for r in caplog.records)

    def test_callback_logs_debug_on_success(self, caplog):
        """Callback logs at DEBUG level on successful delivery."""
        import logging

        mock_msg = MagicMock()
        mock_msg.topic.return_value = "flowos.workflow.events"
        mock_msg.partition.return_value = 0
        mock_msg.offset.return_value = 42
        mock_msg.key.return_value = b"workflow-123"

        with caplog.at_level(logging.DEBUG):
            _default_delivery_callback(None, mock_msg)

        assert any("delivery confirmed" in r.message.lower() for r in caplog.records)
