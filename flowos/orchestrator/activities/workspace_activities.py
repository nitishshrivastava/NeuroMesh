"""
orchestrator/activities/workspace_activities.py — Workspace Management Activities

Temporal activities for provisioning, syncing, and archiving agent workspaces.
Workspaces are Git-backed local state boundaries for agents.

The orchestrator's role in workspace management:
1. Record workspace metadata in PostgreSQL
2. Publish workspace events to Kafka (agents handle actual Git operations)
3. Track workspace status and health
4. Coordinate workspace archival when tasks complete

The actual Git operations (clone, commit, push, etc.) are performed by the
workspace manager running alongside each agent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from temporalio import activity

from shared.db.session import async_db_session
from shared.db.models import WorkspaceORM
from shared.models.workspace import WorkspaceStatus, WorkspaceType
from shared.models.event import EventSource, EventType
from shared.kafka.producer import get_producer
from shared.kafka.schemas import build_event, WorkspaceCreatedPayload

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Workspace provisioning
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="provision_workspace")
async def provision_workspace(
    agent_id: str,
    task_id: str,
    workflow_id: str,
    workspace_type: str = "local",
    root_path: str | None = None,
    git_remote_url: str | None = None,
    branch: str = "main",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Provision a new workspace for an agent and publish a WORKSPACE_CREATED event.

    Creates a workspace metadata record in PostgreSQL and notifies the
    workspace manager via Kafka to initialise the actual Git repository.

    Args:
        agent_id:        Agent that will own this workspace.
        task_id:         Task this workspace is being created for.
        workflow_id:     Workflow this workspace is associated with.
        workspace_type:  Type of workspace (local, remote, ephemeral, shared).
        root_path:       Root directory path for the workspace.
        git_remote_url:  Optional remote Git repository URL.
        branch:          Default Git branch name.
        metadata:        Arbitrary key/value metadata.

    Returns:
        Dict with workspace_id, status, and root_path.

    Raises:
        ApplicationError: If the workspace cannot be provisioned.
    """
    workspace_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Derive root path if not provided
    if root_path is None:
        from shared.config import settings
        root_path = f"{settings.workspace_root}/{agent_id}/{workspace_id}"

    # Persist to database
    async with async_db_session() as db:
        workspace_row = WorkspaceORM(
            workspace_id=workspace_id,
            agent_id=agent_id,
            task_id=task_id,
            workflow_id=workflow_id,
            workspace_type=workspace_type,
            status=WorkspaceStatus.CREATING.value,
            root_path=root_path,
            repo_url=git_remote_url,
            git_state={"branch": branch, "commit_sha": None, "is_dirty": False},
            snapshots=[],
            workspace_metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        db.add(workspace_row)
        await db.commit()

    # Publish WORKSPACE_CREATED event
    payload = WorkspaceCreatedPayload(
        workspace_id=workspace_id,
        agent_id=agent_id,
        task_id=task_id,
        workflow_id=workflow_id,
        workspace_path=root_path,
        git_remote_url=git_remote_url,
        branch=branch,
    )
    event = build_event(
        event_type=EventType.WORKSPACE_CREATED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    producer = get_producer()
    producer.produce_sync(event, timeout=10.0)

    logger.info(
        "Provisioned workspace | workspace_id=%s agent_id=%s task_id=%s path=%s",
        workspace_id,
        agent_id,
        task_id,
        root_path,
    )
    return {
        "workspace_id": workspace_id,
        "status": WorkspaceStatus.CREATING.value,
        "root_path": root_path,
        "branch": branch,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Workspace sync
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="sync_workspace")
async def sync_workspace(
    workspace_id: str,
    agent_id: str,
    commit_sha: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """
    Update workspace metadata after a sync operation.

    Called after the workspace manager has synced the workspace (e.g. after
    a handoff where the new agent clones the workspace from a checkpoint).

    Args:
        workspace_id: Workspace that was synced.
        agent_id:     Agent that performed the sync.
        commit_sha:   HEAD commit SHA after sync.
        branch:       Current branch after sync.

    Returns:
        Dict with workspace_id and updated status.

    Raises:
        ApplicationError: If the workspace cannot be updated.
    """
    now = datetime.now(timezone.utc)

    # Build updated git_state
    git_state_update: dict[str, Any] = {}
    if commit_sha:
        git_state_update["commit_sha"] = commit_sha
    if branch:
        git_state_update["branch"] = branch

    update_values: dict[str, Any] = {
        "status": WorkspaceStatus.ACTIVE.value,
        "updated_at": now,
        "last_synced_at": now,
    }

    async with async_db_session() as db:
        # Fetch current git_state to merge
        stmt = select(WorkspaceORM).where(WorkspaceORM.workspace_id == workspace_id)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            raise ValueError(f"Workspace {workspace_id!r} not found.")

        # Merge git_state
        current_git_state = dict(row.git_state or {})
        current_git_state.update(git_state_update)
        update_values["git_state"] = current_git_state

        update_stmt = (
            update(WorkspaceORM)
            .where(WorkspaceORM.workspace_id == workspace_id)
            .values(**update_values)
        )
        await db.execute(update_stmt)
        await db.commit()

    logger.info(
        "Synced workspace | workspace_id=%s agent_id=%s sha=%s",
        workspace_id,
        agent_id,
        commit_sha or "unknown",
    )
    return {
        "workspace_id": workspace_id,
        "status": WorkspaceStatus.ACTIVE.value,
        "head_commit_sha": commit_sha,
        "synced_at": now.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Workspace archival
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="archive_workspace")
async def archive_workspace(
    workspace_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """
    Archive a workspace when its associated task completes.

    Marks the workspace as ARCHIVED in the database.  The actual cleanup
    (deleting local files, pushing final state to remote) is handled by
    the workspace manager.

    Args:
        workspace_id: Workspace to archive.
        reason:       Optional reason for archival.

    Returns:
        Dict with workspace_id and archived status.

    Raises:
        ApplicationError: If the workspace cannot be archived.
    """
    now = datetime.now(timezone.utc)

    async with async_db_session() as db:
        stmt = (
            update(WorkspaceORM)
            .where(WorkspaceORM.workspace_id == workspace_id)
            .values(
                status=WorkspaceStatus.ARCHIVED.value,
                archived_at=now,
                updated_at=now,
            )
        )
        result = await db.execute(stmt)
        await db.commit()

        if result.rowcount == 0:
            raise ValueError(f"Workspace {workspace_id!r} not found.")

    logger.info(
        "Archived workspace | workspace_id=%s reason=%s",
        workspace_id,
        reason or "task completed",
    )
    return {
        "workspace_id": workspace_id,
        "status": WorkspaceStatus.ARCHIVED.value,
        "archived_at": now.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Workspace status query
# ─────────────────────────────────────────────────────────────────────────────


@activity.defn(name="get_workspace_status")
async def get_workspace_status(workspace_id: str) -> dict[str, Any]:
    """
    Retrieve the current status of a workspace from the database.

    Args:
        workspace_id: Workspace to query.

    Returns:
        Dict with workspace metadata and current status.

    Raises:
        ApplicationError: If the workspace is not found.
    """
    async with async_db_session() as db:
        stmt = select(WorkspaceORM).where(WorkspaceORM.workspace_id == workspace_id)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            raise ValueError(f"Workspace {workspace_id!r} not found.")

        git_state = row.git_state or {}
        return {
            "workspace_id": row.workspace_id,
            "agent_id": row.agent_id,
            "task_id": row.task_id,
            "workflow_id": row.workflow_id,
            "workspace_type": row.workspace_type,
            "status": row.status,
            "root_path": row.root_path,
            "git_remote_url": row.repo_url,
            "branch": git_state.get("branch", "main"),
            "head_commit_sha": git_state.get("commit_sha"),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
