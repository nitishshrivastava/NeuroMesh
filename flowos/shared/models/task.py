"""
shared/models/task.py — FlowOS Task Domain Model

A Task is the atomic unit of work in FlowOS.  Tasks are created by the
orchestrator from workflow step definitions and assigned to agents via Kafka
events.  Agents accept, execute, checkpoint, hand off, or complete tasks
through the CLI.

Task lifecycle:
    PENDING → ASSIGNED → ACCEPTED → IN_PROGRESS → COMPLETED
                                               ↘ FAILED
                                               ↘ CHECKPOINTED → IN_PROGRESS (resumed)
    IN_PROGRESS → HANDOFF_REQUESTED → HANDOFF_ACCEPTED → IN_PROGRESS (new agent)
    IN_PROGRESS → REVERT_REQUESTED → REVERTED
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    """Lifecycle states of a FlowOS task."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    CHECKPOINTED = "checkpointed"
    HANDOFF_REQUESTED = "handoff_requested"
    HANDOFF_ACCEPTED = "handoff_accepted"
    REVERT_REQUESTED = "revert_requested"
    REVERTED = "reverted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class TaskPriority(StrEnum):
    """Priority levels for task scheduling and assignment."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TaskType(StrEnum):
    """
    Classifies the nature of work a task represents.

    Used by the orchestrator to route tasks to appropriate agent types.
    """

    HUMAN = "human"            # Requires human review, decision, or action
    AI = "ai"                  # Executed by an AI agent (LangGraph/LangChain)
    BUILD = "build"            # Compilation, testing, packaging (Kubernetes/Argo)
    DEPLOY = "deploy"          # Deployment to an environment
    REVIEW = "review"          # Code or artifact review
    APPROVAL = "approval"      # Explicit approval gate
    NOTIFICATION = "notification"  # Send a notification (no output expected)
    INTEGRATION = "integration"    # External system integration call
    ANALYSIS = "analysis"      # Data analysis or report generation


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class TaskInput(BaseModel):
    """
    A named input parameter for a task.

    Attributes:
        name:        Parameter name.
        value:       Parameter value (any JSON-serialisable type).
        description: Optional description of this parameter.
        required:    Whether this parameter must be provided.
    """

    name: str = Field(min_length=1, max_length=255)
    value: Any = Field(default=None)
    description: str | None = Field(default=None)
    required: bool = Field(default=False)


class TaskOutput(BaseModel):
    """
    A named output produced by a completed task.

    Attributes:
        name:        Output parameter name.
        value:       Output value (any JSON-serialisable type).
        description: Optional description of this output.
        produced_at: UTC timestamp when this output was produced.
    """

    name: str = Field(min_length=1, max_length=255)
    value: Any = Field(default=None)
    description: str | None = Field(default=None)
    produced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskAttempt(BaseModel):
    """
    Records a single execution attempt of a task.

    A task may be attempted multiple times due to failures or handoffs.
    Each attempt is recorded with its agent, start/end times, and outcome.

    Attributes:
        attempt_number: Sequential attempt number (1-based).
        agent_id:       Agent that executed this attempt.
        started_at:     UTC timestamp when this attempt began.
        ended_at:       UTC timestamp when this attempt ended.
        status:         Outcome of this attempt.
        error_message:  Error description if the attempt failed.
    """

    attempt_number: int = Field(ge=1)
    agent_id: str = Field(description="Agent that executed this attempt.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    ended_at: datetime | None = Field(default=None)
    status: TaskStatus = Field(default=TaskStatus.IN_PROGRESS)
    error_message: str | None = Field(default=None)

    @property
    def duration_seconds(self) -> float | None:
        """Return attempt duration in seconds, or None if still running."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Task(BaseModel):
    """
    An atomic unit of work assigned to an agent.

    Tasks are created by the orchestrator from workflow step definitions and
    assigned to agents via Kafka events.  The orchestrator tracks task truth;
    agents track local workspace truth.

    Attributes:
        task_id:           Globally unique task identifier.
        workflow_id:       Workflow this task belongs to.
        step_id:           Workflow step this task was created from.
        name:              Human-readable task name.
        description:       Optional detailed description.
        task_type:         Nature of work (human, ai, build, etc.).
        status:            Current lifecycle state.
        priority:          Scheduling priority.
        assigned_agent_id: Agent currently responsible for this task.
        previous_agent_id: Agent that previously held this task (for handoffs).
        workspace_id:      Workspace where this task is being executed.
        inputs:            Input parameters for this task.
        outputs:           Output parameters produced by this task.
        attempts:          History of execution attempts.
        depends_on:        Task IDs that must complete before this task starts.
        timeout_secs:      Maximum execution time in seconds (0 = no limit).
        retry_count:       Maximum number of automatic retries.
        current_retry:     Current retry attempt number.
        error_message:     Human-readable error if status=FAILED.
        error_details:     Structured error details.
        tags:              Arbitrary tags for filtering and routing.
        metadata:          Arbitrary key/value metadata.
        created_at:        UTC timestamp when this task was created.
        assigned_at:       UTC timestamp when this task was assigned.
        started_at:        UTC timestamp when execution began.
        completed_at:      UTC timestamp when execution finished.
        updated_at:        UTC timestamp of the last state change.
        due_at:            Optional deadline for task completion.
    """

    task_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique task identifier.",
    )
    workflow_id: str = Field(
        description="Workflow this task belongs to.",
    )
    step_id: str | None = Field(
        default=None,
        description="Workflow step this task was created from.",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable task name.",
    )
    description: str | None = Field(
        default=None,
        description="Optional detailed description of what this task requires.",
    )
    task_type: TaskType = Field(
        default=TaskType.HUMAN,
        description="Nature of work (human, ai, build, etc.).",
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING,
        description="Current lifecycle state.",
    )
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL,
        description="Scheduling priority.",
    )
    assigned_agent_id: str | None = Field(
        default=None,
        description="Agent currently responsible for this task.",
    )
    previous_agent_id: str | None = Field(
        default=None,
        description="Agent that previously held this task (populated on handoff).",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Workspace where this task is being executed.",
    )
    inputs: list[TaskInput] = Field(
        default_factory=list,
        description="Input parameters for this task.",
    )
    outputs: list[TaskOutput] = Field(
        default_factory=list,
        description="Output parameters produced by this task.",
    )
    attempts: list[TaskAttempt] = Field(
        default_factory=list,
        description="History of execution attempts.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this task starts.",
    )
    timeout_secs: int = Field(
        default=0,
        ge=0,
        description="Maximum execution time in seconds. 0 means no limit.",
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Maximum number of automatic retries on failure.",
    )
    current_retry: int = Field(
        default=0,
        ge=0,
        description="Current retry attempt number.",
    )
    error_message: str | None = Field(
        default=None,
        description="Human-readable error description if status=FAILED.",
    )
    error_details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured error details (stack trace, exception type, etc.).",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering and routing.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    assigned_at: datetime | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    due_at: datetime | None = Field(
        default=None,
        description="Optional deadline for task completion.",
    )

    @field_validator("task_id", "workflow_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def validate_retry_bounds(self) -> "Task":
        """Ensure current_retry does not exceed retry_count."""
        if self.current_retry > self.retry_count:
            raise ValueError(
                f"current_retry ({self.current_retry}) cannot exceed "
                f"retry_count ({self.retry_count})."
            )
        return self

    @property
    def is_terminal(self) -> bool:
        """Return True if the task is in a terminal state."""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
            TaskStatus.REVERTED,
        )

    @property
    def is_active(self) -> bool:
        """Return True if the task is currently being worked on."""
        return self.status in (
            TaskStatus.ACCEPTED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.CHECKPOINTED,
        )

    @property
    def duration_seconds(self) -> float | None:
        """Return execution duration in seconds, or None if not yet started."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    @property
    def current_attempt(self) -> TaskAttempt | None:
        """Return the most recent execution attempt, or None."""
        if not self.attempts:
            return None
        return max(self.attempts, key=lambda a: a.attempt_number)

    def get_input(self, name: str) -> Any:
        """Return the value of a named input parameter, or None if not found."""
        for inp in self.inputs:
            if inp.name == name:
                return inp.value
        return None

    def get_output(self, name: str) -> Any:
        """Return the value of a named output parameter, or None if not found."""
        for out in self.outputs:
            if out.name == name:
                return out.value
        return None
