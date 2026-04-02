"""
tests/unit/test_dsl_parser.py — Unit tests for FlowOS DSL parser.

Tests cover:
- DSLParser.parse(): valid YAML parsing
- DSLParser.parse_file(): file-based parsing
- parse_workflow_yaml() convenience function
- parse_workflow_file() convenience function
- Error cases: empty YAML, invalid YAML, missing required fields
- Step dependency validation
- Step ID pattern validation
- Workflow name validation
- Version handling
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from orchestrator.dsl.parser import (
    DSLParser,
    DSLParseError,
    parse_workflow_yaml,
    parse_workflow_file,
    VALID_AGENT_TYPES,
    SUPPORTED_SCHEMA_VERSIONS,
)
from shared.models.workflow import WorkflowDefinition, WorkflowStep


# ---------------------------------------------------------------------------
# Sample YAML fixtures
# ---------------------------------------------------------------------------


MINIMAL_YAML = textwrap.dedent("""
    name: minimal-workflow
    steps:
      - id: step-one
        name: "Step One"
        agent_type: human
""")

FULL_YAML = textwrap.dedent("""
    name: full-workflow
    version: "1.0.0"
    description: "A complete workflow definition"

    inputs:
      feature_branch: "main"
      ticket_id: null

    outputs:
      artifact_url: null

    steps:
      - id: plan
        name: "Plan Feature"
        description: "AI planning step"
        agent_type: ai
        timeout_secs: 300
        retry_count: 2
        inputs:
          ticket_id: "{{ inputs.ticket_id }}"
        tags:
          - planning
          - ai

      - id: implement
        name: "Implement Feature"
        agent_type: human
        depends_on:
          - plan
        timeout_secs: 86400

      - id: build
        name: "Build and Test"
        agent_type: build
        depends_on:
          - implement
        timeout_secs: 1800
        retry_count: 3

    tags:
      - feature
      - delivery
""")

INVALID_DEPENDENCY_YAML = textwrap.dedent("""
    name: bad-deps
    steps:
      - id: step-a
        name: "Step A"
        agent_type: human
        depends_on:
          - nonexistent-step
""")

EMPTY_YAML = ""

INVALID_YAML_SYNTAX = "name: [unclosed bracket"

NOT_A_DICT_YAML = "- item1\n- item2\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDSLParserParse:
    """Tests for DSLParser.parse()."""

    def setup_method(self):
        self.parser = DSLParser()

    def test_parse_minimal_yaml(self):
        """parse() handles a minimal valid YAML definition."""
        defn = self.parser.parse(MINIMAL_YAML)
        assert isinstance(defn, WorkflowDefinition)
        assert defn.name == "minimal-workflow"
        assert len(defn.steps) == 1

    def test_parse_full_yaml(self):
        """parse() handles a complete YAML definition with all fields."""
        defn = self.parser.parse(FULL_YAML)
        assert defn.name == "full-workflow"
        assert defn.version == "1.0.0"
        assert defn.description == "A complete workflow definition"
        assert len(defn.steps) == 3
        assert defn.inputs["feature_branch"] == "main"
        assert "artifact_url" in defn.outputs
        assert "feature" in defn.tags

    def test_parse_step_fields(self):
        """parse() correctly maps all step fields."""
        defn = self.parser.parse(FULL_YAML)
        plan_step = defn.steps[0]

        assert plan_step.step_id == "plan"
        assert plan_step.name == "Plan Feature"
        assert plan_step.agent_type == "ai"
        assert plan_step.timeout_secs == 300
        assert plan_step.retry_count == 2
        assert "planning" in plan_step.tags

    def test_parse_step_dependencies(self):
        """parse() correctly maps step dependencies."""
        defn = self.parser.parse(FULL_YAML)
        implement_step = defn.steps[1]

        assert implement_step.depends_on == ["plan"]

    def test_parse_preserves_dsl_source(self):
        """parse() preserves the raw YAML source in dsl_source."""
        defn = self.parser.parse(FULL_YAML)
        assert defn.dsl_source is not None
        assert "full-workflow" in defn.dsl_source

    def test_parse_empty_yaml_raises(self):
        """parse() raises DSLParseError for empty YAML."""
        with pytest.raises(DSLParseError, match="empty"):
            self.parser.parse(EMPTY_YAML)

    def test_parse_whitespace_only_raises(self):
        """parse() raises DSLParseError for whitespace-only YAML."""
        with pytest.raises(DSLParseError, match="empty"):
            self.parser.parse("   \n\t  ")

    def test_parse_invalid_yaml_syntax_raises(self):
        """parse() raises DSLParseError for invalid YAML syntax."""
        with pytest.raises(DSLParseError):
            self.parser.parse(INVALID_YAML_SYNTAX)

    def test_parse_non_dict_yaml_raises(self):
        """parse() raises DSLParseError when YAML is not a mapping."""
        with pytest.raises(DSLParseError, match="mapping"):
            self.parser.parse(NOT_A_DICT_YAML)

    def test_parse_missing_name_raises(self):
        """parse() raises DSLParseError when 'name' field is missing."""
        yaml_no_name = textwrap.dedent("""
            steps:
              - id: step-one
                name: "Step One"
                agent_type: human
        """)
        with pytest.raises(DSLParseError):
            self.parser.parse(yaml_no_name)

    def test_parse_invalid_dependency_raises(self):
        """parse() raises DSLParseError when a step depends on a nonexistent step."""
        with pytest.raises(DSLParseError):
            self.parser.parse(INVALID_DEPENDENCY_YAML)

    def test_parse_default_version(self):
        """parse() uses '1.0.0' as the default version when not specified."""
        defn = self.parser.parse(MINIMAL_YAML)
        assert defn.version == "1.0.0"

    def test_parse_empty_steps_list(self):
        """parse() handles a workflow with no steps."""
        yaml_no_steps = textwrap.dedent("""
            name: empty-workflow
            steps: []
        """)
        defn = self.parser.parse(yaml_no_steps)
        assert defn.steps == []

    def test_parse_step_without_id_generates_step_id(self):
        """parse() generates a step_id when 'id' is not specified."""
        yaml_no_id = textwrap.dedent("""
            name: test-workflow
            steps:
              - name: "Step Without ID"
                agent_type: human
        """)
        defn = self.parser.parse(yaml_no_id)
        assert len(defn.steps) == 1
        # step_id should be a non-empty string
        assert defn.steps[0].step_id
        assert isinstance(defn.steps[0].step_id, str)

    def test_parse_all_valid_agent_types(self):
        """parse() accepts all valid agent type strings."""
        for agent_type in VALID_AGENT_TYPES:
            yaml_content = textwrap.dedent(f"""
                name: test-{agent_type}
                steps:
                  - id: step-one
                    name: "Test Step"
                    agent_type: {agent_type}
            """)
            defn = self.parser.parse(yaml_content)
            assert defn.steps[0].agent_type == agent_type

    def test_parse_multiple_steps_with_chain_dependencies(self):
        """parse() handles a chain of step dependencies."""
        yaml_chain = textwrap.dedent("""
            name: chain-workflow
            steps:
              - id: step-a
                name: "Step A"
                agent_type: ai
              - id: step-b
                name: "Step B"
                agent_type: human
                depends_on: [step-a]
              - id: step-c
                name: "Step C"
                agent_type: build
                depends_on: [step-b]
        """)
        defn = self.parser.parse(yaml_chain)
        assert len(defn.steps) == 3
        assert defn.steps[2].depends_on == ["step-b"]


class TestDSLParserParseFile:
    """Tests for DSLParser.parse_file()."""

    def setup_method(self):
        self.parser = DSLParser()

    def test_parse_file_reads_yaml_file(self, tmp_path: Path):
        """parse_file() reads and parses a YAML file."""
        yaml_file = tmp_path / "workflow.yaml"
        yaml_file.write_text(FULL_YAML)

        defn = self.parser.parse_file(str(yaml_file))

        assert defn.name == "full-workflow"
        assert len(defn.steps) == 3

    def test_parse_file_raises_for_missing_file(self, tmp_path: Path):
        """parse_file() raises FileNotFoundError for a missing file."""
        with pytest.raises(FileNotFoundError):
            self.parser.parse_file(str(tmp_path / "nonexistent.yaml"))

    def test_parse_file_accepts_path_object(self, tmp_path: Path):
        """parse_file() accepts a Path object."""
        yaml_file = tmp_path / "workflow.yaml"
        yaml_file.write_text(MINIMAL_YAML)

        defn = self.parser.parse_file(yaml_file)

        assert defn.name == "minimal-workflow"


class TestParseWorkflowYaml:
    """Tests for the parse_workflow_yaml() convenience function."""

    def test_parse_workflow_yaml_returns_definition(self):
        """parse_workflow_yaml() returns a WorkflowDefinition."""
        defn = parse_workflow_yaml(MINIMAL_YAML)
        assert isinstance(defn, WorkflowDefinition)
        assert defn.name == "minimal-workflow"

    def test_parse_workflow_yaml_raises_on_empty(self):
        """parse_workflow_yaml() raises DSLParseError for empty input."""
        with pytest.raises(DSLParseError):
            parse_workflow_yaml("")


class TestParseWorkflowFile:
    """Tests for the parse_workflow_file() convenience function."""

    def test_parse_workflow_file_returns_definition(self, tmp_path: Path):
        """parse_workflow_file() returns a WorkflowDefinition."""
        yaml_file = tmp_path / "workflow.yaml"
        yaml_file.write_text(FULL_YAML)

        defn = parse_workflow_file(str(yaml_file))

        assert isinstance(defn, WorkflowDefinition)
        assert defn.name == "full-workflow"

    def test_parse_workflow_file_raises_for_missing_file(self, tmp_path: Path):
        """parse_workflow_file() raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            parse_workflow_file(str(tmp_path / "missing.yaml"))


class TestParseExamplePipeline:
    """Tests parsing the actual example feature delivery pipeline."""

    def test_parse_feature_delivery_pipeline(self, feature_delivery_yaml_path: Path):
        """The example feature delivery pipeline YAML parses successfully."""
        defn = parse_workflow_file(str(feature_delivery_yaml_path))

        assert defn.name == "feature-delivery-pipeline"
        assert defn.version == "1.0.0"
        assert len(defn.steps) == 6

    def test_feature_delivery_pipeline_step_names(self, feature_delivery_yaml_path: Path):
        """The example pipeline has the expected step names."""
        defn = parse_workflow_file(str(feature_delivery_yaml_path))

        step_names = [s.name for s in defn.steps]
        assert "Design Feature" in step_names
        assert "Implement Feature" in step_names
        assert "Build and Test" in step_names
        assert "Code Review" in step_names

    def test_feature_delivery_pipeline_dependencies(self, feature_delivery_yaml_path: Path):
        """The example pipeline has correct step dependencies."""
        defn = parse_workflow_file(str(feature_delivery_yaml_path))

        step_by_id = {s.step_id: s for s in defn.steps}
        implement = step_by_id["implement"]
        build = step_by_id["build"]
        review = step_by_id["review"]

        assert "design" in implement.depends_on
        assert "implement" in build.depends_on
        assert "build" in review.depends_on


class TestDSLParseError:
    """Tests for DSLParseError exception."""

    def test_dsl_parse_error_message(self):
        """DSLParseError stores the message."""
        err = DSLParseError("Something went wrong")
        assert "Something went wrong" in str(err)

    def test_dsl_parse_error_with_line(self):
        """DSLParseError includes line number in message when provided."""
        err = DSLParseError("Bad field", line=42)
        assert "42" in str(err)

    def test_dsl_parse_error_with_field(self):
        """DSLParseError includes field name in message when provided."""
        err = DSLParseError("Missing value", field="agent_type")
        assert "agent_type" in str(err)
