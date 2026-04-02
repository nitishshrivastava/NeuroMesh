"""
shared/models/agent.py — FlowOS Agent Domain Model

In FlowOS every participant — human, machine, or AI — is a first-class Agent
with a CLI interface, a local workspace manager, and a Git-backed state
boundary.  This module defines the Pydantic domain model for agents.

Agent types:
- HUMAN    — A human operator interacting via the FlowOS CLI
- AI       — An autonomous AI agent running LangGraph/LangChain workflows
- BUILD    — A build/test runner (Kubernetes + Argo Workflows)
- DEPLOY   — A deployment agent managing environment rollouts
- SYSTEM   — Internal FlowOS system components (orchestrator, policy engine)
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


class AgentType(StrEnum):
    """Classifies the kind of participant an agent represents."""

    HUMAN = "human"
    AI = "ai"
    BUILD = "build"
    DEPLOY = "deploy"
    SYSTEM = "system"


class AgentStatus(StrEnum):
    """
    Operational status of an agent.

    Transitions:
        OFFLINE → ONLINE → IDLE → BUSY → IDLE
        ONLINE  → OFFLINE
        BUSY    → OFFLINE (unexpected disconnect)
    """

    OFFLINE = "offline"
    ONLINE = "online"
    IDLE = "idle"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    DEREGISTERED = "deregistered"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting models
# ─────────────────────────────────────────────────────────────────────────────


class AgentCapability(BaseModel):
    """
    A declared capability of an agent.

    Capabilities are used by the orchestrator to route tasks to agents that
    can handle them.  For example, an AI agent might declare capabilities
    for ``code_review``, ``test_generation``, and ``documentation``.

    Attributes:
        name:        Capability identifier (e.g. 'code_review').
        version:     Optional version of this capability.
        description: Human-readable description.
        parameters:  Optional parameters/constraints for this capability.
        enabled:     Whether this capability is currently active.
    """

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Capability identifier (e.g. 'code_review', 'python_build').",
    )
    version: str | None = Field(
        default=None,
        description="Optional version of this capability.",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable description of what this capability provides.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional parameters or constraints for this capability.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether this capability is currently active.",
    )


class AgentHeartbeat(BaseModel):
    """
    A heartbeat record from an agent.

    Agents periodically emit heartbeats to signal liveness.  The orchestrator
    uses heartbeats to detect agent failures and trigger task reassignment.

    Attributes:
        agent_id:    Agent that sent this heartbeat.
        status:      Agent status at the time of the heartbeat.
        task_id:     Currently executing task, if any.
        cpu_percent: CPU utilisation percentage (0-100).
        memory_mb:   Memory usage in megabytes.
        occurred_at: UTC timestamp of the heartbeat.
        metadata:    Arbitrary key/value metadata.
    """

    agent_id: str = Field(description="Agent that sent this heartbeat.")
    status: AgentStatus = Field(description="Agent status at heartbeat time.")
    task_id: str | None = Field(
        default=None,
        description="Currently executing task ID, if any.",
    )
    cpu_percent: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="CPU utilisation percentage.",
    )
    memory_mb: float | None = Field(
        default=None,
        ge=0.0,
        description="Memory usage in megabytes.",
    )
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the heartbeat.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Primary Domain Model
# ─────────────────────────────────────────────────────────────────────────────


class Agent(BaseModel):
    """
    A first-class participant in the FlowOS system.

    Every participant — human, machine, or AI — is represented as an Agent.
    Agents register with the system, declare their capabilities, and receive
    task assignments via Kafka events.

    Attributes:
        agent_id:          Globally unique agent identifier.
        name:              Human-readable display name.
        agent_type:        Kind of participant (human, ai, build, etc.).
        status:            Current operational status.
        capabilities:      List of declared capabilities.
        current_task_id:   Task currently being executed, if any.
        workspace_id:      Current workspace, if any.
        email:             Contact email (primarily for human agents).
        display_name:      Optional display name (defaults to name).
        avatar_url:        Optional avatar/profile image URL.
        timezone:          Agent's timezone (for human agents).
        kafka_group_id:    Kafka consumer group ID for this agent.
        max_concurrent_tasks: Maximum number of tasks this agent can handle.
        tags:              Arbitrary tags for filtering and routing.
        metadata:          Arbitrary key/value metadata.
        last_heartbeat_at: UTC timestamp of the last heartbeat.
        registered_at:     UTC timestamp when this agent registered.
        updated_at:        UTC timestamp of the last state change.
        deregistered_at:   UTC timestamp when this agent was deregistered.
    """

    agent_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique agent identifier.",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Human-readable display name.",
    )
    agent_type: AgentType = Field(
        description="Kind of participant (human, ai, build, deploy, system).",
    )
    status: AgentStatus = Field(
        default=AgentStatus.OFFLINE,
        description="Current operational status.",
    )
    capabilities: list[AgentCapability] = Field(
        default_factory=list,
        description="List of declared capabilities.",
    )
    current_task_id: str | None = Field(
        default=None,
        description="Task currently being executed, if any.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Current workspace ID, if any.",
    )
    email: str | None = Field(
        default=None,
        description="Contact email (primarily for human agents).",
    )
    display_name: str | None = Field(
        default=None,
        max_length=255,
        description="Optional display name (defaults to name if not set).",
    )
    avatar_url: str | None = Field(
        default=None,
        description="Optional avatar/profile image URL.",
    )
    timezone: str = Field(
        default="UTC",
        description="Agent's timezone (IANA timezone name, e.g. 'America/New_York').",
    )
    kafka_group_id: str | None = Field(
        default=None,
        description="Kafka consumer group ID for this agent's event subscription.",
    )
    max_concurrent_tasks: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Maximum number of tasks this agent can handle concurrently.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering and routing.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key/value metadata.",
    )
    last_heartbeat_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the last heartbeat received.",
    )
    registered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when this agent registered.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the last state change.",
    )
    deregistered_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when this agent was deregistered.",
    )

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        """Ensure agent_id is a valid UUID."""
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"agent_id must be a valid UUID, got: {v!r}") from exc
        return v

    @property
    def effective_display_name(self) -> str:
        """Return display_name if set, otherwise name."""
        return self.display_name or self.name

    @property
    def is_available(self) -> bool:
        """Return True if the agent can accept new tasks."""
        return self.status == AgentStatus.IDLE

    @property
    def is_online(self) -> bool:
        """Return True if the agent is connected to the system."""
        return self.status in (AgentStatus.ONLINE, AgentStatus.IDLE, AgentStatus.BUSY)

    def has_capability(self, capability_name: str) -> bool:
        """Return True if the agent has the named capability enabled."""
        return any(
            c.name == capability_name and c.enabled
            for c in self.capabilities
        )

    def get_capability(self, capability_name: str) -> AgentCapability | None:
        """Return the named capability, or None if not found."""
        for cap in self.capabilities:
            if cap.name == capability_name:
                return cap
        return None
