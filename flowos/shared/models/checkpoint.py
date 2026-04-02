"""
shared/models/checkpoint.py — FlowOS Checkpoint Domain Model

A Checkpoint is a durable, reversible snapshot of task execution state.
Checkpoints are created by agents at meaningful milestones during task
execution and stored as Git commits in the agent's workspace.

Checkpoints serve three purposes:
1. **Resumability** — if an agent fails or goes offline, the next agent can
   resume from the last checkpoint rather than starting from scratch.
2. **Reversibility** — the orchestrator can revert a task to a previous
   checkpoint if a downstream step fails.
3. **Auditability** — checkpoints provide a complete history of how a task
   evolved over time.

Checkpoint lifecycle:
    CREATING → COMMITTED → VERIFIED
    CREATING → FAILED
    COMMITTED → REVERTED
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


class CheckpointStatus(StrEnum):
    """Lifecycle states of a checkpoint."""

    CREATING = "creating"
    COMMITTED = "committed"
    VERIFIED = "verified"
    REVERTED = "reverted"
    FAILED = "failed"


class CheckpointType(StrEnum):
    """
    Classifies why a checkpoint was created.

    - MANUAL:      Explicitly created by the agent via CLI.
    - AUTO:        Automatically created by the system at regular intervals.
    - PRE_HANDOFF: Created before handing off a task to another agent.
    - PRE_REVERT:  Created before reverting to a previous state.
    - MILESTONE:   Created at a significant workflow milestone.
    - AI_PROPOSAL: Created before applying an AI-proposed patch.
    - COMPLETION:  Created when a task is completed.
    """

    MANUAL = "manual"
    AUTO = "auto"
    PRE_HANDOFF = "pre_handoff"
    PRE_REVERT = "pre_revert"
    MILESTONE = "milestone"
    AI_PROPOSAL = "ai_proposal"
    COMPLETION = "completion"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class CheckpointFile(BaseModel):
    """
    A file included in a checkpoint.

    Tracks which files changed as part of this checkpoint for audit and
    diff display purposes.

    Attributes:
        path:        Relative path within the workspace.
        status:      Git status (added, modified, deleted, renamed).
        old_path:    Previous path for renamed files.
        size_bytes:  File size in bytes.
        checksum:    SHA-256 checksum of the file content.
    """

    path: str = Field(description="Relative path within the workspace.")
    status: str = Field(
        description="Git status: 'added', 'modified', 'deleted', 'renamed'.",
    )
    old_path: str | None = Field(
        default=None,
        description="Previous path for renamed files.",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="File size in bytes.",
    )
    checksum: str | None = Field(
        default=None,
        description="SHA-256 checksum of the file content.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Checkpoint(BaseModel):
    """
    A durable, reversible snapshot of task execution state.

    Checkpoints are Git commits in the agent's workspace.  They capture the
    complete state of the workspace at a point in time, allowing the system
    to resume or revert to any previous state.

    Attributes:
        checkpoint_id:   Globally unique checkpoint identifier.
        task_id:         Task this checkpoint belongs to.
        workflow_id:     Workflow this checkpoint is associated with.
        agent_id:        Agent that created this checkpoint.
        workspace_id:    Workspace where this checkpoint was created.
        checkpoint_type: Why this checkpoint was created.
        status:          Current lifecycle state.
        sequence:        Monotonically increasing sequence number within a task.
        git_commit_sha:  Git commit SHA of this checkpoint.
        git_branch:      Git branch at checkpoint time.
        git_tag:         Optional Git tag applied to this commit.
        message:         Human-readable description of this checkpoint.
        files_changed:   List of files changed in this checkpoint.
        file_count:      Total number of files changed.
        lines_added:     Total lines added.
        lines_removed:   Total lines removed.
        s3_archive_key:  Optional S3 key for a full workspace archive.
        task_progress:   Estimated task completion percentage (0-100).
        notes:           Free-form notes from the agent.
        metadata:        Arbitrary key/value metadata.
        created_at:      UTC timestamp when this checkpoint was created.
        verified_at:     UTC timestamp when this checkpoint was verified.
        reverted_at:     UTC timestamp when this checkpoint was reverted.
    """

    checkpoint_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique checkpoint identifier.",
    )
    task_id: str = Field(
        description="Task this checkpoint belongs to.",
    )
    workflow_id: str = Field(
        description="Workflow this checkpoint is associated with.",
    )
    agent_id: str = Field(
        description="Agent that created this checkpoint.",
    )
    workspace_id: str = Field(
        description="Workspace where this checkpoint was created.",
    )
    checkpoint_type: CheckpointType = Field(
        default=CheckpointType.MANUAL,
        description="Why this checkpoint was created.",
    )
    status: CheckpointStatus = Field(
        default=CheckpointStatus.CREATING,
        description="Current lifecycle state.",
    )
    sequence: int = Field(
        default=1,
        ge=1,
        description="Monotonically increasing sequence number within a task.",
    )
    git_commit_sha: str | None = Field(
        default=None,
        description="Git commit SHA of this checkpoint.",
    )
    git_branch: str = Field(
        default="main",
        description="Git branch at checkpoint time.",
    )
    git_tag: str | None = Field(
        default=None,
        description="Optional Git tag applied to this commit.",
    )
    message: str = Field(
        min_length=1,
        max_length=500,
        description="Human-readable description of this checkpoint.",
    )
    files_changed: list[CheckpointFile] = Field(
        default_factory=list,
        description="List of files changed in this checkpoint.",
    )
    file_count: int = Field(
        default=0,
        ge=0,
        description="Total number of files changed.",
    )
    lines_added: int = Field(
        default=0,
        ge=0,
        description="Total lines added.",
    )
    lines_removed: int = Field(
        default=0,
        ge=0,
        description="Total lines removed.",
    )
    s3_archive_key: str | None = Field(
        default=None,
        description="Optional S3 key for a full workspace archive.",
    )
    task_progress: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Estimated task completion percentage (0-100).",
    )
    notes: str | None = Field(
        default=None,
        description="Free-form notes from the agent about this checkpoint.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    verified_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when this checkpoint was verified.",
    )
    reverted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when this checkpoint was reverted.",
    )

    @field_validator("checkpoint_id", "task_id", "workflow_id", "agent_id", "workspace_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v

    @field_validator("git_commit_sha")
    @classmethod
    def validate_git_sha(cls, v: str | None) -> str | None:
        """Validate that git_commit_sha looks like a valid Git SHA."""
        if v is None:
            return v
        # Git SHAs are 40 hex chars (full) or 7+ chars (abbreviated)
        if not (7 <= len(v) <= 40) or not all(c in "0123456789abcdefABCDEF" for c in v):
            raise ValueError(
                f"git_commit_sha must be a valid Git SHA (7-40 hex chars), got: {v!r}"
            )
        return v.lower()

    @property
    def is_committed(self) -> bool:
        """Return True if this checkpoint has been committed to Git."""
        return self.status in (CheckpointStatus.COMMITTED, CheckpointStatus.VERIFIED)

    @property
    def is_reverted(self) -> bool:
        """Return True if this checkpoint has been reverted."""
        return self.status == CheckpointStatus.REVERTED

    @property
    def net_lines_changed(self) -> int:
        """Return the net line change (added - removed)."""
        return self.lines_added - self.lines_removed
