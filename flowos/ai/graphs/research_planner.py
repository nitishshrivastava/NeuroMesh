"""
ai/graphs/research_planner.py — FlowOS LangGraph Research Planner Reasoning Graph

Implements a LangGraph reasoning graph for AI-driven research and planning tasks.
The graph:

1. Decomposes the research objective into sub-questions.
2. Searches the codebase and available resources for relevant information.
3. Synthesises findings into a structured research report.
4. Generates an actionable implementation plan.
5. Emits the plan as an AI suggestion.

Graph nodes:
    load_context      — Load task context and understand the objective.
    decompose         — Break the objective into research sub-questions.
    gather_context    — Gather relevant information from the workspace.
    synthesise        — Synthesise findings into a research report.
    generate_plan     — Generate a structured implementation plan.
    emit_plan         — Emit the plan as a Kafka AI_SUGGESTION_CREATED event.

Usage::

    from ai.graphs.research_planner import build_research_planner_graph, ResearchPlannerState

    graph = build_research_planner_graph(llm=my_llm, workspace_root="/path/to/workspace")
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
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from ai.context_loader import TaskContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------


class ResearchPlannerState(TypedDict):
    """
    State for the research planner reasoning graph.

    Attributes:
        task_context:       The assembled task context.
        task_id:            Task identifier.
        workflow_id:        Workflow identifier.
        agent_id:           AI agent identifier.
        messages:           LangGraph message history.
        sub_questions:      Research sub-questions derived from the objective.
        gathered_context:   Information gathered from the workspace.
        research_findings:  Synthesised research findings.
        implementation_plan: Structured implementation plan.
        plan_steps:         Individual plan steps with details.
        estimated_effort:   Estimated effort (hours or story points).
        risks:              Identified risks and mitigations.
        dependencies:       External dependencies identified.
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
    sub_questions: list[str]
    gathered_context: dict[str, str]
    research_findings: str
    implementation_plan: str
    plan_steps: list[dict[str, Any]]
    estimated_effort: str
    risks: list[dict[str, Any]]
    dependencies: list[str]
    suggestion_id: str
    error: str | None
    step_count: int
    reasoning_steps: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert software architect and technical researcher for the FlowOS platform.
Your role is to research technical topics, analyse codebases, and produce clear, actionable
implementation plans that development teams can follow.

You approach research systematically:
1. Break complex objectives into specific, answerable sub-questions
2. Gather relevant information from the codebase and available resources
3. Synthesise findings into clear insights
4. Translate insights into concrete, prioritised action steps
5. Identify risks and dependencies proactively

Your plans are:
- Specific: each step has a clear deliverable
- Ordered: steps are sequenced by dependency
- Realistic: effort estimates are grounded in the codebase complexity
- Risk-aware: risks are identified with mitigations
"""

_DECOMPOSE_PROMPT = """Break down the following research/planning objective into specific sub-questions.

Objective: {task_description}

Context:
- Workspace branch: {workspace_branch}
- Changed files: {changed_files_summary}
- Build status: {build_status}

Generate 3-7 specific sub-questions that, when answered, will provide everything needed
to create a comprehensive implementation plan.

Format each sub-question on a new line starting with "Q: "
"""

_GATHER_CONTEXT_PROMPT = """Based on the research sub-questions and available workspace context,
gather and summarise the relevant information.

Sub-questions to answer:
{sub_questions}

Available workspace information:
- Recent commits: {recent_commits}
- Changed files: {changed_files}
- Artifacts: {artifacts}
- Build status: {build_status}
- Test status: {test_status}

For each sub-question, provide a brief answer based on the available context.
If information is not available in the workspace, note what would need to be investigated.

Format:
Q: <question>
A: <answer based on available context>
"""

_SYNTHESISE_PROMPT = """Synthesise the research findings into a coherent technical analysis.

Objective: {task_description}

Research Findings:
{gathered_context}

Provide:
1. KEY FINDINGS: The most important insights from the research (3-5 bullet points)
2. TECHNICAL APPROACH: The recommended technical approach to achieve the objective
3. CONSTRAINTS: Technical constraints or limitations to be aware of
4. OPEN QUESTIONS: Any questions that still need to be answered
"""

_GENERATE_PLAN_PROMPT = """Generate a detailed, actionable implementation plan.

Objective: {task_description}

Research Synthesis:
{research_findings}

Create a structured implementation plan with:
1. PLAN OVERVIEW: A 2-3 sentence summary of the approach
2. STEPS: Numbered implementation steps, each with:
   - Title: brief step name
   - Description: what to do and how
   - Deliverable: the concrete output of this step
   - Effort: estimated time (e.g. "2 hours", "1 day")
   - Dependencies: which prior steps must be complete
3. RISKS: Key risks with mitigation strategies
4. DEPENDENCIES: External dependencies (libraries, services, APIs)
5. ESTIMATED_TOTAL_EFFORT: Total estimated effort

Format steps as:
STEP N: <title>
  Description: <what to do>
  Deliverable: <concrete output>
  Effort: <time estimate>
  Dependencies: <step numbers or "none">
"""


# ---------------------------------------------------------------------------
# Graph node functions
# ---------------------------------------------------------------------------


def _load_context_node(state: ResearchPlannerState) -> dict[str, Any]:
    """Load and validate the task context."""
    logger.info("research_planner: load_context | task_id=%s", state.get("task_id"))

    ctx = state.get("task_context")
    if not ctx:
        return {
            "error": "No task context provided.",
            "step_count": state.get("step_count", 0) + 1,
        }

    step = {
        "step_type": "plan",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Loaded context for research planning task: {ctx.task_name}. "
            f"Objective: {ctx.task_description[:200]}. "
            f"Workspace: {ctx.workspace_branch} branch, "
            f"{len(ctx.changed_files)} changed files."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "step_count": state.get("step_count", 0) + 1,
        "reasoning_steps": state.get("reasoning_steps", []) + [step],
        "error": None,
    }


def _make_decompose_node(llm: BaseChatModel) -> Any:
    """Create the decompose node."""

    def _decompose_node(state: ResearchPlannerState) -> dict[str, Any]:
        """Decompose the objective into research sub-questions."""
        if state.get("error"):
            return {}

        logger.info("research_planner: decompose | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        if not ctx:
            return {"error": "No task context", "step_count": state.get("step_count", 0) + 1}

        changed_files_summary = (
            ", ".join(f.path for f in ctx.changed_files[:5])
            if ctx.changed_files
            else "none"
        )

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_DECOMPOSE_PROMPT.format(
                task_description=ctx.task_description,
                workspace_branch=ctx.workspace_branch,
                changed_files_summary=changed_files_summary,
                build_status=ctx.build_status,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            decomposition = response.content if hasattr(response, "content") else str(response)

            # Parse sub-questions
            sub_questions = [
                line[2:].strip()
                for line in decomposition.splitlines()
                if line.strip().startswith("Q:")
            ]
            if not sub_questions:
                # Fall back: treat each non-empty line as a question
                sub_questions = [
                    line.strip()
                    for line in decomposition.splitlines()
                    if line.strip() and len(line.strip()) > 10
                ][:7]

            step = {
                "step_type": "plan",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Decomposed objective into {len(sub_questions)} sub-questions:\n{decomposition}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "sub_questions": sub_questions,
                "messages": [AIMessage(content=decomposition)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("decompose failed: %s", exc)
            return {
                "sub_questions": [ctx.task_description],
                "error": f"Decomposition failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _decompose_node


def _make_gather_context_node(llm: BaseChatModel, tools: list[BaseTool]) -> Any:
    """Create the gather_context node with optional tool access."""

    def _gather_context_node(state: ResearchPlannerState) -> dict[str, Any]:
        """Gather relevant information from the workspace."""
        if state.get("error"):
            return {}

        logger.info("research_planner: gather_context | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        if not ctx:
            return {"error": "No task context", "step_count": state.get("step_count", 0) + 1}

        sub_questions = state.get("sub_questions", [ctx.task_description])

        # Format workspace information
        recent_commits = "\n".join(
            f"  {c.short_sha} {c.committed_at[:10]} {c.message[:60]}"
            for c in ctx.recent_commits[:5]
        ) or "  No recent commits"

        changed_files = "\n".join(
            f"  [{f.status}] {f.path}"
            for f in ctx.changed_files[:10]
        ) or "  No changed files"

        artifacts = "\n".join(
            f"  [{a.artifact_type}] {a.name}"
            for a in ctx.artifacts[:5]
        ) or "  No artifacts"

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_GATHER_CONTEXT_PROMPT.format(
                sub_questions="\n".join(f"Q: {q}" for q in sub_questions),
                recent_commits=recent_commits,
                changed_files=changed_files,
                artifacts=artifacts,
                build_status=ctx.build_status,
                test_status=ctx.test_status,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            gathered = response.content if hasattr(response, "content") else str(response)

            # Parse Q&A pairs into a dict
            gathered_context: dict[str, str] = {}
            current_q = ""
            current_a_lines: list[str] = []

            for line in gathered.splitlines():
                if line.strip().startswith("Q:"):
                    if current_q and current_a_lines:
                        gathered_context[current_q] = "\n".join(current_a_lines).strip()
                    current_q = line[2:].strip()
                    current_a_lines = []
                elif line.strip().startswith("A:") and current_q:
                    current_a_lines.append(line[2:].strip())
                elif current_q and current_a_lines is not None:
                    current_a_lines.append(line)

            if current_q and current_a_lines:
                gathered_context[current_q] = "\n".join(current_a_lines).strip()

            if not gathered_context:
                gathered_context["general"] = gathered

            step = {
                "step_type": "observation",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Gathered context for {len(gathered_context)} sub-questions:\n{gathered[:500]}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "gathered_context": gathered_context,
                "messages": [AIMessage(content=gathered)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("gather_context failed: %s", exc)
            return {
                "gathered_context": {"error": str(exc)},
                "error": f"Context gathering failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _gather_context_node


def _make_synthesise_node(llm: BaseChatModel) -> Any:
    """Create the synthesise node."""

    def _synthesise_node(state: ResearchPlannerState) -> dict[str, Any]:
        """Synthesise research findings into a coherent technical analysis."""
        if state.get("error"):
            return {}

        logger.info("research_planner: synthesise | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        task_description = ctx.task_description if ctx else "Research task"

        gathered_context = state.get("gathered_context", {})
        context_text = "\n\n".join(
            f"Q: {q}\nA: {a}" for q, a in gathered_context.items()
        ) or "No context gathered."

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_SYNTHESISE_PROMPT.format(
                task_description=task_description,
                gathered_context=context_text,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            synthesis = response.content if hasattr(response, "content") else str(response)

            step = {
                "step_type": "thought",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Research synthesis:\n{synthesis}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "research_findings": synthesis,
                "messages": [AIMessage(content=synthesis)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("synthesise failed: %s", exc)
            return {
                "research_findings": f"Synthesis failed: {exc}",
                "error": f"Synthesis failed: {exc}",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _synthesise_node


def _make_generate_plan_node(llm: BaseChatModel) -> Any:
    """Create the generate_plan node."""

    def _generate_plan_node(state: ResearchPlannerState) -> dict[str, Any]:
        """Generate a structured implementation plan."""
        logger.info("research_planner: generate_plan | task_id=%s", state.get("task_id"))

        ctx = state.get("task_context")
        task_description = ctx.task_description if ctx else "Research task"

        research_findings = state.get("research_findings", "No research findings available.")
        if state.get("error"):
            research_findings = f"Note: Research was incomplete due to: {state.get('error')}\n\n{research_findings}"

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_GENERATE_PLAN_PROMPT.format(
                task_description=task_description,
                research_findings=research_findings,
            )),
        ])

        try:
            response = llm.invoke(prompt.format_messages())
            plan_text = response.content if hasattr(response, "content") else str(response)

            # Parse plan components
            plan_steps, risks, dependencies, effort = _parse_plan(plan_text)

            step = {
                "step_type": "proposal",
                "sequence": state.get("step_count", 0) + 1,
                "content": f"Implementation plan generated:\n{plan_text[:500]}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "implementation_plan": plan_text,
                "plan_steps": plan_steps,
                "risks": risks,
                "dependencies": dependencies,
                "estimated_effort": effort,
                "messages": [AIMessage(content=plan_text)],
                "step_count": state.get("step_count", 0) + 1,
                "reasoning_steps": state.get("reasoning_steps", []) + [step],
                "error": None,  # Clear any prior errors — we have a plan
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("generate_plan failed: %s", exc)
            return {
                "implementation_plan": f"Plan generation failed: {exc}",
                "plan_steps": [],
                "risks": [],
                "dependencies": [],
                "estimated_effort": "unknown",
                "step_count": state.get("step_count", 0) + 1,
            }

    return _generate_plan_node


def _emit_plan_node(state: ResearchPlannerState) -> dict[str, Any]:
    """Emit the implementation plan as a Kafka AI_SUGGESTION_CREATED event."""
    logger.info("research_planner: emit_plan | task_id=%s", state.get("task_id"))

    suggestion_id = str(uuid.uuid4())

    plan = state.get("implementation_plan", "No plan generated.")
    plan_steps = state.get("plan_steps", [])
    effort = state.get("estimated_effort", "unknown")

    # Build suggestion content
    content = f"Implementation Plan\n\n{plan[:2000]}"
    if len(plan) > 2000:
        content += "\n\n[... plan truncated ...]"

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
            suggestion_type="research_plan",
            content=content,
            confidence=0.8,
            reasoning=(
                f"Research plan generated with {len(plan_steps)} steps. "
                f"Estimated effort: {effort}. "
                f"Risks identified: {len(state.get('risks', []))}."
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
            "research_planner: emitted plan | suggestion_id=%s steps=%d",
            suggestion_id,
            len(plan_steps),
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to emit Kafka events: %s", exc)

    step = {
        "step_type": "final_answer",
        "sequence": state.get("step_count", 0) + 1,
        "content": (
            f"Emitted AI_SUGGESTION_CREATED event with implementation plan. "
            f"Suggestion ID: {suggestion_id}. "
            f"Plan has {len(plan_steps)} steps, estimated effort: {effort}."
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


def _parse_plan(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str]:
    """
    Parse plan steps, risks, dependencies, and effort from plan text.

    Returns: (plan_steps, risks, dependencies, estimated_effort)
    """
    plan_steps: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    dependencies: list[str] = []
    estimated_effort = "unknown"

    current_step: dict[str, Any] | None = None
    section = "none"

    for line in text.splitlines():
        line_stripped = line.strip()
        upper = line_stripped.upper()

        # Detect section headers
        if upper.startswith("STEP ") and ":" in line_stripped:
            if current_step:
                plan_steps.append(current_step)
            step_title = line_stripped.split(":", 1)[-1].strip()
            current_step = {
                "title": step_title,
                "description": "",
                "deliverable": "",
                "effort": "",
                "dependencies": [],
            }
            section = "step"

        elif upper.startswith("RISKS:") or upper == "RISKS":
            if current_step:
                plan_steps.append(current_step)
                current_step = None
            section = "risks"

        elif upper.startswith("DEPENDENCIES:") or upper == "DEPENDENCIES":
            section = "dependencies"

        elif upper.startswith("ESTIMATED_TOTAL_EFFORT:") or upper.startswith("TOTAL EFFORT:"):
            effort_raw = line_stripped.split(":", 1)[-1].strip()
            if effort_raw:
                estimated_effort = effort_raw
            section = "none"

        elif section == "step" and current_step is not None:
            if "description:" in line_stripped.lower():
                current_step["description"] = line_stripped.split(":", 1)[-1].strip()
            elif "deliverable:" in line_stripped.lower():
                current_step["deliverable"] = line_stripped.split(":", 1)[-1].strip()
            elif "effort:" in line_stripped.lower():
                current_step["effort"] = line_stripped.split(":", 1)[-1].strip()
            elif "dependencies:" in line_stripped.lower():
                deps_raw = line_stripped.split(":", 1)[-1].strip()
                if deps_raw.lower() != "none":
                    current_step["dependencies"] = [d.strip() for d in deps_raw.split(",")]

        elif section == "risks" and line_stripped.startswith("-"):
            risk_text = line_stripped[1:].strip()
            if risk_text:
                risks.append({
                    "risk_level": "medium",
                    "description": risk_text,
                    "mitigation": "",
                })

        elif section == "dependencies" and line_stripped.startswith("-"):
            dep = line_stripped[1:].strip()
            if dep:
                dependencies.append(dep)

    if current_step:
        plan_steps.append(current_step)

    return plan_steps, risks, dependencies, estimated_effort


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_research_planner_graph(
    llm: BaseChatModel,
    workspace_root: str | None = None,
    tools: list[BaseTool] | None = None,
) -> StateGraph:
    """
    Build and compile the research planner LangGraph reasoning graph.

    Args:
        llm:            The LangChain chat model to use for reasoning.
        workspace_root: Optional workspace root path.
        tools:          Optional list of LangChain tools for context gathering.

    Returns:
        A compiled LangGraph ``StateGraph`` ready for invocation.
    """
    graph = StateGraph(ResearchPlannerState)

    # Add nodes
    graph.add_node("load_context", _load_context_node)
    graph.add_node("decompose", _make_decompose_node(llm))
    graph.add_node("gather_context", _make_gather_context_node(llm, tools or []))
    graph.add_node("synthesise", _make_synthesise_node(llm))
    graph.add_node("generate_plan", _make_generate_plan_node(llm))
    graph.add_node("emit_plan", _emit_plan_node)

    # Define edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "decompose")
    graph.add_edge("decompose", "gather_context")
    graph.add_edge("gather_context", "synthesise")
    graph.add_edge("synthesise", "generate_plan")
    graph.add_edge("generate_plan", "emit_plan")
    graph.add_edge("emit_plan", END)

    return graph.compile()
