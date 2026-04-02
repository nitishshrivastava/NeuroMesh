"""
ai/tools/artifact_tool.py — FlowOS LangChain Artifact Tools

Provides LangChain tools that allow AI reasoning agents to read and write
build artifacts, generated files, and workspace outputs.

Artifacts are stored in the ``artifacts/`` subdirectory of the workspace.
They include build logs, test results, generated patches, reports, and
any other files produced during task execution.

Tools:
    ArtifactReadTool  — Read the contents of an artifact file.
    ArtifactWriteTool — Write content to an artifact file.
    ArtifactListTool  — List available artifacts in the workspace.

Usage::

    from ai.tools.artifact_tool import get_artifact_tools

    tools = get_artifact_tools(workspace_root="/flowos/workspaces/agent-abc")
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

# Maximum file size to read (5 MB)
MAX_READ_SIZE_BYTES = 5 * 1024 * 1024

# Allowed artifact subdirectories (security: prevent path traversal)
ALLOWED_ARTIFACT_DIRS = {"artifacts", "patches", "logs"}


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class ArtifactReadInput(BaseModel):
    """Input schema for ArtifactReadTool."""

    artifact_name: str = Field(
        description=(
            "Name of the artifact file to read (e.g. 'build.log', 'test_results.xml', "
            "'proposed.patch'). Can include a subdirectory prefix like 'patches/fix.patch'."
        )
    )
    max_lines: int = Field(
        default=300,
        ge=1,
        le=5000,
        description="Maximum number of lines to return.",
    )
    tail: bool = Field(
        default=False,
        description="If True, return the last N lines. If False, return the first N lines.",
    )


class ArtifactWriteInput(BaseModel):
    """Input schema for ArtifactWriteTool."""

    artifact_name: str = Field(
        description=(
            "Name of the artifact file to write (e.g. 'analysis.md', 'proposed.patch'). "
            "Will be created in the artifacts/ directory."
        )
    )
    content: str = Field(
        description="The content to write to the artifact file.",
    )
    artifact_type: str = Field(
        default="report",
        description=(
            "Type of artifact: 'report', 'patch', 'analysis', 'plan', 'log'. "
            "Used for metadata tagging."
        ),
    )
    append: bool = Field(
        default=False,
        description="If True, append to an existing file. If False, overwrite.",
    )


class ArtifactListInput(BaseModel):
    """Input schema for ArtifactListTool."""

    artifact_type: Optional[str] = Field(
        default=None,
        description=(
            "Optional filter by artifact type: 'build_log', 'test_result', "
            "'patch', 'report', 'analysis'. If not provided, lists all artifacts."
        ),
    )
    include_metadata: bool = Field(
        default=True,
        description="If True, include file size and modification time in the listing.",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class ArtifactReadTool(BaseTool):
    """
    Read the contents of an artifact file from the workspace.

    Supports reading build logs, test results, patches, reports, and any
    other files stored in the artifacts/ directory.
    """

    name: str = "artifact_read"
    description: str = (
        "Read the contents of an artifact file from the workspace artifacts/ directory. "
        "Use this to inspect build logs, test results, patches, or generated reports. "
        "Input: artifact_name (required, e.g. 'build.log', 'test_results.xml'), "
        "max_lines (default 300), tail (default False for first N lines)."
    )
    args_schema: Type[BaseModel] = ArtifactReadInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        artifact_name: str,
        max_lines: int = 300,
        tail: bool = False,
        **kwargs: Any,
    ) -> str:
        """Read an artifact file."""
        root = Path(self.workspace_root or os.getcwd())
        artifact_path = self._resolve_artifact_path(root, artifact_name)

        if artifact_path is None:
            return f"ERROR: Invalid artifact path: {artifact_name!r}. Path traversal not allowed."

        if not artifact_path.exists():
            # Try to find the file by name in any artifact directory
            found = self._find_artifact(root, artifact_name)
            if found:
                artifact_path = found
            else:
                return (
                    f"Artifact '{artifact_name}' not found. "
                    f"Use artifact_list to see available artifacts."
                )

        if not artifact_path.is_file():
            return f"ERROR: '{artifact_name}' is not a file."

        # Check file size
        size = artifact_path.stat().st_size
        if size > MAX_READ_SIZE_BYTES:
            return (
                f"ERROR: Artifact '{artifact_name}' is too large to read "
                f"({size / 1024 / 1024:.1f} MB). Maximum is 5 MB."
            )

        try:
            content = artifact_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            total_lines = len(lines)

            if total_lines <= max_lines:
                return f"=== {artifact_name} ({total_lines} lines) ===\n{content}"

            if tail:
                selected = lines[-max_lines:]
                header = (
                    f"=== {artifact_name} (showing last {max_lines} of {total_lines} lines) ===\n"
                )
            else:
                selected = lines[:max_lines]
                header = (
                    f"=== {artifact_name} (showing first {max_lines} of {total_lines} lines) ===\n"
                )

            return header + "\n".join(selected)

        except OSError as exc:
            return f"ERROR: Failed to read artifact: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)

    def _resolve_artifact_path(self, root: Path, artifact_name: str) -> Path | None:
        """
        Resolve the artifact path, preventing path traversal attacks.

        Returns None if the path is invalid or outside allowed directories.
        """
        # Normalise the artifact name
        clean_name = artifact_name.lstrip("/").lstrip("./")

        # Check for path traversal attempts
        if ".." in clean_name:
            return None

        # Try artifacts/ first
        candidate = (root / "artifacts" / clean_name).resolve()
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            return None

    def _find_artifact(self, root: Path, artifact_name: str) -> Path | None:
        """Search for an artifact by name across all artifact directories."""
        name = Path(artifact_name).name  # Just the filename, no directory
        for dir_name in ALLOWED_ARTIFACT_DIRS:
            dir_path = root / dir_name
            if dir_path.exists():
                for f in dir_path.rglob(name):
                    if f.is_file():
                        return f
        return None


class ArtifactWriteTool(BaseTool):
    """
    Write content to an artifact file in the workspace.

    Creates or overwrites files in the artifacts/ directory.  Used by AI
    agents to save analysis reports, proposed patches, plans, and other
    generated outputs.
    """

    name: str = "artifact_write"
    description: str = (
        "Write content to an artifact file in the workspace artifacts/ directory. "
        "Use this to save analysis reports, proposed patches, plans, or any generated output. "
        "Input: artifact_name (required), content (required), "
        "artifact_type (default 'report'), append (default False)."
    )
    args_schema: Type[BaseModel] = ArtifactWriteInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        artifact_name: str,
        content: str,
        artifact_type: str = "report",
        append: bool = False,
        **kwargs: Any,
    ) -> str:
        """Write content to an artifact file."""
        root = Path(self.workspace_root or os.getcwd())

        # Validate artifact name
        clean_name = artifact_name.lstrip("/").lstrip("./")
        if ".." in clean_name:
            return "ERROR: Invalid artifact name. Path traversal not allowed."

        artifacts_dir = root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = artifacts_dir / clean_name
        # Ensure parent directories exist (for nested paths)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        # Security: ensure we stay within artifacts/
        try:
            artifact_path.resolve().relative_to(artifacts_dir.resolve())
        except ValueError:
            return "ERROR: Artifact path must be within the artifacts/ directory."

        try:
            mode = "a" if append else "w"
            with artifact_path.open(mode, encoding="utf-8") as f:
                if append and artifact_path.stat().st_size > 0:
                    f.write("\n")  # Separator when appending
                f.write(content)

            # Write metadata sidecar
            meta_path = artifact_path.with_suffix(artifact_path.suffix + ".meta.json")
            meta = {
                "artifact_name": clean_name,
                "artifact_type": artifact_type,
                "size_bytes": artifact_path.stat().st_size,
                "written_at": datetime.now(timezone.utc).isoformat(),
                "appended": append,
            }
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            action = "appended to" if append else "written to"
            size = artifact_path.stat().st_size
            logger.info(
                "Artifact %s | name=%s type=%s size=%d",
                action,
                clean_name,
                artifact_type,
                size,
            )
            return (
                f"Successfully {action} artifact '{clean_name}'.\n"
                f"Type: {artifact_type}\n"
                f"Size: {size} bytes\n"
                f"Path: artifacts/{clean_name}"
            )

        except OSError as exc:
            logger.error("Failed to write artifact '%s': %s", artifact_name, exc)
            return f"ERROR: Failed to write artifact: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


class ArtifactListTool(BaseTool):
    """
    List available artifacts in the workspace.

    Returns a formatted list of artifact files with their types, sizes,
    and modification times.  Useful for discovering what outputs are
    available before reading them.
    """

    name: str = "artifact_list"
    description: str = (
        "List available artifact files in the workspace. "
        "Returns file names, types, sizes, and modification times. "
        "Use this to discover what build logs, test results, or reports are available. "
        "Input: artifact_type (optional filter), include_metadata (default True)."
    )
    args_schema: Type[BaseModel] = ArtifactListInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        artifact_type: Optional[str] = None,
        include_metadata: bool = True,
        **kwargs: Any,
    ) -> str:
        """List artifacts in the workspace."""
        root = Path(self.workspace_root or os.getcwd())

        all_artifacts: list[dict[str, Any]] = []

        for dir_name in ALLOWED_ARTIFACT_DIRS:
            dir_path = root / dir_name
            if not dir_path.exists():
                continue

            for entry in sorted(dir_path.rglob("*")):
                if not entry.is_file():
                    continue
                # Skip metadata sidecar files
                if entry.name.endswith(".meta.json"):
                    continue

                # Determine artifact type
                inferred_type = self._infer_type(entry)

                # Apply type filter
                if artifact_type and inferred_type != artifact_type:
                    continue

                try:
                    rel_path = entry.relative_to(root)
                except ValueError:
                    rel_path = entry

                artifact_info: dict[str, Any] = {
                    "name": str(rel_path),
                    "type": inferred_type,
                }

                if include_metadata:
                    stat = entry.stat()
                    artifact_info["size_bytes"] = stat.st_size
                    artifact_info["modified_at"] = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC")

                    # Check for metadata sidecar
                    meta_path = entry.with_suffix(entry.suffix + ".meta.json")
                    if meta_path.exists():
                        try:
                            meta = json.loads(meta_path.read_text(encoding="utf-8"))
                            artifact_info["type"] = meta.get("artifact_type", inferred_type)
                        except (json.JSONDecodeError, OSError):
                            pass

                all_artifacts.append(artifact_info)

        if not all_artifacts:
            filter_msg = f" of type '{artifact_type}'" if artifact_type else ""
            return f"No artifacts{filter_msg} found in workspace."

        lines = [f"Found {len(all_artifacts)} artifact(s):\n"]
        for a in all_artifacts:
            if include_metadata:
                size_kb = a["size_bytes"] / 1024
                lines.append(
                    f"  [{a['type']:12s}] {a['name']:<40s} "
                    f"{size_kb:8.1f} KB  {a.get('modified_at', '')}"
                )
            else:
                lines.append(f"  [{a['type']:12s}] {a['name']}")

        return "\n".join(lines)

    @staticmethod
    def _infer_type(path: Path) -> str:
        """Infer artifact type from file name and extension."""
        name_lower = path.name.lower()
        suffix = path.suffix.lower()

        if suffix in (".patch", ".diff"):
            return "patch"
        if suffix == ".log" or "build" in name_lower:
            return "build_log"
        if "test" in name_lower and suffix in (".xml", ".json", ".txt"):
            return "test_result"
        if suffix in (".md", ".html", ".rst"):
            return "report"
        if "analysis" in name_lower or "plan" in name_lower:
            return "analysis"
        return "artifact"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def get_artifact_tools(workspace_root: str | None = None) -> list[BaseTool]:
    """
    Return all artifact tools configured for the given workspace.

    Args:
        workspace_root: Absolute path to the workspace root.
                        Defaults to the current working directory.

    Returns:
        List of configured LangChain tool instances.
    """
    root = workspace_root or os.getcwd()
    return [
        ArtifactReadTool(workspace_root=root),
        ArtifactWriteTool(workspace_root=root),
        ArtifactListTool(workspace_root=root),
    ]
