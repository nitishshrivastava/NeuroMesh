"""
api/routers/tasks.py — FlowOS Task REST Endpoints

Provides CRUD and lifecycle management endpoints for FlowOS tasks.

Endpoints:
    GET    /tasks                      — List tasks (paginated, filterable)
    GET    /tasks/{task_id}            — Get a single task
    PATCH  /tasks/{task_id}            — Update task metadata / status
    POST   /tasks/{task_id}/accept     — Agent accepts a task assignment
    POST   /tasks/{task_id}/start      — Agent starts executing a task
    POST   /tasks/{task_id}/complete   — Agent marks a task as complete
    POST   /tasks/{task_id}/fail       — Agent marks a task as failed
    POST   /tasks/{task_id}/cancel     — Cancel a task
    GET    /tasks/{task_id}/attempts   — List execution attempts for a task
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import DBSession, KafkaProducer, OptionalAgentID, Pagination, RequiredAgentID
from shared.db.models import TaskAttemptORM, TaskORM
from shared.kafka.schemas import (
    TaskAcceptedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    TaskStartedPayload,
    TaskUpdatedPayload,
    build_event,
)
from shared.models.event import EventSource, EventType
from shared.models.task import TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class TaskUpdateRequest(BaseModel):
    """Request body for updating task metadata."""

    description: str | None = Field(default=None)
    priority: str | None = Field(default=None)
    tags: list[str] | None = Field(default=None)
    due_at: datetime | None = Field(default=None)
    task_metadata: dict[str, str] | None = Field(default=None)


class TaskAcceptRequest(BaseModel):
    """Request body for accepting a task."""

    agent_id: str = Field(description="Agent ID accepting the task.")


class TaskStartRequest(BaseModel):
    """Request body for starting a task."""

    agent_id: str = Field(description="Agent ID starting the task.")
    attempt_number: int = Field(default=1, ge=1, description="Execution attempt number.")


class TaskCompleteRequest(BaseModel):
    """Request body for completing a task."""

    agent_id: str = Field(description="Agent ID completing the task.")
    outputs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Output parameters produced by this task.",
    )
    message: str | None = Field(default=None, description="Optional completion message.")


class TaskFailRequest(BaseModel):
    """Request body for failing a task."""

    agent_id: str = Field(description="Agent ID reporting the failure.")
    error_message: str = Field(description="Human-readable error description.")
    error_details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured error details.",
    )


class TaskCancelRequest(BaseModel):
    """Request body for cancelling a task."""

    reason: str | None = Field(default=None, description="Cancellation reason.")


class TaskResponse(BaseModel):
    """API response schema for a task record."""

    task_id: str
    workflow_id: str
    step_id: str | None
    name: str
    description: str | None
    task_type: str
    status: str
    priority: str
    assigned_agent_id: str | None
    previous_agent_id: str | None
    workspace_id: str | None
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    depends_on: list[str]
    timeout_secs: int
    retry_count: int
    current_retry: int
    error_message: str | None
    error_details: dict[str, Any]
    tags: list[str]
    task_metadata: dict[str, str]
    created_at: datetime
    assigned_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    due_at: datetime | None

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    """Paginated list of tasks."""

    items: list[TaskResponse]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(task: TaskORM) -> TaskResponse:
    """Convert a TaskORM instance to a TaskResponse."""
    return TaskResponse(
        task_id=task.task_id,
        workflow_id=task.workflow_id,
        step_id=task.step_id,
        name=task.name,
        description=task.description,
        task_type=task.task_type,
        status=task.status,
        priority=task.priority,
        assigned_agent_id=task.assigned_agent_id,
        previous_agent_id=task.previous_agent_id,
        workspace_id=task.workspace_id,
        inputs=task.inputs or [],
        outputs=task.outputs or [],
        depends_on=task.depends_on or [],
        timeout_secs=task.timeout_secs,
        retry_count=task.retry_count,
        current_retry=task.current_retry,
        error_message=task.error_message,
        error_details=task.error_details or {},
        tags=task.tags or [],
        task_metadata=task.task_metadata or {},
        created_at=task.created_at,
        assigned_at=task.assigned_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        updated_at=task.updated_at,
        due_at=task.due_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /tasks — List tasks
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List tasks",
    description="Returns a paginated list of tasks, optionally filtered by status, type, agent, or workflow.",
)
async def list_tasks(
    db: DBSession,
    pagination: Pagination,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by task status.",
    ),
    task_type: str | None = Query(default=None, description="Filter by task type."),
    assigned_agent_id: str | None = Query(default=None, description="Filter by assigned agent."),
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID."),
    priority: str | None = Query(default=None, description="Filter by priority."),
) -> TaskListResponse:
    """List tasks with optional filters."""
    stmt = select(TaskORM).order_by(TaskORM.created_at.desc())

    if status_filter:
        stmt = stmt.where(TaskORM.status == status_filter)
    if task_type:
        stmt = stmt.where(TaskORM.task_type == task_type)
    if assigned_agent_id:
        stmt = stmt.where(TaskORM.assigned_agent_id == assigned_agent_id)
    if workflow_id:
        stmt = stmt.where(TaskORM.workflow_id == workflow_id)
    if priority:
        stmt = stmt.where(TaskORM.priority == priority)

    # Count total
    count_stmt = stmt.order_by(None)
    total_result = await db.execute(count_stmt)
    total = len(total_result.scalars().all())

    # Apply pagination
    stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(stmt)
    tasks = result.scalars().all()

    return TaskListResponse(
        items=[_orm_to_response(t) for t in tasks],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /tasks/{task_id} — Get a single task
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Get a task",
    description="Returns the full state of a single task.",
)
async def get_task(
    task_id: str,
    db: DBSession,
) -> TaskResponse:
    """Retrieve a task by ID."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /tasks/{task_id} — Update task metadata
# ─────────────────────────────────────────────────────────────────────────────


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update task metadata",
    description="Updates mutable metadata fields on a task.",
)
async def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    db: DBSession,
    producer: KafkaProducer,
    agent_id: OptionalAgentID,
) -> TaskResponse:
    """Update mutable fields on a task."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    if body.description is not None:
        task.description = body.description
    if body.priority is not None:
        task.priority = body.priority
    if body.tags is not None:
        task.tags = body.tags
    if body.due_at is not None:
        task.due_at = body.due_at
    if body.task_metadata is not None:
        task.task_metadata = body.task_metadata

    await db.flush()

    # Publish TASK_UPDATED event
    try:
        payload = TaskUpdatedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=agent_id or task.assigned_agent_id or "system",
            update_type="metadata",
            message="Task metadata updated via API.",
        )
        event = build_event(
            event_type=EventType.TASK_UPDATED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_UPDATED event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# POST /tasks/{task_id}/accept — Agent accepts a task
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/accept",
    response_model=TaskResponse,
    summary="Accept a task assignment",
    description=(
        "Called by an agent to accept a task that has been assigned to them. "
        "Transitions the task from ASSIGNED to ACCEPTED and publishes a "
        "TASK_ACCEPTED event."
    ),
)
async def accept_task(
    task_id: str,
    body: TaskAcceptRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> TaskResponse:
    """Agent accepts a task assignment."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    if task.status != TaskStatus.ASSIGNED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only accept an ASSIGNED task, current status: {task.status!r}.",
        )

    if task.assigned_agent_id and task.assigned_agent_id != body.agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Task {task_id!r} is assigned to agent {task.assigned_agent_id!r}, "
                f"not {body.agent_id!r}."
            ),
        )

    task.status = TaskStatus.ACCEPTED
    await db.flush()

    try:
        payload = TaskAcceptedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=body.agent_id,
        )
        event = build_event(
            event_type=EventType.TASK_ACCEPTED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_ACCEPTED event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# POST /tasks/{task_id}/start — Agent starts executing a task
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/start",
    response_model=TaskResponse,
    summary="Start executing a task",
    description=(
        "Called by an agent to signal that they have begun executing a task. "
        "Transitions the task to IN_PROGRESS and publishes a TASK_STARTED event."
    ),
)
async def start_task(
    task_id: str,
    body: TaskStartRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> TaskResponse:
    """Agent starts executing a task."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    if task.status not in (TaskStatus.ACCEPTED, TaskStatus.CHECKPOINTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only start an ACCEPTED or CHECKPOINTED task, "
                f"current status: {task.status!r}."
            ),
        )

    task.status = TaskStatus.IN_PROGRESS
    task.started_at = task.started_at or datetime.now(timezone.utc)
    await db.flush()

    try:
        payload = TaskStartedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=body.agent_id,
            attempt_number=body.attempt_number,
        )
        event = build_event(
            event_type=EventType.TASK_STARTED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_STARTED event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# POST /tasks/{task_id}/complete — Agent completes a task
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/complete",
    response_model=TaskResponse,
    summary="Complete a task",
    description=(
        "Called by an agent to mark a task as completed.  Stores the output "
        "parameters and publishes a TASK_COMPLETED event."
    ),
)
async def complete_task(
    task_id: str,
    body: TaskCompleteRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> TaskResponse:
    """Agent marks a task as completed."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.CHECKPOINTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only complete an IN_PROGRESS or CHECKPOINTED task, "
                f"current status: {task.status!r}."
            ),
        )

    now = datetime.now(timezone.utc)
    task.status = TaskStatus.COMPLETED
    task.outputs = body.outputs
    task.completed_at = now
    await db.flush()

    try:
        payload = TaskCompletedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=body.agent_id,
            outputs=body.outputs,
        )
        event = build_event(
            event_type=EventType.TASK_COMPLETED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_COMPLETED event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# POST /tasks/{task_id}/fail — Agent reports task failure
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/fail",
    response_model=TaskResponse,
    summary="Fail a task",
    description=(
        "Called by an agent to report that a task has failed.  Stores the error "
        "details and publishes a TASK_FAILED event."
    ),
)
async def fail_task(
    task_id: str,
    body: TaskFailRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> TaskResponse:
    """Agent reports a task failure."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.CHECKPOINTED, TaskStatus.ACCEPTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot fail a task in status: {task.status!r}.",
        )

    now = datetime.now(timezone.utc)
    task.status = TaskStatus.FAILED
    task.error_message = body.error_message
    task.error_details = body.error_details
    task.completed_at = now
    await db.flush()

    try:
        payload = TaskFailedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=body.agent_id,
            error_message=body.error_message,
            error_details=body.error_details,
        )
        event = build_event(
            event_type=EventType.TASK_FAILED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_FAILED event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# POST /tasks/{task_id}/cancel — Cancel a task
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{task_id}/cancel",
    response_model=TaskResponse,
    summary="Cancel a task",
    description="Cancels a task that is not yet in a terminal state.",
)
async def cancel_task(
    task_id: str,
    body: TaskCancelRequest,
    db: DBSession,
    producer: KafkaProducer,
    agent_id: OptionalAgentID,
) -> TaskResponse:
    """Cancel a task."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    terminal_statuses = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
        TaskStatus.REVERTED,
    }
    if task.status in terminal_statuses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel a task in terminal state: {task.status!r}.",
        )

    now = datetime.now(timezone.utc)
    task.status = TaskStatus.CANCELLED
    task.completed_at = now
    await db.flush()

    try:
        payload = TaskUpdatedPayload(
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=agent_id or task.assigned_agent_id or "system",
            update_type="cancelled",
            message=body.reason or "Task cancelled via API.",
        )
        event = build_event(
            event_type=EventType.TASK_UPDATED,
            payload=payload,
            source=EventSource.API,
            workflow_id=task.workflow_id,
            task_id=task_id,
            agent_id=agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_UPDATED (cancel) event: %s", exc)

    await db.refresh(task)
    return _orm_to_response(task)


# ─────────────────────────────────────────────────────────────────────────────
# GET /tasks/{task_id}/attempts — List execution attempts
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{task_id}/attempts",
    summary="List task execution attempts",
    description="Returns all execution attempts for a task, ordered by attempt number.",
)
async def list_task_attempts(
    task_id: str,
    db: DBSession,
) -> dict[str, Any]:
    """List execution attempts for a task."""
    task = await db.get(TaskORM, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found.",
        )

    stmt = (
        select(TaskAttemptORM)
        .where(TaskAttemptORM.task_id == task_id)
        .order_by(TaskAttemptORM.attempt_number.asc())
    )
    result = await db.execute(stmt)
    attempts = result.scalars().all()

    return {
        "task_id": task_id,
        "items": [a.to_dict() for a in attempts],
        "total": len(attempts),
    }
