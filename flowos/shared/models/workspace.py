"""
shared/models/workspace.py — FlowOS Workspace Domain Model

A Workspace is the local, reversible state boundary for an agent.  Each agent
has a Git-backed workspace that tracks the files, code, and artifacts produced
during task execution.  Workspaces support checkpointing (Git commits),
branching, and handoff (pushing state to another agent's workspace).

Workspace lifecycle:
    CREATING → READY → ACTIVE → IDLE
    ACTIVE   → SYNCING → ACTIVE
    ACTIVE   → ARCHIVED
    CREATING → ERROR
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceStatus(StrEnum):
    """Lifecycle states of a FlowOS workspace."""

    CREATING = "creating"
    READY = "ready"
    ACTIVE = "active"
    SYNCING = "syncing"
    IDLE = "idle"
    ARCHIVED = "archived"
    ERROR = "error"


class WorkspaceType(StrEnum):
    """
    Classifies the kind of workspace.

    - LOCAL:   Files on the agent's local filesystem (primary type)
    - REMOTE:  Files on a remote filesystem (NFS, EFS, etc.)
    - EPHEMERAL: Temporary workspace (e.g. for build runners in Kubernetes)
    - SHARED:  Shared workspace accessible by multiple agents
    """

    LOCAL = "local"
    REMOTE = "remote"
    EPHEMERAL = "ephemeral"
    SHARED = "shared"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class GitState(BaseModel):
    """
    The current Git state of a workspace.

    Tracks the active branch, latest commit, and any uncommitted changes.

    Attributes:
        branch:          Current Git branch name.
        commit_sha:      SHA of the latest commit (HEAD).
        commit_message:  Message of the latest commit.
        commit_author:   Author of the latest commit.
        committed_at:    UTC timestamp of the latest commit.
        is_dirty:        True if there are uncommitted changes.
        staged_files:    List of staged file paths.
        unstaged_files:  List of unstaged modified file paths.
        untracked_files: List of untracked file paths.
        remote_url:      Remote repository URL, if any.
        ahead_count:     Number of commits ahead of the remote branch.
        behind_count:    Number of commits behind the remote branch.
    """

    branch: str = Field(
        default="main",
        description="Current Git branch name.",
    )
    commit_sha: str | None = Field(
        default=None,
        description="SHA of the latest commit (HEAD).",
    )
    commit_message: str | None = Field(
        default=None,
        description="Message of the latest commit.",
    )
    commit_author: str | None = Field(
        default=None,
        description="Author of the latest commit.",
    )
    committed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest commit.",
    )
    is_dirty: bool = Field(
        default=False,
        description="True if there are uncommitted changes.",
    )
    staged_files: list[str] = Field(
        default_factory=list,
        description="List of staged file paths.",
    )
    unstaged_files: list[str] = Field(
        default_factory=list,
        description="List of unstaged modified file paths.",
    )
    untracked_files: list[str] = Field(
        default_factory=list,
        description="List of untracked file paths.",
    )
    remote_url: str | None = Field(
        default=None,
        description="Remote repository URL, if any.",
    )
    ahead_count: int = Field(
        default=0,
        ge=0,
        description="Number of commits ahead of the remote branch.",
    )
    behind_count: int = Field(
        default=0,
        ge=0,
        description="Number of commits behind the remote branch.",
    )


class WorkspaceSnapshot(BaseModel):
    """
    A point-in-time snapshot of workspace state.

    Snapshots are created before risky operations (e.g. applying AI patches)
    to allow rollback.  They reference a Git commit SHA.

    Attributes:
        snapshot_id:   Unique snapshot identifier.
        commit_sha:    Git commit SHA of this snapshot.
        branch:        Branch at snapshot time.
        description:   Human-readable description of why this snapshot was taken.
        created_by:    Agent that created this snapshot.
        created_at:    UTC timestamp when this snapshot was created.
        s3_key:        Optional S3 key for a full workspace archive.
        size_bytes:    Size of the snapshot archive in bytes.
    """

    snapshot_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
    )
    commit_sha: str = Field(
        description="Git commit SHA of this snapshot.",
    )
    branch: str = Field(
        description="Branch at snapshot time.",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable description of why this snapshot was taken.",
    )
    created_by: str = Field(
        description="Agent ID that created this snapshot.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    s3_key: str | None = Field(
        default=None,
        description="Optional S3 key for a full workspace archive.",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Size of the snapshot archive in bytes.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Workspace(BaseModel):
    """
    A Git-backed local state boundary for an agent.

    Each agent has a workspace that tracks the files, code, and artifacts
    produced during task execution.  The workspace is the agent's local
    source of truth; the orchestrator tracks workflow-level truth.

    Attributes:
        workspace_id:    Globally unique workspace identifier.
        agent_id:        Agent that owns this workspace.
        task_id:         Task currently being executed in this workspace.
        workflow_id:     Workflow this workspace is associated with.
        workspace_type:  Kind of workspace (local, remote, ephemeral, shared).
        status:          Current lifecycle state.
        root_path:       Absolute filesystem path to the workspace root.
        git_state:       Current Git state of the workspace.
        snapshots:       List of point-in-time snapshots.
        repo_url:        Remote Git repository URL (if cloned from remote).
        branch_prefix:   Prefix for branches created in this workspace.
        disk_usage_mb:   Current disk usage in megabytes.
        max_disk_mb:     Maximum allowed disk usage in megabytes.
        tags:            Arbitrary tags for filtering.
        metadata:        Arbitrary key/value metadata.
        created_at:      UTC timestamp when this workspace was created.
        last_synced_at:  UTC timestamp of the last sync with remote.
        updated_at:      UTC timestamp of the last state change.
        archived_at:     UTC timestamp when this workspace was archived.
    """

    workspace_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique workspace identifier.",
    )
    agent_id: str = Field(
        description="Agent that owns this workspace.",
    )
    task_id: str | None = Field(
        default=None,
        description="Task currently being executed in this workspace.",
    )
    workflow_id: str | None = Field(
        default=None,
        description="Workflow this workspace is associated with.",
    )
    workspace_type: WorkspaceType = Field(
        default=WorkspaceType.LOCAL,
        description="Kind of workspace.",
    )
    status: WorkspaceStatus = Field(
        default=WorkspaceStatus.CREATING,
        description="Current lifecycle state.",
    )
    root_path: str = Field(
        description="Absolute filesystem path to the workspace root.",
    )
    git_state: GitState = Field(
        default_factory=GitState,
        description="Current Git state of the workspace.",
    )
    snapshots: list[WorkspaceSnapshot] = Field(
        default_factory=list,
        description="List of point-in-time snapshots.",
    )
    repo_url: str | None = Field(
        default=None,
        description="Remote Git repository URL (if cloned from remote).",
    )
    branch_prefix: str = Field(
        default="flowos",
        description="Prefix for branches created in this workspace.",
    )
    disk_usage_mb: float = Field(
        default=0.0,
        ge=0.0,
        description="Current disk usage in megabytes.",
    )
    max_disk_mb: float = Field(
        default=10_240.0,  # 10 GB default
        ge=0.0,
        description="Maximum allowed disk usage in megabytes.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    last_synced_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the last sync with remote.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    archived_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when this workspace was archived.",
    )

    @field_validator("workspace_id", "agent_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v

    @property
    def is_over_disk_limit(self) -> bool:
        """Return True if disk usage exceeds the maximum allowed."""
        return self.max_disk_mb > 0 and self.disk_usage_mb > self.max_disk_mb

    @property
    def current_branch(self) -> str:
        """Return the current Git branch name."""
        return self.git_state.branch

    @property
    def current_commit(self) -> str | None:
        """Return the current Git commit SHA."""
        return self.git_state.commit_sha

    @property
    def has_uncommitted_changes(self) -> bool:
        """Return True if the workspace has uncommitted changes."""
        return self.git_state.is_dirty

    def task_branch_name(self, task_id: str) -> str:
        """Return the conventional branch name for a given task."""
        short_id = task_id.replace("-", "")[:12]
        return f"{self.branch_prefix}/task/{short_id}"
