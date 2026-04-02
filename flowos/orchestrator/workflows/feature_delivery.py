"""
orchestrator/workflows/feature_delivery.py — Feature Delivery Workflow

Implements the end-to-end feature delivery workflow for FlowOS.
This workflow orchestrates the complete lifecycle of a feature from
planning through implementation, build, and review.

Workflow steps:
1. PLAN    — AI agent analyses requirements and creates an implementation plan
2. IMPLEMENT — Human agent implements the feature based on the plan
3. BUILD   — Build runner compiles and tests the implementation
4. REVIEW  — Human agent reviews the code and build results
5. APPROVE — Optional approval gate before merge

The workflow handles:
- Parallel step execution where dependencies allow
- Automatic retries on transient failures
- Handoffs between human and AI agents
- Checkpoint creation at each step boundary
- Pause/resume for human review gates
- Cancellation at any point

DSL example::

    name: feature-delivery
    version: "1.0.0"
    description: "End-to-end feature delivery workflow"
    steps:
      - id: plan
        name: "Plan Feature"
        agent_type: ai
        timeout_secs: 300
        retry_count: 2
      - id: implement
        name: "Implement Feature"
        agent_type: human
        depends_on: [plan]
        timeout_secs: 86400
      - id: build
        name: "Build & Test"
        agent_type: build
        depends_on: [implement]
        timeout_secs: 1800
        retry_count: 3
      - id: review
        name: "Code Review"
        agent_type: human
        depends_on: [build]
        timeout_secs: 86400
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from orchestrator.workflows.base_workflow import (
    BaseFlowOSWorkflow,
    WorkflowInput,
    WorkflowResult,
    DEFAULT_ACTIVITY_RETRY_POLICY,
    KAFKA_RETRY_POLICY,
    LONG_RUNNING_RETRY_POLICY,
)

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.task_activities import (
        create_task,
        assign_task,
        wait_for_task_completion,
        update_task_status,
        cancel_task,
    )
    from orchestrator.activities.workspace_activities import (
        provision_workspace,
        archive_workspace,
    )
    from orchestrator.activities.kafka_activities import (
        publish_workflow_event,
        publish_task_event,
    )

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Input type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FeatureDeliveryInput(WorkflowInput):
    """
    Input parameters specific to the FeatureDeliveryWorkflow.

    Extends WorkflowInput with feature-delivery-specific fields.

    Attributes:
        feature_branch:    Git branch for the feature.
        ticket_id:         Optional issue/ticket ID.
        ai_agent_id:       AI agent to use for planning.
        human_agent_id:    Human agent to use for implementation and review.
        build_agent_id:    Build runner agent ID.
        require_approval:  Whether an explicit approval gate is required.
        auto_assign:       Whether to auto-assign tasks to available agents.
    """

    feature_branch: str = "main"
    ticket_id: str | None = None
    ai_agent_id: str | None = None
    human_agent_id: str | None = None
    build_agent_id: str | None = None
    require_approval: bool = False
    auto_assign: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Workflow definition
# ─────────────────────────────────────────────────────────────────────────────


@workflow.defn(name="feature-delivery")
class FeatureDeliveryWorkflow(BaseFlowOSWorkflow):
    """
    End-to-end feature delivery workflow.

    Orchestrates the complete lifecycle of a software feature from planning
    through implementation, build, and review.

    Supports:
    - AI-assisted planning
    - Human implementation with workspace management
    - Automated build and test
    - Code review gate
    - Optional approval gate
    - Pause/resume at any step
    - Cancellation with cleanup
    """

    @workflow.run
    async def run(self, input: FeatureDeliveryInput) -> WorkflowResult:
        """
        Execute the feature delivery workflow.

        Args:
            input: Feature delivery workflow input parameters.

        Returns:
            WorkflowResult with final status and outputs.
        """
        workflow.logger.info(
            "Starting FeatureDeliveryWorkflow | workflow_id=%s name=%s",
            input.workflow_id,
            input.name,
        )

        start_ns = int(workflow.now().timestamp() * 1e9)
        outputs: dict[str, Any] = {}

        # Publish WORKFLOW_STARTED event
        await self.publish_workflow_started(input)

        try:
            # ─────────────────────────────────────────────────────────────────
            # Step 1: Provision workspace
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            workspace_result = await workflow.execute_activity(
                provision_workspace,
                args=[
                    input.owner_agent_id or "system",
                    "",  # task_id will be set after task creation
                    input.workflow_id,
                    "local",
                    None,  # root_path (auto-derived)
                    None,  # git_remote_url
                    input.feature_branch,
                ],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )
            workspace_id = workspace_result["workspace_id"]
            outputs["workspace_id"] = workspace_id

            # ─────────────────────────────────────────────────────────────────
            # Step 2: Plan (AI agent)
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("plan")
            plan_result = await self._execute_step(
                input=input,
                step_id="plan",
                step_name="Plan Feature",
                task_type="ai",
                agent_id=input.ai_agent_id,
                workspace_id=workspace_id,
                timeout_secs=300,
                retry_count=2,
                step_inputs={"ticket_id": input.ticket_id, "branch": input.feature_branch},
            )

            if plan_result.get("status") == "failed":
                return await self.fail_workflow(
                    input,
                    f"Planning step failed: {plan_result.get('error_message', 'Unknown error')}",
                )

            outputs["plan"] = plan_result.get("outputs", {})
            self.mark_step_completed("plan", plan_result)

            # ─────────────────────────────────────────────────────────────────
            # Step 3: Implement (human agent)
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("implement")
            implement_result = await self._execute_step(
                input=input,
                step_id="implement",
                step_name="Implement Feature",
                task_type="human",
                agent_id=input.human_agent_id,
                workspace_id=workspace_id,
                timeout_secs=86400,  # 24 hours
                retry_count=0,
                step_inputs={
                    "plan": outputs.get("plan", {}),
                    "branch": input.feature_branch,
                    "ticket_id": input.ticket_id,
                },
            )

            if implement_result.get("status") == "failed":
                return await self.fail_workflow(
                    input,
                    f"Implementation step failed: {implement_result.get('error_message', 'Unknown error')}",
                )

            outputs["implementation"] = implement_result.get("outputs", {})
            self.mark_step_completed("implement", implement_result)

            # ─────────────────────────────────────────────────────────────────
            # Step 4: Build & Test (build runner)
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("build")
            build_result = await self._execute_step(
                input=input,
                step_id="build",
                step_name="Build & Test",
                task_type="build",
                agent_id=input.build_agent_id,
                workspace_id=workspace_id,
                timeout_secs=1800,  # 30 minutes
                retry_count=3,
                step_inputs={"branch": input.feature_branch},
            )

            if build_result.get("status") == "failed":
                return await self.fail_workflow(
                    input,
                    f"Build step failed: {build_result.get('error_message', 'Unknown error')}",
                )

            outputs["build"] = build_result.get("outputs", {})
            self.mark_step_completed("build", build_result)

            # ─────────────────────────────────────────────────────────────────
            # Step 5: Code Review (human agent)
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("review")
            review_result = await self._execute_step(
                input=input,
                step_id="review",
                step_name="Code Review",
                task_type="review",
                agent_id=input.human_agent_id,
                workspace_id=workspace_id,
                timeout_secs=86400,  # 24 hours
                retry_count=0,
                step_inputs={
                    "build_result": outputs.get("build", {}),
                    "branch": input.feature_branch,
                },
            )

            if review_result.get("status") == "failed":
                return await self.fail_workflow(
                    input,
                    f"Review step failed: {review_result.get('error_message', 'Unknown error')}",
                )

            outputs["review"] = review_result.get("outputs", {})
            self.mark_step_completed("review", review_result)

            # ─────────────────────────────────────────────────────────────────
            # Step 6: Optional approval gate
            # ─────────────────────────────────────────────────────────────────
            if input.require_approval:
                await self.wait_if_paused()
                if self.is_cancelled():
                    return await self._handle_cancellation(input)

                self.mark_step_started("approve")
                approval_result = await self._execute_step(
                    input=input,
                    step_id="approve",
                    step_name="Approval Gate",
                    task_type="approval",
                    agent_id=input.human_agent_id,
                    workspace_id=workspace_id,
                    timeout_secs=172800,  # 48 hours
                    retry_count=0,
                    step_inputs={"review_result": outputs.get("review", {})},
                )

                if approval_result.get("status") == "failed":
                    return await self.fail_workflow(
                        input,
                        f"Approval denied: {approval_result.get('error_message', 'Approval rejected')}",
                    )

                outputs["approval"] = approval_result.get("outputs", {})
                self.mark_step_completed("approve", approval_result)

            # ─────────────────────────────────────────────────────────────────
            # Archive workspace
            # ─────────────────────────────────────────────────────────────────
            await workflow.execute_activity(
                archive_workspace,
                args=[workspace_id, "Feature delivery completed"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )

            return await self.complete_workflow(input, outputs, start_ns)

        except Exception as exc:
            workflow.logger.error(
                "FeatureDeliveryWorkflow failed | workflow_id=%s error=%s",
                input.workflow_id,
                exc,
            )
            return await self.fail_workflow(
                input,
                str(exc),
                {"exception_type": type(exc).__name__},
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _execute_step(
        self,
        input: FeatureDeliveryInput,
        step_id: str,
        step_name: str,
        task_type: str,
        agent_id: str | None,
        workspace_id: str,
        timeout_secs: int,
        retry_count: int,
        step_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a single workflow step by creating a task, assigning it,
        and waiting for completion.

        Args:
            input:        Workflow input.
            step_id:      Step identifier.
            step_name:    Human-readable step name.
            task_type:    Type of task (ai, human, build, etc.).
            agent_id:     Agent to assign the task to (None = auto-assign).
            workspace_id: Workspace for this task.
            timeout_secs: Maximum execution time.
            retry_count:  Number of retries on failure.
            step_inputs:  Input parameters for this step.

        Returns:
            Dict with task status and outputs.
        """
        # Create the task
        task_result = await workflow.execute_activity(
            create_task,
            args=[
                input.workflow_id,
                step_id,
                step_name,
                task_type,
                "normal",  # priority
                None,  # description
                [{"name": k, "value": v} for k, v in (step_inputs or {}).items()],
                [],  # depends_on (handled at workflow level)
                timeout_secs,
                retry_count,
                input.tags,
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
        )
        task_id = task_result["task_id"]

        # Assign the task to an agent (if specified)
        if agent_id:
            await workflow.execute_activity(
                assign_task,
                args=[task_id, input.workflow_id, agent_id, step_name, task_type],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )

        # Wait for task completion
        completion_result = await workflow.execute_activity(
            wait_for_task_completion,
            args=[task_id, 10, timeout_secs + 300],  # poll every 10s, timeout + buffer
            start_to_close_timeout=timedelta(seconds=timeout_secs + 600),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=LONG_RUNNING_RETRY_POLICY,
        )

        return completion_result

    async def _handle_cancellation(self, input: FeatureDeliveryInput) -> WorkflowResult:
        """Handle workflow cancellation with cleanup."""
        await self.publish_workflow_cancelled(input, self._cancel_reason)
        return WorkflowResult(
            workflow_id=input.workflow_id,
            status="cancelled",
            error_message=self._cancel_reason or "Workflow cancelled",
            step_results=self._step_results,
        )
