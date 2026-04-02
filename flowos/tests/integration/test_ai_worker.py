"""
tests/integration/test_ai_worker.py — Integration tests for AI Worker.

Tests the AI worker's reasoning context loading, graph structure, and
event emission using mocked LLM and Kafka infrastructure.

Tests cover:
- TaskContext creation and serialisation
- ContextLoader loading workspace state
- CodeReviewState structure
- AI event emission (AI_SUGGESTION_CREATED, AI_REVIEW_COMPLETED)
- Reasoning graph state management
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.kafka.schemas import (
    AISuggestionCreatedPayload,
    AIReviewCompletedPayload,
    build_event,
)
from ai.context_loader import (
    TaskContext,
    FileChange,
    CommitSummary,
    ArtifactRef,
    ReasoningTraceSummary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_context(
    task_id: str | None = None,
    workflow_id: str | None = None,
) -> TaskContext:
    """Create a minimal TaskContext for testing."""
    return TaskContext(
        task_id=task_id or str(uuid.uuid4()),
        task_name="Code Review",
        task_type="ai",
        task_description="Review the authentication module implementation",
        workflow_id=workflow_id or str(uuid.uuid4()),
        workflow_name="feature-delivery",
        agent_id=str(uuid.uuid4()),
        workspace_root="/flowos/workspaces/agent-001",
        workspace_branch="feature/auth-module",
        workspace_commit_sha="abc123def456" * 3,
        workspace_is_dirty=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTaskContext:
    """Tests for TaskContext data structure."""

    def test_create_minimal_task_context(self):
        """TaskContext can be created with required fields."""
        ctx = _make_task_context()

        assert ctx.task_id is not None
        assert ctx.task_name == "Code Review"
        assert ctx.task_type == "ai"
        assert ctx.task_description is not None

    def test_task_context_defaults(self):
        """TaskContext has sensible defaults for optional fields."""
        ctx = _make_task_context()

        assert ctx.task_priority == "normal"
        assert ctx.task_inputs == {}
        assert ctx.task_tags == []
        assert ctx.recent_commits == []
        assert ctx.changed_files == []
        assert ctx.artifacts == []
        assert ctx.prior_sessions == []

    def test_task_context_with_changed_files(self):
        """TaskContext accepts a list of changed files."""
        changed_files = [
            FileChange(path="src/auth.py", status="modified"),
            FileChange(path="tests/test_auth.py", status="added"),
        ]
        ctx = _make_task_context()
        ctx.changed_files = changed_files

        assert len(ctx.changed_files) == 2
        assert ctx.changed_files[0].path == "src/auth.py"
        assert ctx.changed_files[1].status == "added"

    def test_task_context_with_recent_commits(self):
        """TaskContext accepts a list of recent commits."""
        commits = [
            CommitSummary(
                sha="abc123" * 7,
                short_sha="abc123",
                message="feat: add auth module",
                author="Developer <dev@example.com>",
                committed_at="2026-04-01T10:00:00Z",
            )
        ]
        ctx = _make_task_context()
        ctx.recent_commits = commits

        assert len(ctx.recent_commits) == 1
        assert ctx.recent_commits[0].message == "feat: add auth module"

    def test_task_context_with_artifacts(self):
        """TaskContext accepts a list of artifact references."""
        artifacts = [
            ArtifactRef(
                name="build.log",
                path="/workspace/artifacts/build.log",
                artifact_type="build_log",
                size_bytes=1024,
                created_at="2026-04-01T10:00:00Z",
            )
        ]
        ctx = _make_task_context()
        ctx.artifacts = artifacts

        assert len(ctx.artifacts) == 1
        assert ctx.artifacts[0].artifact_type == "build_log"

    def test_task_context_with_prior_sessions(self):
        """TaskContext accepts prior reasoning session summaries."""
        sessions = [
            ReasoningTraceSummary(
                session_id=str(uuid.uuid4()),
                status="completed",
                step_count=5,
                final_output="Review completed: 3 issues found",
                completed_at="2026-04-01T09:00:00Z",
                model_name="gpt-4",
            )
        ]
        ctx = _make_task_context()
        ctx.prior_sessions = sessions

        assert len(ctx.prior_sessions) == 1
        assert ctx.prior_sessions[0].step_count == 5

    def test_task_context_serialisable_to_dict(self):
        """TaskContext can be serialised to a dict."""
        ctx = _make_task_context()
        data = asdict(ctx)

        assert isinstance(data, dict)
        assert "task_id" in data
        assert "task_name" in data
        assert "workflow_id" in data

    def test_task_context_with_error_context(self):
        """TaskContext accepts error context for retry scenarios."""
        ctx = _make_task_context()
        ctx.error_context = "Previous attempt failed: timeout after 300s"

        assert ctx.error_context is not None
        assert "timeout" in ctx.error_context

    def test_task_context_with_custom_context(self):
        """TaskContext accepts arbitrary custom context."""
        ctx = _make_task_context()
        ctx.custom_context = {
            "review_focus": "security",
            "max_issues": 10,
        }

        assert ctx.custom_context["review_focus"] == "security"


class TestContextLoader:
    """Tests for ContextLoader."""

    def test_context_loader_initialises(self, tmp_path: Path):
        """ContextLoader can be initialised with a workspace root."""
        from ai.context_loader import ContextLoader

        loader = ContextLoader(workspace_root=str(tmp_path))
        assert loader is not None

    def test_context_loader_loads_minimal_context(self, tmp_path: Path):
        """ContextLoader.load() returns a TaskContext."""
        from ai.context_loader import ContextLoader

        loader = ContextLoader(workspace_root=str(tmp_path))
        ctx = loader.load(
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            agent_id=str(uuid.uuid4()),
            task_name="Test Task",
            task_type="ai",
            task_description="Test description",
        )

        assert isinstance(ctx, TaskContext)
        assert ctx.task_name == "Test Task"
        assert ctx.task_type == "ai"

    def test_context_loader_with_workspace_manager(self, tmp_path: Path):
        """ContextLoader works with an initialised workspace."""
        from ai.context_loader import ContextLoader
        from cli.workspace_manager import WorkspaceManager

        # Set up a real workspace
        manager = WorkspaceManager(root=str(tmp_path / "workspace"))
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)

        # Create a file and checkpoint
        test_file = manager.repo_dir / "auth.py"
        test_file.write_text("# Auth module\ndef authenticate(user, password): pass")
        manager.checkpoint("feat: add auth module", emit_event=False)

        loader = ContextLoader(workspace_root=str(tmp_path / "workspace"))
        ctx = loader.load(
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            agent_id=agent_id,
            task_name="Code Review",
            task_type="ai",
            task_description="Review the auth module",
        )

        assert isinstance(ctx, TaskContext)
        assert ctx.workspace_branch is not None


class TestAIEventEmission:
    """Tests for AI event emission."""

    def test_ai_suggestion_created_event_structure(self):
        """AI_SUGGESTION_CREATED event has correct structure."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        suggestion_id = str(uuid.uuid4())

        payload = AISuggestionCreatedPayload(
            suggestion_id=suggestion_id,
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id="ai-agent-001",
            suggestion_type="code_review",
            content="SQL injection risk in auth.py line 42",
            confidence=0.95,
        )
        event = build_event(
            event_type=EventType.AI_SUGGESTION_CREATED,
            payload=payload,
            source=EventSource.AI_AGENT,
            workflow_id=workflow_id,
            task_id=task_id,
        )

        assert event.event_type == EventType.AI_SUGGESTION_CREATED
        assert event.topic == EventTopic.AI_EVENTS
        assert event.payload["suggestion_id"] == suggestion_id
        assert event.payload["suggestion_type"] == "code_review"
        assert event.payload["confidence"] == pytest.approx(0.95)

    def test_ai_review_completed_event_structure(self):
        """AI_REVIEW_COMPLETED event has correct structure."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())

        payload = AIReviewCompletedPayload(
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id="ai-agent-001",
            review_outcome="needs_changes",
            summary="Found 3 security issues and 2 style violations",
            findings=[{"type": "security", "severity": "high", "message": "SQL injection"}],
        )
        event = build_event(
            event_type=EventType.AI_REVIEW_COMPLETED,
            payload=payload,
            source=EventSource.AI_AGENT,
            workflow_id=workflow_id,
            task_id=task_id,
        )

        assert event.event_type == EventType.AI_REVIEW_COMPLETED
        assert event.payload["review_outcome"] == "needs_changes"
        assert len(event.payload["findings"]) == 1

    def test_ai_events_roundtrip(self):
        """AI events survive serialisation/deserialisation."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        suggestion_id = str(uuid.uuid4())

        payload = AISuggestionCreatedPayload(
            suggestion_id=suggestion_id,
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id="ai-agent-001",
            suggestion_type="code_review",
            content="Test description",
            confidence=0.8,
        )
        event = build_event(
            event_type=EventType.AI_SUGGESTION_CREATED,
            payload=payload,
            source=EventSource.AI_AGENT,
        )

        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_type == EventType.AI_SUGGESTION_CREATED
        assert restored.payload["suggestion_id"] == suggestion_id
        assert restored.payload["confidence"] == pytest.approx(0.8)


class TestCodeReviewGraph:
    """Tests for the code review reasoning graph structure."""

    def test_code_review_state_structure(self):
        """CodeReviewState TypedDict has the expected keys."""
        from ai.graphs.code_review import CodeReviewState

        # Verify the TypedDict has the expected keys
        annotations = CodeReviewState.__annotations__
        assert "task_context" in annotations
        assert "task_id" in annotations
        assert "workflow_id" in annotations
        assert "agent_id" in annotations
        assert "messages" in annotations
        assert "review_findings" in annotations
        assert "review_outcome" in annotations
        assert "review_summary" in annotations

    def test_build_code_review_graph_returns_graph(self):
        """build_code_review_graph() returns a compiled graph."""
        from ai.graphs.code_review import build_code_review_graph

        mock_llm = MagicMock()
        graph = build_code_review_graph(llm=mock_llm, workspace_root="/tmp/workspace")

        assert graph is not None

    def test_fix_validation_state_structure(self):
        """FixValidationState TypedDict has the expected keys."""
        from ai.graphs.fix_validation import FixValidationState

        annotations = FixValidationState.__annotations__
        assert "task_context" in annotations
        assert "task_id" in annotations
        assert "workflow_id" in annotations
        assert "validation_result" in annotations
