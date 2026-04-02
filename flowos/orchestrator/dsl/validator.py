"""
orchestrator/dsl/validator.py — FlowOS Workflow DSL Semantic Validator

Performs semantic validation of parsed ``WorkflowDefinition`` objects.
While the parser handles structural validation (YAML syntax, required fields,
type checks), the validator handles semantic rules:

- Dependency graph is a valid DAG (no cycles)
- Step execution order is achievable (topological sort)
- Input/output references are resolvable
- Agent type constraints are consistent
- Timeout and retry configurations are sensible
- Workflow-level inputs/outputs are properly declared

Usage::

    from orchestrator.dsl.validator import DSLValidator, ValidationResult

    validator = DSLValidator()
    result = validator.validate(definition)

    if result.is_valid:
        print("Workflow is valid!")
    else:
        for error in result.errors:
            print(f"ERROR: {error.message} (field: {error.field})")
        for warning in result.warnings:
            print(f"WARNING: {warning.message}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.models.workflow import WorkflowDefinition, WorkflowStep

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ValidationError:
    """
    A single validation error found in a workflow definition.

    Attributes:
        message:  Human-readable error description.
        field:    Optional dot-path to the field that caused the error.
        step_id:  Optional step ID where the error occurred.
        code:     Optional machine-readable error code.
    """

    message: str
    field: str | None = None
    step_id: str | None = None
    code: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.step_id:
            parts.append(f"(step: {self.step_id})")
        if self.field:
            parts.append(f"[field: {self.field}]")
        return " ".join(parts)


@dataclass
class ValidationWarning:
    """
    A non-fatal validation warning for a workflow definition.

    Attributes:
        message:  Human-readable warning description.
        field:    Optional dot-path to the field that triggered the warning.
        step_id:  Optional step ID where the warning occurred.
        code:     Optional machine-readable warning code.
    """

    message: str
    field: str | None = None
    step_id: str | None = None
    code: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.step_id:
            parts.append(f"(step: {self.step_id})")
        if self.field:
            parts.append(f"[field: {self.field}]")
        return " ".join(parts)


@dataclass
class ValidationResult:
    """
    The result of validating a workflow definition.

    Attributes:
        errors:           List of validation errors (fatal).
        warnings:         List of validation warnings (non-fatal).
        execution_order:  Topologically sorted step IDs (empty if invalid).
        step_levels:      Dict mapping step_id to its parallel execution level.
    """

    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationWarning] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)
    step_levels: dict[str, int] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Return True if there are no validation errors."""
        return len(self.errors) == 0

    @property
    def error_count(self) -> int:
        """Return the number of validation errors."""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """Return the number of validation warnings."""
        return len(self.warnings)

    def add_error(
        self,
        message: str,
        field: str | None = None,
        step_id: str | None = None,
        code: str | None = None,
    ) -> None:
        """Add a validation error."""
        self.errors.append(ValidationError(
            message=message,
            field=field,
            step_id=step_id,
            code=code,
        ))

    def add_warning(
        self,
        message: str,
        field: str | None = None,
        step_id: str | None = None,
        code: str | None = None,
    ) -> None:
        """Add a validation warning."""
        self.warnings.append(ValidationWarning(
            message=message,
            field=field,
            step_id=step_id,
            code=code,
        ))

    def summary(self) -> str:
        """Return a human-readable summary of the validation result."""
        if self.is_valid:
            return (
                f"Workflow is valid. "
                f"{self.warning_count} warning(s). "
                f"Execution order: {' → '.join(self.execution_order)}"
            )
        return (
            f"Workflow is INVALID. "
            f"{self.error_count} error(s), {self.warning_count} warning(s)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────


class DSLValidator:
    """
    Semantic validator for FlowOS workflow definitions.

    Validates a parsed ``WorkflowDefinition`` against semantic rules:
    - DAG integrity (no cycles, valid dependencies)
    - Topological ordering
    - Input/output reference consistency
    - Timeout and retry sanity checks
    - Agent type consistency

    Thread-safe: a single instance can be reused across calls.
    """

    # Thresholds for warnings
    WARN_TIMEOUT_SECS: int = 86400 * 7  # 7 days
    WARN_RETRY_COUNT: int = 5
    WARN_STEP_COUNT: int = 50
    WARN_PARALLEL_WIDTH: int = 20  # Steps at the same level

    def validate(self, definition: WorkflowDefinition) -> ValidationResult:
        """
        Validate a ``WorkflowDefinition`` and return a ``ValidationResult``.

        Args:
            definition: The parsed workflow definition to validate.

        Returns:
            A ``ValidationResult`` with errors, warnings, and execution order.
        """
        result = ValidationResult()

        # Run all validation checks
        self._validate_metadata(definition, result)
        self._validate_steps_exist(definition, result)

        if not result.errors:
            # Only proceed with graph analysis if basic structure is valid
            self._validate_dependency_graph(definition, result)

        if not result.errors:
            # Compute execution order and levels
            order, levels = self._topological_sort(definition)
            result.execution_order = order
            result.step_levels = levels
            self._validate_parallel_width(definition, levels, result)

        self._validate_step_details(definition, result)
        self._validate_inputs_outputs(definition, result)

        logger.debug(
            "Validated workflow | name=%s valid=%s errors=%d warnings=%d",
            definition.name,
            result.is_valid,
            result.error_count,
            result.warning_count,
        )
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Validation checks
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_metadata(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """Validate workflow-level metadata."""
        if not definition.name or not definition.name.strip():
            result.add_error(
                "Workflow name is required and must not be empty.",
                field="name",
                code="MISSING_NAME",
            )

        if not definition.version:
            result.add_warning(
                "Workflow version is not specified. Defaulting to '1.0.0'.",
                field="version",
                code="MISSING_VERSION",
            )

        if not definition.description:
            result.add_warning(
                "Workflow description is not provided. "
                "Consider adding a description for documentation purposes.",
                field="description",
                code="MISSING_DESCRIPTION",
            )

        if len(definition.steps) == 0:
            result.add_warning(
                "Workflow has no steps defined. "
                "An empty workflow will complete immediately.",
                field="steps",
                code="EMPTY_WORKFLOW",
            )

        if len(definition.steps) > self.WARN_STEP_COUNT:
            result.add_warning(
                f"Workflow has {len(definition.steps)} steps, which may be complex to manage. "
                f"Consider splitting into sub-workflows if steps exceed {self.WARN_STEP_COUNT}.",
                field="steps",
                code="LARGE_WORKFLOW",
            )

    def _validate_steps_exist(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """Validate that all step IDs are unique and non-empty."""
        seen_ids: set[str] = set()
        for i, step in enumerate(definition.steps):
            if not step.step_id:
                result.add_error(
                    f"Step at index {i} has an empty step_id.",
                    field=f"steps[{i}].step_id",
                    code="EMPTY_STEP_ID",
                )
                continue

            if step.step_id in seen_ids:
                result.add_error(
                    f"Duplicate step ID {step.step_id!r} at index {i}.",
                    field=f"steps[{i}].step_id",
                    step_id=step.step_id,
                    code="DUPLICATE_STEP_ID",
                )
            else:
                seen_ids.add(step.step_id)

    def _validate_dependency_graph(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """
        Validate the dependency graph:
        - All referenced step IDs exist
        - No self-dependencies
        - No cycles (DAG check)
        """
        step_ids = {s.step_id for s in definition.steps}

        for step in definition.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    result.add_error(
                        f"Step {step.step_id!r} depends on unknown step {dep!r}.",
                        field="depends_on",
                        step_id=step.step_id,
                        code="UNKNOWN_DEPENDENCY",
                    )
                if dep == step.step_id:
                    result.add_error(
                        f"Step {step.step_id!r} depends on itself.",
                        field="depends_on",
                        step_id=step.step_id,
                        code="SELF_DEPENDENCY",
                    )

        # Cycle detection via DFS
        if not result.errors:
            self._detect_cycles(definition, result)

    def _detect_cycles(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """Detect cycles in the dependency graph using iterative DFS."""
        step_map = {s.step_id: s for s in definition.steps}
        # Build adjacency list: step_id -> list of steps that depend on it
        # For cycle detection, we traverse in dependency direction
        visited: set[str] = set()
        in_stack: set[str] = set()
        cycle_found: list[str] = []

        def dfs(step_id: str, path: list[str]) -> bool:
            """Return True if a cycle is found."""
            visited.add(step_id)
            in_stack.add(step_id)
            path.append(step_id)

            step = step_map.get(step_id)
            if step:
                for dep in step.depends_on:
                    if dep not in visited:
                        if dfs(dep, path):
                            return True
                    elif dep in in_stack:
                        # Found a cycle
                        cycle_start = path.index(dep)
                        cycle_found.extend(path[cycle_start:] + [dep])
                        return True

            path.pop()
            in_stack.discard(step_id)
            return False

        for step in definition.steps:
            if step.step_id not in visited:
                if dfs(step.step_id, []):
                    cycle_path = " → ".join(cycle_found)
                    result.add_error(
                        f"Circular dependency detected in workflow: {cycle_path}",
                        field="steps",
                        code="CIRCULAR_DEPENDENCY",
                    )
                    break  # Report first cycle only

    def _topological_sort(
        self,
        definition: WorkflowDefinition,
    ) -> tuple[list[str], dict[str, int]]:
        """
        Compute a topological sort of the steps (Kahn's algorithm).

        Returns:
            A tuple of (ordered_step_ids, step_levels) where step_levels
            maps each step_id to its parallel execution level (0-based).
        """
        step_map = {s.step_id: s for s in definition.steps}
        in_degree: dict[str, int] = {s.step_id: 0 for s in definition.steps}
        dependents: dict[str, list[str]] = {s.step_id: [] for s in definition.steps}

        for step in definition.steps:
            for dep in step.depends_on:
                in_degree[step.step_id] += 1
                dependents[dep].append(step.step_id)

        # Kahn's algorithm with level tracking
        queue: list[str] = [sid for sid, deg in in_degree.items() if deg == 0]
        order: list[str] = []
        levels: dict[str, int] = {sid: 0 for sid in queue}

        while queue:
            # Process all steps at the current level
            next_queue: list[str] = []
            for step_id in queue:
                order.append(step_id)
                for dependent in dependents[step_id]:
                    in_degree[dependent] -= 1
                    # Level = max(parent levels) + 1
                    levels[dependent] = max(
                        levels.get(dependent, 0),
                        levels[step_id] + 1,
                    )
                    if in_degree[dependent] == 0:
                        next_queue.append(dependent)
            queue = next_queue

        return order, levels

    def _validate_parallel_width(
        self,
        definition: WorkflowDefinition,
        levels: dict[str, int],
        result: ValidationResult,
    ) -> None:
        """Warn if too many steps run in parallel at any level."""
        level_counts: dict[int, int] = {}
        for level in levels.values():
            level_counts[level] = level_counts.get(level, 0) + 1

        for level, count in level_counts.items():
            if count > self.WARN_PARALLEL_WIDTH:
                result.add_warning(
                    f"Level {level} has {count} parallel steps, which may overwhelm agents. "
                    f"Consider adding dependencies to limit parallelism to "
                    f"{self.WARN_PARALLEL_WIDTH} or fewer.",
                    field="steps",
                    code="HIGH_PARALLELISM",
                )

    def _validate_step_details(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """Validate individual step configurations."""
        for step in definition.steps:
            # Timeout warnings
            if step.timeout_secs > self.WARN_TIMEOUT_SECS:
                result.add_warning(
                    f"Step {step.step_id!r} has a very long timeout of "
                    f"{step.timeout_secs}s ({step.timeout_secs // 86400} days). "
                    "Verify this is intentional.",
                    field="timeout_secs",
                    step_id=step.step_id,
                    code="LONG_TIMEOUT",
                )

            # Retry warnings
            if step.retry_count >= self.WARN_RETRY_COUNT:
                result.add_warning(
                    f"Step {step.step_id!r} has {step.retry_count} retries configured. "
                    "High retry counts may mask underlying issues.",
                    field="retry_count",
                    step_id=step.step_id,
                    code="HIGH_RETRY_COUNT",
                )

            # Agent type specific checks
            if step.agent_type == "approval" and step.timeout_secs == 0:
                result.add_warning(
                    f"Approval step {step.step_id!r} has no timeout. "
                    "Approval steps without timeouts can block workflows indefinitely.",
                    field="timeout_secs",
                    step_id=step.step_id,
                    code="APPROVAL_NO_TIMEOUT",
                )

            if step.agent_type == "build" and step.retry_count == 0:
                result.add_warning(
                    f"Build step {step.step_id!r} has no retries configured. "
                    "Build steps often benefit from at least 1 retry for transient failures.",
                    field="retry_count",
                    step_id=step.step_id,
                    code="BUILD_NO_RETRY",
                )

            # Step name check
            if not step.name or not step.name.strip():
                result.add_error(
                    f"Step {step.step_id!r} has an empty name.",
                    field="name",
                    step_id=step.step_id,
                    code="EMPTY_STEP_NAME",
                )

    def _validate_inputs_outputs(
        self,
        definition: WorkflowDefinition,
        result: ValidationResult,
    ) -> None:
        """
        Validate workflow-level inputs and outputs.

        Checks that:
        - Input/output keys are valid identifiers
        - Steps referencing workflow inputs use declared keys
        """
        # Validate input key names
        for key in definition.inputs:
            if not key or not key.strip():
                result.add_error(
                    "Workflow inputs contain an empty key.",
                    field="inputs",
                    code="EMPTY_INPUT_KEY",
                )
            elif not key.replace("_", "").replace("-", "").isalnum():
                result.add_warning(
                    f"Workflow input key {key!r} contains special characters. "
                    "Use alphanumeric characters, hyphens, and underscores.",
                    field=f"inputs.{key}",
                    code="UNUSUAL_INPUT_KEY",
                )

        # Validate output key names
        for key in definition.outputs:
            if not key or not key.strip():
                result.add_error(
                    "Workflow outputs contain an empty key.",
                    field="outputs",
                    code="EMPTY_OUTPUT_KEY",
                )

        # Check for template references in step inputs
        # (e.g. {{ inputs.ticket_id }} should reference declared inputs)
        import re
        template_pattern = re.compile(r"\{\{\s*inputs\.(\w+)\s*\}\}")

        for step in definition.steps:
            for input_key, input_value in step.inputs.items():
                if isinstance(input_value, str):
                    for match in template_pattern.finditer(input_value):
                        ref_key = match.group(1)
                        if definition.inputs and ref_key not in definition.inputs:
                            result.add_warning(
                                f"Step {step.step_id!r} references undeclared workflow "
                                f"input {ref_key!r} in input {input_key!r}. "
                                "Declare it in the workflow 'inputs' section.",
                                field=f"steps.{step.step_id}.inputs.{input_key}",
                                step_id=step.step_id,
                                code="UNDECLARED_INPUT_REF",
                            )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level convenience function
# ─────────────────────────────────────────────────────────────────────────────

#: Module-level singleton validator instance.
_validator = DSLValidator()


def validate_workflow(definition: WorkflowDefinition) -> ValidationResult:
    """
    Validate a ``WorkflowDefinition`` and return a ``ValidationResult``.

    Convenience wrapper around ``DSLValidator.validate()``.

    Args:
        definition: The parsed workflow definition to validate.

    Returns:
        A ``ValidationResult`` with errors, warnings, and execution order.
    """
    return _validator.validate(definition)
