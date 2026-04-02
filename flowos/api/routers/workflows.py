"""
api/routers/workflows.py — FlowOS Workflow REST Endpoints

Provides CRUD and lifecycle management endpoints for FlowOS workflows.

Endpoints:
    POST   /workflows                  — Start a new workflow
    GET    /workflows                  — List workflows (paginated, filterable)
    GET    /workflows/{workflow_id}    — Get a single workflow
    PATCH  /workflows/{workflow_id}    — Update workflow metadata
    POST   /workflows/{workflow_id}/cancel  — Cancel a running workflow
    POST   /workflows/{workflow_id}/pause   — Pause a running workflow
    POST   /workflows/{workflow_id}/resume  — Resume a paused workflow
    GET    /workflows/{workflow_id}/tasks   — List tasks for a workflow
    GET    /workflows/{workflow_id}/events  — List events for a workflow
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.dependencies import (
    DBSession,
    OptionalAgentID,
    OptionalKafkaProducer,
    Pagination,
)
from shared.db.models import TaskORM, WorkflowORM
from shared.kafka.schemas import (
    WorkflowCancelledPayload,
    WorkflowCreatedPayload,
    WorkflowPausedPayload,
    WorkflowResumedPayload,
    build_event,
)
from shared.models.event import EventSource, EventType
from shared.models.workflow import WorkflowStatus, WorkflowTrigger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class WorkflowCreateRequest(BaseModel):
    """Request body for starting a new workflow."""

    name: str = Field(min_length=1, max_length=255, description="Human-readable workflow name.")
    definition: dict[str, Any] | None = Field(
        default=None,
        description="Parsed workflow DSL definition as JSON.",
    )
    trigger: WorkflowTrigger = Field(
        default=WorkflowTrigger.API,
        description="How this workflow was initiated.",
    )
    owner_agent_id: str | None = Field(
        default=None,
        description="Agent ID that owns this workflow run.",
    )
    project: str | None = Field(
        default=None,
        max_length=255,
        description="Project/namespace for this workflow run.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime input parameters.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering.",
    )
    workflow_id: str | None = Field(
        default=None,
        description="Explicit workflow ID (UUID). Auto-generated if omitted.",
    )


class WorkflowUpdateRequest(BaseModel):
    """Request body for updating workflow metadata."""

    project: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = Field(default=None)
    outputs: dict[str, Any] | None = Field(default=None)


class WorkflowCancelRequest(BaseModel):
    """Request body for cancelling a workflow."""

    reason: str | None = Field(default=None, description="Cancellation reason.")


class WorkflowPauseRequest(BaseModel):
    """Request body for pausing a workflow."""

    reason: str | None = Field(default=None, description="Reason for pausing.")


class WorkflowResponse(BaseModel):
    """API response schema for a workflow record."""

    workflow_id: str
    name: str
    status: str
    trigger: str
    definition: dict[str, Any] | None
    temporal_run_id: str | None
    temporal_workflow_id: str | None
    owner_agent_id: str | None
    project: str | None
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    error_message: str | None
    error_details: dict[str, Any]
    tags: list[str]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkflowListResponse(BaseModel):
    """Paginated list of workflows."""

    items: list[WorkflowResponse]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(wf: WorkflowORM) -> WorkflowResponse:
    """Convert a WorkflowORM instance to a WorkflowResponse."""
    return WorkflowResponse(
        workflow_id=wf.workflow_id,
        name=wf.name,
        status=wf.status,
        trigger=wf.trigger,
        definition=wf.definition,
        temporal_run_id=wf.temporal_run_id,
        temporal_workflow_id=wf.temporal_workflow_id,
        owner_agent_id=wf.owner_agent_id,
        project=wf.project,
        inputs=wf.inputs or {},
        outputs=wf.outputs or {},
        error_message=wf.error_message,
        error_details=wf.error_details or {},
        tags=wf.tags or [],
        created_at=wf.created_at,
        started_at=wf.started_at,
        completed_at=wf.completed_at,
        updated_at=wf.updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /workflows — Start a new workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new workflow",
    description=(
        "Creates a new workflow instance and publishes a WORKFLOW_CREATED event "
        "to the Kafka event bus.  The orchestrator picks up the event and begins "
        "durable execution via Temporal.  If Kafka is unavailable, the workflow "
        "is still persisted to the database and the event is skipped with a warning."
    ),
)
async def create_workflow(
    body: WorkflowCreateRequest,
    db: DBSession,
    producer: OptionalKafkaProducer,
    agent_id: OptionalAgentID,
) -> WorkflowResponse:
    """Start a new workflow from a definition payload."""
    workflow_id = body.workflow_id or str(uuid.uuid4())

    # Validate explicit workflow_id if provided
    if body.workflow_id:
        try:
            uuid.UUID(body.workflow_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"workflow_id must be a valid UUID, got: {body.workflow_id!r}",
            )

    # Check for duplicate workflow_id
    existing = await db.get(WorkflowORM, workflow_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workflow with ID {workflow_id!r} already exists.",
        )

    owner = body.owner_agent_id or agent_id

    # Persist the workflow record
    wf = WorkflowORM(
        workflow_id=workflow_id,
        name=body.name,
        status=WorkflowStatus.PENDING,
        trigger=body.trigger,
        definition=body.definition,
        owner_agent_id=owner,
        project=body.project,
        inputs=body.inputs,
        outputs={},
        error_message=None,
        error_details={},
        tags=body.tags,
    )
    db.add(wf)
    await db.flush()

    # Publish WORKFLOW_CREATED event (optional — Kafka may be unavailable)
    if producer is not None:
        try:
            payload = WorkflowCreatedPayload(
                workflow_id=workflow_id,
                name=body.name,
                trigger=body.trigger,
                owner_agent_id=owner,
                project=body.project,
                inputs=body.inputs,
                tags=body.tags,
            )
            evt = build_event(
                event_type=EventType.WORKFLOW_CREATED,
                payload=payload,
                source=EventSource.API,
                workflow_id=workflow_id,
                agent_id=owner,
            )
            producer.produce(evt)
        except Exception as exc:
            logger.warning(
                "Failed to publish WORKFLOW_CREATED event for workflow %s: %s",
                workflow_id,
                exc,
            )
            # Don't fail the request — the record is persisted
    else:
        logger.warning(
            "Kafka producer unavailable — WORKFLOW_CREATED event not published for workflow %s",
            workflow_id,
        )

    logger.info("Created workflow %s (%s)", workflow_id, body.name)
    return _orm_to_response(wf)


# ─────────────────────────────────────────────────────────────────────────────
# GET /workflows — List workflows
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=WorkflowListResponse,
    summary="List workflows",
    description="Returns a paginated list of workflow instances, optionally filtered by status, project, or owner.",
)
async def list_workflows(
    db: DBSession,
    pagination: Pagination,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by workflow status (pending, running, paused, completed, failed, cancelled).",
    ),
    project: str | None = Query(default=None, description="Filter by project name."),
    owner_agent_id: str | None = Query(default=None, description="Filter by owner agent ID."),
    tag: str | None = Query(default=None, description="Filter by tag (exact match)."),
) -> WorkflowListResponse:
    """List workflow instances with optional filters.

    Returns an empty list (HTTP 200) if the database is empty or unreachable,
    rather than propagating a 500 error.  This allows the UI sidebar to render
    an empty state instead of an error state when the system is first starting.
    """
    try:
        stmt = select(WorkflowORM).order_by(WorkflowORM.created_at.desc())

        if status_filter:
            stmt = stmt.where(WorkflowORM.status == status_filter)
        if project:
            stmt = stmt.where(WorkflowORM.project == project)
        if owner_agent_id:
            stmt = stmt.where(WorkflowORM.owner_agent_id == owner_agent_id)
        if tag:
            # JSONB array contains operator
            stmt = stmt.where(WorkflowORM.tags.contains([tag]))

        # Count total (without pagination)
        count_stmt = select(WorkflowORM).order_by(None)
        if status_filter:
            count_stmt = count_stmt.where(WorkflowORM.status == status_filter)
        if project:
            count_stmt = count_stmt.where(WorkflowORM.project == project)
        if owner_agent_id:
            count_stmt = count_stmt.where(WorkflowORM.owner_agent_id == owner_agent_id)
        if tag:
            count_stmt = count_stmt.where(WorkflowORM.tags.contains([tag]))

        total_result = await db.execute(count_stmt)
        total = len(total_result.scalars().all())

        # Apply pagination
        stmt = stmt.offset(pagination.offset).limit(pagination.limit)
        result = await db.execute(stmt)
        workflows = result.scalars().all()

        return WorkflowListResponse(
            items=[_orm_to_response(wf) for wf in workflows],
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
        )

    except Exception as exc:
        # Log the error but return an empty list rather than a 500.
        # This allows the UI to show an empty state while the DB is starting up
        # or when migrations have not yet been applied.
        logger.error(
            "Failed to list workflows (DB may be unavailable or migrations pending): %s: %s",
            type(exc).__name__,
            exc,
        )
        return WorkflowListResponse(
            items=[],
            total=0,
            limit=pagination.limit,
            offset=pagination.offset,
        )


# ─────────────────────────────────────────────────────────────────────────────
# GET /workflows/{workflow_id} — Get a single workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Get a workflow",
    description="Returns the full state of a single workflow instance.",
)
async def get_workflow(
    workflow_id: str,
    db: DBSession,
) -> WorkflowResponse:
    """Retrieve a workflow by ID."""
    try:
        wf = await db.get(WorkflowORM, workflow_id)
        if wf is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id!r} not found.",
            )
        return _orm_to_response(wf)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to retrieve workflow %s: %s: %s",
            workflow_id,
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is temporarily unavailable. Please try again shortly.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /workflows/{workflow_id} — Update workflow metadata
# ─────────────────────────────────────────────────────────────────────────────


@router.patch(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Update workflow metadata",
    description="Updates mutable metadata fields (project, tags, outputs) on a workflow.",
)
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    db: DBSession,
) -> WorkflowResponse:
    """Update mutable fields on a workflow."""
    wf = await db.get(WorkflowORM, workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id!r} not found.",
        )

    if body.project is not None:
        wf.project = body.project
    if body.tags is not None:
        wf.tags = body.tags
    if body.outputs is not None:
        wf.outputs = body.outputs

    await db.flush()
    await db.refresh(wf)
    return _orm_to_response(wf)


# ─────────────────────────────────────────────────────────────────────────────
# POST /workflows/{workflow_id}/cancel — Cancel a workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowResponse,
    summary="Cancel a workflow",
    description=(
        "Cancels a running or paused workflow.  Publishes a WORKFLOW_CANCELLED "
        "event to the Kafka event bus so the orchestrator can terminate the "
        "Temporal workflow.  If Kafka is unavailable, the status is still updated "
        "in the database."
    ),
)
async def cancel_workflow(
    workflow_id: str,
    body: WorkflowCancelRequest,
    db: DBSession,
    producer: OptionalKafkaProducer,
    agent_id: OptionalAgentID,
) -> WorkflowResponse:
    """Cancel a running or paused workflow."""
    wf = await db.get(WorkflowORM, workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id!r} not found.",
        )

    if wf.status in (
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel workflow in terminal state: {wf.status!r}.",
        )

    wf.status = WorkflowStatus.CANCELLED
    await db.flush()

    # Publish WORKFLOW_CANCELLED event (optional — Kafka may be unavailable)
    if producer is not None:
        try:
            payload = WorkflowCancelledPayload(
                workflow_id=workflow_id,
                name=wf.name,
                reason=body.reason,
                cancelled_by=agent_id,
            )
            evt = build_event(
                event_type=EventType.WORKFLOW_CANCELLED,
                payload=payload,
                source=EventSource.API,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            producer.produce(evt)
        except Exception as exc:
            logger.warning("Failed to publish WORKFLOW_CANCELLED event: %s", exc)
    else:
        logger.warning(
            "Kafka producer unavailable — WORKFLOW_CANCELLED event not published for workflow %s",
            workflow_id,
        )

    await db.refresh(wf)
    return _orm_to_response(wf)


# ─────────────────────────────────────────────────────────────────────────────
# POST /workflows/{workflow_id}/pause — Pause a workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{workflow_id}/pause",
    response_model=WorkflowResponse,
    summary="Pause a running workflow",
    description=(
        "Pauses a running workflow.  Publishes a WORKFLOW_PAUSED event so the "
        "orchestrator can signal the Temporal workflow to pause.  If Kafka is "
        "unavailable, the status is still updated in the database."
    ),
)
async def pause_workflow(
    workflow_id: str,
    body: WorkflowPauseRequest,
    db: DBSession,
    producer: OptionalKafkaProducer,
    agent_id: OptionalAgentID,
) -> WorkflowResponse:
    """Pause a running workflow."""
    wf = await db.get(WorkflowORM, workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id!r} not found.",
        )

    if wf.status != WorkflowStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only pause a RUNNING workflow, current status: {wf.status!r}.",
        )

    wf.status = WorkflowStatus.PAUSED
    await db.flush()

    if producer is not None:
        try:
            payload = WorkflowPausedPayload(
                workflow_id=workflow_id,
                name=wf.name,
                reason=body.reason,
                paused_by=agent_id,
            )
            evt = build_event(
                event_type=EventType.WORKFLOW_PAUSED,
                payload=payload,
                source=EventSource.API,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            producer.produce(evt)
        except Exception as exc:
            logger.warning("Failed to publish WORKFLOW_PAUSED event: %s", exc)
    else:
        logger.warning(
            "Kafka producer unavailable — WORKFLOW_PAUSED event not published for workflow %s",
            workflow_id,
        )

    await db.refresh(wf)
    return _orm_to_response(wf)


# ─────────────────────────────────────────────────────────────────────────────
# POST /workflows/{workflow_id}/resume — Resume a paused workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{workflow_id}/resume",
    response_model=WorkflowResponse,
    summary="Resume a paused workflow",
    description=(
        "Resumes a paused workflow.  Publishes a WORKFLOW_RESUMED event so the "
        "orchestrator can signal the Temporal workflow to continue.  If Kafka is "
        "unavailable, the status is still updated in the database."
    ),
)
async def resume_workflow(
    workflow_id: str,
    db: DBSession,
    producer: OptionalKafkaProducer,
    agent_id: OptionalAgentID,
) -> WorkflowResponse:
    """Resume a paused workflow."""
    wf = await db.get(WorkflowORM, workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id!r} not found.",
        )

    if wf.status != WorkflowStatus.PAUSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only resume a PAUSED workflow, current status: {wf.status!r}.",
        )

    wf.status = WorkflowStatus.RUNNING
    await db.flush()

    if producer is not None:
        try:
            payload = WorkflowResumedPayload(
                workflow_id=workflow_id,
                name=wf.name,
                resumed_by=agent_id,
            )
            evt = build_event(
                event_type=EventType.WORKFLOW_RESUMED,
                payload=payload,
                source=EventSource.API,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            producer.produce(evt)
        except Exception as exc:
            logger.warning("Failed to publish WORKFLOW_RESUMED event: %s", exc)
    else:
        logger.warning(
            "Kafka producer unavailable — WORKFLOW_RESUMED event not published for workflow %s",
            workflow_id,
        )

    await db.refresh(wf)
    return _orm_to_response(wf)


# ─────────────────────────────────────────────────────────────────────────────
# GET /workflows/{workflow_id}/tasks — List tasks for a workflow
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{workflow_id}/tasks",
    summary="List tasks for a workflow",
    description="Returns all tasks belonging to a workflow, optionally filtered by status.",
)
async def list_workflow_tasks(
    workflow_id: str,
    db: DBSession,
    pagination: Pagination,
    task_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by task status.",
    ),
) -> dict[str, Any]:
    """List tasks belonging to a workflow."""
    try:
        # Verify workflow exists
        wf = await db.get(WorkflowORM, workflow_id)
        if wf is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id!r} not found.",
            )

        stmt = (
            select(TaskORM)
            .where(TaskORM.workflow_id == workflow_id)
            .order_by(TaskORM.created_at.asc())
        )
        if task_status:
            stmt = stmt.where(TaskORM.status == task_status)

        result = await db.execute(stmt)
        tasks = result.scalars().all()

        # Apply pagination
        total = len(tasks)
        paginated = tasks[pagination.offset : pagination.offset + pagination.limit]

        return {
            "workflow_id": workflow_id,
            "items": [t.to_dict() for t in paginated],
            "total": total,
            "limit": pagination.limit,
            "offset": pagination.offset,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to list tasks for workflow %s: %s: %s",
            workflow_id,
            type(exc).__name__,
            exc,
        )
        return {
            "workflow_id": workflow_id,
            "items": [],
            "total": 0,
            "limit": pagination.limit,
            "offset": pagination.offset,
        }
