"""
cli/workspace_manager.py — FlowOS Workspace Manager

Provides the ``WorkspaceManager`` class, which orchestrates the full lifecycle
of a FlowOS agent workspace: initialisation, Git operations, checkpointing,
handoff preparation, and Kafka event emission.

A workspace is the local, reversible state boundary for an agent.  It consists
of:
  - A Git repository (``repo/``) for versioned file tracking
  - A ``.flowos/`` metadata directory for workspace state files
  - An ``artifacts/`` directory for build outputs and generated files
  - A ``logs/`` directory for execution logs
  - A ``patches/`` directory for AI-proposed and applied patches

Workspace lifecycle::

    manager = WorkspaceManager(root="/flowos/workspaces/agent-abc")

    # Initialise a fresh workspace
    workspace = manager.init(agent_id="agent-abc", task_id="task-xyz")

    # Create a checkpoint
    sha = manager.checkpoint("feat: implemented auth module", task_progress=50.0)

    # Prepare a handoff
    manager.prepare_handoff(handoff_id="hoff-123", to_agent_id="agent-def")

    # Revert to a previous checkpoint
    manager.revert_to_checkpoint(checkpoint_id="ckpt-456")

    # Archive the workspace
    manager.archive()

Kafka events emitted:
  - WORKSPACE_CREATED  — on init()
  - CHECKPOINT_CREATED — on checkpoint()
  - CHECKPOINT_REVERTED — on revert_to_checkpoint()
  - BRANCH_CREATED     — on create_task_branch()
  - HANDOFF_PREPARED   — on prepare_handoff()
  - SNAPSHOT_CREATED   — on create_snapshot()
  - WORKSPACE_SYNCED   — on sync()
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.git_ops import (
    CommitInfo,
    GitOpsError,
    LocalGitOps,
    RepoState,
)
from shared.models.checkpoint import Checkpoint, CheckpointStatus, CheckpointType
from shared.models.workspace import (
    GitState,
    Workspace,
    WorkspaceSnapshot,
    WorkspaceStatus,
    WorkspaceType,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: Sub-directories created inside every workspace root.
WORKSPACE_SUBDIRS = ("repo", ".flowos", "artifacts", "logs", "patches")

#: Files created inside ``.flowos/`` on workspace initialisation.
FLOWOS_META_FILES = {
    "workspace.json": None,   # populated with Workspace model JSON
    "state.json": None,       # mutable runtime state
    "checkpoints.json": [],   # list of checkpoint records
    "config.json": {},        # workspace-local config overrides
}

#: Default Git branch for new workspaces.
DEFAULT_BRANCH = "main"


# ─────────────────────────────────────────────────────────────────────────────
# Directory initialiser (standalone utility)
# ─────────────────────────────────────────────────────────────────────────────


def _create_workspace_dirs(root: str) -> dict[str, Path]:
    """
    Create the standard FlowOS workspace directory structure.

    Creates the following layout under ``root``::

        <root>/
        ├── repo/           ← Git repository
        ├── .flowos/        ← FlowOS metadata (workspace.json, state.json, …)
        ├── artifacts/      ← Build outputs, generated files
        ├── logs/           ← Execution logs
        └── patches/        ← AI-proposed and applied patches

    This function is idempotent — calling it on an existing workspace will not
    overwrite existing files.

    Args:
        root: Absolute or relative path to the workspace root directory.

    Returns:
        A dict mapping sub-directory name → absolute ``Path`` object for each
        created directory.

    Raises:
        OSError: If any directory cannot be created due to permissions or
                 filesystem errors.
    """
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)

    created: dict[str, Path] = {"root": root_path}

    for subdir in WORKSPACE_SUBDIRS:
        subdir_path = root_path / subdir
        subdir_path.mkdir(parents=True, exist_ok=True)
        created[subdir] = subdir_path
        logger.debug(
            "_create_workspace_dirs: ensured directory | path=%s", subdir_path
        )

    # Populate .flowos/ with skeleton metadata files (only if they don't exist)
    flowos_dir = root_path / ".flowos"
    for filename, default_content in FLOWOS_META_FILES.items():
        meta_file = flowos_dir / filename
        if not meta_file.exists():
            if default_content is None:
                meta_file.write_text("{}", encoding="utf-8")
            else:
                meta_file.write_text(
                    json.dumps(default_content, indent=2),
                    encoding="utf-8",
                )
            logger.debug(
                "_create_workspace_dirs: created meta file | path=%s", meta_file
            )

    # Create a .gitkeep in artifacts/, logs/, patches/ so they are tracked by Git
    for subdir in ("artifacts", "logs", "patches"):
        gitkeep = root_path / subdir / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()

    logger.info(
        "_create_workspace_dirs: workspace structure ready | root=%s", root_path
    )
    return created


# ─────────────────────────────────────────────────────────────────────────────
# WorkspaceManager
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceManager:
    """
    Orchestrates the full lifecycle of a FlowOS agent workspace.

    Combines directory management, Git operations (via ``LocalGitOps``), domain
    model persistence (``Workspace``, ``Checkpoint``), and Kafka event emission
    into a single cohesive interface.

    The manager is intentionally synchronous — it is called from Click CLI
    commands and Temporal activities, both of which run in thread pools.

    Args:
        root:         Absolute path to the workspace root directory.
        author_name:  Git author name for commits.
        author_email: Git author email for commits.
        producer:     Optional ``FlowOSProducer`` instance for Kafka events.
                      If None, Kafka events are logged but not emitted.
    """

    def __init__(
        self,
        root: str,
        author_name: str = LocalGitOps.DEFAULT_AUTHOR_NAME,
        author_email: str = LocalGitOps.DEFAULT_AUTHOR_EMAIL,
        producer: Any | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._author_name = author_name
        self._author_email = author_email
        self._producer = producer

        # Paths to key sub-directories
        self._repo_dir = self._root / "repo"
        self._flowos_dir = self._root / ".flowos"
        self._artifacts_dir = self._root / "artifacts"
        self._logs_dir = self._root / "logs"
        self._patches_dir = self._root / "patches"

        # Lazy-initialised Git ops wrapper
        self._git: LocalGitOps | None = None

        # In-memory workspace model (loaded from disk on first access)
        self._workspace: Workspace | None = None

    # ─────────────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        """Absolute path to the workspace root."""
        return self._root

    @property
    def repo_dir(self) -> Path:
        """Absolute path to the Git repository sub-directory."""
        return self._repo_dir

    @property
    def flowos_dir(self) -> Path:
        """Absolute path to the ``.flowos/`` metadata directory."""
        return self._flowos_dir

    @property
    def artifacts_dir(self) -> Path:
        """Absolute path to the ``artifacts/`` directory."""
        return self._artifacts_dir

    @property
    def logs_dir(self) -> Path:
        """Absolute path to the ``logs/`` directory."""
        return self._logs_dir

    @property
    def patches_dir(self) -> Path:
        """Absolute path to the ``patches/`` directory."""
        return self._patches_dir

    @property
    def git(self) -> LocalGitOps:
        """
        Return the ``LocalGitOps`` instance for this workspace.

        Lazily initialised on first access.
        """
        if self._git is None:
            self._git = LocalGitOps(
                str(self._repo_dir),
                author_name=self._author_name,
                author_email=self._author_email,
            )
        return self._git

    @property
    def is_initialised(self) -> bool:
        """Return True if the workspace has been initialised."""
        return (
            self._flowos_dir.exists()
            and (self._flowos_dir / "workspace.json").exists()
            and self.git.is_initialised
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def init(
        self,
        agent_id: str,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workspace_id: str | None = None,
        branch: str = DEFAULT_BRANCH,
        repo_url: str | None = None,
        workspace_type: WorkspaceType = WorkspaceType.LOCAL,
        branch_prefix: str = "flowos",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        emit_event: bool = True,
    ) -> Workspace:
        """
        Initialise a new workspace for an agent.

        Creates the directory structure, initialises the Git repository,
        writes the initial ``.flowos/workspace.json``, and emits a
        ``WORKSPACE_CREATED`` Kafka event.

        If the workspace already exists (idempotent), loads and returns the
        existing ``Workspace`` model without re-initialising.

        Args:
            agent_id:       Agent that owns this workspace.
            task_id:        Task this workspace is being created for.
            workflow_id:    Workflow this workspace is associated with.
            workspace_id:   Explicit workspace ID.  Auto-generated if None.
            branch:         Initial Git branch name.
            repo_url:       Remote Git repository URL to clone from.  If None,
                            a fresh local repo is initialised.
            workspace_type: Kind of workspace.
            branch_prefix:  Prefix for task branches (e.g. 'flowos').
            tags:           Arbitrary tags for filtering.
            metadata:       Arbitrary key/value metadata.
            emit_event:     If True, emit a WORKSPACE_CREATED Kafka event.

        Returns:
            The initialised ``Workspace`` domain model.

        Raises:
            GitOpsError: If Git initialisation or cloning fails.
            OSError: If directory creation fails.
        """
        # Idempotency: if already initialised, return existing workspace
        if self.is_initialised:
            logger.debug(
                "init: workspace already initialised, loading existing | root=%s",
                self._root,
            )
            return self._load_workspace()

        # 1. Create directory structure
        _create_workspace_dirs(str(self._root))

        # 2. Initialise or clone the Git repository
        if repo_url:
            self.git.clone_repo(repo_url, branch=branch)
        else:
            self.git.init_repo(initial_branch=branch)

        # 3. Create the initial commit (so HEAD is valid)
        readme_path = self._repo_dir / "README.md"
        if not readme_path.exists():
            readme_path.write_text(
                f"# FlowOS Workspace\n\nAgent: {agent_id}\n"
                f"Created: {datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
        self.git.stage_all()
        try:
            initial_sha = self.git.commit("chore: initialise workspace")
        except GitOpsError:
            # Nothing to commit (e.g. cloned repo already has commits)
            initial_sha = None

        # 4. Build the Workspace domain model
        ws_id = workspace_id or str(uuid.uuid4())
        git_state = self._build_git_state()

        workspace = Workspace(
            workspace_id=ws_id,
            agent_id=agent_id,
            task_id=task_id,
            workflow_id=workflow_id,
            workspace_type=workspace_type,
            status=WorkspaceStatus.READY,
            root_path=str(self._root),
            git_state=git_state,
            repo_url=repo_url,
            branch_prefix=branch_prefix,
            tags=tags or [],
            metadata=metadata or {},
        )

        # 5. Persist workspace model to .flowos/workspace.json
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "init: workspace initialised | workspace_id=%s agent_id=%s root=%s",
            ws_id,
            agent_id,
            self._root,
        )

        # 6. Emit Kafka event
        if emit_event:
            self._emit_workspace_created(workspace)

        return workspace

    def load(self) -> Workspace:
        """
        Load an existing workspace from disk.

        Returns:
            The ``Workspace`` domain model loaded from ``.flowos/workspace.json``.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            ValueError: If the workspace.json is malformed.
        """
        return self._load_workspace()

    # ─────────────────────────────────────────────────────────────────────────
    # Checkpointing
    # ─────────────────────────────────────────────────────────────────────────

    def checkpoint(
        self,
        message: str,
        checkpoint_type: CheckpointType = CheckpointType.MANUAL,
        task_progress: float | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        emit_event: bool = True,
    ) -> Checkpoint:
        """
        Stage all changes and create a Git commit checkpoint.

        Stages all working tree changes, commits them, records the checkpoint
        in ``.flowos/checkpoints.json``, and emits a ``CHECKPOINT_CREATED``
        Kafka event.

        Args:
            message:         Human-readable checkpoint description.
            checkpoint_type: Why this checkpoint is being created.
            task_progress:   Estimated task completion percentage (0–100).
            notes:           Free-form notes from the agent.
            tags:            Arbitrary tags for filtering.
            metadata:        Arbitrary key/value metadata.
            emit_event:      If True, emit a CHECKPOINT_CREATED Kafka event.

        Returns:
            The ``Checkpoint`` domain model for the new checkpoint.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            GitOpsError: If staging or committing fails.
        """
        workspace = self._load_workspace()

        # Stage + commit
        sha = self.git.checkpoint(message)

        # Build checkpoint model
        git_state = self._build_git_state()
        checkpoint_id = str(uuid.uuid4())
        sequence = self._next_checkpoint_sequence()

        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            task_id=workspace.task_id or str(uuid.uuid4()),
            workflow_id=workspace.workflow_id or str(uuid.uuid4()),
            agent_id=workspace.agent_id,
            workspace_id=workspace.workspace_id,
            checkpoint_type=checkpoint_type,
            status=CheckpointStatus.COMMITTED,
            sequence=sequence,
            git_commit_sha=sha,
            git_branch=git_state.branch,
            message=message,
            task_progress=task_progress,
            notes=notes,
            tags=tags or [],
            metadata=metadata or {},
        )

        # Persist checkpoint record
        self._append_checkpoint(checkpoint)

        # Update workspace git state
        workspace = workspace.model_copy(
            update={
                "git_state": git_state,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "checkpoint: created checkpoint | id=%s sha=%s seq=%d path=%s",
            checkpoint_id,
            sha[:8],
            sequence,
            self._root,
        )

        # Emit Kafka event
        if emit_event:
            self._emit_checkpoint_created(checkpoint)

        return checkpoint

    def revert_to_checkpoint(
        self,
        checkpoint_id: str,
        emit_event: bool = True,
    ) -> Checkpoint:
        """
        Revert the workspace to a specific checkpoint.

        Performs a hard Git reset to the checkpoint's commit SHA, updates the
        workspace state, and emits a ``CHECKPOINT_REVERTED`` Kafka event.

        Args:
            checkpoint_id: ID of the checkpoint to revert to.
            emit_event:    If True, emit a CHECKPOINT_REVERTED Kafka event.

        Returns:
            The ``Checkpoint`` that was reverted to.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            KeyError: If the checkpoint_id is not found.
            GitOpsError: If the Git reset fails.
        """
        workspace = self._load_workspace()
        checkpoints = self._load_checkpoints()

        # Find the target checkpoint
        target: Checkpoint | None = None
        for ckpt in checkpoints:
            if ckpt.checkpoint_id == checkpoint_id:
                target = ckpt
                break

        if target is None:
            raise KeyError(
                f"Checkpoint {checkpoint_id!r} not found in workspace {self._root}."
            )

        if target.git_commit_sha is None:
            raise GitOpsError(
                f"Checkpoint {checkpoint_id!r} has no git_commit_sha.",
                repo_path=str(self._repo_dir),
            )

        # Hard reset to the checkpoint commit
        self.git.revert_to(target.git_commit_sha, hard=True)

        # Update checkpoint status
        reverted_checkpoint = target.model_copy(
            update={
                "status": CheckpointStatus.REVERTED,
                "reverted_at": datetime.now(timezone.utc),
            }
        )

        # Persist updated checkpoint list
        updated_checkpoints = [
            reverted_checkpoint if c.checkpoint_id == checkpoint_id else c
            for c in checkpoints
        ]
        self._save_checkpoints(updated_checkpoints)

        # Update workspace git state
        git_state = self._build_git_state()
        workspace = workspace.model_copy(
            update={
                "git_state": git_state,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "revert_to_checkpoint: reverted | id=%s sha=%s path=%s",
            checkpoint_id,
            target.git_commit_sha[:8],
            self._root,
        )

        # Emit Kafka event
        if emit_event:
            self._emit_checkpoint_reverted(reverted_checkpoint, workspace)

        return reverted_checkpoint

    def list_checkpoints(self) -> list[Checkpoint]:
        """
        Return all checkpoints for this workspace, ordered by sequence number.

        Returns:
            List of ``Checkpoint`` objects, oldest first.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
        """
        return sorted(self._load_checkpoints(), key=lambda c: c.sequence)

    # ─────────────────────────────────────────────────────────────────────────
    # Branch management
    # ─────────────────────────────────────────────────────────────────────────

    def create_task_branch(
        self,
        task_id: str,
        base_ref: str | None = None,
        emit_event: bool = True,
    ) -> str:
        """
        Create a task-scoped Git branch following the FlowOS naming convention.

        Branch name format: ``<branch_prefix>/task/<short_task_id>``

        Args:
            task_id:   Task ID to create the branch for.
            base_ref:  Base commit or branch.  Defaults to HEAD.
            emit_event: If True, emit a BRANCH_CREATED Kafka event.

        Returns:
            The name of the created branch.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            GitOpsError: If branch creation fails.
        """
        workspace = self._load_workspace()
        branch_name = workspace.task_branch_name(task_id)
        base_sha = self.git.create_branch(branch_name, base_ref=base_ref, checkout=True)

        # Update workspace git state
        git_state = self._build_git_state()
        workspace = workspace.model_copy(
            update={
                "git_state": git_state,
                "task_id": task_id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "create_task_branch: created branch | name=%s base=%s path=%s",
            branch_name,
            base_sha[:8] if base_sha else "HEAD",
            self._root,
        )

        if emit_event:
            self._emit_branch_created(
                workspace=workspace,
                branch_name=branch_name,
                base_sha=base_sha,
            )

        return branch_name

    # ─────────────────────────────────────────────────────────────────────────
    # Handoff preparation
    # ─────────────────────────────────────────────────────────────────────────

    def prepare_handoff(
        self,
        handoff_id: str,
        to_agent_id: str,
        message: str | None = None,
        emit_event: bool = True,
    ) -> Checkpoint:
        """
        Prepare the workspace for a handoff to another agent.

        Creates a ``PRE_HANDOFF`` checkpoint to capture the current state,
        then emits a ``HANDOFF_PREPARED`` Kafka event so the target agent can
        clone/sync the workspace.

        Args:
            handoff_id:  ID of the handoff record.
            to_agent_id: Agent receiving the handoff.
            message:     Optional checkpoint message.  Defaults to a standard
                         handoff message.
            emit_event:  If True, emit a HANDOFF_PREPARED Kafka event.

        Returns:
            The ``PRE_HANDOFF`` checkpoint created.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            GitOpsError: If the checkpoint commit fails.
        """
        workspace = self._load_workspace()
        commit_message = message or f"handoff: prepare workspace for {to_agent_id}"

        # Create a PRE_HANDOFF checkpoint
        ckpt = self.checkpoint(
            message=commit_message,
            checkpoint_type=CheckpointType.PRE_HANDOFF,
            emit_event=False,  # We'll emit HANDOFF_PREPARED instead
        )

        logger.info(
            "prepare_handoff: prepared handoff | handoff_id=%s to=%s sha=%s path=%s",
            handoff_id,
            to_agent_id,
            ckpt.git_commit_sha[:8] if ckpt.git_commit_sha else "?",
            self._root,
        )

        if emit_event:
            self._emit_handoff_prepared(
                handoff_id=handoff_id,
                checkpoint=ckpt,
                workspace=workspace,
                to_agent_id=to_agent_id,
            )

        return ckpt

    # ─────────────────────────────────────────────────────────────────────────
    # Snapshots
    # ─────────────────────────────────────────────────────────────────────────

    def create_snapshot(
        self,
        description: str | None = None,
        snapshot_type: str = "manual",
        emit_event: bool = True,
    ) -> WorkspaceSnapshot:
        """
        Create a point-in-time snapshot of the workspace.

        A snapshot is a lightweight record referencing the current HEAD commit.
        It does NOT create a new commit — use ``checkpoint()`` for that.

        Args:
            description:   Human-readable description of why this snapshot was taken.
            snapshot_type: Type label (e.g. 'checkpoint', 'handoff', 'backup').
            emit_event:    If True, emit a SNAPSHOT_CREATED Kafka event.

        Returns:
            The ``WorkspaceSnapshot`` domain model.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            GitOpsError: If the repository has no commits yet.
        """
        workspace = self._load_workspace()
        state = self.git.get_state()

        if state.commit_sha is None:
            raise GitOpsError(
                "Cannot create snapshot: repository has no commits.",
                repo_path=str(self._repo_dir),
            )

        snapshot = WorkspaceSnapshot(
            commit_sha=state.commit_sha,
            branch=state.branch,
            description=description,
            created_by=workspace.agent_id,
        )

        # Append snapshot to workspace model
        updated_snapshots = list(workspace.snapshots) + [snapshot]
        workspace = workspace.model_copy(
            update={
                "snapshots": updated_snapshots,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "create_snapshot: created snapshot | id=%s sha=%s path=%s",
            snapshot.snapshot_id,
            state.commit_sha[:8],
            self._root,
        )

        if emit_event:
            self._emit_snapshot_created(
                snapshot=snapshot,
                workspace=workspace,
                snapshot_type=snapshot_type,
            )

        return snapshot

    # ─────────────────────────────────────────────────────────────────────────
    # Sync
    # ─────────────────────────────────────────────────────────────────────────

    def sync(
        self,
        remote: str = "origin",
        branch: str | None = None,
        emit_event: bool = True,
    ) -> None:
        """
        Pull the latest changes from the remote repository.

        Args:
            remote:     Remote name (default: 'origin').
            branch:     Branch to pull.  Defaults to the current branch.
            emit_event: If True, emit a WORKSPACE_SYNCED Kafka event.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
            GitOpsError: If the pull fails.
        """
        workspace = self._load_workspace()

        # Update workspace status to SYNCING
        workspace = workspace.model_copy(
            update={"status": WorkspaceStatus.SYNCING}
        )
        self._save_workspace(workspace)

        try:
            self.git.pull(remote=remote, branch=branch)
        except GitOpsError:
            workspace = workspace.model_copy(
                update={"status": WorkspaceStatus.ERROR}
            )
            self._save_workspace(workspace)
            raise

        # Update git state and status
        git_state = self._build_git_state()
        workspace = workspace.model_copy(
            update={
                "git_state": git_state,
                "status": WorkspaceStatus.ACTIVE,
                "last_synced_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "sync: synced workspace | remote=%s branch=%s path=%s",
            remote,
            branch or "current",
            self._root,
        )

        if emit_event:
            self._emit_workspace_synced(workspace)

    # ─────────────────────────────────────────────────────────────────────────
    # Status & state
    # ─────────────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """
        Return a comprehensive status summary of the workspace.

        Returns:
            A dict with keys: workspace, git_state, checkpoints, disk_usage_mb.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
        """
        workspace = self._load_workspace()
        git_state = self._build_git_state()
        checkpoints = self._load_checkpoints()
        disk_mb = self.git.disk_usage_mb() if self.git.is_initialised else 0.0

        return {
            "workspace": workspace.model_dump(mode="json"),
            "git_state": git_state.model_dump(mode="json"),
            "checkpoints": [c.model_dump(mode="json") for c in checkpoints],
            "disk_usage_mb": round(disk_mb, 2),
        }

    def update_status(self, status: WorkspaceStatus) -> Workspace:
        """
        Update the workspace lifecycle status.

        Args:
            status: New ``WorkspaceStatus`` value.

        Returns:
            The updated ``Workspace`` model.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
        """
        workspace = self._load_workspace()
        workspace = workspace.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace
        return workspace

    # ─────────────────────────────────────────────────────────────────────────
    # Archive / cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def archive(self, emit_event: bool = False) -> Workspace:
        """
        Mark the workspace as archived.

        Sets the workspace status to ARCHIVED and records the archive timestamp.
        Does NOT delete any files — use ``destroy()`` for that.

        Args:
            emit_event: Reserved for future use.

        Returns:
            The updated ``Workspace`` model.

        Raises:
            FileNotFoundError: If the workspace has not been initialised.
        """
        workspace = self._load_workspace()
        workspace = workspace.model_copy(
            update={
                "status": WorkspaceStatus.ARCHIVED,
                "archived_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._save_workspace(workspace)
        self._workspace = workspace

        logger.info(
            "archive: archived workspace | workspace_id=%s path=%s",
            workspace.workspace_id,
            self._root,
        )
        return workspace

    def destroy(self, confirm: bool = False) -> None:
        """
        Permanently delete the workspace directory and all its contents.

        This is irreversible.  Pass ``confirm=True`` to proceed.

        Args:
            confirm: Must be True to actually delete the workspace.

        Raises:
            ValueError: If ``confirm`` is False.
            OSError: If deletion fails.
        """
        if not confirm:
            raise ValueError(
                "Pass confirm=True to permanently delete the workspace. "
                "This action is irreversible."
            )

        if self._git is not None:
            self._git.close()
            self._git = None

        shutil.rmtree(str(self._root), ignore_errors=False)
        self._workspace = None

        logger.warning(
            "destroy: permanently deleted workspace | root=%s", self._root
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Context manager
    # ─────────────────────────────────────────────────────────────────────────

    def __enter__(self) -> "WorkspaceManager":
        return self

    def __exit__(self, *args: object) -> None:
        if self._git is not None:
            self._git.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Private: persistence helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _workspace_json_path(self) -> Path:
        return self._flowos_dir / "workspace.json"

    def _checkpoints_json_path(self) -> Path:
        return self._flowos_dir / "checkpoints.json"

    def _save_workspace(self, workspace: Workspace) -> None:
        """Persist the Workspace model to .flowos/workspace.json."""
        self._flowos_dir.mkdir(parents=True, exist_ok=True)
        self._workspace_json_path().write_text(
            workspace.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _load_workspace(self) -> Workspace:
        """Load the Workspace model from .flowos/workspace.json."""
        if self._workspace is not None:
            return self._workspace

        ws_path = self._workspace_json_path()
        if not ws_path.exists():
            raise FileNotFoundError(
                f"Workspace not initialised at {self._root}. "
                "Run 'flowos workspace init' first."
            )
        try:
            data = json.loads(ws_path.read_text(encoding="utf-8"))
            workspace = Workspace.model_validate(data)
            self._workspace = workspace
            return workspace
        except Exception as exc:
            raise ValueError(
                f"Failed to load workspace from {ws_path}: {exc}"
            ) from exc

    def _save_checkpoints(self, checkpoints: list[Checkpoint]) -> None:
        """Persist the checkpoint list to .flowos/checkpoints.json."""
        self._flowos_dir.mkdir(parents=True, exist_ok=True)
        data = [c.model_dump(mode="json") for c in checkpoints]
        self._checkpoints_json_path().write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )

    def _load_checkpoints(self) -> list[Checkpoint]:
        """Load checkpoints from .flowos/checkpoints.json."""
        ckpt_path = self._checkpoints_json_path()
        if not ckpt_path.exists():
            return []
        try:
            raw = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            return [Checkpoint.model_validate(item) for item in raw]
        except Exception as exc:
            logger.warning(
                "_load_checkpoints: failed to parse checkpoints.json | error=%s path=%s",
                exc,
                ckpt_path,
            )
            return []

    def _append_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Append a new checkpoint to .flowos/checkpoints.json."""
        checkpoints = self._load_checkpoints()
        checkpoints.append(checkpoint)
        self._save_checkpoints(checkpoints)

    def _next_checkpoint_sequence(self) -> int:
        """Return the next monotonically increasing checkpoint sequence number."""
        checkpoints = self._load_checkpoints()
        if not checkpoints:
            return 1
        return max(c.sequence for c in checkpoints) + 1

    # ─────────────────────────────────────────────────────────────────────────
    # Private: Git state helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _build_git_state(self) -> GitState:
        """Build a ``GitState`` model from the current repository state."""
        if not self.git.is_initialised:
            return GitState()

        try:
            state: RepoState = self.git.get_state()
            return GitState(
                branch=state.branch,
                commit_sha=state.commit_sha,
                commit_message=state.commit_message,
                commit_author=state.commit_author,
                committed_at=state.committed_at,
                is_dirty=state.is_dirty,
                staged_files=state.staged_files,
                unstaged_files=state.unstaged_files,
                untracked_files=state.untracked_files,
                remote_url=state.remote_url,
                ahead_count=state.ahead_count,
                behind_count=state.behind_count,
            )
        except GitOpsError:
            return GitState()

    # ─────────────────────────────────────────────────────────────────────────
    # Private: Kafka event emission
    # ─────────────────────────────────────────────────────────────────────────

    def _emit_workspace_created(self, workspace: Workspace) -> None:
        """Emit a WORKSPACE_CREATED Kafka event."""
        if self._producer is None:
            logger.debug(
                "_emit_workspace_created: no producer configured, skipping | workspace_id=%s",
                workspace.workspace_id,
            )
            return
        try:
            from shared.kafka.schemas import WorkspaceCreatedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = WorkspaceCreatedPayload(
                workspace_id=workspace.workspace_id,
                agent_id=workspace.agent_id,
                task_id=workspace.task_id,
                workflow_id=workspace.workflow_id,
                workspace_path=str(self._root),
                git_remote_url=workspace.repo_url,
                branch=workspace.git_state.branch,
            )
            event = build_event(
                event_type=EventType.WORKSPACE_CREATED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=workspace.workflow_id,
                task_id=workspace.task_id,
                agent_id=workspace.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_workspace_created: failed to emit event | error=%s", exc
            )

    def _emit_checkpoint_created(self, checkpoint: Checkpoint) -> None:
        """Emit a CHECKPOINT_CREATED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import CheckpointCreatedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = CheckpointCreatedPayload(
                checkpoint_id=checkpoint.checkpoint_id,
                task_id=checkpoint.task_id,
                workspace_id=checkpoint.workspace_id,
                agent_id=checkpoint.agent_id,
                git_commit_sha=checkpoint.git_commit_sha or "",
                branch=checkpoint.git_branch,
                message=checkpoint.message,
                checkpoint_type=checkpoint.checkpoint_type,
            )
            event = build_event(
                event_type=EventType.CHECKPOINT_CREATED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                agent_id=checkpoint.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_checkpoint_created: failed to emit event | error=%s", exc
            )

    def _emit_checkpoint_reverted(
        self,
        checkpoint: Checkpoint,
        workspace: Workspace,
    ) -> None:
        """Emit a CHECKPOINT_REVERTED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import CheckpointRevertedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = CheckpointRevertedPayload(
                checkpoint_id=checkpoint.checkpoint_id,
                task_id=checkpoint.task_id,
                workspace_id=checkpoint.workspace_id,
                agent_id=checkpoint.agent_id,
            )
            event = build_event(
                event_type=EventType.CHECKPOINT_REVERTED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                agent_id=checkpoint.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_checkpoint_reverted: failed to emit event | error=%s", exc
            )

    def _emit_branch_created(
        self,
        workspace: Workspace,
        branch_name: str,
        base_sha: str | None,
    ) -> None:
        """Emit a BRANCH_CREATED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import BranchCreatedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = BranchCreatedPayload(
                workspace_id=workspace.workspace_id,
                agent_id=workspace.agent_id,
                branch_name=branch_name,
                base_branch=workspace.git_state.branch,
                base_commit_sha=base_sha,
            )
            event = build_event(
                event_type=EventType.BRANCH_CREATED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=workspace.workflow_id,
                task_id=workspace.task_id,
                agent_id=workspace.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_branch_created: failed to emit event | error=%s", exc
            )

    def _emit_handoff_prepared(
        self,
        handoff_id: str,
        checkpoint: Checkpoint,
        workspace: Workspace,
        to_agent_id: str,
    ) -> None:
        """Emit a HANDOFF_PREPARED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import HandoffPreparedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = HandoffPreparedPayload(
                handoff_id=handoff_id,
                task_id=checkpoint.task_id,
                workspace_id=workspace.workspace_id,
                from_agent_id=workspace.agent_id,
                to_agent_id=to_agent_id,
                snapshot_commit_sha=checkpoint.git_commit_sha,
            )
            event = build_event(
                event_type=EventType.HANDOFF_PREPARED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=checkpoint.workflow_id,
                task_id=checkpoint.task_id,
                agent_id=workspace.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_handoff_prepared: failed to emit event | error=%s", exc
            )

    def _emit_snapshot_created(
        self,
        snapshot: WorkspaceSnapshot,
        workspace: Workspace,
        snapshot_type: str,
    ) -> None:
        """Emit a SNAPSHOT_CREATED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import SnapshotCreatedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = SnapshotCreatedPayload(
                snapshot_id=snapshot.snapshot_id,
                workspace_id=workspace.workspace_id,
                agent_id=workspace.agent_id,
                git_commit_sha=snapshot.commit_sha,
                snapshot_type=snapshot_type,
                size_bytes=snapshot.size_bytes,
            )
            event = build_event(
                event_type=EventType.SNAPSHOT_CREATED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=workspace.workflow_id,
                task_id=workspace.task_id,
                agent_id=workspace.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_snapshot_created: failed to emit event | error=%s", exc
            )

    def _emit_workspace_synced(self, workspace: Workspace) -> None:
        """Emit a WORKSPACE_SYNCED Kafka event."""
        if self._producer is None:
            return
        try:
            from shared.kafka.schemas import WorkspaceSyncedPayload, build_event
            from shared.models.event import EventSource, EventType

            payload = WorkspaceSyncedPayload(
                workspace_id=workspace.workspace_id,
                agent_id=workspace.agent_id,
                commit_sha=workspace.git_state.commit_sha,
                branch=workspace.git_state.branch,
            )
            event = build_event(
                event_type=EventType.WORKSPACE_SYNCED,
                payload=payload,
                source=EventSource.WORKSPACE_MANAGER,
                workflow_id=workspace.workflow_id,
                task_id=workspace.task_id,
                agent_id=workspace.agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning(
                "_emit_workspace_synced: failed to emit event | error=%s", exc
            )
