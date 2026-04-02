"""
orchestrator/dsl/parser.py — FlowOS Workflow DSL Parser

Parses YAML workflow definition files into validated ``WorkflowDefinition``
domain model objects.  The DSL is the declarative contract between workflow
authors and the orchestrator.

DSL Schema (YAML):
------------------

    name: feature-delivery
    version: "1.0.0"
    description: "End-to-end feature delivery workflow"

    inputs:
      feature_branch: "main"
      ticket_id: null

    outputs:
      artifact_url: null
      review_status: null

    steps:
      - id: plan
        name: "Plan Feature"
        description: "AI agent analyses requirements and creates a plan"
        agent_type: ai
        timeout_secs: 300
        retry_count: 2
        inputs:
          ticket_id: "{{ inputs.ticket_id }}"
        tags:
          - planning

      - id: implement
        name: "Implement Feature"
        agent_type: human
        depends_on:
          - plan
        timeout_secs: 86400
        inputs:
          plan: "{{ steps.plan.outputs.plan }}"

      - id: build
        name: "Build & Test"
        agent_type: build
        depends_on:
          - implement
        timeout_secs: 1800
        retry_count: 3

      - id: review
        name: "Code Review"
        agent_type: human
        depends_on:
          - build
        timeout_secs: 86400

    tags:
      - feature
      - delivery

Usage::

    from orchestrator.dsl.parser import parse_workflow_yaml, parse_workflow_file

    # From a YAML string
    definition = parse_workflow_yaml(yaml_content)

    # From a file path
    definition = parse_workflow_file("/path/to/workflow.yaml")

    # Using the class directly
    parser = DSLParser()
    definition = parser.parse(yaml_content)
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from shared.models.workflow import WorkflowDefinition, WorkflowStep

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: Supported DSL schema versions.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0", "1.0.0", "1"})

#: Valid agent type strings in the DSL.
VALID_AGENT_TYPES: frozenset[str] = frozenset({
    "human",
    "ai",
    "build",
    "deploy",
    "review",
    "approval",
    "notification",
    "integration",
    "analysis",
})

#: Maximum number of steps allowed in a single workflow.
MAX_STEPS: int = 500

#: Maximum workflow name length.
MAX_NAME_LENGTH: int = 255

#: Regex for valid step IDs (alphanumeric, hyphens, underscores).
STEP_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

#: Regex for valid workflow names.
WORKFLOW_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_\-\. ]{1,255}$")


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class DSLParseError(Exception):
    """Raised when the YAML DSL cannot be parsed or is structurally invalid."""

    def __init__(self, message: str, line: int | None = None, field: str | None = None) -> None:
        self.line = line
        self.field = field
        detail = f" (line {line})" if line else ""
        field_detail = f" [field: {field}]" if field else ""
        super().__init__(f"DSL parse error{detail}{field_detail}: {message}")


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────


class DSLParser:
    """
    Parses FlowOS YAML workflow DSL into ``WorkflowDefinition`` objects.

    The parser performs structural validation (required fields, type checks,
    dependency graph integrity) but does NOT perform semantic validation
    (e.g. checking that referenced agents exist).  Use ``DSLValidator`` for
    semantic validation.

    Thread-safe: a single instance can be reused across calls.
    """

    def parse(self, yaml_content: str) -> WorkflowDefinition:
        """
        Parse a YAML string into a ``WorkflowDefinition``.

        Args:
            yaml_content: Raw YAML string containing the workflow definition.

        Returns:
            A validated ``WorkflowDefinition`` instance.

        Raises:
            DSLParseError: If the YAML is malformed or structurally invalid.
        """
        if not yaml_content or not yaml_content.strip():
            raise DSLParseError("Workflow YAML content is empty.")

        # Parse YAML
        try:
            raw: Any = yaml.safe_load(yaml_content)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = (mark.line + 1) if mark else None
            raise DSLParseError(
                f"Invalid YAML syntax: {exc}",
                line=line,
            ) from exc

        if not isinstance(raw, dict):
            raise DSLParseError(
                "Workflow definition must be a YAML mapping (dict), "
                f"got {type(raw).__name__}."
            )

        # Extract and validate top-level fields
        name = self._require_str(raw, "name")
        self._validate_name(name)

        version = str(raw.get("version", "1.0.0"))
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise DSLParseError("'description' must be a string.", field="description")

        inputs = self._parse_dict_field(raw, "inputs")
        outputs = self._parse_dict_field(raw, "outputs")
        tags = self._parse_list_of_strings(raw, "tags")

        # Parse steps
        steps = self._parse_steps(raw.get("steps", []))

        # Build the definition
        try:
            definition = WorkflowDefinition(
                name=name,
                version=version,
                description=description,
                steps=steps,
                inputs=inputs,
                outputs=outputs,
                tags=tags,
                dsl_source=yaml_content,
            )
        except Exception as exc:
            raise DSLParseError(f"Failed to construct WorkflowDefinition: {exc}") from exc

        logger.debug(
            "Parsed workflow DSL | name=%s version=%s steps=%d",
            definition.name,
            definition.version,
            len(definition.steps),
        )
        return definition

    def parse_file(self, path: str | Path) -> WorkflowDefinition:
        """
        Parse a YAML workflow definition file.

        Args:
            path: Path to the YAML file.

        Returns:
            A validated ``WorkflowDefinition`` instance.

        Raises:
            DSLParseError: If the file cannot be read or is invalid.
            FileNotFoundError: If the file does not exist.
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Workflow DSL file not found: {file_path}")
        if not file_path.is_file():
            raise DSLParseError(f"Path is not a file: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DSLParseError(f"Cannot read workflow file {file_path}: {exc}") from exc

        logger.info("Parsing workflow DSL file: %s", file_path)
        return self.parse(content)

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _require_str(self, data: dict[str, Any], field: str) -> str:
        """Extract a required string field from a dict."""
        value = data.get(field)
        if value is None:
            raise DSLParseError(f"Required field '{field}' is missing.", field=field)
        if not isinstance(value, str):
            raise DSLParseError(
                f"Field '{field}' must be a string, got {type(value).__name__}.",
                field=field,
            )
        value = value.strip()
        if not value:
            raise DSLParseError(f"Field '{field}' must not be empty.", field=field)
        return value

    def _validate_name(self, name: str) -> None:
        """Validate a workflow name."""
        if len(name) > MAX_NAME_LENGTH:
            raise DSLParseError(
                f"Workflow name exceeds maximum length of {MAX_NAME_LENGTH} characters.",
                field="name",
            )
        if not WORKFLOW_NAME_PATTERN.match(name):
            raise DSLParseError(
                f"Workflow name {name!r} contains invalid characters. "
                "Use alphanumeric characters, hyphens, underscores, dots, and spaces.",
                field="name",
            )

    def _parse_dict_field(self, data: dict[str, Any], field: str) -> dict[str, Any]:
        """Parse an optional dict field, defaulting to empty dict."""
        value = data.get(field)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise DSLParseError(
                f"Field '{field}' must be a mapping (dict), got {type(value).__name__}.",
                field=field,
            )
        return dict(value)

    def _parse_list_of_strings(self, data: dict[str, Any], field: str) -> list[str]:
        """Parse an optional list-of-strings field."""
        value = data.get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            raise DSLParseError(
                f"Field '{field}' must be a list, got {type(value).__name__}.",
                field=field,
            )
        result: list[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise DSLParseError(
                    f"Field '{field}[{i}]' must be a string, got {type(item).__name__}.",
                    field=field,
                )
            result.append(item)
        return result

    def _parse_steps(self, raw_steps: Any) -> list[WorkflowStep]:
        """Parse the steps list from the raw YAML data."""
        if raw_steps is None:
            return []
        if not isinstance(raw_steps, list):
            raise DSLParseError(
                f"'steps' must be a list, got {type(raw_steps).__name__}.",
                field="steps",
            )
        if len(raw_steps) > MAX_STEPS:
            raise DSLParseError(
                f"Workflow has {len(raw_steps)} steps, exceeding the maximum of {MAX_STEPS}.",
                field="steps",
            )

        # First pass: collect all step IDs for dependency validation
        step_id_map: dict[str, int] = {}  # step_id -> index
        for i, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                raise DSLParseError(
                    f"Step at index {i} must be a mapping (dict), "
                    f"got {type(raw_step).__name__}.",
                    field=f"steps[{i}]",
                )
            step_id = raw_step.get("id")
            if step_id is None:
                # Auto-generate a step ID if not provided
                step_id = f"step-{i + 1}"
            step_id = str(step_id).strip()
            if step_id in step_id_map:
                raise DSLParseError(
                    f"Duplicate step ID {step_id!r} at index {i}. "
                    "Step IDs must be unique within a workflow.",
                    field=f"steps[{i}].id",
                )
            step_id_map[step_id] = i

        # Second pass: parse each step
        steps: list[WorkflowStep] = []
        for i, raw_step in enumerate(raw_steps):
            step = self._parse_step(raw_step, i, step_id_map)
            steps.append(step)

        return steps

    def _parse_step(
        self,
        raw: dict[str, Any],
        index: int,
        all_step_ids: dict[str, int],
    ) -> WorkflowStep:
        """Parse a single step definition."""
        # Step ID
        step_id = raw.get("id")
        if step_id is None:
            step_id = f"step-{index + 1}"
        step_id = str(step_id).strip()

        if not STEP_ID_PATTERN.match(step_id):
            raise DSLParseError(
                f"Step ID {step_id!r} at index {index} contains invalid characters. "
                "Use alphanumeric characters, hyphens, and underscores (1-128 chars).",
                field=f"steps[{index}].id",
            )

        # Step name
        name = raw.get("name")
        if name is None:
            name = step_id  # Default name to ID
        name = str(name).strip()
        if not name:
            raise DSLParseError(
                f"Step at index {index} has an empty name.",
                field=f"steps[{index}].name",
            )

        # Description
        description = raw.get("description")
        if description is not None:
            description = str(description).strip() or None

        # Agent type
        agent_type = raw.get("agent_type", raw.get("type", "human"))
        agent_type = str(agent_type).strip().lower()
        if agent_type not in VALID_AGENT_TYPES:
            raise DSLParseError(
                f"Step {step_id!r} has invalid agent_type {agent_type!r}. "
                f"Valid types: {sorted(VALID_AGENT_TYPES)}",
                field=f"steps[{index}].agent_type",
            )

        # Dependencies
        depends_on_raw = raw.get("depends_on", [])
        if not isinstance(depends_on_raw, list):
            raise DSLParseError(
                f"Step {step_id!r} 'depends_on' must be a list.",
                field=f"steps[{index}].depends_on",
            )
        depends_on: list[str] = []
        for dep in depends_on_raw:
            dep_str = str(dep).strip()
            if dep_str not in all_step_ids:
                raise DSLParseError(
                    f"Step {step_id!r} depends on unknown step ID {dep_str!r}. "
                    f"Known step IDs: {sorted(all_step_ids.keys())}",
                    field=f"steps[{index}].depends_on",
                )
            if dep_str == step_id:
                raise DSLParseError(
                    f"Step {step_id!r} cannot depend on itself.",
                    field=f"steps[{index}].depends_on",
                )
            depends_on.append(dep_str)

        # Timeout
        timeout_secs = raw.get("timeout_secs", raw.get("timeout", 0))
        try:
            timeout_secs = int(timeout_secs)
            if timeout_secs < 0:
                raise ValueError("timeout_secs must be >= 0")
        except (TypeError, ValueError) as exc:
            raise DSLParseError(
                f"Step {step_id!r} 'timeout_secs' must be a non-negative integer.",
                field=f"steps[{index}].timeout_secs",
            ) from exc

        # Retry count
        retry_count = raw.get("retry_count", raw.get("retries", 0))
        try:
            retry_count = int(retry_count)
            if not (0 <= retry_count <= 10):
                raise ValueError("retry_count must be between 0 and 10")
        except (TypeError, ValueError) as exc:
            raise DSLParseError(
                f"Step {step_id!r} 'retry_count' must be an integer between 0 and 10.",
                field=f"steps[{index}].retry_count",
            ) from exc

        # Inputs
        inputs_raw = raw.get("inputs", {})
        if not isinstance(inputs_raw, dict):
            raise DSLParseError(
                f"Step {step_id!r} 'inputs' must be a mapping.",
                field=f"steps[{index}].inputs",
            )
        inputs = dict(inputs_raw)

        # Tags
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list):
            raise DSLParseError(
                f"Step {step_id!r} 'tags' must be a list.",
                field=f"steps[{index}].tags",
            )
        tags = [str(t) for t in tags_raw]

        return WorkflowStep(
            step_id=step_id,
            name=name,
            description=description,
            agent_type=agent_type,
            depends_on=depends_on,
            timeout_secs=timeout_secs,
            retry_count=retry_count,
            inputs=inputs,
            tags=tags,
        )

    def _detect_cycles(self, steps: list[WorkflowStep]) -> None:
        """
        Detect cycles in the step dependency graph using DFS.

        Raises:
            DSLParseError: If a cycle is detected.
        """
        step_map = {s.step_id: s for s in steps}
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(step_id: str) -> None:
            visited.add(step_id)
            in_stack.add(step_id)
            step = step_map.get(step_id)
            if step:
                for dep in step.depends_on:
                    if dep not in visited:
                        dfs(dep)
                    elif dep in in_stack:
                        raise DSLParseError(
                            f"Circular dependency detected: step {step_id!r} "
                            f"creates a cycle through {dep!r}.",
                            field="steps",
                        )
            in_stack.discard(step_id)

        for step in steps:
            if step.step_id not in visited:
                dfs(step.step_id)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level convenience functions
# ─────────────────────────────────────────────────────────────────────────────

#: Module-level singleton parser instance.
_parser = DSLParser()


def parse_workflow_yaml(yaml_content: str) -> WorkflowDefinition:
    """
    Parse a YAML string into a ``WorkflowDefinition``.

    Convenience wrapper around ``DSLParser.parse()``.

    Args:
        yaml_content: Raw YAML string containing the workflow definition.

    Returns:
        A validated ``WorkflowDefinition`` instance.

    Raises:
        DSLParseError: If the YAML is malformed or structurally invalid.
    """
    return _parser.parse(yaml_content)


def parse_workflow_file(path: str | Path) -> WorkflowDefinition:
    """
    Parse a YAML workflow definition file.

    Convenience wrapper around ``DSLParser.parse_file()``.

    Args:
        path: Path to the YAML file.

    Returns:
        A validated ``WorkflowDefinition`` instance.

    Raises:
        DSLParseError: If the file cannot be read or is invalid.
        FileNotFoundError: If the file does not exist.
    """
    return _parser.parse_file(path)
