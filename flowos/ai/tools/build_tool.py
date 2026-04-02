"""
ai/tools/build_tool.py — FlowOS LangChain Build Tools

Provides LangChain tools that allow AI reasoning agents to interact with the
build system.  Tools support triggering builds, checking build status, and
retrieving build logs from the workspace artifacts directory.

In production, build triggers publish Kafka events (BUILD_TRIGGERED) which
are consumed by the build runner.  In the AI context, these tools primarily
read build artifacts and status from the workspace.

Tools:
    BuildTriggerTool  — Request a build by publishing a Kafka event.
    BuildStatusTool   — Check the status of the most recent build.
    BuildLogsTool     — Read build logs from the artifacts directory.

Usage::

    from ai.tools.build_tool import get_build_tools

    tools = get_build_tools(
        workspace_root="/flowos/workspaces/agent-abc",
        task_id="task-xyz",
        workflow_id="wf-123",
        agent_id="agent-abc",
    )
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class BuildTriggerInput(BaseModel):
    """Input schema for BuildTriggerTool."""

    build_command: str = Field(
        description=(
            "The build command to execute (e.g. 'make test', 'pytest', 'npm run build'). "
            "This will be recorded as a build request."
        )
    )
    reason: str = Field(
        default="AI-triggered build",
        description="Human-readable reason for triggering this build.",
    )
    environment: str = Field(
        default="ci",
        description="Target environment for the build (ci, staging, etc.).",
    )


class BuildStatusInput(BaseModel):
    """Input schema for BuildStatusTool."""

    build_id: Optional[str] = Field(
        default=None,
        description="Optional build ID to check. If not provided, returns the latest build status.",
    )


class BuildLogsInput(BaseModel):
    """Input schema for BuildLogsTool."""

    log_file: Optional[str] = Field(
        default=None,
        description=(
            "Optional: specific log file name in the artifacts directory. "
            "If not provided, returns the most recent build log."
        ),
    )
    max_lines: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="Maximum number of log lines to return.",
    )
    tail: bool = Field(
        default=True,
        description="If True, return the last N lines (tail). If False, return the first N lines.",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class BuildTriggerTool(BaseTool):
    """
    Request a build by recording a build trigger in the workspace.

    In a full FlowOS deployment, this publishes a BUILD_TRIGGERED Kafka event.
    In the AI reasoning context, it records the build request in the workspace
    artifacts directory so the build runner can pick it up.
    """

    name: str = "build_trigger"
    description: str = (
        "Request a build or test run. Records the build request in the workspace. "
        "The build runner will execute the command and store results in artifacts/. "
        "Input: build_command (required), reason (optional), environment (optional)."
    )
    args_schema: Type[BaseModel] = BuildTriggerInput

    workspace_root: str = Field(default="", description="Workspace root path.")
    task_id: str = Field(default="", description="Current task ID.")
    workflow_id: str = Field(default="", description="Current workflow ID.")
    agent_id: str = Field(default="", description="Current agent ID.")

    def _run(
        self,
        build_command: str,
        reason: str = "AI-triggered build",
        environment: str = "ci",
        **kwargs: Any,
    ) -> str:
        """Record a build trigger request."""
        root = Path(self.workspace_root or os.getcwd())
        artifacts_dir = root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        trigger = {
            "build_command": build_command,
            "reason": reason,
            "environment": environment,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "agent_id": self.agent_id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }

        trigger_path = artifacts_dir / "build_trigger.json"
        try:
            trigger_path.write_text(
                json.dumps(trigger, indent=2), encoding="utf-8"
            )
            logger.info(
                "Build trigger recorded | command=%s task_id=%s",
                build_command,
                self.task_id,
            )
            return (
                f"Build trigger recorded successfully.\n"
                f"Command: {build_command}\n"
                f"Environment: {environment}\n"
                f"Reason: {reason}\n"
                f"Status: pending\n"
                f"The build runner will execute this command and store results in artifacts/."
            )
        except OSError as exc:
            logger.error("Failed to record build trigger: %s", exc)
            return f"ERROR: Failed to record build trigger: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


class BuildStatusTool(BaseTool):
    """
    Check the status of the most recent build.

    Reads build status from the workspace artifacts directory.
    Returns a structured summary of the build outcome.
    """

    name: str = "build_status"
    description: str = (
        "Check the status of the most recent build or a specific build. "
        "Returns build outcome (passed/failed), duration, and a brief summary. "
        "Input: build_id (optional)."
    )
    args_schema: Type[BaseModel] = BuildStatusInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        build_id: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Check build status from workspace artifacts."""
        root = Path(self.workspace_root or os.getcwd())
        artifacts_dir = root / "artifacts"

        if not artifacts_dir.exists():
            return "No artifacts directory found. No builds have been run yet."

        # Look for build result files
        result_files = sorted(
            artifacts_dir.glob("build_result*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not result_files:
            # Check for a trigger file (build pending)
            trigger_file = artifacts_dir / "build_trigger.json"
            if trigger_file.exists():
                try:
                    trigger = json.loads(trigger_file.read_text(encoding="utf-8"))
                    return (
                        f"Build Status: PENDING\n"
                        f"Command: {trigger.get('build_command', 'unknown')}\n"
                        f"Requested at: {trigger.get('requested_at', 'unknown')}\n"
                        f"The build has been requested but not yet executed."
                    )
                except (json.JSONDecodeError, OSError):
                    pass
            return "No build results found. No builds have completed yet."

        # Read the most recent result
        result_file = result_files[0]
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
            status = result.get("status", "unknown").upper()
            command = result.get("command", "unknown")
            duration = result.get("duration_seconds", "unknown")
            completed_at = result.get("completed_at", "unknown")
            exit_code = result.get("exit_code", "unknown")
            summary = result.get("summary", "")

            output = [
                f"Build Status: {status}",
                f"Command: {command}",
                f"Exit Code: {exit_code}",
                f"Duration: {duration}s",
                f"Completed: {completed_at}",
            ]
            if summary:
                output.append(f"Summary: {summary}")

            return "\n".join(output)

        except (json.JSONDecodeError, OSError) as exc:
            return f"ERROR: Failed to read build result: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


class BuildLogsTool(BaseTool):
    """
    Read build logs from the workspace artifacts directory.

    Returns the content of build log files.  Supports reading the most
    recent log or a specific log file.  Can return the head or tail of
    the log for large files.
    """

    name: str = "build_logs"
    description: str = (
        "Read build logs from the workspace artifacts directory. "
        "Returns log content to help diagnose build failures or understand test results. "
        "Input: log_file (optional, defaults to most recent), "
        "max_lines (default 200), tail (default True for last N lines)."
    )
    args_schema: Type[BaseModel] = BuildLogsInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        log_file: Optional[str] = None,
        max_lines: int = 200,
        tail: bool = True,
        **kwargs: Any,
    ) -> str:
        """Read build logs from the artifacts directory."""
        root = Path(self.workspace_root or os.getcwd())
        artifacts_dir = root / "artifacts"

        if not artifacts_dir.exists():
            return "No artifacts directory found. No build logs available."

        # Find the target log file
        if log_file:
            target = artifacts_dir / log_file
            if not target.exists():
                return f"ERROR: Log file '{log_file}' not found in artifacts/."
        else:
            # Find the most recent log file
            log_files = sorted(
                [
                    f for f in artifacts_dir.iterdir()
                    if f.is_file() and f.suffix in (".log", ".txt")
                    and "build" in f.name.lower()
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not log_files:
                # Fall back to any log file
                log_files = sorted(
                    [f for f in artifacts_dir.iterdir() if f.is_file() and f.suffix == ".log"],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            if not log_files:
                return "No build log files found in artifacts/."
            target = log_files[0]

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            total_lines = len(lines)

            if total_lines <= max_lines:
                return f"=== {target.name} ({total_lines} lines) ===\n{content}"

            if tail:
                selected = lines[-max_lines:]
                header = f"=== {target.name} (showing last {max_lines} of {total_lines} lines) ===\n"
            else:
                selected = lines[:max_lines]
                header = f"=== {target.name} (showing first {max_lines} of {total_lines} lines) ===\n"

            return header + "\n".join(selected)

        except OSError as exc:
            return f"ERROR: Failed to read log file: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def get_build_tools(
    workspace_root: str | None = None,
    task_id: str = "",
    workflow_id: str = "",
    agent_id: str = "",
) -> list[BaseTool]:
    """
    Return all build tools configured for the given workspace.

    Args:
        workspace_root: Absolute path to the workspace root.
        task_id:        Current task ID (for build trigger metadata).
        workflow_id:    Current workflow ID.
        agent_id:       Current agent ID.

    Returns:
        List of configured LangChain tool instances.
    """
    root = workspace_root or os.getcwd()
    return [
        BuildTriggerTool(
            workspace_root=root,
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
        ),
        BuildStatusTool(workspace_root=root),
        BuildLogsTool(workspace_root=root),
    ]
