"""
policy/engine.py — FlowOS Policy Engine

The ``PolicyEngine`` is the central coordinator of the FlowOS policy system.
It:

1. **Consumes** events from Kafka topics that the policy engine subscribes to
   (``flowos.workflow.events``, ``flowos.task.events``,
   ``flowos.workspace.events``, ``flowos.build.events``).

2. **Enriches** each event into an ``EvaluationContext`` by extracting agent
   metadata, branch names, handoff types, and other relevant fields from the
   event payload.

3. **Evaluates** the context against all registered ``PolicyRule`` instances
   via the ``PolicyEvaluator``.

4. **Emits** the appropriate policy events to ``flowos.policy.events``:
   - ``POLICY_EVALUATED``           — always emitted after evaluation
   - ``POLICY_VIOLATION_DETECTED``  — emitted when outcome is DENY
   - ``APPROVAL_REQUESTED``         — emitted when outcome is PENDING_APPROVAL
   - ``BRANCH_PROTECTION_TRIGGERED``— emitted when a branch protection rule fires

5. **Runs** as a long-lived Kafka consumer loop (``run()`` / ``run_async()``).

Architecture:
    The engine is intentionally stateless between evaluations.  All state
    (agent metadata, approval records) lives in the Kafka event stream and
    the PostgreSQL metadata store.  The engine is horizontally scalable —
    multiple instances can run in the same consumer group.

Usage::

    from policy.engine import PolicyEngine
    from policy.rules.branch_protection import BranchProtectionRule, BranchProtectionConfig
    from policy.rules.approval_gates import ai_handoff_approval_gate
    from policy.rules.machine_safety import MachineSafetyRule

    engine = PolicyEngine()
    engine.register_rule(MachineSafetyRule())
    engine.register_rule(BranchProtectionRule())
    engine.register_rule(ai_handoff_approval_gate(approvers=["human-lead-001"]))

    # Evaluate a single event (useful for testing)
    result = engine.evaluate_event(event)

    # Run the full consumer loop
    engine.run()
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from shared.config import settings
from shared.models.event import EventRecord, EventSource, EventTopic, EventType
from shared.models.agent import AgentType
from shared.kafka.consumer import FlowOSConsumer
from shared.kafka.producer import FlowOSProducer
from shared.kafka.topics import ConsumerGroup, KafkaTopic
from shared.kafka.schemas import (
    ApprovalRequestedPayload,
    BranchProtectionTriggeredPayload,
    PolicyEvaluatedPayload,
    PolicyViolationDetectedPayload,
    build_event,
)

from policy.evaluator import (
    AggregateEvaluationResult,
    EvaluationContext,
    EvaluationOutcome,
    PolicyEvaluator,
    PolicyRule,
)
from policy.rules.branch_protection import BranchProtectionRule, BranchProtectionConfig
from policy.rules.approval_gates import (
    ApprovalGateRule,
    ai_handoff_approval_gate,
    ai_patch_approval_gate,
)
from policy.rules.machine_safety import MachineSafetyRule, MachineSafetyConfig

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Topics the policy engine subscribes to
# ─────────────────────────────────────────────────────────────────────────────

_SUBSCRIBED_TOPICS: list[str] = [
    KafkaTopic.WORKFLOW_EVENTS,
    KafkaTopic.TASK_EVENTS,
    KafkaTopic.WORKSPACE_EVENTS,
    KafkaTopic.BUILD_EVENTS,
]

# ─────────────────────────────────────────────────────────────────────────────
# Engine identifier
# ─────────────────────────────────────────────────────────────────────────────

_ENGINE_INSTANCE_ID: str = f"policy-engine-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# Policy Engine
# ─────────────────────────────────────────────────────────────────────────────


class PolicyEngine:
    """
    Central coordinator of the FlowOS policy system.

    The engine consumes events from Kafka, evaluates them against registered
    policy rules, and emits policy events back to Kafka.

    Lifecycle::

        engine = PolicyEngine()
        engine.register_rule(MachineSafetyRule())
        engine.register_rule(BranchProtectionRule())
        engine.run()          # blocks until shutdown signal

    Thread safety:
        ``evaluate_event()`` is thread-safe.  ``register_rule()`` and
        ``unregister_rule()`` should be called before ``run()`` starts.

    Args:
        evaluator:          Optional pre-configured ``PolicyEvaluator``.
                            If None, a default evaluator is created.
        producer:           Optional pre-configured ``FlowOSProducer``.
                            If None, a new producer is created.
        consumer:           Optional pre-configured ``FlowOSConsumer``.
                            If None, a new consumer is created.
        engine_id:          Override the engine instance ID.
        fail_fast:          If True (default), stop rule evaluation after the
                            first DENY.
        load_default_rules: If True (default), load the built-in default rules.
    """

    def __init__(
        self,
        evaluator: PolicyEvaluator | None = None,
        producer: FlowOSProducer | None = None,
        consumer: FlowOSConsumer | None = None,
        engine_id: str | None = None,
        fail_fast: bool = True,
        load_default_rules: bool = True,
    ) -> None:
        self._engine_id = engine_id or _ENGINE_INSTANCE_ID
        self._evaluator = evaluator or PolicyEvaluator(fail_fast=fail_fast)
        self._producer = producer
        self._consumer = consumer
        self._running = False
        self._shutdown_event = threading.Event()

        if load_default_rules:
            self._load_default_rules()

        logger.info(
            "PolicyEngine initialised | engine_id=%s rules=%d",
            self._engine_id,
            len(self._evaluator.rules),
        )

    # ── Rule management ───────────────────────────────────────────────────────

    def register_rule(self, rule: PolicyRule) -> None:
        """
        Register a policy rule with the engine.

        Args:
            rule: The ``PolicyRule`` instance to register.
        """
        self._evaluator.register(rule)
        logger.info(
            "Registered rule | engine_id=%s rule_id=%s rule_name=%s priority=%d",
            self._engine_id,
            rule.rule_id,
            rule.rule_name,
            rule.priority,
        )

    def unregister_rule(self, rule_id: str) -> bool:
        """
        Remove a rule by its rule_id.

        Args:
            rule_id: The rule identifier to remove.

        Returns:
            True if the rule was found and removed, False otherwise.
        """
        removed = self._evaluator.unregister(rule_id)
        if removed:
            logger.info(
                "Unregistered rule | engine_id=%s rule_id=%s",
                self._engine_id,
                rule_id,
            )
        return removed

    @property
    def rules(self) -> list[PolicyRule]:
        """Return the registered rules in priority order."""
        return self._evaluator.rules

    # ── Event evaluation ──────────────────────────────────────────────────────

    def evaluate_event(self, event: EventRecord) -> AggregateEvaluationResult:
        """
        Evaluate a single event against all registered policy rules.

        This method:
        1. Builds an ``EvaluationContext`` from the event.
        2. Runs the evaluator.
        3. Emits the appropriate policy events to Kafka.
        4. Returns the aggregate result.

        Args:
            event: The ``EventRecord`` to evaluate.

        Returns:
            The ``AggregateEvaluationResult`` from the evaluator.
        """
        context = self._build_context(event)
        result = self._evaluator.evaluate(context)
        self._emit_policy_events(result)
        return result

    # ── Consumer loop ─────────────────────────────────────────────────────────

    def run(self, poll_timeout: float = 1.0) -> None:
        """
        Start the policy engine consumer loop.

        Blocks until a shutdown signal (SIGINT/SIGTERM) is received or
        ``stop()`` is called.

        Args:
            poll_timeout: Kafka poll timeout in seconds.
        """
        self._running = True
        self._shutdown_event.clear()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

        consumer = self._get_consumer()
        producer = self._get_producer()

        logger.info(
            "PolicyEngine starting consumer loop | engine_id=%s topics=%s",
            self._engine_id,
            _SUBSCRIBED_TOPICS,
        )

        try:
            while not self._shutdown_event.is_set():
                try:
                    msg = consumer.poll(timeout=poll_timeout)
                    if msg is None:
                        continue

                    event = EventRecord.from_kafka_value(msg.value())
                    logger.debug(
                        "PolicyEngine received event | event_type=%s event_id=%s",
                        event.event_type,
                        event.event_id,
                    )

                    try:
                        self.evaluate_event(event)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "PolicyEngine failed to evaluate event | event_id=%s error=%s",
                            event.event_id,
                            exc,
                            exc_info=True,
                        )

                    consumer.commit(msg)

                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "PolicyEngine consumer loop error | engine_id=%s error=%s",
                        self._engine_id,
                        exc,
                        exc_info=True,
                    )

        finally:
            logger.info(
                "PolicyEngine shutting down | engine_id=%s",
                self._engine_id,
            )
            consumer.close()
            producer.flush()
            producer.close()
            self._running = False

    def stop(self) -> None:
        """Signal the consumer loop to stop gracefully."""
        logger.info("PolicyEngine stop requested | engine_id=%s", self._engine_id)
        self._shutdown_event.set()

    # ── Context building ──────────────────────────────────────────────────────

    def _build_context(self, event: EventRecord) -> EvaluationContext:
        """
        Build an ``EvaluationContext`` from an ``EventRecord``.

        Extracts agent metadata, branch names, handoff types, and other
        relevant fields from the event payload and envelope.

        Args:
            event: The ``EventRecord`` to build context from.

        Returns:
            A fully populated ``EvaluationContext``.
        """
        payload = event.payload
        agent_id = event.agent_id or payload.get("agent_id", "") or payload.get("from_agent_id", "")
        agent_type_str = payload.get("agent_type", "") or payload.get("from_agent_type", "")

        # Resolve agent type
        agent_type = self._resolve_agent_type(agent_type_str, event.source)

        # Extract branch information
        target_branch = self._extract_target_branch(event)
        source_branch = self._extract_source_branch(event)

        # Extract handoff type
        handoff_type = payload.get("handoff_type") or payload.get("reason")

        # Extract workspace ID
        workspace_id = (
            event.payload.get("workspace_id")
            or payload.get("workspace_id")
        )

        return EvaluationContext(
            event=event,
            agent_type=agent_type,
            agent_id=str(agent_id),
            agent_roles=self._extract_agent_roles(payload),
            agent_tags=self._extract_agent_tags(payload),
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            workspace_id=workspace_id,
            target_branch=target_branch,
            source_branch=source_branch,
            handoff_type=handoff_type,
            extra=self._extract_extra(event),
        )

    @staticmethod
    def _resolve_agent_type(
        agent_type_str: str,
        source: EventSource,
    ) -> AgentType | str:
        """
        Resolve the agent type from the payload string or event source.

        Falls back to inferring from the event source if the payload doesn't
        specify an agent type.
        """
        if agent_type_str:
            try:
                return AgentType(agent_type_str.lower())
            except ValueError:
                return agent_type_str.lower()

        # Infer from event source
        source_to_type: dict[str, AgentType] = {
            EventSource.AI_AGENT: AgentType.AI,
            EventSource.HUMAN_AGENT: AgentType.HUMAN,
            EventSource.BUILD_RUNNER: AgentType.BUILD,
            EventSource.ORCHESTRATOR: AgentType.SYSTEM,
            EventSource.POLICY_ENGINE: AgentType.SYSTEM,
            EventSource.SYSTEM: AgentType.SYSTEM,
            EventSource.WORKSPACE_MANAGER: AgentType.SYSTEM,
        }
        return source_to_type.get(source, AgentType.SYSTEM)

    @staticmethod
    def _extract_target_branch(event: EventRecord) -> str | None:
        """Extract the target branch from the event payload."""
        payload = event.payload
        for key in ("branch_name", "branch", "target_branch", "to_branch"):
            val = payload.get(key)
            if val and isinstance(val, str):
                return val
        return None

    @staticmethod
    def _extract_source_branch(event: EventRecord) -> str | None:
        """Extract the source branch from the event payload."""
        payload = event.payload
        for key in ("base_branch", "from_branch", "source_branch"):
            val = payload.get(key)
            if val and isinstance(val, str):
                return val
        return None

    @staticmethod
    def _extract_agent_roles(payload: dict[str, Any]) -> list[str]:
        """Extract RBAC roles from the event payload."""
        roles = payload.get("agent_roles") or payload.get("roles") or []
        if isinstance(roles, list):
            return [str(r) for r in roles]
        return []

    @staticmethod
    def _extract_agent_tags(payload: dict[str, Any]) -> list[str]:
        """Extract agent tags from the event payload."""
        tags = payload.get("agent_tags") or payload.get("tags") or []
        if isinstance(tags, list):
            return [str(t) for t in tags]
        return []

    @staticmethod
    def _extract_extra(event: EventRecord) -> dict[str, Any]:
        """Extract extra context data from the event."""
        return {
            "event_id": event.event_id,
            "source": str(event.source),
            "schema_version": event.schema_version,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
        }

    # ── Policy event emission ─────────────────────────────────────────────────

    def _emit_policy_events(self, result: AggregateEvaluationResult) -> None:
        """
        Emit the appropriate policy events to Kafka based on the evaluation result.

        Always emits ``POLICY_EVALUATED``.
        Emits ``POLICY_VIOLATION_DETECTED`` for DENY outcomes.
        Emits ``APPROVAL_REQUESTED`` for PENDING_APPROVAL outcomes.
        Emits ``BRANCH_PROTECTION_TRIGGERED`` when a branch protection rule fires.
        """
        producer = self._get_producer()
        context = result.context
        event = context.event

        # Always emit POLICY_EVALUATED
        self._emit_policy_evaluated(producer, result)

        # Emit violation event for DENY outcomes
        if result.is_denied:
            self._emit_policy_violation(producer, result)

        # Emit approval request for PENDING_APPROVAL outcomes
        if result.requires_approval:
            self._emit_approval_requested(producer, result)

        # Emit branch protection event if a branch protection rule fired
        self._maybe_emit_branch_protection(producer, result)

    def _emit_policy_evaluated(
        self,
        producer: FlowOSProducer,
        result: AggregateEvaluationResult,
    ) -> None:
        """Emit a ``POLICY_EVALUATED`` event."""
        context = result.context
        event = context.event

        # Use the primary blocking result for the policy_id/name, or the first result
        primary = (
            result.primary_violation
            or result.primary_approval_request
            or (result.results[0] if result.results else None)
        )

        payload = PolicyEvaluatedPayload(
            policy_id=primary.rule_id if primary else "no-rules",
            policy_name=primary.rule_name if primary else "No Rules",
            subject_type=self._infer_subject_type(event),
            subject_id=self._infer_subject_id(event),
            outcome=str(result.final_outcome),
            evaluated_by=self._engine_id,
        )

        policy_event = build_event(
            event_type=EventType.POLICY_EVALUATED,
            payload=payload,
            source=EventSource.POLICY_ENGINE,
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            agent_id=context.agent_id or None,
            causation_id=event.event_id,
        )

        try:
            producer.produce(policy_event)
            logger.debug(
                "Emitted POLICY_EVALUATED | outcome=%s event_id=%s",
                result.final_outcome,
                policy_event.event_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to emit POLICY_EVALUATED | error=%s",
                exc,
                exc_info=True,
            )

    def _emit_policy_violation(
        self,
        producer: FlowOSProducer,
        result: AggregateEvaluationResult,
    ) -> None:
        """Emit a ``POLICY_VIOLATION_DETECTED`` event."""
        context = result.context
        event = context.event
        violation = result.primary_violation

        if violation is None:
            return

        payload = PolicyViolationDetectedPayload(
            policy_id=violation.rule_id,
            policy_name=violation.rule_name,
            subject_type=self._infer_subject_type(event),
            subject_id=self._infer_subject_id(event),
            violation_message=violation.reason,
            severity=violation.severity,
        )

        policy_event = build_event(
            event_type=EventType.POLICY_VIOLATION_DETECTED,
            payload=payload,
            source=EventSource.POLICY_ENGINE,
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            agent_id=context.agent_id or None,
            causation_id=event.event_id,
        )

        try:
            producer.produce(policy_event)
            logger.warning(
                "POLICY_VIOLATION_DETECTED | rule=%s agent=%s event_type=%s reason=%s",
                violation.rule_id,
                context.agent_id,
                event.event_type,
                violation.reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to emit POLICY_VIOLATION_DETECTED | error=%s",
                exc,
                exc_info=True,
            )

    def _emit_approval_requested(
        self,
        producer: FlowOSProducer,
        result: AggregateEvaluationResult,
    ) -> None:
        """Emit an ``APPROVAL_REQUESTED`` event."""
        context = result.context
        event = context.event
        approval_req = result.primary_approval_request

        if approval_req is None:
            return

        payload = ApprovalRequestedPayload(
            subject_type=self._infer_subject_type(event),
            subject_id=self._infer_subject_id(event),
            requested_by=context.agent_id or self._engine_id,
            approvers=approval_req.approvers,
            reason=approval_req.approval_reason or approval_req.reason,
        )

        policy_event = build_event(
            event_type=EventType.APPROVAL_REQUESTED,
            payload=payload,
            source=EventSource.POLICY_ENGINE,
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            agent_id=context.agent_id or None,
            causation_id=event.event_id,
        )

        try:
            producer.produce(policy_event)
            logger.info(
                "APPROVAL_REQUESTED | rule=%s agent=%s event_type=%s approvers=%s",
                approval_req.rule_id,
                context.agent_id,
                event.event_type,
                approval_req.approvers,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to emit APPROVAL_REQUESTED | error=%s",
                exc,
                exc_info=True,
            )

    def _maybe_emit_branch_protection(
        self,
        producer: FlowOSProducer,
        result: AggregateEvaluationResult,
    ) -> None:
        """
        Emit a ``BRANCH_PROTECTION_TRIGGERED`` event if a branch protection
        rule fired (DENY or PENDING_APPROVAL from the branch-protection rule).
        """
        context = result.context
        event = context.event

        # Find any branch protection rule results that are blocking
        branch_results = [
            r for r in result.results
            if r.rule_id.startswith("branch-protection") and r.is_blocking
        ]

        if not branch_results:
            return

        branch_result = branch_results[0]
        branch = (
            context.target_branch
            or context.source_branch
            or event.payload.get("branch_name")
            or event.payload.get("branch")
            or "unknown"
        )

        payload = BranchProtectionTriggeredPayload(
            workspace_id=context.workspace_id or event.payload.get("workspace_id", ""),
            branch_name=branch,
            attempted_action=str(event.event_type),
            attempted_by=context.agent_id or "unknown",
            policy_id=branch_result.rule_id,
        )

        policy_event = build_event(
            event_type=EventType.BRANCH_PROTECTION_TRIGGERED,
            payload=payload,
            source=EventSource.POLICY_ENGINE,
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            agent_id=context.agent_id or None,
            causation_id=event.event_id,
        )

        try:
            producer.produce(policy_event)
            logger.info(
                "BRANCH_PROTECTION_TRIGGERED | branch=%s agent=%s event_type=%s",
                branch,
                context.agent_id,
                event.event_type,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to emit BRANCH_PROTECTION_TRIGGERED | error=%s",
                exc,
                exc_info=True,
            )

    # ── Default rules ─────────────────────────────────────────────────────────

    def _load_default_rules(self) -> None:
        """
        Load the built-in default policy rules.

        Default rules (in priority order):
        1. MachineSafetyRule (priority=5)   — blocks unsafe AI/machine actions
        2. BranchProtectionRule (priority=10) — protects main/production branches
        3. AI Handoff Approval Gate (priority=15) — gates AI handoffs
        4. AI Patch Approval Gate (priority=25) — gates AI code patches
        """
        self._evaluator.register(MachineSafetyRule())
        self._evaluator.register(BranchProtectionRule())
        self._evaluator.register(ai_handoff_approval_gate())
        self._evaluator.register(ai_patch_approval_gate())

        logger.info(
            "Loaded default policy rules | engine_id=%s count=%d",
            self._engine_id,
            len(self._evaluator.rules),
        )

    # ── Lazy resource initialisation ──────────────────────────────────────────

    def _get_producer(self) -> FlowOSProducer:
        """Return the producer, creating it lazily if needed."""
        if self._producer is None:
            self._producer = FlowOSProducer()
        return self._producer

    def _get_consumer(self) -> FlowOSConsumer:
        """Return the consumer, creating it lazily if needed."""
        if self._consumer is None:
            self._consumer = FlowOSConsumer(
                topics=_SUBSCRIBED_TOPICS,
                group_id=ConsumerGroup.POLICY_ENGINE,
            )
        return self._consumer

    # ── Signal handling ───────────────────────────────────────────────────────

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM by setting the shutdown event."""
        logger.info(
            "PolicyEngine received shutdown signal %d | engine_id=%s",
            signum,
            self._engine_id,
        )
        self._shutdown_event.set()

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _infer_subject_type(event: EventRecord) -> str:
        """Infer the subject type (task, workflow, agent, workspace) from the event."""
        event_type_str = str(event.event_type)
        if event_type_str.startswith("TASK_"):
            return "task"
        if event_type_str.startswith("WORKFLOW_"):
            return "workflow"
        if event_type_str.startswith("AGENT_"):
            return "agent"
        if event_type_str.startswith(("WORKSPACE_", "CHECKPOINT_", "BRANCH_", "HANDOFF_")):
            return "workspace"
        if event_type_str.startswith("BUILD_"):
            return "build"
        if event_type_str.startswith("AI_"):
            return "ai_task"
        return "event"

    @staticmethod
    def _infer_subject_id(event: EventRecord) -> str:
        """Infer the subject ID from the event envelope and payload."""
        return (
            event.task_id
            or event.workflow_id
            or event.agent_id
            or event.payload.get("task_id")
            or event.payload.get("workflow_id")
            or event.payload.get("workspace_id")
            or event.event_id
        )
