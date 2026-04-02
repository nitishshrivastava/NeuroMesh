"""
shared/kafka/schemas.py — FlowOS Kafka Event Payload Schemas

Defines strongly-typed Pydantic schemas for the ``payload`` field of every
``EventRecord`` variant.  These schemas serve as the contract between
producers and consumers: producers validate their payloads before sending;
consumers validate after deserialising.

Design:
- Each schema is a frozen Pydantic ``BaseModel`` named ``<EventType>Payload``.
- All schemas inherit from ``BaseEventPayload`` which provides common helpers.
- The ``EVENT_PAYLOAD_SCHEMAS`` registry maps ``EventType`` → schema class.
- ``build_event`` is a convenience factory that constructs a fully-validated
  ``EventRecord`` from a payload object, automatically selecting the correct
  topic via ``topic_for_event``.

Usage::

    from shared.kafka.schemas import WorkflowCreatedPayload, build_event
    from shared.models.event import EventSource, EventType

    payload = WorkflowCreatedPayload(
        workflow_id="...",
        name="deploy-service",
        trigger="manual",
        owner_agent_id="agent-123",
    )
    event = build_event(
        event_type=EventType.WORKFLOW_CREATED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
    )
    # event is a fully-validated EventRecord ready for produce()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Type

from pydantic import BaseModel, Field

from shared.models.event import EventRecord, EventSource, EventTopic, EventType
from shared.kafka.topics import KafkaTopic, topic_for_event


# ─────────────────────────────────────────────────────────────────────────────
# Base payload
# ─────────────────────────────────────────────────────────────────────────────


class BaseEventPayload(BaseModel):
    """
    Base class for all FlowOS event payload schemas.

    Provides:
    - ``to_dict()``  — serialise to a plain dict for embedding in EventRecord.
    - ``schema_version`` — forward-compatibility version string.
    """

    model_config = {"frozen": True}

    schema_version: str = Field(
        default="1.0",
        description="Payload schema version for forward compatibility.",
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise this payload to a plain dict (JSON-safe)."""
        return self.model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────────────────
# Workflow event payloads
# ─────────────────────────────────────────────────────────────────────────────


class WorkflowCreatedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_CREATED`` events."""

    workflow_id: str = Field(description="Globally unique workflow instance ID.")
    name: str = Field(description="Human-readable workflow name.")
    trigger: str = Field(description="How the workflow was initiated (manual, scheduled, etc.).")
    owner_agent_id: str | None = Field(
        default=None,
        description="Agent that owns / initiated this workflow.",
    )
    project: str | None = Field(
        default=None,
        description="Optional project/namespace this workflow belongs to.",
    )
    definition_name: str | None = Field(
        default=None,
        description="Name of the workflow definition (DSL blueprint).",
    )
    definition_version: str | None = Field(
        default=None,
        description="Version of the workflow definition.",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime input parameters provided at trigger time.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Arbitrary tags for filtering and search.",
    )


class WorkflowStartedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_STARTED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    temporal_workflow_id: str | None = Field(
        default=None,
        description="Temporal workflow ID (stable across retries).",
    )
    temporal_run_id: str | None = Field(
        default=None,
        description="Temporal workflow run ID.",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when execution began.",
    )


class WorkflowPausedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_PAUSED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    reason: str | None = Field(default=None, description="Reason for pausing.")
    paused_by: str | None = Field(default=None, description="Agent or system that paused the workflow.")
    paused_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class WorkflowResumedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_RESUMED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    resumed_by: str | None = Field(default=None, description="Agent or system that resumed the workflow.")
    resumed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class WorkflowCompletedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_COMPLETED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Collected output parameters from completed steps.",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Total execution duration in seconds.",
    )
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class WorkflowFailedPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_FAILED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    error_message: str = Field(description="Human-readable error description.")
    error_details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured error details (stack trace, exception type, etc.).",
    )
    failed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class WorkflowCancelledPayload(BaseEventPayload):
    """Payload for ``WORKFLOW_CANCELLED`` events."""

    workflow_id: str = Field(description="Workflow instance ID.")
    name: str = Field(description="Workflow name.")
    reason: str | None = Field(default=None, description="Cancellation reason.")
    cancelled_by: str | None = Field(default=None, description="Agent or system that cancelled.")
    cancelled_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task event payloads
# ─────────────────────────────────────────────────────────────────────────────


class TaskCreatedPayload(BaseEventPayload):
    """Payload for ``TASK_CREATED`` events."""

    task_id: str = Field(description="Globally unique task ID.")
    workflow_id: str = Field(description="Workflow this task belongs to.")
    step_id: str | None = Field(default=None, description="Workflow step this task was created from.")
    name: str = Field(description="Human-readable task name.")
    task_type: str = Field(description="Nature of work (human, ai, build, etc.).")
    priority: str = Field(default="normal", description="Scheduling priority.")
    depends_on: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this task starts.",
    )
    inputs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Input parameters for this task.",
    )
    tags: list[str] = Field(default_factory=list)


class TaskAssignedPayload(BaseEventPayload):
    """Payload for ``TASK_ASSIGNED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    name: str = Field(description="Task name.")
    assigned_agent_id: str = Field(description="Agent this task was assigned to.")
    task_type: str = Field(description="Nature of work.")
    priority: str = Field(default="normal")
    assigned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskAcceptedPayload(BaseEventPayload):
    """Payload for ``TASK_ACCEPTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="Agent that accepted the task.")
    accepted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskStartedPayload(BaseEventPayload):
    """Payload for ``TASK_STARTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="Agent executing the task.")
    attempt_number: int = Field(default=1, ge=1, description="Execution attempt number.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskUpdatedPayload(BaseEventPayload):
    """Payload for ``TASK_UPDATED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="Agent that produced the update.")
    update_type: str = Field(description="Type of update (progress, status, etc.).")
    progress_pct: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Optional progress percentage (0–100).",
    )
    message: str | None = Field(default=None, description="Human-readable update message.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCheckpointedPayload(BaseEventPayload):
    """Payload for ``TASK_CHECKPOINTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="Agent that created the checkpoint.")
    checkpoint_id: str = Field(description="Checkpoint identifier.")
    git_commit_sha: str | None = Field(default=None, description="Git commit SHA of the checkpoint.")
    message: str | None = Field(default=None, description="Checkpoint message.")
    checkpointed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskCompletedPayload(BaseEventPayload):
    """Payload for ``TASK_COMPLETED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="Agent that completed the task.")
    outputs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Output parameters produced by this task.",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Task execution duration in seconds.",
    )
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskFailedPayload(BaseEventPayload):
    """Payload for ``TASK_FAILED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str | None = Field(default=None, description="Agent that was executing the task.")
    error_message: str = Field(description="Human-readable error description.")
    error_details: dict[str, Any] = Field(default_factory=dict)
    attempt_number: int = Field(default=1, ge=1)
    will_retry: bool = Field(default=False, description="Whether the task will be retried.")
    failed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskRevertRequestedPayload(BaseEventPayload):
    """Payload for ``TASK_REVERT_REQUESTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    requested_by: str = Field(description="Agent or system requesting the revert.")
    target_checkpoint_id: str | None = Field(
        default=None,
        description="Checkpoint to revert to (None = revert to initial state).",
    )
    reason: str | None = Field(default=None)
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskHandoffRequestedPayload(BaseEventPayload):
    """Payload for ``TASK_HANDOFF_REQUESTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    from_agent_id: str = Field(description="Agent handing off the task.")
    to_agent_id: str = Field(description="Agent receiving the task.")
    handoff_id: str = Field(description="Handoff record identifier.")
    reason: str | None = Field(default=None, description="Reason for the handoff.")
    context_summary: str | None = Field(
        default=None,
        description="Summary of work done so far for the receiving agent.",
    )
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TaskHandoffAcceptedPayload(BaseEventPayload):
    """Payload for ``TASK_HANDOFF_ACCEPTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    handoff_id: str = Field(description="Handoff record identifier.")
    new_agent_id: str = Field(description="Agent that accepted the handoff.")
    accepted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent event payloads
# ─────────────────────────────────────────────────────────────────────────────


class AgentRegisteredPayload(BaseEventPayload):
    """Payload for ``AGENT_REGISTERED`` events."""

    agent_id: str = Field(description="Globally unique agent ID.")
    name: str = Field(description="Human-readable agent name.")
    agent_type: str = Field(description="Agent type (human, ai, build_runner, etc.).")
    capabilities: list[str] = Field(
        default_factory=list,
        description="List of capability names this agent supports.",
    )
    registered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AgentOnlinePayload(BaseEventPayload):
    """Payload for ``AGENT_ONLINE`` events."""

    agent_id: str = Field(description="Agent ID.")
    name: str = Field(description="Agent name.")
    online_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AgentOfflinePayload(BaseEventPayload):
    """Payload for ``AGENT_OFFLINE`` events."""

    agent_id: str = Field(description="Agent ID.")
    name: str = Field(description="Agent name.")
    reason: str | None = Field(default=None, description="Reason for going offline.")
    offline_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AgentBusyPayload(BaseEventPayload):
    """Payload for ``AGENT_BUSY`` events."""

    agent_id: str = Field(description="Agent ID.")
    current_task_id: str | None = Field(default=None, description="Task the agent is working on.")
    busy_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AgentIdlePayload(BaseEventPayload):
    """Payload for ``AGENT_IDLE`` events."""

    agent_id: str = Field(description="Agent ID.")
    idle_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AgentCapabilityUpdatedPayload(BaseEventPayload):
    """Payload for ``AGENT_CAPABILITY_UPDATED`` events."""

    agent_id: str = Field(description="Agent ID.")
    capabilities: list[str] = Field(description="Updated list of capability names.")
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build event payloads
# ─────────────────────────────────────────────────────────────────────────────


class BuildTriggeredPayload(BaseEventPayload):
    """Payload for ``BUILD_TRIGGERED`` events."""

    build_id: str = Field(description="Globally unique build ID.")
    task_id: str | None = Field(default=None, description="Task that triggered this build.")
    workflow_id: str | None = Field(default=None, description="Workflow this build belongs to.")
    repository: str = Field(description="Repository URL or name.")
    branch: str = Field(description="Branch to build.")
    commit_sha: str | None = Field(default=None, description="Specific commit SHA to build.")
    build_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Build configuration parameters.",
    )
    triggered_by: str | None = Field(default=None, description="Agent or system that triggered the build.")
    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BuildStartedPayload(BaseEventPayload):
    """Payload for ``BUILD_STARTED`` events."""

    build_id: str = Field(description="Build ID.")
    runner_id: str | None = Field(default=None, description="Build runner executing this build.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BuildSucceededPayload(BaseEventPayload):
    """Payload for ``BUILD_SUCCEEDED`` events."""

    build_id: str = Field(description="Build ID.")
    task_id: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    artifacts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of produced artifacts (name, url, size, etc.).",
    )
    duration_seconds: float | None = Field(default=None)
    succeeded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BuildFailedPayload(BaseEventPayload):
    """Payload for ``BUILD_FAILED`` events."""

    build_id: str = Field(description="Build ID.")
    task_id: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    error_message: str = Field(description="Human-readable build failure description.")
    error_details: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None)
    failed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TestStartedPayload(BaseEventPayload):
    """Payload for ``TEST_STARTED`` events."""

    build_id: str = Field(description="Build ID this test run belongs to.")
    test_suite: str = Field(description="Test suite name.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TestFinishedPayload(BaseEventPayload):
    """Payload for ``TEST_FINISHED`` events."""

    build_id: str = Field(description="Build ID.")
    test_suite: str = Field(description="Test suite name.")
    total: int = Field(ge=0, description="Total number of tests.")
    passed: int = Field(ge=0, description="Number of passing tests.")
    failed: int = Field(ge=0, description="Number of failing tests.")
    skipped: int = Field(ge=0, description="Number of skipped tests.")
    duration_seconds: float | None = Field(default=None)
    finished_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TestPassedPayload(BaseEventPayload):
    """Payload for ``TEST_PASSED`` events (individual test case)."""

    build_id: str = Field(description="Build ID.")
    test_name: str = Field(description="Test case name.")
    test_suite: str = Field(description="Test suite name.")
    duration_seconds: float | None = Field(default=None)


class TestFailedPayload(BaseEventPayload):
    """Payload for ``TEST_FAILED`` events (individual test case)."""

    build_id: str = Field(description="Build ID.")
    test_name: str = Field(description="Test case name.")
    test_suite: str = Field(description="Test suite name.")
    error_message: str = Field(description="Test failure message.")
    stack_trace: str | None = Field(default=None)
    duration_seconds: float | None = Field(default=None)


class ArtifactUploadedPayload(BaseEventPayload):
    """Payload for ``ARTIFACT_UPLOADED`` events."""

    build_id: str = Field(description="Build ID.")
    artifact_name: str = Field(description="Artifact name.")
    artifact_url: str = Field(description="URL where the artifact can be downloaded.")
    artifact_size_bytes: int | None = Field(default=None, ge=0)
    content_type: str | None = Field(default=None)
    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# AI event payloads
# ─────────────────────────────────────────────────────────────────────────────


class AITaskStartedPayload(BaseEventPayload):
    """Payload for ``AI_TASK_STARTED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="AI agent ID.")
    model_name: str | None = Field(default=None, description="LLM model being used.")
    reasoning_session_id: str | None = Field(
        default=None,
        description="LangGraph reasoning session ID.",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AISuggestionCreatedPayload(BaseEventPayload):
    """Payload for ``AI_SUGGESTION_CREATED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="AI agent ID.")
    suggestion_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique suggestion ID.",
    )
    suggestion_type: str = Field(description="Type of suggestion (code, config, plan, etc.).")
    content: str = Field(description="The suggestion content.")
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0–1.0).",
    )
    reasoning: str | None = Field(default=None, description="Explanation of the suggestion.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AIPatchProposedPayload(BaseEventPayload):
    """Payload for ``AI_PATCH_PROPOSED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="AI agent ID.")
    patch_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique patch ID.",
    )
    files_changed: list[str] = Field(
        default_factory=list,
        description="List of file paths modified by this patch.",
    )
    diff_summary: str | None = Field(default=None, description="Human-readable diff summary.")
    git_branch: str | None = Field(default=None, description="Branch containing the patch.")
    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AIReviewCompletedPayload(BaseEventPayload):
    """Payload for ``AI_REVIEW_COMPLETED`` events."""

    task_id: str = Field(description="Task ID.")
    workflow_id: str = Field(description="Workflow ID.")
    agent_id: str = Field(description="AI agent ID.")
    review_outcome: str = Field(description="Outcome: approved, rejected, needs_changes.")
    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of review findings (severity, message, file, line).",
    )
    summary: str | None = Field(default=None, description="Review summary.")
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AIReasoningTracePayload(BaseEventPayload):
    """Payload for ``AI_REASONING_TRACE`` events."""

    task_id: str = Field(description="Task ID.")
    agent_id: str = Field(description="AI agent ID.")
    reasoning_session_id: str = Field(description="LangGraph session ID.")
    step_number: int = Field(ge=1, description="Step number within the reasoning session.")
    step_type: str = Field(description="Step type (thought, action, observation, etc.).")
    content: str = Field(description="Step content.")
    token_count: int | None = Field(default=None, ge=0)
    traced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class AIToolCalledPayload(BaseEventPayload):
    """Payload for ``AI_TOOL_CALLED`` events."""

    task_id: str = Field(description="Task ID.")
    agent_id: str = Field(description="AI agent ID.")
    reasoning_session_id: str = Field(description="LangGraph session ID.")
    tool_name: str = Field(description="Name of the tool called.")
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Input parameters passed to the tool.",
    )
    tool_output: Any = Field(
        default=None,
        description="Output returned by the tool.",
    )
    duration_ms: int | None = Field(default=None, ge=0, description="Tool execution time in ms.")
    called_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workspace event payloads
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceCreatedPayload(BaseEventPayload):
    """Payload for ``WORKSPACE_CREATED`` events."""

    workspace_id: str = Field(description="Globally unique workspace ID.")
    agent_id: str = Field(description="Agent this workspace belongs to.")
    task_id: str | None = Field(default=None, description="Task this workspace was created for.")
    workflow_id: str | None = Field(default=None)
    workspace_path: str = Field(description="Filesystem path of the workspace.")
    git_remote_url: str | None = Field(default=None, description="Remote Git repository URL.")
    branch: str | None = Field(default=None, description="Initial branch name.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class WorkspaceSyncedPayload(BaseEventPayload):
    """Payload for ``WORKSPACE_SYNCED`` events."""

    workspace_id: str = Field(description="Workspace ID.")
    agent_id: str = Field(description="Agent that performed the sync.")
    commit_sha: str | None = Field(default=None, description="HEAD commit SHA after sync.")
    branch: str | None = Field(default=None)
    synced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class CheckpointCreatedPayload(BaseEventPayload):
    """Payload for ``CHECKPOINT_CREATED`` events."""

    checkpoint_id: str = Field(description="Checkpoint ID.")
    task_id: str = Field(description="Task this checkpoint belongs to.")
    workspace_id: str = Field(description="Workspace ID.")
    agent_id: str = Field(description="Agent that created the checkpoint.")
    git_commit_sha: str = Field(description="Git commit SHA of the checkpoint.")
    branch: str = Field(description="Branch name.")
    message: str | None = Field(default=None, description="Checkpoint message.")
    checkpoint_type: str = Field(
        default="manual",
        description="Checkpoint type (manual, auto, pre_handoff, etc.).",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class CheckpointRevertedPayload(BaseEventPayload):
    """Payload for ``CHECKPOINT_REVERTED`` events."""

    checkpoint_id: str = Field(description="Checkpoint that was reverted to.")
    task_id: str = Field(description="Task ID.")
    workspace_id: str = Field(description="Workspace ID.")
    agent_id: str = Field(description="Agent that performed the revert.")
    reverted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BranchCreatedPayload(BaseEventPayload):
    """Payload for ``BRANCH_CREATED`` events."""

    workspace_id: str = Field(description="Workspace ID.")
    agent_id: str = Field(description="Agent that created the branch.")
    branch_name: str = Field(description="New branch name.")
    base_branch: str | None = Field(default=None, description="Branch this was created from.")
    base_commit_sha: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class HandoffPreparedPayload(BaseEventPayload):
    """Payload for ``HANDOFF_PREPARED`` events."""

    handoff_id: str = Field(description="Handoff record ID.")
    task_id: str = Field(description="Task being handed off.")
    workspace_id: str = Field(description="Workspace ID.")
    from_agent_id: str = Field(description="Agent handing off.")
    to_agent_id: str = Field(description="Agent receiving.")
    snapshot_commit_sha: str | None = Field(
        default=None,
        description="Git commit SHA of the handoff snapshot.",
    )
    prepared_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class SnapshotCreatedPayload(BaseEventPayload):
    """Payload for ``SNAPSHOT_CREATED`` events."""

    snapshot_id: str = Field(description="Snapshot ID.")
    workspace_id: str = Field(description="Workspace ID.")
    agent_id: str = Field(description="Agent that created the snapshot.")
    git_commit_sha: str = Field(description="Git commit SHA.")
    snapshot_type: str = Field(description="Snapshot type (checkpoint, handoff, backup, etc.).")
    size_bytes: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Policy event payloads
# ─────────────────────────────────────────────────────────────────────────────


class PolicyEvaluatedPayload(BaseEventPayload):
    """Payload for ``POLICY_EVALUATED`` events."""

    policy_id: str = Field(description="Policy rule ID.")
    policy_name: str = Field(description="Policy rule name.")
    subject_type: str = Field(description="Type of subject evaluated (task, workflow, agent).")
    subject_id: str = Field(description="ID of the subject.")
    outcome: str = Field(description="Evaluation outcome: allowed, denied, pending_approval.")
    evaluated_by: str = Field(description="Policy engine instance ID.")
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class PolicyViolationDetectedPayload(BaseEventPayload):
    """Payload for ``POLICY_VIOLATION_DETECTED`` events."""

    policy_id: str = Field(description="Policy rule ID.")
    policy_name: str = Field(description="Policy rule name.")
    subject_type: str = Field(description="Type of subject that violated the policy.")
    subject_id: str = Field(description="ID of the subject.")
    violation_message: str = Field(description="Human-readable violation description.")
    severity: str = Field(default="warning", description="Violation severity.")
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class ApprovalRequestedPayload(BaseEventPayload):
    """Payload for ``APPROVAL_REQUESTED`` events."""

    approval_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique approval request ID.",
    )
    subject_type: str = Field(description="Type of subject requiring approval.")
    subject_id: str = Field(description="ID of the subject.")
    requested_by: str = Field(description="Agent or system requesting approval.")
    approvers: list[str] = Field(
        default_factory=list,
        description="List of agent IDs that can approve.",
    )
    reason: str | None = Field(default=None, description="Reason approval is required.")
    expires_at: datetime | None = Field(default=None, description="Approval request expiry.")
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class ApprovalGrantedPayload(BaseEventPayload):
    """Payload for ``APPROVAL_GRANTED`` events."""

    approval_id: str = Field(description="Approval request ID.")
    subject_type: str = Field(description="Type of subject that was approved.")
    subject_id: str = Field(description="ID of the subject.")
    approved_by: str = Field(description="Agent that granted approval.")
    comment: str | None = Field(default=None)
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class ApprovalDeniedPayload(BaseEventPayload):
    """Payload for ``APPROVAL_DENIED`` events."""

    approval_id: str = Field(description="Approval request ID.")
    subject_type: str = Field(description="Type of subject that was denied.")
    subject_id: str = Field(description="ID of the subject.")
    denied_by: str = Field(description="Agent that denied approval.")
    reason: str | None = Field(default=None, description="Reason for denial.")
    denied_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BranchProtectionTriggeredPayload(BaseEventPayload):
    """Payload for ``BRANCH_PROTECTION_TRIGGERED`` events."""

    workspace_id: str = Field(description="Workspace ID.")
    branch_name: str = Field(description="Protected branch name.")
    attempted_action: str = Field(description="Action that was blocked (push, merge, delete).")
    attempted_by: str = Field(description="Agent that attempted the action.")
    policy_id: str | None = Field(default=None, description="Policy rule that triggered protection.")
    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Observability event payloads
# ─────────────────────────────────────────────────────────────────────────────


class MetricRecordedPayload(BaseEventPayload):
    """Payload for ``METRIC_RECORDED`` events."""

    metric_name: str = Field(description="Metric name (e.g. task.duration_seconds).")
    metric_value: float = Field(description="Metric value.")
    metric_type: str = Field(
        default="gauge",
        description="Metric type: gauge, counter, histogram, summary.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Metric labels/dimensions.",
    )
    unit: str | None = Field(default=None, description="Unit of measurement.")
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TraceSpanStartedPayload(BaseEventPayload):
    """Payload for ``TRACE_SPAN_STARTED`` events."""

    trace_id: str = Field(description="Distributed trace ID.")
    span_id: str = Field(description="Span ID.")
    parent_span_id: str | None = Field(default=None, description="Parent span ID.")
    operation_name: str = Field(description="Name of the operation being traced.")
    service_name: str = Field(description="Service that started this span.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class TraceSpanEndedPayload(BaseEventPayload):
    """Payload for ``TRACE_SPAN_ENDED`` events."""

    trace_id: str = Field(description="Distributed trace ID.")
    span_id: str = Field(description="Span ID.")
    operation_name: str = Field(description="Name of the operation.")
    service_name: str = Field(description="Service that ended this span.")
    status: str = Field(default="ok", description="Span status: ok, error.")
    duration_ms: int | None = Field(default=None, ge=0)
    ended_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class HealthCheckResultPayload(BaseEventPayload):
    """Payload for ``HEALTH_CHECK_RESULT`` events."""

    service_name: str = Field(description="Service that performed the health check.")
    status: str = Field(description="Health status: healthy, degraded, unhealthy.")
    checks: dict[str, Any] = Field(
        default_factory=dict,
        description="Individual check results (name → status/details).",
    )
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class SystemAlertPayload(BaseEventPayload):
    """Payload for ``SYSTEM_ALERT`` events."""

    alert_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique alert ID.",
    )
    alert_name: str = Field(description="Alert rule name.")
    severity: str = Field(description="Alert severity: info, warning, error, critical.")
    message: str = Field(description="Human-readable alert message.")
    service_name: str | None = Field(default=None, description="Service that raised the alert.")
    labels: dict[str, str] = Field(default_factory=dict)
    fired_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema registry
# ─────────────────────────────────────────────────────────────────────────────


#: Maps every ``EventType`` to its payload schema class.
#: Used for validation and documentation.
EVENT_PAYLOAD_SCHEMAS: dict[EventType, Type[BaseEventPayload]] = {
    # Workflow
    EventType.WORKFLOW_CREATED: WorkflowCreatedPayload,
    EventType.WORKFLOW_STARTED: WorkflowStartedPayload,
    EventType.WORKFLOW_PAUSED: WorkflowPausedPayload,
    EventType.WORKFLOW_RESUMED: WorkflowResumedPayload,
    EventType.WORKFLOW_COMPLETED: WorkflowCompletedPayload,
    EventType.WORKFLOW_FAILED: WorkflowFailedPayload,
    EventType.WORKFLOW_CANCELLED: WorkflowCancelledPayload,
    # Task
    EventType.TASK_CREATED: TaskCreatedPayload,
    EventType.TASK_ASSIGNED: TaskAssignedPayload,
    EventType.TASK_ACCEPTED: TaskAcceptedPayload,
    EventType.TASK_STARTED: TaskStartedPayload,
    EventType.TASK_UPDATED: TaskUpdatedPayload,
    EventType.TASK_CHECKPOINTED: TaskCheckpointedPayload,
    EventType.TASK_COMPLETED: TaskCompletedPayload,
    EventType.TASK_FAILED: TaskFailedPayload,
    EventType.TASK_REVERT_REQUESTED: TaskRevertRequestedPayload,
    EventType.TASK_HANDOFF_REQUESTED: TaskHandoffRequestedPayload,
    EventType.TASK_HANDOFF_ACCEPTED: TaskHandoffAcceptedPayload,
    # Agent
    EventType.AGENT_REGISTERED: AgentRegisteredPayload,
    EventType.AGENT_ONLINE: AgentOnlinePayload,
    EventType.AGENT_OFFLINE: AgentOfflinePayload,
    EventType.AGENT_BUSY: AgentBusyPayload,
    EventType.AGENT_IDLE: AgentIdlePayload,
    EventType.AGENT_CAPABILITY_UPDATED: AgentCapabilityUpdatedPayload,
    # Build
    EventType.BUILD_TRIGGERED: BuildTriggeredPayload,
    EventType.BUILD_STARTED: BuildStartedPayload,
    EventType.BUILD_SUCCEEDED: BuildSucceededPayload,
    EventType.BUILD_FAILED: BuildFailedPayload,
    EventType.TEST_STARTED: TestStartedPayload,
    EventType.TEST_FINISHED: TestFinishedPayload,
    EventType.TEST_PASSED: TestPassedPayload,
    EventType.TEST_FAILED: TestFailedPayload,
    EventType.ARTIFACT_UPLOADED: ArtifactUploadedPayload,
    # AI
    EventType.AI_TASK_STARTED: AITaskStartedPayload,
    EventType.AI_SUGGESTION_CREATED: AISuggestionCreatedPayload,
    EventType.AI_PATCH_PROPOSED: AIPatchProposedPayload,
    EventType.AI_REVIEW_COMPLETED: AIReviewCompletedPayload,
    EventType.AI_REASONING_TRACE: AIReasoningTracePayload,
    EventType.AI_TOOL_CALLED: AIToolCalledPayload,
    # Workspace
    EventType.WORKSPACE_CREATED: WorkspaceCreatedPayload,
    EventType.WORKSPACE_SYNCED: WorkspaceSyncedPayload,
    EventType.CHECKPOINT_CREATED: CheckpointCreatedPayload,
    EventType.CHECKPOINT_REVERTED: CheckpointRevertedPayload,
    EventType.BRANCH_CREATED: BranchCreatedPayload,
    EventType.HANDOFF_PREPARED: HandoffPreparedPayload,
    EventType.SNAPSHOT_CREATED: SnapshotCreatedPayload,
    # Policy
    EventType.POLICY_EVALUATED: PolicyEvaluatedPayload,
    EventType.POLICY_VIOLATION_DETECTED: PolicyViolationDetectedPayload,
    EventType.APPROVAL_REQUESTED: ApprovalRequestedPayload,
    EventType.APPROVAL_GRANTED: ApprovalGrantedPayload,
    EventType.APPROVAL_DENIED: ApprovalDeniedPayload,
    EventType.BRANCH_PROTECTION_TRIGGERED: BranchProtectionTriggeredPayload,
    # Observability
    EventType.METRIC_RECORDED: MetricRecordedPayload,
    EventType.TRACE_SPAN_STARTED: TraceSpanStartedPayload,
    EventType.TRACE_SPAN_ENDED: TraceSpanEndedPayload,
    EventType.HEALTH_CHECK_RESULT: HealthCheckResultPayload,
    EventType.SYSTEM_ALERT: SystemAlertPayload,
}


# ─────────────────────────────────────────────────────────────────────────────
# Factory helpers
# ─────────────────────────────────────────────────────────────────────────────


def build_event(
    event_type: EventType,
    payload: BaseEventPayload,
    source: EventSource,
    workflow_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> EventRecord:
    """
    Construct a fully-validated ``EventRecord`` from a typed payload object.

    Automatically selects the correct ``EventTopic`` based on the event type
    using the ``TOPIC_FOR_EVENT`` routing table.

    Args:
        event_type:     The event type discriminator.
        payload:        A typed payload schema instance.
        source:         The component producing this event.
        workflow_id:    Optional workflow scope.
        task_id:        Optional task scope.
        agent_id:       Optional agent scope.
        correlation_id: Optional correlation ID for request/response linking.
        causation_id:   Optional ID of the event that caused this one.
        metadata:       Optional key/value metadata bag.

    Returns:
        A fully-validated, immutable ``EventRecord`` ready for produce().

    Raises:
        KeyError: If the event_type has no registered topic mapping.
        ValidationError: If the EventRecord fails Pydantic validation.
    """
    kafka_topic = topic_for_event(event_type)
    # Convert KafkaTopic → EventTopic (same string values)
    event_topic = EventTopic(str(kafka_topic))

    return EventRecord(
        event_type=event_type,
        topic=event_topic,
        source=source,
        workflow_id=workflow_id,
        task_id=task_id,
        agent_id=agent_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload.to_dict(),
        metadata=metadata or {},
        schema_version=payload.schema_version,
    )


def validate_payload(
    event_type: EventType,
    payload_dict: dict[str, Any],
) -> BaseEventPayload:
    """
    Validate a raw payload dict against the registered schema for an event type.

    Args:
        event_type:   The event type discriminator.
        payload_dict: Raw payload dict (e.g. from ``EventRecord.payload``).

    Returns:
        A validated payload schema instance.

    Raises:
        KeyError: If no schema is registered for the event type.
        ValidationError: If the payload fails schema validation.
    """
    schema_cls = EVENT_PAYLOAD_SCHEMAS.get(event_type)
    if schema_cls is None:
        raise KeyError(
            f"No payload schema registered for EventType {event_type!r}. "
            "Add it to EVENT_PAYLOAD_SCHEMAS in shared/kafka/schemas.py."
        )
    return schema_cls.model_validate(payload_dict)
