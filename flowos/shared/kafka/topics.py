"""
shared/kafka/topics.py — FlowOS Kafka Topic & Consumer-Group Registry

Provides a single authoritative source of truth for:

- All Kafka topic names (as typed constants via ``KafkaTopic``)
- The mapping from ``EventType`` → ``KafkaTopic`` (``TOPIC_FOR_EVENT``)
- Consumer-group identifiers (``ConsumerGroup``)
- Per-topic partition counts and retention metadata (``TOPIC_METADATA``)

Usage::

    from shared.kafka.topics import KafkaTopic, ConsumerGroup, topic_for_event
    from shared.models.event import EventType

    topic = topic_for_event(EventType.WORKFLOW_CREATED)
    # → KafkaTopic.WORKFLOW_EVENTS  ("flowos.workflow.events")

    group = ConsumerGroup.ORCHESTRATOR
    # → "flowos-orchestrator"
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from shared.models.event import EventTopic, EventType


# ─────────────────────────────────────────────────────────────────────────────
# Topic names
# ─────────────────────────────────────────────────────────────────────────────


class KafkaTopic(StrEnum):
    """
    Canonical Kafka topic names for FlowOS.

    Values mirror ``EventTopic`` (and ``infra/kafka/topics.json``) so that
    both the domain model and the infrastructure layer share the same strings
    without coupling.
    """

    WORKFLOW_EVENTS = "flowos.workflow.events"
    TASK_EVENTS = "flowos.task.events"
    AGENT_EVENTS = "flowos.agent.events"
    BUILD_EVENTS = "flowos.build.events"
    AI_EVENTS = "flowos.ai.events"
    WORKSPACE_EVENTS = "flowos.workspace.events"
    POLICY_EVENTS = "flowos.policy.events"
    OBSERVABILITY_EVENTS = "flowos.observability.events"

    @classmethod
    def all_topics(cls) -> list["KafkaTopic"]:
        """Return all topic names as a list."""
        return list(cls)

    @classmethod
    def from_event_topic(cls, event_topic: EventTopic) -> "KafkaTopic":
        """Convert an ``EventTopic`` enum value to a ``KafkaTopic``."""
        return cls(event_topic.value)


# ─────────────────────────────────────────────────────────────────────────────
# Consumer group identifiers
# ─────────────────────────────────────────────────────────────────────────────


class ConsumerGroup(StrEnum):
    """
    Named consumer groups in the FlowOS event bus.

    Each group has a distinct role and subscribes to a specific set of topics.
    The fan-out model ensures that every relevant participant receives every
    event without polling.

    Groups:
        ORCHESTRATOR    — Temporal workflow orchestrator; drives state transitions.
        API             — REST API server; updates metadata store and serves state.
        UI_STREAM       — WebSocket/SSE bridge; pushes real-time updates to browsers.
        POLICY_ENGINE   — Evaluates events against RBAC rules and approval gates.
        OBSERVABILITY   — Captures all events for metrics, tracing, and replay.
        CLI             — CLI agent; receives task assignments and status updates.
        AI_AGENT        — AI reasoning agent; receives AI task assignments.
        BUILD_RUNNER    — Build/test runner; receives build trigger events.
        WORKSPACE_MGR   — Workspace manager; handles Git state events.
    """

    ORCHESTRATOR = "flowos-orchestrator"
    API = "flowos-api"
    UI_STREAM = "flowos-ui-stream"
    POLICY_ENGINE = "flowos-policy-engine"
    OBSERVABILITY = "flowos-observability"
    CLI = "flowos-cli"
    AI_AGENT = "flowos-ai-agent"
    BUILD_RUNNER = "flowos-build-runner"
    WORKSPACE_MGR = "flowos-workspace-manager"


# ─────────────────────────────────────────────────────────────────────────────
# Topic metadata
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TopicMetadata:
    """
    Static metadata for a Kafka topic.

    Attributes:
        name:               Topic name string.
        partitions:         Number of partitions.
        replication_factor: Replication factor.
        retention_ms:       Message retention in milliseconds.
        max_message_bytes:  Maximum message size in bytes.
        description:        Human-readable description.
    """

    name: str
    partitions: int
    replication_factor: int
    retention_ms: int
    max_message_bytes: int
    description: str


#: Per-topic metadata aligned with ``infra/kafka/topics.json``.
TOPIC_METADATA: Final[dict[KafkaTopic, TopicMetadata]] = {
    KafkaTopic.WORKFLOW_EVENTS: TopicMetadata(
        name=KafkaTopic.WORKFLOW_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=604_800_000,   # 7 days
        max_message_bytes=1_048_576,  # 1 MiB
        description=(
            "Lifecycle events for workflows: WORKFLOW_CREATED, WORKFLOW_STARTED, "
            "WORKFLOW_PAUSED, WORKFLOW_RESUMED, WORKFLOW_COMPLETED, WORKFLOW_FAILED, "
            "WORKFLOW_CANCELLED."
        ),
    ),
    KafkaTopic.TASK_EVENTS: TopicMetadata(
        name=KafkaTopic.TASK_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=604_800_000,   # 7 days
        max_message_bytes=1_048_576,  # 1 MiB
        description=(
            "Lifecycle events for tasks: TASK_CREATED, TASK_ASSIGNED, TASK_ACCEPTED, "
            "TASK_STARTED, TASK_UPDATED, TASK_CHECKPOINTED, TASK_COMPLETED, TASK_FAILED, "
            "TASK_REVERT_REQUESTED, TASK_HANDOFF_REQUESTED, TASK_HANDOFF_ACCEPTED."
        ),
    ),
    KafkaTopic.AGENT_EVENTS: TopicMetadata(
        name=KafkaTopic.AGENT_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=259_200_000,   # 3 days
        max_message_bytes=524_288,   # 512 KiB
        description=(
            "Agent registration and status events: AGENT_REGISTERED, AGENT_ONLINE, "
            "AGENT_OFFLINE, AGENT_BUSY, AGENT_IDLE, AGENT_CAPABILITY_UPDATED."
        ),
    ),
    KafkaTopic.BUILD_EVENTS: TopicMetadata(
        name=KafkaTopic.BUILD_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=604_800_000,   # 7 days
        max_message_bytes=2_097_152,  # 2 MiB
        description=(
            "Build and execution events: BUILD_TRIGGERED, BUILD_STARTED, BUILD_SUCCEEDED, "
            "BUILD_FAILED, TEST_STARTED, TEST_FINISHED, TEST_PASSED, TEST_FAILED, "
            "ARTIFACT_UPLOADED."
        ),
    ),
    KafkaTopic.AI_EVENTS: TopicMetadata(
        name=KafkaTopic.AI_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=604_800_000,   # 7 days
        max_message_bytes=4_194_304,  # 4 MiB
        description=(
            "AI reasoning and suggestion events: AI_TASK_STARTED, AI_SUGGESTION_CREATED, "
            "AI_PATCH_PROPOSED, AI_REVIEW_COMPLETED, AI_REASONING_TRACE, AI_TOOL_CALLED."
        ),
    ),
    KafkaTopic.WORKSPACE_EVENTS: TopicMetadata(
        name=KafkaTopic.WORKSPACE_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=604_800_000,   # 7 days
        max_message_bytes=1_048_576,  # 1 MiB
        description=(
            "Workspace and Git state events: WORKSPACE_CREATED, WORKSPACE_SYNCED, "
            "CHECKPOINT_CREATED, CHECKPOINT_REVERTED, BRANCH_CREATED, HANDOFF_PREPARED, "
            "SNAPSHOT_CREATED."
        ),
    ),
    KafkaTopic.POLICY_EVENTS: TopicMetadata(
        name=KafkaTopic.POLICY_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=2_592_000_000,  # 30 days (audit retention)
        max_message_bytes=524_288,   # 512 KiB
        description=(
            "Policy engine events: POLICY_EVALUATED, POLICY_VIOLATION_DETECTED, "
            "APPROVAL_REQUESTED, APPROVAL_GRANTED, APPROVAL_DENIED, "
            "BRANCH_PROTECTION_TRIGGERED."
        ),
    ),
    KafkaTopic.OBSERVABILITY_EVENTS: TopicMetadata(
        name=KafkaTopic.OBSERVABILITY_EVENTS,
        partitions=12,
        replication_factor=1,
        retention_ms=86_400_000,    # 1 day (high-volume metrics)
        max_message_bytes=524_288,   # 512 KiB
        description=(
            "Observability and metrics events: METRIC_RECORDED, TRACE_SPAN_STARTED, "
            "TRACE_SPAN_ENDED, HEALTH_CHECK_RESULT, SYSTEM_ALERT."
        ),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Consumer-group → topic subscriptions
# ─────────────────────────────────────────────────────────────────────────────


#: Defines which topics each consumer group subscribes to.
#: Mirrors the ``consumer_groups`` section of ``infra/kafka/topics.json``.
CONSUMER_GROUP_TOPICS: Final[dict[ConsumerGroup, list[KafkaTopic]]] = {
    ConsumerGroup.ORCHESTRATOR: [
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.AGENT_EVENTS,
        KafkaTopic.BUILD_EVENTS,
        KafkaTopic.AI_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
        KafkaTopic.POLICY_EVENTS,
    ],
    ConsumerGroup.API: [
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.AGENT_EVENTS,
        KafkaTopic.BUILD_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
    ],
    ConsumerGroup.UI_STREAM: [
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.AGENT_EVENTS,
        KafkaTopic.BUILD_EVENTS,
        KafkaTopic.AI_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
        KafkaTopic.POLICY_EVENTS,
        KafkaTopic.OBSERVABILITY_EVENTS,
    ],
    ConsumerGroup.POLICY_ENGINE: [
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
        KafkaTopic.BUILD_EVENTS,
    ],
    ConsumerGroup.OBSERVABILITY: [
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.AGENT_EVENTS,
        KafkaTopic.BUILD_EVENTS,
        KafkaTopic.AI_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
        KafkaTopic.POLICY_EVENTS,
        KafkaTopic.OBSERVABILITY_EVENTS,
    ],
    ConsumerGroup.CLI: [
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.WORKFLOW_EVENTS,
        KafkaTopic.AGENT_EVENTS,
    ],
    ConsumerGroup.AI_AGENT: [
        KafkaTopic.TASK_EVENTS,
        KafkaTopic.AI_EVENTS,
        KafkaTopic.WORKSPACE_EVENTS,
    ],
    ConsumerGroup.BUILD_RUNNER: [
        KafkaTopic.BUILD_EVENTS,
        KafkaTopic.TASK_EVENTS,
    ],
    ConsumerGroup.WORKSPACE_MGR: [
        KafkaTopic.WORKSPACE_EVENTS,
        KafkaTopic.TASK_EVENTS,
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# EventType → KafkaTopic routing table
# ─────────────────────────────────────────────────────────────────────────────


#: Maps every ``EventType`` to its canonical ``KafkaTopic``.
#: Used by the producer to automatically select the correct topic.
TOPIC_FOR_EVENT: Final[dict[EventType, KafkaTopic]] = {
    # Workflow lifecycle → flowos.workflow.events
    EventType.WORKFLOW_CREATED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_STARTED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_PAUSED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_RESUMED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_COMPLETED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_FAILED: KafkaTopic.WORKFLOW_EVENTS,
    EventType.WORKFLOW_CANCELLED: KafkaTopic.WORKFLOW_EVENTS,
    # Task lifecycle → flowos.task.events
    EventType.TASK_CREATED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_ASSIGNED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_ACCEPTED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_STARTED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_UPDATED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_CHECKPOINTED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_COMPLETED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_FAILED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_REVERT_REQUESTED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_HANDOFF_REQUESTED: KafkaTopic.TASK_EVENTS,
    EventType.TASK_HANDOFF_ACCEPTED: KafkaTopic.TASK_EVENTS,
    # Agent lifecycle → flowos.agent.events
    EventType.AGENT_REGISTERED: KafkaTopic.AGENT_EVENTS,
    EventType.AGENT_ONLINE: KafkaTopic.AGENT_EVENTS,
    EventType.AGENT_OFFLINE: KafkaTopic.AGENT_EVENTS,
    EventType.AGENT_BUSY: KafkaTopic.AGENT_EVENTS,
    EventType.AGENT_IDLE: KafkaTopic.AGENT_EVENTS,
    EventType.AGENT_CAPABILITY_UPDATED: KafkaTopic.AGENT_EVENTS,
    # Build / execution → flowos.build.events
    EventType.BUILD_TRIGGERED: KafkaTopic.BUILD_EVENTS,
    EventType.BUILD_STARTED: KafkaTopic.BUILD_EVENTS,
    EventType.BUILD_SUCCEEDED: KafkaTopic.BUILD_EVENTS,
    EventType.BUILD_FAILED: KafkaTopic.BUILD_EVENTS,
    EventType.TEST_STARTED: KafkaTopic.BUILD_EVENTS,
    EventType.TEST_FINISHED: KafkaTopic.BUILD_EVENTS,
    EventType.TEST_PASSED: KafkaTopic.BUILD_EVENTS,
    EventType.TEST_FAILED: KafkaTopic.BUILD_EVENTS,
    EventType.ARTIFACT_UPLOADED: KafkaTopic.BUILD_EVENTS,
    # AI reasoning → flowos.ai.events
    EventType.AI_TASK_STARTED: KafkaTopic.AI_EVENTS,
    EventType.AI_SUGGESTION_CREATED: KafkaTopic.AI_EVENTS,
    EventType.AI_PATCH_PROPOSED: KafkaTopic.AI_EVENTS,
    EventType.AI_REVIEW_COMPLETED: KafkaTopic.AI_EVENTS,
    EventType.AI_REASONING_TRACE: KafkaTopic.AI_EVENTS,
    EventType.AI_TOOL_CALLED: KafkaTopic.AI_EVENTS,
    # Workspace / Git → flowos.workspace.events
    EventType.WORKSPACE_CREATED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.WORKSPACE_SYNCED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.CHECKPOINT_CREATED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.CHECKPOINT_REVERTED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.BRANCH_CREATED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.HANDOFF_PREPARED: KafkaTopic.WORKSPACE_EVENTS,
    EventType.SNAPSHOT_CREATED: KafkaTopic.WORKSPACE_EVENTS,
    # Policy engine → flowos.policy.events
    EventType.POLICY_EVALUATED: KafkaTopic.POLICY_EVENTS,
    EventType.POLICY_VIOLATION_DETECTED: KafkaTopic.POLICY_EVENTS,
    EventType.APPROVAL_REQUESTED: KafkaTopic.POLICY_EVENTS,
    EventType.APPROVAL_GRANTED: KafkaTopic.POLICY_EVENTS,
    EventType.APPROVAL_DENIED: KafkaTopic.POLICY_EVENTS,
    EventType.BRANCH_PROTECTION_TRIGGERED: KafkaTopic.POLICY_EVENTS,
    # Observability → flowos.observability.events
    EventType.METRIC_RECORDED: KafkaTopic.OBSERVABILITY_EVENTS,
    EventType.TRACE_SPAN_STARTED: KafkaTopic.OBSERVABILITY_EVENTS,
    EventType.TRACE_SPAN_ENDED: KafkaTopic.OBSERVABILITY_EVENTS,
    EventType.HEALTH_CHECK_RESULT: KafkaTopic.OBSERVABILITY_EVENTS,
    EventType.SYSTEM_ALERT: KafkaTopic.OBSERVABILITY_EVENTS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────


def topic_for_event(event_type: EventType) -> KafkaTopic:
    """
    Return the ``KafkaTopic`` that the given ``EventType`` should be produced to.

    Args:
        event_type: The event type discriminator.

    Returns:
        The canonical ``KafkaTopic`` for this event type.

    Raises:
        KeyError: If the event type has no registered topic mapping.
    """
    try:
        return TOPIC_FOR_EVENT[event_type]
    except KeyError:
        raise KeyError(
            f"No topic mapping found for EventType {event_type!r}. "
            "Add it to TOPIC_FOR_EVENT in shared/kafka/topics.py."
        ) from None


def topics_for_group(group: ConsumerGroup) -> list[KafkaTopic]:
    """
    Return the list of topics a consumer group should subscribe to.

    Args:
        group: The consumer group identifier.

    Returns:
        List of ``KafkaTopic`` values for this group.

    Raises:
        KeyError: If the group has no registered topic subscriptions.
    """
    try:
        return CONSUMER_GROUP_TOPICS[group]
    except KeyError:
        raise KeyError(
            f"No topic subscriptions found for ConsumerGroup {group!r}. "
            "Add it to CONSUMER_GROUP_TOPICS in shared/kafka/topics.py."
        ) from None


def topic_names_for_group(group: ConsumerGroup) -> list[str]:
    """
    Return topic name strings for a consumer group (for use with confluent-kafka).

    Args:
        group: The consumer group identifier.

    Returns:
        List of topic name strings.
    """
    return [str(t) for t in topics_for_group(group)]
