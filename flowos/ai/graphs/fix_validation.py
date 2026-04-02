"""
ai/graphs/fix_validation.py — FlowOS LangGraph Fix Validation Reasoning Graph

Implements a LangGraph reasoning graph that validates proposed fixes against
failing tests or build errors.  The graph:

1. Analyses the failing test/build output to understand the root cause.
2. Examines the proposed fix (patch/diff).
3. Validates whether the fix correctly addresses the root cause.
4. Checks for regressions or side effects.
5. Emits a structured validation result.

Graph nodes:
    load_context      — Load task context and extract failure information.
    analyse_failure   — Understand the root cause of the failure.
    examine_fix       — Examine the proposed fix in detail.
    validate_fix      — Validate whether the fix addresses the root cause.
    check_regressions — Check for potential regressions.
    emit_result       — Emit the validation result as a Kafka event.

Usage::

    from ai.graphs.fix_validation import build_fix_validation_graph, FixValidationState

    graph = build_fix_validation_graph(llm=my_llm)
    result = graph.invoke({
        "task_context": ctx,
        "task_id": "task-xyz",
        "workflow_id": "wf-123",
        "agent_id": "agent-abc",
        "failure_output": "...",
        "proposed_fix": "...",
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


class FixValidationState(TypedDict):
    """
    State for the fix validation reasoning graph.

    Attributes:
        task_context:       The assembled task context.
        task_id:            Task identifier.
        workflow_id:        Workflow identifier.
        agent_id:           AI agent identifier.
        messages:           LangGraph message history.
        failure_output:     The failing test/build output.
        proposed_fix:       The proposed fix (patch/diff/code).
        root_cause:         Identified root cause of the failure.
        fix_analysis:       Analysis of the proposed fix.
        validation_result:  Whether the fix is valid: valid, invalid, partial.
        validation_confidence: Confidence score (0.0-1.0).
        regression_risks:   Potential regression risks identified.
        validation_summary: Human-readable validation summary.
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
    failure_output: str
    proposed_fix: str
    root_cause: str
    fix_analysis: str
    validation_result: str
    validation_confidence: float
    regression_risks: list[dict[str, Any]]
    validation_summary: str
    suggestion_id: str
    error: str | None
    step_count: int
    reasoning_steps: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert software engineer specialising in debugging and fix validation.
Your role is to analyse failing tests or build errors, examine proposed fixes, and determine
whether the fix correctly addresses the root cause without introducing regressions.

You approach validation systematically:
1. Understand exactly what is failing and why
2. Trace the root cause to its origin
3. Verify the fix addresses the root cause directly
4. Check for edge cases the fix might miss
5. Identify potential side effects or regressions

Be precise and technical. Reference specific error messages, stack traces, and code paths.
"""

_ANALYSE_FAILURE_PROMPT = """Analyse the following failure output to identify the root cause.

Task Description: {task_description}

Failure Output:
```
{failure_output}
```

Provide:
1. The type of failure (test failure, compilation error, runtime error, etc.)
2. The specific error message or assertion that failed
3. The root cause of the failure (what is actually wrong)
4. The affected component(s) or code path(s)
5. What a correct fix should address
"""

_EXAMINE_FIX_PROMPT = """Examine the proposed fix in the context of the identified failure.

Root Cause:
{root_cause}

Proposed Fix:
```
{proposed_fix}
```

Analyse:
1. What does this fix change?
2. Does it directly address the identified root cause?
3. Are there any parts of the fix that seem unrelated to the failure?
4. Are there edge cases or scenarios the fix might not handle?
5. Is the fix complete, or does it only partially address the issue?
"""

_VALIDATE_FIX_PROMPT = """Validate whether the proposed fix correctly resolves the failure.

Failure Output:
```
{failure_output}
```

Root Cause: {root_cause}

Fix Analysis: {fix_analysis}

Determine:
1. VALIDATION_RESULT: valid / invalid / partial
   - valid: the fix correctly and completely addresses the root cause
   - invalid: the fix does not address the root cause or introduces new issues
   - partial: the fix addresses part of the issue but is incomplete
2. CONFIDENCE: a score from 0.0 to 1.0 indicating your confidence in this assessment
3. REASONING: explain your validation decision in 2-3 sentences

Format:
VALIDATION_RESULT: <valid|invalid|partial>
CONFIDENCE: <0.0-1.0>
REASONING: <your reasoning>
"""

_REGRESSION_PROMPT = """Check the proposed fix for potential regressions or side effects.

Proposed Fix:
```
{proposed_fix}
```

Context (changed files): {changed_files}

Identify:
1. Any code paths that might be affected by this change beyond the failing test
2. Potential performance implications
3. API contract changes that could break callers
4. State management issues
5. Concurrency or thread-safety concerns

For each risk, provide:
- risk_level: high/medium/low
- description: what could go wrong
- affected_area: what part of the system is at risk
"""


# ---------------------------------------------------------------------------
# Graph node functions
# ---------------------------------------------------------------------------


def _load_context_node(state: FixValidationState) -> dict[str, Any]:
    """Load and validate the task context."""
    logger.info("fix_validation: load_context | task_id=%s", state.get("task_id"))

    ctx = state.get("task_context")
    failure_output = state.get("failure_output", "")
    proposed_fix = state.get("proposed_fix", "")

    if not failure_output:
        # Try to load from artifacts
        if ctx and ctx.workspace_root:
            from pathlib import Path
            artifacts_dir = Path(ctx.workspace_root) / "artifacts"
            for log_file in sorted(artifacts_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    failure_output = log_file.read_text(encoding="utf-8", errors="replace")[:5000]
                    break
                except OSError:
                    pass

    if not proposed_fix:
        # Try to load from patches directory
        if ctx and ctx.workspace_root:
            from pathlib import Path
            patches_dir = Path(ctx.workspace_root) / "patches"
            for patch_file in sorted(patches_dir.glob("*.patch"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    proposed_fix = patch_file.read_text(encoding="utf-8", errors="replace")[:10000]
                    break
                except OSError:
                    pass

    step = {
        "step_type": "plan",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Loaded context for fix validation. "
            f"Failure output: {len(failure_output)} chars. "
            f"Proposed fix: {len(proposed_fix)} chars."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "failure_output": failure_output,
        "proposed_fix": proposed_fix,
        "step_count": state.get("step_count", 0) + 1,
        "reasoning_steps": state.get("reasoning_steps", []) + [step],
        "error": None,
    }


def _make_analyse_failure_node(llm: BaseChatModel) -> Any:
    """Create the analyse_failure node."""

    def _analyse_failure_node(state: FixValidationState) -> dict[str, Any]:
        """Analyse the failure output to identify the root cause."""
        if state.get("error"):
            return {}

        logger.info("fix_validation: analyse_failure | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        task_description = ctx.task_description if ctx else "Fix validation task"

        failure_output = state.get("failure_output", "No failure output provided.")
        # Truncate very long failure outputs
        if len(failure_output) > 3000:
            failure_output = failure_output[:1500] + "\n...[truncated]...\n" + failure_output[-1500:]

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_ANALYSE_FAILURE_PROMPT.format(
                task_description=task_description,
                failure_output=failure_output,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            root_cause = response.content if hasattr(response, "content") else str(response)

            step = {
                "step_type": "thought",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Root cause analysis:\n{root_cause}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "root_cause": root_cause,
                "messages": [AIMessage(content=root_cause)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("analyse_failure failed: %s", exc)
            return {
                "root_cause": f"Analysis failed: {exc}",
                "error": f"Failure analysis failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _analyse_failure_node


def _make_examine_fix_node(llm: BaseChatModel) -> Any:
    """Create the examine_fix node."""

    def _examine_fix_node(state: FixValidationState) -> dict[str, Any]:
        """Examine the proposed fix in detail."""
        if state.get("error"):
            return {}

        logger.info("fix_validation: examine_fix | task_id=%s", state.get("task_id"))

        proposed_fix = state.get("proposed_fix", "No fix provided.")
        if len(proposed_fix) > 5000:
            proposed_fix = proposed_fix[:2500] + "\n...[truncated]...\n" + proposed_fix[-2500:]

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_EXAMINE_FIX_PROMPT.format(
                root_cause=state.get("root_cause", "Unknown root cause"),
                proposed_fix=proposed_fix,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            fix_analysis = response.content if hasattr(response, "content") else str(response)

            step = {
                "step_type": "observation",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Fix examination:\n{fix_analysis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "fix_analysis": fix_analysis,
                "messages": [AIMessage(content=fix_analysis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("examine_fix failed: %s", exc)
            return {
                "fix_analysis": f"Examination failed: {exc}",
                "error": f"Fix examination failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _examine_fix_node


def _make_validate_fix_node(llm: BaseChatModel) -> Any:
    """Create the validate_fix node."""

    def _validate_fix_node(state: FixValidationState) -> dict[str, Any]:
        """Validate whether the fix correctly resolves the failure."""
        if state.get("error"):
            return {
                "validation_result": "invalid",
                "validation_confidence": 0.0,
                "validation_summary": f"Validation failed: {state.get('error')}",
            }

        logger.info("fix_validation: validate_fix | task_id=%s", state.get("task_id"))

        failure_output = state.get("failure_output", "")
        if len(failure_output) > 2000:
            failure_output = failure_output[:2000] + "\n...[truncated]..."

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_VALIDATE_FIX_PROMPT.format(
                failure_output=failure_output,
                root_cause=state.get("root_cause", "Unknown"),
                fix_analysis=state.get("fix_analysis", "No analysis available"),
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            validation_text = response.content if hasattr(response, "content") else str(response)

            # Parse validation result
            result, confidence, summary = _parse_validation(validation_text)

            step = {
                "step_type": "decision",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Validation decision:\n{validation_text}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "validation_result": result,
                "validation_confidence": confidence,
                "validation_summary": summary,
                "messages": [AIMessage(content=validation_text)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("validate_fix failed: %s", exc)
            return {
                "validation_result": "invalid",
                "validation_confidence": 0.0,
                "validation_summary": f"Validation failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _validate_fix_node


def _make_regression_node(llm: BaseChatModel) -> Any:
    """Create the check_regressions node."""

    def _check_regressions_node(state: FixValidationState) -> dict[str, Any]:
        """Check for potential regressions introduced by the fix."""
        logger.info("fix_validation: check_regressions | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        changed_files = ", ".join(f.path for f in ctx.changed_files[:10]) if ctx else "unknown"

        proposed_fix = state.get("proposed_fix", "No fix provided.")
        if len(proposed_fix) > 3000:
            proposed_fix = proposed_fix[:3000] + "\n...[truncated]..."

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_REGRESSION_PROMPT.format(
                proposed_fix=proposed_fix,
                changed_files=changed_files,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            regression_text = response.content if hasattr(response, "content") else str(response)

            risks = _parse_regression_risks(regression_text)

            step = {
                "step_type": "reflection",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Regression analysis:\n{regression_text}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "regression_risks": risks,
                "messages": [AIMessage(content=regression_text)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("check_regressions failed: %s", exc)
            return {
                "regression_risks": [],
                "step_count": state.get("step_count", 0) + 1,
            }

    return _check_regressions_node


def _emit_result_node(state: FixValidationState) -> dict[str, Any]:
    """Emit the validation result as a Kafka AI_SUGGESTION_CREATED event."""
    logger.info("fix_validation: emit_result | task_id=%s", state.get("task_id"))

    suggestion_id = str(uuid.uuid4())

    validation_result = state.get("validation_result", "invalid")
    confidence = state.get("validation_confidence", 0.0)
    summary = state.get("validation_summary", "")
    regression_risks = state.get("regression_risks", [])

    # Build the full suggestion content
    content_parts = [
        f"Fix Validation Result: {validation_result.upper()}",
        f"Confidence: {confidence:.0%}",
        f"Summary: {summary}",
    ]
    if regression_risks:
        content_parts.append(f"Regression Risks: {len(regression_risks)} identified")
        for risk in regression_risks[:3]:
            content_parts.append(
                f"  [{risk.get('risk_level', 'unknown').upper()}] {risk.get('description', '')}"
            )
    content = "\n".join(content_parts)

    try:
        from shared.kafka.producer import get_producer
        from shared.kafka.schemas import AISuggestionCreatedPayload, build_event
        from shared.models.event import EventSource, EventType

        producer = get_producer()

        payload = AISuggestionCreatedPayload(
            task_id=state.get("task_id", ""),
            workflow_id=state.get("workflow_id", ""),
            agent_id=state.get("agent_id", ""),
            suggestion_id=suggestion_id,
            suggestion_type="fix_validation",
            content=content,
            confidence=confidence,
            reasoning=(
                f"Validation result: {validation_result}. "
                f"Root cause addressed: {validation_result in ('valid', 'partial')}. "
                f"Regression risks: {len(regression_risks)}."
            ),
        )
        event = build_event(
            event_type=EventType.AI_SUGGESTION_CREATED,
            payload=payload,
            source=EventSource.AI_AGENT,
            task_id=state.get("task_id"),
            workflow_id=state.get("workflow_id"),
            agent_id=state.get("agent_id"),
        )
        producer.produce(event)
        producer.flush(timeout=10.0)

        logger.info(
            "fix_validation: emitted suggestion | suggestion_id=%s result=%s",
            suggestion_id,
            validation_result,
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit Kafka events: %s", exc)

    step = {
        "step_type": "final_answer",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Emitted AI_SUGGESTION_CREATED event. "
            f"Suggestion ID: {suggestion_id}. "
            f"Validation result: {validation_result} (confidence: {confidence:.0%})."
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


def _parse_validation(text: str) -> tuple[str, float, str]:
    """Parse validation result, confidence, and reasoning from LLM response."""
    result = "invalid"
    confidence = 0.5
    summary = text[:300]

    for line in text.splitlines():
        line_stripped = line.strip()
        upper = line_stripped.upper()

        if upper.startswith("VALIDATION_RESULT:"):
            raw = line_stripped.split(":", 1)[-1].strip().lower()
            if "valid" in raw and "invalid" not in raw:
                result = "valid"
            elif "partial" in raw:
                result = "partial"
            else:
                result = "invalid"

        elif upper.startswith("CONFIDENCE:"):
            try:
                conf_str = line_stripped.split(":", 1)[-1].strip()
                confidence = float(conf_str)
                confidence = max(0.0, min(1.0, confidence))
            except ValueError:
                pass

        elif upper.startswith("REASONING:"):
            summary = line_stripped.split(":", 1)[-1].strip()

    return result, confidence, summary


def _parse_regression_risks(text: str) -> list[dict[str, Any]]:
    """Parse regression risks from LLM response."""
    risks: list[dict[str, Any]] = []
    risk_levels = ["high", "medium", "low"]

    current_risk: dict[str, Any] | None = None

    for line in text.splitlines():
        line_lower = line.lower().strip()

        for level in risk_levels:
            if f"risk_level: {level}" in line_lower or f"- {level}:" in line_lower:
                if current_risk:
                    risks.append(current_risk)
                current_risk = {
                    "risk_level": level,
                    "description": line.strip(),
                    "affected_area": "",
                }
                break

        if current_risk:
            if "description:" in line_lower:
                current_risk["description"] = line.split(":", 1)[-1].strip()
            elif "affected_area:" in line_lower or "affected:" in line_lower:
                current_risk["affected_area"] = line.split(":", 1)[-1].strip()

    if current_risk:
        risks.append(current_risk)

    return risks


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_fix_validation_graph(
    llm: BaseChatModel,
    workspace_root: str | None = None,
) -> StateGraph:
    """
    Build and compile the fix validation LangGraph reasoning graph.

    Args:
        llm:            The LangChain chat model to use for reasoning.
        workspace_root: Optional workspace root path.

    Returns:
        A compiled LangGraph ``StateGraph`` ready for invocation.
    """
    graph = StateGraph(FixValidationState)

    # Add nodes
    graph.add_node("load_context", _load_context_node)
    graph.add_node("analyse_failure", _make_analyse_failure_node(llm))
    graph.add_node("examine_fix", _make_examine_fix_node(llm))
    graph.add_node("validate_fix", _make_validate_fix_node(llm))
    graph.add_node("check_regressions", _make_regression_node(llm))
    graph.add_node("emit_result", _emit_result_node)

    # Define edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "analyse_failure")
    graph.add_edge("analyse_failure", "examine_fix")
    graph.add_edge("examine_fix", "validate_fix")
    graph.add_edge("validate_fix", "check_regressions")
    graph.add_edge("check_regressions", "emit_result")
    graph.add_edge("emit_result", END)

    return graph.compile()
