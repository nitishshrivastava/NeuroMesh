"""
observability/metrics.py — FlowOS Prometheus Metrics Registry

Defines all Prometheus metric instruments for the FlowOS observability layer.
Exposes a ``/metrics`` HTTP endpoint via a lightweight FastAPI application
that can be mounted standalone or embedded in the main API.

Metric categories:
    - Event bus throughput (events consumed per topic/type)
    - Workflow lifecycle counters and duration histograms
    - Task lifecycle counters and duration histograms
    - Agent status gauges
    - Build/test result counters
    - AI reasoning counters and token usage
    - Policy evaluation counters
    - Consumer lag and processing latency
    - System health gauges

Design:
    All metrics are registered in a single ``CollectorRegistry`` instance
    (``REGISTRY``) so they can be shared across the consumer and the HTTP
    endpoint without duplication.  The ``/metrics`` endpoint is served by a
    minimal FastAPI app (``create_metrics_app()``) that returns the
    Prometheus text exposition format.

Usage::

    # In the observability service entrypoint:
    from observability.metrics import create_metrics_app, REGISTRY
    import uvicorn

    app = create_metrics_app()
    uvicorn.run(app, host="0.0.0.0", port=9090)

    # Incrementing a counter from the consumer:
    from observability.metrics import (
        EVENTS_CONSUMED_TOTAL,
        WORKFLOW_EVENTS_TOTAL,
    )
    EVENTS_CONSUMED_TOTAL.labels(topic="flowos.workflow.events", event_type="WORKFLOW_CREATED").inc()
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Summary,
        generate_latest,
        CONTENT_TYPE_LATEST,
        REGISTRY as DEFAULT_REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

# Use the default Prometheus registry so that process/platform collectors
# (CPU, memory, GC) are automatically included.
if _PROMETHEUS_AVAILABLE:
    REGISTRY = DEFAULT_REGISTRY
else:
    REGISTRY = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Fallback stubs when prometheus_client is not installed
# ─────────────────────────────────────────────────────────────────────────────

class _NoOpMetric:
    """No-op metric stub used when prometheus_client is unavailable."""

    def labels(self, **kwargs: Any) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass

    def time(self) -> Any:
        import contextlib

        @contextlib.contextmanager
        def _ctx():  # type: ignore[return]
            yield

        return _ctx()


def _make_counter(name: str, documentation: str, labelnames: list[str]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        return Counter(name, documentation, labelnames)
    return _NoOpMetric()


def _make_gauge(name: str, documentation: str, labelnames: list[str]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        return Gauge(name, documentation, labelnames)
    return _NoOpMetric()


def _make_histogram(
    name: str,
    documentation: str,
    labelnames: list[str],
    buckets: tuple[float, ...] | None = None,
) -> Any:
    if _PROMETHEUS_AVAILABLE:
        kwargs: dict[str, Any] = {}
        if buckets is not None:
            kwargs["buckets"] = buckets
        return Histogram(name, documentation, labelnames, **kwargs)
    return _NoOpMetric()


def _make_summary(name: str, documentation: str, labelnames: list[str]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        return Summary(name, documentation, labelnames)
    return _NoOpMetric()


# ─────────────────────────────────────────────────────────────────────────────
# Event Bus Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total number of Kafka events consumed, labelled by topic and event_type.
EVENTS_CONSUMED_TOTAL = _make_counter(
    name="flowos_events_consumed_total",
    documentation=(
        "Total number of Kafka events consumed by the observability consumer, "
        "partitioned by topic and event_type."
    ),
    labelnames=["topic", "event_type"],
)

#: Total number of events that failed processing (deserialization or handler error).
EVENTS_PROCESSING_ERRORS_TOTAL = _make_counter(
    name="flowos_events_processing_errors_total",
    documentation=(
        "Total number of events that encountered a processing error "
        "(deserialization failure or handler exception)."
    ),
    labelnames=["topic", "error_type"],
)

#: Histogram of event processing latency in seconds.
EVENT_PROCESSING_LATENCY_SECONDS = _make_histogram(
    name="flowos_event_processing_latency_seconds",
    documentation="Latency of processing a single Kafka event in the observability consumer.",
    labelnames=["topic", "event_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

#: Current consumer lag (number of unconsumed messages) per topic-partition.
CONSUMER_LAG = _make_gauge(
    name="flowos_consumer_lag",
    documentation="Current consumer lag (unconsumed messages) per topic.",
    labelnames=["topic"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total workflow lifecycle events, labelled by event_type.
WORKFLOW_EVENTS_TOTAL = _make_counter(
    name="flowos_workflow_events_total",
    documentation="Total workflow lifecycle events by event_type.",
    labelnames=["event_type"],
)

#: Number of workflows currently in each status.
WORKFLOWS_ACTIVE = _make_gauge(
    name="flowos_workflows_active",
    documentation="Number of workflows currently in each lifecycle status.",
    labelnames=["status"],
)

#: Histogram of workflow duration from STARTED to terminal state (seconds).
WORKFLOW_DURATION_SECONDS = _make_histogram(
    name="flowos_workflow_duration_seconds",
    documentation="Duration of completed/failed/cancelled workflows in seconds.",
    labelnames=["status"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)

#: Total workflows completed successfully.
WORKFLOWS_COMPLETED_TOTAL = _make_counter(
    name="flowos_workflows_completed_total",
    documentation="Total workflows that reached a terminal state.",
    labelnames=["terminal_status"],  # completed, failed, cancelled
)


# ─────────────────────────────────────────────────────────────────────────────
# Task Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total task lifecycle events, labelled by event_type and task_type.
TASK_EVENTS_TOTAL = _make_counter(
    name="flowos_task_events_total",
    documentation="Total task lifecycle events by event_type and task_type.",
    labelnames=["event_type", "task_type"],
)

#: Number of tasks currently in each status.
TASKS_ACTIVE = _make_gauge(
    name="flowos_tasks_active",
    documentation="Number of tasks currently in each lifecycle status.",
    labelnames=["status", "task_type"],
)

#: Histogram of task duration from STARTED to terminal state (seconds).
TASK_DURATION_SECONDS = _make_histogram(
    name="flowos_task_duration_seconds",
    documentation="Duration of completed/failed tasks in seconds.",
    labelnames=["status", "task_type"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)

#: Total task handoffs requested and accepted.
TASK_HANDOFFS_TOTAL = _make_counter(
    name="flowos_task_handoffs_total",
    documentation="Total task handoff events (requested and accepted).",
    labelnames=["event_type"],
)

#: Total task checkpoints created.
TASK_CHECKPOINTS_TOTAL = _make_counter(
    name="flowos_task_checkpoints_total",
    documentation="Total task checkpoint events.",
    labelnames=["event_type"],
)

#: Total task revert requests.
TASK_REVERTS_TOTAL = _make_counter(
    name="flowos_task_reverts_total",
    documentation="Total task revert requests.",
    labelnames=[],
)


# ─────────────────────────────────────────────────────────────────────────────
# Agent Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total agent lifecycle events.
AGENT_EVENTS_TOTAL = _make_counter(
    name="flowos_agent_events_total",
    documentation="Total agent lifecycle events by event_type and agent_type.",
    labelnames=["event_type", "agent_type"],
)

#: Number of agents currently in each status.
AGENTS_BY_STATUS = _make_gauge(
    name="flowos_agents_by_status",
    documentation="Number of agents currently in each status (online, offline, busy, idle).",
    labelnames=["status", "agent_type"],
)

#: Total agents registered.
AGENTS_REGISTERED_TOTAL = _make_counter(
    name="flowos_agents_registered_total",
    documentation="Total agent registration events.",
    labelnames=["agent_type"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Build / Test Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total build events by event_type.
BUILD_EVENTS_TOTAL = _make_counter(
    name="flowos_build_events_total",
    documentation="Total build and test lifecycle events by event_type.",
    labelnames=["event_type"],
)

#: Total build outcomes (succeeded / failed).
BUILD_OUTCOMES_TOTAL = _make_counter(
    name="flowos_build_outcomes_total",
    documentation="Total build outcomes partitioned by result (succeeded/failed).",
    labelnames=["result"],
)

#: Total test outcomes (passed / failed).
TEST_OUTCOMES_TOTAL = _make_counter(
    name="flowos_test_outcomes_total",
    documentation="Total test outcomes partitioned by result (passed/failed).",
    labelnames=["result"],
)

#: Total artifacts uploaded.
ARTIFACTS_UPLOADED_TOTAL = _make_counter(
    name="flowos_artifacts_uploaded_total",
    documentation="Total artifact upload events.",
    labelnames=[],
)


# ─────────────────────────────────────────────────────────────────────────────
# AI Reasoning Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total AI events by event_type.
AI_EVENTS_TOTAL = _make_counter(
    name="flowos_ai_events_total",
    documentation="Total AI reasoning events by event_type.",
    labelnames=["event_type"],
)

#: Total AI suggestions created.
AI_SUGGESTIONS_TOTAL = _make_counter(
    name="flowos_ai_suggestions_total",
    documentation="Total AI suggestions created.",
    labelnames=["suggestion_type"],
)

#: Total AI patches proposed.
AI_PATCHES_TOTAL = _make_counter(
    name="flowos_ai_patches_total",
    documentation="Total AI patches proposed.",
    labelnames=[],
)

#: Total AI tool calls made.
AI_TOOL_CALLS_TOTAL = _make_counter(
    name="flowos_ai_tool_calls_total",
    documentation="Total AI tool/function calls made by AI agents.",
    labelnames=["tool_name"],
)

#: Total AI reasoning traces recorded.
AI_REASONING_TRACES_TOTAL = _make_counter(
    name="flowos_ai_reasoning_traces_total",
    documentation="Total AI reasoning trace events.",
    labelnames=[],
)

#: Histogram of AI token usage per event.
AI_TOKENS_USED = _make_histogram(
    name="flowos_ai_tokens_used",
    documentation="Token usage per AI event (prompt + completion tokens).",
    labelnames=["token_type"],  # prompt, completion, total
    buckets=(10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000),
)


# ─────────────────────────────────────────────────────────────────────────────
# Workspace / Git Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total workspace events by event_type.
WORKSPACE_EVENTS_TOTAL = _make_counter(
    name="flowos_workspace_events_total",
    documentation="Total workspace and Git state events by event_type.",
    labelnames=["event_type"],
)

#: Total checkpoints created.
CHECKPOINTS_CREATED_TOTAL = _make_counter(
    name="flowos_checkpoints_created_total",
    documentation="Total checkpoint creation events.",
    labelnames=[],
)

#: Total checkpoint reverts.
CHECKPOINTS_REVERTED_TOTAL = _make_counter(
    name="flowos_checkpoints_reverted_total",
    documentation="Total checkpoint revert events.",
    labelnames=[],
)

#: Total branches created.
BRANCHES_CREATED_TOTAL = _make_counter(
    name="flowos_branches_created_total",
    documentation="Total branch creation events.",
    labelnames=[],
)


# ─────────────────────────────────────────────────────────────────────────────
# Policy Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total policy events by event_type.
POLICY_EVENTS_TOTAL = _make_counter(
    name="flowos_policy_events_total",
    documentation="Total policy engine events by event_type.",
    labelnames=["event_type"],
)

#: Total policy violations detected.
POLICY_VIOLATIONS_TOTAL = _make_counter(
    name="flowos_policy_violations_total",
    documentation="Total policy violations detected.",
    labelnames=["policy_name"],
)

#: Total approval requests and outcomes.
APPROVAL_EVENTS_TOTAL = _make_counter(
    name="flowos_approval_events_total",
    documentation="Total approval events (requested, granted, denied).",
    labelnames=["outcome"],  # requested, granted, denied
)


# ─────────────────────────────────────────────────────────────────────────────
# Observability / System Metrics
# ─────────────────────────────────────────────────────────────────────────────

#: Total observability events by event_type.
OBSERVABILITY_EVENTS_TOTAL = _make_counter(
    name="flowos_observability_events_total",
    documentation="Total observability events by event_type.",
    labelnames=["event_type"],
)

#: Total system alerts by severity.
SYSTEM_ALERTS_TOTAL = _make_counter(
    name="flowos_system_alerts_total",
    documentation="Total system alert events by severity.",
    labelnames=["severity"],
)

#: Total health check results by status.
HEALTH_CHECK_RESULTS_TOTAL = _make_counter(
    name="flowos_health_check_results_total",
    documentation="Total health check result events by status.",
    labelnames=["status", "component"],
)

#: Gauge tracking the last time the observability consumer processed an event.
LAST_EVENT_PROCESSED_TIMESTAMP = _make_gauge(
    name="flowos_last_event_processed_timestamp_seconds",
    documentation="Unix timestamp of the last event processed by the observability consumer.",
    labelnames=["topic"],
)

#: Total audit log entries written to the database.
AUDIT_LOG_WRITES_TOTAL = _make_counter(
    name="flowos_audit_log_writes_total",
    documentation="Total audit log entries written to the database.",
    labelnames=["status"],  # success, failure
)

#: Histogram of audit log write latency.
AUDIT_LOG_WRITE_LATENCY_SECONDS = _make_histogram(
    name="flowos_audit_log_write_latency_seconds",
    documentation="Latency of writing an event to the audit log database.",
    labelnames=[],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics HTTP Application
# ─────────────────────────────────────────────────────────────────────────────


def create_metrics_app(
    title: str = "FlowOS Observability Metrics",
    path: str = "/metrics",
) -> FastAPI:
    """
    Create a minimal FastAPI application that serves Prometheus metrics.

    The ``/metrics`` endpoint returns the Prometheus text exposition format
    (``text/plain; version=0.0.4``).  The ``/health`` endpoint returns a
    simple JSON health check.

    Args:
        title: Application title shown in the OpenAPI docs.
        path:  URL path for the metrics endpoint (default: ``/metrics``).

    Returns:
        A configured ``FastAPI`` application instance.

    Example::

        import uvicorn
        from observability.metrics import create_metrics_app

        app = create_metrics_app()
        uvicorn.run(app, host="0.0.0.0", port=9090)
    """
    app = FastAPI(
        title=title,
        description="Prometheus metrics endpoint for FlowOS observability.",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.get(path, response_class=PlainTextResponse, include_in_schema=False)
    def metrics_endpoint() -> Response:
        """
        Serve Prometheus metrics in text exposition format.

        Returns:
            HTTP 200 with ``Content-Type: text/plain; version=0.0.4``
            containing all registered metric samples.
        """
        if not _PROMETHEUS_AVAILABLE:
            return Response(
                content="# prometheus_client not installed\n",
                media_type="text/plain",
                status_code=503,
            )
        output = generate_latest(REGISTRY)
        return Response(
            content=output,
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/health")
    def health_endpoint() -> dict[str, str]:
        """Simple liveness probe."""
        return {"status": "ok", "service": "flowos-observability"}

    @app.get("/")
    def root_endpoint() -> dict[str, str]:
        """Root endpoint — redirects to /metrics."""
        return {
            "service": "flowos-observability",
            "metrics_url": path,
            "health_url": "/health",
        }

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers
# ─────────────────────────────────────────────────────────────────────────────


def record_event_consumed(topic: str, event_type: str, latency_seconds: float) -> None:
    """
    Record that an event was successfully consumed and processed.

    Updates:
        - ``EVENTS_CONSUMED_TOTAL`` counter
        - ``EVENT_PROCESSING_LATENCY_SECONDS`` histogram
        - ``LAST_EVENT_PROCESSED_TIMESTAMP`` gauge

    Args:
        topic:           Kafka topic name.
        event_type:      Event type discriminator string.
        latency_seconds: Time taken to process the event in seconds.
    """
    EVENTS_CONSUMED_TOTAL.labels(topic=topic, event_type=event_type).inc()
    EVENT_PROCESSING_LATENCY_SECONDS.labels(topic=topic, event_type=event_type).observe(
        latency_seconds
    )
    LAST_EVENT_PROCESSED_TIMESTAMP.labels(topic=topic).set(time.time())


def record_processing_error(topic: str, error_type: str) -> None:
    """
    Record that an event failed processing.

    Args:
        topic:      Kafka topic name.
        error_type: Short error class name (e.g. ``ValidationError``).
    """
    EVENTS_PROCESSING_ERRORS_TOTAL.labels(topic=topic, error_type=error_type).inc()
