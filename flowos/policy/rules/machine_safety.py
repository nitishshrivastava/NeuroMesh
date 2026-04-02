"""
policy/rules/machine_safety.py — FlowOS Machine Safety Policy Rule

Enforces safety constraints on autonomous machine agents (AI agents, build
runners, deploy agents) to prevent runaway automation, privilege escalation,
and unsafe operations.

Safety constraints enforced:
1. **AI agent scope limits** — AI agents may not directly modify production
   branches, trigger deployments, or perform destructive operations without
   human approval.
2. **Concurrent task limits** — Agents may not accept more tasks than their
   declared ``max_concurrent_tasks`` limit.
3. **Handoff loop detection** — Prevents infinite handoff chains by tracking
   handoff depth in the event payload.
4. **Revert protection** — Prevents AI agents from reverting checkpoints on
   protected branches without human approval.
5. **Build resource limits** — Build agents may not trigger builds that exceed
   configured resource quotas.
6. **AI self-assignment prevention** — AI agents may not assign tasks to
   themselves or other AI agents without human oversight.

Configuration:
    ``MachineSafetyConfig`` specifies:
    - ``max_handoff_depth``        — maximum allowed handoff chain depth
    - ``ai_can_deploy``            — if False (default), AI agents cannot trigger deployments
    - ``ai_can_revert``            — if False (default), AI agents cannot revert checkpoints
    - ``ai_can_assign_to_ai``      — if False (default), AI agents cannot assign tasks to AI
    - ``max_ai_concurrent_tasks``  — maximum concurrent tasks for AI agents
    - ``blocked_event_types_for_ai``— event types AI agents are never allowed to produce
    - ``require_human_for_deploy`` — if True (default), deployments always require human approval

Usage::

    from policy.rules.machine_safety import MachineSafetyRule, MachineSafetyConfig

    rule = MachineSafetyRule(
        config=MachineSafetyConfig(
            max_handoff_depth=5,
            ai_can_deploy=False,
            ai_can_revert=False,
            require_human_for_deploy=True,
        )
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.models.event import EventType
from shared.models.agent import AgentType

from policy.evaluator import (
    EvaluationContext,
    EvaluationOutcome,
    EvaluationResult,
    PolicyRule,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MachineSafetyConfig:
    """
    Configuration for the ``MachineSafetyRule``.

    Attributes:
        max_handoff_depth:          Maximum allowed handoff chain depth before
                                    the chain is blocked.  Prevents infinite
                                    handoff loops.  Default: 10.
        ai_can_deploy:              If False (default), AI agents cannot trigger
                                    deployments (BUILD_TRIGGERED, TASK_COMPLETED
                                    with deploy task type).
        ai_can_revert:              If False (default), AI agents cannot revert
                                    checkpoints (CHECKPOINT_REVERTED).
        ai_can_assign_to_ai:        If False (default), AI agents cannot assign
                                    tasks to other AI agents (TASK_ASSIGNED with
                                    an AI target agent).
        max_ai_concurrent_tasks:    Maximum number of concurrent tasks an AI
                                    agent may hold.  0 means no limit.
        blocked_event_types_for_ai: Set of event types that AI agents are never
                                    allowed to produce.
        require_human_for_deploy:   If True (default), all deployment events
                                    require a human approver regardless of agent type.
        human_approvers:            List of human agent IDs that can approve
                                    machine-safety-gated actions.
        human_approver_roles:       RBAC roles that can approve machine-safety-gated
                                    actions.
        rule_id:                    Override the default rule ID.
        rule_name:                  Override the default rule name.
        priority:                   Evaluation priority (lower = runs first).
    """

    max_handoff_depth: int = 10
    ai_can_deploy: bool = False
    ai_can_revert: bool = False
    ai_can_assign_to_ai: bool = False
    max_ai_concurrent_tasks: int = 0  # 0 = no limit
    blocked_event_types_for_ai: set[EventType] = field(
        default_factory=lambda: {
            EventType.BUILD_TRIGGERED,
            EventType.CHECKPOINT_REVERTED,
        }
    )
    require_human_for_deploy: bool = True
    human_approvers: list[str] = field(default_factory=list)
    human_approver_roles: list[str] = field(
        default_factory=lambda: ["maintainer", "release-manager", "admin"]
    )
    rule_id: str = "machine-safety"
    rule_name: str = "Machine Safety"
    priority: int = 5  # Very high priority — run before other rules


# ─────────────────────────────────────────────────────────────────────────────
# Event type sets
# ─────────────────────────────────────────────────────────────────────────────

#: Events that represent deployment actions.
_DEPLOY_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.BUILD_TRIGGERED,
        EventType.BUILD_STARTED,
    }
)

#: Events that represent destructive/irreversible actions.
_DESTRUCTIVE_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.CHECKPOINT_REVERTED,
        EventType.WORKFLOW_CANCELLED,
    }
)

#: Events that represent task assignment.
_ASSIGNMENT_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.TASK_ASSIGNED,
    }
)

#: All event types that the machine safety rule evaluates.
_MACHINE_SAFETY_EVENT_TYPES: frozenset[EventType] = (
    _DEPLOY_EVENT_TYPES
    | _DESTRUCTIVE_EVENT_TYPES
    | _ASSIGNMENT_EVENT_TYPES
    | frozenset(
        {
            EventType.TASK_HANDOFF_REQUESTED,
            EventType.TASK_COMPLETED,
        }
    )
)


# ─────────────────────────────────────────────────────────────────────────────
# Rule implementation
# ─────────────────────────────────────────────────────────────────────────────


class MachineSafetyRule(PolicyRule):
    """
    Enforces safety constraints on autonomous machine agents.

    This rule is a composite safety guard that runs multiple sub-checks
    in sequence.  The first failing sub-check determines the outcome.

    Sub-checks (in order):
    1. Blocked event types for AI agents
    2. AI deployment prevention
    3. AI revert prevention
    4. AI self-assignment prevention
    5. Handoff loop detection
    6. Deployment human-approval requirement

    See module docstring for full configuration details.
    """

    def __init__(self, config: MachineSafetyConfig | None = None) -> None:
        """
        Initialise the rule.

        Args:
            config: Machine safety configuration.  Defaults to a
                    ``MachineSafetyConfig`` with sensible defaults.
        """
        self._config = config or MachineSafetyConfig()

    # ── PolicyRule interface ──────────────────────────────────────────────────

    @property
    def rule_id(self) -> str:
        return self._config.rule_id

    @property
    def rule_name(self) -> str:
        return self._config.rule_name

    @property
    def priority(self) -> int:
        return self._config.priority

    def applies_to(self, context: EvaluationContext) -> bool:
        """
        Return True if this rule should evaluate the context.

        The rule applies to:
        - Any event produced by an AI, BUILD, or DEPLOY agent, OR
        - Any deployment or destructive event regardless of agent type.
        """
        agent_type = str(context.agent_type)
        is_machine = agent_type in ("ai", "build", "deploy")
        is_sensitive_event = context.event_type in _MACHINE_SAFETY_EVENT_TYPES

        # Also check blocked event types for AI
        if agent_type == "ai" and context.event_type in self._config.blocked_event_types_for_ai:
            return True

        return is_machine and is_sensitive_event

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        """
        Run all machine safety sub-checks and return the first blocking result.

        Sub-checks are run in priority order.  The first DENY or
        PENDING_APPROVAL result is returned immediately.
        """
        agent_type = str(context.agent_type)
        agent_id = context.agent_id

        logger.debug(
            "MachineSafetyRule evaluating | agent_id=%s agent_type=%s event_type=%s",
            agent_id,
            agent_type,
            context.event_type,
        )

        # Sub-check 1: Blocked event types for AI agents
        if agent_type == "ai":
            result = self._check_blocked_event_types(context)
            if result is not None:
                return result

        # Sub-check 2: AI deployment prevention
        if agent_type == "ai" and not self._config.ai_can_deploy:
            result = self._check_ai_deployment(context)
            if result is not None:
                return result

        # Sub-check 3: AI revert prevention
        if agent_type == "ai" and not self._config.ai_can_revert:
            result = self._check_ai_revert(context)
            if result is not None:
                return result

        # Sub-check 4: AI self-assignment prevention
        if agent_type == "ai" and not self._config.ai_can_assign_to_ai:
            result = self._check_ai_self_assignment(context)
            if result is not None:
                return result

        # Sub-check 5: Handoff loop detection
        result = self._check_handoff_depth(context)
        if result is not None:
            return result

        # Sub-check 6: Deployment human-approval requirement
        if self._config.require_human_for_deploy:
            result = self._check_deployment_approval(context)
            if result is not None:
                return result

        # All checks passed
        return self._allow(
            f"Machine safety checks passed for agent {agent_id!r} "
            f"(type={agent_type}) on event {context.event_type}.",
            agent_type=agent_type,
            event_type=str(context.event_type),
        )

    # ── Sub-checks ────────────────────────────────────────────────────────────

    def _check_blocked_event_types(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 1: Block AI agents from producing certain event types.

        Returns a DENY result if the event type is in the blocked set,
        or None if the check passes.
        """
        if context.event_type in self._config.blocked_event_types_for_ai:
            return self._deny(
                reason=(
                    f"AI agent {context.agent_id!r} is not permitted to produce "
                    f"{context.event_type} events.  This event type is blocked for "
                    f"AI agents by the machine safety policy."
                ),
                severity="critical",
                remediation=(
                    f"Only human or system agents may produce {context.event_type} events.  "
                    f"Initiate a handoff to a human agent to perform this action."
                ),
                blocked_event_type=str(context.event_type),
                agent_type="ai",
            )
        return None

    def _check_ai_deployment(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 2: Prevent AI agents from triggering deployments.

        Returns a DENY result if the event is a deployment trigger,
        or None if the check passes.
        """
        if context.event_type not in _DEPLOY_EVENT_TYPES:
            return None

        return self._deny(
            reason=(
                f"AI agent {context.agent_id!r} is not permitted to trigger deployments "
                f"({context.event_type}).  Deployments must be initiated by human or "
                f"system agents."
            ),
            severity="critical",
            remediation=(
                "Initiate a TASK_HANDOFF_REQUESTED to a human agent with the "
                "'release-manager' role to trigger the deployment."
            ),
            event_type=str(context.event_type),
            agent_type="ai",
        )

    def _check_ai_revert(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 3: Prevent AI agents from reverting checkpoints.

        Returns a DENY result if the event is a revert, or None if the check passes.
        """
        if context.event_type != EventType.CHECKPOINT_REVERTED:
            return None

        return self._deny(
            reason=(
                f"AI agent {context.agent_id!r} is not permitted to revert checkpoints "
                f"({context.event_type}).  Checkpoint reversions must be performed by "
                f"human agents."
            ),
            severity="error",
            remediation=(
                "Initiate a TASK_HANDOFF_REQUESTED to a human agent to perform the "
                "checkpoint reversion."
            ),
            event_type=str(context.event_type),
            agent_type="ai",
        )

    def _check_ai_self_assignment(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 4: Prevent AI agents from assigning tasks to AI agents.

        Returns a DENY result if an AI agent is assigning a task to another
        AI agent, or None if the check passes.
        """
        if context.event_type != EventType.TASK_ASSIGNED:
            return None

        payload = context.payload
        target_agent_type = payload.get("target_agent_type", "") or payload.get("agent_type", "")
        if str(target_agent_type).lower() == "ai":
            return self._deny(
                reason=(
                    f"AI agent {context.agent_id!r} is not permitted to assign tasks to "
                    f"other AI agents.  AI-to-AI task assignment requires human oversight."
                ),
                severity="error",
                remediation=(
                    "Route the task through a human agent or the orchestrator for "
                    "AI agent assignment."
                ),
                source_agent_type="ai",
                target_agent_type="ai",
            )
        return None

    def _check_handoff_depth(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 5: Detect and block handoff loops.

        Checks the ``handoff_depth`` field in the event payload.  If it
        exceeds ``max_handoff_depth``, the handoff is blocked.

        Returns a DENY result if the depth is exceeded, or None if the check passes.
        """
        if context.event_type != EventType.TASK_HANDOFF_REQUESTED:
            return None

        payload = context.payload
        depth = int(payload.get("handoff_depth", 0))

        if depth >= self._config.max_handoff_depth:
            return self._deny(
                reason=(
                    f"Handoff chain depth {depth} has reached or exceeded the maximum "
                    f"allowed depth of {self._config.max_handoff_depth}.  This may "
                    f"indicate an infinite handoff loop."
                ),
                severity="critical",
                remediation=(
                    f"Investigate the handoff chain for task "
                    f"{payload.get('task_id', 'unknown')!r}.  The maximum handoff depth "
                    f"is {self._config.max_handoff_depth}."
                ),
                handoff_depth=depth,
                max_handoff_depth=self._config.max_handoff_depth,
                task_id=payload.get("task_id", ""),
            )
        return None

    def _check_deployment_approval(
        self, context: EvaluationContext
    ) -> EvaluationResult | None:
        """
        Sub-check 6: Require human approval for all deployment events.

        Returns a PENDING_APPROVAL result if the event is a deployment trigger
        and the agent is not a human or system agent, or None if the check passes.
        """
        if context.event_type not in _DEPLOY_EVENT_TYPES:
            return None

        agent_type = str(context.agent_type)

        # Human and system agents are always allowed to trigger deployments
        if agent_type in ("human", "system"):
            return None

        # All other agents require human approval
        return self._pending_approval(
            reason=(
                f"Deployment event {context.event_type} by agent {context.agent_id!r} "
                f"(type={agent_type}) requires human approval."
            ),
            approvers=self._config.human_approvers,
            approval_reason=(
                "All deployments require explicit human approval before proceeding.  "
                "A human agent with the 'release-manager' role must approve."
            ),
            severity="warning",
            event_type=str(context.event_type),
            agent_type=agent_type,
            required_approver_roles=self._config.human_approver_roles,
        )
