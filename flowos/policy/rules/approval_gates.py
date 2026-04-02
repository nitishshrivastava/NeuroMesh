"""
policy/rules/approval_gates.py — FlowOS Approval Gate Policy Rule

Enforces approval gates at critical workflow transition points.  Approval
gates pause workflow execution until a designated human approver explicitly
grants or denies the action.

The rule intercepts events that represent high-impact transitions:
- ``TASK_HANDOFF_REQUESTED``  — any handoff (especially AI → human or AI → protected branch)
- ``WORKFLOW_STARTED``        — workflows that require pre-flight approval
- ``TASK_COMPLETED``          — tasks whose completion triggers a deployment gate
- ``BUILD_SUCCEEDED``         — build artifacts ready for deployment approval
- ``AI_PATCH_PROPOSED``       — AI-generated code changes requiring human review
- ``CHECKPOINT_CREATED``      — checkpoints on gated workflows

Configuration:
    ``ApprovalGateConfig`` specifies:
    - ``gate_id``           — unique identifier for this gate
    - ``gate_name``         — human-readable name
    - ``trigger_events``    — set of ``EventType`` values that trigger this gate
    - ``conditions``        — list of ``GateCondition`` callables that must ALL be
                              True for the gate to fire
    - ``approvers``         — list of agent IDs that can approve
    - ``approver_roles``    — RBAC roles that can approve (alternative to explicit IDs)
    - ``require_all``       — if True, ALL approvers must approve (default: any one)
    - ``auto_approve_roles``— roles that bypass the gate automatically
    - ``priority``          — evaluation priority

Usage::

    from policy.rules.approval_gates import ApprovalGateRule, ApprovalGateConfig

    # Gate: require approval before any AI patch is applied
    rule = ApprovalGateRule(
        config=ApprovalGateConfig(
            gate_id="ai-patch-approval",
            gate_name="AI Patch Approval Gate",
            trigger_events={EventType.AI_PATCH_PROPOSED},
            approvers=["human-lead-001"],
            approver_roles=["tech-lead", "maintainer"],
        )
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

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
# Gate condition type
# ─────────────────────────────────────────────────────────────────────────────

#: A callable that takes an ``EvaluationContext`` and returns True if the
#: gate condition is met (i.e. the gate should fire).
GateCondition = Callable[[EvaluationContext], bool]


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ApprovalGateConfig:
    """
    Configuration for a single approval gate.

    Attributes:
        gate_id:            Unique identifier for this gate (used as rule_id).
        gate_name:          Human-readable name for this gate.
        trigger_events:     Set of ``EventType`` values that trigger this gate.
                            If empty, the gate triggers on all events.
        conditions:         List of callables ``(EvaluationContext) -> bool``.
                            ALL conditions must return True for the gate to fire.
                            If empty, the gate fires on all matching events.
        approvers:          Explicit list of agent IDs that can approve.
        approver_roles:     RBAC roles that can approve (any agent with these
                            roles can approve).
        auto_approve_roles: Roles that bypass the gate automatically (ALLOW).
        auto_approve_agent_types: Agent types that bypass the gate automatically.
        require_all:        If True, ALL listed approvers must approve.
                            If False (default), any one approver suffices.
        approval_message:   Human-readable message explaining why approval is needed.
        priority:           Evaluation priority (lower = runs first).
        enabled:            Whether this gate is active.
    """

    gate_id: str
    gate_name: str
    trigger_events: set[EventType] = field(default_factory=set)
    conditions: list[GateCondition] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)
    approver_roles: list[str] = field(default_factory=lambda: ["maintainer", "tech-lead", "admin"])
    auto_approve_roles: list[str] = field(default_factory=lambda: ["admin"])
    auto_approve_agent_types: list[str] = field(
        default_factory=lambda: ["system"]
    )
    require_all: bool = False
    approval_message: str = "This action requires explicit approval before proceeding."
    priority: int = 20
    enabled: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Built-in gate conditions
# ─────────────────────────────────────────────────────────────────────────────


def is_ai_agent(context: EvaluationContext) -> bool:
    """Gate condition: True if the acting agent is an AI agent."""
    return str(context.agent_type) == "ai"


def is_ai_to_human_handoff(context: EvaluationContext) -> bool:
    """Gate condition: True if this is an AI-to-human handoff."""
    if context.handoff_type:
        return context.handoff_type in ("ai_to_human", "escalation")
    # Infer from payload
    payload = context.payload
    from_agent_type = payload.get("from_agent_type", "")
    to_agent_type = payload.get("to_agent_type", "")
    return str(from_agent_type) == "ai" or str(to_agent_type) == "human"


def is_human_to_ai_handoff(context: EvaluationContext) -> bool:
    """Gate condition: True if this is a human-to-AI handoff."""
    if context.handoff_type:
        return context.handoff_type == "human_to_ai"
    payload = context.payload
    from_agent_type = payload.get("from_agent_type", "")
    to_agent_type = payload.get("to_agent_type", "")
    return str(from_agent_type) == "human" or str(to_agent_type) == "ai"


def targets_production(context: EvaluationContext) -> bool:
    """Gate condition: True if the action targets a production environment."""
    payload = context.payload
    env = payload.get("environment", "") or payload.get("target_environment", "")
    branch = context.target_branch or payload.get("branch", "") or ""
    return (
        str(env).lower() in ("production", "prod")
        or str(branch).lower() in ("main", "master", "production")
    )


def is_high_priority(context: EvaluationContext) -> bool:
    """Gate condition: True if the task/workflow has high or critical priority."""
    payload = context.payload
    priority = payload.get("priority", "normal")
    return str(priority).lower() in ("high", "critical")


# ─────────────────────────────────────────────────────────────────────────────
# Rule implementation
# ─────────────────────────────────────────────────────────────────────────────


class ApprovalGateRule(PolicyRule):
    """
    Enforces an approval gate at a critical workflow transition point.

    When the gate fires, the action is blocked with a PENDING_APPROVAL outcome
    and an ``APPROVAL_REQUESTED`` event is emitted by the engine.

    See module docstring for full configuration details.
    """

    def __init__(self, config: ApprovalGateConfig) -> None:
        """
        Initialise the rule.

        Args:
            config: Approval gate configuration.
        """
        self._config = config

    # ── PolicyRule interface ──────────────────────────────────────────────────

    @property
    def rule_id(self) -> str:
        return self._config.gate_id

    @property
    def rule_name(self) -> str:
        return self._config.gate_name

    @property
    def priority(self) -> int:
        return self._config.priority

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def applies_to(self, context: EvaluationContext) -> bool:
        """
        Return True if this gate should evaluate the context.

        The gate applies when:
        1. The event type is in ``trigger_events`` (or ``trigger_events`` is empty), AND
        2. All ``conditions`` return True.
        """
        # Check event type filter
        if self._config.trigger_events and context.event_type not in self._config.trigger_events:
            return False

        # Check all conditions
        for condition in self._config.conditions:
            try:
                if not condition(context):
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Gate condition raised an exception | gate_id=%s error=%s",
                    self._config.gate_id,
                    exc,
                )
                return False

        return True

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        """
        Evaluate whether the action should be gated for approval.

        Decision tree:
        1. System agents and auto-approve agent types → ALLOW
        2. Agents with auto-approve roles → ALLOW
        3. All other agents → PENDING_APPROVAL
        """
        agent_type = str(context.agent_type)
        agent_id = context.agent_id

        logger.debug(
            "ApprovalGateRule evaluating | gate_id=%s agent_id=%s event_type=%s",
            self._config.gate_id,
            agent_id,
            context.event_type,
        )

        # 1. Auto-approve agent types (system, etc.)
        if agent_type in self._config.auto_approve_agent_types:
            return self._allow(
                f"Agent type {agent_type!r} is auto-approved for gate {self._config.gate_id!r}.",
                gate_id=self._config.gate_id,
                agent_type=agent_type,
            )

        # 2. Auto-approve roles
        if context.has_any_role(*self._config.auto_approve_roles):
            matching = [r for r in self._config.auto_approve_roles if context.has_role(r)]
            return self._allow(
                f"Agent {agent_id!r} has auto-approve role(s) {matching!r} for gate "
                f"{self._config.gate_id!r}.",
                gate_id=self._config.gate_id,
                matching_roles=matching,
            )

        # 3. Gate fires — require approval
        approvers = self._resolve_approvers(context)
        return self._pending_approval(
            reason=(
                f"Gate {self._config.gate_name!r} requires approval for "
                f"{context.event_type} by agent {agent_id!r} (type={agent_type})."
            ),
            approvers=approvers,
            approval_reason=self._config.approval_message,
            severity="warning",
            gate_id=self._config.gate_id,
            agent_type=agent_type,
            event_type=str(context.event_type),
            require_all=self._config.require_all,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resolve_approvers(self, context: EvaluationContext) -> list[str]:
        """
        Resolve the list of approvers for this gate.

        Returns the explicit ``approvers`` list from config.  In a full
        implementation this would also look up agents with ``approver_roles``
        from the agent registry.
        """
        approvers = list(self._config.approvers)
        # Note: In production, this would query the agent registry for agents
        # with the configured approver_roles.  For now, return the explicit list.
        return approvers


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built gate configurations
# ─────────────────────────────────────────────────────────────────────────────


def ai_handoff_approval_gate(
    approvers: list[str] | None = None,
    approver_roles: list[str] | None = None,
) -> ApprovalGateRule:
    """
    Factory: create an approval gate for AI-agent handoffs.

    This gate fires whenever an AI agent requests a handoff (``TASK_HANDOFF_REQUESTED``),
    requiring a human approver to review the handoff before it proceeds.

    Args:
        approvers:      Explicit list of agent IDs that can approve.
        approver_roles: RBAC roles that can approve.

    Returns:
        A configured ``ApprovalGateRule`` instance.
    """
    return ApprovalGateRule(
        config=ApprovalGateConfig(
            gate_id="ai-handoff-approval",
            gate_name="AI Agent Handoff Approval Gate",
            trigger_events={EventType.TASK_HANDOFF_REQUESTED},
            conditions=[is_ai_agent],
            approvers=approvers or [],
            approver_roles=approver_roles or ["maintainer", "tech-lead", "admin"],
            auto_approve_roles=["admin"],
            auto_approve_agent_types=["system"],
            approval_message=(
                "An AI agent is requesting a task handoff.  A human approver must "
                "review the handoff context and approve before the task is transferred."
            ),
            priority=15,
        )
    )


def deployment_approval_gate(
    approvers: list[str] | None = None,
    approver_roles: list[str] | None = None,
) -> ApprovalGateRule:
    """
    Factory: create an approval gate for production deployments.

    This gate fires when a build succeeds and the next step targets production,
    requiring explicit deployment approval.

    Args:
        approvers:      Explicit list of agent IDs that can approve.
        approver_roles: RBAC roles that can approve.

    Returns:
        A configured ``ApprovalGateRule`` instance.
    """
    return ApprovalGateRule(
        config=ApprovalGateConfig(
            gate_id="deployment-approval",
            gate_name="Production Deployment Approval Gate",
            trigger_events={EventType.BUILD_SUCCEEDED, EventType.TASK_COMPLETED},
            conditions=[targets_production],
            approvers=approvers or [],
            approver_roles=approver_roles or ["release-manager", "admin"],
            auto_approve_roles=["admin"],
            auto_approve_agent_types=["system"],
            approval_message=(
                "A production deployment is pending.  A release manager must approve "
                "before the deployment proceeds."
            ),
            priority=20,
        )
    )


def ai_patch_approval_gate(
    approvers: list[str] | None = None,
    approver_roles: list[str] | None = None,
) -> ApprovalGateRule:
    """
    Factory: create an approval gate for AI-proposed code patches.

    This gate fires whenever an AI agent proposes a code patch
    (``AI_PATCH_PROPOSED``), requiring a human code reviewer to approve.

    Args:
        approvers:      Explicit list of agent IDs that can approve.
        approver_roles: RBAC roles that can approve.

    Returns:
        A configured ``ApprovalGateRule`` instance.
    """
    return ApprovalGateRule(
        config=ApprovalGateConfig(
            gate_id="ai-patch-approval",
            gate_name="AI Patch Review Gate",
            trigger_events={EventType.AI_PATCH_PROPOSED},
            conditions=[],  # Fires on all AI_PATCH_PROPOSED events
            approvers=approvers or [],
            approver_roles=approver_roles or ["tech-lead", "maintainer", "admin"],
            auto_approve_roles=["admin"],
            auto_approve_agent_types=["system"],
            approval_message=(
                "An AI agent has proposed a code patch.  A human reviewer must "
                "inspect and approve the patch before it is applied."
            ),
            priority=25,
        )
    )
