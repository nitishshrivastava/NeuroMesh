"""
shared/models/event.py — FlowOS Event Domain Model

Defines the canonical Pydantic model for all Kafka events flowing through the
FlowOS event bus.  Every meaningful action in the system — task assignment,
checkpoint creation, AI suggestion, build result — is expressed as an
``EventRecord`` on a well-defined topic.

Event envelope design:
- ``event_id``   — globally unique identifier (UUID v4)
- ``event_type`` — discriminator string (e.g. TASK_ASSIGNED)
- ``topic``      — Kafka topic the event belongs to
- ``source``     — originating service / component
- ``payload``    — arbitrary JSON-serialisable dict (event-type-specific data)
- ``metadata``   — optional key/value bag for tracing, correlation IDs, etc.
- ``occurred_at``— UTC timestamp of when the event was generated
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class EventTopic(StrEnum):
    """Kafka topic names used by FlowOS.  Maps 1-to-1 with topics.json."""

    WORKFLOW_EVENTS = "flowos.workflow.events"
    TASK_EVENTS = "flowos.task.events"
    AGENT_EVENTS = "flowos.agent.events"
    BUILD_EVENTS = "flowos.build.events"
    AI_EVENTS = "flowos.ai.events"
    WORKSPACE_EVENTS = "flowos.workspace.events"
    POLICY_EVENTS = "flowos.policy.events"
    OBSERVABILITY_EVENTS = "flowos.observability.events"


class EventType(StrEnum):
    """
    All event type discriminators used across FlowOS topics.

    Grouped by domain for readability.  The prefix matches the topic:
    - WORKFLOW_*   → flowos.workflow.events
    - TASK_*       → flowos.task.events
    - AGENT_*      → flowos.agent.events
    - BUILD_*      → flowos.build.events
    - AI_*         → flowos.ai.events
    - WORKSPACE_*  → flowos.workspace.events
    - POLICY_*     → flowos.policy.events
    - METRIC_*     → flowos.observability.events
    """

    # Workflow lifecycle
    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    WORKFLOW_PAUSED = "WORKFLOW_PAUSED"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"

    # Task lifecycle
    TASK_CREATED = "TASK_CREATED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_ACCEPTED = "TASK_ACCEPTED"
    TASK_STARTED = "TASK_STARTED"
    TASK_UPDATED = "TASK_UPDATED"
    TASK_CHECKPOINTED = "TASK_CHECKPOINTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_REVERT_REQUESTED = "TASK_REVERT_REQUESTED"
    TASK_HANDOFF_REQUESTED = "TASK_HANDOFF_REQUESTED"
    TASK_HANDOFF_ACCEPTED = "TASK_HANDOFF_ACCEPTED"

    # Agent lifecycle
    AGENT_REGISTERED = "AGENT_REGISTERED"
    AGENT_ONLINE = "AGENT_ONLINE"
    AGENT_OFFLINE = "AGENT_OFFLINE"
    AGENT_BUSY = "AGENT_BUSY"
    AGENT_IDLE = "AGENT_IDLE"
    AGENT_CAPABILITY_UPDATED = "AGENT_CAPABILITY_UPDATED"

    # Build / execution
    BUILD_TRIGGERED = "BUILD_TRIGGERED"
    BUILD_STARTED = "BUILD_STARTED"
    BUILD_SUCCEEDED = "BUILD_SUCCEEDED"
    BUILD_FAILED = "BUILD_FAILED"
    TEST_STARTED = "TEST_STARTED"
    TEST_FINISHED = "TEST_FINISHED"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    ARTIFACT_UPLOADED = "ARTIFACT_UPLOADED"

    # AI reasoning
    AI_TASK_STARTED = "AI_TASK_STARTED"
    AI_SUGGESTION_CREATED = "AI_SUGGESTION_CREATED"
    AI_PATCH_PROPOSED = "AI_PATCH_PROPOSED"
    AI_REVIEW_COMPLETED = "AI_REVIEW_COMPLETED"
    AI_REASONING_TRACE = "AI_REASONING_TRACE"
    AI_TOOL_CALLED = "AI_TOOL_CALLED"

    # Workspace / Git
    WORKSPACE_CREATED = "WORKSPACE_CREATED"
    WORKSPACE_SYNCED = "WORKSPACE_SYNCED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    CHECKPOINT_REVERTED = "CHECKPOINT_REVERTED"
    BRANCH_CREATED = "BRANCH_CREATED"
    HANDOFF_PREPARED = "HANDOFF_PREPARED"
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"

    # Policy engine
    POLICY_EVALUATED = "POLICY_EVALUATED"
    POLICY_VIOLATION_DETECTED = "POLICY_VIOLATION_DETECTED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    BRANCH_PROTECTION_TRIGGERED = "BRANCH_PROTECTION_TRIGGERED"

    # Observability
    METRIC_RECORDED = "METRIC_RECORDED"
    TRACE_SPAN_STARTED = "TRACE_SPAN_STARTED"
    TRACE_SPAN_ENDED = "TRACE_SPAN_ENDED"
    HEALTH_CHECK_RESULT = "HEALTH_CHECK_RESULT"
    SYSTEM_ALERT = "SYSTEM_ALERT"


class EventSource(StrEnum):
    """Identifies the FlowOS component that produced an event."""

    ORCHESTRATOR = "orchestrator"
    API = "api"
    CLI = "cli"
    WORKER = "worker"
    AI_AGENT = "ai_agent"
    HUMAN_AGENT = "human_agent"
    POLICY_ENGINE = "policy_engine"
    WORKSPACE_MANAGER = "workspace_manager"
    BUILD_RUNNER = "build_runner"
    OBSERVABILITY = "observability"
    SYSTEM = "system"


class EventSeverity(StrEnum):
    """Severity level for observability and alerting events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ─────────────────────────────────────────────────────────────────────────────
# Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class EventRecord(BaseModel):
    """
    Canonical envelope for all FlowOS Kafka events.

    Every event flowing through the system is wrapped in this envelope.
    The ``payload`` field carries event-type-specific data as a free-form
    dict; consumers should validate the payload against the appropriate
    event-specific schema after deserialising.

    Attributes:
        event_id:        Globally unique event identifier (UUID v4).
        event_type:      Discriminator string identifying the event kind.
        topic:           Kafka topic this event belongs to.
        source:          Component that produced this event.
        workflow_id:     Optional workflow this event is scoped to.
        task_id:         Optional task this event is scoped to.
        agent_id:        Optional agent this event is scoped to.
        correlation_id:  Optional ID linking related events (e.g. a request
                         and its response).
        causation_id:    Optional ID of the event that caused this one.
        severity:        Severity level (primarily for observability events).
        payload:         Event-type-specific data.
        metadata:        Arbitrary key/value pairs for tracing, routing, etc.
        occurred_at:     UTC timestamp when the event was generated.
        schema_version:  Payload schema version for forward compatibility.
    """

    model_config = {"frozen": True}

    event_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique event identifier (UUID v4).",
    )
    event_type: EventType = Field(
        description="Discriminator string identifying the event kind.",
    )
    topic: EventTopic = Field(
        description="Kafka topic this event belongs to.",
    )
    source: EventSource = Field(
        description="Component that produced this event.",
    )
    workflow_id: str | None = Field(
        default=None,
        description="Optional workflow this event is scoped to.",
    )
    task_id: str | None = Field(
        default=None,
        description="Optional task this event is scoped to.",
    )
    agent_id: str | None = Field(
        default=None,
        description="Optional agent this event is scoped to.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Optional ID linking related events (e.g. request/response pair).",
    )
    causation_id: str | None = Field(
        default=None,
        description="Optional ID of the event that caused this one.",
    )
    severity: EventSeverity = Field(
        default=EventSeverity.INFO,
        description="Severity level (primarily for observability events).",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-type-specific data as a free-form JSON object.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Arbitrary key/value pairs for tracing, routing, and enrichment.",
    )
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the event was generated.",
    )
    schema_version: str = Field(
        default="1.0",
        description="Payload schema version for forward compatibility.",
    )

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        """Ensure event_id is a valid UUID string."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"event_id must be a valid UUID, got: {v!r}") from exc
        return v

    @field_validator("occurred_at", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Ensure occurred_at is timezone-aware (UTC)."""
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def validate_topic_event_type_alignment(self) -> "EventRecord":
        """
        Soft-validate that the event_type prefix aligns with the topic.

        This is a best-effort check — it warns but does not raise so that
        future event types added to one topic don't break existing consumers.
        """
        _TOPIC_PREFIXES: dict[EventTopic, tuple[str, ...]] = {
            EventTopic.WORKFLOW_EVENTS: ("WORKFLOW_",),
            EventTopic.TASK_EVENTS: ("TASK_",),
            EventTopic.AGENT_EVENTS: ("AGENT_",),
            EventTopic.BUILD_EVENTS: ("BUILD_", "TEST_", "ARTIFACT_"),
            EventTopic.AI_EVENTS: ("AI_",),
            EventTopic.WORKSPACE_EVENTS: (
                "WORKSPACE_",
                "CHECKPOINT_",
                "BRANCH_",
                "HANDOFF_",
                "SNAPSHOT_",
            ),
            EventTopic.POLICY_EVENTS: ("POLICY_", "APPROVAL_", "BRANCH_PROTECTION_"),
            EventTopic.OBSERVABILITY_EVENTS: (
                "METRIC_",
                "TRACE_",
                "HEALTH_",
                "SYSTEM_",
            ),
        }
        prefixes = _TOPIC_PREFIXES.get(self.topic, ())
        if prefixes and not any(
            self.event_type.value.startswith(p) for p in prefixes
        ):
            import warnings

            warnings.warn(
                f"EventType {self.event_type!r} may not belong to topic {self.topic!r}. "
                "Verify the topic/event_type combination.",
                stacklevel=2,
            )
        return self

    def to_kafka_value(self) -> bytes:
        """Serialise this event to JSON bytes for Kafka produce."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_kafka_value(cls, data: bytes) -> "EventRecord":
        """Deserialise an event from Kafka message bytes."""
        return cls.model_validate_json(data)

    def with_correlation(self, correlation_id: str) -> "EventRecord":
        """Return a copy of this event with the given correlation_id set."""
        return self.model_copy(update={"correlation_id": correlation_id})

    def caused_by(self, parent_event_id: str) -> "EventRecord":
        """Return a copy of this event with causation_id set to parent_event_id."""
        return self.model_copy(update={"causation_id": parent_event_id})
