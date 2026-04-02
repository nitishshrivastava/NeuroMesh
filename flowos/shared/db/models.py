"""
shared/db/models.py — FlowOS SQLAlchemy ORM Models

Defines all database tables for the FlowOS metadata store using SQLAlchemy 2.x
declarative ORM syntax.  These models are the authoritative source of truth for
the Alembic migration autogeneration.

Table inventory:
    workflows          — Workflow instances (running executions)
    workflow_steps     — Step definitions within a workflow
    tasks              — Atomic units of work assigned to agents
    task_attempts      — Execution attempt history per task
    agents             — Registered participants (human, AI, build, etc.)
    agent_capabilities — Declared capabilities per agent
    workspaces         — Git-backed agent workspaces
    checkpoints        — Durable task state snapshots (Git commits)
    checkpoint_files   — File-level change records per checkpoint
    handoffs           — Task ownership transfers between agents
    reasoning_sessions — AI reasoning session records
    reasoning_steps    — Individual steps within a reasoning session
    tool_calls         — Tool/function calls made by AI agents
    event_records      — Kafka event audit log (append-only)

Design decisions:
- All primary keys are UUID strings (VARCHAR(36)) for global uniqueness.
- All timestamps are stored as TIMESTAMP WITH TIME ZONE (UTC).
- JSON columns use JSONB for efficient querying on PostgreSQL.
- Soft deletes are not used — records are immutable once in terminal states.
- Indexes are defined on all foreign keys and common query predicates.
- The ``updated_at`` trigger is applied to all mutable tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db.base import Base


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Workflows
# ─────────────────────────────────────────────────────────────────────────────


class WorkflowORM(Base):
    """
    Persisted workflow instance.

    One row per workflow execution.  The ``definition`` column stores the
    full parsed YAML DSL as JSONB.  The ``inputs`` and ``outputs`` columns
    store runtime parameter values.
    """

    __tablename__ = "workflows"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','paused','completed','failed','cancelled')",
            name="ck_workflows_status",
        ),
        CheckConstraint(
            "trigger IN ('manual','scheduled','webhook','event','api','retry')",
            name="ck_workflows_trigger",
        ),
        Index("ix_workflows_status", "status"),
        Index("ix_workflows_owner_agent_id", "owner_agent_id"),
        Index("ix_workflows_project", "project"),
        Index("ix_workflows_created_at", "created_at"),
        Index("ix_workflows_updated_at", "updated_at"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 workflow instance identifier.",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable workflow name.",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        comment="Current lifecycle state.",
    )
    trigger: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="manual",
        comment="How this workflow was initiated.",
    )
    definition: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Parsed workflow definition (YAML DSL as JSON).",
    )
    temporal_run_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Temporal workflow run ID.",
    )
    temporal_workflow_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Temporal workflow ID (stable across retries).",
    )
    owner_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="SET NULL"),
        nullable=True,
        comment="Agent that owns/initiated this workflow.",
    )
    project: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Project/namespace this workflow belongs to.",
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Runtime input parameters.",
    )
    outputs: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Collected output parameters.",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable error description.",
    )
    error_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Structured error details.",
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Arbitrary tags.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        comment="UTC timestamp when this workflow was created.",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when execution began.",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when execution finished.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        comment="UTC timestamp of the last state change.",
    )

    # Relationships
    tasks: Mapped[list["TaskORM"]] = relationship(
        "TaskORM",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="select",
    )
    checkpoints: Mapped[list["CheckpointORM"]] = relationship(
        "CheckpointORM",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="select",
    )
    handoffs: Mapped[list["HandoffORM"]] = relationship(
        "HandoffORM",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="select",
    )
    reasoning_sessions: Mapped[list["ReasoningSessionORM"]] = relationship(
        "ReasoningSessionORM",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="select",
    )
    owner: Mapped["AgentORM | None"] = relationship(
        "AgentORM",
        foreign_keys=[owner_agent_id],
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tasks
# ─────────────────────────────────────────────────────────────────────────────


class TaskORM(Base):
    """
    Persisted task record.

    One row per task.  Tasks are the atomic units of work assigned to agents.
    The ``inputs`` and ``outputs`` columns store parameter lists as JSONB.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'pending','assigned','accepted','in_progress','checkpointed',"
            "'handoff_requested','handoff_accepted','revert_requested',"
            "'reverted','completed','failed','cancelled','skipped'"
            ")",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "priority IN ('low','normal','high','critical')",
            name="ck_tasks_priority",
        ),
        CheckConstraint(
            "task_type IN ("
            "'human','ai','build','deploy','review','approval',"
            "'notification','integration','analysis'"
            ")",
            name="ck_tasks_task_type",
        ),
        Index("ix_tasks_workflow_id", "workflow_id"),
        Index("ix_tasks_assigned_agent_id", "assigned_agent_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_priority", "priority"),
        Index("ix_tasks_task_type", "task_type"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_updated_at", "updated_at"),
        Index("ix_tasks_due_at", "due_at"),
    )

    task_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 task identifier.",
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
        comment="Workflow this task belongs to.",
    )
    step_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Workflow step this task was created from.",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable task name.",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional detailed description.",
    )
    task_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="human",
        comment="Nature of work.",
    )
    status: Mapped[str] = mapped_column(
        String(25),
        nullable=False,
        default="pending",
        comment="Current lifecycle state.",
    )
    priority: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="normal",
        comment="Scheduling priority.",
    )
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="SET NULL"),
        nullable=True,
        comment="Agent currently responsible for this task.",
    )
    previous_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="SET NULL"),
        nullable=True,
        comment="Agent that previously held this task.",
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workspaces.workspace_id", ondelete="SET NULL"),
        nullable=True,
        comment="Workspace where this task is being executed.",
    )
    inputs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Input parameters as a list of {name, value, ...} objects.",
    )
    outputs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Output parameters as a list of {name, value, ...} objects.",
    )
    depends_on: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Task IDs that must complete before this task starts.",
    )
    timeout_secs: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Maximum execution time in seconds. 0 = no limit.",
    )
    retry_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        comment="Maximum number of automatic retries.",
    )
    current_retry: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        comment="Current retry attempt number.",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable error description.",
    )
    error_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Structured error details.",
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Arbitrary tags.",
    )
    task_metadata: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Arbitrary key/value metadata.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Optional deadline for task completion.",
    )

    # Relationships
    workflow: Mapped["WorkflowORM"] = relationship(
        "WorkflowORM",
        back_populates="tasks",
        lazy="select",
    )
    assigned_agent: Mapped["AgentORM | None"] = relationship(
        "AgentORM",
        foreign_keys=[assigned_agent_id],
        lazy="select",
    )
    previous_agent: Mapped["AgentORM | None"] = relationship(
        "AgentORM",
        foreign_keys=[previous_agent_id],
        lazy="select",
    )
    workspace: Mapped["WorkspaceORM | None"] = relationship(
        "WorkspaceORM",
        back_populates="tasks",
        lazy="select",
    )
    attempts: Mapped[list["TaskAttemptORM"]] = relationship(
        "TaskAttemptORM",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskAttemptORM.attempt_number",
        lazy="select",
    )
    checkpoints: Mapped[list["CheckpointORM"]] = relationship(
        "CheckpointORM",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="select",
    )
    handoffs_as_source: Mapped[list["HandoffORM"]] = relationship(
        "HandoffORM",
        foreign_keys="HandoffORM.task_id",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="select",
    )
    reasoning_sessions: Mapped[list["ReasoningSessionORM"]] = relationship(
        "ReasoningSessionORM",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="select",
    )


class TaskAttemptORM(Base):
    """
    Records a single execution attempt of a task.

    A task may be attempted multiple times due to failures or handoffs.
    """

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempts_task_id"),
        Index("ix_task_attempts_task_id", "task_id"),
        Index("ix_task_attempts_agent_id", "agent_id"),
    )

    attempt_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 attempt identifier.",
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="Sequential attempt number (1-based).",
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
        comment="Agent that executed this attempt.",
    )
    status: Mapped[str] = mapped_column(
        String(25),
        nullable=False,
        default="in_progress",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    task: Mapped["TaskORM"] = relationship(
        "TaskORM",
        back_populates="attempts",
        lazy="select",
    )
    agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agents
# ─────────────────────────────────────────────────────────────────────────────


class AgentORM(Base):
    """
    Persisted agent record.

    One row per registered agent.  Capabilities are stored in a separate
    ``agent_capabilities`` table for normalisation.
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "agent_type IN ('human','ai','build','deploy','system')",
            name="ck_agents_agent_type",
        ),
        CheckConstraint(
            "status IN ('offline','online','idle','busy','maintenance','deregistered')",
            name="ck_agents_status",
        ),
        Index("ix_agents_agent_type", "agent_type"),
        Index("ix_agents_status", "status"),
        Index("ix_agents_email", "email"),
        Index("ix_agents_registered_at", "registered_at"),
    )

    agent_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 agent identifier.",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable display name.",
    )
    agent_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Kind of participant.",
    )
    status: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        default="offline",
        comment="Current operational status.",
    )
    current_task_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Task currently being executed.",
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Current workspace ID.",
    )
    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        comment="Contact email (primarily for human agents).",
    )
    display_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )
    timezone: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="UTC",
    )
    kafka_group_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Kafka consumer group ID for this agent.",
    )
    max_concurrent_tasks: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=1,
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    agent_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Arbitrary key/value metadata.",
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    deregistered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    capabilities: Mapped[list["AgentCapabilityORM"]] = relationship(
        "AgentCapabilityORM",
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="select",
    )
    workspaces: Mapped[list["WorkspaceORM"]] = relationship(
        "WorkspaceORM",
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="select",
    )


class AgentCapabilityORM(Base):
    """
    A declared capability of an agent.

    Stored separately from the agent record for efficient querying
    (e.g. "find all agents with capability X").
    """

    __tablename__ = "agent_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "name", name="uq_agent_capabilities_agent_id"
        ),
        Index("ix_agent_capabilities_agent_id", "agent_id"),
        Index("ix_agent_capabilities_name", "name"),
        Index("ix_agent_capabilities_enabled", "enabled"),
    )

    capability_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 capability identifier.",
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Capability identifier.",
    )
    version: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    # Relationships
    agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        back_populates="capabilities",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceORM(Base):
    """
    Persisted workspace record.

    One row per agent workspace.  The ``git_state`` column stores the current
    Git state as JSONB.  Snapshots are stored inline as a JSONB array.
    """

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('creating','ready','active','syncing','idle','archived','error')",
            name="ck_workspaces_status",
        ),
        CheckConstraint(
            "workspace_type IN ('local','remote','ephemeral','shared')",
            name="ck_workspaces_workspace_type",
        ),
        Index("ix_workspaces_agent_id", "agent_id"),
        Index("ix_workspaces_status", "status"),
        Index("ix_workspaces_workspace_type", "workspace_type"),
        Index("ix_workspaces_created_at", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 workspace identifier.",
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
        comment="Agent that owns this workspace.",
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Task currently being executed in this workspace.",
    )
    workflow_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Workflow this workspace is associated with.",
    )
    workspace_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="local",
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="creating",
    )
    root_path: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        comment="Absolute filesystem path to the workspace root.",
    )
    git_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Current Git state (branch, commit SHA, dirty flag, etc.).",
    )
    snapshots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Point-in-time workspace snapshots.",
    )
    repo_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
        comment="Remote Git repository URL.",
    )
    branch_prefix: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="flowos",
    )
    disk_usage_mb: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )
    max_disk_mb: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=10240.0,
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    workspace_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        back_populates="workspaces",
        lazy="select",
    )
    tasks: Mapped[list["TaskORM"]] = relationship(
        "TaskORM",
        back_populates="workspace",
        lazy="select",
    )
    checkpoints: Mapped[list["CheckpointORM"]] = relationship(
        "CheckpointORM",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoints
# ─────────────────────────────────────────────────────────────────────────────


class CheckpointORM(Base):
    """
    Persisted checkpoint record.

    One row per checkpoint.  File-level change records are stored in the
    ``checkpoint_files`` table.
    """

    __tablename__ = "checkpoints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('creating','committed','verified','reverted','failed')",
            name="ck_checkpoints_status",
        ),
        CheckConstraint(
            "checkpoint_type IN ("
            "'manual','auto','pre_handoff','pre_revert','milestone',"
            "'ai_proposal','completion'"
            ")",
            name="ck_checkpoints_checkpoint_type",
        ),
        Index("ix_checkpoints_task_id", "task_id"),
        Index("ix_checkpoints_workflow_id", "workflow_id"),
        Index("ix_checkpoints_agent_id", "agent_id"),
        Index("ix_checkpoints_workspace_id", "workspace_id"),
        Index("ix_checkpoints_status", "status"),
        Index("ix_checkpoints_created_at", "created_at"),
        Index("ix_checkpoints_git_commit_sha", "git_commit_sha"),
    )

    checkpoint_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 checkpoint identifier.",
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    checkpoint_type: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        default="manual",
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="creating",
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Monotonically increasing sequence number within a task.",
    )
    git_commit_sha: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Git commit SHA of this checkpoint.",
    )
    git_branch: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="main",
    )
    git_tag: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Human-readable description of this checkpoint.",
    )
    file_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    lines_added: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    lines_removed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    s3_archive_key: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
        comment="S3 key for a full workspace archive.",
    )
    task_progress: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Estimated task completion percentage (0-100).",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    checkpoint_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reverted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    task: Mapped["TaskORM"] = relationship(
        "TaskORM",
        back_populates="checkpoints",
        lazy="select",
    )
    workflow: Mapped["WorkflowORM"] = relationship(
        "WorkflowORM",
        back_populates="checkpoints",
        lazy="select",
    )
    workspace: Mapped["WorkspaceORM"] = relationship(
        "WorkspaceORM",
        back_populates="checkpoints",
        lazy="select",
    )
    agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        lazy="select",
    )
    files: Mapped[list["CheckpointFileORM"]] = relationship(
        "CheckpointFileORM",
        back_populates="checkpoint",
        cascade="all, delete-orphan",
        lazy="select",
    )
    handoffs: Mapped[list["HandoffORM"]] = relationship(
        "HandoffORM",
        back_populates="checkpoint",
        lazy="select",
    )


class CheckpointFileORM(Base):
    """
    File-level change record for a checkpoint.

    Stores which files changed as part of a checkpoint for audit and diff
    display purposes.
    """

    __tablename__ = "checkpoint_files"
    __table_args__ = (
        Index("ix_checkpoint_files_checkpoint_id", "checkpoint_id"),
        Index("ix_checkpoint_files_path", "path"),
    )

    file_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 file record identifier.",
    )
    checkpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("checkpoints.checkpoint_id", ondelete="CASCADE"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        comment="Relative path within the workspace.",
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Git status: added, modified, deleted, renamed.",
    )
    old_path: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
        comment="Previous path for renamed files.",
    )
    size_bytes: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    checksum: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 checksum of the file content.",
    )

    # Relationships
    checkpoint: Mapped["CheckpointORM"] = relationship(
        "CheckpointORM",
        back_populates="files",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handoffs
# ─────────────────────────────────────────────────────────────────────────────


class HandoffORM(Base):
    """
    Persisted handoff record.

    One row per handoff request.  The ``context`` column stores the briefing
    document as JSONB.
    """

    __tablename__ = "handoffs"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'requested','pending_acceptance','accepted','completed',"
            "'rejected','expired','failed','cancelled'"
            ")",
            name="ck_handoffs_status",
        ),
        CheckConstraint(
            "handoff_type IN ("
            "'delegation','escalation','completion','review','emergency',"
            "'scheduled','ai_to_human','human_to_ai'"
            ")",
            name="ck_handoffs_handoff_type",
        ),
        Index("ix_handoffs_task_id", "task_id"),
        Index("ix_handoffs_workflow_id", "workflow_id"),
        Index("ix_handoffs_source_agent_id", "source_agent_id"),
        Index("ix_handoffs_target_agent_id", "target_agent_id"),
        Index("ix_handoffs_status", "status"),
        Index("ix_handoffs_requested_at", "requested_at"),
    )

    handoff_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 handoff identifier.",
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="SET NULL"),
        nullable=True,
    )
    handoff_type: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        default="delegation",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="requested",
    )
    checkpoint_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("checkpoints.checkpoint_id", ondelete="SET NULL"),
        nullable=True,
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    context: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Briefing context for the target agent.",
    )
    rejection_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    priority: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="normal",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    handoff_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    # Relationships
    task: Mapped["TaskORM"] = relationship(
        "TaskORM",
        foreign_keys=[task_id],
        back_populates="handoffs_as_source",
        lazy="select",
    )
    workflow: Mapped["WorkflowORM"] = relationship(
        "WorkflowORM",
        back_populates="handoffs",
        lazy="select",
    )
    source_agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        foreign_keys=[source_agent_id],
        lazy="select",
    )
    target_agent: Mapped["AgentORM | None"] = relationship(
        "AgentORM",
        foreign_keys=[target_agent_id],
        lazy="select",
    )
    checkpoint: Mapped["CheckpointORM | None"] = relationship(
        "CheckpointORM",
        back_populates="handoffs",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reasoning Sessions
# ─────────────────────────────────────────────────────────────────────────────


class ReasoningSessionORM(Base):
    """
    Persisted AI reasoning session record.

    One row per reasoning session.  Steps are stored in the
    ``reasoning_steps`` table.
    """

    __tablename__ = "reasoning_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'pending','running','completed','failed','cancelled','awaiting_human'"
            ")",
            name="ck_reasoning_sessions_status",
        ),
        Index("ix_reasoning_sessions_task_id", "task_id"),
        Index("ix_reasoning_sessions_workflow_id", "workflow_id"),
        Index("ix_reasoning_sessions_agent_id", "agent_id"),
        Index("ix_reasoning_sessions_status", "status"),
        Index("ix_reasoning_sessions_started_at", "started_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 session identifier.",
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    objective: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The goal/objective for this reasoning session.",
    )
    final_output: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    proposal: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured proposal produced by this session.",
    )
    model_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    total_prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    total_output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    estimated_cost_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    langgraph_run_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    langsmith_run_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    human_review_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    session_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    # Relationships
    task: Mapped["TaskORM"] = relationship(
        "TaskORM",
        back_populates="reasoning_sessions",
        lazy="select",
    )
    workflow: Mapped["WorkflowORM"] = relationship(
        "WorkflowORM",
        back_populates="reasoning_sessions",
        lazy="select",
    )
    agent: Mapped["AgentORM"] = relationship(
        "AgentORM",
        lazy="select",
    )
    steps: Mapped[list["ReasoningStepORM"]] = relationship(
        "ReasoningStepORM",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ReasoningStepORM.sequence",
        lazy="select",
    )


class ReasoningStepORM(Base):
    """
    A single step in an AI reasoning session.

    Tool calls are stored in the ``tool_calls`` table.
    """

    __tablename__ = "reasoning_steps"
    __table_args__ = (
        CheckConstraint(
            "step_type IN ("
            "'thought','tool_call','observation','plan','decision',"
            "'proposal','reflection','human_input','final_answer','error'"
            ")",
            name="ck_reasoning_steps_step_type",
        ),
        Index("ix_reasoning_steps_session_id", "session_id"),
        Index("ix_reasoning_steps_step_type", "step_type"),
        Index("ix_reasoning_steps_sequence", "sequence"),
    )

    step_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 step identifier.",
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("reasoning_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    step_type: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonically increasing sequence number within a session.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    model_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    total_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    step_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Relationships
    session: Mapped["ReasoningSessionORM"] = relationship(
        "ReasoningSessionORM",
        back_populates="steps",
        lazy="select",
    )
    tool_calls: Mapped[list["ToolCallORM"]] = relationship(
        "ToolCallORM",
        back_populates="step",
        cascade="all, delete-orphan",
        lazy="select",
    )


class ToolCallORM(Base):
    """
    A record of a single tool/function call made by an AI agent.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_step_id", "step_id"),
        Index("ix_tool_calls_tool_name", "tool_name"),
        Index("ix_tool_calls_is_error", "is_error"),
    )

    tool_call_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 tool call identifier.",
    )
    step_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("reasoning_steps.step_id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    tool_input: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    tool_output: Mapped[Any] = mapped_column(
        JSONB,
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_error: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # Relationships
    step: Mapped["ReasoningStepORM"] = relationship(
        "ReasoningStepORM",
        back_populates="tool_calls",
        lazy="select",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Event Records (Kafka audit log)
# ─────────────────────────────────────────────────────────────────────────────


class EventRecordORM(Base):
    """
    Append-only Kafka event audit log.

    Every event flowing through the FlowOS event bus is persisted here for
    audit, replay, and observability purposes.  This table is append-only —
    records are never updated or deleted.

    The ``payload`` column stores the full event payload as JSONB for
    efficient querying.
    """

    __tablename__ = "event_records"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('debug','info','warning','error','critical')",
            name="ck_event_records_severity",
        ),
        Index("ix_event_records_event_type", "event_type"),
        Index("ix_event_records_topic", "topic"),
        Index("ix_event_records_source", "source"),
        Index("ix_event_records_workflow_id", "workflow_id"),
        Index("ix_event_records_task_id", "task_id"),
        Index("ix_event_records_agent_id", "agent_id"),
        Index("ix_event_records_occurred_at", "occurred_at"),
        Index("ix_event_records_correlation_id", "correlation_id"),
        # Composite index for the most common query pattern
        Index(
            "ix_event_records_workflow_occurred",
            "workflow_id",
            "occurred_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        comment="UUID v4 event identifier.",
    )
    event_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        comment="Discriminator string identifying the event kind.",
    )
    topic: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Kafka topic this event belongs to.",
    )
    source: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Component that produced this event.",
    )
    workflow_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Workflow this event is scoped to.",
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Task this event is scoped to.",
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Agent this event is scoped to.",
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="ID linking related events.",
    )
    causation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="ID of the event that caused this one.",
    )
    severity: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="info",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Event-type-specific data.",
    )
    event_metadata: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Arbitrary key/value metadata.",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        comment="UTC timestamp when the event was generated.",
    )
    schema_version: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="1.0",
    )
    # NOTE: No updated_at — this table is append-only.


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy event listeners for updated_at auto-update
# ─────────────────────────────────────────────────────────────────────────────
# The ``onupdate`` parameter on mapped_column handles this at the ORM level.
# The PostgreSQL trigger in init.sql handles it at the DB level for direct
# SQL updates that bypass the ORM.
#
# Register the DB-level trigger for all mutable tables:
_MUTABLE_TABLES = [
    "workflows",
    "tasks",
    "agents",
    "workspaces",
    "checkpoints",
    "handoffs",
    "reasoning_sessions",
]


def _register_updated_at_triggers(target: Any, connection: Any, **kwargs: Any) -> None:
    """
    Register the set_updated_at() PostgreSQL trigger on all mutable tables.

    This is called after table creation (e.g. in tests using create_all).
    In production, the trigger is applied via Alembic migrations.
    """
    for table_name in _MUTABLE_TABLES:
        trigger_name = f"trg_{table_name}_updated_at"
        connection.execute(
            __import__("sqlalchemy").text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgname = '{trigger_name}'
                    ) THEN
                        CREATE TRIGGER {trigger_name}
                        BEFORE UPDATE ON {table_name}
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                    END IF;
                END
                $$;
                """
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "WorkflowORM",
    "TaskORM",
    "TaskAttemptORM",
    "AgentORM",
    "AgentCapabilityORM",
    "WorkspaceORM",
    "CheckpointORM",
    "CheckpointFileORM",
    "HandoffORM",
    "ReasoningSessionORM",
    "ReasoningStepORM",
    "ToolCallORM",
    "EventRecordORM",
]
