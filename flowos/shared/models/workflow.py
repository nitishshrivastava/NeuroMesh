"""
shared/models/workflow.py — FlowOS Workflow Domain Model

A Workflow is the top-level orchestration unit in FlowOS.  It is defined
declaratively in a YAML DSL, parsed by the orchestrator, and executed as a
Temporal workflow.  This module provides the Pydantic domain model used for
in-memory representation, API responses, and Kafka event payloads.

Workflow lifecycle:
    PENDING → RUNNING → COMPLETED
                      ↘ FAILED
                      ↘ CANCELLED
    RUNNING → PAUSED → RUNNING (resumed)
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


class WorkflowStatus(StrEnum):
    """Lifecycle states of a FlowOS workflow."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowTrigger(StrEnum):
    """How a workflow was initiated."""

    MANUAL = "manual"          # Triggered by a human via CLI or API
    SCHEDULED = "scheduled"    # Triggered by a cron/schedule
    WEBHOOK = "webhook"        # Triggered by an external webhook
    EVENT = "event"            # Triggered by a Kafka event
    API = "api"                # Triggered programmatically via REST API
    RETRY = "retry"            # Automatic retry of a failed workflow


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class WorkflowStep(BaseModel):
    """
    A single step within a workflow definition.

    Steps are the atomic units of work within a workflow.  Each step maps to
    one or more tasks that will be assigned to agents.

    Attributes:
        step_id:      Unique identifier for this step within the workflow.
        name:         Human-readable step name.
        description:  Optional description of what this step does.
        agent_type:   Type of agent required to execute this step.
        depends_on:   List of step_ids that must complete before this step.
        timeout_secs: Maximum execution time in seconds (0 = no limit).
        retry_count:  Number of automatic retries on failure.
        inputs:       Static input parameters for this step.
        tags:         Arbitrary tags for filtering and routing.
    """

    step_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this step within the workflow.",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable step name.",
    )
    description: str | None = Field(
        default=None,
        description="Optional description of what this step does.",
    )
    agent_type: str = Field(
        description="Type of agent required to execute this step (e.g. 'human', 'ai', 'build').",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="List of step_ids that must complete before this step starts.",
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
        description="Number of automatic retries on failure.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Static input parameters passed to the agent executing this step.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering and routing.",
    )


class WorkflowDefinition(BaseModel):
    """
    The parsed YAML DSL definition of a workflow.

    This is the static blueprint — it does not change once a workflow is
    created.  The orchestrator uses this to spawn tasks and track progress.

    Attributes:
        name:        Workflow name (must be unique within a project).
        version:     Semantic version of this workflow definition.
        description: Optional human-readable description.
        steps:       Ordered list of workflow steps.
        inputs:      Schema/defaults for workflow-level input parameters.
        outputs:     Schema/defaults for workflow-level output parameters.
        tags:        Arbitrary tags for filtering and search.
        dsl_source:  Raw YAML source string (preserved for audit).
    """

    name: str = Field(
        min_length=1,
        max_length=255,
        description="Workflow name (must be unique within a project).",
    )
    version: str = Field(
        default="1.0.0",
        description="Semantic version of this workflow definition.",
    )
    description: str | None = Field(
        default=None,
        description="Optional human-readable description.",
    )
    steps: list[WorkflowStep] = Field(
        default_factory=list,
        description="Ordered list of workflow steps.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Schema/defaults for workflow-level input parameters.",
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Schema/defaults for workflow-level output parameters.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering and search.",
    )
    dsl_source: str | None = Field(
        default=None,
        description="Raw YAML source string preserved for audit and replay.",
    )

    @field_validator("steps")
    @classmethod
    def validate_step_dependencies(cls, steps: list[WorkflowStep]) -> list[WorkflowStep]:
        """Ensure all step dependencies reference valid step IDs."""
        step_ids = {s.step_id for s in steps}
        for step in steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    raise ValueError(
                        f"Step {step.name!r} depends on unknown step_id {dep!r}. "
                        f"Valid step IDs: {sorted(step_ids)}"
                    )
        return steps


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Workflow(BaseModel):
    """
    A running instance of a workflow definition.

    Represents the live execution state of a workflow.  The orchestrator
    creates one Workflow record per execution and updates it as tasks
    progress through their lifecycle.

    Attributes:
        workflow_id:       Globally unique workflow instance identifier.
        name:              Human-readable workflow name.
        status:            Current lifecycle state.
        trigger:           How this workflow was initiated.
        definition:        The parsed workflow definition (blueprint).
        temporal_run_id:   Temporal workflow run ID for durable execution.
        temporal_workflow_id: Temporal workflow ID.
        owner_agent_id:    Agent that owns / initiated this workflow.
        project:           Optional project/namespace this workflow belongs to.
        inputs:            Runtime input parameters provided at trigger time.
        outputs:           Collected output parameters from completed steps.
        error_message:     Human-readable error description if status=FAILED.
        error_details:     Structured error details (stack trace, etc.).
        tags:              Arbitrary tags for filtering and search.
        created_at:        UTC timestamp when this workflow instance was created.
        started_at:        UTC timestamp when execution began.
        completed_at:      UTC timestamp when execution finished (any terminal state).
        updated_at:        UTC timestamp of the last state change.
    """

    workflow_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique workflow instance identifier.",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable workflow name.",
    )
    status: WorkflowStatus = Field(
        default=WorkflowStatus.PENDING,
        description="Current lifecycle state.",
    )
    trigger: WorkflowTrigger = Field(
        default=WorkflowTrigger.MANUAL,
        description="How this workflow was initiated.",
    )
    definition: WorkflowDefinition | None = Field(
        default=None,
        description="The parsed workflow definition (blueprint).",
    )
    temporal_run_id: str | None = Field(
        default=None,
        description="Temporal workflow run ID for durable execution tracking.",
    )
    temporal_workflow_id: str | None = Field(
        default=None,
        description="Temporal workflow ID (stable across retries).",
    )
    owner_agent_id: str | None = Field(
        default=None,
        description="Agent that owns / initiated this workflow.",
    )
    project: str | None = Field(
        default=None,
        max_length=255,
        description="Optional project/namespace this workflow belongs to.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime input parameters provided at trigger time.",
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Collected output parameters from completed steps.",
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
        description="Arbitrary tags for filtering and search.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when this workflow instance was created.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when execution began.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when execution finished (any terminal state).",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the last state change.",
    )

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, v: str) -> str:
        """Ensure workflow_id is a valid UUID."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"workflow_id must be a valid UUID, got: {v!r}") from exc
        return v

    @property
    def is_terminal(self) -> bool:
        """Return True if the workflow is in a terminal state."""
        return self.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        )

    @property
    def is_active(self) -> bool:
        """Return True if the workflow is currently executing."""
        return self.status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED)

    @property
    def duration_seconds(self) -> float | None:
        """Return execution duration in seconds, or None if not yet complete."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()
