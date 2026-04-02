"""
policy/__init__.py — FlowOS Policy Engine Package

The Policy Engine enforces RBAC rules, branch protection, approval gates,
and machine safety constraints across all FlowOS events.

Public API::

    from policy.engine import PolicyEngine
    from policy.evaluator import PolicyEvaluator, EvaluationContext, EvaluationResult
    from policy.rules.branch_protection import BranchProtectionRule
    from policy.rules.approval_gates import ApprovalGateRule
    from policy.rules.machine_safety import MachineSafetyRule
"""

from policy.engine import PolicyEngine
from policy.evaluator import (
    EvaluationContext,
    EvaluationOutcome,
    EvaluationResult,
    PolicyEvaluator,
)

__all__ = [
    "PolicyEngine",
    "PolicyEvaluator",
    "EvaluationContext",
    "EvaluationOutcome",
    "EvaluationResult",
]
