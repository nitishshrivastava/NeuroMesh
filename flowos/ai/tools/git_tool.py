"""
ai/tools/git_tool.py — FlowOS LangChain Git Tools

Provides LangChain tools that allow AI reasoning agents to interact with the
Git repository in their workspace.  All operations are read-only by default;
write operations (commit, branch) are gated behind explicit flags.

Tools:
    GitReadFileTool   — Read the contents of a file at a specific commit/branch.
    GitDiffTool       — Get the diff between two commits or the working tree.
    GitLogTool        — Get the commit history for a file or the whole repo.
    GitStatusTool     — Get the current working tree status.

Usage::

    from ai.tools.git_tool import get_git_tools

    tools = get_git_tools(workspace_root="/flowos/workspaces/agent-abc")
    # Pass tools to a LangGraph agent node
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class GitReadFileInput(BaseModel):
    """Input schema for GitReadFileTool."""

    file_path: str = Field(
        description="Relative path to the file within the repository."
    )
    ref: str = Field(
        default="HEAD",
        description="Git ref (branch, tag, or commit SHA) to read from. Defaults to HEAD.",
    )
    max_lines: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of lines to return. Defaults to 500.",
    )


class GitDiffInput(BaseModel):
    """Input schema for GitDiffTool."""

    from_ref: str = Field(
        default="HEAD~1",
        description="Starting Git ref for the diff (older commit). Defaults to HEAD~1.",
    )
    to_ref: str = Field(
        default="HEAD",
        description="Ending Git ref for the diff (newer commit). Defaults to HEAD.",
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Optional: limit the diff to a specific file path.",
    )
    max_lines: int = Field(
        default=300,
        ge=1,
        le=3000,
        description="Maximum number of diff lines to return.",
    )


class GitLogInput(BaseModel):
    """Input schema for GitLogTool."""

    file_path: Optional[str] = Field(
        default=None,
        description="Optional: limit the log to commits that touched this file.",
    )
    max_commits: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of commits to return.",
    )
    branch: str = Field(
        default="HEAD",
        description="Branch or ref to start the log from.",
    )


class GitStatusInput(BaseModel):
    """Input schema for GitStatusTool."""

    include_untracked: bool = Field(
        default=True,
        description="Include untracked files in the status output.",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class GitReadFileTool(BaseTool):
    """
    Read the contents of a file from the Git repository.

    Returns the file content at the specified ref (branch, tag, or commit SHA).
    Useful for reading source code, configuration files, and documentation.
    """

    name: str = "git_read_file"
    description: str = (
        "Read the contents of a file from the Git repository at a specific commit or branch. "
        "Use this to inspect source code, configuration files, or any tracked file. "
        "Input: file_path (required), ref (optional, default HEAD), max_lines (optional)."
    )
    args_schema: Type[BaseModel] = GitReadFileInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        file_path: str,
        ref: str = "HEAD",
        max_lines: int = 500,
        **kwargs: Any,
    ) -> str:
        """Execute the git_read_file tool."""
        repo_path = self._get_repo_path()
        if not repo_path:
            return "ERROR: No Git repository found in workspace."

        try:
            result = subprocess.run(
                ["git", "show", f"{ref}:{file_path}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return f"ERROR: {result.stderr.strip() or 'File not found at ref ' + ref}"

            lines = result.stdout.splitlines()
            if len(lines) > max_lines:
                truncated = lines[:max_lines]
                return "\n".join(truncated) + f"\n\n[... truncated at {max_lines} lines ...]"
            return result.stdout

        except subprocess.TimeoutExpired:
            return "ERROR: Git command timed out."
        except FileNotFoundError:
            return "ERROR: git executable not found."
        except Exception as exc:  # noqa: BLE001
            logger.error("GitReadFileTool error: %s", exc)
            return f"ERROR: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """Async version delegates to sync."""
        return self._run(*args, **kwargs)

    def _get_repo_path(self) -> Path | None:
        """Return the Git repository path."""
        root = Path(self.workspace_root or os.getcwd())
        # Check repo/ subdirectory first, then root
        for candidate in [root / "repo", root]:
            if (candidate / ".git").exists():
                return candidate
        return None


class GitDiffTool(BaseTool):
    """
    Get the diff between two Git refs or the working tree.

    Returns a unified diff showing what changed between two commits.
    Useful for understanding recent changes, reviewing patches, and
    identifying the scope of modifications.
    """

    name: str = "git_diff"
    description: str = (
        "Get the diff between two Git commits or between a commit and the working tree. "
        "Use this to understand what changed in the codebase. "
        "Input: from_ref (default HEAD~1), to_ref (default HEAD), "
        "file_path (optional), max_lines (optional)."
    )
    args_schema: Type[BaseModel] = GitDiffInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        from_ref: str = "HEAD~1",
        to_ref: str = "HEAD",
        file_path: Optional[str] = None,
        max_lines: int = 300,
        **kwargs: Any,
    ) -> str:
        """Execute the git_diff tool."""
        repo_path = self._get_repo_path()
        if not repo_path:
            return "ERROR: No Git repository found in workspace."

        try:
            cmd = ["git", "diff", from_ref, to_ref]
            if file_path:
                cmd += ["--", file_path]

            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return f"ERROR: {result.stderr.strip()}"

            if not result.stdout.strip():
                return f"No differences found between {from_ref} and {to_ref}."

            lines = result.stdout.splitlines()
            if len(lines) > max_lines:
                truncated = lines[:max_lines]
                return "\n".join(truncated) + f"\n\n[... diff truncated at {max_lines} lines ...]"
            return result.stdout

        except subprocess.TimeoutExpired:
            return "ERROR: Git diff command timed out."
        except FileNotFoundError:
            return "ERROR: git executable not found."
        except Exception as exc:  # noqa: BLE001
            logger.error("GitDiffTool error: %s", exc)
            return f"ERROR: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)

    def _get_repo_path(self) -> Path | None:
        root = Path(self.workspace_root or os.getcwd())
        for candidate in [root / "repo", root]:
            if (candidate / ".git").exists():
                return candidate
        return None


class GitLogTool(BaseTool):
    """
    Get the Git commit history.

    Returns a formatted list of recent commits with SHA, author, date,
    and commit message.  Can be filtered to a specific file path.
    """

    name: str = "git_log"
    description: str = (
        "Get the Git commit history for the repository or a specific file. "
        "Returns commit SHAs, authors, dates, and messages. "
        "Input: file_path (optional), max_commits (default 20), branch (default HEAD)."
    )
    args_schema: Type[BaseModel] = GitLogInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        file_path: Optional[str] = None,
        max_commits: int = 20,
        branch: str = "HEAD",
        **kwargs: Any,
    ) -> str:
        """Execute the git_log tool."""
        repo_path = self._get_repo_path()
        if not repo_path:
            return "ERROR: No Git repository found in workspace."

        try:
            fmt = "%H|%h|%an|%ae|%ai|%s"
            cmd = ["git", "log", f"--max-count={max_commits}", f"--format={fmt}", branch]
            if file_path:
                cmd += ["--", file_path]

            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return f"ERROR: {result.stderr.strip()}"

            if not result.stdout.strip():
                return "No commits found."

            lines = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("|", 5)
                if len(parts) == 6:
                    sha, short_sha, author, email, date, message = parts
                    lines.append(
                        f"{short_sha}  {date[:10]}  {author} <{email}>  {message}"
                    )
                else:
                    lines.append(line)

            return "\n".join(lines)

        except subprocess.TimeoutExpired:
            return "ERROR: Git log command timed out."
        except FileNotFoundError:
            return "ERROR: git executable not found."
        except Exception as exc:  # noqa: BLE001
            logger.error("GitLogTool error: %s", exc)
            return f"ERROR: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)

    def _get_repo_path(self) -> Path | None:
        root = Path(self.workspace_root or os.getcwd())
        for candidate in [root / "repo", root]:
            if (candidate / ".git").exists():
                return candidate
        return None


class GitStatusTool(BaseTool):
    """
    Get the current Git working tree status.

    Returns a summary of staged, unstaged, and untracked files.
    Useful for understanding the current state of the workspace before
    making changes or creating a checkpoint.
    """

    name: str = "git_status"
    description: str = (
        "Get the current Git working tree status showing staged, unstaged, "
        "and untracked files. "
        "Input: include_untracked (default True)."
    )
    args_schema: Type[BaseModel] = GitStatusInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        include_untracked: bool = True,
        **kwargs: Any,
    ) -> str:
        """Execute the git_status tool."""
        repo_path = self._get_repo_path()
        if not repo_path:
            return "ERROR: No Git repository found in workspace."

        try:
            cmd = ["git", "status", "--short"]
            if not include_untracked:
                cmd.append("--untracked-files=no")

            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return f"ERROR: {result.stderr.strip()}"

            if not result.stdout.strip():
                return "Working tree is clean. No changes."

            # Also get the current branch
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

            return f"Branch: {branch}\n\n{result.stdout}"

        except subprocess.TimeoutExpired:
            return "ERROR: Git status command timed out."
        except FileNotFoundError:
            return "ERROR: git executable not found."
        except Exception as exc:  # noqa: BLE001
            logger.error("GitStatusTool error: %s", exc)
            return f"ERROR: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)

    def _get_repo_path(self) -> Path | None:
        root = Path(self.workspace_root or os.getcwd())
        for candidate in [root / "repo", root]:
            if (candidate / ".git").exists():
                return candidate
        return None


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def get_git_tools(workspace_root: str | None = None) -> list[BaseTool]:
    """
    Return all Git tools configured for the given workspace.

    Args:
        workspace_root: Absolute path to the workspace root.
                        Defaults to the current working directory.

    Returns:
        List of configured LangChain tool instances.
    """
    root = workspace_root or os.getcwd()
    return [
        GitReadFileTool(workspace_root=root),
        GitDiffTool(workspace_root=root),
        GitLogTool(workspace_root=root),
        GitStatusTool(workspace_root=root),
    ]
