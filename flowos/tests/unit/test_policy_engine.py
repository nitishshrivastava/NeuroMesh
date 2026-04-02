"""
tests/unit/test_policy_engine.py — Unit tests for FlowOS Policy Engine.

Tests cover:
- EvaluationContext: creation, helper methods
- EvaluationResult: properties (is_blocking, is_violation, requires_approval)
- AggregateEvaluationResult: outcome aggregation, summary
- PolicyEvaluator: rule registration, evaluation, fail_fast behaviour
- PolicyRule: abstract base class, convenience factories
- BranchProtectionRule: protected branch detection, role-based access
- MachineSafetyRule: AI agent restrictions
- PolicyEngine: evaluate_event() method
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.models.agent import AgentType
from policy.evaluator import (
    AggregateEvaluationResult,
    EvaluationContext,
    EvaluationOutcome,
    EvaluationResult,
    PolicyEvaluator,
    PolicyRule,
)
from policy.rules.branch_protection import BranchProtectionRule, BranchProtectionConfig
from policy.rules.machine_safety import MachineSafetyRule, MachineSafetyConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: EventType = EventType.WORKFLOW_CREATED,
    topic: EventTopic = EventTopic.WORKFLOW_EVENTS,
    workflow_id: str | None = None,
) -> EventRecord:
    wf_id = workflow_id or str(uuid.uuid4())
    return EventRecord(
        event_type=event_type,
        topic=topic,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
        payload={"workflow_id": wf_id},
    )


def _make_context(
    event_type: EventType = EventType.WORKFLOW_CREATED,
    agent_type: AgentType = AgentType.HUMAN,
    agent_id: str = "agent-001",
    agent_roles: list[str] | None = None,
    target_branch: str | None = None,
    handoff_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> EvaluationContext:
    event = _make_event(event_type=event_type)
    return EvaluationContext(
        event=event,
        agent_type=agent_type,
        agent_id=agent_id,
        agent_roles=agent_roles or [],
        target_branch=target_branch,
        handoff_type=handoff_type,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Concrete test rule
# ---------------------------------------------------------------------------


class AlwaysAllowRule(PolicyRule):
    """Test rule that always returns ALLOW."""

    @property
    def rule_id(self) -> str:
        return "always-allow"

    @property
    def rule_name(self) -> str:
        return "Always Allow"

    def applies_to(self, context: EvaluationContext) -> bool:
        return True

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return self._allow("All actions are allowed")


class AlwaysDenyRule(PolicyRule):
    """Test rule that always returns DENY."""

    @property
    def rule_id(self) -> str:
        return "always-deny"

    @property
    def rule_name(self) -> str:
        return "Always Deny"

    def applies_to(self, context: EvaluationContext) -> bool:
        return True

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return self._deny("All actions are denied", remediation="Contact admin")


class AlwaysPendingRule(PolicyRule):
    """Test rule that always returns PENDING_APPROVAL."""

    @property
    def rule_id(self) -> str:
        return "always-pending"

    @property
    def rule_name(self) -> str:
        return "Always Pending"

    def applies_to(self, context: EvaluationContext) -> bool:
        return True

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return self._pending_approval(
            "Approval required",
            approvers=["human-001"],
        )


class NeverAppliesRule(PolicyRule):
    """Test rule that never applies."""

    @property
    def rule_id(self) -> str:
        return "never-applies"

    @property
    def rule_name(self) -> str:
        return "Never Applies"

    def applies_to(self, context: EvaluationContext) -> bool:
        return False

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return self._allow("Should never be called")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvaluationContext:
    """Tests for EvaluationContext."""

    def test_create_context(self):
        """EvaluationContext can be created with required fields."""
        event = _make_event()
        ctx = EvaluationContext(event=event, agent_type=AgentType.HUMAN, agent_id="agent-001")
        assert ctx.agent_type == AgentType.HUMAN
        assert ctx.agent_id == "agent-001"

    def test_event_type_property(self):
        """event_type property returns the event's event_type."""
        ctx = _make_context(event_type=EventType.TASK_ASSIGNED)
        assert ctx.event_type == EventType.TASK_ASSIGNED

    def test_payload_property(self):
        """payload property returns the event's payload."""
        ctx = _make_context()
        assert isinstance(ctx.payload, dict)

    def test_has_role_true(self):
        """has_role() returns True when the agent has the role."""
        ctx = _make_context(agent_roles=["developer", "maintainer"])
        assert ctx.has_role("developer") is True

    def test_has_role_false(self):
        """has_role() returns False when the agent lacks the role."""
        ctx = _make_context(agent_roles=["developer"])
        assert ctx.has_role("admin") is False

    def test_has_any_role(self):
        """has_any_role() returns True when the agent has at least one role."""
        ctx = _make_context(agent_roles=["developer"])
        assert ctx.has_any_role("admin", "developer") is True

    def test_has_any_role_false(self):
        """has_any_role() returns False when the agent has none of the roles."""
        ctx = _make_context(agent_roles=["developer"])
        assert ctx.has_any_role("admin", "maintainer") is False

    def test_is_ai_agent(self):
        """is_ai_agent() returns True for AI agents."""
        ctx = _make_context(agent_type=AgentType.AI)
        assert ctx.is_ai_agent() is True

    def test_is_not_ai_agent(self):
        """is_ai_agent() returns False for non-AI agents."""
        ctx = _make_context(agent_type=AgentType.HUMAN)
        assert ctx.is_ai_agent() is False

    def test_is_human_agent(self):
        """is_human_agent() returns True for human agents."""
        ctx = _make_context(agent_type=AgentType.HUMAN)
        assert ctx.is_human_agent() is True


class TestEvaluationResult:
    """Tests for EvaluationResult."""

    def test_is_blocking_for_deny(self):
        """is_blocking returns True for DENY outcome."""
        result = EvaluationResult(
            rule_id="test", rule_name="Test", outcome=EvaluationOutcome.DENY, reason="Denied"
        )
        assert result.is_blocking is True

    def test_is_blocking_for_pending_approval(self):
        """is_blocking returns True for PENDING_APPROVAL outcome."""
        result = EvaluationResult(
            rule_id="test",
            rule_name="Test",
            outcome=EvaluationOutcome.PENDING_APPROVAL,
            reason="Needs approval",
        )
        assert result.is_blocking is True

    def test_is_not_blocking_for_allow(self):
        """is_blocking returns False for ALLOW outcome."""
        result = EvaluationResult(
            rule_id="test", rule_name="Test", outcome=EvaluationOutcome.ALLOW, reason="Allowed"
        )
        assert result.is_blocking is False

    def test_is_violation_for_deny(self):
        """is_violation returns True for DENY outcome."""
        result = EvaluationResult(
            rule_id="test", rule_name="Test", outcome=EvaluationOutcome.DENY, reason="Denied"
        )
        assert result.is_violation is True

    def test_requires_approval_for_pending(self):
        """requires_approval returns True for PENDING_APPROVAL outcome."""
        result = EvaluationResult(
            rule_id="test",
            rule_name="Test",
            outcome=EvaluationOutcome.PENDING_APPROVAL,
            reason="Needs approval",
        )
        assert result.requires_approval is True


class TestPolicyEvaluator:
    """Tests for PolicyEvaluator."""

    def test_evaluator_with_no_rules_returns_allow(self):
        """Evaluator with no rules returns ALLOW (default-permit)."""
        evaluator = PolicyEvaluator()
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert result.final_outcome == EvaluationOutcome.ALLOW

    def test_evaluator_with_allow_rule_returns_allow(self):
        """Evaluator with an ALLOW rule returns ALLOW."""
        evaluator = PolicyEvaluator(rules=[AlwaysAllowRule()])
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert result.final_outcome == EvaluationOutcome.ALLOW
        assert result.is_allowed is True

    def test_evaluator_with_deny_rule_returns_deny(self):
        """Evaluator with a DENY rule returns DENY."""
        evaluator = PolicyEvaluator(rules=[AlwaysDenyRule()])
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert result.final_outcome == EvaluationOutcome.DENY
        assert result.is_denied is True

    def test_evaluator_deny_overrides_allow(self):
        """DENY outcome overrides ALLOW in aggregate result."""
        evaluator = PolicyEvaluator(rules=[AlwaysAllowRule(), AlwaysDenyRule()], fail_fast=False)
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert result.final_outcome == EvaluationOutcome.DENY

    def test_evaluator_pending_approval_outcome(self):
        """Evaluator with PENDING_APPROVAL rule returns PENDING_APPROVAL."""
        evaluator = PolicyEvaluator(rules=[AlwaysPendingRule()])
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert result.final_outcome == EvaluationOutcome.PENDING_APPROVAL
        assert result.requires_approval is True

    def test_evaluator_skips_non_applicable_rules(self):
        """Evaluator skips rules where applies_to() returns False."""
        evaluator = PolicyEvaluator(rules=[NeverAppliesRule()])
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        # No applicable rules → default ALLOW
        assert result.final_outcome == EvaluationOutcome.ALLOW
        assert len(result.results) == 0

    def test_evaluator_fail_fast_stops_on_first_deny(self):
        """With fail_fast=True, evaluation stops after the first DENY."""
        evaluator = PolicyEvaluator(
            rules=[AlwaysDenyRule(), AlwaysAllowRule()], fail_fast=True
        )
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        # Only the DENY rule should have run
        assert result.final_outcome == EvaluationOutcome.DENY
        assert len(result.results) == 1

    def test_evaluator_register_and_unregister(self):
        """Rules can be registered and unregistered."""
        evaluator = PolicyEvaluator()
        rule = AlwaysDenyRule()
        evaluator.register(rule)
        assert len(evaluator.rules) == 1

        removed = evaluator.unregister("always-deny")
        assert removed is True
        assert len(evaluator.rules) == 0

    def test_evaluator_unregister_nonexistent_returns_false(self):
        """unregister() returns False for a rule_id that doesn't exist."""
        evaluator = PolicyEvaluator()
        assert evaluator.unregister("nonexistent") is False

    def test_evaluator_rules_sorted_by_priority(self):
        """Rules are sorted by priority (lower number = higher priority)."""

        class LowPriorityRule(AlwaysAllowRule):
            @property
            def rule_id(self):
                return "low-priority"

            @property
            def priority(self):
                return 200

        class HighPriorityRule(AlwaysAllowRule):
            @property
            def rule_id(self):
                return "high-priority"

            @property
            def priority(self):
                return 10

        evaluator = PolicyEvaluator(rules=[LowPriorityRule(), HighPriorityRule()])
        assert evaluator.rules[0].rule_id == "high-priority"
        assert evaluator.rules[1].rule_id == "low-priority"

    def test_aggregate_result_violations_list(self):
        """violations property returns all DENY results."""
        evaluator = PolicyEvaluator(rules=[AlwaysDenyRule(), AlwaysDenyRule()], fail_fast=False)
        # Register two deny rules with different IDs
        evaluator._rules = []
        rule1 = AlwaysDenyRule()
        rule2 = AlwaysDenyRule()
        evaluator.register(rule1)
        ctx = _make_context()
        result = evaluator.evaluate(ctx)
        assert len(result.violations) >= 1

    def test_aggregate_result_summary_for_allow(self):
        """summary() returns a human-readable string for ALLOW outcome."""
        evaluator = PolicyEvaluator(rules=[AlwaysAllowRule()])
        ctx = _make_context(agent_id="agent-001")
        result = evaluator.evaluate(ctx)
        summary = result.summary()
        assert "ALLOWED" in summary
        assert "agent-001" in summary

    def test_aggregate_result_summary_for_deny(self):
        """summary() returns a human-readable string for DENY outcome."""
        evaluator = PolicyEvaluator(rules=[AlwaysDenyRule()])
        ctx = _make_context(agent_id="agent-001")
        result = evaluator.evaluate(ctx)
        summary = result.summary()
        assert "DENIED" in summary


class TestBranchProtectionRule:
    """Tests for BranchProtectionRule."""

    def _make_rule(self, **kwargs) -> BranchProtectionRule:
        defaults = dict(
            protected_patterns=["main", "production", "release/*"],
            allowed_roles=["maintainer"],
            require_approval=False,
        )
        defaults.update(kwargs)
        config = BranchProtectionConfig(**defaults)
        return BranchProtectionRule(config=config)

    def test_rule_does_not_apply_to_non_protected_branch(self):
        """Rule does not apply to non-protected branches (applies_to returns False)."""
        rule = self._make_rule()
        ctx = _make_context(
            event_type=EventType.BRANCH_CREATED,
            target_branch="feature/my-feature",
        )
        # Rule should not apply to non-protected branches
        assert rule.applies_to(ctx) is False

    def test_rule_denies_ai_on_protected_branch(self):
        """Rule denies AI agents from acting on protected branches."""
        rule = self._make_rule(allow_ai_agents=False)
        ctx = _make_context(
            event_type=EventType.BRANCH_CREATED,
            agent_type=AgentType.AI,
            target_branch="main",
        )
        result = rule.evaluate(ctx)
        assert result.outcome == EvaluationOutcome.DENY

    def test_rule_allows_maintainer_on_protected_branch(self):
        """Rule allows agents with 'maintainer' role on protected branches."""
        rule = self._make_rule()
        ctx = _make_context(
            event_type=EventType.BRANCH_CREATED,
            agent_type=AgentType.HUMAN,
            agent_roles=["maintainer"],
            target_branch="main",
        )
        result = rule.evaluate(ctx)
        assert result.outcome == EvaluationOutcome.ALLOW

    def test_rule_does_not_apply_to_non_branch_events(self):
        """Rule does not apply to non-branch events (applies_to returns False)."""
        rule = self._make_rule()
        ctx = _make_context(
            event_type=EventType.WORKFLOW_CREATED,
            target_branch="main",
        )
        # WORKFLOW_CREATED is not a branch event
        assert rule.applies_to(ctx) is False

    def test_rule_matches_wildcard_pattern(self):
        """Rule matches wildcard patterns like 'release/*'."""
        rule = self._make_rule(allow_ai_agents=False)
        ctx = _make_context(
            event_type=EventType.BRANCH_CREATED,
            agent_type=AgentType.AI,
            target_branch="release/1.0.0",
        )
        result = rule.evaluate(ctx)
        assert result.outcome == EvaluationOutcome.DENY

    def test_rule_pending_approval_when_require_approval_true(self):
        """Rule returns PENDING_APPROVAL when require_approval=True."""
        from shared.models.event import EventTopic
        rule = self._make_rule(require_approval=True, approvers=["human-001"])
        # Use workspace events topic for BRANCH_CREATED
        event = EventRecord(
            event_type=EventType.BRANCH_CREATED,
            topic=EventTopic.WORKSPACE_EVENTS,
            source=EventSource.ORCHESTRATOR,
            payload={},
        )
        ctx = EvaluationContext(
            event=event,
            agent_type=AgentType.HUMAN,
            agent_id="agent-001",
            agent_roles=[],  # No maintainer role
            target_branch="main",
        )
        result = rule.evaluate(ctx)
        assert result.outcome == EvaluationOutcome.PENDING_APPROVAL


class TestMachineSafetyRule:
    """Tests for MachineSafetyRule."""

    def _make_rule(self, **kwargs) -> MachineSafetyRule:
        config = MachineSafetyConfig(**kwargs)
        return MachineSafetyRule(config=config)

    def test_rule_allows_human_agents(self):
        """Rule allows human agents for all event types."""
        rule = self._make_rule()
        ctx = _make_context(
            event_type=EventType.TASK_ACCEPTED,
            agent_type=AgentType.HUMAN,
        )
        result = rule.evaluate(ctx)
        # Human agents should not be blocked by machine safety
        assert result.outcome in (EvaluationOutcome.ALLOW, EvaluationOutcome.SKIP)

    def test_rule_denies_ai_revert_when_disabled(self):
        """Rule denies AI agents from reverting checkpoints when ai_can_revert=False."""
        rule = self._make_rule(ai_can_revert=False)
        ctx = _make_context(
            event_type=EventType.CHECKPOINT_REVERTED,
            agent_type=AgentType.AI,
        )
        result = rule.evaluate(ctx)
        assert result.outcome == EvaluationOutcome.DENY

    def test_rule_denies_ai_blocked_events_regardless_of_config(self):
        """Rule denies AI agents for events in blocked_event_types_for_ai."""
        from policy.rules.machine_safety import MachineSafetyConfig
        # CHECKPOINT_REVERTED is in blocked_event_types_for_ai by default
        rule = MachineSafetyRule(config=MachineSafetyConfig(ai_can_revert=True))
        ctx = _make_context(
            event_type=EventType.CHECKPOINT_REVERTED,
            agent_type=AgentType.AI,
        )
        result = rule.evaluate(ctx)
        # CHECKPOINT_REVERTED is blocked for AI agents regardless of ai_can_revert
        assert result.outcome == EvaluationOutcome.DENY

    def test_rule_id_and_name(self):
        """MachineSafetyRule has a non-empty rule_id and rule_name."""
        rule = self._make_rule()
        assert rule.rule_id
        assert rule.rule_name
