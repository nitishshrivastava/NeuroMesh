"""
observability — FlowOS Observability Layer

This package provides the complete observability infrastructure for FlowOS:

- ``consumer.py``   — Kafka consumer that subscribes to all topics, updates
                      Prometheus metrics, and writes events to the audit log.
- ``metrics.py``    — Prometheus metric instruments and the ``/metrics`` HTTP
                      endpoint (FastAPI application).
- ``audit_log.py``  — Audit log writer that persists every event to the
                      ``event_records`` PostgreSQL table.

Quick start::

    from observability.consumer import ObservabilityConsumer

    obs = ObservabilityConsumer(metrics_port=9090)
    obs.start()  # blocks; Ctrl-C for graceful shutdown

Metrics endpoint::

    curl http://localhost:9090/metrics
"""

from observability.consumer import ObservabilityConsumer
from observability.audit_log import AuditLogWriter, get_audit_log_writer
from observability.metrics import (
    REGISTRY,
    create_metrics_app,
    record_event_consumed,
    record_processing_error,
    EVENTS_CONSUMED_TOTAL,
    EVENTS_PROCESSING_ERRORS_TOTAL,
    EVENT_PROCESSING_LATENCY_SECONDS,
    WORKFLOW_EVENTS_TOTAL,
    TASK_EVENTS_TOTAL,
    AGENT_EVENTS_TOTAL,
    BUILD_EVENTS_TOTAL,
    AI_EVENTS_TOTAL,
    WORKSPACE_EVENTS_TOTAL,
    POLICY_EVENTS_TOTAL,
    OBSERVABILITY_EVENTS_TOTAL,
    AUDIT_LOG_WRITES_TOTAL,
)

__all__ = [
    # Consumer
    "ObservabilityConsumer",
    # Audit log
    "AuditLogWriter",
    "get_audit_log_writer",
    # Metrics
    "REGISTRY",
    "create_metrics_app",
    "record_event_consumed",
    "record_processing_error",
    # Counters
    "EVENTS_CONSUMED_TOTAL",
    "EVENTS_PROCESSING_ERRORS_TOTAL",
    "EVENT_PROCESSING_LATENCY_SECONDS",
    "WORKFLOW_EVENTS_TOTAL",
    "TASK_EVENTS_TOTAL",
    "AGENT_EVENTS_TOTAL",
    "BUILD_EVENTS_TOTAL",
    "AI_EVENTS_TOTAL",
    "WORKSPACE_EVENTS_TOTAL",
    "POLICY_EVENTS_TOTAL",
    "OBSERVABILITY_EVENTS_TOTAL",
    "AUDIT_LOG_WRITES_TOTAL",
]
