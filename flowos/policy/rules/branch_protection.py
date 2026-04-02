"""
policy/rules/branch_protection.py — FlowOS Branch Protection Policy Rule

Enforces Git branch protection policies.  Protected branches (e.g. ``main``,
``production``, ``release/*``) cannot be directly pushed to, merged into, or
deleted by agents that lack the required roles or approvals.

The rule intercepts the following event types:
- ``BRANCH_CREATED``        — creating a branch from a protected base
- ``HANDOFF_PREPARED``      — handing off a workspace targeting a protected branch
- ``TASK_HANDOFF_REQUESTED``— AI agent handing off to a human for protected-branch work
- ``CHECKPOINT_CREATED``    — checkpointing on a protected branch without approval
- ``WORKSPACE_SYNCED``      — syncing to a protected branch

Configuration:
    ``BranchProtectionConfig`` is a dataclass that specifies:
    - ``protected_patterns``  — list of branch name glob patterns (e.g. ``["main", "release/*"]``)
    - ``allowed_roles``       — roles that may act on protected branches directly
    - ``require_approval``    — if True, non-allowed agents get PENDING_APPROVAL instead of DENY
    - ``approvers``           — list of agent IDs that can approve protected-branch actions
    - ``allow_ai_agents``     — if False (default), AI agents are always denied on protected branches
    - ``allow_build_agents``  — if True (default), build/deploy agents are allowed

Usage::

    from policy.rules.branch_protection import BranchProtectionRule, BranchProtectionConfig

    rule = BranchProtectionRule(
        config=BranchProtectionConfig(
            protected_patterns=["main", "production", "release/*"],
            allowed_roles=["maintainer", "release-manager"],
            require_approval=True,
            approvers=["human-agent-001", "human-agent-002"],
            allow_ai_agents=False,
        )
    )
"""

from __future__ import annotations

import fnmatch
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
class BranchProtectionConfig:
    """
    Configuration for the ``BranchProtectionRule``.

    Attributes:
        protected_patterns:  Glob patterns for protected branch names.
                             Supports ``*`` and ``?`` wildcards.
                             Examples: ``["main", "production", "release/*"]``.
        allowed_roles:       RBAC roles that may act on protected branches
                             without requiring approval.
                             Examples: ``["maintainer", "release-manager"]``.
        require_approval:    If True, agents without ``allowed_roles`` receive
                             PENDING_APPROVAL instead of DENY.  If False, they
                             receive DENY immediately.
        approvers:           Agent IDs that can approve protected-branch actions.
                             Only relevant when ``require_approval=True``.
        allow_ai_agents:     If False (default), AI agents are always denied on
                             protected branches regardless of roles.
        allow_build_agents:  If True (default), BUILD and DEPLOY agents are
                             permitted on protected branches (they need to push
                             build artifacts).
        allow_system_agents: If True (default), SYSTEM agents are always
                             permitted (orchestrator, policy engine, etc.).
        rule_id:             Override the default rule ID.
        rule_name:           Override the default rule name.
        priority:            Evaluation priority (lower = runs first).
    """

    protected_patterns: list[str] = field(
        default_factory=lambda: ["main", "master", "production", "release/*", "hotfix/*"]
    )
    allowed_roles: list[str] = field(
        default_factory=lambda: ["maintainer", "release-manager", "admin"]
    )
    require_approval: bool = True
    approvers: list[str] = field(default_factory=list)
    allow_ai_agents: bool = False
    allow_build_agents: bool = True
    allow_system_agents: bool = True
    rule_id: str = "branch-protection"
    rule_name: str = "Branch Protection"
    priority: int = 10  # High priority — run early


# ─────────────────────────────────────────────────────────────────────────────
# Applicable event types
# ─────────────────────────────────────────────────────────────────────────────

#: Event types that may involve branch operations and should be evaluated.
_BRANCH_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.BRANCH_CREATED,
        EventType.HANDOFF_PREPARED,
        EventType.TASK_HANDOFF_REQUESTED,
        EventType.CHECKPOINT_CREATED,
        EventType.WORKSPACE_SYNCED,
        EventType.WORKSPACE_CREATED,
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Rule implementation
# ─────────────────────────────────────────────────────────────────────────────


class BranchProtectionRule(PolicyRule):
    """
    Enforces Git branch protection policies.

    Blocks or gates actions that target protected branches by agents that
    lack the required roles or are of a disallowed type (e.g. AI agents).

    See module docstring for full configuration details.
    """

    def __init__(self, config: BranchProtectionConfig | None = None) -> None:
        """
        Initialise the rule.

        Args:
            config: Branch protection configuration.  Defaults to a
                    ``BranchProtectionConfig`` with sensible defaults.
        """
        self._config = config or BranchProtectionConfig()

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
        Return True if the event involves a branch operation.

        The rule applies when:
        1. The event type is one of the branch-related event types, AND
        2. A target or source branch can be extracted from the context.
        """
        if context.event_type not in _BRANCH_EVENT_TYPES:
            return False

        branch = self._extract_branch(context)
        if branch is None:
            return False

        return self._is_protected(branch)

    def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        """
        Evaluate whether the agent may act on the protected branch.

        Decision tree:
        1. SYSTEM agents → ALLOW (always)
        2. BUILD/DEPLOY agents → ALLOW if ``allow_build_agents=True``
        3. AI agents → DENY if ``allow_ai_agents=False``
        4. Agents with an allowed role → ALLOW
        5. ``require_approval=True`` → PENDING_APPROVAL
        6. Otherwise → DENY
        """
        branch = self._extract_branch(context)
        agent_type = context.agent_type
        agent_id = context.agent_id

        logger.debug(
            "BranchProtectionRule evaluating | branch=%s agent_id=%s agent_type=%s",
            branch,
            agent_id,
            agent_type,
        )

        # 1. System agents are always allowed
        if self._config.allow_system_agents and self._is_system_agent(agent_type):
            return self._allow(
                f"System agent {agent_id!r} is always permitted on protected branch {branch!r}.",
                branch=branch,
                agent_type=str(agent_type),
            )

        # 2. Build/deploy agents
        if self._config.allow_build_agents and self._is_build_agent(agent_type):
            return self._allow(
                f"Build/deploy agent {agent_id!r} is permitted on protected branch {branch!r}.",
                branch=branch,
                agent_type=str(agent_type),
            )

        # 3. AI agents are blocked by default
        if not self._config.allow_ai_agents and self._is_ai_agent(agent_type):
            return self._deny(
                reason=(
                    f"AI agent {agent_id!r} is not permitted to act on protected branch "
                    f"{branch!r}.  AI agents must hand off to a human agent for "
                    f"protected-branch operations."
                ),
                severity="error",
                remediation=(
                    "Initiate a TASK_HANDOFF_REQUESTED to a human agent with the "
                    "'maintainer' or 'release-manager' role before targeting this branch."
                ),
                branch=branch,
                agent_type=str(agent_type),
                event_type=str(context.event_type),
            )

        # 4. Check RBAC roles
        if context.has_any_role(*self._config.allowed_roles):
            matching_roles = [r for r in self._config.allowed_roles if context.has_role(r)]
            return self._allow(
                f"Agent {agent_id!r} has role(s) {matching_roles!r} allowing access to "
                f"protected branch {branch!r}.",
                branch=branch,
                matching_roles=matching_roles,
            )

        # 5. Require approval if configured
        if self._config.require_approval:
            return self._pending_approval(
                reason=(
                    f"Agent {agent_id!r} (type={agent_type}) requires approval to act on "
                    f"protected branch {branch!r}.  Required roles: {self._config.allowed_roles}."
                ),
                approvers=self._config.approvers,
                approval_reason=(
                    f"Protected branch {branch!r} requires approval from a maintainer or "
                    f"release-manager before this action can proceed."
                ),
                severity="warning",
                branch=branch,
                agent_type=str(agent_type),
                required_roles=self._config.allowed_roles,
            )

        # 6. Deny
        return self._deny(
            reason=(
                f"Agent {agent_id!r} (type={agent_type}) is not authorised to act on "
                f"protected branch {branch!r}.  Required roles: {self._config.allowed_roles}."
            ),
            severity="error",
            remediation=(
                f"Assign one of the following roles to the agent: "
                f"{self._config.allowed_roles}."
            ),
            branch=branch,
            agent_type=str(agent_type),
            required_roles=self._config.allowed_roles,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _is_protected(self, branch: str) -> bool:
        """Return True if the branch name matches any protected pattern."""
        for pattern in self._config.protected_patterns:
            if fnmatch.fnmatch(branch, pattern):
                return True
        return False

    def _extract_branch(self, context: EvaluationContext) -> str | None:
        """
        Extract the target branch name from the context.

        Checks (in order):
        1. ``context.target_branch`` (set by the engine from payload enrichment)
        2. ``context.source_branch``
        3. ``payload["branch_name"]`` (BRANCH_CREATED, BRANCH_PROTECTION_TRIGGERED)
        4. ``payload["branch"]`` (CHECKPOINT_CREATED, WORKSPACE_SYNCED, WORKSPACE_CREATED)
        5. ``payload["target_branch"]``
        """
        if context.target_branch:
            return context.target_branch
        if context.source_branch:
            return context.source_branch

        payload = context.payload
        for key in ("branch_name", "branch", "target_branch", "base_branch"):
            val = payload.get(key)
            if val and isinstance(val, str):
                return val

        return None

    @staticmethod
    def _is_ai_agent(agent_type: AgentType | str) -> bool:
        return str(agent_type) == "ai"

    @staticmethod
    def _is_system_agent(agent_type: AgentType | str) -> bool:
        return str(agent_type) == "system"

    @staticmethod
    def _is_build_agent(agent_type: AgentType | str) -> bool:
        return str(agent_type) in ("build", "deploy")
