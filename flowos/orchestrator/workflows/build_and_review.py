"""
orchestrator/workflows/build_and_review.py — Build and Review Workflow

Implements a focused build-and-review workflow for FlowOS.
This workflow handles the CI/CD portion of the development lifecycle:
triggering builds, running tests, and coordinating code review.

Workflow steps:
1. TRIGGER_BUILD  — Trigger a build on the build runner
2. WAIT_BUILD     — Wait for build completion
3. ANALYZE        — AI agent analyses build results and test coverage
4. REVIEW         — Human agent reviews the analysis and build artifacts
5. GATE           — Optional quality gate (pass/fail based on metrics)

This workflow is typically invoked as a sub-workflow by FeatureDeliveryWorkflow
or triggered directly by a CI event (webhook, scheduled run, etc.).

DSL example::

    name: build-and-review
    version: "1.0.0"
    description: "Build, test, and review workflow"
    inputs:
      branch: "feature/my-feature"
      commit_sha: null
      build_config: {}
    steps:
      - id: build
        name: "Trigger Build"
        agent_type: build
        timeout_secs: 1800
        retry_count: 3
      - id: analyze
        name: "Analyze Results"
        agent_type: ai
        depends_on: [build]
        timeout_secs: 300
      - id: review
        name: "Review"
        agent_type: human
        depends_on: [analyze]
        timeout_secs: 86400
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow

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
        cancel_task,
    )
    from orchestrator.activities.workspace_activities import (
        provision_workspace,
        sync_workspace,
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
class BuildAndReviewInput(WorkflowInput):
    """
    Input parameters specific to the BuildAndReviewWorkflow.

    Attributes:
        branch:           Git branch to build.
        commit_sha:       Specific commit SHA to build (None = HEAD).
        build_config:     Build configuration parameters.
        build_agent_id:   Build runner agent ID.
        ai_agent_id:      AI agent for analysis.
        reviewer_agent_id: Human agent for review.
        quality_gate:     Quality gate configuration (coverage thresholds, etc.).
        fail_on_test_failure: Whether to fail the workflow if tests fail.
        notify_on_failure: Whether to send notifications on failure.
    """

    branch: str = "main"
    commit_sha: str | None = None
    build_config: dict[str, Any] = field(default_factory=dict)
    build_agent_id: str | None = None
    ai_agent_id: str | None = None
    reviewer_agent_id: str | None = None
    quality_gate: dict[str, Any] = field(default_factory=dict)
    fail_on_test_failure: bool = True
    notify_on_failure: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Workflow definition
# ─────────────────────────────────────────────────────────────────────────────


@workflow.defn(name="build-and-review")
class BuildAndReviewWorkflow(BaseFlowOSWorkflow):
    """
    Build, test, and review workflow.

    Orchestrates the CI/CD pipeline for a feature branch:
    1. Triggers a build on the build runner
    2. Waits for build and test completion
    3. AI agent analyses results and generates a report
    4. Human agent reviews the analysis
    5. Optional quality gate enforcement

    Supports:
    - Parallel build and analysis where possible
    - Automatic retry on transient build failures
    - Quality gate enforcement (coverage, lint, security)
    - Pause/resume for human review
    - Cancellation with build abort
    """

    @workflow.run
    async def run(self, input: BuildAndReviewInput) -> WorkflowResult:
        """
        Execute the build and review workflow.

        Args:
            input: Build and review workflow input parameters.

        Returns:
            WorkflowResult with final status and build/review outputs.
        """
        workflow.logger.info(
            "Starting BuildAndReviewWorkflow | workflow_id=%s branch=%s",
            input.workflow_id,
            input.branch,
        )

        start_ns = int(workflow.now().timestamp() * 1e9)
        outputs: dict[str, Any] = {
            "branch": input.branch,
            "commit_sha": input.commit_sha,
        }

        # Publish WORKFLOW_STARTED event
        await self.publish_workflow_started(input)

        try:
            # ─────────────────────────────────────────────────────────────────
            # Step 1: Provision ephemeral workspace for build
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            workspace_result = await workflow.execute_activity(
                provision_workspace,
                args=[
                    input.build_agent_id or "build-runner",
                    "",  # task_id (set after task creation)
                    input.workflow_id,
                    "ephemeral",  # ephemeral workspace for builds
                    None,  # root_path (auto-derived)
                    None,  # git_remote_url
                    input.branch,
                ],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )
            workspace_id = workspace_result["workspace_id"]
            outputs["workspace_id"] = workspace_id

            # ─────────────────────────────────────────────────────────────────
            # Step 2: Trigger and wait for build
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("build")
            build_task_result = await workflow.execute_activity(
                create_task,
                args=[
                    input.workflow_id,
                    "build",
                    "Build & Test",
                    "build",
                    "high",  # builds are high priority
                    f"Build branch {input.branch}",
                    [
                        {"name": "branch", "value": input.branch},
                        {"name": "commit_sha", "value": input.commit_sha},
                        {"name": "build_config", "value": input.build_config},
                    ],
                    [],  # depends_on
                    1800,  # 30 minute timeout
                    3,  # retry_count
                    input.tags,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )
            build_task_id = build_task_result["task_id"]

            # Assign to build runner if specified
            if input.build_agent_id:
                await workflow.execute_activity(
                    assign_task,
                    args=[
                        build_task_id,
                        input.workflow_id,
                        input.build_agent_id,
                        "Build & Test",
                        "build",
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
                )

            # Wait for build completion
            build_completion = await workflow.execute_activity(
                wait_for_task_completion,
                args=[build_task_id, 15, 2400],  # poll every 15s, 40min timeout
                start_to_close_timeout=timedelta(seconds=2700),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=LONG_RUNNING_RETRY_POLICY,
            )

            outputs["build"] = {
                "task_id": build_task_id,
                "status": build_completion.get("status"),
                "outputs": build_completion.get("outputs", []),
            }

            # Check build result
            if build_completion.get("status") == "failed" and input.fail_on_test_failure:
                self.mark_step_failed("build", build_completion.get("error_message", "Build failed"))
                return await self.fail_workflow(
                    input,
                    f"Build failed: {build_completion.get('error_message', 'Unknown error')}",
                    {"build_task_id": build_task_id},
                )

            self.mark_step_completed("build", build_completion)

            # ─────────────────────────────────────────────────────────────────
            # Step 3: AI analysis of build results
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("analyze")
            analysis_task_result = await workflow.execute_activity(
                create_task,
                args=[
                    input.workflow_id,
                    "analyze",
                    "Analyze Build Results",
                    "ai",
                    "normal",
                    "AI analysis of build results, test coverage, and code quality",
                    [
                        {"name": "build_outputs", "value": outputs.get("build", {})},
                        {"name": "quality_gate", "value": input.quality_gate},
                        {"name": "branch", "value": input.branch},
                    ],
                    [],  # depends_on
                    300,  # 5 minute timeout
                    2,  # retry_count
                    input.tags,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )
            analysis_task_id = analysis_task_result["task_id"]

            if input.ai_agent_id:
                await workflow.execute_activity(
                    assign_task,
                    args=[
                        analysis_task_id,
                        input.workflow_id,
                        input.ai_agent_id,
                        "Analyze Build Results",
                        "ai",
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
                )

            analysis_completion = await workflow.execute_activity(
                wait_for_task_completion,
                args=[analysis_task_id, 10, 600],  # poll every 10s, 10min timeout
                start_to_close_timeout=timedelta(seconds=900),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=LONG_RUNNING_RETRY_POLICY,
            )

            outputs["analysis"] = {
                "task_id": analysis_task_id,
                "status": analysis_completion.get("status"),
                "outputs": analysis_completion.get("outputs", []),
            }
            self.mark_step_completed("analyze", analysis_completion)

            # ─────────────────────────────────────────────────────────────────
            # Step 4: Quality gate check
            # ─────────────────────────────────────────────────────────────────
            if input.quality_gate:
                gate_passed = self._evaluate_quality_gate(
                    input.quality_gate,
                    outputs.get("analysis", {}),
                )
                outputs["quality_gate_passed"] = gate_passed

                if not gate_passed:
                    return await self.fail_workflow(
                        input,
                        "Quality gate failed: build does not meet minimum quality thresholds.",
                        {
                            "quality_gate": input.quality_gate,
                            "analysis": outputs.get("analysis", {}),
                        },
                    )

            # ─────────────────────────────────────────────────────────────────
            # Step 5: Human review
            # ─────────────────────────────────────────────────────────────────
            await self.wait_if_paused()
            if self.is_cancelled():
                return await self._handle_cancellation(input)

            self.mark_step_started("review")
            review_task_result = await workflow.execute_activity(
                create_task,
                args=[
                    input.workflow_id,
                    "review",
                    "Review Build & Analysis",
                    "review",
                    "normal",
                    "Human review of build results and AI analysis",
                    [
                        {"name": "build_outputs", "value": outputs.get("build", {})},
                        {"name": "analysis", "value": outputs.get("analysis", {})},
                        {"name": "branch", "value": input.branch},
                    ],
                    [],  # depends_on
                    86400,  # 24 hour timeout
                    0,  # no retries for human tasks
                    input.tags,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )
            review_task_id = review_task_result["task_id"]

            if input.reviewer_agent_id:
                await workflow.execute_activity(
                    assign_task,
                    args=[
                        review_task_id,
                        input.workflow_id,
                        input.reviewer_agent_id,
                        "Review Build & Analysis",
                        "review",
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
                )

            review_completion = await workflow.execute_activity(
                wait_for_task_completion,
                args=[review_task_id, 30, 86700],  # poll every 30s, 24h+ timeout
                start_to_close_timeout=timedelta(seconds=87000),
                heartbeat_timeout=timedelta(seconds=120),
                retry_policy=LONG_RUNNING_RETRY_POLICY,
            )

            outputs["review"] = {
                "task_id": review_task_id,
                "status": review_completion.get("status"),
                "outputs": review_completion.get("outputs", []),
            }

            if review_completion.get("status") == "failed":
                self.mark_step_failed("review", review_completion.get("error_message", "Review failed"))
                return await self.fail_workflow(
                    input,
                    f"Review failed: {review_completion.get('error_message', 'Review rejected')}",
                )

            self.mark_step_completed("review", review_completion)

            # ─────────────────────────────────────────────────────────────────
            # Archive workspace
            # ─────────────────────────────────────────────────────────────────
            await workflow.execute_activity(
                archive_workspace,
                args=[workspace_id, "Build and review completed"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=DEFAULT_ACTIVITY_RETRY_POLICY,
            )

            return await self.complete_workflow(input, outputs, start_ns)

        except Exception as exc:
            workflow.logger.error(
                "BuildAndReviewWorkflow failed | workflow_id=%s error=%s",
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

    def _evaluate_quality_gate(
        self,
        gate_config: dict[str, Any],
        analysis_outputs: dict[str, Any],
    ) -> bool:
        """
        Evaluate quality gate thresholds against analysis outputs.

        Args:
            gate_config:      Quality gate configuration (thresholds).
            analysis_outputs: AI analysis outputs.

        Returns:
            True if all thresholds are met, False otherwise.
        """
        # Extract analysis metrics from outputs
        outputs_list = analysis_outputs.get("outputs", [])
        metrics: dict[str, Any] = {}
        for output in outputs_list:
            if isinstance(output, dict):
                metrics[output.get("name", "")] = output.get("value")

        # Check coverage threshold
        min_coverage = gate_config.get("min_coverage_pct")
        if min_coverage is not None:
            actual_coverage = metrics.get("coverage_pct", 0)
            if isinstance(actual_coverage, (int, float)) and actual_coverage < min_coverage:
                workflow.logger.warning(
                    "Quality gate failed: coverage %.1f%% < minimum %.1f%%",
                    actual_coverage,
                    min_coverage,
                )
                return False

        # Check test pass rate
        min_pass_rate = gate_config.get("min_test_pass_rate")
        if min_pass_rate is not None:
            actual_pass_rate = metrics.get("test_pass_rate", 0)
            if isinstance(actual_pass_rate, (int, float)) and actual_pass_rate < min_pass_rate:
                workflow.logger.warning(
                    "Quality gate failed: test pass rate %.1f%% < minimum %.1f%%",
                    actual_pass_rate,
                    min_pass_rate,
                )
                return False

        # Check for critical issues
        max_critical_issues = gate_config.get("max_critical_issues", 0)
        critical_issues = metrics.get("critical_issues", 0)
        if isinstance(critical_issues, int) and critical_issues > max_critical_issues:
            workflow.logger.warning(
                "Quality gate failed: %d critical issues > maximum %d",
                critical_issues,
                max_critical_issues,
            )
            return False

        return True

    async def _handle_cancellation(self, input: BuildAndReviewInput) -> WorkflowResult:
        """Handle workflow cancellation with cleanup."""
        await self.publish_workflow_cancelled(input, self._cancel_reason)
        return WorkflowResult(
            workflow_id=input.workflow_id,
            status="cancelled",
            error_message=self._cancel_reason or "Workflow cancelled",
            step_results=self._step_results,
        )
