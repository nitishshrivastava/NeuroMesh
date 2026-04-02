"""
orchestrator/activities/checkpoint_activities.py — Checkpoint Management Activities

Temporal activities for creating, verifying, and reverting task checkpoints.
Checkpoints are durable, reversible snapshots of task execution state stored
as Git commits in the agent's workspace.

The orchestrator's role in checkpointing:
1. Record checkpoint metadata in PostgreSQL (this module)
2. Publish CHECKPOINT_CREATED events to Kafka (agents handle Git operations)
3. Verify checkpoint integrity when needed
4. Coordinate revert operations across the system

The actual Git operations are performed by the workspace manager (agent-side).
The orchestrator only manages the metadata and event coordination.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from temporalio import activity

from shared.db.session import async_db_session
from shared.db.models import CheckpointORM
from shared.models.checkpoint import CheckpointStatus, CheckpointType
from shared.models.event import EventSource, EventType
from shared.kafka.producer import get_producer
from shared.kafka.schemas import (
    build_event,
    CheckpointCreatedPayload,
    CheckpointRevertedPayload,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint creation
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="create_checkpoint_record")
async def create_checkpoint_record(
    task_id: str,
    workflow_id: str,
    agent_id: str,
    workspace_id: str,
    message: str,
    checkpoint_type: str = "manual",
    git_commit_sha: str | None = None,
    git_branch: str = "main",
    git_tag: str | None = None,
    sequence: int = 1,
    file_count: int = 0,
    lines_added: int = 0,
    lines_removed: int = 0,
    task_progress: float | None = None,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a checkpoint metadata record in the database and publish a
    CHECKPOINT_CREATED event.

    This activity records the checkpoint in PostgreSQL and notifies the
    system via Kafka.  The actual Git commit is performed by the agent's
    workspace manager before calling this activity.

    Args:
        task_id:         Task this checkpoint belongs to.
        workflow_id:     Workflow this checkpoint is associated with.
        agent_id:        Agent that created this checkpoint.
        workspace_id:    Workspace where this checkpoint was created.
        message:         Human-readable checkpoint description.
        checkpoint_type: Why this checkpoint was created (manual, auto, etc.).
        git_commit_sha:  Git commit SHA of this checkpoint.
        git_branch:      Git branch at checkpoint time.
        git_tag:         Optional Git tag applied to this commit.
        sequence:        Monotonically increasing sequence number within a task.
        file_count:      Total number of files changed.
        lines_added:     Total lines added.
        lines_removed:   Total lines removed.
        task_progress:   Estimated task completion percentage (0-100).
        notes:           Free-form notes from the agent.
        metadata:        Arbitrary key/value metadata.

    Returns:
        Dict with checkpoint_id, status, and git_commit_sha.

    Raises:
        ApplicationError: If the checkpoint cannot be created.
    """
    checkpoint_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    status = (
        CheckpointStatus.COMMITTED.value
        if git_commit_sha
        else CheckpointStatus.CREATING.value
    )

    # Persist to database
    async with async_db_session() as db:
        checkpoint_row = CheckpointORM(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            checkpoint_type=checkpoint_type,
            status=status,
            sequence=sequence,
            git_commit_sha=git_commit_sha,
            git_branch=git_branch,
            git_tag=git_tag,
            message=message,
            file_count=file_count,
            lines_added=lines_added,
            lines_removed=lines_removed,
            task_progress=task_progress,
            notes=notes,
            checkpoint_metadata=metadata or {},
            created_at=now,
        )
        db.add(checkpoint_row)
        await db.commit()

    # Publish CHECKPOINT_CREATED event (only if we have a commit SHA)
    if git_commit_sha:
        payload = CheckpointCreatedPayload(
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            git_commit_sha=git_commit_sha,
            branch=git_branch,
            message=message,
            checkpoint_type=checkpoint_type,
        )
        event = build_event(
            event_type=EventType.CHECKPOINT_CREATED,
            payload=payload,
            source=EventSource.ORCHESTRATOR,
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=agent_id,
        )
        producer = get_producer()
        producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Created checkpoint | checkpoint_id=%s task_id=%s agent_id=%s sha=%s",
        checkpoint_id,
        task_id,
        agent_id,
        git_commit_sha or "pending",
    )
    return {
        "checkpoint_id": checkpoint_id,
        "status": status,
        "git_commit_sha": git_commit_sha,
        "sequence": sequence,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint verification
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="verify_checkpoint")
async def verify_checkpoint(
    checkpoint_id: str,
    git_commit_sha: str | None = None,
) -> dict[str, Any]:
    """
    Mark a checkpoint as verified and optionally update its Git commit SHA.

    Called after the workspace manager confirms the Git commit is accessible
    and the workspace state is consistent.

    Args:
        checkpoint_id:  Checkpoint to verify.
        git_commit_sha: Git commit SHA to record (if not already set).

    Returns:
        Dict with checkpoint_id and verified status.

    Raises:
        ApplicationError: If the checkpoint is not found or cannot be verified.
    """
    now = datetime.now(timezone.utc)

    update_values: dict[str, Any] = {
        "status": CheckpointStatus.VERIFIED.value,
        "verified_at": now,
    }
    if git_commit_sha:
        update_values["git_commit_sha"] = git_commit_sha

    async with async_db_session() as db:
        stmt = (
            update(CheckpointORM)
            .where(CheckpointORM.checkpoint_id == checkpoint_id)
            .values(**update_values)
        )
        result = await db.execute(stmt)
        await db.commit()

        if result.rowcount == 0:
            raise ValueError(f"Checkpoint {checkpoint_id!r} not found.")

    logger.info(
        "Verified checkpoint | checkpoint_id=%s sha=%s",
        checkpoint_id,
        git_commit_sha or "unchanged",
    )
    return {
        "checkpoint_id": checkpoint_id,
        "status": CheckpointStatus.VERIFIED.value,
        "verified_at": now.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint revert
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="revert_to_checkpoint")
async def revert_to_checkpoint(
    checkpoint_id: str,
    task_id: str,
    workflow_id: str,
    agent_id: str,
    workspace_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Mark a checkpoint as reverted and publish a CHECKPOINT_REVERTED event.

    The actual Git revert operation is performed by the workspace manager.
    This activity records the revert in the database and notifies the system.

    Args:
        checkpoint_id: Checkpoint to revert to.
        task_id:       Task being reverted.
        workflow_id:   Workflow this revert is associated with.
        agent_id:      Agent performing the revert.
        workspace_id:  Workspace being reverted.
        reason:        Optional reason for the revert.

    Returns:
        Dict with checkpoint_id and reverted status.

    Raises:
        ApplicationError: If the revert cannot be recorded.
    """
    now = datetime.now(timezone.utc)

    async with async_db_session() as db:
        # Mark the checkpoint as reverted
        stmt = (
            update(CheckpointORM)
            .where(CheckpointORM.checkpoint_id == checkpoint_id)
            .values(
                status=CheckpointStatus.REVERTED.value,
                reverted_at=now,
            )
        )
        result = await db.execute(stmt)
        await db.commit()

        if result.rowcount == 0:
            raise ValueError(f"Checkpoint {checkpoint_id!r} not found.")

    # Publish CHECKPOINT_REVERTED event
    payload = CheckpointRevertedPayload(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    event = build_event(
        event_type=EventType.CHECKPOINT_REVERTED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Reverted to checkpoint | checkpoint_id=%s task_id=%s agent_id=%s",
        checkpoint_id,
        task_id,
        agent_id,
    )
    return {
        "checkpoint_id": checkpoint_id,
        "status": CheckpointStatus.REVERTED.value,
        "reverted_at": now.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint listing
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="list_task_checkpoints")
async def list_task_checkpoints(
    task_id: str,
    include_reverted: bool = False,
) -> list[dict[str, Any]]:
    """
    List all checkpoints for a task, ordered by sequence number.

    Args:
        task_id:          Task to list checkpoints for.
        include_reverted: If True, include reverted checkpoints.

    Returns:
        List of checkpoint dicts ordered by sequence number (ascending).
    """
    async with async_db_session() as db:
        stmt = select(CheckpointORM).where(CheckpointORM.task_id == task_id)

        if not include_reverted:
            stmt = stmt.where(
                CheckpointORM.status != CheckpointStatus.REVERTED.value
            )

        stmt = stmt.order_by(CheckpointORM.sequence)
        result = await db.execute(stmt)
        rows = result.scalars().all()

        checkpoints = []
        for row in rows:
            checkpoints.append({
                "checkpoint_id": row.checkpoint_id,
                "task_id": row.task_id,
                "workflow_id": row.workflow_id,
                "agent_id": row.agent_id,
                "workspace_id": row.workspace_id,
                "checkpoint_type": row.checkpoint_type,
                "status": row.status,
                "sequence": row.sequence,
                "git_commit_sha": row.git_commit_sha,
                "git_branch": row.git_branch,
                "message": row.message,
                "file_count": row.file_count,
                "task_progress": row.task_progress,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })

        logger.debug(
            "Listed checkpoints | task_id=%s count=%d",
            task_id,
            len(checkpoints),
        )
        return checkpoints
