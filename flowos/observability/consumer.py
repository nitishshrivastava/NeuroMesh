"""
observability/consumer.py — FlowOS Observability Kafka Consumer

The observability consumer subscribes to ALL FlowOS Kafka topics and performs
two duties for every event it receives:

1. **Metrics update** — increments the appropriate Prometheus counter/gauge/
   histogram defined in ``observability.metrics``.
2. **Audit log write** — persists the event to the ``event_records`` PostgreSQL
   table via ``observability.audit_log.AuditLogWriter``.

The consumer runs in the ``flowos-observability`` consumer group so it receives
a copy of every event without interfering with other consumer groups.

Architecture:
    - Uses ``shared.kafka.consumer.FlowOSConsumer`` (handler-based dispatch).
    - Registers a wildcard handler (``on_all``) that routes each event to the
      appropriate metric updater and the audit log writer.
    - Runs the metrics HTTP server (``/metrics``) in a background thread so
      Prometheus can scrape it while the consumer loop runs in the main thread.
    - Graceful shutdown via SIGINT/SIGTERM: flushes the audit log buffer,
      closes the Kafka consumer, and stops the metrics server.

Usage::

    # Run the observability service directly:
    python -m observability.consumer

    # Or import and control programmatically:
    from observability.consumer import ObservabilityConsumer

    obs = ObservabilityConsumer()
    obs.start()          # starts metrics server + consumer loop (blocks)
    # ... or in a thread:
    import threading
    t = threading.Thread(target=obs.start, daemon=True)
    t.start()
    obs.stop()
    t.join()
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

import uvicorn

from shared.kafka.consumer import FlowOSConsumer
from shared.kafka.topics import ConsumerGroup, KafkaTopic, CONSUMER_GROUP_TOPICS
from shared.models.event import EventRecord, EventSeverity, EventTopic, EventType

from observability.audit_log import AuditLogWriter, get_audit_log_writer
from observability.metrics import (
    # Event bus
    record_event_consumed,
    record_processing_error,
    CONSUMER_LAG,
    # Workflow
    WORKFLOW_EVENTS_TOTAL,
    WORKFLOWS_ACTIVE,
    WORKFLOW_DURATION_SECONDS,
    WORKFLOWS_COMPLETED_TOTAL,
    # Task
    TASK_EVENTS_TOTAL,
    TASKS_ACTIVE,
    TASK_DURATION_SECONDS,
    TASK_HANDOFFS_TOTAL,
    TASK_CHECKPOINTS_TOTAL,
    TASK_REVERTS_TOTAL,
    # Agent
    AGENT_EVENTS_TOTAL,
    AGENTS_BY_STATUS,
    AGENTS_REGISTERED_TOTAL,
    # Build
    BUILD_EVENTS_TOTAL,
    BUILD_OUTCOMES_TOTAL,
    TEST_OUTCOMES_TOTAL,
    ARTIFACTS_UPLOADED_TOTAL,
    # AI
    AI_EVENTS_TOTAL,
    AI_SUGGESTIONS_TOTAL,
    AI_PATCHES_TOTAL,
    AI_TOOL_CALLS_TOTAL,
    AI_REASONING_TRACES_TOTAL,
    AI_TOKENS_USED,
    # Workspace
    WORKSPACE_EVENTS_TOTAL,
    CHECKPOINTS_CREATED_TOTAL,
    CHECKPOINTS_REVERTED_TOTAL,
    BRANCHES_CREATED_TOTAL,
    # Policy
    POLICY_EVENTS_TOTAL,
    POLICY_VIOLATIONS_TOTAL,
    APPROVAL_EVENTS_TOTAL,
    # Observability
    OBSERVABILITY_EVENTS_TOTAL,
    SYSTEM_ALERTS_TOTAL,
    HEALTH_CHECK_RESULTS_TOTAL,
    create_metrics_app,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Metric dispatch helpers
# ─────────────────────────────────────────────────────────────────────────────


def _update_workflow_metrics(event: EventRecord) -> None:
    """Update workflow-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    WORKFLOW_EVENTS_TOTAL.labels(event_type=et).inc()

    payload = event.payload or {}

    if event.event_type == EventType.WORKFLOW_STARTED:
        WORKFLOWS_ACTIVE.labels(status="running").inc()
        WORKFLOWS_ACTIVE.labels(status="pending").dec()

    elif event.event_type == EventType.WORKFLOW_PAUSED:
        WORKFLOWS_ACTIVE.labels(status="paused").inc()
        WORKFLOWS_ACTIVE.labels(status="running").dec()

    elif event.event_type == EventType.WORKFLOW_RESUMED:
        WORKFLOWS_ACTIVE.labels(status="running").inc()
        WORKFLOWS_ACTIVE.labels(status="paused").dec()

    elif event.event_type == EventType.WORKFLOW_CREATED:
        WORKFLOWS_ACTIVE.labels(status="pending").inc()

    elif event.event_type in (
        EventType.WORKFLOW_COMPLETED,
        EventType.WORKFLOW_FAILED,
        EventType.WORKFLOW_CANCELLED,
    ):
        terminal_status = et.replace("WORKFLOW_", "").lower()
        WORKFLOWS_COMPLETED_TOTAL.labels(terminal_status=terminal_status).inc()
        WORKFLOWS_ACTIVE.labels(status="running").dec()

        # Record duration if available in payload
        duration_seconds = payload.get("duration_seconds")
        if duration_seconds is not None:
            try:
                WORKFLOW_DURATION_SECONDS.labels(status=terminal_status).observe(
                    float(duration_seconds)
                )
            except (TypeError, ValueError):
                pass


def _update_task_metrics(event: EventRecord) -> None:
    """Update task-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    payload = event.payload or {}
    task_type = payload.get("task_type", "unknown")

    TASK_EVENTS_TOTAL.labels(event_type=et, task_type=task_type).inc()

    if event.event_type == EventType.TASK_CREATED:
        TASKS_ACTIVE.labels(status="pending", task_type=task_type).inc()

    elif event.event_type == EventType.TASK_ASSIGNED:
        TASKS_ACTIVE.labels(status="assigned", task_type=task_type).inc()
        TASKS_ACTIVE.labels(status="pending", task_type=task_type).dec()

    elif event.event_type == EventType.TASK_ACCEPTED:
        TASKS_ACTIVE.labels(status="accepted", task_type=task_type).inc()
        TASKS_ACTIVE.labels(status="assigned", task_type=task_type).dec()

    elif event.event_type == EventType.TASK_STARTED:
        TASKS_ACTIVE.labels(status="in_progress", task_type=task_type).inc()
        TASKS_ACTIVE.labels(status="accepted", task_type=task_type).dec()

    elif event.event_type == EventType.TASK_CHECKPOINTED:
        TASK_CHECKPOINTS_TOTAL.labels(event_type=et).inc()

    elif event.event_type == EventType.TASK_HANDOFF_REQUESTED:
        TASK_HANDOFFS_TOTAL.labels(event_type=et).inc()

    elif event.event_type == EventType.TASK_HANDOFF_ACCEPTED:
        TASK_HANDOFFS_TOTAL.labels(event_type=et).inc()

    elif event.event_type == EventType.TASK_REVERT_REQUESTED:
        TASK_REVERTS_TOTAL.inc()

    elif event.event_type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
        terminal_status = et.replace("TASK_", "").lower()
        TASKS_ACTIVE.labels(status="in_progress", task_type=task_type).dec()

        duration_seconds = payload.get("duration_seconds")
        if duration_seconds is not None:
            try:
                TASK_DURATION_SECONDS.labels(
                    status=terminal_status, task_type=task_type
                ).observe(float(duration_seconds))
            except (TypeError, ValueError):
                pass


def _update_agent_metrics(event: EventRecord) -> None:
    """Update agent-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    payload = event.payload or {}
    agent_type = payload.get("agent_type", "unknown")

    AGENT_EVENTS_TOTAL.labels(event_type=et, agent_type=agent_type).inc()

    if event.event_type == EventType.AGENT_REGISTERED:
        AGENTS_REGISTERED_TOTAL.labels(agent_type=agent_type).inc()
        AGENTS_BY_STATUS.labels(status="offline", agent_type=agent_type).inc()

    elif event.event_type == EventType.AGENT_ONLINE:
        AGENTS_BY_STATUS.labels(status="online", agent_type=agent_type).inc()
        AGENTS_BY_STATUS.labels(status="offline", agent_type=agent_type).dec()

    elif event.event_type == EventType.AGENT_OFFLINE:
        AGENTS_BY_STATUS.labels(status="offline", agent_type=agent_type).inc()
        AGENTS_BY_STATUS.labels(status="online", agent_type=agent_type).dec()

    elif event.event_type == EventType.AGENT_BUSY:
        AGENTS_BY_STATUS.labels(status="busy", agent_type=agent_type).inc()
        AGENTS_BY_STATUS.labels(status="idle", agent_type=agent_type).dec()

    elif event.event_type == EventType.AGENT_IDLE:
        AGENTS_BY_STATUS.labels(status="idle", agent_type=agent_type).inc()
        AGENTS_BY_STATUS.labels(status="busy", agent_type=agent_type).dec()


def _update_build_metrics(event: EventRecord) -> None:
    """Update build/test-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    BUILD_EVENTS_TOTAL.labels(event_type=et).inc()

    if event.event_type == EventType.BUILD_SUCCEEDED:
        BUILD_OUTCOMES_TOTAL.labels(result="succeeded").inc()
    elif event.event_type == EventType.BUILD_FAILED:
        BUILD_OUTCOMES_TOTAL.labels(result="failed").inc()
    elif event.event_type == EventType.TEST_PASSED:
        TEST_OUTCOMES_TOTAL.labels(result="passed").inc()
    elif event.event_type == EventType.TEST_FAILED:
        TEST_OUTCOMES_TOTAL.labels(result="failed").inc()
    elif event.event_type == EventType.ARTIFACT_UPLOADED:
        ARTIFACTS_UPLOADED_TOTAL.inc()


def _update_ai_metrics(event: EventRecord) -> None:
    """Update AI reasoning-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    payload = event.payload or {}

    AI_EVENTS_TOTAL.labels(event_type=et).inc()

    if event.event_type == EventType.AI_SUGGESTION_CREATED:
        suggestion_type = payload.get("suggestion_type", "unknown")
        AI_SUGGESTIONS_TOTAL.labels(suggestion_type=suggestion_type).inc()

    elif event.event_type == EventType.AI_PATCH_PROPOSED:
        AI_PATCHES_TOTAL.inc()

    elif event.event_type == EventType.AI_TOOL_CALLED:
        tool_name = payload.get("tool_name", "unknown")
        AI_TOOL_CALLS_TOTAL.labels(tool_name=tool_name).inc()

    elif event.event_type == EventType.AI_REASONING_TRACE:
        AI_REASONING_TRACES_TOTAL.inc()

    # Record token usage if present in payload
    prompt_tokens = payload.get("prompt_tokens")
    completion_tokens = payload.get("completion_tokens")
    total_tokens = payload.get("total_tokens")

    if prompt_tokens is not None:
        try:
            AI_TOKENS_USED.labels(token_type="prompt").observe(float(prompt_tokens))
        except (TypeError, ValueError):
            pass
    if completion_tokens is not None:
        try:
            AI_TOKENS_USED.labels(token_type="completion").observe(
                float(completion_tokens)
            )
        except (TypeError, ValueError):
            pass
    if total_tokens is not None:
        try:
            AI_TOKENS_USED.labels(token_type="total").observe(float(total_tokens))
        except (TypeError, ValueError):
            pass


def _update_workspace_metrics(event: EventRecord) -> None:
    """Update workspace/Git-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    WORKSPACE_EVENTS_TOTAL.labels(event_type=et).inc()

    if event.event_type == EventType.CHECKPOINT_CREATED:
        CHECKPOINTS_CREATED_TOTAL.inc()
    elif event.event_type == EventType.CHECKPOINT_REVERTED:
        CHECKPOINTS_REVERTED_TOTAL.inc()
    elif event.event_type == EventType.BRANCH_CREATED:
        BRANCHES_CREATED_TOTAL.inc()


def _update_policy_metrics(event: EventRecord) -> None:
    """Update policy engine-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    payload = event.payload or {}

    POLICY_EVENTS_TOTAL.labels(event_type=et).inc()

    if event.event_type == EventType.POLICY_VIOLATION_DETECTED:
        policy_name = payload.get("policy_name", "unknown")
        POLICY_VIOLATIONS_TOTAL.labels(policy_name=policy_name).inc()

    elif event.event_type == EventType.APPROVAL_REQUESTED:
        APPROVAL_EVENTS_TOTAL.labels(outcome="requested").inc()
    elif event.event_type == EventType.APPROVAL_GRANTED:
        APPROVAL_EVENTS_TOTAL.labels(outcome="granted").inc()
    elif event.event_type == EventType.APPROVAL_DENIED:
        APPROVAL_EVENTS_TOTAL.labels(outcome="denied").inc()


def _update_observability_metrics(event: EventRecord) -> None:
    """Update observability-related Prometheus metrics for the given event."""
    et = str(event.event_type)
    payload = event.payload or {}

    OBSERVABILITY_EVENTS_TOTAL.labels(event_type=et).inc()

    if event.event_type == EventType.SYSTEM_ALERT:
        severity = payload.get("severity", "unknown")
        SYSTEM_ALERTS_TOTAL.labels(severity=severity).inc()

    elif event.event_type == EventType.HEALTH_CHECK_RESULT:
        status = payload.get("status", "unknown")
        component = payload.get("component", "unknown")
        HEALTH_CHECK_RESULTS_TOTAL.labels(status=status, component=component).inc()


# ─────────────────────────────────────────────────────────────────────────────
# Topic → metric dispatcher
# ─────────────────────────────────────────────────────────────────────────────

#: Maps Kafka topic strings to their metric update function.
_TOPIC_METRIC_HANDLERS: dict[str, Any] = {
    str(KafkaTopic.WORKFLOW_EVENTS): _update_workflow_metrics,
    str(KafkaTopic.TASK_EVENTS): _update_task_metrics,
    str(KafkaTopic.AGENT_EVENTS): _update_agent_metrics,
    str(KafkaTopic.BUILD_EVENTS): _update_build_metrics,
    str(KafkaTopic.AI_EVENTS): _update_ai_metrics,
    str(KafkaTopic.WORKSPACE_EVENTS): _update_workspace_metrics,
    str(KafkaTopic.POLICY_EVENTS): _update_policy_metrics,
    str(KafkaTopic.OBSERVABILITY_EVENTS): _update_observability_metrics,
}


# ─────────────────────────────────────────────────────────────────────────────
# Observability Consumer
# ─────────────────────────────────────────────────────────────────────────────


class ObservabilityConsumer:
    """
    Kafka consumer for the FlowOS observability layer.

    Subscribes to all FlowOS topics, updates Prometheus metrics, and writes
    every event to the audit log database.

    Lifecycle:
        obs = ObservabilityConsumer()
        obs.start()   # blocks until stop() is called or SIGINT/SIGTERM received

    Attributes:
        metrics_host:  Host for the Prometheus metrics HTTP server.
        metrics_port:  Port for the Prometheus metrics HTTP server.
        audit_batch:   Number of events to buffer before flushing to the DB.
    """

    def __init__(
        self,
        *,
        metrics_host: str = "0.0.0.0",
        metrics_port: int = 9090,
        audit_batch_size: int = 1,
        poll_timeout_ms: int = 1000,
    ) -> None:
        """
        Initialise the observability consumer.

        Args:
            metrics_host:     Host to bind the Prometheus HTTP server to.
            metrics_port:     Port to bind the Prometheus HTTP server to.
            audit_batch_size: Number of events to buffer before flushing to DB.
                              Set to 1 for immediate writes (default).
            poll_timeout_ms:  Kafka poll timeout in milliseconds.
        """
        self.metrics_host = metrics_host
        self.metrics_port = metrics_port
        self.audit_batch_size = audit_batch_size
        self.poll_timeout_ms = poll_timeout_ms

        # All topics the observability group subscribes to
        self._topics: list[KafkaTopic] = CONSUMER_GROUP_TOPICS[ConsumerGroup.OBSERVABILITY]

        # Kafka consumer
        self._consumer = FlowOSConsumer(
            group_id=ConsumerGroup.OBSERVABILITY,
            topics=self._topics,
            poll_timeout_ms=poll_timeout_ms,
            error_strategy="skip",
            auto_commit=False,
        )

        # Audit log writer
        self._audit_writer = get_audit_log_writer(batch_size=audit_batch_size)

        # Metrics HTTP server (uvicorn in a background thread)
        self._metrics_app = create_metrics_app()
        self._metrics_server: uvicorn.Server | None = None
        self._metrics_thread: threading.Thread | None = None

        # Shutdown coordination
        self._stop_event = threading.Event()

        # Register the wildcard handler
        self._consumer.register_wildcard(self._handle_event)

        logger.info(
            "ObservabilityConsumer initialised | topics=%s metrics=%s:%d",
            [str(t) for t in self._topics],
            metrics_host,
            metrics_port,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the observability consumer.

        1. Starts the Prometheus metrics HTTP server in a background thread.
        2. Installs SIGINT/SIGTERM handlers for graceful shutdown.
        3. Runs the Kafka consumer loop in the calling thread (blocks).

        This method blocks until ``stop()`` is called or a signal is received.
        """
        self._install_signal_handlers()
        self._start_metrics_server()

        logger.info(
            "ObservabilityConsumer starting | metrics=http://%s:%d/metrics",
            self.metrics_host,
            self.metrics_port,
        )

        try:
            self._consumer.run()
        finally:
            self._shutdown()

    def stop(self) -> None:
        """
        Signal the consumer to stop gracefully.

        Stops the Kafka consumer loop and the metrics HTTP server.
        """
        logger.info("ObservabilityConsumer stop requested.")
        self._stop_event.set()
        self._consumer.stop()

    # ─────────────────────────────────────────────────────────────────────────
    # Event handler
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_event(self, event: EventRecord) -> None:
        """
        Wildcard handler: process every event from every topic.

        1. Routes the event to the appropriate metric update function.
        2. Writes the event to the audit log.

        Args:
            event: The deserialized ``EventRecord`` from Kafka.
        """
        start = time.monotonic()
        topic_str = str(event.topic)

        try:
            # 1. Update Prometheus metrics
            metric_handler = _TOPIC_METRIC_HANDLERS.get(topic_str)
            if metric_handler is not None:
                metric_handler(event)
            else:
                logger.warning(
                    "No metric handler for topic %r | event_type=%s",
                    topic_str,
                    event.event_type,
                )

            # 2. Write to audit log
            if self.audit_batch_size <= 1:
                self._audit_writer.write(event)
            else:
                self._audit_writer.buffer(event)

            # 3. Record consumption metrics
            latency = time.monotonic() - start
            record_event_consumed(
                topic=topic_str,
                event_type=str(event.event_type),
                latency_seconds=latency,
            )

            logger.debug(
                "Processed event | event_type=%s topic=%s event_id=%s latency=%.3fs",
                event.event_type,
                topic_str,
                event.event_id,
                latency,
            )

        except Exception as exc:
            error_type = type(exc).__name__
            record_processing_error(topic=topic_str, error_type=error_type)
            logger.error(
                "Error processing event | event_type=%s topic=%s event_id=%s error=%s: %s",
                event.event_type,
                topic_str,
                event.event_id,
                error_type,
                exc,
                exc_info=True,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Metrics server
    # ─────────────────────────────────────────────────────────────────────────

    def _start_metrics_server(self) -> None:
        """Start the Prometheus metrics HTTP server in a background thread."""
        config = uvicorn.Config(
            app=self._metrics_app,
            host=self.metrics_host,
            port=self.metrics_port,
            log_level="warning",
            access_log=False,
        )
        self._metrics_server = uvicorn.Server(config)

        def _run_server() -> None:
            try:
                self._metrics_server.run()  # type: ignore[union-attr]
            except Exception as exc:
                logger.error("Metrics server error: %s", exc)

        self._metrics_thread = threading.Thread(
            target=_run_server,
            name="metrics-server",
            daemon=True,
        )
        self._metrics_thread.start()
        logger.info(
            "Metrics HTTP server started | http://%s:%d/metrics",
            self.metrics_host,
            self.metrics_port,
        )

    def _stop_metrics_server(self) -> None:
        """Stop the Prometheus metrics HTTP server."""
        if self._metrics_server is not None:
            self._metrics_server.should_exit = True
        if self._metrics_thread is not None:
            self._metrics_thread.join(timeout=5.0)
        logger.info("Metrics HTTP server stopped.")

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        """Perform graceful shutdown: flush audit buffer and stop metrics server."""
        logger.info("ObservabilityConsumer shutting down…")

        # Flush any buffered audit log entries
        written, skipped = self._audit_writer.flush_buffer()
        if written or skipped:
            logger.info(
                "Audit log buffer flushed on shutdown | written=%d skipped=%d",
                written,
                skipped,
            )

        # Stop the metrics HTTP server
        self._stop_metrics_server()

        logger.info("ObservabilityConsumer shutdown complete.")

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for graceful shutdown."""

        def _handler(signum: int, frame: object) -> None:
            logger.info(
                "ObservabilityConsumer received signal %d — stopping…", signum
            )
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)


# ─────────────────────────────────────────────────────────────────────────────
# Module entrypoint
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """
    Entrypoint for running the observability consumer as a standalone service.

    Reads configuration from environment variables via ``shared.config.settings``.
    Starts the Kafka consumer loop and the Prometheus metrics HTTP server.

    Environment variables:
        OBSERVABILITY_METRICS_HOST  — Host for the metrics server (default: 0.0.0.0)
        OBSERVABILITY_METRICS_PORT  — Port for the metrics server (default: 9090)
        OBSERVABILITY_AUDIT_BATCH   — Audit log batch size (default: 1)
    """
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    metrics_host = os.environ.get("OBSERVABILITY_METRICS_HOST", "0.0.0.0")
    metrics_port = int(os.environ.get("OBSERVABILITY_METRICS_PORT", "9090"))
    audit_batch = int(os.environ.get("OBSERVABILITY_AUDIT_BATCH", "1"))

    logger.info(
        "Starting FlowOS Observability Consumer | metrics=%s:%d audit_batch=%d",
        metrics_host,
        metrics_port,
        audit_batch,
    )

    consumer = ObservabilityConsumer(
        metrics_host=metrics_host,
        metrics_port=metrics_port,
        audit_batch_size=audit_batch,
    )
    consumer.start()


if __name__ == "__main__":
    main()
