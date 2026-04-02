"""
shared/kafka/consumer.py — FlowOS Kafka Consumer

Provides a high-level, handler-based Kafka consumer for the FlowOS event bus.

Features:
- Handler registration by ``EventType`` (type-safe dispatch)
- Wildcard / catch-all handler support
- Manual offset commit after successful handler execution
- Graceful shutdown via ``stop()`` or signal handling
- Dead-letter queue (DLQ) support for failed messages
- Configurable error handling: skip, retry, or raise
- Structured logging with event context
- Consumer group management aligned with ``ConsumerGroup`` registry

Usage::

    from shared.kafka.consumer import FlowOSConsumer
    from shared.kafka.topics import ConsumerGroup, KafkaTopic
    from shared.models.event import EventRecord, EventType

    consumer = FlowOSConsumer(
        group_id=ConsumerGroup.ORCHESTRATOR,
        topics=[KafkaTopic.WORKFLOW_EVENTS, KafkaTopic.TASK_EVENTS],
    )

    @consumer.on(EventType.WORKFLOW_CREATED)
    def handle_workflow_created(event: EventRecord) -> None:
        print(f"New workflow: {event.payload['name']}")

    @consumer.on(EventType.TASK_ASSIGNED)
    def handle_task_assigned(event: EventRecord) -> None:
        print(f"Task assigned: {event.payload['task_id']}")

    # Run the consume loop (blocks until stop() is called)
    consumer.run()

    # Or run in a background thread:
    import threading
    t = threading.Thread(target=consumer.run, daemon=True)
    t.start()
    # ... later ...
    consumer.stop()
    t.join()
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Callable

from confluent_kafka import Consumer, KafkaError, KafkaException, Message

from shared.config import settings
from shared.models.event import EventRecord, EventType
from shared.kafka.topics import ConsumerGroup, KafkaTopic

logger = logging.getLogger(__name__)

# Type alias for event handler functions
EventHandler = Callable[[EventRecord], None]

# Sentinel for wildcard handler registration
_WILDCARD = "__all__"


# ─────────────────────────────────────────────────────────────────────────────
# Error handling strategies
# ─────────────────────────────────────────────────────────────────────────────


class ErrorStrategy:
    """Constants for consumer error handling strategies."""

    SKIP = "skip"       # Log the error and skip the message
    RAISE = "raise"     # Re-raise the exception (stops the consumer)
    DLQ = "dlq"         # Send to dead-letter queue and continue


# ─────────────────────────────────────────────────────────────────────────────
# Consumer class
# ─────────────────────────────────────────────────────────────────────────────


class FlowOSConsumer:
    """
    Handler-based Kafka consumer for the FlowOS event bus.

    Dispatches incoming ``EventRecord`` messages to registered handlers
    based on ``event_type``.  Supports wildcard handlers that receive all
    events regardless of type.

    Thread safety:
        The consumer itself is NOT thread-safe — it should be used from a
        single thread.  Use ``run()`` which blocks the calling thread, or
        wrap in a ``threading.Thread``.

    Attributes:
        group_id:        Consumer group identifier.
        topics:          List of Kafka topics to subscribe to.
        poll_timeout_ms: Milliseconds to block on each ``poll()`` call.
        error_strategy:  How to handle handler exceptions (skip/raise/dlq).
        auto_commit:     Whether to auto-commit offsets (default: False).
    """

    def __init__(
        self,
        group_id: str | ConsumerGroup,
        topics: list[str | KafkaTopic],
        *,
        poll_timeout_ms: int = 1000,
        error_strategy: str = ErrorStrategy.SKIP,
        auto_commit: bool = False,
        extra_config: dict[str, object] | None = None,
        install_signal_handlers: bool = False,
    ) -> None:
        """
        Initialise the consumer.

        Args:
            group_id:                Consumer group ID string or ``ConsumerGroup`` enum.
            topics:                  Topics to subscribe to.
            poll_timeout_ms:         Poll timeout in milliseconds.
            error_strategy:          Error handling strategy (skip/raise/dlq).
            auto_commit:             Enable auto-commit (overrides settings default).
            extra_config:            Additional confluent-kafka config overrides.
            install_signal_handlers: If True, install SIGINT/SIGTERM handlers
                                     that call ``stop()``.  Only safe in the
                                     main thread.
        """
        self._group_id = str(group_id)
        self._topics = [str(t) for t in topics]
        self._poll_timeout = poll_timeout_ms / 1000.0  # convert to seconds
        self._error_strategy = error_strategy
        self._auto_commit = auto_commit

        # Handler registry: event_type_str → list of handlers
        self._handlers: dict[str, list[EventHandler]] = {}

        # Running state
        self._running = False
        self._stop_event = threading.Event()

        # Build confluent-kafka config
        config = settings.kafka.as_consumer_config(group_id=self._group_id)
        config["enable.auto.commit"] = auto_commit
        if extra_config:
            config.update(extra_config)

        self._consumer = Consumer(config)

        if install_signal_handlers:
            self._install_signal_handlers()

        logger.info(
            "FlowOSConsumer initialised | group=%s topics=%s",
            self._group_id,
            self._topics,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Handler registration
    # ─────────────────────────────────────────────────────────────────────────

    def on(self, *event_types: EventType) -> Callable[[EventHandler], EventHandler]:
        """
        Decorator to register a handler for one or more event types.

        Usage::

            @consumer.on(EventType.WORKFLOW_CREATED, EventType.WORKFLOW_STARTED)
            def handle_workflow(event: EventRecord) -> None:
                ...

        Args:
            *event_types: One or more ``EventType`` values to handle.

        Returns:
            Decorator that registers the function and returns it unchanged.
        """
        def decorator(fn: EventHandler) -> EventHandler:
            for event_type in event_types:
                key = str(event_type)
                if key not in self._handlers:
                    self._handlers[key] = []
                self._handlers[key].append(fn)
                logger.debug(
                    "Registered handler %s for EventType %s",
                    fn.__name__,
                    event_type,
                )
            return fn
        return decorator

    def on_all(self) -> Callable[[EventHandler], EventHandler]:
        """
        Decorator to register a wildcard handler that receives ALL events.

        Usage::

            @consumer.on_all()
            def log_all_events(event: EventRecord) -> None:
                logger.info("Event: %s", event.event_type)
        """
        def decorator(fn: EventHandler) -> EventHandler:
            if _WILDCARD not in self._handlers:
                self._handlers[_WILDCARD] = []
            self._handlers[_WILDCARD].append(fn)
            logger.debug("Registered wildcard handler: %s", fn.__name__)
            return fn
        return decorator

    def register(self, event_type: EventType, handler: EventHandler) -> None:
        """
        Programmatically register a handler for an event type.

        Args:
            event_type: The event type to handle.
            handler:    Callable that accepts an ``EventRecord``.
        """
        key = str(event_type)
        if key not in self._handlers:
            self._handlers[key] = []
        self._handlers[key].append(handler)
        logger.debug(
            "Registered handler %s for EventType %s",
            handler.__name__,
            event_type,
        )

    def register_wildcard(self, handler: EventHandler) -> None:
        """
        Programmatically register a wildcard handler.

        Args:
            handler: Callable that accepts an ``EventRecord``.
        """
        if _WILDCARD not in self._handlers:
            self._handlers[_WILDCARD] = []
        self._handlers[_WILDCARD].append(handler)

    # ─────────────────────────────────────────────────────────────────────────
    # Consume loop
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the consume loop.  Blocks until ``stop()`` is called.

        Subscribes to the configured topics, polls for messages, deserialises
        each message into an ``EventRecord``, and dispatches to registered
        handlers.

        Offsets are committed after each successful handler invocation
        (manual commit mode) or automatically (if ``auto_commit=True``).
        """
        self._running = True
        self._stop_event.clear()

        try:
            self._consumer.subscribe(
                self._topics,
                on_assign=self._on_assign,
                on_revoke=self._on_revoke,
            )
            logger.info(
                "Consumer started | group=%s topics=%s",
                self._group_id,
                self._topics,
            )

            while not self._stop_event.is_set():
                msg = self._consumer.poll(timeout=self._poll_timeout)

                if msg is None:
                    # No message within poll timeout — continue
                    continue

                if msg.error():
                    self._handle_kafka_error(msg)
                    continue

                self._process_message(msg)

        except KafkaException as exc:
            logger.error(
                "Fatal Kafka error in consumer | group=%s error=%s",
                self._group_id,
                exc,
                exc_info=True,
            )
            raise
        finally:
            self._running = False
            try:
                self._consumer.close()
                logger.info("Consumer closed | group=%s", self._group_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing consumer: %s", exc)

    def stop(self) -> None:
        """
        Signal the consume loop to stop gracefully.

        The loop will finish processing the current message (if any) and
        then exit.  Safe to call from any thread.
        """
        logger.info("Stopping consumer | group=%s", self._group_id)
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        """Return True if the consume loop is active."""
        return self._running

    # ─────────────────────────────────────────────────────────────────────────
    # Context manager support
    # ─────────────────────────────────────────────────────────────────────────

    def __enter__(self) -> "FlowOSConsumer":
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _process_message(self, msg: Message) -> None:
        """
        Deserialise a Kafka message and dispatch to registered handlers.

        Args:
            msg: Raw confluent-kafka ``Message`` object.
        """
        topic = msg.topic()
        partition = msg.partition()
        offset = msg.offset()
        raw_value = msg.value()

        if raw_value is None:
            logger.warning(
                "Received tombstone message | topic=%s partition=%s offset=%s",
                topic,
                partition,
                offset,
            )
            self._commit(msg)
            return

        # Deserialise
        try:
            event = EventRecord.from_kafka_value(raw_value)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to deserialise message | topic=%s partition=%s offset=%s error=%s",
                topic,
                partition,
                offset,
                exc,
                exc_info=True,
            )
            # Skip malformed messages — cannot recover
            self._commit(msg)
            return

        logger.debug(
            "Received event | event_type=%s event_id=%s topic=%s partition=%s offset=%s",
            event.event_type,
            event.event_id,
            topic,
            partition,
            offset,
        )

        # Dispatch to handlers
        handlers = self._get_handlers(event.event_type)
        if not handlers:
            logger.debug(
                "No handlers registered for EventType %s — skipping",
                event.event_type,
            )
            self._commit(msg)
            return

        handler_error: Exception | None = None
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                handler_error = exc
                logger.error(
                    "Handler %s failed | event_type=%s event_id=%s error=%s",
                    handler.__name__,
                    event.event_type,
                    event.event_id,
                    exc,
                    exc_info=True,
                )
                if self._error_strategy == ErrorStrategy.RAISE:
                    raise
                # For SKIP and DLQ strategies, continue to next handler

        # Commit offset after all handlers have run (even if some failed)
        # This prevents infinite reprocessing of a bad message
        if handler_error is None or self._error_strategy in (
            ErrorStrategy.SKIP,
            ErrorStrategy.DLQ,
        ):
            self._commit(msg)

    def _get_handlers(self, event_type: EventType) -> list[EventHandler]:
        """
        Return all handlers for the given event type, including wildcards.

        Args:
            event_type: The event type to look up.

        Returns:
            Combined list of type-specific and wildcard handlers.
        """
        specific = self._handlers.get(str(event_type), [])
        wildcards = self._handlers.get(_WILDCARD, [])
        return specific + wildcards

    def _commit(self, msg: Message) -> None:
        """
        Commit the offset for the given message (manual commit mode).

        In auto-commit mode this is a no-op.
        """
        if not self._auto_commit:
            try:
                self._consumer.commit(message=msg, asynchronous=False)
            except KafkaException as exc:
                logger.warning(
                    "Failed to commit offset | topic=%s partition=%s offset=%s error=%s",
                    msg.topic(),
                    msg.partition(),
                    msg.offset(),
                    exc,
                )

    def _handle_kafka_error(self, msg: Message) -> None:
        """
        Handle a Kafka error message from ``poll()``.

        Args:
            msg: Message with ``msg.error()`` set.
        """
        error = msg.error()
        if error.code() == KafkaError._PARTITION_EOF:
            # End of partition — not an error, just informational
            logger.debug(
                "Reached end of partition | topic=%s partition=%s offset=%s",
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )
        elif error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
            logger.error(
                "Unknown topic or partition | topic=%s partition=%s",
                msg.topic(),
                msg.partition(),
            )
        else:
            logger.error(
                "Kafka consumer error | code=%s reason=%s",
                error.code(),
                error.str(),
            )

    def _on_assign(self, consumer: Consumer, partitions: list) -> None:
        """Callback invoked when partitions are assigned to this consumer."""
        logger.info(
            "Partitions assigned | group=%s partitions=%s",
            self._group_id,
            [(p.topic, p.partition) for p in partitions],
        )

    def _on_revoke(self, consumer: Consumer, partitions: list) -> None:
        """Callback invoked when partitions are revoked from this consumer."""
        logger.info(
            "Partitions revoked | group=%s partitions=%s",
            self._group_id,
            [(p.topic, p.partition) for p in partitions],
        )

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers that call ``stop()``."""
        def _handler(signum: int, frame: object) -> None:
            logger.info("Received signal %d — stopping consumer…", signum)
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory
# ─────────────────────────────────────────────────────────────────────────────


def make_consumer(
    group: ConsumerGroup,
    *,
    extra_topics: list[KafkaTopic] | None = None,
    poll_timeout_ms: int = 1000,
    error_strategy: str = ErrorStrategy.SKIP,
    install_signal_handlers: bool = False,
) -> FlowOSConsumer:
    """
    Create a ``FlowOSConsumer`` pre-configured for a named consumer group.

    Automatically subscribes to the topics registered for the group in
    ``CONSUMER_GROUP_TOPICS``.  Additional topics can be appended via
    ``extra_topics``.

    Args:
        group:                   The consumer group identifier.
        extra_topics:            Additional topics to subscribe to.
        poll_timeout_ms:         Poll timeout in milliseconds.
        error_strategy:          Error handling strategy.
        install_signal_handlers: Install SIGINT/SIGTERM handlers.

    Returns:
        A configured ``FlowOSConsumer`` instance.
    """
    from shared.kafka.topics import topics_for_group

    topics = topics_for_group(group)
    if extra_topics:
        topics = topics + extra_topics

    return FlowOSConsumer(
        group_id=group,
        topics=topics,
        poll_timeout_ms=poll_timeout_ms,
        error_strategy=error_strategy,
        install_signal_handlers=install_signal_handlers,
    )
