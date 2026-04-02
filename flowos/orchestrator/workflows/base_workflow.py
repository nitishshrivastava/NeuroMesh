"""
orchestrator/workflows/base_workflow.py — Base FlowOS Temporal Workflow

Provides the abstract base class and shared infrastructure for all FlowOS
Temporal workflows.  Concrete workflow implementations (FeatureDeliveryWorkflow,
BuildAndReviewWorkflow, etc.) inherit from ``BaseFlowOSWorkflow``.

Design principles:
- All workflows are deterministic: no I/O, no random, no datetime.now() in
  workflow code (use activity results instead).
- Workflows communicate with the outside world exclusively through activities.
- Workflow state is persisted by Temporal; no additional state store needed.
- Kafka events are published via activities (not directly from workflow code).
- Signals allow external actors to pause, resume, or cancel workflows.
- Queries allow external actors to inspect workflow state without modifying it.

Temporal workflow constraints (enforced here):
- No threading or async I/O in workflow code.
- No non-deterministic operations (random, time.time(), etc.).
- All side effects go through activities.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes for workflow input/output
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WorkflowInput:
    """
    Standard input parameters for all FlowOS workflows.

    Attributes:
        workflow_id:    Globally unique workflow instance identifier.
        name:           Human-readable workflow name.
        definition:     Serialised WorkflowDefinition (as a dict).
        inputs:         Runtime input parameters provided at trigger time.
        owner_agent_id: Agent that initiated this workflow.
        project:        Optional project/namespace.
        trigger:        How this workflow was initiated.
        tags:           Arbitrary tags for filtering.
        correlation_id: Optional correlation ID for request tracing.
    """

    workflow_id: str
    name: str
    definition: dict[str, Any]
    inputs: dict[str, Any] = field(default_factory=dict)
    owner_agent_id: str | None = None
    project: str | None = None
    trigger: str = "manual"
    tags: list[str] = field(default_factory=list)
    correlation_id: str | None = None


@dataclass
class WorkflowResult:
    """
    Standard result returned by all FlowOS workflows.

    Attributes:
        workflow_id:     Workflow instance identifier.
        status:          Final workflow status (completed, failed, cancelled).
        outputs:         Collected output parameters from completed steps.
        error_message:   Error description if status=failed.
        error_details:   Structured error details.
        step_results:    Per-step execution results.
        duration_seconds: Total execution duration in seconds.
    """

    workflow_id: str
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    step_results: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Retry policies
# ─────────────────────────────────────────────────────────────────────────────

#: Default retry policy for short-lived activities (DB writes, Kafka publishes).
DEFAULT_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Retry policy for long-running activities (task waits, handoff waits).
LONG_RUNNING_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)

#: Retry policy for Kafka publishing activities.
KAFKA_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=10,
)

#: No-retry policy for activities that must not be retried (e.g. idempotency-sensitive).
NO_RETRY_POLICY = RetryPolicy(
    maximum_attempts=1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Base workflow class
# ─────────────────────────────────────────────────────────────────────────────


class BaseFlowOSWorkflow(ABC):
    """
    Abstract base class for all FlowOS Temporal workflows.

    Provides:
    - Signal handlers for pause, resume, and cancel
    - Query handlers for status inspection
    - Shared activity execution helpers with retry policies
    - Workflow state tracking (paused, cancelled, step results)

    Subclasses must implement ``run()`` with the ``@workflow.run`` decorator.

    Usage::

        @workflow.defn(name="my-workflow")
        class MyWorkflow(BaseFlowOSWorkflow):

            @workflow.run
            async def run(self, input: WorkflowInput) -> WorkflowResult:
                await self.publish_workflow_started(input)
                # ... execute steps ...
                return await self.complete_workflow(input, outputs={})
    """

    def __init__(self) -> None:
        self._is_paused: bool = False
        self._is_cancelled: bool = False
        self._pause_reason: str | None = None
        self._cancel_reason: str | None = None
        self._current_step: str | None = None
        self._step_results: dict[str, Any] = {}
        self._completed_steps: set[str] = set()
        self._failed_steps: set[str] = set()

    # ─────────────────────────────────────────────────────────────────────────
    # Signals
    # ─────────────────────────────────────────────────────────────────────────

    @workflow.signal(name="pause")
    async def pause_workflow(self, reason: str | None = None) -> None:
        """
        Signal: Pause the workflow at the next safe checkpoint.

        The workflow will stop executing new steps but will not interrupt
        any currently running activities.
        """
        self._is_paused = True
        self._pause_reason = reason
        workflow.logger.info(
            "Workflow pause signal received | reason=%s",
            reason or "no reason provided",
        )

    @workflow.signal(name="resume")
    async def resume_workflow(self) -> None:
        """
        Signal: Resume a paused workflow.

        The workflow will continue executing from where it left off.
        """
        self._is_paused = False
        self._pause_reason = None
        workflow.logger.info("Workflow resume signal received")

    @workflow.signal(name="cancel")
    async def cancel_workflow(self, reason: str | None = None) -> None:
        """
        Signal: Cancel the workflow.

        The workflow will stop executing and transition to CANCELLED status.
        """
        self._is_cancelled = True
        self._cancel_reason = reason
        workflow.logger.info(
            "Workflow cancel signal received | reason=%s",
            reason or "no reason provided",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Queries
    # ─────────────────────────────────────────────────────────────────────────

    @workflow.query(name="get_status")
    def get_status(self) -> dict[str, Any]:
        """
        Query: Return the current workflow execution status.

        Returns a dict with is_paused, is_cancelled, current_step,
        completed_steps, and failed_steps.
        """
        return {
            "is_paused": self._is_paused,
            "is_cancelled": self._is_cancelled,
            "pause_reason": self._pause_reason,
            "cancel_reason": self._cancel_reason,
            "current_step": self._current_step,
            "completed_steps": list(self._completed_steps),
            "failed_steps": list(self._failed_steps),
            "step_count": len(self._completed_steps) + len(self._failed_steps),
        }

    @workflow.query(name="get_step_result")
    def get_step_result(self, step_id: str) -> dict[str, Any] | None:
        """
        Query: Return the result of a specific step.

        Args:
            step_id: Step ID to query.

        Returns:
            Step result dict, or None if the step has not completed.
        """
        return self._step_results.get(step_id)

    # ─────────────────────────────────────────────────────────────────────────
    # Abstract interface
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def run(self, input: WorkflowInput) -> WorkflowResult:
        """
        Execute the workflow.

        Subclasses must implement this method with the ``@workflow.run``
        decorator.

        Args:
            input: Workflow input parameters.

        Returns:
            WorkflowResult with final status and outputs.
        """
        ...

    # ─────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def wait_if_paused(self) -> None:
        """
        Wait until the workflow is unpaused or cancelled.

        Call this at the start of each step to respect pause signals.
        """
        if self._is_paused:
            workflow.logger.info("Workflow is paused, waiting for resume signal...")
            await workflow.wait_condition(
                lambda: not self._is_paused or self._is_cancelled
            )

    def is_cancelled(self) -> bool:
        """Return True if the workflow has been cancelled."""
        return self._is_cancelled

    def mark_step_started(self, step_id: str) -> None:
        """Record that a step has started executing."""
        self._current_step = step_id

    def mark_step_completed(self, step_id: str, result: dict[str, Any]) -> None:
        """Record that a step has completed successfully."""
        self._completed_steps.add(step_id)
        self._step_results[step_id] = result
        if self._current_step == step_id:
            self._current_step = None

    def mark_step_failed(self, step_id: str, error: str) -> None:
        """Record that a step has failed."""
        self._failed_steps.add(step_id)
        self._step_results[step_id] = {"error": error, "status": "failed"}
        if self._current_step == step_id:
            self._current_step = None

    async def publish_workflow_started(self, input: WorkflowInput) -> None:
        """Publish a WORKFLOW_STARTED event via activity."""
        from orchestrator.activities.kafka_activities import publish_workflow_event
        await workflow.execute_activity(
            publish_workflow_event,
            args=[
                "WORKFLOW_STARTED",
                input.workflow_id,
                input.name,
                {
                    "temporal_workflow_id": workflow.info().workflow_id,
                    "temporal_run_id": workflow.info().run_id,
                    "correlation_id": input.correlation_id,
                },
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=KAFKA_RETRY_POLICY,
        )

    async def publish_workflow_completed(
        self,
        input: WorkflowInput,
        outputs: dict[str, Any],
        duration_seconds: float | None = None,
    ) -> None:
        """Publish a WORKFLOW_COMPLETED event via activity."""
        from orchestrator.activities.kafka_activities import publish_workflow_event
        await workflow.execute_activity(
            publish_workflow_event,
            args=[
                "WORKFLOW_COMPLETED",
                input.workflow_id,
                input.name,
                {
                    "outputs": outputs,
                    "duration_seconds": duration_seconds,
                    "correlation_id": input.correlation_id,
                },
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=KAFKA_RETRY_POLICY,
        )

    async def publish_workflow_failed(
        self,
        input: WorkflowInput,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Publish a WORKFLOW_FAILED event via activity."""
        from orchestrator.activities.kafka_activities import publish_workflow_event
        await workflow.execute_activity(
            publish_workflow_event,
            args=[
                "WORKFLOW_FAILED",
                input.workflow_id,
                input.name,
                {
                    "error_message": error_message,
                    "error_details": error_details or {},
                    "correlation_id": input.correlation_id,
                },
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=KAFKA_RETRY_POLICY,
        )

    async def publish_workflow_cancelled(
        self,
        input: WorkflowInput,
        reason: str | None = None,
    ) -> None:
        """Publish a WORKFLOW_CANCELLED event via activity."""
        from orchestrator.activities.kafka_activities import publish_workflow_event
        await workflow.execute_activity(
            publish_workflow_event,
            args=[
                "WORKFLOW_CANCELLED",
                input.workflow_id,
                input.name,
                {
                    "reason": reason or self._cancel_reason,
                    "correlation_id": input.correlation_id,
                },
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=KAFKA_RETRY_POLICY,
        )

    async def complete_workflow(
        self,
        input: WorkflowInput,
        outputs: dict[str, Any],
        start_time_ns: int | None = None,
    ) -> WorkflowResult:
        """
        Finalise a successful workflow execution.

        Publishes WORKFLOW_COMPLETED event and returns a WorkflowResult.

        Args:
            input:          Original workflow input.
            outputs:        Collected output parameters.
            start_time_ns:  Workflow start time in nanoseconds (from workflow.now()).

        Returns:
            WorkflowResult with status=completed.
        """
        duration: float | None = None
        if start_time_ns is not None:
            duration = (workflow.now().timestamp() * 1e9 - start_time_ns) / 1e9

        await self.publish_workflow_completed(input, outputs, duration)

        return WorkflowResult(
            workflow_id=input.workflow_id,
            status="completed",
            outputs=outputs,
            step_results=self._step_results,
            duration_seconds=duration,
        )

    async def fail_workflow(
        self,
        input: WorkflowInput,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """
        Finalise a failed workflow execution.

        Publishes WORKFLOW_FAILED event and returns a WorkflowResult.

        Args:
            input:         Original workflow input.
            error_message: Human-readable error description.
            error_details: Structured error details.

        Returns:
            WorkflowResult with status=failed.
        """
        await self.publish_workflow_failed(input, error_message, error_details)

        return WorkflowResult(
            workflow_id=input.workflow_id,
            status="failed",
            error_message=error_message,
            error_details=error_details or {},
            step_results=self._step_results,
        )
