"""
shared/models/handoff.py — FlowOS Handoff Domain Model

A Handoff is the transfer of task ownership from one agent to another.
Handoffs are a first-class concept in FlowOS — they allow human agents to
delegate work to AI agents, AI agents to escalate to humans, and build runners
to pass artifacts to deployment agents.

The handoff process:
1. Source agent requests a handoff (TASK_HANDOFF_REQUESTED event)
2. A checkpoint is created capturing the current workspace state
3. Target agent receives the handoff request (via Kafka)
4. Target agent accepts the handoff (TASK_HANDOFF_ACCEPTED event)
5. Target agent clones/syncs the workspace from the checkpoint
6. Task ownership transfers to the target agent

Handoff lifecycle:
    REQUESTED → PENDING_ACCEPTANCE → ACCEPTED → COMPLETED
    REQUESTED → REJECTED
    PENDING_ACCEPTANCE → EXPIRED (timeout)
    ACCEPTED → FAILED
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


class HandoffStatus(StrEnum):
    """Lifecycle states of a handoff."""

    REQUESTED = "requested"
    PENDING_ACCEPTANCE = "pending_acceptance"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HandoffType(StrEnum):
    """
    Classifies the reason for a handoff.

    - DELEGATION:   Source agent delegates work to a more capable agent.
    - ESCALATION:   Source agent escalates to a human for review/decision.
    - COMPLETION:   Source agent has finished its portion; target continues.
    - REVIEW:       Source agent requests review from another agent.
    - EMERGENCY:    Urgent handoff due to agent failure or unavailability.
    - SCHEDULED:    Pre-planned handoff at a workflow step boundary.
    - AI_TO_HUMAN:  AI agent escalates to human for approval or guidance.
    - HUMAN_TO_AI:  Human delegates routine work to an AI agent.
    """

    DELEGATION = "delegation"
    ESCALATION = "escalation"
    COMPLETION = "completion"
    REVIEW = "review"
    EMERGENCY = "emergency"
    SCHEDULED = "scheduled"
    AI_TO_HUMAN = "ai_to_human"
    HUMAN_TO_AI = "human_to_ai"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class HandoffContext(BaseModel):
    """
    Context information passed from source to target agent during a handoff.

    This is the "briefing document" that helps the target agent understand
    what has been done and what remains to be done.

    Attributes:
        summary:          Human-readable summary of work completed so far.
        remaining_work:   Description of what still needs to be done.
        known_issues:     List of known issues or blockers.
        suggested_approach: Suggested approach for the target agent.
        relevant_files:   List of files most relevant to the remaining work.
        references:       External references (URLs, docs, tickets).
        custom_data:      Arbitrary structured data for the target agent.
    """

    summary: str = Field(
        description="Human-readable summary of work completed so far.",
    )
    remaining_work: str | None = Field(
        default=None,
        description="Description of what still needs to be done.",
    )
    known_issues: list[str] = Field(
        default_factory=list,
        description="List of known issues or blockers.",
    )
    suggested_approach: str | None = Field(
        default=None,
        description="Suggested approach for the target agent.",
    )
    relevant_files: list[str] = Field(
        default_factory=list,
        description="List of file paths most relevant to the remaining work.",
    )
    references: list[str] = Field(
        default_factory=list,
        description="External references (URLs, documentation, ticket IDs).",
    )
    custom_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured data for the target agent.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Handoff(BaseModel):
    """
    A transfer of task ownership from one agent to another.

    Handoffs are a first-class concept in FlowOS.  They are tracked as
    persistent records so the complete ownership history of a task is
    always available for audit and replay.

    Attributes:
        handoff_id:          Globally unique handoff identifier.
        task_id:             Task being handed off.
        workflow_id:         Workflow this handoff is associated with.
        source_agent_id:     Agent initiating the handoff.
        target_agent_id:     Agent receiving the handoff (None if broadcast).
        handoff_type:        Reason for the handoff.
        status:              Current lifecycle state.
        checkpoint_id:       Checkpoint created before the handoff.
        workspace_id:        Workspace being transferred.
        context:             Briefing context for the target agent.
        rejection_reason:    Reason for rejection if status=REJECTED.
        failure_reason:      Reason for failure if status=FAILED.
        priority:            Urgency of this handoff request.
        expires_at:          UTC timestamp after which the handoff expires.
        accepted_at:         UTC timestamp when the target agent accepted.
        completed_at:        UTC timestamp when the handoff completed.
        tags:                Arbitrary tags for filtering.
        metadata:            Arbitrary key/value metadata.
        requested_at:        UTC timestamp when the handoff was requested.
        updated_at:          UTC timestamp of the last state change.
    """

    handoff_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique handoff identifier.",
    )
    task_id: str = Field(
        description="Task being handed off.",
    )
    workflow_id: str = Field(
        description="Workflow this handoff is associated with.",
    )
    source_agent_id: str = Field(
        description="Agent initiating the handoff.",
    )
    target_agent_id: str | None = Field(
        default=None,
        description=(
            "Agent receiving the handoff. None means the orchestrator will "
            "select an appropriate agent."
        ),
    )
    handoff_type: HandoffType = Field(
        default=HandoffType.DELEGATION,
        description="Reason for the handoff.",
    )
    status: HandoffStatus = Field(
        default=HandoffStatus.REQUESTED,
        description="Current lifecycle state.",
    )
    checkpoint_id: str | None = Field(
        default=None,
        description="Checkpoint created before the handoff to capture workspace state.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Workspace being transferred.",
    )
    context: HandoffContext | None = Field(
        default=None,
        description="Briefing context for the target agent.",
    )
    rejection_reason: str | None = Field(
        default=None,
        description="Reason for rejection if status=REJECTED.",
    )
    failure_reason: str | None = Field(
        default=None,
        description="Reason for failure if status=FAILED.",
    )
    priority: str = Field(
        default="normal",
        description="Urgency of this handoff request (low, normal, high, critical).",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="UTC timestamp after which the handoff expires if not accepted.",
    )
    accepted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the target agent accepted the handoff.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the handoff completed.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the handoff was requested.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the last state change.",
    )

    @field_validator("handoff_id", "task_id", "workflow_id", "source_agent_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def validate_source_target_differ(self) -> "Handoff":
        """Ensure source and target agents are different."""
        if (
            self.target_agent_id is not None
            and self.source_agent_id == self.target_agent_id
        ):
            raise ValueError(
                "source_agent_id and target_agent_id must be different agents."
            )
        return self

    @model_validator(mode="after")
    def validate_rejection_reason_present(self) -> "Handoff":
        """Ensure rejection_reason is set when status=REJECTED."""
        if self.status == HandoffStatus.REJECTED and not self.rejection_reason:
            raise ValueError(
                "rejection_reason must be provided when status is REJECTED."
            )
        return self

    @property
    def is_terminal(self) -> bool:
        """Return True if the handoff is in a terminal state."""
        return self.status in (
            HandoffStatus.COMPLETED,
            HandoffStatus.REJECTED,
            HandoffStatus.EXPIRED,
            HandoffStatus.FAILED,
            HandoffStatus.CANCELLED,
        )

    @property
    def is_pending(self) -> bool:
        """Return True if the handoff is awaiting acceptance."""
        return self.status in (
            HandoffStatus.REQUESTED,
            HandoffStatus.PENDING_ACCEPTANCE,
        )

    @property
    def response_time_seconds(self) -> float | None:
        """Return time from request to acceptance in seconds, or None."""
        if self.accepted_at is None:
            return None
        return (self.accepted_at - self.requested_at).total_seconds()
