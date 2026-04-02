"""
ai/graphs/code_review.py — FlowOS LangGraph Code Review Reasoning Graph

Implements a multi-step LangGraph reasoning graph for AI-driven code review.
The graph analyses code changes, identifies issues, and produces structured
review findings with severity ratings and actionable suggestions.

Graph nodes:
    load_context      — Load and validate the task context.
    analyse_changes   — Analyse the diff and changed files.
    security_check    — Check for security vulnerabilities.
    quality_check     — Check code quality, style, and best practices.
    synthesise        — Synthesise findings into a structured review.
    emit_suggestion   — Emit the AI_SUGGESTION_CREATED Kafka event.

State transitions::

    load_context → analyse_changes → security_check → quality_check
                                                     ↓
                                              synthesise → emit_suggestion → END

Usage::

    from ai.graphs.code_review import build_code_review_graph, CodeReviewState
    from ai.context_loader import TaskContext

    graph = build_code_review_graph(llm=my_llm, workspace_root="/path/to/workspace")
    result = graph.invoke({
        "task_context": ctx,
        "task_id": "task-xyz",
        "workflow_id": "wf-123",
        "agent_id": "agent-abc",
    })
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from ai.context_loader import TaskContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------


class CodeReviewState(TypedDict):
    """
    State for the code review reasoning graph.

    Attributes:
        task_context:       The assembled task context.
        task_id:            Task identifier.
        workflow_id:        Workflow identifier.
        agent_id:           AI agent identifier.
        messages:           LangGraph message history.
        diff_content:       The code diff being reviewed.
        changed_files:      List of changed file paths.
        security_findings:  Security issues found during review.
        quality_findings:   Code quality issues found during review.
        review_findings:    All consolidated review findings.
        review_outcome:     Overall outcome: approved, rejected, needs_changes.
        review_summary:     Human-readable review summary.
        suggestion_id:      ID of the AI suggestion created.
        error:              Error message if the graph fails.
        step_count:         Number of reasoning steps taken.
        reasoning_steps:    List of reasoning step records.
    """

    task_context: TaskContext
    task_id: str
    workflow_id: str
    agent_id: str
    messages: Annotated[list, add_messages]
    diff_content: str
    changed_files: list[str]
    security_findings: list[dict[str, Any]]
    quality_findings: list[dict[str, Any]]
    review_findings: list[dict[str, Any]]
    review_outcome: str
    review_summary: str
    suggestion_id: str
    error: str | None
    step_count: int
    reasoning_steps: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert code reviewer for the FlowOS distributed orchestration platform.
Your role is to review code changes thoroughly and provide actionable, constructive feedback.

When reviewing code, you:
1. Identify security vulnerabilities (injection, auth bypass, data exposure, etc.)
2. Check for correctness issues (logic errors, edge cases, null handling)
3. Evaluate code quality (readability, maintainability, test coverage)
4. Verify adherence to best practices for the language/framework
5. Assess performance implications of the changes

Always be specific: reference file names, line numbers, and exact code snippets.
Rate each finding by severity: critical, high, medium, low, info.
Provide concrete suggestions for how to fix each issue.
"""

_ANALYSE_PROMPT = """Review the following code changes and provide an initial analysis.

Task: {task_description}

Changed Files:
{changed_files}

Code Diff:
```
{diff_content}
```

Provide:
1. A brief summary of what these changes do
2. The primary areas of concern to investigate further
3. Any immediately obvious issues
"""

_SECURITY_PROMPT = """Perform a security review of the following code changes.

Code Diff:
```
{diff_content}
```

Check for:
- SQL injection, command injection, path traversal
- Authentication/authorization bypass
- Sensitive data exposure (credentials, PII, secrets)
- Insecure cryptography or random number generation
- Input validation issues
- Dependency vulnerabilities
- Race conditions or TOCTOU issues

For each security issue found, provide:
- severity: critical/high/medium/low
- category: the type of vulnerability
- file: the affected file
- description: what the issue is
- recommendation: how to fix it

If no security issues are found, state that clearly.
"""

_QUALITY_PROMPT = """Perform a code quality review of the following code changes.

Code Diff:
```
{diff_content}
```

Check for:
- Code clarity and readability
- Proper error handling
- Test coverage for new functionality
- Code duplication (DRY violations)
- Performance issues (N+1 queries, unnecessary loops, etc.)
- Documentation and comments
- Naming conventions
- Complexity (cyclomatic complexity, function length)

For each quality issue found, provide:
- severity: high/medium/low/info
- category: the type of issue
- file: the affected file
- description: what the issue is
- recommendation: how to improve it

If the code quality is good, state that clearly.
"""

_SYNTHESISE_PROMPT = """Synthesise the code review findings into a final review decision.

Task: {task_description}

Security Findings:
{security_findings}

Quality Findings:
{quality_findings}

Based on these findings, provide:
1. Overall review outcome: "approved", "needs_changes", or "rejected"
   - approved: no significant issues, changes can be merged
   - needs_changes: issues found that should be addressed before merging
   - rejected: critical issues that must be fixed before the changes can proceed
2. A concise review summary (2-4 sentences)
3. The top 3 most important action items (if any)

Format your response as:
OUTCOME: <approved|needs_changes|rejected>
SUMMARY: <your summary>
ACTION_ITEMS:
- <item 1>
- <item 2>
- <item 3>
"""


# ---------------------------------------------------------------------------
# Graph node functions
# ---------------------------------------------------------------------------


def _load_context_node(state: CodeReviewState) -> dict[str, Any]:
    """Load and validate the task context, extract diff content."""
    logger.info("code_review: load_context | task_id=%s", state.get("task_id"))

    ctx = state.get("task_context")
    if not ctx:
        return {
            "error": "No task context provided.",
            "step_count": state.get("step_count", 0) + 1,
        }

    # Extract diff content from changed files
    diff_parts: list[str] = []
    changed_file_paths: list[str] = []

    for fc in ctx.changed_files[:20]:  # Cap at 20 files
        changed_file_paths.append(fc.path)
        if fc.diff_snippet:
            diff_parts.append(f"--- {fc.path} ({fc.status}) ---\n{fc.diff_snippet}")

    diff_content = "\n\n".join(diff_parts) if diff_parts else "No diff content available."

    step = {
        "step_type": "plan",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Loaded context for code review task. "
            f"Found {len(changed_file_paths)} changed files. "
            f"Diff content: {len(diff_content)} characters."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "diff_content": diff_content,
        "changed_files": changed_file_paths,
        "step_count": state.get("step_count", 0) + 1,
        "reasoning_steps": state.get("reasoning_steps", []) + [step],
        "error": None,
    }


def _make_analyse_node(llm: BaseChatModel) -> Any:
    """Create the analyse_changes node with the given LLM."""

    def _analyse_changes_node(state: CodeReviewState) -> dict[str, Any]:
        """Analyse the code changes and identify areas of concern."""
        if state.get("error"):
            return {}

        logger.info("code_review: analyse_changes | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        task_description = ctx.task_description if ctx else "Code review task"

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_ANALYSE_PROMPT.format(
                task_description=task_description,
                changed_files="\n".join(f"  - {f}" for f in state.get("changed_files", [])),
                diff_content=state.get("diff_content", "No diff available"),
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            analysis = response.content if hasattr(response, "content") else str(response)

            step = {
                "step_type": "thought",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Initial analysis:\n{analysis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "messages": [AIMessage(content=analysis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("analyse_changes failed: %s", exc)
            return {
                "error": f"Analysis failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _analyse_changes_node


def _make_security_node(llm: BaseChatModel) -> Any:
    """Create the security_check node with the given LLM."""

    def _security_check_node(state: CodeReviewState) -> dict[str, Any]:
        """Check for security vulnerabilities in the code changes."""
        if state.get("error"):
            return {}

        logger.info("code_review: security_check | task_id=%s", state.get("task_id"))

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_SECURITY_PROMPT.format(
                diff_content=state.get("diff_content", "No diff available"),
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            security_analysis = response.content if hasattr(response, "content") else str(response)

            # Parse findings from the response
            findings = _parse_findings(security_analysis, "security")

            step = {
                "step_type": "observation",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Security analysis:\n{security_analysis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "security_findings": findings,
                "messages": [AIMessage(content=security_analysis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("security_check failed: %s", exc)
            return {
                "security_findings": [],
                "error": f"Security check failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _security_check_node


def _make_quality_node(llm: BaseChatModel) -> Any:
    """Create the quality_check node with the given LLM."""

    def _quality_check_node(state: CodeReviewState) -> dict[str, Any]:
        """Check code quality, style, and best practices."""
        if state.get("error"):
            return {}

        logger.info("code_review: quality_check | task_id=%s", state.get("task_id"))

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_QUALITY_PROMPT.format(
                diff_content=state.get("diff_content", "No diff available"),
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            quality_analysis = response.content if hasattr(response, "content") else str(response)

            findings = _parse_findings(quality_analysis, "quality")

            step = {
                "step_type": "observation",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Quality analysis:\n{quality_analysis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "quality_findings": findings,
                "messages": [AIMessage(content=quality_analysis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("quality_check failed: %s", exc)
            return {
                "quality_findings": [],
                "error": f"Quality check failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _quality_check_node


def _make_synthesise_node(llm: BaseChatModel) -> Any:
    """Create the synthesise node with the given LLM."""

    def _synthesise_node(state: CodeReviewState) -> dict[str, Any]:
        """Synthesise all findings into a final review decision."""
        logger.info("code_review: synthesise | task_id=%s", state.get("task_id"))

        # If there was an error, produce a failed review
        if state.get("error"):
            return {
                "review_outcome": "needs_changes",
                "review_summary": f"Review incomplete due to error: {state.get('error')}",
                "review_findings": [],
                "step_count": state.get("step_count", 0) + 1,
            }

        ctx = state.get("task_context")
        task_description = ctx.task_description if ctx else "Code review task"

        security_text = _format_findings(state.get("security_findings", []))
        quality_text = _format_findings(state.get("quality_findings", []))

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_SYNTHESISE_PROMPT.format(
                task_description=task_description,
                security_findings=security_text or "No security issues found.",
                quality_findings=quality_text or "No quality issues found.",
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            synthesis = response.content if hasattr(response, "content") else str(response)

            # Parse outcome and summary from response
            outcome, summary = _parse_synthesis(synthesis)

            # Combine all findings
            all_findings = (
                state.get("security_findings", []) + state.get("quality_findings", [])
            )

            step = {
                "step_type": "final_answer",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Review synthesis:\n{synthesis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "review_outcome": outcome,
                "review_summary": summary,
                "review_findings": all_findings,
                "messages": [AIMessage(content=synthesis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("synthesise failed: %s", exc)
            return {
                "review_outcome": "needs_changes",
                "review_summary": f"Review synthesis failed: {exc}",
                "review_findings": (
                    state.get("security_findings", []) + state.get("quality_findings", [])
                ),
                "step_count": state.get("step_count", 0) + 1,
            }

    return _synthesise_node


def _emit_suggestion_node(state: CodeReviewState) -> dict[str, Any]:
    """
    Emit the AI_SUGGESTION_CREATED Kafka event with the review results.

    This node publishes the review outcome to the Kafka AI events topic
    so the orchestrator and UI can react to the completed review.
    """
    logger.info("code_review: emit_suggestion | task_id=%s", state.get("task_id"))

    suggestion_id = str(uuid.uuid4())

    try:
        from shared.kafka.producer import get_producer
        from shared.kafka.schemas import AISuggestionCreatedPayload, AIReviewCompletedPayload, build_event
        from shared.models.event import EventSource, EventType

        producer = get_producer()

        # Emit AI_SUGGESTION_CREATED
        suggestion_payload = AISuggestionCreatedPayload(
            task_id=state.get("task_id", ""),
            workflow_id=state.get("workflow_id", ""),
            agent_id=state.get("agent_id", ""),
            suggestion_id=suggestion_id,
            suggestion_type="code_review",
            content=state.get("review_summary", ""),
            confidence=_outcome_to_confidence(state.get("review_outcome", "needs_changes")),
            reasoning=f"Review outcome: {state.get('review_outcome')}. "
                      f"Findings: {len(state.get('review_findings', []))} issues found.",
        )
        suggestion_event = build_event(
            event_type=EventType.AI_SUGGESTION_CREATED,
            payload=suggestion_payload,
            source=EventSource.AI_AGENT,
            task_id=state.get("task_id"),
            workflow_id=state.get("workflow_id"),
            agent_id=state.get("agent_id"),
        )
        producer.produce(suggestion_event)

        # Emit AI_REVIEW_COMPLETED
        review_payload = AIReviewCompletedPayload(
            task_id=state.get("task_id", ""),
            workflow_id=state.get("workflow_id", ""),
            agent_id=state.get("agent_id", ""),
            review_outcome=state.get("review_outcome", "needs_changes"),
            findings=state.get("review_findings", []),
            summary=state.get("review_summary", ""),
        )
        review_event = build_event(
            event_type=EventType.AI_REVIEW_COMPLETED,
            payload=review_payload,
            source=EventSource.AI_AGENT,
            task_id=state.get("task_id"),
            workflow_id=state.get("workflow_id"),
            agent_id=state.get("agent_id"),
        )
        producer.produce(review_event)
        producer.flush(timeout=10.0)

        logger.info(
            "code_review: emitted suggestion | suggestion_id=%s outcome=%s",
            suggestion_id,
            state.get("review_outcome"),
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit Kafka events: %s", exc)
        # Don't fail the graph — the review result is still valid

    step = {
        "step_type": "proposal",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Emitted AI_SUGGESTION_CREATED event. "
            f"Suggestion ID: {suggestion_id}. "
            f"Review outcome: {state.get('review_outcome')}."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "suggestion_id": suggestion_id,
        "step_count": state.get("step_count", 0) + 1,
        "reasoning_steps": state.get("reasoning_steps", []) + [step],
    }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _parse_findings(text: str, category: str) -> list[dict[str, Any]]:
    """
    Parse structured findings from LLM response text.

    Extracts severity, file, description, and recommendation from
    the free-form LLM output.
    """
    findings: list[dict[str, Any]] = []

    # Simple heuristic: look for severity keywords
    severity_keywords = ["critical", "high", "medium", "low", "info"]
    lines = text.splitlines()

    current_finding: dict[str, Any] | None = None

    for line in lines:
        line_lower = line.lower().strip()

        # Detect severity markers
        for sev in severity_keywords:
            if f"severity: {sev}" in line_lower or f"- {sev}:" in line_lower:
                if current_finding:
                    findings.append(current_finding)
                current_finding = {
                    "severity": sev,
                    "category": category,
                    "file": "",
                    "description": line.strip(),
                    "recommendation": "",
                }
                break

        if current_finding:
            if "file:" in line_lower:
                current_finding["file"] = line.split(":", 1)[-1].strip()
            elif "recommendation:" in line_lower or "fix:" in line_lower:
                current_finding["recommendation"] = line.split(":", 1)[-1].strip()
            elif "description:" in line_lower:
                current_finding["description"] = line.split(":", 1)[-1].strip()

    if current_finding:
        findings.append(current_finding)

    # If no structured findings were parsed but the text mentions issues,
    # create a single finding from the full text
    if not findings and any(
        kw in text.lower() for kw in ["issue", "problem", "vulnerability", "error", "warning"]
    ):
        findings.append({
            "severity": "medium",
            "category": category,
            "file": "",
            "description": text[:500],
            "recommendation": "Review the identified issues.",
        })

    return findings


def _format_findings(findings: list[dict[str, Any]]) -> str:
    """Format findings list as a readable text block."""
    if not findings:
        return ""
    lines = []
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. [{f.get('severity', 'unknown').upper()}] {f.get('description', '')}"
        )
        if f.get("file"):
            lines.append(f"   File: {f['file']}")
        if f.get("recommendation"):
            lines.append(f"   Fix: {f['recommendation']}")
    return "\n".join(lines)


def _parse_synthesis(text: str) -> tuple[str, str]:
    """Parse outcome and summary from synthesis response."""
    outcome = "needs_changes"
    summary = text[:500]

    for line in text.splitlines():
        line_stripped = line.strip()
        if line_stripped.upper().startswith("OUTCOME:"):
            outcome_raw = line_stripped.split(":", 1)[-1].strip().lower()
            if "approved" in outcome_raw:
                outcome = "approved"
            elif "rejected" in outcome_raw:
                outcome = "rejected"
            else:
                outcome = "needs_changes"
        elif line_stripped.upper().startswith("SUMMARY:"):
            summary = line_stripped.split(":", 1)[-1].strip()

    return outcome, summary


def _outcome_to_confidence(outcome: str) -> float:
    """Convert review outcome to a confidence score."""
    return {"approved": 0.9, "needs_changes": 0.7, "rejected": 0.95}.get(outcome, 0.7)


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_code_review_graph(
    llm: BaseChatModel,
    workspace_root: str | None = None,
) -> StateGraph:
    """
    Build and compile the code review LangGraph reasoning graph.

    Args:
        llm:            The LangChain chat model to use for reasoning.
        workspace_root: Optional workspace root path (for tool context).

    Returns:
        A compiled LangGraph ``StateGraph`` ready for invocation.
    """
    graph = StateGraph(CodeReviewState)

    # Add nodes
    graph.add_node("load_context", _load_context_node)
    graph.add_node("analyse_changes", _make_analyse_node(llm))
    graph.add_node("security_check", _make_security_node(llm))
    graph.add_node("quality_check", _make_quality_node(llm))
    graph.add_node("synthesise", _make_synthesise_node(llm))
    graph.add_node("emit_suggestion", _emit_suggestion_node)

    # Define edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "analyse_changes")
    graph.add_edge("analyse_changes", "security_check")
    graph.add_edge("security_check", "quality_check")
    graph.add_edge("quality_check", "synthesise")
    graph.add_edge("synthesise", "emit_suggestion")
    graph.add_edge("emit_suggestion", END)

    return graph.compile()
