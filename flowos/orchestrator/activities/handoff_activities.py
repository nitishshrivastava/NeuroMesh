"""
orchestrator/activities/handoff_activities.py — Task Handoff Activities

Temporal activities for managing task handoffs between agents.
A handoff is the transfer of task ownership from one agent to another.

Handoff process:
1. Source agent requests handoff → ``initiate_handoff()``
2. A pre-handoff checkpoint is created (by checkpoint_activities)
3. Target agent receives TASK_HANDOFF_REQUESTED event via Kafka
4. Target agent accepts → ``wait_for_handoff_acceptance()`` unblocks
5. Workspace is synced to target agent
6. ``complete_handoff()`` finalises the transfer

The orchestrator coordinates the handoff; agents perform the actual
workspace operations.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from temporalio import activity

from shared.db.session import async_db_session
from shared.db.models import HandoffORM, TaskORM
from shared.models.handoff import HandoffStatus, HandoffType
from shared.models.task import TaskStatus
from shared.models.event import EventSource, EventType
from shared.kafka.producer import get_producer
from shared.kafka.schemas import (
    build_event,
    TaskHandoffRequestedPayload,
    TaskHandoffAcceptedPayload,
)

logger = logging.getLogger(__name__)

# Default handoff acceptance timeout (seconds)
DEFAULT_HANDOFF_TIMEOUT_SECS: int = 3600  # 1 hour

# Poll interval for waiting on handoff acceptance (seconds)
HANDOFF_POLL_INTERVAL_SECS: int = 15

# Heartbeat interval during wait (seconds)
HEARTBEAT_INTERVAL_SECS: int = 30


# ─────────────────────────────────────────────────────────────────────────────
# Handoff initiation
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="initiate_handoff")
async def initiate_handoff(
    task_id: str,
    workflow_id: str,
    source_agent_id: str,
    target_agent_id: str | None,
    handoff_type: str = "delegation",
    context_summary: str | None = None,
    remaining_work: str | None = None,
    known_issues: list[str] | None = None,
    suggested_approach: str | None = None,
    relevant_files: list[str] | None = None,
    checkpoint_id: str | None = None,
    workspace_id: str | None = None,
    priority: str = "normal",
    expires_in_secs: int = DEFAULT_HANDOFF_TIMEOUT_SECS,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Initiate a task handoff from one agent to another.

    Creates a handoff record in the database, updates the task status to
    HANDOFF_REQUESTED, and publishes a TASK_HANDOFF_REQUESTED event so the
    target agent receives the handoff via Kafka.

    Args:
        task_id:           Task being handed off.
        workflow_id:       Workflow this handoff is associated with.
        source_agent_id:   Agent initiating the handoff.
        target_agent_id:   Agent receiving the handoff (None = orchestrator selects).
        handoff_type:      Reason for the handoff (delegation, escalation, etc.).
        context_summary:   Summary of work completed so far.
        remaining_work:    Description of what still needs to be done.
        known_issues:      List of known issues or blockers.
        suggested_approach: Suggested approach for the target agent.
        relevant_files:    List of relevant file paths.
        checkpoint_id:     Pre-handoff checkpoint ID.
        workspace_id:      Workspace being transferred.
        priority:          Urgency of this handoff request.
        expires_in_secs:   Seconds until the handoff request expires.
        metadata:          Arbitrary key/value metadata.

    Returns:
        Dict with handoff_id, status, and expiry time.

    Raises:
        ApplicationError: If the handoff cannot be initiated.
    """
    handoff_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in_secs)

    # Build context dict for storage
    context_data: dict[str, Any] | None = None
    if context_summary or remaining_work:
        context_data = {
            "summary": context_summary or "Task handoff requested.",
            "remaining_work": remaining_work,
            "known_issues": known_issues or [],
            "suggested_approach": suggested_approach,
            "relevant_files": relevant_files or [],
        }

    # Persist handoff record
    async with async_db_session() as db:
        handoff_row = HandoffORM(
            handoff_id=handoff_id,
            task_id=task_id,
            workflow_id=workflow_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            handoff_type=handoff_type,
            status=HandoffStatus.REQUESTED.value,
            checkpoint_id=checkpoint_id,
            workspace_id=workspace_id,
            context=context_data,
            priority=priority,
            expires_at=expires_at,
            handoff_metadata=metadata or {},
            requested_at=now,
            updated_at=now,
        )
        db.add(handoff_row)

        # Update task status to HANDOFF_REQUESTED
        await db.execute(
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(
                status=TaskStatus.HANDOFF_REQUESTED.value,
                updated_at=now,
            )
        )
        await db.commit()

    # Publish TASK_HANDOFF_REQUESTED event
    payload = TaskHandoffRequestedPayload(
        task_id=task_id,
        workflow_id=workflow_id,
        from_agent_id=source_agent_id,
        to_agent_id=target_agent_id or "any",
        handoff_id=handoff_id,
        reason=context_summary,
        context_summary=context_summary,
    )
    event = build_event(
        event_type=EventType.TASK_HANDOFF_REQUESTED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=target_agent_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Initiated handoff | handoff_id=%s task_id=%s from=%s to=%s",
        handoff_id,
        task_id,
        source_agent_id,
        target_agent_id or "any",
    )
    return {
        "handoff_id": handoff_id,
        "status": HandoffStatus.REQUESTED.value,
        "expires_at": expires_at.isoformat(),
        "target_agent_id": target_agent_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Waiting for handoff acceptance
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="wait_for_handoff_acceptance")
async def wait_for_handoff_acceptance(
    handoff_id: str,
    timeout_secs: int = DEFAULT_HANDOFF_TIMEOUT_SECS,
) -> dict[str, Any]:
    """
    Poll the database until a handoff is accepted, rejected, or expires.

    Uses Temporal heartbeats to prevent activity timeout during long waits.

    Args:
        handoff_id:   Handoff to wait for.
        timeout_secs: Maximum time to wait (seconds).

    Returns:
        Dict with handoff_id, status, and accepting agent_id.

    Raises:
        ApplicationError: If the handoff is rejected, expires, or times out.
    """
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_secs)
    poll_count = 0

    while True:
        if datetime.now(timezone.utc) > deadline:
            # Mark handoff as expired
            await _expire_handoff(handoff_id)
            raise TimeoutError(
                f"Handoff {handoff_id!r} timed out after {timeout_secs}s "
                "without acceptance."
            )

        # Heartbeat
        poll_count += 1
        if poll_count % max(1, HEARTBEAT_INTERVAL_SECS // HANDOFF_POLL_INTERVAL_SECS) == 0:
            activity.heartbeat(
                f"Waiting for handoff {handoff_id} acceptance (poll #{poll_count})"
            )

        async with async_db_session() as db:
            stmt = select(HandoffORM).where(HandoffORM.handoff_id == handoff_id)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                raise ValueError(f"Handoff {handoff_id!r} not found.")

            if row.status == HandoffStatus.ACCEPTED.value:
                logger.info(
                    "Handoff accepted | handoff_id=%s agent_id=%s",
                    handoff_id,
                    row.target_agent_id,
                )
                return {
                    "handoff_id": handoff_id,
                    "status": HandoffStatus.ACCEPTED.value,
                    "target_agent_id": row.target_agent_id,
                    "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
                }

            if row.status == HandoffStatus.REJECTED.value:
                raise ValueError(
                    f"Handoff {handoff_id!r} was rejected by agent "
                    f"{row.target_agent_id!r}: {row.rejection_reason}"
                )

            if row.status in (
                HandoffStatus.EXPIRED.value,
                HandoffStatus.CANCELLED.value,
                HandoffStatus.FAILED.value,
            ):
                raise ValueError(
                    f"Handoff {handoff_id!r} is in terminal state: {row.status}"
                )

        await asyncio.sleep(HANDOFF_POLL_INTERVAL_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# Handoff completion
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="complete_handoff")
async def complete_handoff(
    handoff_id: str,
    task_id: str,
    workflow_id: str,
    new_agent_id: str,
) -> dict[str, Any]:
    """
    Finalise a handoff by updating the task ownership and publishing events.

    Called after the target agent has accepted the handoff and the workspace
    has been synced.  Updates the task's assigned_agent_id and publishes a
    TASK_HANDOFF_ACCEPTED event.

    Args:
        handoff_id:   Handoff being completed.
        task_id:      Task being transferred.
        workflow_id:  Workflow this handoff is associated with.
        new_agent_id: Agent that accepted the handoff.

    Returns:
        Dict with handoff_id and completed status.

    Raises:
        ApplicationError: If the completion fails.
    """
    now = datetime.now(timezone.utc)

    async with async_db_session() as db:
        # Mark handoff as completed
        await db.execute(
            update(HandoffORM)
            .where(HandoffORM.handoff_id == handoff_id)
            .values(
                status=HandoffStatus.COMPLETED.value,
                completed_at=now,
                updated_at=now,
            )
        )

        # Transfer task ownership
        await db.execute(
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(
                assigned_agent_id=new_agent_id,
                status=TaskStatus.HANDOFF_ACCEPTED.value,
                updated_at=now,
            )
        )
        await db.commit()

    # Publish TASK_HANDOFF_ACCEPTED event
    payload = TaskHandoffAcceptedPayload(
        task_id=task_id,
        workflow_id=workflow_id,
        handoff_id=handoff_id,
        new_agent_id=new_agent_id,
    )
    event = build_event(
        event_type=EventType.TASK_HANDOFF_ACCEPTED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=new_agent_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Completed handoff | handoff_id=%s task_id=%s new_agent=%s",
        handoff_id,
        task_id,
        new_agent_id,
    )
    return {
        "handoff_id": handoff_id,
        "status": HandoffStatus.COMPLETED.value,
        "new_agent_id": new_agent_id,
        "completed_at": now.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Handoff cancellation
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="cancel_handoff")
async def cancel_handoff(
    handoff_id: str,
    task_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Cancel a pending handoff and restore the task to IN_PROGRESS status.

    Args:
        handoff_id: Handoff to cancel.
        task_id:    Task associated with the handoff.
        reason:     Optional cancellation reason.

    Returns:
        Dict with handoff_id and cancelled status.

    Raises:
        ApplicationError: If the cancellation fails.
    """
    now = datetime.now(timezone.utc)
    cancel_reason = reason or "Handoff cancelled by orchestrator"

    async with async_db_session() as db:
        await db.execute(
            update(HandoffORM)
            .where(HandoffORM.handoff_id == handoff_id)
            .values(
                status=HandoffStatus.CANCELLED.value,
                failure_reason=cancel_reason,
                updated_at=now,
            )
        )

        # Restore task to IN_PROGRESS
        await db.execute(
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(
                status=TaskStatus.IN_PROGRESS.value,
                updated_at=now,
            )
        )
        await db.commit()

    logger.info(
        "Cancelled handoff | handoff_id=%s task_id=%s reason=%s",
        handoff_id,
        task_id,
        cancel_reason,
    )
    return {
        "handoff_id": handoff_id,
        "status": HandoffStatus.CANCELLED.value,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _expire_handoff(handoff_id: str) -> None:
    """Mark a handoff as expired in the database."""
    now = datetime.now(timezone.utc)
    async with async_db_session() as db:
        await db.execute(
            update(HandoffORM)
            .where(HandoffORM.handoff_id == handoff_id)
            .values(
                status=HandoffStatus.EXPIRED.value,
                updated_at=now,
            )
        )
        await db.commit()
    logger.warning("Handoff expired | handoff_id=%s", handoff_id)
