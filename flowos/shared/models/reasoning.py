"""
shared/models/reasoning.py — FlowOS AI Reasoning Domain Model

Defines the Pydantic models for AI reasoning sessions, steps, and tool calls.
These models capture the complete reasoning trace of an AI agent as it works
through a task using LangGraph/LangChain.

The reasoning trace is:
- Stored in the database for audit and replay
- Streamed to the UI via Kafka (flowos.ai.events topic)
- Used by the policy engine to evaluate AI decisions
- Available to human agents for review before applying AI proposals

Reasoning session structure:
    ReasoningSession
    └── ReasoningStep[]
        └── ToolCall[]  (for TOOL_CALL steps)
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


class ReasoningStatus(StrEnum):
    """Lifecycle states of a reasoning session."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_HUMAN = "awaiting_human"  # Paused waiting for human input


class ReasoningStepType(StrEnum):
    """
    Classifies the type of reasoning step.

    - THOUGHT:      Internal reasoning / chain-of-thought step.
    - TOOL_CALL:    The AI called an external tool or function.
    - OBSERVATION:  The AI observed the result of a tool call.
    - PLAN:         The AI produced a structured plan.
    - DECISION:     The AI made a decision (e.g. which approach to take).
    - PROPOSAL:     The AI proposed a concrete action (e.g. a code patch).
    - REFLECTION:   The AI reflected on its previous steps.
    - HUMAN_INPUT:  A human provided input to the reasoning session.
    - FINAL_ANSWER: The AI produced its final answer/output.
    - ERROR:        An error occurred during this step.
    """

    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    PLAN = "plan"
    DECISION = "decision"
    PROPOSAL = "proposal"
    REFLECTION = "reflection"
    HUMAN_INPUT = "human_input"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class ToolCall(BaseModel):
    """
    A record of a single tool/function call made by the AI agent.

    Tool calls are the primary way AI agents interact with the external world
    (reading files, running commands, querying APIs, etc.).

    Attributes:
        tool_call_id:  Unique identifier for this tool call.
        tool_name:     Name of the tool/function called.
        tool_input:    Input arguments passed to the tool.
        tool_output:   Output returned by the tool.
        error:         Error message if the tool call failed.
        duration_ms:   Execution time in milliseconds.
        started_at:    UTC timestamp when the tool call started.
        ended_at:      UTC timestamp when the tool call ended.
        is_error:      True if the tool call resulted in an error.
    """

    tool_call_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this tool call.",
    )
    tool_name: str = Field(
        min_length=1,
        max_length=255,
        description="Name of the tool/function called.",
    )
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Input arguments passed to the tool.",
    )
    tool_output: Any = Field(
        default=None,
        description="Output returned by the tool (any JSON-serialisable type).",
    )
    error: str | None = Field(
        default=None,
        description="Error message if the tool call failed.",
    )
    duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="Execution time in milliseconds.",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    ended_at: datetime | None = Field(default=None)
    is_error: bool = Field(
        default=False,
        description="True if the tool call resulted in an error.",
    )


class ReasoningStep(BaseModel):
    """
    A single step in an AI reasoning session.

    Steps are the atomic units of the reasoning trace.  They capture what
    the AI was thinking, what tools it called, and what it observed.

    Attributes:
        step_id:       Unique identifier for this step.
        session_id:    Reasoning session this step belongs to.
        step_type:     Type of reasoning step.
        sequence:      Monotonically increasing sequence number within a session.
        content:       The text content of this step (thought, plan, etc.).
        tool_calls:    Tool calls made during this step (for TOOL_CALL steps).
        model_name:    LLM model used for this step.
        prompt_tokens: Number of prompt tokens consumed.
        output_tokens: Number of output tokens produced.
        total_tokens:  Total tokens consumed (prompt + output).
        confidence:    AI's self-reported confidence (0.0-1.0).
        metadata:      Arbitrary key/value metadata.
        created_at:    UTC timestamp when this step was created.
        duration_ms:   Time taken to produce this step in milliseconds.
    """

    step_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this step.",
    )
    session_id: str = Field(
        description="Reasoning session this step belongs to.",
    )
    step_type: ReasoningStepType = Field(
        description="Type of reasoning step.",
    )
    sequence: int = Field(
        ge=1,
        description="Monotonically increasing sequence number within a session.",
    )
    content: str = Field(
        description="The text content of this step.",
    )
    tool_calls: list[ToolCall] = Field(
        default_factory=list,
        description="Tool calls made during this step.",
    )
    model_name: str | None = Field(
        default=None,
        description="LLM model used for this step (e.g. 'gpt-4o', 'claude-3-5-sonnet').",
    )
    prompt_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Number of prompt tokens consumed.",
    )
    output_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Number of output tokens produced.",
    )
    total_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Total tokens consumed (prompt + output).",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="AI's self-reported confidence score (0.0-1.0).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    duration_ms: int | None = Field(
        default=None,
        ge=0,
        description="Time taken to produce this step in milliseconds.",
    )

    @field_validator("step_id", "session_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class ReasoningSession(BaseModel):
    """
    A complete AI reasoning session for a task.

    A reasoning session captures the full trace of an AI agent's thought
    process as it works through a task.  Sessions are associated with a
    specific task and agent, and contain an ordered list of reasoning steps.

    Attributes:
        session_id:        Globally unique session identifier.
        task_id:           Task this session is working on.
        workflow_id:       Workflow this session is associated with.
        agent_id:          AI agent running this session.
        status:            Current lifecycle state.
        objective:         The goal/objective for this reasoning session.
        steps:             Ordered list of reasoning steps.
        final_output:      The final output produced by this session.
        proposal:          Structured proposal (e.g. code patch) if applicable.
        model_name:        Primary LLM model used.
        total_prompt_tokens:  Total prompt tokens across all steps.
        total_output_tokens:  Total output tokens across all steps.
        total_tokens:      Total tokens consumed.
        estimated_cost_usd: Estimated cost in USD.
        langgraph_run_id:  LangGraph run ID for tracing.
        langsmith_run_id:  LangSmith run ID for observability.
        error_message:     Error description if status=FAILED.
        requires_human_review: True if the AI requests human review.
        human_review_reason:   Reason for requesting human review.
        tags:              Arbitrary tags for filtering.
        metadata:          Arbitrary key/value metadata.
        started_at:        UTC timestamp when this session started.
        completed_at:      UTC timestamp when this session completed.
        updated_at:        UTC timestamp of the last state change.
    """

    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique session identifier.",
    )
    task_id: str = Field(
        description="Task this session is working on.",
    )
    workflow_id: str = Field(
        description="Workflow this session is associated with.",
    )
    agent_id: str = Field(
        description="AI agent running this session.",
    )
    status: ReasoningStatus = Field(
        default=ReasoningStatus.PENDING,
        description="Current lifecycle state.",
    )
    objective: str = Field(
        min_length=1,
        description="The goal/objective for this reasoning session.",
    )
    steps: list[ReasoningStep] = Field(
        default_factory=list,
        description="Ordered list of reasoning steps.",
    )
    final_output: str | None = Field(
        default=None,
        description="The final output produced by this session.",
    )
    proposal: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured proposal produced by this session "
            "(e.g. code patch, configuration change)."
        ),
    )
    model_name: str | None = Field(
        default=None,
        description="Primary LLM model used (e.g. 'gpt-4o', 'claude-3-5-sonnet').",
    )
    total_prompt_tokens: int = Field(
        default=0,
        ge=0,
        description="Total prompt tokens consumed across all steps.",
    )
    total_output_tokens: int = Field(
        default=0,
        ge=0,
        description="Total output tokens produced across all steps.",
    )
    total_tokens: int = Field(
        default=0,
        ge=0,
        description="Total tokens consumed (prompt + output).",
    )
    estimated_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Estimated cost in USD based on token usage.",
    )
    langgraph_run_id: str | None = Field(
        default=None,
        description="LangGraph run ID for distributed tracing.",
    )
    langsmith_run_id: str | None = Field(
        default=None,
        description="LangSmith run ID for observability and debugging.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description if status=FAILED.",
    )
    requires_human_review: bool = Field(
        default=False,
        description="True if the AI requests human review before applying its proposal.",
    )
    human_review_reason: str | None = Field(
        default=None,
        description="Reason why the AI is requesting human review.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    completed_at: datetime | None = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("session_id", "task_id", "workflow_id", "agent_id")
    @classmethod
    def validate_uuid_fields(cls, v: str) -> str:
        """Ensure UUID fields contain valid UUIDs."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"Field must be a valid UUID, got: {v!r}") from exc
        return v

    @property
    def is_terminal(self) -> bool:
        """Return True if the session is in a terminal state."""
        return self.status in (
            ReasoningStatus.COMPLETED,
            ReasoningStatus.FAILED,
            ReasoningStatus.CANCELLED,
        )

    @property
    def step_count(self) -> int:
        """Return the total number of reasoning steps."""
        return len(self.steps)

    @property
    def tool_call_count(self) -> int:
        """Return the total number of tool calls across all steps."""
        return sum(len(step.tool_calls) for step in self.steps)

    @property
    def duration_seconds(self) -> float | None:
        """Return session duration in seconds, or None if not yet complete."""
        end = self.completed_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def get_steps_by_type(self, step_type: ReasoningStepType) -> list[ReasoningStep]:
        """Return all steps of the given type."""
        return [s for s in self.steps if s.step_type == step_type]

    def get_latest_step(self) -> ReasoningStep | None:
        """Return the most recent reasoning step, or None."""
        if not self.steps:
            return None
        return max(self.steps, key=lambda s: s.sequence)
