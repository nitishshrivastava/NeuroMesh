"""
ai/tools/__init__.py — FlowOS LangChain AI Tools

Exposes the four core tools available to AI reasoning agents:
- git_tool:      Read Git history, diffs, file contents, and branch state.
- build_tool:    Trigger builds, run tests, and retrieve build logs.
- search_tool:   Search the codebase, documentation, and web resources.
- artifact_tool: Read and write build artifacts and generated files.
"""

from __future__ import annotations

from ai.tools.git_tool import (
    GitReadFileTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
    get_git_tools,
)
from ai.tools.build_tool import (
    BuildTriggerTool,
    BuildStatusTool,
    BuildLogsTool,
    get_build_tools,
)
from ai.tools.search_tool import (
    CodeSearchTool,
    FileSearchTool,
    WebSearchTool,
    get_search_tools,
)
from ai.tools.artifact_tool import (
    ArtifactReadTool,
    ArtifactWriteTool,
    ArtifactListTool,
    get_artifact_tools,
)

__all__ = [
    # Git tools
    "GitReadFileTool",
    "GitDiffTool",
    "GitLogTool",
    "GitStatusTool",
    "get_git_tools",
    # Build tools
    "BuildTriggerTool",
    "BuildStatusTool",
    "BuildLogsTool",
    "get_build_tools",
    # Search tools
    "CodeSearchTool",
    "FileSearchTool",
    "WebSearchTool",
    "get_search_tools",
    # Artifact tools
    "ArtifactReadTool",
    "ArtifactWriteTool",
    "ArtifactListTool",
    "get_artifact_tools",
]
