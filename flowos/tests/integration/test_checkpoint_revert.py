"""
tests/integration/test_checkpoint_revert.py — Integration tests for checkpoint/revert.

Tests the complete checkpoint creation and revert lifecycle using the
WorkspaceManager and LocalGitOps, exercising real Git operations.

Tests cover:
- Creating checkpoints at different task progress levels
- Reverting to a specific checkpoint
- Multiple checkpoints in sequence
- Checkpoint metadata (type, progress, notes)
- Revert restores exact file state
- Checkpoint records persisted to .flowos/checkpoints.json
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from cli.workspace_manager import WorkspaceManager
from shared.models.checkpoint import Checkpoint, CheckpointStatus, CheckpointType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_manager(tmp_path: Path) -> WorkspaceManager:
    """Return an initialised WorkspaceManager."""
    manager = WorkspaceManager(root=str(tmp_path / "workspace"))
    agent_id = str(uuid.uuid4())
    manager.init(agent_id=agent_id, task_id=str(uuid.uuid4()), emit_event=False)
    return manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckpointCreation:
    """Tests for checkpoint creation."""

    def test_create_checkpoint_returns_checkpoint_model(self, workspace_manager: WorkspaceManager):
        """checkpoint() returns a Checkpoint domain model."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("initial content")

        ckpt = workspace_manager.checkpoint("Initial checkpoint", emit_event=False)

        assert isinstance(ckpt, Checkpoint)
        assert ckpt.status == CheckpointStatus.COMMITTED
        assert ckpt.git_commit_sha is not None

    def test_checkpoint_has_valid_id(self, workspace_manager: WorkspaceManager):
        """Checkpoint has a valid UUID as checkpoint_id."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint("Test checkpoint", emit_event=False)

        uuid.UUID(ckpt.checkpoint_id)

    def test_checkpoint_stores_message(self, workspace_manager: WorkspaceManager):
        """Checkpoint stores the commit message."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint("feat: add authentication", emit_event=False)

        assert ckpt.message == "feat: add authentication"

    def test_checkpoint_with_task_progress(self, workspace_manager: WorkspaceManager):
        """Checkpoint stores task progress percentage."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint(
            "50% complete",
            task_progress=50.0,
            emit_event=False,
        )

        assert ckpt.task_progress == 50.0

    def test_checkpoint_with_notes(self, workspace_manager: WorkspaceManager):
        """Checkpoint stores agent notes."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint(
            "Checkpoint with notes",
            notes="Implemented auth module, need to add tests",
            emit_event=False,
        )

        assert ckpt.notes == "Implemented auth module, need to add tests"

    def test_checkpoint_type_manual(self, workspace_manager: WorkspaceManager):
        """Default checkpoint type is MANUAL."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint("Manual checkpoint", emit_event=False)

        assert ckpt.checkpoint_type == CheckpointType.MANUAL

    def test_checkpoint_type_pre_handoff(self, workspace_manager: WorkspaceManager):
        """Checkpoint type can be set to PRE_HANDOFF."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint(
            "Pre-handoff checkpoint",
            checkpoint_type=CheckpointType.PRE_HANDOFF,
            emit_event=False,
        )

        assert ckpt.checkpoint_type == CheckpointType.PRE_HANDOFF

    def test_checkpoint_git_sha_is_valid_hex(self, workspace_manager: WorkspaceManager):
        """Checkpoint git_commit_sha is a valid hex string."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint("Test", emit_event=False)

        assert all(c in "0123456789abcdef" for c in ckpt.git_commit_sha.lower())
        assert len(ckpt.git_commit_sha) == 40

    def test_checkpoint_sequence_increments(self, workspace_manager: WorkspaceManager):
        """Checkpoint sequence numbers increment with each checkpoint."""
        for i in range(3):
            f = workspace_manager.repo_dir / f"file{i}.txt"
            f.write_text(f"content {i}")
            ckpt = workspace_manager.checkpoint(f"Checkpoint {i}", emit_event=False)
            assert ckpt.sequence == i + 1

    def test_checkpoint_persisted_to_json(self, workspace_manager: WorkspaceManager):
        """Checkpoint is persisted to .flowos/checkpoints.json."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = workspace_manager.checkpoint("Persisted checkpoint", emit_event=False)

        checkpoints_file = workspace_manager.flowos_dir / "checkpoints.json"
        checkpoints = json.loads(checkpoints_file.read_text())
        checkpoint_ids = [c["checkpoint_id"] for c in checkpoints]
        assert ckpt.checkpoint_id in checkpoint_ids


class TestMultipleCheckpoints:
    """Tests for creating multiple checkpoints in sequence."""

    def test_multiple_checkpoints_create_history(self, workspace_manager: WorkspaceManager):
        """Multiple checkpoints create a history in checkpoints.json."""
        for i in range(3):
            f = workspace_manager.repo_dir / f"file{i}.txt"
            f.write_text(f"version {i}")
            workspace_manager.checkpoint(f"Checkpoint {i}", emit_event=False)

        checkpoints_file = workspace_manager.flowos_dir / "checkpoints.json"
        checkpoints = json.loads(checkpoints_file.read_text())
        assert len(checkpoints) == 3

    def test_checkpoints_have_different_shas(self, workspace_manager: WorkspaceManager):
        """Each checkpoint has a unique git_commit_sha."""
        shas = []
        for i in range(3):
            f = workspace_manager.repo_dir / f"file{i}.txt"
            f.write_text(f"content {i}")
            ckpt = workspace_manager.checkpoint(f"Checkpoint {i}", emit_event=False)
            shas.append(ckpt.git_commit_sha)

        assert len(set(shas)) == 3  # All unique

    def test_checkpoints_have_different_ids(self, workspace_manager: WorkspaceManager):
        """Each checkpoint has a unique checkpoint_id."""
        ids = []
        for i in range(3):
            f = workspace_manager.repo_dir / f"file{i}.txt"
            f.write_text(f"content {i}")
            ckpt = workspace_manager.checkpoint(f"Checkpoint {i}", emit_event=False)
            ids.append(ckpt.checkpoint_id)

        assert len(set(ids)) == 3  # All unique


class TestCheckpointRevert:
    """Tests for reverting to a checkpoint."""

    def test_revert_restores_file_content(self, workspace_manager: WorkspaceManager):
        """revert_to_checkpoint() restores the exact file content from the checkpoint."""
        # Create initial state
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("version 1")
        ckpt_v1 = workspace_manager.checkpoint("v1", emit_event=False)

        # Modify and create second checkpoint
        test_file.write_text("version 2")
        workspace_manager.checkpoint("v2", emit_event=False)

        # Revert to v1
        workspace_manager.revert_to_checkpoint(
            checkpoint_id=ckpt_v1.checkpoint_id,
            emit_event=False,
        )

        assert test_file.read_text() == "version 1"

    def test_revert_removes_files_added_after_checkpoint(self, workspace_manager: WorkspaceManager):
        """revert_to_checkpoint() removes files that were added after the checkpoint."""
        # Create initial state
        test_file = workspace_manager.repo_dir / "original.txt"
        test_file.write_text("original")
        ckpt = workspace_manager.checkpoint("initial", emit_event=False)

        # Add a new file and checkpoint
        new_file = workspace_manager.repo_dir / "new_file.txt"
        new_file.write_text("new content")
        workspace_manager.checkpoint("added new file", emit_event=False)

        # Revert to initial checkpoint
        workspace_manager.revert_to_checkpoint(
            checkpoint_id=ckpt.checkpoint_id,
            emit_event=False,
        )

        # The new file should be gone
        assert not new_file.exists()
        assert test_file.read_text() == "original"

    def test_revert_to_nonexistent_checkpoint_raises(self, workspace_manager: WorkspaceManager):
        """revert_to_checkpoint() raises KeyError for a nonexistent checkpoint_id."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("content")
        workspace_manager.checkpoint("initial", emit_event=False)

        with pytest.raises(KeyError):
            workspace_manager.revert_to_checkpoint(
                checkpoint_id=str(uuid.uuid4()),  # Random non-existent ID
                emit_event=False,
            )

    def test_revert_to_middle_checkpoint(self, workspace_manager: WorkspaceManager):
        """revert_to_checkpoint() can revert to any checkpoint in the history."""
        # Create three checkpoints
        test_file = workspace_manager.repo_dir / "test.txt"

        test_file.write_text("v1")
        workspace_manager.checkpoint("v1", emit_event=False)

        test_file.write_text("v2")
        ckpt_v2 = workspace_manager.checkpoint("v2", emit_event=False)

        test_file.write_text("v3")
        workspace_manager.checkpoint("v3", emit_event=False)

        # Revert to v2 (middle checkpoint)
        workspace_manager.revert_to_checkpoint(
            checkpoint_id=ckpt_v2.checkpoint_id,
            emit_event=False,
        )

        assert test_file.read_text() == "v2"

    def test_revert_returns_checkpoint_model(self, workspace_manager: WorkspaceManager):
        """revert_to_checkpoint() returns the Checkpoint that was reverted to."""
        test_file = workspace_manager.repo_dir / "test.txt"
        test_file.write_text("v1")
        ckpt = workspace_manager.checkpoint("v1", emit_event=False)

        test_file.write_text("v2")
        workspace_manager.checkpoint("v2", emit_event=False)

        reverted = workspace_manager.revert_to_checkpoint(
            checkpoint_id=ckpt.checkpoint_id,
            emit_event=False,
        )

        assert isinstance(reverted, Checkpoint)
        assert reverted.checkpoint_id == ckpt.checkpoint_id


class TestCheckpointWithKafkaEvents:
    """Tests for checkpoint Kafka event emission."""

    def test_checkpoint_emits_event_when_producer_provided(self, tmp_path: Path):
        """checkpoint() emits a CHECKPOINT_CREATED event when producer is provided."""
        mock_producer = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        manager = WorkspaceManager(root=str(tmp_path / "ws"), producer=mock_producer)
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)

        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("content")

        manager.checkpoint("Test checkpoint", emit_event=True)

        # Producer should have been called
        assert mock_producer.produce.call_count >= 1

    def test_revert_emits_event_when_producer_provided(self, tmp_path: Path):
        """revert_to_checkpoint() emits a CHECKPOINT_REVERTED event when producer is provided."""
        mock_producer = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        manager = WorkspaceManager(root=str(tmp_path / "ws"), producer=mock_producer)
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)

        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("v1")
        ckpt = manager.checkpoint("v1", emit_event=False)

        test_file.write_text("v2")
        manager.checkpoint("v2", emit_event=False)

        # Reset call count
        mock_producer.produce.reset_mock()

        manager.revert_to_checkpoint(checkpoint_id=ckpt.checkpoint_id, emit_event=True)

        # Producer should have been called for the revert event
        assert mock_producer.produce.call_count >= 1
