"""
ai/tools/search_tool.py — FlowOS LangChain Search Tools

Provides LangChain tools that allow AI reasoning agents to search the
codebase, documentation, and external resources.

Tools:
    CodeSearchTool  — Search for patterns in the codebase using grep/ripgrep.
    FileSearchTool  — Find files by name pattern in the workspace.
    WebSearchTool   — Search the web for documentation and references.

Usage::

    from ai.tools.search_tool import get_search_tools

    tools = get_search_tools(workspace_root="/flowos/workspaces/agent-abc")
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


class CodeSearchInput(BaseModel):
    """Input schema for CodeSearchTool."""

    pattern: str = Field(
        description=(
            "The search pattern (regex or literal string) to look for in the codebase."
        )
    )
    file_pattern: Optional[str] = Field(
        default=None,
        description=(
            "Optional glob pattern to restrict search to specific file types "
            "(e.g. '*.py', '*.ts', '*.yaml')."
        ),
    )
    case_sensitive: bool = Field(
        default=False,
        description="If True, perform a case-sensitive search. Default is case-insensitive.",
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of matching lines to return.",
    )
    context_lines: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Number of context lines to show before and after each match.",
    )


class FileSearchInput(BaseModel):
    """Input schema for FileSearchTool."""

    name_pattern: str = Field(
        description=(
            "Glob pattern to match file names (e.g. '*.py', 'config*.yaml', 'test_*.py')."
        )
    )
    search_path: Optional[str] = Field(
        default=None,
        description=(
            "Optional subdirectory to search within. "
            "Defaults to the entire workspace."
        ),
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of files to return.",
    )
    include_dirs: bool = Field(
        default=False,
        description="If True, also include matching directories.",
    )


class WebSearchInput(BaseModel):
    """Input schema for WebSearchTool."""

    query: str = Field(
        description=(
            "The search query to look up. Be specific and include relevant "
            "technology names, error messages, or concepts."
        )
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of search results to return.",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class CodeSearchTool(BaseTool):
    """
    Search for patterns in the codebase.

    Uses ripgrep (rg) if available, falling back to grep.  Returns matching
    lines with file paths and line numbers.  Useful for finding function
    definitions, usages, error patterns, and configuration values.
    """

    name: str = "code_search"
    description: str = (
        "Search for a pattern (regex or literal string) in the codebase. "
        "Returns matching lines with file paths and line numbers. "
        "Use this to find function definitions, usages, error patterns, or config values. "
        "Input: pattern (required), file_pattern (optional, e.g. '*.py'), "
        "case_sensitive (default False), max_results (default 50), "
        "context_lines (default 2)."
    )
    args_schema: Type[BaseModel] = CodeSearchInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        pattern: str,
        file_pattern: Optional[str] = None,
        case_sensitive: bool = False,
        max_results: int = 50,
        context_lines: int = 2,
        **kwargs: Any,
    ) -> str:
        """Execute the code_search tool."""
        search_root = self._get_search_root()

        # Try ripgrep first, fall back to grep
        if self._has_ripgrep():
            return self._search_with_ripgrep(
                pattern, search_root, file_pattern, case_sensitive, max_results, context_lines
            )
        return self._search_with_grep(
            pattern, search_root, file_pattern, case_sensitive, max_results, context_lines
        )

    def _search_with_ripgrep(
        self,
        pattern: str,
        search_root: Path,
        file_pattern: Optional[str],
        case_sensitive: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        """Search using ripgrep."""
        cmd = ["rg", "--line-number", "--no-heading", f"--context={context_lines}"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if file_pattern:
            cmd += ["--glob", file_pattern]
        cmd += [pattern, str(search_root)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if not output.strip():
                return f"No matches found for pattern: {pattern!r}"

            lines = output.splitlines()
            if len(lines) > max_results:
                lines = lines[:max_results]
                output = "\n".join(lines) + f"\n\n[... truncated at {max_results} results ...]"
            else:
                output = "\n".join(lines)

            return output

        except subprocess.TimeoutExpired:
            return "ERROR: Search timed out."
        except Exception as exc:  # noqa: BLE001
            logger.error("ripgrep search error: %s", exc)
            return f"ERROR: {exc}"

    def _search_with_grep(
        self,
        pattern: str,
        search_root: Path,
        file_pattern: Optional[str],
        case_sensitive: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        """Search using grep."""
        cmd = ["grep", "-rn", f"--context={context_lines}"]
        if not case_sensitive:
            cmd.append("-i")
        if file_pattern:
            cmd += ["--include", file_pattern]
        cmd += [pattern, str(search_root)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 1:
                return f"No matches found for pattern: {pattern!r}"
            if result.returncode > 1:
                return f"ERROR: {result.stderr.strip()}"

            lines = result.stdout.splitlines()
            if len(lines) > max_results:
                lines = lines[:max_results]
                return "\n".join(lines) + f"\n\n[... truncated at {max_results} results ...]"
            return result.stdout

        except subprocess.TimeoutExpired:
            return "ERROR: Search timed out."
        except FileNotFoundError:
            return "ERROR: grep executable not found."
        except Exception as exc:  # noqa: BLE001
            logger.error("grep search error: %s", exc)
            return f"ERROR: {exc}"

    def _get_search_root(self) -> Path:
        """Return the root directory to search in."""
        root = Path(self.workspace_root or os.getcwd())
        # Prefer the repo subdirectory if it exists
        repo_dir = root / "repo"
        return repo_dir if repo_dir.exists() else root

    @staticmethod
    def _has_ripgrep() -> bool:
        """Check if ripgrep is available."""
        try:
            subprocess.run(
                ["rg", "--version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


class FileSearchTool(BaseTool):
    """
    Find files by name pattern in the workspace.

    Returns a list of matching file paths.  Useful for discovering
    configuration files, test files, or any files matching a pattern.
    """

    name: str = "file_search"
    description: str = (
        "Find files by name pattern in the workspace. "
        "Returns a list of matching file paths. "
        "Use this to discover configuration files, test files, or source files. "
        "Input: name_pattern (required, e.g. '*.py', 'config*.yaml'), "
        "search_path (optional subdirectory), max_results (default 50)."
    )
    args_schema: Type[BaseModel] = FileSearchInput

    workspace_root: str = Field(default="", description="Workspace root path.")

    def _run(
        self,
        name_pattern: str,
        search_path: Optional[str] = None,
        max_results: int = 50,
        include_dirs: bool = False,
        **kwargs: Any,
    ) -> str:
        """Execute the file_search tool."""
        root = Path(self.workspace_root or os.getcwd())

        if search_path:
            # Resolve relative to workspace root
            search_dir = (root / search_path).resolve()
            # Security: ensure we stay within the workspace
            try:
                search_dir.relative_to(root)
            except ValueError:
                return "ERROR: search_path must be within the workspace."
        else:
            # Default to repo/ subdirectory if it exists
            search_dir = root / "repo" if (root / "repo").exists() else root

        if not search_dir.exists():
            return f"ERROR: Search directory does not exist: {search_dir}"

        try:
            matches: list[str] = []
            for item in search_dir.rglob(name_pattern):
                if item.is_file() or (include_dirs and item.is_dir()):
                    # Return path relative to workspace root
                    try:
                        rel_path = item.relative_to(root)
                        matches.append(str(rel_path))
                    except ValueError:
                        matches.append(str(item))

                if len(matches) >= max_results:
                    break

            if not matches:
                return f"No files found matching pattern: {name_pattern!r}"

            result = f"Found {len(matches)} file(s) matching '{name_pattern}':\n"
            result += "\n".join(f"  {m}" for m in sorted(matches))
            if len(matches) >= max_results:
                result += f"\n\n[... results truncated at {max_results} ...]"
            return result

        except OSError as exc:
            return f"ERROR: File search failed: {exc}"

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


class WebSearchTool(BaseTool):
    """
    Search the web for documentation, error messages, and references.

    This tool provides a structured interface for web searches.  In
    production, it integrates with a search API (e.g. Tavily, SerpAPI).
    When no API key is configured, it returns a helpful message indicating
    what would be searched.

    The tool is designed to be used when the AI needs external context
    that is not available in the workspace (e.g. library documentation,
    known bug reports, best practices).
    """

    name: str = "web_search"
    description: str = (
        "Search the web for documentation, error messages, library references, "
        "or best practices. Use this when you need external context not available "
        "in the codebase. "
        "Input: query (required), max_results (default 5)."
    )
    args_schema: Type[BaseModel] = WebSearchInput

    api_key: Optional[str] = Field(
        default=None,
        description="API key for the search provider (Tavily, SerpAPI, etc.).",
    )
    search_provider: str = Field(
        default="tavily",
        description="Search provider to use (tavily, serpapi, duckduckgo).",
    )

    def _run(
        self,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> str:
        """Execute the web_search tool."""
        # Try Tavily if available
        if self.search_provider == "tavily" and self.api_key:
            return self._search_tavily(query, max_results)

        # Try DuckDuckGo as a fallback (no API key required)
        return self._search_duckduckgo(query, max_results)

    def _search_tavily(self, query: str, max_results: int) -> str:
        """Search using Tavily API."""
        try:
            from langchain_community.tools.tavily_search import TavilySearchResults
            import os

            os.environ.setdefault("TAVILY_API_KEY", self.api_key or "")
            tavily = TavilySearchResults(max_results=max_results)
            results = tavily.run(query)

            if isinstance(results, list):
                output_lines = [f"Web search results for: {query!r}\n"]
                for i, r in enumerate(results, 1):
                    if isinstance(r, dict):
                        title = r.get("title", "No title")
                        url = r.get("url", "")
                        content = r.get("content", "")[:300]
                        output_lines.append(f"{i}. {title}")
                        output_lines.append(f"   URL: {url}")
                        output_lines.append(f"   {content}")
                        output_lines.append("")
                return "\n".join(output_lines)
            return str(results)

        except ImportError:
            logger.debug("Tavily not available, falling back to DuckDuckGo")
            return self._search_duckduckgo(query, max_results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily search failed: %s", exc)
            return self._search_duckduckgo(query, max_results)

    def _search_duckduckgo(self, query: str, max_results: int) -> str:
        """Search using DuckDuckGo (no API key required)."""
        try:
            from langchain_community.tools import DuckDuckGoSearchRun

            ddg = DuckDuckGoSearchRun()
            result = ddg.run(query)
            return f"Web search results for: {query!r}\n\n{result}"

        except ImportError:
            # No search provider available — return a helpful message
            return (
                f"Web search requested for: {query!r}\n\n"
                "NOTE: No web search provider is configured. "
                "To enable web search, install langchain-community and configure "
                "a search API key (TAVILY_API_KEY or SERPAPI_API_KEY).\n\n"
                "Suggested search terms to look up manually:\n"
                f"  - {query}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DuckDuckGo search failed: %s", exc)
            return (
                f"Web search for {query!r} failed: {exc}\n"
                "Please search manually for this query."
            )

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def get_search_tools(
    workspace_root: str | None = None,
    web_search_api_key: str | None = None,
    web_search_provider: str = "tavily",
) -> list[BaseTool]:
    """
    Return all search tools configured for the given workspace.

    Args:
        workspace_root:        Absolute path to the workspace root.
        web_search_api_key:    Optional API key for web search provider.
        web_search_provider:   Search provider name (tavily, serpapi, duckduckgo).

    Returns:
        List of configured LangChain tool instances.
    """
    root = workspace_root or os.getcwd()
    # Also check environment for API key
    api_key = web_search_api_key or os.environ.get("TAVILY_API_KEY") or os.environ.get("SERPAPI_API_KEY")

    return [
        CodeSearchTool(workspace_root=root),
        FileSearchTool(workspace_root=root),
        WebSearchTool(
            api_key=api_key,
            search_provider=web_search_provider,
        ),
    ]
