"""
shared/models — FlowOS Domain Model Package

Pydantic-based domain models for all core FlowOS entities.  These models are
used for:

- Kafka event payloads (serialisation / deserialisation)
- API request/response schemas
- Inter-service data transfer objects
- Validation of workflow DSL inputs

All models use ``from __future__ import annotations`` for forward-reference
support and are built on Pydantic v2.
"""

from shared.models.agent import (
    Agent,
    AgentCapability,
    AgentStatus,
    AgentType,
)
from shared.models.checkpoint import (
    Checkpoint,
    CheckpointStatus,
    CheckpointType,
)
from shared.models.event import (
    EventRecord,
    EventSeverity,
    EventSource,
    EventTopic,
    EventType,
)
from shared.models.handoff import (
    Handoff,
    HandoffStatus,
    HandoffType,
)
from shared.models.reasoning import (
    ReasoningSession,
    ReasoningStep,
    ReasoningStepType,
    ReasoningStatus,
    ToolCall,
)
from shared.models.task import (
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from shared.models.workflow import (
    Workflow,
    WorkflowStatus,
    WorkflowTrigger,
)
from shared.models.workspace import (
    Workspace,
    WorkspaceStatus,
    WorkspaceType,
)

__all__ = [
    # Agent
    "Agent",
    "AgentCapability",
    "AgentStatus",
    "AgentType",
    # Checkpoint
    "Checkpoint",
    "CheckpointStatus",
    "CheckpointType",
    # Event
    "EventRecord",
    "EventSeverity",
    "EventSource",
    "EventTopic",
    "EventType",
    # Handoff
    "Handoff",
    "HandoffStatus",
    "HandoffType",
    # Reasoning
    "ReasoningSession",
    "ReasoningStep",
    "ReasoningStepType",
    "ReasoningStatus",
    "ToolCall",
    # Task
    "Task",
    "TaskPriority",
    "TaskStatus",
    "TaskType",
    # Workflow
    "Workflow",
    "WorkflowStatus",
    "WorkflowTrigger",
    # Workspace
    "Workspace",
    "WorkspaceStatus",
    "WorkspaceType",
]
