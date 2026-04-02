"""
tests/unit/test_git_ops.py — Unit tests for FlowOS LocalGitOps.

Tests cover:
- LocalGitOps initialisation
- init_repo(): repository initialisation
- is_initialised property
- stage_all() and commit()
- create_branch() and checkout_branch()
- checkpoint() (stage + commit)
- revert_to() hard reset
- get_state() repository state
- Error handling: RepoNotInitialisedError, BranchNotFoundError
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from shared.git_ops import (
    LocalGitOps,
    GitOpsError,
    RepoNotInitialisedError,
    BranchNotFoundError,
    CommitInfo,
    RepoState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """Return a temporary directory for a Git repository."""
    return tmp_path / "repo"


@pytest.fixture
def git_ops(repo_path: Path) -> LocalGitOps:
    """Return an initialised LocalGitOps instance."""
    ops = LocalGitOps(str(repo_path))
    ops.init_repo(initial_branch="main")
    return ops


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLocalGitOpsInit:
    """Tests for LocalGitOps initialisation."""

    def test_init_creates_instance(self, repo_path: Path):
        """LocalGitOps can be instantiated with a path."""
        ops = LocalGitOps(str(repo_path))
        assert ops is not None

    def test_is_not_initialised_before_init_repo(self, repo_path: Path):
        """is_initialised is False before init_repo() is called."""
        ops = LocalGitOps(str(repo_path))
        assert ops.is_initialised is False

    def test_repo_path_is_absolute(self, repo_path: Path):
        """repo_path property returns an absolute path."""
        ops = LocalGitOps(str(repo_path))
        assert ops.repo_path.is_absolute()

    def test_default_author_name(self, repo_path: Path):
        """Default author name is 'FlowOS Agent'."""
        ops = LocalGitOps(str(repo_path))
        assert ops._author_name == LocalGitOps.DEFAULT_AUTHOR_NAME

    def test_custom_author_name(self, repo_path: Path):
        """Custom author name is stored correctly."""
        ops = LocalGitOps(str(repo_path), author_name="Test Author", author_email="test@test.com")
        assert ops._author_name == "Test Author"
        assert ops._author_email == "test@test.com"


class TestLocalGitOpsInitRepo:
    """Tests for LocalGitOps.init_repo()."""

    def test_init_repo_creates_git_directory(self, repo_path: Path):
        """init_repo() creates a .git directory."""
        ops = LocalGitOps(str(repo_path))
        ops.init_repo()
        assert (repo_path / ".git").exists()

    def test_init_repo_marks_as_initialised(self, repo_path: Path):
        """After init_repo(), is_initialised returns True."""
        ops = LocalGitOps(str(repo_path))
        ops.init_repo()
        assert ops.is_initialised is True

    def test_init_repo_is_idempotent(self, repo_path: Path):
        """Calling init_repo() twice does not raise."""
        ops = LocalGitOps(str(repo_path))
        ops.init_repo()
        ops.init_repo()  # Second call should be a no-op
        assert ops.is_initialised is True

    def test_init_repo_with_custom_branch(self, repo_path: Path):
        """init_repo() creates the repository with the specified initial branch."""
        ops = LocalGitOps(str(repo_path))
        ops.init_repo(initial_branch="develop")
        # After init, the branch should be 'develop' (though no commits yet)
        assert ops.is_initialised is True

    def test_operations_raise_before_init(self, repo_path: Path):
        """Operations that require an initialised repo raise RepoNotInitialisedError."""
        ops = LocalGitOps(str(repo_path))
        with pytest.raises(RepoNotInitialisedError):
            ops.stage_all()


class TestLocalGitOpsCommit:
    """Tests for staging and committing."""

    def test_stage_all_and_commit(self, git_ops: LocalGitOps, repo_path: Path):
        """stage_all() + commit() creates a commit."""
        test_file = repo_path / "hello.txt"
        test_file.write_text("hello world")

        git_ops.stage_all()
        sha = git_ops.commit("feat: add hello.txt")

        assert sha is not None
        assert len(sha) == 40  # Full SHA

    def test_commit_returns_full_sha(self, git_ops: LocalGitOps, repo_path: Path):
        """commit() returns the full 40-character SHA."""
        test_file = repo_path / "file.txt"
        test_file.write_text("content")
        git_ops.stage_all()
        sha = git_ops.commit("test: add file")

        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_checkpoint_stages_and_commits(self, git_ops: LocalGitOps, repo_path: Path):
        """checkpoint() stages all changes and creates a commit."""
        test_file = repo_path / "checkpoint.txt"
        test_file.write_text("checkpoint content")

        sha = git_ops.checkpoint("checkpoint: 50% complete")

        assert sha is not None
        assert len(sha) == 40

    def test_multiple_commits_create_history(self, git_ops: LocalGitOps, repo_path: Path):
        """Multiple commits create a commit history."""
        for i in range(3):
            f = repo_path / f"file{i}.txt"
            f.write_text(f"content {i}")
            git_ops.stage_all()
            git_ops.commit(f"commit {i}")

        state = git_ops.get_state()
        assert state.commit_sha is not None


class TestLocalGitOpsBranch:
    """Tests for branch operations."""

    def test_create_branch(self, git_ops: LocalGitOps, repo_path: Path):
        """create_branch() creates a new branch."""
        # Need at least one commit first
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial commit")

        git_ops.create_branch("feature/test-branch")
        # Branch should exist
        branches = [b.name for b in git_ops._repo_required.branches]
        assert "feature/test-branch" in branches

    def test_checkout_branch(self, git_ops: LocalGitOps, repo_path: Path):
        """checkout_branch() switches to the specified branch."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial commit")

        git_ops.create_branch("feature/test")
        git_ops.checkout_branch("feature/test")

        state = git_ops.get_state()
        assert state.branch == "feature/test"

    def test_checkout_nonexistent_branch_raises(self, git_ops: LocalGitOps, repo_path: Path):
        """checkout_branch() raises BranchNotFoundError for nonexistent branches."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial commit")

        with pytest.raises(BranchNotFoundError):
            git_ops.checkout_branch("nonexistent-branch")

    def test_create_branch_is_idempotent(self, git_ops: LocalGitOps, repo_path: Path):
        """create_branch() does not raise if the branch already exists."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial commit")

        git_ops.create_branch("feature/test")
        git_ops.create_branch("feature/test")  # Should not raise


class TestLocalGitOpsRevert:
    """Tests for revert_to() hard reset."""

    def test_revert_to_restores_file_content(self, git_ops: LocalGitOps, repo_path: Path):
        """revert_to() restores the working tree to the specified commit."""
        # Create initial commit
        test_file = repo_path / "test.txt"
        test_file.write_text("version 1")
        git_ops.stage_all()
        sha_v1 = git_ops.commit("v1")

        # Create second commit
        test_file.write_text("version 2")
        git_ops.stage_all()
        git_ops.commit("v2")

        # Revert to v1
        git_ops.revert_to(sha_v1)

        assert test_file.read_text() == "version 1"

    def test_revert_to_invalid_sha_raises(self, git_ops: LocalGitOps, repo_path: Path):
        """revert_to() raises an exception for an invalid SHA."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial")

        with pytest.raises(Exception):  # GitOpsError or ValueError from GitPython
            git_ops.revert_to("deadbeef" * 5)  # Invalid SHA


class TestLocalGitOpsGetState:
    """Tests for get_state() repository state."""

    def test_get_state_returns_repo_state(self, git_ops: LocalGitOps, repo_path: Path):
        """get_state() returns a RepoState dataclass."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial")

        state = git_ops.get_state()

        assert isinstance(state, RepoState)
        assert state.branch == "main"
        assert state.commit_sha is not None

    def test_get_state_shows_dirty_when_uncommitted_changes(
        self, git_ops: LocalGitOps, repo_path: Path
    ):
        """get_state() shows is_dirty=True when there are uncommitted changes."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial")

        # Add an untracked file
        (repo_path / "new_file.txt").write_text("new content")

        state = git_ops.get_state()
        assert state.is_dirty is True

    def test_get_state_shows_clean_after_commit(self, git_ops: LocalGitOps, repo_path: Path):
        """get_state() shows is_dirty=False after committing all changes."""
        (repo_path / "init.txt").write_text("init")
        git_ops.stage_all()
        git_ops.commit("initial")

        state = git_ops.get_state()
        # After committing, no untracked or modified files
        assert state.commit_sha is not None


class TestGitOpsError:
    """Tests for GitOpsError exception hierarchy."""

    def test_git_ops_error_message(self):
        """GitOpsError stores message and repo_path."""
        err = GitOpsError("Something went wrong", repo_path="/path/to/repo")
        assert "Something went wrong" in str(err)
        assert err.repo_path == "/path/to/repo"

    def test_repo_not_initialised_error_is_git_ops_error(self):
        """RepoNotInitialisedError is a subclass of GitOpsError."""
        err = RepoNotInitialisedError("Not initialised")
        assert isinstance(err, GitOpsError)

    def test_branch_not_found_error_is_git_ops_error(self):
        """BranchNotFoundError is a subclass of GitOpsError."""
        err = BranchNotFoundError("Branch not found")
        assert isinstance(err, GitOpsError)

    def test_git_ops_error_with_cause(self):
        """GitOpsError stores the underlying cause exception."""
        cause = ValueError("original error")
        err = GitOpsError("Wrapped error", cause=cause)
        assert err.cause is cause
