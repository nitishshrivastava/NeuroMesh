"""
policy/evaluator.py — FlowOS Policy Evaluator

Defines the core evaluation primitives used by all policy rules:

- ``EvaluationContext``  — Immutable snapshot of the event and its metadata
  that rules inspect to make decisions.
- ``EvaluationOutcome``  — Enum of possible rule outcomes (ALLOW, DENY,
  PENDING_APPROVAL, SKIP).
- ``EvaluationResult``   — Structured result returned by every rule evaluation,
  carrying the outcome, human-readable reason, and optional remediation hints.
- ``PolicyRule``         — Abstract base class that every concrete rule must
  implement.
- ``PolicyEvaluator``    — Orchestrates a list of ``PolicyRule`` instances,
  runs them in priority order, and aggregates the final outcome.

Design principles:
- Rules are stateless; all context is passed via ``EvaluationContext``.
- Rules are composable; the evaluator short-circuits on the first DENY unless
  ``fail_fast=False`` is requested.
- Every evaluation produces a structured audit trail via ``EvaluationResult``.
- The evaluator never raises; it catches rule exceptions and converts them to
  DENY outcomes with an error message so the engine always gets a result.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shared.models.event import EventRecord, EventType
from shared.models.agent import AgentType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class EvaluationOutcome(StrEnum):
    """
    Possible outcomes of a policy rule evaluation.

    - ALLOW:            The action is permitted; proceed normally.
    - DENY:             The action is forbidden; block and emit a violation event.
    - PENDING_APPROVAL: The action requires explicit human approval before
                        proceeding; emit an APPROVAL_REQUESTED event.
    - SKIP:             This rule does not apply to the current context; the
                        evaluator should continue to the next rule.
    """

    ALLOW = "allow"
    DENY = "deny"
    PENDING_APPROVAL = "pending_approval"
    SKIP = "skip"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation context
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvaluationContext:
    """
    Immutable snapshot of the event and its surrounding metadata.

    Rules inspect this context to decide whether to ALLOW, DENY, or request
    approval.  The context is constructed by the ``PolicyEngine`` from the
    incoming ``EventRecord`` and any enrichment data fetched from the
    metadata store.

    Attributes:
        event:              The raw ``EventRecord`` being evaluated.
        agent_type:         Type of the agent that produced the event
                            (human, ai, build, deploy, system).
        agent_id:           ID of the agent that produced the event.
        agent_roles:        RBAC roles assigned to the agent (e.g. ``["developer"]``).
        agent_tags:         Arbitrary tags on the agent (e.g. ``["trusted"]``).
        workflow_id:        Workflow scope, if any.
        task_id:            Task scope, if any.
        workspace_id:       Workspace scope, if any.
        target_branch:      Git branch being targeted by the action, if any.
        source_branch:      Git branch the action originates from, if any.
        handoff_type:       Handoff type string if the event is a handoff
                            (e.g. ``"ai_to_human"``, ``"human_to_ai"``).
        extra:              Arbitrary additional context data for rule-specific
                            enrichment.
    """

    event: EventRecord
    agent_type: AgentType | str = AgentType.SYSTEM
    agent_id: str = ""
    agent_roles: list[str] = field(default_factory=list)
    agent_tags: list[str] = field(default_factory=list)
    workflow_id: str | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    target_branch: str | None = None
    source_branch: str | None = None
    handoff_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> EventType:
        """Convenience accessor for the event type."""
        return self.event.event_type

    @property
    def payload(self) -> dict[str, Any]:
        """Convenience accessor for the event payload."""
        return self.event.payload

    def has_role(self, role: str) -> bool:
        """Return True if the agent has the given RBAC role."""
        return role in self.agent_roles

    def has_any_role(self, *roles: str) -> bool:
        """Return True if the agent has at least one of the given roles."""
        return any(r in self.agent_roles for r in roles)

    def has_tag(self, tag: str) -> bool:
        """Return True if the agent has the given tag."""
        return tag in self.agent_tags

    def is_ai_agent(self) -> bool:
        """Return True if the acting agent is an AI agent."""
        return self.agent_type == AgentType.AI or str(self.agent_type) == "ai"

    def is_human_agent(self) -> bool:
        """Return True if the acting agent is a human agent."""
        return self.agent_type == AgentType.HUMAN or str(self.agent_type) == "human"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class EvaluationResult:
    """
    Structured result returned by a single policy rule evaluation.

    Attributes:
        rule_id:            Unique identifier of the rule that produced this result.
        rule_name:          Human-readable name of the rule.
        outcome:            The evaluation outcome (ALLOW, DENY, PENDING_APPROVAL, SKIP).
        reason:             Human-readable explanation of the outcome.
        severity:           Severity level for violation events
                            (``"info"``, ``"warning"``, ``"error"``, ``"critical"``).
        remediation:        Optional hint on how to resolve a DENY or get approval.
        approvers:          List of agent IDs that can approve (for PENDING_APPROVAL).
        approval_reason:    Reason why approval is required (for PENDING_APPROVAL).
        metadata:           Arbitrary key/value metadata for audit purposes.
        evaluation_ms:      Time taken to evaluate this rule in milliseconds.
    """

    rule_id: str
    rule_name: str
    outcome: EvaluationOutcome
    reason: str
    severity: str = "info"
    remediation: str | None = None
    approvers: list[str] = field(default_factory=list)
    approval_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluation_ms: float = 0.0

    @property
    def is_blocking(self) -> bool:
        """Return True if this result blocks the action (DENY or PENDING_APPROVAL)."""
        return self.outcome in (EvaluationOutcome.DENY, EvaluationOutcome.PENDING_APPROVAL)

    @property
    def is_violation(self) -> bool:
        """Return True if this result represents a policy violation (DENY)."""
        return self.outcome == EvaluationOutcome.DENY

    @property
    def requires_approval(self) -> bool:
        """Return True if this result requires human approval."""
        return self.outcome == EvaluationOutcome.PENDING_APPROVAL


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate evaluation result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AggregateEvaluationResult:
    """
    Aggregated result from running all applicable rules against a context.

    The aggregate outcome is determined by the most restrictive individual
    result: DENY > PENDING_APPROVAL > ALLOW > SKIP.

    Attributes:
        context:        The evaluation context that was evaluated.
        results:        Individual results from each rule that ran.
        final_outcome:  The most restrictive outcome across all rules.
        total_ms:       Total evaluation time in milliseconds.
    """

    context: EvaluationContext
    results: list[EvaluationResult] = field(default_factory=list)
    final_outcome: EvaluationOutcome = EvaluationOutcome.ALLOW
    total_ms: float = 0.0

    @property
    def is_allowed(self) -> bool:
        """Return True if the action is permitted."""
        return self.final_outcome == EvaluationOutcome.ALLOW

    @property
    def is_denied(self) -> bool:
        """Return True if the action is blocked."""
        return self.final_outcome == EvaluationOutcome.DENY

    @property
    def requires_approval(self) -> bool:
        """Return True if the action requires human approval."""
        return self.final_outcome == EvaluationOutcome.PENDING_APPROVAL

    @property
    def violations(self) -> list[EvaluationResult]:
        """Return all DENY results."""
        return [r for r in self.results if r.outcome == EvaluationOutcome.DENY]

    @property
    def approval_requests(self) -> list[EvaluationResult]:
        """Return all PENDING_APPROVAL results."""
        return [r for r in self.results if r.outcome == EvaluationOutcome.PENDING_APPROVAL]

    @property
    def primary_violation(self) -> EvaluationResult | None:
        """Return the first DENY result, or None if there are no violations."""
        violations = self.violations
        return violations[0] if violations else None

    @property
    def primary_approval_request(self) -> EvaluationResult | None:
        """Return the first PENDING_APPROVAL result, or None."""
        approvals = self.approval_requests
        return approvals[0] if approvals else None

    def summary(self) -> str:
        """Return a one-line human-readable summary of the aggregate result."""
        event_type = self.context.event.event_type
        agent_id = self.context.agent_id or "unknown"
        if self.is_denied:
            v = self.primary_violation
            return (
                f"DENIED: {event_type} by agent={agent_id} — "
                f"{v.rule_name}: {v.reason}" if v else f"DENIED: {event_type}"
            )
        if self.requires_approval:
            a = self.primary_approval_request
            return (
                f"PENDING_APPROVAL: {event_type} by agent={agent_id} — "
                f"{a.rule_name}: {a.reason}" if a else f"PENDING_APPROVAL: {event_type}"
            )
        return f"ALLOWED: {event_type} by agent={agent_id} ({len(self.results)} rules evaluated)"


# ─────────────────────────────────────────────────────────────────────────────
# Abstract rule base class
# ─────────────────────────────────────────────────────────────────────────────


class PolicyRule(abc.ABC):
    """
    Abstract base class for all FlowOS policy rules.

    Subclasses must implement:
    - ``rule_id``    — unique string identifier
    - ``rule_name``  — human-readable name
    - ``priority``   — lower numbers run first (default 100)
    - ``applies_to`` — return True if this rule should evaluate the context
    - ``evaluate``   — perform the actual evaluation and return an EvaluationResult

    Rules are stateless; all context is passed via ``EvaluationContext``.
    """

    @property
    @abc.abstractmethod
    def rule_id(self) -> str:
        """Unique string identifier for this rule (e.g. 'branch-protection-main')."""

    @property
    @abc.abstractmethod
    def rule_name(self) -> str:
        """Human-readable name for this rule."""

    @property
    def priority(self) -> int:
        """
        Evaluation priority.  Lower numbers run first.

        Rules with the same priority run in registration order.
        Default: 100.
        """
        return 100

    @property
    def enabled(self) -> bool:
        """Whether this rule is active.  Disabled rules are skipped entirely."""
        return True

    @abc.abstractmethod
    def applies_to(self, context: EvaluationContext) -> bool:
        """
        Return True if this rule should evaluate the given context.

        This is a fast pre-filter.  If it returns False, the rule is skipped
        without calling ``evaluate()``.  Use this to filter by event type,
        agent type, branch name, etc.
        """

    @abc.abstractmethod
    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        """
        Evaluate the context against this rule and return a result.

        This method must not raise exceptions.  If an unexpected error occurs,
        return a DENY result with an appropriate error message.

        Args:
            context: The evaluation context to inspect.

        Returns:
            An ``EvaluationResult`` with the outcome and explanation.
        """

    def _allow(self, reason: str, **metadata: Any) -> EvaluationResult:
        """Convenience factory for ALLOW results."""
        return EvaluationResult(
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            outcome=EvaluationOutcome.ALLOW,
            reason=reason,
            severity="info",
            metadata=dict(metadata),
        )

    def _deny(
        self,
        reason: str,
        severity: str = "error",
        remediation: str | None = None,
        **metadata: Any,
    ) -> EvaluationResult:
        """Convenience factory for DENY results."""
        return EvaluationResult(
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            outcome=EvaluationOutcome.DENY,
            reason=reason,
            severity=severity,
            remediation=remediation,
            metadata=dict(metadata),
        )

    def _pending_approval(
        self,
        reason: str,
        approvers: list[str] | None = None,
        approval_reason: str | None = None,
        severity: str = "warning",
        **metadata: Any,
    ) -> EvaluationResult:
        """Convenience factory for PENDING_APPROVAL results."""
        return EvaluationResult(
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            outcome=EvaluationOutcome.PENDING_APPROVAL,
            reason=reason,
            severity=severity,
            approvers=approvers or [],
            approval_reason=approval_reason or reason,
            metadata=dict(metadata),
        )

    def _skip(self, reason: str = "Rule does not apply") -> EvaluationResult:
        """Convenience factory for SKIP results."""
        return EvaluationResult(
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            outcome=EvaluationOutcome.SKIP,
            reason=reason,
            severity="info",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Policy evaluator
# ─────────────────────────────────────────────────────────────────────────────


class PolicyEvaluator:
    """
    Orchestrates a collection of ``PolicyRule`` instances.

    The evaluator runs all applicable rules in priority order and aggregates
    the results into a single ``AggregateEvaluationResult``.

    Outcome aggregation logic:
    1. Rules that return SKIP are excluded from the aggregate.
    2. If any rule returns DENY, the aggregate outcome is DENY (most restrictive).
    3. If no DENY but at least one PENDING_APPROVAL, the aggregate is PENDING_APPROVAL.
    4. If all applicable rules return ALLOW, the aggregate is ALLOW.
    5. If no rules apply (all SKIP), the aggregate is ALLOW (default-permit).

    Args:
        rules:      List of ``PolicyRule`` instances to evaluate.
        fail_fast:  If True (default), stop evaluating after the first DENY.
                    If False, run all rules and collect all violations.
    """

    def __init__(
        self,
        rules: list[PolicyRule] | None = None,
        fail_fast: bool = True,
    ) -> None:
        self._rules: list[PolicyRule] = []
        self._fail_fast = fail_fast
        for rule in (rules or []):
            self.register(rule)

    def register(self, rule: PolicyRule) -> None:
        """
        Register a rule with the evaluator.

        Rules are kept sorted by priority (ascending) so lower-priority-number
        rules always run first.

        Args:
            rule: The ``PolicyRule`` instance to register.
        """
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        logger.debug(
            "Registered policy rule | rule_id=%s priority=%d",
            rule.rule_id,
            rule.priority,
        )

    def unregister(self, rule_id: str) -> bool:
        """
        Remove a rule by its rule_id.

        Args:
            rule_id: The rule identifier to remove.

        Returns:
            True if the rule was found and removed, False otherwise.
        """
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        removed = len(self._rules) < before
        if removed:
            logger.debug("Unregistered policy rule | rule_id=%s", rule_id)
        return removed

    @property
    def rules(self) -> list[PolicyRule]:
        """Return the registered rules in priority order (read-only copy)."""
        return list(self._rules)

    def evaluate(self, context: EvaluationContext) -> AggregateEvaluationResult:
        """
        Evaluate all applicable rules against the given context.

        Args:
            context: The ``EvaluationContext`` to evaluate.

        Returns:
            An ``AggregateEvaluationResult`` with the final outcome and all
            individual rule results.
        """
        wall_start = time.monotonic()
        results: list[EvaluationResult] = []

        for rule in self._rules:
            if not rule.enabled:
                logger.debug("Skipping disabled rule | rule_id=%s", rule.rule_id)
                continue

            try:
                if not rule.applies_to(context):
                    logger.debug(
                        "Rule does not apply | rule_id=%s event_type=%s",
                        rule.rule_id,
                        context.event_type,
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Rule applies_to() raised an exception | rule_id=%s error=%s",
                    rule.rule_id,
                    exc,
                    exc_info=True,
                )
                # Treat as DENY to be safe
                results.append(
                    EvaluationResult(
                        rule_id=rule.rule_id,
                        rule_name=rule.rule_name,
                        outcome=EvaluationOutcome.DENY,
                        reason=f"Rule applies_to() raised an unexpected error: {exc}",
                        severity="critical",
                    )
                )
                if self._fail_fast:
                    break
                continue

            rule_start = time.monotonic()
            try:
                result = rule.evaluate(context)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Rule evaluate() raised an exception | rule_id=%s error=%s",
                    rule.rule_id,
                    exc,
                    exc_info=True,
                )
                result = EvaluationResult(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule_name,
                    outcome=EvaluationOutcome.DENY,
                    reason=f"Rule evaluate() raised an unexpected error: {exc}",
                    severity="critical",
                )
            finally:
                rule_ms = (time.monotonic() - rule_start) * 1000

            result.evaluation_ms = rule_ms
            results.append(result)

            logger.debug(
                "Rule evaluated | rule_id=%s outcome=%s reason=%s ms=%.2f",
                result.rule_id,
                result.outcome,
                result.reason,
                rule_ms,
            )

            if result.outcome == EvaluationOutcome.DENY and self._fail_fast:
                logger.info(
                    "Fail-fast: stopping evaluation after DENY | rule_id=%s",
                    rule.rule_id,
                )
                break

        # Aggregate outcome
        final_outcome = self._aggregate_outcome(results)
        total_ms = (time.monotonic() - wall_start) * 1000

        aggregate = AggregateEvaluationResult(
            context=context,
            results=results,
            final_outcome=final_outcome,
            total_ms=total_ms,
        )

        logger.info(
            "Policy evaluation complete | event_type=%s agent_id=%s outcome=%s "
            "rules_run=%d total_ms=%.2f",
            context.event_type,
            context.agent_id,
            final_outcome,
            len(results),
            total_ms,
        )

        return aggregate

    @staticmethod
    def _aggregate_outcome(results: list[EvaluationResult]) -> EvaluationOutcome:
        """
        Determine the most restrictive outcome from a list of results.

        DENY > PENDING_APPROVAL > ALLOW > SKIP (default-permit when no rules apply).
        """
        # Filter out SKIP results
        active = [r for r in results if r.outcome != EvaluationOutcome.SKIP]
        if not active:
            return EvaluationOutcome.ALLOW

        if any(r.outcome == EvaluationOutcome.DENY for r in active):
            return EvaluationOutcome.DENY

        if any(r.outcome == EvaluationOutcome.PENDING_APPROVAL for r in active):
            return EvaluationOutcome.PENDING_APPROVAL

        return EvaluationOutcome.ALLOW
