"""
ai/context_loader.py — FlowOS AI Task Context Loader

Assembles the full context needed by an AI reasoning agent before it begins
working on a task.  Context includes:

- Task metadata (description, type, priority, inputs)
- Workflow metadata (name, current step, prior steps)
- Workspace state (current branch, recent commits, changed files)
- Recent reasoning traces from prior sessions on the same task
- Relevant artifacts (build logs, test results, prior patches)
- Agent capabilities and constraints

The ``TaskContext`` dataclass is the primary input to all LangGraph reasoning
graphs.  It is serialisable to JSON so it can be stored in ``.flowos/`` and
replayed for debugging.

Usage::

    from ai.context_loader import ContextLoader, TaskContext

    loader = ContextLoader(workspace_root="/flowos/workspaces/agent-abc")
    ctx = loader.load(
        task_id="task-xyz",
        workflow_id="wf-123",
        agent_id="agent-abc",
    )
    print(ctx.task_description)
    print(ctx.workspace_branch)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context data structures
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    """Represents a single changed file in the workspace."""

    path: str
    status: str  # added, modified, deleted, renamed
    diff_snippet: str | None = None  # First N lines of the diff


@dataclass
class CommitSummary:
    """A brief summary of a Git commit."""

    sha: str
    short_sha: str
    message: str
    author: str
    committed_at: str  # ISO-8601 string


@dataclass
class ArtifactRef:
    """Reference to a build artifact or generated file."""

    name: str
    path: str
    artifact_type: str  # build_log, test_result, patch, report
    size_bytes: int
    created_at: str  # ISO-8601 string


@dataclass
class ReasoningTraceSummary:
    """Summary of a prior reasoning session on this task."""

    session_id: str
    status: str
    step_count: int
    final_output: str | None
    completed_at: str | None
    model_name: str | None


@dataclass
class TaskContext:
    """
    Complete context assembled for an AI reasoning session.

    This is the primary input to all LangGraph reasoning graphs.  It captures
    everything the AI needs to understand the task, the workspace state, and
    any prior work done on this task.

    Attributes:
        task_id:              Unique task identifier.
        task_name:            Human-readable task name.
        task_type:            Nature of work (ai, review, analysis, etc.).
        task_description:     Full task description / objective.
        task_priority:        Scheduling priority.
        task_inputs:          Input parameters provided at task creation.
        task_tags:            Tags for filtering and routing.
        workflow_id:          Workflow this task belongs to.
        workflow_name:        Human-readable workflow name.
        workflow_step_id:     Current workflow step ID.
        agent_id:             AI agent executing this task.
        workspace_root:       Absolute path to the workspace root.
        workspace_branch:     Current Git branch.
        workspace_commit_sha: HEAD commit SHA.
        workspace_is_dirty:   True if there are uncommitted changes.
        recent_commits:       Recent commit history (last N commits).
        changed_files:        Files changed since the task branch was created.
        artifacts:            Available artifacts (build logs, test results, etc.).
        prior_sessions:       Summaries of prior reasoning sessions on this task.
        build_status:         Latest build status (passed, failed, unknown).
        test_status:          Latest test status (passed, failed, unknown).
        error_context:        Error message / stack trace if task is retrying.
        custom_context:       Arbitrary additional context from the workflow DSL.
        loaded_at:            UTC timestamp when this context was assembled.
    """

    # Task identity
    task_id: str
    task_name: str
    task_type: str
    task_description: str
    task_priority: str = "normal"
    task_inputs: dict[str, Any] = field(default_factory=dict)
    task_tags: list[str] = field(default_factory=list)

    # Workflow context
    workflow_id: str = ""
    workflow_name: str = ""
    workflow_step_id: str | None = None

    # Agent identity
    agent_id: str = ""

    # Workspace state
    workspace_root: str = ""
    workspace_branch: str = "main"
    workspace_commit_sha: str | None = None
    workspace_is_dirty: bool = False

    # Git history
    recent_commits: list[CommitSummary] = field(default_factory=list)
    changed_files: list[FileChange] = field(default_factory=list)

    # Artifacts
    artifacts: list[ArtifactRef] = field(default_factory=list)

    # Prior reasoning
    prior_sessions: list[ReasoningTraceSummary] = field(default_factory=list)

    # Build / test status
    build_status: str = "unknown"
    test_status: str = "unknown"

    # Error context (for retry scenarios)
    error_context: str | None = None

    # Arbitrary additional context
    custom_context: dict[str, Any] = field(default_factory=dict)

    # Metadata
    loaded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-safe)."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_prompt_text(self) -> str:
        """
        Render the context as a structured text block suitable for inclusion
        in an LLM prompt.

        Returns a human-readable summary of the task context.
        """
        lines: list[str] = [
            "=== TASK CONTEXT ===",
            f"Task ID:          {self.task_id}",
            f"Task Name:        {self.task_name}",
            f"Task Type:        {self.task_type}",
            f"Priority:         {self.task_priority}",
            f"Workflow:         {self.workflow_name} ({self.workflow_id})",
            f"Agent:            {self.agent_id}",
            "",
            "--- OBJECTIVE ---",
            self.task_description,
            "",
        ]

        if self.task_inputs:
            lines.append("--- INPUTS ---")
            for k, v in self.task_inputs.items():
                lines.append(f"  {k}: {v}")
            lines.append("")

        lines += [
            "--- WORKSPACE STATE ---",
            f"Branch:           {self.workspace_branch}",
            f"HEAD Commit:      {self.workspace_commit_sha or 'none'}",
            f"Dirty:            {self.workspace_is_dirty}",
            f"Build Status:     {self.build_status}",
            f"Test Status:      {self.test_status}",
            "",
        ]

        if self.changed_files:
            lines.append("--- CHANGED FILES ---")
            for f in self.changed_files[:20]:  # cap at 20 for prompt size
                lines.append(f"  [{f.status}] {f.path}")
            if len(self.changed_files) > 20:
                lines.append(f"  ... and {len(self.changed_files) - 20} more")
            lines.append("")

        if self.recent_commits:
            lines.append("--- RECENT COMMITS ---")
            for c in self.recent_commits[:10]:
                lines.append(f"  {c.short_sha}  {c.committed_at[:10]}  {c.message[:80]}")
            lines.append("")

        if self.artifacts:
            lines.append("--- AVAILABLE ARTIFACTS ---")
            for a in self.artifacts:
                lines.append(f"  [{a.artifact_type}] {a.name}  ({a.size_bytes} bytes)")
            lines.append("")

        if self.prior_sessions:
            lines.append("--- PRIOR REASONING SESSIONS ---")
            for s in self.prior_sessions:
                lines.append(
                    f"  Session {s.session_id[:8]}  status={s.status}  "
                    f"steps={s.step_count}  model={s.model_name or 'unknown'}"
                )
                if s.final_output:
                    lines.append(f"    Output: {s.final_output[:120]}")
            lines.append("")

        if self.error_context:
            lines.append("--- ERROR CONTEXT (RETRY) ---")
            lines.append(self.error_context[:500])
            lines.append("")

        if self.custom_context:
            lines.append("--- ADDITIONAL CONTEXT ---")
            for k, v in self.custom_context.items():
                lines.append(f"  {k}: {v}")
            lines.append("")

        lines.append("=== END CONTEXT ===")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context Loader
# ---------------------------------------------------------------------------


class ContextLoader:
    """
    Assembles a ``TaskContext`` from the workspace filesystem and metadata.

    The loader reads from:
    - ``.flowos/workspace.json``   — workspace metadata
    - ``.flowos/task.json``        — task metadata (written by the worker)
    - ``.flowos/reasoning_trace.json`` — prior reasoning sessions
    - ``artifacts/``               — build artifacts and generated files
    - Git repository state         — via GitPython (if available)

    If any source is unavailable (e.g. no Git repo yet), the loader falls
    back gracefully and populates the context with whatever is available.

    Args:
        workspace_root: Absolute path to the workspace root directory.
                        Defaults to the current working directory.
    """

    #: Maximum number of recent commits to include in context.
    MAX_COMMITS = 15

    #: Maximum number of changed files to include in context.
    MAX_CHANGED_FILES = 50

    #: Maximum number of diff lines per file to include in context.
    MAX_DIFF_LINES = 30

    #: Maximum number of prior reasoning sessions to include.
    MAX_PRIOR_SESSIONS = 5

    def __init__(self, workspace_root: str | None = None) -> None:
        self._root = Path(workspace_root or os.getcwd()).resolve()
        self._flowos_dir = self._root / ".flowos"
        self._repo_dir = self._root / "repo"
        self._artifacts_dir = self._root / "artifacts"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self,
        task_id: str,
        workflow_id: str,
        agent_id: str,
        task_description: str | None = None,
        task_name: str | None = None,
        task_type: str = "ai",
        task_priority: str = "normal",
        task_inputs: dict[str, Any] | None = None,
        task_tags: list[str] | None = None,
        custom_context: dict[str, Any] | None = None,
        error_context: str | None = None,
    ) -> TaskContext:
        """
        Assemble a complete ``TaskContext`` for the given task.

        Args:
            task_id:          Task identifier.
            workflow_id:      Workflow identifier.
            agent_id:         AI agent identifier.
            task_description: Task objective (overrides .flowos/task.json if provided).
            task_name:        Task name (overrides .flowos/task.json if provided).
            task_type:        Task type string.
            task_priority:    Task priority string.
            task_inputs:      Input parameters.
            task_tags:        Task tags.
            custom_context:   Arbitrary additional context.
            error_context:    Error message for retry scenarios.

        Returns:
            A fully populated ``TaskContext`` instance.
        """
        logger.info(
            "Loading task context | task_id=%s workflow_id=%s agent_id=%s",
            task_id,
            workflow_id,
            agent_id,
        )

        # Load persisted task metadata (may override caller-provided values)
        task_meta = self._load_task_metadata()
        workspace_meta = self._load_workspace_metadata()

        # Resolve task fields (caller args take precedence over persisted data)
        resolved_name = task_name or task_meta.get("name", f"Task {task_id[:8]}")
        resolved_description = task_description or task_meta.get(
            "description", "No description provided."
        )
        resolved_type = task_type or task_meta.get("task_type", "ai")
        resolved_priority = task_priority or task_meta.get("priority", "normal")
        resolved_inputs = task_inputs or task_meta.get("inputs", {})
        resolved_tags = task_tags or task_meta.get("tags", [])

        # Workflow metadata
        workflow_name = workspace_meta.get("workflow_name", "")
        workflow_step_id = task_meta.get("step_id")

        # Git state
        git_state = self._load_git_state()

        # Artifacts
        artifacts = self._load_artifacts()

        # Prior reasoning sessions
        prior_sessions = self._load_prior_sessions(task_id)

        # Build / test status from artifacts
        build_status = self._infer_build_status(artifacts)
        test_status = self._infer_test_status(artifacts)

        ctx = TaskContext(
            task_id=task_id,
            task_name=resolved_name,
            task_type=resolved_type,
            task_description=resolved_description,
            task_priority=resolved_priority,
            task_inputs=resolved_inputs,
            task_tags=resolved_tags,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_step_id=workflow_step_id,
            agent_id=agent_id,
            workspace_root=str(self._root),
            workspace_branch=git_state.get("branch", "main"),
            workspace_commit_sha=git_state.get("commit_sha"),
            workspace_is_dirty=git_state.get("is_dirty", False),
            recent_commits=git_state.get("recent_commits", []),
            changed_files=git_state.get("changed_files", []),
            artifacts=artifacts,
            prior_sessions=prior_sessions,
            build_status=build_status,
            test_status=test_status,
            error_context=error_context,
            custom_context=custom_context or {},
        )

        logger.info(
            "Context loaded | task_id=%s commits=%d files=%d artifacts=%d sessions=%d",
            task_id,
            len(ctx.recent_commits),
            len(ctx.changed_files),
            len(ctx.artifacts),
            len(ctx.prior_sessions),
        )
        return ctx

    def save_context(self, ctx: TaskContext) -> Path:
        """
        Persist the context to ``.flowos/task_context.json``.

        Returns the path to the saved file.
        """
        self._flowos_dir.mkdir(parents=True, exist_ok=True)
        path = self._flowos_dir / "task_context.json"
        path.write_text(ctx.to_json(), encoding="utf-8")
        logger.debug("Context saved | path=%s", path)
        return path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_task_metadata(self) -> dict[str, Any]:
        """Load task metadata from ``.flowos/task.json`` if it exists."""
        path = self._flowos_dir / "task.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load task.json: %s", exc)
        return {}

    def _load_workspace_metadata(self) -> dict[str, Any]:
        """Load workspace metadata from ``.flowos/workspace.json`` if it exists."""
        path = self._flowos_dir / "workspace.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load workspace.json: %s", exc)
        return {}

    def _load_git_state(self) -> dict[str, Any]:
        """
        Load Git repository state from the workspace repo directory.

        Returns a dict with keys: branch, commit_sha, is_dirty,
        recent_commits, changed_files.
        """
        result: dict[str, Any] = {
            "branch": "main",
            "commit_sha": None,
            "is_dirty": False,
            "recent_commits": [],
            "changed_files": [],
        }

        # Try the repo subdirectory first, then the workspace root itself
        repo_path = self._repo_dir if self._repo_dir.exists() else self._root

        try:
            import git as gitpython
            from git import InvalidGitRepositoryError, NoSuchPathError

            try:
                repo = gitpython.Repo(str(repo_path))
            except (InvalidGitRepositoryError, NoSuchPathError):
                logger.debug("No Git repo found at %s", repo_path)
                return result

            # Branch
            try:
                result["branch"] = repo.active_branch.name
            except TypeError:
                result["branch"] = "HEAD"  # detached HEAD

            # HEAD commit
            if repo.head.is_valid():
                head = repo.head.commit
                result["commit_sha"] = head.hexsha
                result["is_dirty"] = repo.is_dirty(untracked_files=True)

                # Recent commits
                commits: list[CommitSummary] = []
                for commit in repo.iter_commits(max_count=self.MAX_COMMITS):
                    commits.append(
                        CommitSummary(
                            sha=commit.hexsha,
                            short_sha=commit.hexsha[:8],
                            message=commit.message.strip().split("\n")[0],
                            author=f"{commit.author.name} <{commit.author.email}>",
                            committed_at=datetime.fromtimestamp(
                                commit.committed_date, tz=timezone.utc
                            ).isoformat(),
                        )
                    )
                result["recent_commits"] = commits

                # Changed files (diff against HEAD~1 or index)
                changed: list[FileChange] = []
                try:
                    if repo.is_dirty():
                        # Unstaged changes
                        for diff in repo.index.diff(None)[: self.MAX_CHANGED_FILES]:
                            changed.append(
                                FileChange(
                                    path=diff.a_path or diff.b_path,
                                    status=self._diff_type_to_status(diff.change_type),
                                    diff_snippet=self._get_diff_snippet(diff),
                                )
                            )
                        # Staged changes
                        for diff in repo.index.diff("HEAD")[: self.MAX_CHANGED_FILES]:
                            changed.append(
                                FileChange(
                                    path=diff.a_path or diff.b_path,
                                    status=self._diff_type_to_status(diff.change_type),
                                )
                            )
                        # Untracked files
                        for upath in repo.untracked_files[: self.MAX_CHANGED_FILES]:
                            changed.append(FileChange(path=upath, status="untracked"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Error reading changed files: %s", exc)

                result["changed_files"] = changed

        except ImportError:
            logger.warning("GitPython not available; skipping Git state loading")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error loading Git state: %s", exc)

        return result

    def _load_artifacts(self) -> list[ArtifactRef]:
        """Scan the artifacts directory and return references to available files."""
        artifacts: list[ArtifactRef] = []

        if not self._artifacts_dir.exists():
            return artifacts

        # Map file extensions / names to artifact types
        type_map = {
            ".log": "build_log",
            ".txt": "build_log",
            ".json": "report",
            ".xml": "test_result",
            ".html": "report",
            ".patch": "patch",
            ".diff": "patch",
        }

        try:
            for entry in sorted(self._artifacts_dir.iterdir()):
                if not entry.is_file():
                    continue
                artifact_type = type_map.get(entry.suffix.lower(), "artifact")
                # Infer type from filename patterns
                name_lower = entry.name.lower()
                if "test" in name_lower and "result" in name_lower:
                    artifact_type = "test_result"
                elif "build" in name_lower and "log" in name_lower:
                    artifact_type = "build_log"
                elif "patch" in name_lower or entry.suffix == ".patch":
                    artifact_type = "patch"

                stat = entry.stat()
                artifacts.append(
                    ArtifactRef(
                        name=entry.name,
                        path=str(entry),
                        artifact_type=artifact_type,
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    )
                )
        except OSError as exc:
            logger.warning("Error scanning artifacts directory: %s", exc)

        return artifacts

    def _load_prior_sessions(self, task_id: str) -> list[ReasoningTraceSummary]:
        """
        Load summaries of prior reasoning sessions from
        ``.flowos/reasoning_trace.json``.
        """
        sessions: list[ReasoningTraceSummary] = []
        path = self._flowos_dir / "reasoning_trace.json"

        if not path.exists():
            return sessions

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # reasoning_trace.json may contain a list of sessions or a single session
            if isinstance(data, dict):
                data = [data]

            for entry in data[-self.MAX_PRIOR_SESSIONS :]:
                # Only include sessions for this task
                if entry.get("task_id") != task_id:
                    continue
                sessions.append(
                    ReasoningTraceSummary(
                        session_id=entry.get("session_id", "unknown"),
                        status=entry.get("status", "unknown"),
                        step_count=len(entry.get("steps", [])),
                        final_output=entry.get("final_output"),
                        completed_at=entry.get("completed_at"),
                        model_name=entry.get("model_name"),
                    )
                )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Failed to load reasoning_trace.json: %s", exc)

        return sessions

    def _infer_build_status(self, artifacts: list[ArtifactRef]) -> str:
        """Infer build status from available artifacts."""
        for artifact in artifacts:
            if artifact.artifact_type == "build_log":
                try:
                    content = Path(artifact.path).read_text(encoding="utf-8", errors="replace")
                    content_lower = content.lower()
                    if "build failed" in content_lower or "error:" in content_lower:
                        return "failed"
                    if "build succeeded" in content_lower or "build successful" in content_lower:
                        return "passed"
                except OSError:
                    pass
        return "unknown"

    def _infer_test_status(self, artifacts: list[ArtifactRef]) -> str:
        """Infer test status from available artifacts."""
        for artifact in artifacts:
            if artifact.artifact_type == "test_result":
                try:
                    content = Path(artifact.path).read_text(encoding="utf-8", errors="replace")
                    content_lower = content.lower()
                    if "failed" in content_lower or "error" in content_lower:
                        return "failed"
                    if "passed" in content_lower or "ok" in content_lower:
                        return "passed"
                except OSError:
                    pass
        return "unknown"

    @staticmethod
    def _diff_type_to_status(change_type: str) -> str:
        """Convert a GitPython diff change type to a human-readable status."""
        mapping = {
            "A": "added",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "C": "copied",
            "T": "type_changed",
            "U": "unmerged",
        }
        return mapping.get(change_type, "modified")

    @staticmethod
    def _get_diff_snippet(diff: Any) -> str | None:
        """Extract the first MAX_DIFF_LINES lines of a diff."""
        try:
            diff_text = diff.diff.decode("utf-8", errors="replace")
            lines = diff_text.splitlines()[:ContextLoader.MAX_DIFF_LINES]
            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return None
