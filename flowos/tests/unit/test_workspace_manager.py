"""
tests/unit/test_workspace_manager.py — Unit tests for FlowOS WorkspaceManager.

Tests cover:
- WorkspaceManager initialisation and properties
- _create_workspace_dirs() directory structure creation
- init() workspace initialisation
- checkpoint() creation
- revert_to_checkpoint() revert logic
- is_initialised property
- Kafka event emission (mocked)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.workspace_manager import WorkspaceManager, _create_workspace_dirs, WORKSPACE_SUBDIRS


class TestCreateWorkspaceDirs:
    """Tests for the _create_workspace_dirs() utility function."""

    def test_creates_all_subdirectories(self, tmp_path: Path):
        """_create_workspace_dirs() creates all required subdirectories."""
        root = str(tmp_path / "workspace")
        dirs = _create_workspace_dirs(root)

        for subdir in WORKSPACE_SUBDIRS:
            assert (tmp_path / "workspace" / subdir).exists()

    def test_creates_flowos_meta_files(self, tmp_path: Path):
        """_create_workspace_dirs() creates .flowos/ metadata files."""
        root = str(tmp_path / "workspace")
        _create_workspace_dirs(root)

        flowos_dir = tmp_path / "workspace" / ".flowos"
        assert (flowos_dir / "workspace.json").exists()
        assert (flowos_dir / "state.json").exists()
        assert (flowos_dir / "checkpoints.json").exists()
        assert (flowos_dir / "config.json").exists()

    def test_creates_gitkeep_files(self, tmp_path: Path):
        """_create_workspace_dirs() creates .gitkeep in artifacts/, logs/, patches/."""
        root = str(tmp_path / "workspace")
        _create_workspace_dirs(root)

        for subdir in ("artifacts", "logs", "patches"):
            assert (tmp_path / "workspace" / subdir / ".gitkeep").exists()

    def test_is_idempotent(self, tmp_path: Path):
        """_create_workspace_dirs() can be called multiple times without error."""
        root = str(tmp_path / "workspace")
        _create_workspace_dirs(root)
        _create_workspace_dirs(root)  # Second call should not raise

        for subdir in WORKSPACE_SUBDIRS:
            assert (tmp_path / "workspace" / subdir).exists()

    def test_returns_dict_with_paths(self, tmp_path: Path):
        """_create_workspace_dirs() returns a dict mapping subdir names to Paths."""
        root = str(tmp_path / "workspace")
        dirs = _create_workspace_dirs(root)

        assert "root" in dirs
        assert isinstance(dirs["root"], Path)
        for subdir in WORKSPACE_SUBDIRS:
            assert subdir in dirs
            assert isinstance(dirs[subdir], Path)

    def test_creates_nested_root_directory(self, tmp_path: Path):
        """_create_workspace_dirs() creates nested root directories."""
        root = str(tmp_path / "deep" / "nested" / "workspace")
        _create_workspace_dirs(root)
        assert (tmp_path / "deep" / "nested" / "workspace").exists()


class TestWorkspaceManagerInit:
    """Tests for WorkspaceManager initialisation."""

    def test_manager_stores_root_path(self, tmp_path: Path):
        """WorkspaceManager stores the root path as an absolute Path."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        assert manager.root == (tmp_path / "ws").resolve()

    def test_manager_properties_point_to_correct_subdirs(self, tmp_path: Path):
        """WorkspaceManager properties point to the correct subdirectories."""
        root = tmp_path / "ws"
        manager = WorkspaceManager(root=str(root))

        assert manager.repo_dir == root.resolve() / "repo"
        assert manager.flowos_dir == root.resolve() / ".flowos"
        assert manager.artifacts_dir == root.resolve() / "artifacts"
        assert manager.logs_dir == root.resolve() / "logs"
        assert manager.patches_dir == root.resolve() / "patches"

    def test_manager_is_not_initialised_before_init(self, tmp_path: Path):
        """WorkspaceManager.is_initialised is False before init() is called."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        assert manager.is_initialised is False

    def test_manager_accepts_custom_author(self, tmp_path: Path):
        """WorkspaceManager accepts custom author name and email."""
        manager = WorkspaceManager(
            root=str(tmp_path / "ws"),
            author_name="Test Author",
            author_email="test@example.com",
        )
        assert manager._author_name == "Test Author"
        assert manager._author_email == "test@example.com"


class TestWorkspaceManagerInitMethod:
    """Tests for WorkspaceManager.init()."""

    def test_init_creates_workspace_structure(self, tmp_path: Path):
        """init() creates the workspace directory structure."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())

        workspace = manager.init(agent_id=agent_id, emit_event=False)

        assert (tmp_path / "ws" / "repo").exists()
        assert (tmp_path / "ws" / ".flowos").exists()
        assert (tmp_path / "ws" / "artifacts").exists()

    def test_init_returns_workspace_model(self, tmp_path: Path):
        """init() returns a Workspace domain model."""
        from shared.models.workspace import Workspace

        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())

        workspace = manager.init(agent_id=agent_id, emit_event=False)

        assert isinstance(workspace, Workspace)
        assert workspace.agent_id == agent_id

    def test_init_marks_workspace_as_initialised(self, tmp_path: Path):
        """After init(), is_initialised returns True."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())

        manager.init(agent_id=agent_id, emit_event=False)

        assert manager.is_initialised is True

    def test_init_is_idempotent(self, tmp_path: Path):
        """Calling init() twice returns the existing workspace without error."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())

        ws1 = manager.init(agent_id=agent_id, emit_event=False)
        ws2 = manager.init(agent_id=agent_id, emit_event=False)

        # Both should return a Workspace with the same workspace_id
        assert ws1.workspace_id == ws2.workspace_id

    def test_init_writes_workspace_json(self, tmp_path: Path):
        """init() writes workspace.json to the .flowos/ directory."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())

        manager.init(agent_id=agent_id, emit_event=False)

        workspace_json = tmp_path / "ws" / ".flowos" / "workspace.json"
        assert workspace_json.exists()
        data = json.loads(workspace_json.read_text())
        assert data["agent_id"] == agent_id

    def test_init_with_task_id(self, tmp_path: Path):
        """init() stores the task_id in the workspace model."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())

        workspace = manager.init(agent_id=agent_id, task_id=task_id, emit_event=False)

        assert workspace.task_id == task_id

    def test_init_emits_kafka_event_when_producer_provided(self, tmp_path: Path):
        """init() emits a WORKSPACE_CREATED Kafka event when a producer is provided."""
        mock_producer = MagicMock()
        manager = WorkspaceManager(root=str(tmp_path / "ws"), producer=mock_producer)
        agent_id = str(uuid.uuid4())

        manager.init(agent_id=agent_id, emit_event=True)

        mock_producer.produce.assert_called_once()


class TestWorkspaceManagerCheckpoint:
    """Tests for WorkspaceManager.checkpoint()."""

    def _init_manager(self, tmp_path: Path) -> WorkspaceManager:
        """Helper to create and initialise a workspace manager."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)
        return manager

    def test_checkpoint_creates_git_commit(self, tmp_path: Path):
        """checkpoint() creates a Git commit in the workspace repo."""
        from shared.models.checkpoint import Checkpoint
        manager = self._init_manager(tmp_path)

        # Write a file to the repo
        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("hello world")

        ckpt = manager.checkpoint("test: initial commit", emit_event=False)

        assert isinstance(ckpt, Checkpoint)
        assert ckpt.git_commit_sha is not None
        assert len(ckpt.git_commit_sha) > 0

    def test_checkpoint_returns_checkpoint_model(self, tmp_path: Path):
        """checkpoint() returns a Checkpoint domain model with a git_commit_sha."""
        from shared.models.checkpoint import Checkpoint
        manager = self._init_manager(tmp_path)

        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("content")

        ckpt = manager.checkpoint("test: checkpoint", emit_event=False)

        assert isinstance(ckpt, Checkpoint)
        # git_commit_sha should be a hex string
        assert all(c in "0123456789abcdef" for c in ckpt.git_commit_sha.lower())

    def test_checkpoint_records_in_checkpoints_json(self, tmp_path: Path):
        """checkpoint() records the checkpoint in .flowos/checkpoints.json."""
        manager = self._init_manager(tmp_path)

        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("content")

        manager.checkpoint("test: checkpoint", emit_event=False)

        checkpoints_file = manager.flowos_dir / "checkpoints.json"
        checkpoints = json.loads(checkpoints_file.read_text())
        assert len(checkpoints) >= 1

    def test_checkpoint_emits_kafka_event_when_producer_provided(self, tmp_path: Path):
        """checkpoint() emits a CHECKPOINT_CREATED event when a producer is provided."""
        mock_producer = MagicMock()
        manager = WorkspaceManager(root=str(tmp_path / "ws"), producer=mock_producer)
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)

        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("content")

        manager.checkpoint("test: checkpoint", emit_event=True)

        # Producer should have been called for the checkpoint event
        assert mock_producer.produce.call_count >= 1


class TestWorkspaceManagerRevert:
    """Tests for WorkspaceManager.revert_to_checkpoint()."""

    def _init_manager_with_checkpoint(self, tmp_path: Path):
        """Helper to create a workspace with a checkpoint."""
        manager = WorkspaceManager(root=str(tmp_path / "ws"))
        agent_id = str(uuid.uuid4())
        manager.init(agent_id=agent_id, emit_event=False)

        # Create initial file and checkpoint
        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("version 1")
        ckpt = manager.checkpoint("v1", emit_event=False)

        return manager, ckpt

    def test_revert_restores_workspace_to_checkpoint(self, tmp_path: Path):
        """revert_to_checkpoint() restores the workspace to the checkpoint state."""
        manager, ckpt = self._init_manager_with_checkpoint(tmp_path)

        # Modify the file after checkpoint
        test_file = manager.repo_dir / "test.txt"
        test_file.write_text("version 2")
        manager.checkpoint("v2", emit_event=False)

        # Revert to the first checkpoint using its checkpoint_id
        manager.revert_to_checkpoint(checkpoint_id=ckpt.checkpoint_id, emit_event=False)

        # The file should be back to version 1
        assert test_file.read_text() == "version 1"
