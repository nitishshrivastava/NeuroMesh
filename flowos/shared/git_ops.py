"""
shared/git_ops.py — FlowOS Local Git Operations

Provides the ``LocalGitOps`` class, a high-level wrapper around GitPython that
manages all Git operations within a FlowOS agent workspace.

Design principles:
- Every public method is idempotent where possible (safe to call multiple times).
- All exceptions are wrapped in ``GitOpsError`` with rich context for debugging.
- Operations that mutate state (commit, branch, revert) emit structured log
  entries at INFO level; read-only operations log at DEBUG level.
- The class is intentionally synchronous — it is called from CLI commands and
  Temporal activities, both of which run in thread pools.

Typical usage::

    from shared.git_ops import LocalGitOps

    ops = LocalGitOps("/flowos/workspaces/agent-abc/repo")

    # Initialise a fresh repo
    ops.init_repo(initial_branch="main")

    # Stage and commit
    ops.stage_all()
    sha = ops.commit("feat: initial workspace scaffold")

    # Create a task branch
    ops.create_branch("flowos/task/abc123")

    # Checkpoint (stage + commit)
    sha = ops.checkpoint("checkpoint: task 50% complete")

    # Revert to a previous commit
    ops.revert_to(sha)

    # Get current state
    state = ops.get_state()
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import git
from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class GitOpsError(Exception):
    """
    Base exception for all LocalGitOps failures.

    Attributes:
        message:   Human-readable description of what went wrong.
        repo_path: Filesystem path of the repository involved.
        cause:     The underlying exception, if any.
    """

    def __init__(
        self,
        message: str,
        repo_path: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.message = message
        self.repo_path = repo_path
        self.cause = cause
        detail = f" (repo={repo_path!r})" if repo_path else ""
        super().__init__(f"{message}{detail}")

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"repo_path={self.repo_path!r}, "
            f"cause={self.cause!r})"
        )


class RepoNotInitialisedError(GitOpsError):
    """Raised when an operation requires an initialised repo but none exists."""


class BranchNotFoundError(GitOpsError):
    """Raised when a referenced branch does not exist."""


class CommitNotFoundError(GitOpsError):
    """Raised when a referenced commit SHA does not exist."""


class DirtyWorkspaceError(GitOpsError):
    """Raised when an operation requires a clean workspace but changes exist."""


class MergeConflictError(GitOpsError):
    """Raised when a merge or cherry-pick results in conflicts."""


# ─────────────────────────────────────────────────────────────────────────────
# Data transfer objects
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FileStatus:
    """
    Status of a single file in the working tree.

    Attributes:
        path:     Relative path within the repository.
        status:   One of 'added', 'modified', 'deleted', 'renamed', 'untracked'.
        old_path: Previous path for renamed files.
        staged:   True if the change is in the index (staging area).
        checksum: SHA-256 hex digest of the file content (None for deleted/untracked).
    """

    path: str
    status: str
    old_path: str | None = None
    staged: bool = False
    checksum: str | None = None


@dataclass
class CommitInfo:
    """
    Metadata about a single Git commit.

    Attributes:
        sha:        Full 40-character commit SHA.
        short_sha:  First 8 characters of the SHA.
        message:    Commit message (first line only for summary).
        author:     Author name and email as ``"Name <email>"``.
        committed_at: UTC datetime of the commit.
        parents:    List of parent commit SHAs.
        files_changed: Number of files changed in this commit.
        insertions:   Lines inserted.
        deletions:    Lines deleted.
    """

    sha: str
    short_sha: str
    message: str
    author: str
    committed_at: datetime
    parents: list[str] = field(default_factory=list)
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass
class RepoState:
    """
    A snapshot of the current repository state.

    Attributes:
        branch:          Current branch name (or 'HEAD' if detached).
        commit_sha:      HEAD commit SHA (None if no commits yet).
        commit_message:  HEAD commit message (None if no commits yet).
        commit_author:   HEAD commit author (None if no commits yet).
        committed_at:    UTC datetime of HEAD commit (None if no commits yet).
        is_dirty:        True if there are uncommitted changes.
        staged_files:    Files staged for commit.
        unstaged_files:  Modified but unstaged files.
        untracked_files: Untracked files.
        remote_url:      Remote 'origin' URL, or None.
        ahead_count:     Commits ahead of remote tracking branch.
        behind_count:    Commits behind remote tracking branch.
        is_detached:     True if HEAD is detached.
    """

    branch: str
    commit_sha: str | None
    commit_message: str | None
    commit_author: str | None
    committed_at: datetime | None
    is_dirty: bool
    staged_files: list[str]
    unstaged_files: list[str]
    untracked_files: list[str]
    remote_url: str | None
    ahead_count: int
    behind_count: int
    is_detached: bool


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────


class LocalGitOps:
    """
    High-level Git operations manager for a FlowOS agent workspace.

    Wraps GitPython to provide a clean, FlowOS-specific API for all Git
    operations needed by the workspace manager and CLI.

    All methods that require an initialised repository will raise
    ``RepoNotInitialisedError`` if the repository does not exist.

    Thread safety: instances are NOT thread-safe.  Create one instance per
    thread or protect access with a lock.

    Args:
        repo_path: Absolute or relative filesystem path to the Git repository
                   root (the directory containing ``.git/``).
        author_name:  Git author name for commits.  Defaults to 'FlowOS Agent'.
        author_email: Git author email for commits.  Defaults to
                      'agent@flowos.local'.
    """

    DEFAULT_AUTHOR_NAME = "FlowOS Agent"
    DEFAULT_AUTHOR_EMAIL = "agent@flowos.local"
    DEFAULT_BRANCH = "main"

    def __init__(
        self,
        repo_path: str,
        author_name: str = DEFAULT_AUTHOR_NAME,
        author_email: str = DEFAULT_AUTHOR_EMAIL,
    ) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._author_name = author_name
        self._author_email = author_email
        self._repo: Repo | None = None

        # Attempt to open an existing repo (lazy — don't fail if not yet init'd)
        self._try_open_repo()

    # ─────────────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def repo_path(self) -> Path:
        """Absolute path to the repository root."""
        return self._repo_path

    @property
    def is_initialised(self) -> bool:
        """Return True if the repository has been initialised."""
        return self._repo is not None

    @property
    def _repo_required(self) -> Repo:
        """Return the Repo object, raising if not initialised."""
        if self._repo is None:
            raise RepoNotInitialisedError(
                "Repository has not been initialised. Call init_repo() first.",
                repo_path=str(self._repo_path),
            )
        return self._repo

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def init_repo(
        self,
        initial_branch: str = DEFAULT_BRANCH,
        bare: bool = False,
    ) -> None:
        """
        Initialise a new Git repository at ``repo_path``.

        If the repository already exists, this is a no-op (idempotent).

        Args:
            initial_branch: Name of the initial branch (default: 'main').
            bare:           If True, create a bare repository.

        Raises:
            GitOpsError: If initialisation fails.
        """
        if self._repo is not None:
            logger.debug(
                "init_repo: repo already initialised, skipping | path=%s",
                self._repo_path,
            )
            return

        try:
            self._repo_path.mkdir(parents=True, exist_ok=True)
            self._repo = Repo.init(
                str(self._repo_path),
                initial_branch=initial_branch,
                bare=bare,
            )
            # Configure author identity in the repo's local config
            with self._repo.config_writer() as cfg:
                cfg.set_value("user", "name", self._author_name)
                cfg.set_value("user", "email", self._author_email)

            logger.info(
                "init_repo: initialised Git repository | path=%s branch=%s",
                self._repo_path,
                initial_branch,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to initialise repository: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def clone_repo(
        self,
        remote_url: str,
        branch: str | None = None,
        depth: int | None = None,
    ) -> None:
        """
        Clone a remote repository into ``repo_path``.

        Args:
            remote_url: URL of the remote repository to clone.
            branch:     Branch to check out after cloning.  Defaults to the
                        remote's default branch.
            depth:      If set, create a shallow clone with this many commits.

        Raises:
            GitOpsError: If the clone fails or the directory already contains
                         a repository.
        """
        if self._repo is not None:
            raise GitOpsError(
                "Cannot clone into an already-initialised repository.",
                repo_path=str(self._repo_path),
            )

        try:
            self._repo_path.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, Any] = {}
            if branch:
                kwargs["branch"] = branch
            if depth:
                kwargs["depth"] = depth

            self._repo = Repo.clone_from(
                remote_url,
                str(self._repo_path),
                **kwargs,
            )
            # Configure author identity
            with self._repo.config_writer() as cfg:
                cfg.set_value("user", "name", self._author_name)
                cfg.set_value("user", "email", self._author_email)

            logger.info(
                "clone_repo: cloned repository | url=%s path=%s branch=%s",
                remote_url,
                self._repo_path,
                branch or "default",
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to clone repository from {remote_url!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Staging & committing
    # ─────────────────────────────────────────────────────────────────────────

    def stage_all(self) -> list[str]:
        """
        Stage all changes (new, modified, deleted) in the working tree.

        Equivalent to ``git add -A``.

        Returns:
            List of relative file paths that were staged.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If staging fails.
        """
        repo = self._repo_required
        try:
            repo.git.add(A=True)
            # Determine staged files depending on whether HEAD exists
            if repo.head.is_valid():
                staged = [item.a_path for item in repo.index.diff("HEAD")]
            else:
                # No commits yet — all index entries are staged
                staged = [entry[0] for entry in repo.index.entries]

            logger.debug(
                "stage_all: staged %d file(s) | path=%s",
                len(staged),
                self._repo_path,
            )
            return staged
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to stage changes: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def stage_files(self, paths: list[str]) -> None:
        """
        Stage specific files.

        Args:
            paths: List of relative file paths to stage.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If staging fails.
        """
        repo = self._repo_required
        if not paths:
            return
        try:
            repo.index.add(paths)
            logger.debug(
                "stage_files: staged %d file(s) | path=%s",
                len(paths),
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to stage files {paths!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def unstage_files(self, paths: list[str]) -> None:
        """
        Unstage specific files (remove from index without discarding changes).

        Args:
            paths: List of relative file paths to unstage.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If unstaging fails.
        """
        repo = self._repo_required
        if not paths:
            return
        try:
            repo.index.reset(paths=paths)
            logger.debug(
                "unstage_files: unstaged %d file(s) | path=%s",
                len(paths),
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to unstage files {paths!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def commit(
        self,
        message: str,
        allow_empty: bool = False,
    ) -> str:
        """
        Create a commit with the currently staged changes.

        Args:
            message:     Commit message.
            allow_empty: If True, allow commits with no changes.

        Returns:
            The full 40-character SHA of the new commit.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If the commit fails (e.g. nothing staged).
        """
        repo = self._repo_required
        try:
            actor = git.Actor(self._author_name, self._author_email)
            commit_obj = repo.index.commit(
                message,
                author=actor,
                committer=actor,
                skip_hooks=True,
            )
            sha = commit_obj.hexsha
            logger.info(
                "commit: created commit | sha=%s message=%r path=%s",
                sha[:8],
                message[:80],
                self._repo_path,
            )
            return sha
        except GitCommandError as exc:
            if "nothing to commit" in str(exc) and not allow_empty:
                raise GitOpsError(
                    "Nothing to commit. Stage changes first or use allow_empty=True.",
                    repo_path=str(self._repo_path),
                    cause=exc,
                ) from exc
            raise GitOpsError(
                f"Failed to create commit: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def checkpoint(
        self,
        message: str,
        allow_empty: bool = False,
    ) -> str:
        """
        Stage all changes and create a commit in one operation.

        This is the primary method for creating FlowOS checkpoints — it
        combines ``stage_all()`` + ``commit()`` atomically.

        Args:
            message:     Commit message for the checkpoint.
            allow_empty: If True, create a commit even if nothing changed.

        Returns:
            The full 40-character SHA of the checkpoint commit.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If staging or committing fails.
        """
        self.stage_all()
        return self.commit(message, allow_empty=allow_empty)

    # ─────────────────────────────────────────────────────────────────────────
    # Branch management
    # ─────────────────────────────────────────────────────────────────────────

    def create_branch(
        self,
        branch_name: str,
        base_ref: str | None = None,
        checkout: bool = True,
    ) -> str:
        """
        Create a new branch.

        Args:
            branch_name: Name of the new branch.
            base_ref:    Commit SHA or branch name to base the new branch on.
                         Defaults to HEAD.
            checkout:    If True, check out the new branch after creation.

        Returns:
            The commit SHA that the new branch points to.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If branch creation fails.
        """
        repo = self._repo_required
        try:
            if base_ref:
                base = repo.commit(base_ref)
            else:
                base = repo.head.commit

            new_branch = repo.create_head(branch_name, commit=base)
            if checkout:
                new_branch.checkout()

            sha = base.hexsha
            logger.info(
                "create_branch: created branch | name=%s base=%s checkout=%s path=%s",
                branch_name,
                sha[:8],
                checkout,
                self._repo_path,
            )
            return sha
        except git.BadName as exc:
            raise CommitNotFoundError(
                f"Base ref {base_ref!r} not found.",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to create branch {branch_name!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def checkout_branch(self, branch_name: str) -> None:
        """
        Check out an existing branch.

        Args:
            branch_name: Name of the branch to check out.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            BranchNotFoundError: If the branch does not exist.
            DirtyWorkspaceError: If there are uncommitted changes.
            GitOpsError: If checkout fails.
        """
        repo = self._repo_required
        try:
            branch = repo.heads[branch_name]
            branch.checkout()
            logger.info(
                "checkout_branch: checked out branch | name=%s path=%s",
                branch_name,
                self._repo_path,
            )
        except IndexError as exc:
            raise BranchNotFoundError(
                f"Branch {branch_name!r} not found.",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc
        except GitCommandError as exc:
            if "Your local changes" in str(exc):
                raise DirtyWorkspaceError(
                    f"Cannot checkout {branch_name!r}: uncommitted changes exist.",
                    repo_path=str(self._repo_path),
                    cause=exc,
                ) from exc
            raise GitOpsError(
                f"Failed to checkout branch {branch_name!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def delete_branch(
        self,
        branch_name: str,
        force: bool = False,
    ) -> None:
        """
        Delete a local branch.

        Args:
            branch_name: Name of the branch to delete.
            force:       If True, delete even if not fully merged.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            BranchNotFoundError: If the branch does not exist.
            GitOpsError: If deletion fails.
        """
        repo = self._repo_required
        try:
            repo.delete_head(branch_name, force=force)
            logger.info(
                "delete_branch: deleted branch | name=%s force=%s path=%s",
                branch_name,
                force,
                self._repo_path,
            )
        except GitCommandError as exc:
            if "not found" in str(exc).lower():
                raise BranchNotFoundError(
                    f"Branch {branch_name!r} not found.",
                    repo_path=str(self._repo_path),
                    cause=exc,
                ) from exc
            raise GitOpsError(
                f"Failed to delete branch {branch_name!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def list_branches(self, include_remote: bool = False) -> list[str]:
        """
        List all local branches (and optionally remote branches).

        Args:
            include_remote: If True, also include remote-tracking branches.

        Returns:
            List of branch names.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required
        branches = [h.name for h in repo.heads]
        if include_remote:
            branches += [r.name for r in repo.remotes[0].refs] if repo.remotes else []
        return branches

    def current_branch(self) -> str:
        """
        Return the name of the currently checked-out branch.

        Returns:
            Branch name, or 'HEAD' if in detached HEAD state.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required
        try:
            return repo.active_branch.name
        except TypeError:
            # Detached HEAD
            return "HEAD"

    # ─────────────────────────────────────────────────────────────────────────
    # Revert / reset operations
    # ─────────────────────────────────────────────────────────────────────────

    def revert_to(
        self,
        commit_sha: str,
        hard: bool = True,
    ) -> None:
        """
        Reset the working tree to a specific commit.

        By default performs a hard reset (discards all uncommitted changes and
        moves HEAD to the target commit).  Use ``hard=False`` for a mixed reset
        (moves HEAD but keeps working tree changes unstaged).

        Args:
            commit_sha: Full or abbreviated commit SHA to reset to.
            hard:       If True, perform a hard reset (default).

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            CommitNotFoundError: If the commit SHA does not exist.
            GitOpsError: If the reset fails.
        """
        repo = self._repo_required
        try:
            commit = repo.commit(commit_sha)
            reset_type = "hard" if hard else "mixed"
            repo.git.reset(f"--{reset_type}", commit.hexsha)
            logger.info(
                "revert_to: reset to commit | sha=%s type=%s path=%s",
                commit.hexsha[:8],
                reset_type,
                self._repo_path,
            )
        except git.BadName as exc:
            raise CommitNotFoundError(
                f"Commit {commit_sha!r} not found.",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to reset to {commit_sha!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def discard_changes(self, paths: list[str] | None = None) -> None:
        """
        Discard uncommitted changes in the working tree.

        Args:
            paths: List of relative file paths to discard.  If None, discard
                   all changes (equivalent to ``git checkout -- .``).

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If the discard fails.
        """
        repo = self._repo_required
        try:
            if paths:
                repo.git.checkout("--", *paths)
            else:
                repo.git.checkout("--", ".")
            logger.info(
                "discard_changes: discarded changes | paths=%s path=%s",
                paths or "all",
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to discard changes: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def cherry_pick(self, commit_sha: str) -> str:
        """
        Cherry-pick a commit onto the current branch.

        Args:
            commit_sha: SHA of the commit to cherry-pick.

        Returns:
            SHA of the new cherry-picked commit.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            CommitNotFoundError: If the commit SHA does not exist.
            MergeConflictError: If the cherry-pick results in conflicts.
            GitOpsError: If the cherry-pick fails.
        """
        repo = self._repo_required
        try:
            repo.git.cherry_pick(commit_sha)
            new_sha = repo.head.commit.hexsha
            logger.info(
                "cherry_pick: cherry-picked commit | src=%s new=%s path=%s",
                commit_sha[:8],
                new_sha[:8],
                self._repo_path,
            )
            return new_sha
        except git.BadName as exc:
            raise CommitNotFoundError(
                f"Commit {commit_sha!r} not found.",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc
        except GitCommandError as exc:
            if "CONFLICT" in str(exc) or "conflict" in str(exc).lower():
                raise MergeConflictError(
                    f"Cherry-pick of {commit_sha!r} resulted in conflicts.",
                    repo_path=str(self._repo_path),
                    cause=exc,
                ) from exc
            raise GitOpsError(
                f"Failed to cherry-pick {commit_sha!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Tagging
    # ─────────────────────────────────────────────────────────────────────────

    def create_tag(
        self,
        tag_name: str,
        message: str | None = None,
        ref: str | None = None,
    ) -> None:
        """
        Create a lightweight or annotated Git tag.

        Args:
            tag_name: Name of the tag.
            message:  If provided, create an annotated tag with this message.
            ref:      Commit SHA or ref to tag.  Defaults to HEAD.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If tag creation fails.
        """
        repo = self._repo_required
        try:
            kwargs: dict[str, Any] = {}
            if message:
                kwargs["message"] = message
            if ref:
                kwargs["ref"] = ref

            repo.create_tag(tag_name, **kwargs)
            logger.info(
                "create_tag: created tag | name=%s annotated=%s path=%s",
                tag_name,
                bool(message),
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to create tag {tag_name!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def delete_tag(self, tag_name: str) -> None:
        """
        Delete a local tag.

        Args:
            tag_name: Name of the tag to delete.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If tag deletion fails.
        """
        repo = self._repo_required
        try:
            repo.delete_tag(tag_name)
            logger.info(
                "delete_tag: deleted tag | name=%s path=%s",
                tag_name,
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to delete tag {tag_name!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def list_tags(self) -> list[str]:
        """
        Return a list of all tag names in the repository.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required
        return [tag.name for tag in repo.tags]

    # ─────────────────────────────────────────────────────────────────────────
    # Remote operations
    # ─────────────────────────────────────────────────────────────────────────

    def add_remote(self, name: str, url: str) -> None:
        """
        Add a remote to the repository.

        Args:
            name: Remote name (e.g. 'origin').
            url:  Remote URL.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If adding the remote fails.
        """
        repo = self._repo_required
        try:
            repo.create_remote(name, url)
            logger.info(
                "add_remote: added remote | name=%s url=%s path=%s",
                name,
                url,
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to add remote {name!r} → {url!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def fetch(
        self,
        remote: str = "origin",
        branch: str | None = None,
    ) -> None:
        """
        Fetch from a remote.

        Args:
            remote: Remote name (default: 'origin').
            branch: Specific branch to fetch.  If None, fetch all.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If the fetch fails.
        """
        repo = self._repo_required
        try:
            remote_obj = repo.remote(remote)
            if branch:
                remote_obj.fetch(branch)
            else:
                remote_obj.fetch()
            logger.info(
                "fetch: fetched from remote | remote=%s branch=%s path=%s",
                remote,
                branch or "all",
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to fetch from {remote!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def push(
        self,
        remote: str = "origin",
        branch: str | None = None,
        force: bool = False,
        set_upstream: bool = False,
    ) -> None:
        """
        Push the current branch to a remote.

        Args:
            remote:       Remote name (default: 'origin').
            branch:       Branch to push.  Defaults to the current branch.
            force:        If True, force-push (use with caution).
            set_upstream: If True, set the upstream tracking branch.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If the push fails.
        """
        repo = self._repo_required
        try:
            push_branch = branch or self.current_branch()
            remote_obj = repo.remote(remote)
            kwargs: dict[str, Any] = {}
            if force:
                kwargs["force"] = True
            if set_upstream:
                kwargs["set_upstream"] = True

            remote_obj.push(push_branch, **kwargs)
            logger.info(
                "push: pushed branch | remote=%s branch=%s force=%s path=%s",
                remote,
                push_branch,
                force,
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to push to {remote!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def pull(
        self,
        remote: str = "origin",
        branch: str | None = None,
        rebase: bool = False,
    ) -> None:
        """
        Pull from a remote.

        Args:
            remote: Remote name (default: 'origin').
            branch: Branch to pull.  Defaults to the current branch.
            rebase: If True, rebase instead of merge.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            MergeConflictError: If the pull results in conflicts.
            GitOpsError: If the pull fails.
        """
        repo = self._repo_required
        try:
            pull_branch = branch or self.current_branch()
            remote_obj = repo.remote(remote)
            kwargs: dict[str, Any] = {}
            if rebase:
                kwargs["rebase"] = True

            remote_obj.pull(pull_branch, **kwargs)
            logger.info(
                "pull: pulled from remote | remote=%s branch=%s rebase=%s path=%s",
                remote,
                pull_branch,
                rebase,
                self._repo_path,
            )
        except GitCommandError as exc:
            if "CONFLICT" in str(exc) or "conflict" in str(exc).lower():
                raise MergeConflictError(
                    f"Pull from {remote!r}/{pull_branch!r} resulted in conflicts.",
                    repo_path=str(self._repo_path),
                    cause=exc,
                ) from exc
            raise GitOpsError(
                f"Failed to pull from {remote!r}: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Patch operations
    # ─────────────────────────────────────────────────────────────────────────

    def create_patch(
        self,
        from_ref: str | None = None,
        to_ref: str | None = None,
        output_path: str | None = None,
    ) -> str:
        """
        Create a unified diff patch between two refs.

        Args:
            from_ref:    Starting ref (commit SHA or branch).  Defaults to
                         the parent of HEAD.
            to_ref:      Ending ref.  Defaults to HEAD.
            output_path: If provided, write the patch to this file path.

        Returns:
            The patch content as a string.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If patch creation fails.
        """
        repo = self._repo_required
        try:
            if from_ref and to_ref:
                patch_content = repo.git.diff(from_ref, to_ref, unified=3)
            elif to_ref:
                patch_content = repo.git.diff(to_ref, unified=3)
            else:
                # Diff HEAD against its parent
                if repo.head.commit.parents:
                    parent = repo.head.commit.parents[0].hexsha
                    patch_content = repo.git.diff(parent, "HEAD", unified=3)
                else:
                    # First commit — diff against empty tree
                    patch_content = repo.git.diff(
                        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
                        "HEAD",
                        unified=3,
                    )

            if output_path:
                Path(output_path).write_text(patch_content, encoding="utf-8")
                logger.info(
                    "create_patch: wrote patch | output=%s path=%s",
                    output_path,
                    self._repo_path,
                )

            return patch_content
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to create patch: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def apply_patch(
        self,
        patch_content: str | None = None,
        patch_file: str | None = None,
        check: bool = False,
    ) -> None:
        """
        Apply a unified diff patch to the working tree.

        Args:
            patch_content: Patch content as a string.  Mutually exclusive with
                           ``patch_file``.
            patch_file:    Path to a patch file.  Mutually exclusive with
                           ``patch_content``.
            check:         If True, perform a dry-run check without applying.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            ValueError: If neither or both of patch_content/patch_file are given.
            GitOpsError: If the patch cannot be applied.
        """
        if patch_content is None and patch_file is None:
            raise ValueError("Provide either patch_content or patch_file.")
        if patch_content is not None and patch_file is not None:
            raise ValueError("Provide only one of patch_content or patch_file.")

        repo = self._repo_required
        try:
            args: list[str] = []
            if check:
                args.append("--check")

            if patch_file:
                repo.git.apply(*args, patch_file)
            else:
                # Write to a temp file and apply
                import tempfile

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".patch",
                    delete=False,
                    encoding="utf-8",
                ) as tmp:
                    tmp.write(patch_content)  # type: ignore[arg-type]
                    tmp_path = tmp.name
                try:
                    repo.git.apply(*args, tmp_path)
                finally:
                    os.unlink(tmp_path)

            logger.info(
                "apply_patch: applied patch | check=%s path=%s",
                check,
                self._repo_path,
            )
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to apply patch: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # State inspection
    # ─────────────────────────────────────────────────────────────────────────

    def get_state(self) -> RepoState:
        """
        Return a comprehensive snapshot of the current repository state.

        Returns:
            A ``RepoState`` dataclass with all relevant state information.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required

        # Branch / HEAD
        is_detached = repo.head.is_detached
        try:
            branch = repo.active_branch.name
        except TypeError:
            branch = "HEAD"

        # Commit info
        commit_sha: str | None = None
        commit_message: str | None = None
        commit_author: str | None = None
        committed_at: datetime | None = None

        if repo.head.is_valid():
            head_commit = repo.head.commit
            commit_sha = head_commit.hexsha
            commit_message = head_commit.message.strip()
            commit_author = f"{head_commit.author.name} <{head_commit.author.email}>"
            committed_at = datetime.fromtimestamp(
                head_commit.committed_date, tz=timezone.utc
            )

        # Dirty state
        is_dirty = repo.is_dirty(untracked_files=True)

        # Staged files
        staged_files: list[str] = []
        if repo.head.is_valid():
            staged_files = [item.a_path for item in repo.index.diff("HEAD")]
        else:
            staged_files = [entry[0] for entry in repo.index.entries]

        # Unstaged modified files
        unstaged_files = [item.a_path for item in repo.index.diff(None)]

        # Untracked files
        untracked_files = repo.untracked_files

        # Remote info
        remote_url: str | None = None
        ahead_count = 0
        behind_count = 0

        if repo.remotes:
            try:
                origin = repo.remote("origin")
                remote_url = origin.url
                # Ahead/behind counts
                if repo.head.is_valid() and not is_detached:
                    tracking = repo.active_branch.tracking_branch()
                    if tracking:
                        ahead_count = sum(
                            1
                            for _ in repo.iter_commits(
                                f"{tracking.name}..{branch}"
                            )
                        )
                        behind_count = sum(
                            1
                            for _ in repo.iter_commits(
                                f"{branch}..{tracking.name}"
                            )
                        )
            except (ValueError, GitCommandError):
                pass

        return RepoState(
            branch=branch,
            commit_sha=commit_sha,
            commit_message=commit_message,
            commit_author=commit_author,
            committed_at=committed_at,
            is_dirty=is_dirty,
            staged_files=staged_files,
            unstaged_files=unstaged_files,
            untracked_files=list(untracked_files),
            remote_url=remote_url,
            ahead_count=ahead_count,
            behind_count=behind_count,
            is_detached=is_detached,
        )

    def get_commit_info(self, ref: str = "HEAD") -> CommitInfo:
        """
        Return detailed information about a specific commit.

        Args:
            ref: Commit SHA, branch name, or 'HEAD'.

        Returns:
            A ``CommitInfo`` dataclass.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            CommitNotFoundError: If the ref does not exist.
        """
        repo = self._repo_required
        try:
            commit = repo.commit(ref)
            stats = commit.stats
            return CommitInfo(
                sha=commit.hexsha,
                short_sha=commit.hexsha[:8],
                message=commit.message.strip(),
                author=f"{commit.author.name} <{commit.author.email}>",
                committed_at=datetime.fromtimestamp(
                    commit.committed_date, tz=timezone.utc
                ),
                parents=[p.hexsha for p in commit.parents],
                files_changed=stats.total["files"],
                insertions=stats.total["insertions"],
                deletions=stats.total["deletions"],
            )
        except git.BadName as exc:
            raise CommitNotFoundError(
                f"Ref {ref!r} not found.",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def get_file_status(self) -> list[FileStatus]:
        """
        Return the status of all changed files in the working tree.

        Returns:
            List of ``FileStatus`` objects for all staged, unstaged, and
            untracked files.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required
        results: list[FileStatus] = []

        # Staged changes (index vs HEAD)
        if repo.head.is_valid():
            for diff in repo.index.diff("HEAD"):
                status = self._diff_type_to_status(diff.change_type)
                checksum = self._file_checksum(diff.a_path)
                results.append(
                    FileStatus(
                        path=diff.a_path,
                        status=status,
                        old_path=diff.rename_from if diff.renamed_file else None,
                        staged=True,
                        checksum=checksum,
                    )
                )
        else:
            # No commits yet — all index entries are "added"
            for entry_key in repo.index.entries:
                path = entry_key[0]
                checksum = self._file_checksum(path)
                results.append(
                    FileStatus(path=path, status="added", staged=True, checksum=checksum)
                )

        # Unstaged changes (working tree vs index)
        for diff in repo.index.diff(None):
            status = self._diff_type_to_status(diff.change_type)
            checksum = self._file_checksum(diff.a_path)
            results.append(
                FileStatus(
                    path=diff.a_path,
                    status=status,
                    old_path=diff.rename_from if diff.renamed_file else None,
                    staged=False,
                    checksum=checksum,
                )
            )

        # Untracked files
        for path in repo.untracked_files:
            checksum = self._file_checksum(path)
            results.append(
                FileStatus(path=path, status="untracked", staged=False, checksum=checksum)
            )

        return results

    def get_log(
        self,
        branch: str | None = None,
        max_count: int = 20,
        since: str | None = None,
    ) -> list[CommitInfo]:
        """
        Return the commit log for a branch.

        Args:
            branch:    Branch name.  Defaults to the current branch.
            max_count: Maximum number of commits to return.
            since:     Only return commits after this date string (e.g. '1 week ago').

        Returns:
            List of ``CommitInfo`` objects, newest first.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        repo = self._repo_required
        try:
            ref = branch or self.current_branch()
            kwargs: dict[str, Any] = {"max_count": max_count}
            if since:
                kwargs["since"] = since

            commits = list(repo.iter_commits(ref, **kwargs))
            return [
                CommitInfo(
                    sha=c.hexsha,
                    short_sha=c.hexsha[:8],
                    message=c.message.strip(),
                    author=f"{c.author.name} <{c.author.email}>",
                    committed_at=datetime.fromtimestamp(
                        c.committed_date, tz=timezone.utc
                    ),
                    parents=[p.hexsha for p in c.parents],
                    files_changed=c.stats.total["files"],
                    insertions=c.stats.total["insertions"],
                    deletions=c.stats.total["deletions"],
                )
                for c in commits
            ]
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to get log: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    def diff(
        self,
        from_ref: str | None = None,
        to_ref: str | None = None,
        stat_only: bool = False,
    ) -> str:
        """
        Return the diff between two refs as a string.

        Args:
            from_ref:  Starting ref.  Defaults to HEAD~1 (parent of HEAD).
            to_ref:    Ending ref.  Defaults to HEAD.
            stat_only: If True, return only the diffstat (file names + counts).

        Returns:
            Diff output as a string.

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
            GitOpsError: If the diff fails.
        """
        repo = self._repo_required
        try:
            args: list[str] = []
            if stat_only:
                args.append("--stat")
            else:
                args.extend(["--unified=3"])

            if from_ref and to_ref:
                return repo.git.diff(*args, from_ref, to_ref)
            elif from_ref:
                return repo.git.diff(*args, from_ref)
            elif to_ref:
                return repo.git.diff(*args, to_ref)
            else:
                # Working tree vs HEAD
                return repo.git.diff(*args)
        except GitCommandError as exc:
            raise GitOpsError(
                f"Failed to compute diff: {exc}",
                repo_path=str(self._repo_path),
                cause=exc,
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Disk usage
    # ─────────────────────────────────────────────────────────────────────────

    def disk_usage_mb(self) -> float:
        """
        Return the total disk usage of the repository in megabytes.

        Includes the ``.git`` directory and all working tree files.

        Returns:
            Disk usage in megabytes (float).

        Raises:
            RepoNotInitialisedError: If the repository is not initialised.
        """
        self._repo_required  # ensure initialised
        total_bytes = sum(
            f.stat().st_size
            for f in self._repo_path.rglob("*")
            if f.is_file()
        )
        return total_bytes / (1024 * 1024)

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Release resources held by the GitPython Repo object.

        Safe to call multiple times.
        """
        if self._repo is not None:
            self._repo.close()
            self._repo = None
            logger.debug("close: released repo resources | path=%s", self._repo_path)

    def __enter__(self) -> "LocalGitOps":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _try_open_repo(self) -> None:
        """Attempt to open an existing repository; silently ignore if absent."""
        try:
            self._repo = Repo(str(self._repo_path))
        except (InvalidGitRepositoryError, NoSuchPathError):
            self._repo = None

    def _file_checksum(self, relative_path: str) -> str | None:
        """Compute the SHA-256 checksum of a file, or None if it doesn't exist."""
        full_path = self._repo_path / relative_path
        if not full_path.is_file():
            return None
        try:
            h = hashlib.sha256()
            with open(full_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _diff_type_to_status(change_type: str) -> str:
        """Map a GitPython diff change_type character to a human-readable status."""
        mapping = {
            "A": "added",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "C": "copied",
            "T": "modified",  # type change
            "U": "modified",  # unmerged
        }
        return mapping.get(change_type, "modified")
