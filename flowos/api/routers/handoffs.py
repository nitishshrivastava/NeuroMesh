"""
api/routers/handoffs.py — FlowOS Handoff REST Endpoints

Provides endpoints for managing task ownership transfers between agents.

Endpoints:
    POST   /handoffs                       — Request a task handoff
    GET    /handoffs                       — List handoffs (paginated, filterable)
    GET    /handoffs/{handoff_id}          — Get a single handoff
    POST   /handoffs/{handoff_id}/accept   — Target agent accepts a handoff
    POST   /handoffs/{handoff_id}/reject   — Target agent rejects a handoff
    POST   /handoffs/{handoff_id}/complete — Mark a handoff as completed
    POST   /handoffs/{handoff_id}/cancel   — Cancel a pending handoff
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.dependencies import DBSession, KafkaProducer, OptionalAgentID, Pagination
from shared.db.models import HandoffORM, TaskORM
from shared.kafka.schemas import (
    HandoffPreparedPayload,
    TaskHandoffAcceptedPayload,
    TaskHandoffRequestedPayload,
    TaskUpdatedPayload,
    build_event,
)
from shared.models.event import EventSource, EventType
from shared.models.handoff import HandoffStatus, HandoffType
from shared.models.task import TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/handoffs", tags=["handoffs"])


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class HandoffCreateRequest(BaseModel):
    """Request body for requesting a task handoff."""

    task_id: str = Field(description="Task ID to hand off.")
    workflow_id: str = Field(description="Workflow ID.")
    source_agent_id: str = Field(description="Agent requesting the handoff.")
    target_agent_id: str | None = Field(
        default=None,
        description="Target agent ID (optional — can be assigned later).",
    )
    handoff_type: HandoffType = Field(
        default=HandoffType.DELEGATION,
        description="Reason for the handoff.",
    )
    checkpoint_id: str | None = Field(
        default=None,
        description="Checkpoint ID capturing the current state.",
    )
    workspace_id: str | None = Field(default=None)
    context: dict[str, Any] | None = Field(
        default=None,
        description="Briefing context for the target agent.",
    )
    priority: str = Field(default="normal")
    tags: list[str] = Field(default_factory=list)
    expires_at: datetime | None = Field(
        default=None,
        description="Optional expiry time for the handoff request.",
    )


class HandoffAcceptRequest(BaseModel):
    """Request body for accepting a handoff."""

    agent_id: str = Field(description="Agent ID accepting the handoff.")


class HandoffRejectRequest(BaseModel):
    """Request body for rejecting a handoff."""

    agent_id: str = Field(description="Agent ID rejecting the handoff.")
    reason: str | None = Field(default=None, description="Rejection reason.")


class HandoffCancelRequest(BaseModel):
    """Request body for cancelling a handoff."""

    reason: str | None = Field(default=None, description="Cancellation reason.")


class HandoffResponse(BaseModel):
    """API response schema for a handoff record."""

    handoff_id: str
    task_id: str
    workflow_id: str
    source_agent_id: str
    target_agent_id: str | None
    handoff_type: str
    status: str
    checkpoint_id: str | None
    workspace_id: str | None
    context: dict[str, Any] | None
    rejection_reason: str | None
    failure_reason: str | None
    priority: str
    expires_at: datetime | None
    accepted_at: datetime | None
    completed_at: datetime | None
    tags: list[str]
    handoff_metadata: dict[str, Any]
    requested_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HandoffListResponse(BaseModel):
    """Paginated list of handoffs."""

    items: list[HandoffResponse]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(h: HandoffORM) -> HandoffResponse:
    """Convert a HandoffORM instance to a HandoffResponse."""
    return HandoffResponse(
        handoff_id=h.handoff_id,
        task_id=h.task_id,
        workflow_id=h.workflow_id,
        source_agent_id=h.source_agent_id,
        target_agent_id=h.target_agent_id,
        handoff_type=h.handoff_type,
        status=h.status,
        checkpoint_id=h.checkpoint_id,
        workspace_id=h.workspace_id,
        context=h.context,
        rejection_reason=h.rejection_reason,
        failure_reason=h.failure_reason,
        priority=h.priority,
        expires_at=h.expires_at,
        accepted_at=h.accepted_at,
        completed_at=h.completed_at,
        tags=h.tags or [],
        handoff_metadata=h.handoff_metadata or {},
        requested_at=h.requested_at,
        updated_at=h.updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /handoffs — Request a task handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=HandoffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request a task handoff",
    description=(
        "Creates a handoff request to transfer task ownership from one agent "
        "to another.  Publishes a TASK_HANDOFF_REQUESTED event and a "
        "HANDOFF_PREPARED event to the Kafka event bus."
    ),
)
async def create_handoff(
    body: HandoffCreateRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> HandoffResponse:
    """Request a task handoff."""
    # Verify task exists
    task = await db.get(TaskORM, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {body.task_id!r} not found.",
        )

    # Task must be in an active state to be handed off
    active_statuses = {
        TaskStatus.IN_PROGRESS,
        TaskStatus.ACCEPTED,
        TaskStatus.CHECKPOINTED,
    }
    if task.status not in active_statuses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only hand off a task in an active state "
                f"(in_progress, accepted, checkpointed), current: {task.status!r}."
            ),
        )

    handoff_id = str(uuid.uuid4())

    handoff = HandoffORM(
        handoff_id=handoff_id,
        task_id=body.task_id,
        workflow_id=body.workflow_id,
        source_agent_id=body.source_agent_id,
        target_agent_id=body.target_agent_id,
        handoff_type=body.handoff_type,
        status=HandoffStatus.REQUESTED,
        checkpoint_id=body.checkpoint_id,
        workspace_id=body.workspace_id,
        context=body.context,
        priority=body.priority,
        tags=body.tags,
        expires_at=body.expires_at,
        handoff_metadata={},
    )
    db.add(handoff)

    # Update task status to handoff_requested
    task.status = TaskStatus.HANDOFF_REQUESTED
    await db.flush()

    # Publish TASK_HANDOFF_REQUESTED event
    try:
        task_payload = TaskHandoffRequestedPayload(
            task_id=body.task_id,
            workflow_id=body.workflow_id,
            from_agent_id=body.source_agent_id,
            to_agent_id=body.target_agent_id or "",
            handoff_id=handoff_id,
            reason=body.context.get("reason") if body.context else None,
        )
        task_event = build_event(
            event_type=EventType.TASK_HANDOFF_REQUESTED,
            payload=task_payload,
            source=EventSource.API,
            workflow_id=body.workflow_id,
            task_id=body.task_id,
            agent_id=body.source_agent_id,
        )
        producer.produce(task_event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_HANDOFF_REQUESTED event: %s", exc)

    # Publish HANDOFF_PREPARED event
    try:
        ws_payload = HandoffPreparedPayload(
            handoff_id=handoff_id,
            task_id=body.task_id,
            workspace_id=body.workspace_id or "",
            from_agent_id=body.source_agent_id,
            to_agent_id=body.target_agent_id or "",
        )
        ws_event = build_event(
            event_type=EventType.HANDOFF_PREPARED,
            payload=ws_payload,
            source=EventSource.API,
            workflow_id=body.workflow_id,
            task_id=body.task_id,
            agent_id=body.source_agent_id,
        )
        producer.produce(ws_event)
    except Exception as exc:
        logger.warning("Failed to publish HANDOFF_PREPARED event: %s", exc)

    await db.refresh(handoff)
    logger.info(
        "Created handoff %s for task %s (%s → %s)",
        handoff_id,
        body.task_id,
        body.source_agent_id,
        body.target_agent_id,
    )
    return _orm_to_response(handoff)


# ─────────────────────────────────────────────────────────────────────────────
# GET /handoffs — List handoffs
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=HandoffListResponse,
    summary="List handoffs",
    description="Returns a paginated list of handoffs, optionally filtered by task, agent, or status.",
)
async def list_handoffs(
    db: DBSession,
    pagination: Pagination,
    task_id: str | None = Query(default=None, description="Filter by task ID."),
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID."),
    source_agent_id: str | None = Query(default=None, description="Filter by source agent."),
    target_agent_id: str | None = Query(default=None, description="Filter by target agent."),
    handoff_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by handoff status.",
    ),
) -> HandoffListResponse:
    """List handoffs with optional filters."""
    stmt = select(HandoffORM).order_by(HandoffORM.requested_at.desc())

    if task_id:
        stmt = stmt.where(HandoffORM.task_id == task_id)
    if workflow_id:
        stmt = stmt.where(HandoffORM.workflow_id == workflow_id)
    if source_agent_id:
        stmt = stmt.where(HandoffORM.source_agent_id == source_agent_id)
    if target_agent_id:
        stmt = stmt.where(HandoffORM.target_agent_id == target_agent_id)
    if handoff_status:
        stmt = stmt.where(HandoffORM.status == handoff_status)

    # Count total
    count_stmt = stmt.order_by(None)
    total_result = await db.execute(count_stmt)
    total = len(total_result.scalars().all())

    # Apply pagination
    stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(stmt)
    handoffs = result.scalars().all()

    return HandoffListResponse(
        items=[_orm_to_response(h) for h in handoffs],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /handoffs/{handoff_id} — Get a single handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{handoff_id}",
    response_model=HandoffResponse,
    summary="Get a handoff",
    description="Returns the full state of a single handoff.",
)
async def get_handoff(
    handoff_id: str,
    db: DBSession,
) -> HandoffResponse:
    """Retrieve a handoff by ID."""
    h = await db.get(HandoffORM, handoff_id)
    if h is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Handoff {handoff_id!r} not found.",
        )
    return _orm_to_response(h)


# ─────────────────────────────────────────────────────────────────────────────
# POST /handoffs/{handoff_id}/accept — Accept a handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{handoff_id}/accept",
    response_model=HandoffResponse,
    summary="Accept a handoff",
    description=(
        "Called by the target agent to accept a handoff request.  "
        "Transitions the handoff to ACCEPTED and publishes a "
        "TASK_HANDOFF_ACCEPTED event."
    ),
)
async def accept_handoff(
    handoff_id: str,
    body: HandoffAcceptRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> HandoffResponse:
    """Target agent accepts a handoff."""
    h = await db.get(HandoffORM, handoff_id)
    if h is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Handoff {handoff_id!r} not found.",
        )

    if h.status not in (HandoffStatus.REQUESTED, HandoffStatus.PENDING_ACCEPTANCE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only accept a REQUESTED or PENDING_ACCEPTANCE handoff, "
                f"current status: {h.status!r}."
            ),
        )

    now = datetime.now(timezone.utc)
    h.status = HandoffStatus.ACCEPTED
    h.target_agent_id = body.agent_id
    h.accepted_at = now

    # Update task status
    task = await db.get(TaskORM, h.task_id)
    if task is not None:
        task.status = TaskStatus.HANDOFF_ACCEPTED
        task.assigned_agent_id = body.agent_id

    await db.flush()

    # Publish TASK_HANDOFF_ACCEPTED event
    try:
        payload = TaskHandoffAcceptedPayload(
            task_id=h.task_id,
            workflow_id=h.workflow_id,
            handoff_id=handoff_id,
            new_agent_id=body.agent_id,
        )
        event = build_event(
            event_type=EventType.TASK_HANDOFF_ACCEPTED,
            payload=payload,
            source=EventSource.API,
            workflow_id=h.workflow_id,
            task_id=h.task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_HANDOFF_ACCEPTED event: %s", exc)

    await db.refresh(h)
    return _orm_to_response(h)


# ─────────────────────────────────────────────────────────────────────────────
# POST /handoffs/{handoff_id}/reject — Reject a handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{handoff_id}/reject",
    response_model=HandoffResponse,
    summary="Reject a handoff",
    description="Called by the target agent to reject a handoff request.",
)
async def reject_handoff(
    handoff_id: str,
    body: HandoffRejectRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> HandoffResponse:
    """Target agent rejects a handoff."""
    h = await db.get(HandoffORM, handoff_id)
    if h is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Handoff {handoff_id!r} not found.",
        )

    if h.status not in (HandoffStatus.REQUESTED, HandoffStatus.PENDING_ACCEPTANCE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only reject a REQUESTED or PENDING_ACCEPTANCE handoff, "
                f"current status: {h.status!r}."
            ),
        )

    h.status = HandoffStatus.REJECTED
    h.rejection_reason = body.reason

    # Revert task status to in_progress
    task = await db.get(TaskORM, h.task_id)
    if task is not None and task.status == TaskStatus.HANDOFF_REQUESTED:
        task.status = TaskStatus.IN_PROGRESS

    await db.flush()

    # Publish a TASK_UPDATED event to notify the source agent
    try:
        payload = TaskUpdatedPayload(
            task_id=h.task_id,
            workflow_id=h.workflow_id,
            agent_id=body.agent_id,
            update_type="handoff_rejected",
            message=body.reason or "Handoff rejected.",
        )
        event = build_event(
            event_type=EventType.TASK_UPDATED,
            payload=payload,
            source=EventSource.API,
            workflow_id=h.workflow_id,
            task_id=h.task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish TASK_UPDATED (handoff rejected) event: %s", exc)

    await db.refresh(h)
    return _orm_to_response(h)


# ─────────────────────────────────────────────────────────────────────────────
# POST /handoffs/{handoff_id}/complete — Complete a handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{handoff_id}/complete",
    response_model=HandoffResponse,
    summary="Complete a handoff",
    description="Marks a handoff as completed once the target agent has taken over.",
)
async def complete_handoff(
    handoff_id: str,
    db: DBSession,
    agent_id: OptionalAgentID,
) -> HandoffResponse:
    """Mark a handoff as completed."""
    h = await db.get(HandoffORM, handoff_id)
    if h is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Handoff {handoff_id!r} not found.",
        )

    if h.status != HandoffStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only complete an ACCEPTED handoff, current status: {h.status!r}.",
        )

    now = datetime.now(timezone.utc)
    h.status = HandoffStatus.COMPLETED
    h.completed_at = now
    await db.flush()

    await db.refresh(h)
    return _orm_to_response(h)


# ─────────────────────────────────────────────────────────────────────────────
# POST /handoffs/{handoff_id}/cancel — Cancel a handoff
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{handoff_id}/cancel",
    response_model=HandoffResponse,
    summary="Cancel a handoff",
    description="Cancels a pending handoff request.",
)
async def cancel_handoff(
    handoff_id: str,
    body: HandoffCancelRequest,
    db: DBSession,
    producer: KafkaProducer,
    agent_id: OptionalAgentID,
) -> HandoffResponse:
    """Cancel a pending handoff."""
    h = await db.get(HandoffORM, handoff_id)
    if h is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Handoff {handoff_id!r} not found.",
        )

    terminal_statuses = {
        HandoffStatus.COMPLETED,
        HandoffStatus.REJECTED,
        HandoffStatus.EXPIRED,
        HandoffStatus.FAILED,
        HandoffStatus.CANCELLED,
    }
    if h.status in terminal_statuses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel a handoff in terminal state: {h.status!r}.",
        )

    h.status = HandoffStatus.CANCELLED

    # Revert task status if it was in handoff_requested
    task = await db.get(TaskORM, h.task_id)
    if task is not None and task.status == TaskStatus.HANDOFF_REQUESTED:
        task.status = TaskStatus.IN_PROGRESS

    await db.flush()

    await db.refresh(h)
    return _orm_to_response(h)
