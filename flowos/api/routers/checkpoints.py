"""
api/routers/checkpoints.py — FlowOS Checkpoint REST Endpoints

Provides read and management endpoints for FlowOS checkpoints.

Endpoints:
    GET    /checkpoints                        — List checkpoints (paginated, filterable)
    GET    /checkpoints/{checkpoint_id}        — Get a single checkpoint
    GET    /checkpoints/{checkpoint_id}/files  — List files changed in a checkpoint
    POST   /checkpoints/{checkpoint_id}/revert — Revert a task to this checkpoint
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.dependencies import DBSession, KafkaProducer, OptionalAgentID, Pagination
from shared.db.models import CheckpointFileORM, CheckpointORM
from shared.kafka.schemas import CheckpointRevertedPayload, build_event
from shared.models.event import EventSource, EventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])


# ─────────────────────────────────────────────────────────────────────────────
# Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class CheckpointFileResponse(BaseModel):
    """API response schema for a checkpoint file record."""

    file_id: str
    checkpoint_id: str
    path: str
    status: str
    old_path: str | None
    size_bytes: int | None
    checksum: str | None

    model_config = {"from_attributes": True}


class CheckpointResponse(BaseModel):
    """API response schema for a checkpoint record."""

    checkpoint_id: str
    task_id: str
    workflow_id: str
    agent_id: str
    workspace_id: str
    checkpoint_type: str
    status: str
    sequence: int
    git_commit_sha: str | None
    git_branch: str
    git_tag: str | None
    message: str
    file_count: int
    lines_added: int
    lines_removed: int
    s3_archive_key: str | None
    task_progress: float | None
    notes: str | None
    checkpoint_metadata: dict[str, Any]
    created_at: datetime
    verified_at: datetime | None
    reverted_at: datetime | None

    model_config = {"from_attributes": True}


class CheckpointListResponse(BaseModel):
    """Paginated list of checkpoints."""

    items: list[CheckpointResponse]
    total: int
    limit: int
    offset: int


class CheckpointRevertRequest(BaseModel):
    """Request body for reverting to a checkpoint."""

    agent_id: str = Field(description="Agent ID performing the revert.")
    reason: str | None = Field(default=None, description="Reason for reverting.")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(cp: CheckpointORM) -> CheckpointResponse:
    """Convert a CheckpointORM instance to a CheckpointResponse."""
    return CheckpointResponse(
        checkpoint_id=cp.checkpoint_id,
        task_id=cp.task_id,
        workflow_id=cp.workflow_id,
        agent_id=cp.agent_id,
        workspace_id=cp.workspace_id,
        checkpoint_type=cp.checkpoint_type,
        status=cp.status,
        sequence=cp.sequence,
        git_commit_sha=cp.git_commit_sha,
        git_branch=cp.git_branch,
        git_tag=cp.git_tag,
        message=cp.message,
        file_count=cp.file_count,
        lines_added=cp.lines_added,
        lines_removed=cp.lines_removed,
        s3_archive_key=cp.s3_archive_key,
        task_progress=cp.task_progress,
        notes=cp.notes,
        checkpoint_metadata=cp.checkpoint_metadata or {},
        created_at=cp.created_at,
        verified_at=cp.verified_at,
        reverted_at=cp.reverted_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /checkpoints — List checkpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=CheckpointListResponse,
    summary="List checkpoints",
    description=(
        "Returns a paginated list of checkpoints, optionally filtered by "
        "task, workflow, agent, or status."
    ),
)
async def list_checkpoints(
    db: DBSession,
    pagination: Pagination,
    task_id: str | None = Query(default=None, description="Filter by task ID."),
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID."),
    agent_id: str | None = Query(default=None, description="Filter by agent ID."),
    checkpoint_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by checkpoint status.",
    ),
    checkpoint_type: str | None = Query(default=None, description="Filter by checkpoint type."),
) -> CheckpointListResponse:
    """List checkpoints with optional filters."""
    stmt = select(CheckpointORM).order_by(CheckpointORM.created_at.desc())

    if task_id:
        stmt = stmt.where(CheckpointORM.task_id == task_id)
    if workflow_id:
        stmt = stmt.where(CheckpointORM.workflow_id == workflow_id)
    if agent_id:
        stmt = stmt.where(CheckpointORM.agent_id == agent_id)
    if checkpoint_status:
        stmt = stmt.where(CheckpointORM.status == checkpoint_status)
    if checkpoint_type:
        stmt = stmt.where(CheckpointORM.checkpoint_type == checkpoint_type)

    # Count total
    count_stmt = stmt.order_by(None)
    total_result = await db.execute(count_stmt)
    total = len(total_result.scalars().all())

    # Apply pagination
    stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(stmt)
    checkpoints = result.scalars().all()

    return CheckpointListResponse(
        items=[_orm_to_response(cp) for cp in checkpoints],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /checkpoints/{checkpoint_id} — Get a single checkpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{checkpoint_id}",
    response_model=CheckpointResponse,
    summary="Get a checkpoint",
    description="Returns the full state of a single checkpoint.",
)
async def get_checkpoint(
    checkpoint_id: str,
    db: DBSession,
) -> CheckpointResponse:
    """Retrieve a checkpoint by ID."""
    cp = await db.get(CheckpointORM, checkpoint_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Checkpoint {checkpoint_id!r} not found.",
        )
    return _orm_to_response(cp)


# ─────────────────────────────────────────────────────────────────────────────
# GET /checkpoints/{checkpoint_id}/files — List files in a checkpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{checkpoint_id}/files",
    response_model=list[CheckpointFileResponse],
    summary="List files changed in a checkpoint",
    description=(
        "Returns the list of files that were added, modified, or deleted "
        "as part of this checkpoint."
    ),
)
async def list_checkpoint_files(
    checkpoint_id: str,
    db: DBSession,
) -> list[CheckpointFileResponse]:
    """List files changed in a checkpoint."""
    cp = await db.get(CheckpointORM, checkpoint_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Checkpoint {checkpoint_id!r} not found.",
        )

    stmt = (
        select(CheckpointFileORM)
        .where(CheckpointFileORM.checkpoint_id == checkpoint_id)
        .order_by(CheckpointFileORM.path.asc())
    )
    result = await db.execute(stmt)
    files = result.scalars().all()

    return [
        CheckpointFileResponse(
            file_id=f.file_id,
            checkpoint_id=f.checkpoint_id,
            path=f.path,
            status=f.status,
            old_path=f.old_path,
            size_bytes=f.size_bytes,
            checksum=f.checksum,
        )
        for f in files
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST /checkpoints/{checkpoint_id}/revert — Revert to a checkpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{checkpoint_id}/revert",
    response_model=CheckpointResponse,
    summary="Revert to a checkpoint",
    description=(
        "Marks a checkpoint as reverted and publishes a CHECKPOINT_REVERTED "
        "event to the Kafka event bus.  The workspace manager handles the "
        "actual Git revert operation."
    ),
)
async def revert_checkpoint(
    checkpoint_id: str,
    body: CheckpointRevertRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> CheckpointResponse:
    """Revert a task to a specific checkpoint."""
    cp = await db.get(CheckpointORM, checkpoint_id)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Checkpoint {checkpoint_id!r} not found.",
        )

    if cp.status not in ("committed", "verified"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Can only revert to a COMMITTED or VERIFIED checkpoint, "
                f"current status: {cp.status!r}."
            ),
        )

    cp.status = "reverted"
    from datetime import timezone
    cp.reverted_at = datetime.now(timezone.utc)
    await db.flush()

    # Publish CHECKPOINT_REVERTED event
    try:
        payload = CheckpointRevertedPayload(
            checkpoint_id=checkpoint_id,
            task_id=cp.task_id,
            workspace_id=cp.workspace_id,
            agent_id=body.agent_id,
        )
        event = build_event(
            event_type=EventType.CHECKPOINT_REVERTED,
            payload=payload,
            source=EventSource.API,
            workflow_id=cp.workflow_id,
            task_id=cp.task_id,
            agent_id=body.agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish CHECKPOINT_REVERTED event: %s", exc)

    await db.refresh(cp)
    return _orm_to_response(cp)
