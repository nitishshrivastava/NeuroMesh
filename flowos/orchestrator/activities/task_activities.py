"""
orchestrator/activities/task_activities.py — Task Management Activities

Temporal activities for creating, assigning, tracking, and completing tasks.
These activities interact with the PostgreSQL metadata store to persist task
state and publish Kafka events to notify agents.

Task lifecycle managed here:
    PENDING → ASSIGNED → ACCEPTED → IN_PROGRESS → COMPLETED
                                               ↘ FAILED
                                               ↘ CHECKPOINTED

Design notes:
- All database operations use async SQLAlchemy sessions.
- Activities are idempotent where possible (upsert semantics).
- Long-running waits (e.g. waiting for task completion) use Temporal's
  heartbeat mechanism to prevent timeout.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from temporalio import activity

from shared.db.session import async_db_session
from shared.db.models import TaskORM
from shared.models.task import TaskStatus, TaskType, TaskPriority
from shared.models.event import EventSource, EventType
from shared.kafka.producer import get_producer
from shared.kafka.schemas import (
    build_event,
    TaskCreatedPayload,
    TaskAssignedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
)

logger = logging.getLogger(__name__)

# How often to heartbeat during long-running waits (seconds)
HEARTBEAT_INTERVAL_SECS: int = 30

# Maximum time to wait for task completion before timing out (seconds)
DEFAULT_TASK_WAIT_TIMEOUT_SECS: int = 86400  # 24 hours


# ─────────────────────────────────────────────────────────────────────────────
# Task creation
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="create_task")
async def create_task(
    workflow_id: str,
    step_id: str | None,
    name: str,
    task_type: str,
    priority: str = "normal",
    description: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    depends_on: list[str] | None = None,
    timeout_secs: int = 0,
    retry_count: int = 0,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a new task record in the database and publish a TASK_CREATED event.

    Args:
        workflow_id:  Workflow this task belongs to.
        step_id:      Workflow step this task was created from.
        name:         Human-readable task name.
        task_type:    Nature of work (human, ai, build, etc.).
        priority:     Scheduling priority (low, normal, high, critical).
        description:  Optional detailed description.
        inputs:       List of input parameter dicts.
        depends_on:   Task IDs that must complete before this task starts.
        timeout_secs: Maximum execution time in seconds (0 = no limit).
        retry_count:  Maximum number of automatic retries.
        tags:         Arbitrary tags for filtering and routing.
        metadata:     Arbitrary key/value metadata.

    Returns:
        Dict containing the created task's ID and status.

    Raises:
        ApplicationError: If the task cannot be created.
    """
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Persist to database
    async with async_db_session() as db:
        task_row = TaskORM(
            task_id=task_id,
            workflow_id=workflow_id,
            step_id=step_id,
            name=name,
            description=description,
            task_type=task_type,
            status=TaskStatus.PENDING.value,
            priority=priority,
            inputs=inputs or [],
            depends_on=depends_on or [],
            timeout_secs=timeout_secs,
            retry_count=retry_count,
            tags=tags or [],
            task_metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        db.add(task_row)
        await db.commit()

    # Publish TASK_CREATED event
    payload = TaskCreatedPayload(
        task_id=task_id,
        workflow_id=workflow_id,
        step_id=step_id,
        name=name,
        task_type=task_type,
        priority=priority,
        depends_on=depends_on or [],
        inputs=inputs or [],
        tags=tags or [],
    )
    event = build_event(
        event_type=EventType.TASK_CREATED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Created task | task_id=%s workflow_id=%s name=%s type=%s",
        task_id,
        workflow_id,
        name,
        task_type,
    )
    return {
        "task_id": task_id,
        "status": TaskStatus.PENDING.value,
        "name": name,
        "task_type": task_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Task assignment
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="assign_task")
async def assign_task(
    task_id: str,
    workflow_id: str,
    agent_id: str,
    task_name: str = "",
    task_type: str = "human",
    priority: str = "normal",
) -> dict[str, Any]:
    """
    Assign a task to an agent and publish a TASK_ASSIGNED event.

    Updates the task status to ASSIGNED and sets the assigned_agent_id.
    Publishes a TASK_ASSIGNED event so the agent receives the assignment
    via Kafka.

    Args:
        task_id:    Task to assign.
        workflow_id: Workflow this task belongs to.
        agent_id:   Agent to assign the task to.
        task_name:  Human-readable task name (for the event payload).
        task_type:  Nature of work (for the event payload).
        priority:   Scheduling priority (for the event payload).

    Returns:
        Dict with task_id, agent_id, and new status.

    Raises:
        ApplicationError: If the task cannot be assigned.
    """
    now = datetime.now(timezone.utc)

    async with async_db_session() as db:
        # Fetch current task data first
        stmt = select(TaskORM).where(TaskORM.task_id == task_id)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if row:
            task_name = task_name or row.name
            task_type = task_type or row.task_type
            priority = priority or row.priority

        # Update task status and assignment
        update_stmt = (
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(
                status=TaskStatus.ASSIGNED.value,
                assigned_agent_id=agent_id,
                assigned_at=now,
                updated_at=now,
            )
        )
        await db.execute(update_stmt)
        await db.commit()

    # Publish TASK_ASSIGNED event
    payload = TaskAssignedPayload(
        task_id=task_id,
        workflow_id=workflow_id,
        name=task_name,
        assigned_agent_id=agent_id,
        task_type=task_type,
        priority=priority,
    )
    event = build_event(
        event_type=EventType.TASK_ASSIGNED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Assigned task | task_id=%s agent_id=%s workflow_id=%s",
        task_id,
        agent_id,
        workflow_id,
    )
    return {
        "task_id": task_id,
        "agent_id": agent_id,
        "status": TaskStatus.ASSIGNED.value,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Task status polling
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="get_task_status")
async def get_task_status(task_id: str) -> dict[str, Any]:
    """
    Retrieve the current status of a task from the database.

    Args:
        task_id: Task to query.

    Returns:
        Dict with task_id, status, assigned_agent_id, and timestamps.

    Raises:
        ApplicationError: If the task is not found.
    """
    async with async_db_session() as db:
        stmt = select(TaskORM).where(TaskORM.task_id == task_id)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            raise ValueError(f"Task {task_id!r} not found in database.")

        return {
            "task_id": row.task_id,
            "status": row.status,
            "assigned_agent_id": row.assigned_agent_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "error_message": row.error_message,
        }


@activity.defn(name="wait_for_task_completion")
async def wait_for_task_completion(
    task_id: str,
    poll_interval_secs: int = 10,
    timeout_secs: int = DEFAULT_TASK_WAIT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """
    Poll the database until a task reaches a terminal state.

    Uses Temporal heartbeats to prevent the activity from timing out during
    long-running waits.

    Args:
        task_id:           Task to wait for.
        poll_interval_secs: How often to poll the database (seconds).
        timeout_secs:      Maximum time to wait before raising a timeout error.

    Returns:
        Dict with final task status and outputs.

    Raises:
        ApplicationError: If the task fails or times out.
    """
    import asyncio

    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_secs)
    heartbeat_counter = 0

    while True:
        # Check if we've exceeded the deadline
        if datetime.now(timezone.utc) > deadline:
            raise TimeoutError(
                f"Timed out waiting for task {task_id!r} to complete "
                f"after {timeout_secs}s."
            )

        # Heartbeat to prevent Temporal activity timeout
        heartbeat_counter += 1
        if heartbeat_counter % max(1, HEARTBEAT_INTERVAL_SECS // poll_interval_secs) == 0:
            activity.heartbeat(f"Waiting for task {task_id} (poll #{heartbeat_counter})")

        # Poll task status
        async with async_db_session() as db:
            stmt = select(TaskORM).where(TaskORM.task_id == task_id)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                raise ValueError(f"Task {task_id!r} not found in database.")

            terminal_statuses = {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.SKIPPED.value,
                TaskStatus.REVERTED.value,
            }

            if row.status in terminal_statuses:
                logger.info(
                    "Task reached terminal state | task_id=%s status=%s",
                    task_id,
                    row.status,
                )
                return {
                    "task_id": row.task_id,
                    "status": row.status,
                    "outputs": row.outputs or [],
                    "error_message": row.error_message,
                    "completed_at": (
                        row.completed_at.isoformat() if row.completed_at else None
                    ),
                }

        # Wait before next poll
        await asyncio.sleep(poll_interval_secs)


# ─────────────────────────────────────────────────────────────────────────────
# Task status updates
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="update_task_status")
async def update_task_status(
    task_id: str,
    new_status: str,
    agent_id: str | None = None,
    error_message: str | None = None,
    outputs: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Update a task's status in the database.

    Args:
        task_id:       Task to update.
        new_status:    New TaskStatus value.
        agent_id:      Agent performing the update (optional).
        error_message: Error description if transitioning to FAILED.
        outputs:       Output parameters if transitioning to COMPLETED.
        metadata:      Additional metadata to merge.

    Returns:
        Dict with task_id and new status.

    Raises:
        ApplicationError: If the update fails.
    """
    now = datetime.now(timezone.utc)
    status = TaskStatus(new_status)

    update_values: dict[str, Any] = {
        "status": status.value,
        "updated_at": now,
    }

    if agent_id:
        update_values["assigned_agent_id"] = agent_id

    if error_message:
        update_values["error_message"] = error_message

    if outputs is not None:
        update_values["outputs"] = outputs

    # Set timestamp fields based on status transition
    if status == TaskStatus.IN_PROGRESS:
        update_values["started_at"] = now
    elif status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.REVERTED,
    ):
        update_values["completed_at"] = now

    async with async_db_session() as db:
        stmt = (
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(**update_values)
        )
        await db.execute(stmt)
        await db.commit()

    logger.info(
        "Updated task status | task_id=%s status=%s",
        task_id,
        new_status,
    )
    return {"task_id": task_id, "status": new_status}


# ─────────────────────────────────────────────────────────────────────────────
# Task cancellation
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="cancel_task")
async def cancel_task(
    task_id: str,
    workflow_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Cancel a task and publish a TASK_FAILED event with cancellation details.

    Args:
        task_id:    Task to cancel.
        workflow_id: Workflow this task belongs to.
        reason:     Optional cancellation reason.

    Returns:
        Dict with task_id and cancelled status.

    Raises:
        ApplicationError: If the cancellation fails.
    """
    now = datetime.now(timezone.utc)
    cancel_reason = reason or "Task cancelled by orchestrator"

    async with async_db_session() as db:
        stmt = (
            update(TaskORM)
            .where(TaskORM.task_id == task_id)
            .values(
                status=TaskStatus.CANCELLED.value,
                error_message=cancel_reason,
                completed_at=now,
                updated_at=now,
            )
        )
        await db.execute(stmt)
        await db.commit()

    # Publish TASK_FAILED event (cancellation is treated as a failure variant)
    payload = TaskFailedPayload(
        task_id=task_id,
        workflow_id=workflow_id,
        error_message=cancel_reason,
        will_retry=False,
    )
    event = build_event(
        event_type=EventType.TASK_FAILED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Cancelled task | task_id=%s workflow_id=%s reason=%s",
        task_id,
        workflow_id,
        cancel_reason,
    )
    return {"task_id": task_id, "status": TaskStatus.CANCELLED.value}
