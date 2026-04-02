"""
workers/ai_worker.py — FlowOS AI Worker

The AI Worker is the Kafka consumer that listens for AI task assignments and
orchestrates LangGraph/LangChain reasoning sessions to complete them.

Architecture:
    1. Subscribes to ``flowos.task.events`` as consumer group ``flowos-ai-agent``.
    2. On ``TASK_ASSIGNED`` events where ``task_type == "ai"``, accepts the task.
    3. Assembles task context via ``ContextLoader``.
    4. Selects the appropriate LangGraph reasoning graph based on task type/tags.
    5. Runs the graph, streaming reasoning steps to Kafka (``AI_REASONING_TRACE``).
    6. Writes the full reasoning trace to ``.flowos/reasoning_trace.json``.
    7. Emits ``AI_SUGGESTION_CREATED`` on completion.
    8. Marks the task as completed or failed.

Supported task subtypes (via task tags or metadata):
    - ``code_review``      → CodeReviewGraph
    - ``fix_validation``   → FixValidationGraph
    - ``research_planner`` → ResearchPlannerGraph
    - (default)            → ResearchPlannerGraph (general AI task)

LLM Configuration:
    The worker reads LLM configuration from environment variables:
    - ``OPENAI_API_KEY``    → Uses ChatOpenAI (gpt-4o by default)
    - ``ANTHROPIC_API_KEY`` → Uses ChatAnthropic (claude-3-5-sonnet by default)
    - ``AI_MODEL_NAME``     → Override the model name
    - ``AI_MODEL_PROVIDER`` → Override the provider (openai, anthropic, fake)

Usage::

    # Run the AI worker (blocks until interrupted)
    python -m workers.ai_worker

    # Or import and run programmatically:
    from workers.ai_worker import AIWorker
    worker = AIWorker()
    worker.run()
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.config import settings
from shared.kafka.consumer import FlowOSConsumer
from shared.kafka.producer import FlowOSProducer, get_producer
from shared.kafka.schemas import (
    AIReasoningTracePayload,
    AITaskStartedPayload,
    AISuggestionCreatedPayload,
    TaskAcceptedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    build_event,
)
from shared.kafka.topics import ConsumerGroup, KafkaTopic
from shared.models.event import EventRecord, EventSource, EventType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default LLM model name when using OpenAI
DEFAULT_OPENAI_MODEL = "gpt-4o"

#: Default LLM model name when using Anthropic
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"

#: Maximum number of concurrent AI tasks
MAX_CONCURRENT_TASKS = int(os.environ.get("AI_WORKER_MAX_CONCURRENT", "3"))

#: Task types that this worker handles
AI_TASK_TYPES = {"ai", "review", "analysis"}

#: Graph selection based on task tags
GRAPH_TAG_MAP = {
    "code_review": "code_review",
    "review": "code_review",
    "fix_validation": "fix_validation",
    "fix": "fix_validation",
    "validate": "fix_validation",
    "research": "research_planner",
    "plan": "research_planner",
    "planning": "research_planner",
    "analysis": "research_planner",
}


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def _build_llm() -> Any:
    """
    Build the LangChain LLM based on environment configuration.

    Priority:
    1. ``AI_MODEL_PROVIDER`` env var (explicit override)
    2. ``OPENAI_API_KEY`` present → ChatOpenAI
    3. ``ANTHROPIC_API_KEY`` present → ChatAnthropic
    4. Fallback → FakeListChatModel (for testing without API keys)

    Returns:
        A LangChain BaseChatModel instance.
    """
    provider = os.environ.get("AI_MODEL_PROVIDER", "").lower()
    model_name = os.environ.get("AI_MODEL_NAME", "")

    # Explicit provider override
    if provider == "openai" or (not provider and os.environ.get("OPENAI_API_KEY")):
        try:
            from langchain_openai import ChatOpenAI
            model = model_name or DEFAULT_OPENAI_MODEL
            logger.info("Using OpenAI LLM | model=%s", model)
            return ChatOpenAI(
                model=model,
                temperature=0.1,
                max_tokens=4096,
            )
        except ImportError:
            logger.warning("langchain-openai not installed, trying Anthropic")

    if provider == "anthropic" or (not provider and os.environ.get("ANTHROPIC_API_KEY")):
        try:
            from langchain_anthropic import ChatAnthropic
            model = model_name or DEFAULT_ANTHROPIC_MODEL
            logger.info("Using Anthropic LLM | model=%s", model)
            return ChatAnthropic(
                model=model,
                temperature=0.1,
                max_tokens=4096,
            )
        except ImportError:
            logger.warning("langchain-anthropic not installed")

    if provider == "fake" or os.environ.get("AI_USE_FAKE_LLM", "").lower() in ("1", "true", "yes"):
        logger.warning("Using FakeListChatModel — AI responses will be synthetic")
        return _build_fake_llm()

    # Last resort: try to use a fake LLM for development
    logger.warning(
        "No LLM API key found (OPENAI_API_KEY or ANTHROPIC_API_KEY). "
        "Using FakeListChatModel. Set AI_USE_FAKE_LLM=true to suppress this warning."
    )
    return _build_fake_llm()


def _build_fake_llm() -> Any:
    """Build a fake LLM for testing without API keys."""
    try:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        return FakeListChatModel(
            responses=[
                "OUTCOME: needs_changes\nSUMMARY: Code review completed. Found 2 issues.\nACTION_ITEMS:\n- Fix error handling\n- Add tests",
                "VALIDATION_RESULT: valid\nCONFIDENCE: 0.8\nREASONING: The fix correctly addresses the root cause.",
                "Q: What is the main objective?\nA: Based on the task description, the main objective is to implement the requested feature.\n\nSTEP 1: Analysis\n  Description: Analyse the existing codebase\n  Deliverable: Analysis report\n  Effort: 2 hours\n  Dependencies: none\n\nESTIMATED_TOTAL_EFFORT: 1 day",
                "Analysis complete. The code changes look reasonable.",
                "Security review complete. No critical vulnerabilities found.",
                "Quality review complete. Code follows best practices.",
            ]
        )
    except ImportError:
        # Minimal fallback
        class _MinimalFakeLLM:
            def invoke(self, messages: Any) -> Any:
                class _Resp:
                    content = "AI analysis complete. Task processed successfully."
                return _Resp()
        return _MinimalFakeLLM()


# ---------------------------------------------------------------------------
# Reasoning trace writer
# ---------------------------------------------------------------------------


def _write_reasoning_trace(
    workspace_root: str,
    session_data: dict[str, Any],
) -> Path:
    """
    Write the reasoning trace to ``.flowos/reasoning_trace.json``.

    If the file already exists, appends the new session to the list.
    Returns the path to the written file.
    """
    flowos_dir = Path(workspace_root) / ".flowos"
    flowos_dir.mkdir(parents=True, exist_ok=True)
    trace_path = flowos_dir / "reasoning_trace.json"

    # Load existing traces
    existing: list[dict[str, Any]] = []
    if trace_path.exists():
        try:
            data = json.loads(trace_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            elif isinstance(data, dict):
                existing = [data]
        except (json.JSONDecodeError, OSError):
            existing = []

    # Append new session
    existing.append(session_data)

    # Keep only the last 50 sessions to prevent unbounded growth
    if len(existing) > 50:
        existing = existing[-50:]

    trace_path.write_text(
        json.dumps(existing, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("Reasoning trace written | path=%s", trace_path)
    return trace_path


# ---------------------------------------------------------------------------
# Graph selector
# ---------------------------------------------------------------------------


def _select_graph_type(task_type: str, task_tags: list[str], task_name: str) -> str:
    """
    Select the appropriate reasoning graph based on task metadata.

    Returns one of: 'code_review', 'fix_validation', 'research_planner'
    """
    # Check tags first (most specific)
    for tag in task_tags:
        tag_lower = tag.lower()
        if tag_lower in GRAPH_TAG_MAP:
            return GRAPH_TAG_MAP[tag_lower]

    # Check task name
    name_lower = task_name.lower()
    if any(kw in name_lower for kw in ["review", "code review", "pr review"]):
        return "code_review"
    if any(kw in name_lower for kw in ["fix", "validate", "validation", "regression"]):
        return "fix_validation"
    if any(kw in name_lower for kw in ["research", "plan", "planning", "analyse", "analyze"]):
        return "research_planner"

    # Default to research_planner for general AI tasks
    return "research_planner"


# ---------------------------------------------------------------------------
# Task executor
# ---------------------------------------------------------------------------


class AITaskExecutor:
    """
    Executes a single AI task using the appropriate LangGraph reasoning graph.

    This class handles the full lifecycle of an AI task:
    1. Accept the task (emit TASK_ACCEPTED)
    2. Start the reasoning session (emit AI_TASK_STARTED)
    3. Run the LangGraph graph
    4. Write the reasoning trace
    5. Emit reasoning trace events to Kafka
    6. Complete or fail the task

    Args:
        producer:       Kafka producer for emitting events.
        workspace_root: Root directory for workspace operations.
    """

    def __init__(
        self,
        producer: FlowOSProducer,
        workspace_root: str | None = None,
    ) -> None:
        self._producer = producer
        self._workspace_root = workspace_root or settings.workspace_root
        self._llm = _build_llm()

    def execute(self, task_event: EventRecord) -> None:
        """
        Execute an AI task from a TASK_ASSIGNED event.

        Args:
            task_event: The TASK_ASSIGNED EventRecord.
        """
        payload = task_event.payload
        task_id = payload.get("task_id", "")
        workflow_id = payload.get("workflow_id", task_event.workflow_id or "")
        agent_id = payload.get("assigned_agent_id", task_event.agent_id or "")
        task_name = payload.get("name", "AI Task")
        task_type = payload.get("task_type", "ai")
        priority = payload.get("priority", "normal")

        # Determine workspace root for this task
        workspace_root = os.path.join(
            self._workspace_root,
            agent_id or "default",
        )

        session_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)

        logger.info(
            "Executing AI task | task_id=%s workflow_id=%s agent_id=%s name=%s",
            task_id,
            workflow_id,
            agent_id,
            task_name,
        )

        # Step 1: Accept the task
        self._emit_task_accepted(task_id, workflow_id, agent_id)

        # Step 2: Load context
        from ai.context_loader import ContextLoader
        loader = ContextLoader(workspace_root=workspace_root)

        # Extract task inputs from payload
        task_inputs_raw = payload.get("inputs", [])
        task_inputs: dict[str, Any] = {}
        if isinstance(task_inputs_raw, list):
            for inp in task_inputs_raw:
                if isinstance(inp, dict) and "name" in inp:
                    task_inputs[inp["name"]] = inp.get("value", inp.get("default"))
        elif isinstance(task_inputs_raw, dict):
            task_inputs = task_inputs_raw

        task_tags = payload.get("tags", [])
        task_description = task_inputs.get("description", "") or payload.get("description", "")
        if not task_description:
            task_description = f"AI task: {task_name}"

        ctx = loader.load(
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            task_description=task_description,
            task_name=task_name,
            task_type=task_type,
            task_priority=priority,
            task_inputs=task_inputs,
            task_tags=task_tags,
        )

        # Save context to workspace
        try:
            loader.save_context(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save context: %s", exc)

        # Step 3: Emit AI_TASK_STARTED
        self._emit_ai_task_started(
            task_id=task_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            session_id=session_id,
        )

        # Step 4: Select and run the graph
        graph_type = _select_graph_type(task_type, task_tags, task_name)
        logger.info(
            "Selected graph | graph_type=%s task_id=%s",
            graph_type,
            task_id,
        )

        reasoning_steps: list[dict[str, Any]] = []
        final_output: str = ""
        suggestion_id: str = ""
        error_message: str | None = None

        try:
            graph_result = self._run_graph(
                graph_type=graph_type,
                ctx=ctx,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                workspace_root=workspace_root,
                task_inputs=task_inputs,
            )

            reasoning_steps = graph_result.get("reasoning_steps", [])
            suggestion_id = graph_result.get("suggestion_id", "")
            error_message = graph_result.get("error")

            # Extract final output based on graph type
            if graph_type == "code_review":
                final_output = (
                    f"Code review completed. "
                    f"Outcome: {graph_result.get('review_outcome', 'unknown')}. "
                    f"{graph_result.get('review_summary', '')}"
                )
            elif graph_type == "fix_validation":
                final_output = (
                    f"Fix validation completed. "
                    f"Result: {graph_result.get('validation_result', 'unknown')} "
                    f"(confidence: {graph_result.get('validation_confidence', 0):.0%}). "
                    f"{graph_result.get('validation_summary', '')}"
                )
            else:
                final_output = (
                    f"Research plan generated. "
                    f"Steps: {len(graph_result.get('plan_steps', []))}. "
                    f"Effort: {graph_result.get('estimated_effort', 'unknown')}."
                )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Graph execution failed | task_id=%s error=%s",
                task_id,
                exc,
                exc_info=True,
            )
            error_message = str(exc)
            final_output = f"AI task failed: {exc}"

        # Step 5: Write reasoning trace
        completed_at = datetime.now(timezone.utc)
        session_data = {
            "session_id": session_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "agent_id": agent_id,
            "graph_type": graph_type,
            "status": "failed" if error_message else "completed",
            "final_output": final_output,
            "suggestion_id": suggestion_id,
            "steps": reasoning_steps,
            "step_count": len(reasoning_steps),
            "model_name": self._get_model_name(),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": (completed_at - started_at).total_seconds(),
            "error_message": error_message,
        }

        try:
            _write_reasoning_trace(workspace_root, session_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write reasoning trace: %s", exc)

        # Step 6: Emit reasoning trace events to Kafka
        self._emit_reasoning_trace_events(
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            reasoning_steps=reasoning_steps,
        )

        # Step 7: Complete or fail the task
        if error_message:
            self._emit_task_failed(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                error_message=error_message,
            )
        else:
            self._emit_task_completed(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                final_output=final_output,
                suggestion_id=suggestion_id,
            )

        logger.info(
            "AI task execution complete | task_id=%s status=%s duration=%.1fs",
            task_id,
            "failed" if error_message else "completed",
            (completed_at - started_at).total_seconds(),
        )

    def _run_graph(
        self,
        graph_type: str,
        ctx: Any,
        task_id: str,
        workflow_id: str,
        agent_id: str,
        workspace_root: str,
        task_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the selected LangGraph reasoning graph."""
        from ai.tools.git_tool import get_git_tools
        from ai.tools.build_tool import get_build_tools
        from ai.tools.search_tool import get_search_tools
        from ai.tools.artifact_tool import get_artifact_tools

        # Build tool set
        tools = (
            get_git_tools(workspace_root)
            + get_build_tools(workspace_root, task_id, workflow_id, agent_id)
            + get_search_tools(workspace_root)
            + get_artifact_tools(workspace_root)
        )

        # Build initial state
        base_state: dict[str, Any] = {
            "task_context": ctx,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "agent_id": agent_id,
            "messages": [],
            "step_count": 0,
            "reasoning_steps": [],
            "error": None,
        }

        if graph_type == "code_review":
            from ai.graphs.code_review import build_code_review_graph
            graph = build_code_review_graph(llm=self._llm, workspace_root=workspace_root)
            state = {
                **base_state,
                "diff_content": "",
                "changed_files": [],
                "security_findings": [],
                "quality_findings": [],
                "review_findings": [],
                "review_outcome": "needs_changes",
                "review_summary": "",
                "suggestion_id": "",
            }

        elif graph_type == "fix_validation":
            from ai.graphs.fix_validation import build_fix_validation_graph
            graph = build_fix_validation_graph(llm=self._llm, workspace_root=workspace_root)
            state = {
                **base_state,
                "failure_output": task_inputs.get("failure_output", ""),
                "proposed_fix": task_inputs.get("proposed_fix", ""),
                "root_cause": "",
                "fix_analysis": "",
                "validation_result": "invalid",
                "validation_confidence": 0.0,
                "regression_risks": [],
                "validation_summary": "",
                "suggestion_id": "",
            }

        else:  # research_planner (default)
            from ai.graphs.research_planner import build_research_planner_graph
            graph = build_research_planner_graph(
                llm=self._llm,
                workspace_root=workspace_root,
                tools=tools,
            )
            state = {
                **base_state,
                "sub_questions": [],
                "gathered_context": {},
                "research_findings": "",
                "implementation_plan": "",
                "plan_steps": [],
                "estimated_effort": "unknown",
                "risks": [],
                "dependencies": [],
                "suggestion_id": "",
            }

        # Run the graph
        result = graph.invoke(state)
        return result

    def _get_model_name(self) -> str:
        """Get the model name from the LLM instance."""
        try:
            return getattr(self._llm, "model_name", None) or getattr(self._llm, "model", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"

    # ------------------------------------------------------------------
    # Kafka event emitters
    # ------------------------------------------------------------------

    def _emit_task_accepted(
        self,
        task_id: str,
        workflow_id: str,
        agent_id: str,
    ) -> None:
        """Emit TASK_ACCEPTED event."""
        try:
            payload = TaskAcceptedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            event = build_event(
                event_type=EventType.TASK_ACCEPTED,
                payload=payload,
                source=EventSource.AI_AGENT,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to emit TASK_ACCEPTED: %s", exc)

    def _emit_ai_task_started(
        self,
        task_id: str,
        workflow_id: str,
        agent_id: str,
        session_id: str,
    ) -> None:
        """Emit AI_TASK_STARTED event."""
        try:
            payload = AITaskStartedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                model_name=self._get_model_name(),
                reasoning_session_id=session_id,
            )
            event = build_event(
                event_type=EventType.AI_TASK_STARTED,
                payload=payload,
                source=EventSource.AI_AGENT,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            self._producer.produce(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to emit AI_TASK_STARTED: %s", exc)

    def _emit_reasoning_trace_events(
        self,
        task_id: str,
        agent_id: str,
        session_id: str,
        reasoning_steps: list[dict[str, Any]],
    ) -> None:
        """Emit AI_REASONING_TRACE events for each reasoning step."""
        for step in reasoning_steps:
            try:
                payload = AIReasoningTracePayload(
                    task_id=task_id,
                    agent_id=agent_id,
                    reasoning_session_id=session_id,
                    step_number=step.get("sequence", 1),
                    step_type=step.get("step_type", "thought"),
                    content=step.get("content", "")[:2000],  # Cap content size
                    token_count=step.get("token_count"),
                )
                event = build_event(
                    event_type=EventType.AI_REASONING_TRACE,
                    payload=payload,
                    source=EventSource.AI_AGENT,
                    task_id=task_id,
                    agent_id=agent_id,
                )
                self._producer.produce(event, poll_after=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to emit AI_REASONING_TRACE step: %s", exc)

        # Flush all trace events
        try:
            self._producer.flush(timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to flush reasoning trace events: %s", exc)

    def _emit_task_completed(
        self,
        task_id: str,
        workflow_id: str,
        agent_id: str,
        final_output: str,
        suggestion_id: str,
    ) -> None:
        """Emit TASK_COMPLETED event."""
        try:
            payload = TaskCompletedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                outputs=[
                    {"name": "final_output", "value": final_output},
                    {"name": "suggestion_id", "value": suggestion_id},
                ],
            )
            event = build_event(
                event_type=EventType.TASK_COMPLETED,
                payload=payload,
                source=EventSource.AI_AGENT,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            self._producer.produce_sync(event, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to emit TASK_COMPLETED: %s", exc)

    def _emit_task_failed(
        self,
        task_id: str,
        workflow_id: str,
        agent_id: str,
        error_message: str,
    ) -> None:
        """Emit TASK_FAILED event."""
        try:
            payload = TaskFailedPayload(
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                error_message=error_message,
                error_details={"source": "ai_worker"},
            )
            event = build_event(
                event_type=EventType.TASK_FAILED,
                payload=payload,
                source=EventSource.AI_AGENT,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
            )
            self._producer.produce_sync(event, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to emit TASK_FAILED: %s", exc)


# ---------------------------------------------------------------------------
# AI Worker
# ---------------------------------------------------------------------------


class AIWorker:
    """
    The FlowOS AI Worker.

    Subscribes to Kafka task events and executes AI reasoning tasks using
    LangGraph/LangChain.  Runs as a long-lived process.

    Args:
        agent_id:       The agent ID for this worker instance.
                        Defaults to ``AI_AGENT_ID`` env var or a generated UUID.
        workspace_root: Root directory for workspace operations.
                        Defaults to ``settings.workspace_root``.
        max_concurrent: Maximum number of concurrent tasks.
    """

    def __init__(
        self,
        agent_id: str | None = None,
        workspace_root: str | None = None,
        max_concurrent: int = MAX_CONCURRENT_TASKS,
    ) -> None:
        self._agent_id = agent_id or os.environ.get("AI_AGENT_ID") or f"ai-agent-{str(uuid.uuid4())[:8]}"
        self._workspace_root = workspace_root or settings.workspace_root
        self._max_concurrent = max_concurrent
        self._running = False
        self._active_tasks: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

        # Kafka components
        self._producer = get_producer()
        self._consumer = FlowOSConsumer(
            group_id=ConsumerGroup.AI_AGENT,
            topics=[KafkaTopic.TASK_EVENTS],
        )

        # Task executor
        self._executor = AITaskExecutor(
            producer=self._producer,
            workspace_root=self._workspace_root,
        )

        # Register event handlers programmatically
        self._consumer.register(EventType.TASK_ASSIGNED, self._handle_task_assigned)

        logger.info(
            "AIWorker initialised | agent_id=%s workspace_root=%s max_concurrent=%d",
            self._agent_id,
            self._workspace_root,
            self._max_concurrent,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the AI worker consume loop.

        Blocks until ``stop()`` is called or a signal is received.
        """
        self._running = True
        self._setup_signal_handlers()

        logger.info(
            "AIWorker starting | agent_id=%s",
            self._agent_id,
        )

        try:
            self._consumer.run()
        except KeyboardInterrupt:
            logger.info("AIWorker interrupted by keyboard")
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Stop the AI worker gracefully."""
        logger.info("AIWorker stopping | agent_id=%s", self._agent_id)
        self._running = False
        self._consumer.stop()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_task_assigned(self, event: EventRecord) -> None:
        """
        Handle a TASK_ASSIGNED event.

        Only processes tasks where:
        - task_type is in AI_TASK_TYPES
        - The assigned_agent_id matches this worker's agent_id (or is unset)
        """
        payload = event.payload
        task_id = payload.get("task_id", "")
        task_type = payload.get("task_type", "")
        assigned_agent_id = payload.get("assigned_agent_id", "")

        # Filter: only handle AI task types
        if task_type not in AI_TASK_TYPES:
            logger.debug(
                "Skipping non-AI task | task_id=%s task_type=%s",
                task_id,
                task_type,
            )
            return

        # Filter: only handle tasks assigned to this agent (or broadcast)
        if assigned_agent_id and assigned_agent_id != self._agent_id:
            logger.debug(
                "Skipping task assigned to different agent | task_id=%s assigned_to=%s our_id=%s",
                task_id,
                assigned_agent_id,
                self._agent_id,
            )
            return

        # Check concurrency limit
        with self._lock:
            if len(self._active_tasks) >= self._max_concurrent:
                logger.warning(
                    "Concurrency limit reached, skipping task | task_id=%s active=%d max=%d",
                    task_id,
                    len(self._active_tasks),
                    self._max_concurrent,
                )
                return

            # Check for duplicate task
            if task_id in self._active_tasks:
                logger.warning("Task already being processed | task_id=%s", task_id)
                return

        logger.info(
            "Accepting AI task | task_id=%s task_type=%s agent_id=%s",
            task_id,
            task_type,
            self._agent_id,
        )

        # Execute in a background thread to avoid blocking the consumer
        thread = threading.Thread(
            target=self._execute_task_safe,
            args=(event,),
            name=f"ai-task-{task_id[:8]}",
            daemon=True,
        )

        with self._lock:
            self._active_tasks[task_id] = thread

        thread.start()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _execute_task_safe(self, event: EventRecord) -> None:
        """Execute a task with error handling and cleanup."""
        task_id = event.payload.get("task_id", "unknown")
        try:
            self._executor.execute(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unhandled error in task execution | task_id=%s error=%s",
                task_id,
                exc,
                exc_info=True,
            )
        finally:
            with self._lock:
                self._active_tasks.pop(task_id, None)

    def _setup_signal_handlers(self) -> None:
        """Set up signal handlers for graceful shutdown."""
        def _handle_signal(signum: int, frame: Any) -> None:
            logger.info("Received signal %d, stopping AIWorker", signum)
            self.stop()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    def _shutdown(self) -> None:
        """Perform graceful shutdown."""
        logger.info(
            "AIWorker shutting down | active_tasks=%d",
            len(self._active_tasks),
        )

        # Wait for active tasks to complete (with timeout)
        with self._lock:
            active_threads = list(self._active_tasks.values())

        for thread in active_threads:
            thread.join(timeout=30.0)
            if thread.is_alive():
                logger.warning("Task thread did not finish within timeout: %s", thread.name)

        # Flush producer
        try:
            self._producer.flush(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to flush producer on shutdown: %s", exc)

        logger.info("AIWorker shutdown complete")


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for running the AI worker as a module."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    worker = AIWorker()
    worker.run()


if __name__ == "__main__":
    main()
