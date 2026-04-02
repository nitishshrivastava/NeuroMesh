"""
shared/kafka/producer.py — FlowOS Kafka Producer

Thread-safe, singleton-capable Kafka producer for the FlowOS event bus.

Features:
- Automatic topic routing via ``TOPIC_FOR_EVENT`` (no need to specify topic manually)
- Synchronous and fire-and-forget (async) produce modes
- Delivery confirmation with configurable timeout
- Structured delivery callbacks with full error context
- Partition key derivation from workflow_id / task_id / agent_id for ordering
- Graceful flush on shutdown
- Retry logic via confluent-kafka's built-in retry mechanism (configured in
  ``KafkaSettings``)

Usage::

    from shared.kafka.producer import FlowOSProducer
    from shared.kafka.schemas import WorkflowCreatedPayload, build_event
    from shared.models.event import EventSource, EventType

    producer = FlowOSProducer()

    # Build a typed event
    payload = WorkflowCreatedPayload(
        workflow_id="wf-123",
        name="deploy-service",
        trigger="manual",
    )
    event = build_event(
        event_type=EventType.WORKFLOW_CREATED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id="wf-123",
    )

    # Produce (fire-and-forget with delivery callback)
    producer.produce(event)

    # Produce and wait for delivery confirmation
    producer.produce_sync(event, timeout=10.0)

    # Flush all pending messages before shutdown
    producer.flush()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from confluent_kafka import KafkaException, Message, Producer

from shared.config import settings
from shared.models.event import EventRecord, EventType
from shared.kafka.topics import KafkaTopic, topic_for_event

logger = logging.getLogger(__name__)

# Type alias for delivery callbacks
DeliveryCallback = Callable[[Exception | None, Message | None], None]


# ─────────────────────────────────────────────────────────────────────────────
# Default delivery callback
# ─────────────────────────────────────────────────────────────────────────────


def _default_delivery_callback(
    err: Exception | None,
    msg: Message | None,
) -> None:
    """
    Default delivery report callback.

    Logs successful deliveries at DEBUG level and failures at ERROR level.
    """
    if err is not None:
        logger.error(
            "Kafka delivery failed | topic=%s partition=%s offset=%s error=%s",
            msg.topic() if msg else "unknown",
            msg.partition() if msg else "unknown",
            msg.offset() if msg else "unknown",
            err,
        )
    else:
        logger.debug(
            "Kafka delivery confirmed | topic=%s partition=%s offset=%s key=%s",
            msg.topic(),
            msg.partition(),
            msg.offset(),
            msg.key().decode("utf-8") if msg.key() else None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Producer class
# ─────────────────────────────────────────────────────────────────────────────


class FlowOSProducer:
    """
    Thread-safe Kafka producer for the FlowOS event bus.

    A single instance can be shared across threads.  The underlying
    confluent-kafka ``Producer`` is thread-safe for ``produce()`` calls.

    Lifecycle:
        producer = FlowOSProducer()
        producer.produce(event)          # fire-and-forget
        producer.produce_sync(event)     # wait for delivery
        producer.flush()                 # drain before shutdown
        producer.close()                 # flush + release resources

    Context manager::

        with FlowOSProducer() as producer:
            producer.produce(event)
    """

    def __init__(
        self,
        extra_config: dict[str, object] | None = None,
        delivery_callback: DeliveryCallback | None = None,
    ) -> None:
        """
        Initialise the producer.

        Args:
            extra_config:      Optional confluent-kafka config overrides merged
                               on top of ``KafkaSettings.as_producer_config()``.
            delivery_callback: Custom delivery report callback.  Defaults to
                               ``_default_delivery_callback``.
        """
        self._lock = threading.Lock()
        self._closed = False
        self._delivery_callback = delivery_callback or _default_delivery_callback

        config = settings.kafka.as_producer_config()
        if extra_config:
            config.update(extra_config)

        self._producer = Producer(config)
        logger.info(
            "FlowOSProducer initialised | brokers=%s",
            settings.kafka.bootstrap_servers,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def produce(
        self,
        event: EventRecord,
        *,
        partition_key: str | None = None,
        on_delivery: DeliveryCallback | None = None,
        poll_after: bool = True,
    ) -> None:
        """
        Produce an event to its canonical Kafka topic (fire-and-forget).

        The topic is derived automatically from ``event.topic``.  The
        partition key defaults to ``workflow_id`` → ``task_id`` → ``agent_id``
        → ``event_id`` (first non-None value) to preserve ordering within a
        workflow/task scope.

        Args:
            event:         The ``EventRecord`` to produce.
            partition_key: Override the partition key.  If None, the key is
                           derived from the event's scope fields.
            on_delivery:   Per-message delivery callback override.
            poll_after:    If True, call ``poll(0)`` after produce to trigger
                           delivery callbacks for previously produced messages.

        Raises:
            RuntimeError: If the producer has been closed.
            KafkaException: If the produce call fails immediately (e.g. buffer full).
        """
        self._check_not_closed()

        topic = str(event.topic)
        key = self._derive_key(event, partition_key)
        value = event.to_kafka_value()
        callback = on_delivery or self._delivery_callback

        headers = self._build_headers(event)

        try:
            self._producer.produce(
                topic=topic,
                key=key,
                value=value,
                headers=headers,
                on_delivery=callback,
            )
            logger.debug(
                "Produced event | event_type=%s topic=%s key=%s event_id=%s",
                event.event_type,
                topic,
                key,
                event.event_id,
            )
        except BufferError as exc:
            # Producer queue is full — flush and retry once
            logger.warning(
                "Producer buffer full, flushing before retry | event_id=%s",
                event.event_id,
            )
            self._producer.flush(timeout=5)
            self._producer.produce(
                topic=topic,
                key=key,
                value=value,
                headers=headers,
                on_delivery=callback,
            )
        except KafkaException as exc:
            logger.error(
                "Failed to produce event | event_type=%s event_id=%s error=%s",
                event.event_type,
                event.event_id,
                exc,
            )
            raise

        if poll_after:
            # Trigger delivery callbacks for previously produced messages
            self._producer.poll(0)

    def produce_sync(
        self,
        event: EventRecord,
        *,
        partition_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Produce an event and wait for delivery confirmation.

        Blocks until the broker acknowledges the message or the timeout
        expires.

        Args:
            event:         The ``EventRecord`` to produce.
            partition_key: Override the partition key.
            timeout:       Maximum seconds to wait for delivery confirmation.

        Raises:
            RuntimeError: If the producer has been closed or delivery times out.
            KafkaException: If the produce or flush call fails.
        """
        self._check_not_closed()

        delivery_results: list[Exception | None] = []

        def _sync_callback(err: Exception | None, msg: Message | None) -> None:
            delivery_results.append(err)
            # Also invoke the default callback for logging
            self._delivery_callback(err, msg)

        self.produce(event, partition_key=partition_key, on_delivery=_sync_callback, poll_after=False)

        # Flush to trigger delivery
        remaining = self._producer.flush(timeout=timeout)
        if remaining > 0:
            raise RuntimeError(
                f"Timed out waiting for delivery of event {event.event_id!r} "
                f"({remaining} message(s) still in queue after {timeout}s)."
            )

        if delivery_results and delivery_results[0] is not None:
            raise KafkaException(delivery_results[0])

    def produce_batch(
        self,
        events: list[EventRecord],
        *,
        flush: bool = True,
        timeout: float = 30.0,
    ) -> None:
        """
        Produce a batch of events efficiently.

        Args:
            events:  List of ``EventRecord`` objects to produce.
            flush:   If True, flush after producing all events.
            timeout: Flush timeout in seconds.

        Raises:
            RuntimeError: If the producer has been closed.
        """
        self._check_not_closed()

        for event in events:
            self.produce(event, poll_after=False)

        if flush:
            remaining = self._producer.flush(timeout=timeout)
            if remaining > 0:
                logger.warning(
                    "Batch flush timed out: %d message(s) still in queue after %.1fs",
                    remaining,
                    timeout,
                )

    def flush(self, timeout: float = 30.0) -> int:
        """
        Wait for all outstanding produce requests to complete.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            Number of messages still in the queue after the timeout.
        """
        self._check_not_closed()
        remaining = self._producer.flush(timeout=timeout)
        if remaining > 0:
            logger.warning(
                "Producer flush timed out: %d message(s) still in queue.",
                remaining,
            )
        return remaining

    def poll(self, timeout: float = 0.0) -> int:
        """
        Serve delivery report callbacks.

        Should be called periodically in long-running producers to prevent
        the internal queue from filling up.

        Args:
            timeout: Maximum seconds to block waiting for events.

        Returns:
            Number of events served.
        """
        self._check_not_closed()
        return self._producer.poll(timeout)

    def close(self) -> None:
        """
        Flush all pending messages and release producer resources.

        After calling ``close()``, the producer cannot be used again.
        """
        with self._lock:
            if self._closed:
                return
            logger.info("Closing FlowOSProducer — flushing pending messages…")
            self._producer.flush(timeout=30)
            self._closed = True
            logger.info("FlowOSProducer closed.")

    # ─────────────────────────────────────────────────────────────────────────
    # Context manager support
    # ─────────────────────────────────────────────────────────────────────────

    def __enter__(self) -> "FlowOSProducer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _check_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError(
                "FlowOSProducer has been closed and cannot produce new messages."
            )

    @staticmethod
    def _derive_key(event: EventRecord, override: str | None) -> bytes | None:
        """
        Derive a partition key for the event.

        Priority: override → workflow_id → task_id → agent_id → event_id.
        Using workflow_id as the primary key ensures all events for a workflow
        land on the same partition, preserving ordering.
        """
        key = (
            override
            or event.workflow_id
            or event.task_id
            or event.agent_id
            or event.event_id
        )
        return key.encode("utf-8") if key else None

    @staticmethod
    def _build_headers(event: EventRecord) -> list[tuple[str, bytes]]:
        """
        Build Kafka message headers from the event envelope.

        Headers allow consumers to filter/route messages without deserialising
        the full payload.
        """
        headers: list[tuple[str, bytes]] = [
            ("event_type", event.event_type.encode("utf-8")),
            ("event_id", event.event_id.encode("utf-8")),
            ("source", event.source.encode("utf-8")),
            ("schema_version", event.schema_version.encode("utf-8")),
        ]
        if event.workflow_id:
            headers.append(("workflow_id", event.workflow_id.encode("utf-8")))
        if event.task_id:
            headers.append(("task_id", event.task_id.encode("utf-8")))
        if event.agent_id:
            headers.append(("agent_id", event.agent_id.encode("utf-8")))
        if event.correlation_id:
            headers.append(("correlation_id", event.correlation_id.encode("utf-8")))
        if event.causation_id:
            headers.append(("causation_id", event.causation_id.encode("utf-8")))
        return headers


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_producer_instance: FlowOSProducer | None = None
_producer_lock = threading.Lock()


def get_producer() -> FlowOSProducer:
    """
    Return the module-level singleton ``FlowOSProducer``.

    Creates the producer on first call.  Subsequent calls return the same
    instance.  Thread-safe.

    Returns:
        The shared ``FlowOSProducer`` instance.
    """
    global _producer_instance
    if _producer_instance is None or _producer_instance._closed:
        with _producer_lock:
            if _producer_instance is None or _producer_instance._closed:
                _producer_instance = FlowOSProducer()
    return _producer_instance


def close_producer() -> None:
    """
    Flush and close the module-level singleton producer.

    Call this during application shutdown to ensure all pending messages
    are delivered before the process exits.
    """
    global _producer_instance
    with _producer_lock:
        if _producer_instance is not None and not _producer_instance._closed:
            _producer_instance.close()
            _producer_instance = None
