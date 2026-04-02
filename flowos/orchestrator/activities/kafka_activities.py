"""
orchestrator/activities/kafka_activities.py — Kafka Event Publishing Activities

Temporal activities for publishing events to the FlowOS Kafka event bus.
These activities are the bridge between the durable Temporal workflow execution
and the real-time Kafka event stream.

All activities are idempotent: re-executing them on retry produces the same
observable outcome (events may be published multiple times, but consumers
handle deduplication via event_id).

Design notes:
- Activities use the module-level FlowOSProducer singleton for efficiency.
- Each activity publishes a single event and waits for delivery confirmation.
- Errors are propagated as ApplicationError so Temporal can retry them.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from temporalio import activity

from shared.config import settings
from shared.kafka.producer import get_producer
from shared.kafka.schemas import (
    build_event,
    WorkflowCreatedPayload,
    WorkflowStartedPayload,
    WorkflowCompletedPayload,
    WorkflowFailedPayload,
    WorkflowCancelledPayload,
    WorkflowPausedPayload,
    WorkflowResumedPayload,
    TaskCreatedPayload,
    TaskAssignedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    TaskCheckpointedPayload,
    TaskHandoffRequestedPayload,
    WorkspaceCreatedPayload,
    CheckpointCreatedPayload,
    CheckpointRevertedPayload,
)
from shared.models.event import EventSource, EventType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow event activities
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="publish_workflow_event")
async def publish_workflow_event(
    event_type: str,
    workflow_id: str,
    workflow_name: str,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Publish a workflow lifecycle event to Kafka.

    Supports: WORKFLOW_CREATED, WORKFLOW_STARTED, WORKFLOW_COMPLETED,
              WORKFLOW_FAILED, WORKFLOW_CANCELLED, WORKFLOW_PAUSED,
              WORKFLOW_RESUMED.

    Args:
        event_type:    EventType string (e.g. "WORKFLOW_CREATED").
        workflow_id:   Workflow instance ID.
        workflow_name: Human-readable workflow name.
        payload_extra: Additional payload fields merged into the event payload.

    Returns:
        Dict with event_id and topic confirming publication.

    Raises:
        ApplicationError: If the event cannot be published after retries.
    """
    extra = payload_extra or {}
    evt_type = EventType(event_type)

    try:
        if evt_type == EventType.WORKFLOW_CREATED:
            payload = WorkflowCreatedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                trigger=extra.get("trigger", "manual"),
                owner_agent_id=extra.get("owner_agent_id"),
                project=extra.get("project"),
                definition_name=extra.get("definition_name"),
                definition_version=extra.get("definition_version"),
                inputs=extra.get("inputs", {}),
                tags=extra.get("tags", []),
            )
        elif evt_type == EventType.WORKFLOW_STARTED:
            payload = WorkflowStartedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                temporal_workflow_id=extra.get("temporal_workflow_id"),
                temporal_run_id=extra.get("temporal_run_id"),
            )
        elif evt_type == EventType.WORKFLOW_COMPLETED:
            payload = WorkflowCompletedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                outputs=extra.get("outputs", {}),
                duration_seconds=extra.get("duration_seconds"),
            )
        elif evt_type == EventType.WORKFLOW_FAILED:
            payload = WorkflowFailedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                error_message=extra.get("error_message", "Unknown error"),
                error_details=extra.get("error_details", {}),
            )
        elif evt_type == EventType.WORKFLOW_CANCELLED:
            payload = WorkflowCancelledPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                reason=extra.get("reason"),
                cancelled_by=extra.get("cancelled_by"),
            )
        elif evt_type == EventType.WORKFLOW_PAUSED:
            payload = WorkflowPausedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                reason=extra.get("reason"),
                paused_by=extra.get("paused_by"),
            )
        elif evt_type == EventType.WORKFLOW_RESUMED:
            payload = WorkflowResumedPayload(
                workflow_id=workflow_id,
                name=workflow_name,
                resumed_by=extra.get("resumed_by"),
            )
        else:
            raise ValueError(f"Unsupported workflow event type: {event_type!r}")

        event = build_event(
            event_type=evt_type,
            payload=payload,
            source=EventSource.ORCHESTRATOR,
            workflow_id=workflow_id,
            correlation_id=extra.get("correlation_id"),
        )

        producer = get_producer()
        producer.produce_sync(event, timeout=10.0)

        logger.info(
            "Published workflow event | event_type=%s workflow_id=%s event_id=%s",
            event_type,
            workflow_id,
            event.event_id,
        )
        return {"event_id": event.event_id, "topic": str(event.topic)}

    except Exception as exc:
        logger.error(
            "Failed to publish workflow event | event_type=%s workflow_id=%s error=%s",
            event_type,
            workflow_id,
            exc,
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Task event activities
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="publish_task_event")
async def publish_task_event(
    event_type: str,
    task_id: str,
    workflow_id: str,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Publish a task lifecycle event to Kafka.

    Supports: TASK_CREATED, TASK_ASSIGNED, TASK_COMPLETED, TASK_FAILED,
              TASK_CHECKPOINTED, TASK_HANDOFF_REQUESTED.

    Args:
        event_type:    EventType string (e.g. "TASK_CREATED").
        task_id:       Task instance ID.
        workflow_id:   Workflow this task belongs to.
        payload_extra: Additional payload fields.

    Returns:
        Dict with event_id and topic confirming publication.

    Raises:
        ApplicationError: If the event cannot be published after retries.
    """
    extra = payload_extra or {}
    evt_type = EventType(event_type)

    try:
        if evt_type == EventType.TASK_CREATED:
            payload = TaskCreatedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                step_id=extra.get("step_id"),
                name=extra.get("name", "Unnamed Task"),
                task_type=extra.get("task_type", "human"),
                priority=extra.get("priority", "normal"),
                depends_on=extra.get("depends_on", []),
                inputs=extra.get("inputs", []),
                tags=extra.get("tags", []),
            )
        elif evt_type == EventType.TASK_ASSIGNED:
            payload = TaskAssignedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                name=extra.get("name", "Unnamed Task"),
                assigned_agent_id=extra["assigned_agent_id"],
                task_type=extra.get("task_type", "human"),
                priority=extra.get("priority", "normal"),
            )
        elif evt_type == EventType.TASK_COMPLETED:
            payload = TaskCompletedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=extra.get("agent_id", "unknown"),
                outputs=extra.get("outputs", []),
                duration_seconds=extra.get("duration_seconds"),
            )
        elif evt_type == EventType.TASK_FAILED:
            payload = TaskFailedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=extra.get("agent_id"),
                error_message=extra.get("error_message", "Unknown error"),
                error_details=extra.get("error_details", {}),
                attempt_number=extra.get("attempt_number", 1),
                will_retry=extra.get("will_retry", False),
            )
        elif evt_type == EventType.TASK_CHECKPOINTED:
            payload = TaskCheckpointedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=extra.get("agent_id", "unknown"),
                checkpoint_id=extra["checkpoint_id"],
                git_commit_sha=extra.get("git_commit_sha"),
                message=extra.get("message"),
            )
        elif evt_type == EventType.TASK_HANDOFF_REQUESTED:
            payload = TaskHandoffRequestedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                from_agent_id=extra["from_agent_id"],
                to_agent_id=extra["to_agent_id"],
                handoff_id=extra["handoff_id"],
                reason=extra.get("reason"),
                context_summary=extra.get("context_summary"),
            )
        else:
            raise ValueError(f"Unsupported task event type: {event_type!r}")

        event = build_event(
            event_type=evt_type,
            payload=payload,
            source=EventSource.ORCHESTRATOR,
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=extra.get("agent_id") or extra.get("assigned_agent_id"),
            correlation_id=extra.get("correlation_id"),
        )

        producer = get_producer()
        producer.produce_sync(event, timeout=10.0)

        logger.info(
            "Published task event | event_type=%s task_id=%s workflow_id=%s event_id=%s",
            event_type,
            task_id,
            workflow_id,
            event.event_id,
        )
        return {"event_id": event.event_id, "topic": str(event.topic)}

    except Exception as exc:
        logger.error(
            "Failed to publish task event | event_type=%s task_id=%s error=%s",
            event_type,
            task_id,
            exc,
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Workspace event activities
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="publish_workspace_event")
async def publish_workspace_event(
    event_type: str,
    workspace_id: str,
    agent_id: str,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Publish a workspace/Git state event to Kafka.

    Supports: WORKSPACE_CREATED, CHECKPOINT_CREATED, CHECKPOINT_REVERTED.

    Args:
        event_type:    EventType string (e.g. "WORKSPACE_CREATED").
        workspace_id:  Workspace instance ID.
        agent_id:      Agent that owns/modified the workspace.
        payload_extra: Additional payload fields.

    Returns:
        Dict with event_id and topic confirming publication.

    Raises:
        ApplicationError: If the event cannot be published after retries.
    """
    extra = payload_extra or {}
    evt_type = EventType(event_type)

    try:
        if evt_type == EventType.WORKSPACE_CREATED:
            payload = WorkspaceCreatedPayload(
                workspace_id=workspace_id,
                agent_id=agent_id,
                workspace_type=extra.get("workspace_type", "local"),
                root_path=extra.get("root_path", ""),
                git_remote_url=extra.get("git_remote_url"),
                branch=extra.get("branch", "main"),
            )
        elif evt_type == EventType.CHECKPOINT_CREATED:
            payload = CheckpointCreatedPayload(
                checkpoint_id=extra["checkpoint_id"],
                task_id=extra["task_id"],
                workspace_id=workspace_id,
                agent_id=agent_id,
                git_commit_sha=extra.get("git_commit_sha", ""),
                branch=extra.get("branch", "main"),
                message=extra.get("message"),
                checkpoint_type=extra.get("checkpoint_type", "manual"),
            )
        elif evt_type == EventType.CHECKPOINT_REVERTED:
            payload = CheckpointRevertedPayload(
                checkpoint_id=extra["checkpoint_id"],
                task_id=extra["task_id"],
                workspace_id=workspace_id,
                agent_id=agent_id,
            )
        else:
            raise ValueError(f"Unsupported workspace event type: {event_type!r}")

        event = build_event(
            event_type=evt_type,
            payload=payload,
            source=EventSource.ORCHESTRATOR,
            workflow_id=extra.get("workflow_id"),
            task_id=extra.get("task_id"),
            agent_id=agent_id,
            correlation_id=extra.get("correlation_id"),
        )

        producer = get_producer()
        producer.produce_sync(event, timeout=10.0)

        logger.info(
            "Published workspace event | event_type=%s workspace_id=%s event_id=%s",
            event_type,
            workspace_id,
            event.event_id,
        )
        return {"event_id": event.event_id, "topic": str(event.topic)}

    except Exception as exc:
        logger.error(
            "Failed to publish workspace event | event_type=%s workspace_id=%s error=%s",
            event_type,
            workspace_id,
            exc,
        )
        raise
