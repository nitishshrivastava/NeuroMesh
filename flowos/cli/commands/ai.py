"""
cli/commands/ai.py -- FlowOS AI Agent Commands

Provides the ``flowos ai`` command group for AI agents to interact with
FlowOS.  These commands are designed to be called programmatically by
LangGraph/LangChain agents running inside task boundaries.

Commands::

    flowos ai reason    --task-id <id> --agent-id <id> --prompt TEXT
    flowos ai propose   --task-id <id> --agent-id <id> --patch-file PATH
    flowos ai apply     --task-id <id> --agent-id <id> --proposal-id <id>
    flowos ai reject    --task-id <id> --agent-id <id> --proposal-id <id> [--reason TEXT]
    flowos ai log-step  --session-id <id> --step-type <type> --content TEXT
    flowos ai session   --task-id <id> --agent-id <id> [--model MODEL]

AI agents use these commands to:
- Start and manage reasoning sessions
- Log individual reasoning steps (thoughts, tool calls, observations)
- Propose code patches or file changes for human review
- Apply or reject AI proposals
- Emit AI reasoning events to Kafka for the observability layer

All commands emit events to the ``flowos.ai.events`` Kafka topic.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from shared.models.event import EventSource, EventType
from shared.models.reasoning import ReasoningStepType, ReasoningStatus

logger = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _output_json(data: Any) -> None:
    """Print data as formatted JSON to stdout."""
    click.echo(json.dumps(data, indent=2, default=str))


def _success(message: str) -> None:
    console.print(f"[bold green]OK[/bold green] {message}")


def _error(message: str) -> None:
    err_console.print(f"[bold red]ERROR[/bold red] {message}")
    sys.exit(1)


def _warn(message: str) -> None:
    console.print(f"[bold yellow]WARN[/bold yellow] {message}")


def _emit_ai_event(
    event_type: EventType,
    agent_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Emit an AI reasoning event to Kafka (flowos.ai.events topic).

    Returns True on success, False if Kafka is unavailable (non-fatal).
    """
    try:
        from shared.kafka.producer import FlowOSProducer
        from shared.models.event import EventRecord, EventTopic

        payload_data: dict[str, Any] = {
            "agent_id": agent_id,
            "task_id": task_id or "",
            "session_id": session_id or "",
        }
        if extra:
            payload_data.update(extra)

        event = EventRecord(
            event_type=event_type,
            topic=EventTopic.AI_EVENTS,
            source=EventSource.CLI,
            payload=payload_data,
            metadata={
                "agent_id": agent_id,
                "task_id": task_id or "",
                "session_id": session_id or "",
            },
        )

        producer = FlowOSProducer()
        producer.produce(event)
        producer.flush(timeout=5.0)
        return True
    except Exception as exc:
        logger.debug("Failed to emit AI Kafka event %s: %s", event_type, exc)
        return False


# ---------------------------------------------------------------------------
# ``flowos ai`` group
# ---------------------------------------------------------------------------


@click.group("ai")
@click.pass_context
def ai_group(ctx: click.Context) -> None:
    """AI agent commands (reasoning, proposals, patch application)."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# ``flowos ai session``
# ---------------------------------------------------------------------------


@ai_group.command("session")
@click.option(
    "--task-id",
    required=True,
    help="Task ID this reasoning session is for.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="AI agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--session-id",
    default=None,
    help="Explicit session ID (auto-generated if omitted).",
    metavar="UUID",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name used for this session (e.g. 'gpt-4o', 'claude-3-5-sonnet').",
    metavar="MODEL",
)
@click.option(
    "--objective",
    default=None,
    help="High-level objective for this reasoning session.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AI_REASONING_STARTED Kafka event.",
)
@click.pass_context
def ai_session(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    session_id: str | None,
    model: str | None,
    objective: str | None,
    no_event: bool,
) -> None:
    """
    Start a new AI reasoning session for a task.

    Creates a reasoning session record and emits an AI_REASONING_STARTED
    event.  The session ID should be used in subsequent ``flowos ai log-step``
    calls to associate steps with this session.

    Example::

        SESSION_ID=$(flowos ai session \\
            --task-id abc123 \\
            --agent-id ai-agent-01 \\
            --model gpt-4o \\
            --objective "Implement the authentication module" \\
            --json-output | jq -r .session_id)
    """
    json_output = ctx.obj.get("json_output", False)

    sid = session_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_ai_event(
            event_type=EventType.AI_TASK_STARTED,
            agent_id=agent_id,
            task_id=task_id,
            session_id=sid,
            extra={
                "model_name": model,
                "objective": objective,
                "started_at": started_at.isoformat(),
                "status": ReasoningStatus.RUNNING,
            },
        )

    result = {
        "session_id": sid,
        "task_id": task_id,
        "agent_id": agent_id,
        "model": model,
        "objective": objective,
        "status": "running",
        "started_at": started_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Reasoning session started: {sid[:8]}...")
    console.print(f"  [dim]Task ID:[/dim]    {task_id}")
    console.print(f"  [dim]Agent ID:[/dim]   {agent_id}")
    if model:
        console.print(f"  [dim]Model:[/dim]     {model}")
    if objective:
        console.print(f"  [dim]Objective:[/dim] {objective}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos ai log-step``
# ---------------------------------------------------------------------------


@ai_group.command("log-step")
@click.option(
    "--session-id",
    required=True,
    help="Reasoning session ID.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="AI agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--task-id",
    default=None,
    help="Task ID this step belongs to.",
    metavar="UUID",
)
@click.option(
    "--step-type",
    required=True,
    type=click.Choice(
        ["thought", "tool_call", "observation", "plan", "decision",
         "proposal", "reflection", "human_input", "final_answer", "error"],
        case_sensitive=False,
    ),
    help="Type of reasoning step.",
)
@click.option(
    "--content",
    required=True,
    help="Text content of this reasoning step.",
    metavar="TEXT",
)
@click.option(
    "--sequence",
    default=None,
    type=int,
    help="Step sequence number within the session.",
    metavar="N",
)
@click.option(
    "--model",
    default=None,
    help="LLM model used for this step.",
    metavar="MODEL",
)
@click.option(
    "--tokens",
    "total_tokens",
    default=None,
    type=int,
    help="Total tokens consumed for this step.",
    metavar="N",
)
@click.option(
    "--confidence",
    default=None,
    type=click.FloatRange(0.0, 1.0),
    help="AI self-reported confidence (0.0-1.0).",
    metavar="FLOAT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AI_REASONING_STEP Kafka event.",
)
@click.pass_context
def ai_log_step(
    ctx: click.Context,
    session_id: str,
    agent_id: str,
    task_id: str | None,
    step_type: str,
    content: str,
    sequence: int | None,
    model: str | None,
    total_tokens: int | None,
    confidence: float | None,
    no_event: bool,
) -> None:
    """
    Log a single reasoning step to a session.

    Records a thought, tool call, observation, plan, or other reasoning step
    and emits it as an AI_REASONING_STEP event to Kafka.

    Examples::

        flowos ai log-step \\
            --session-id $SESSION_ID \\
            --agent-id ai-agent-01 \\
            --step-type thought \\
            --content "I need to analyse the existing auth module first"

        flowos ai log-step \\
            --session-id $SESSION_ID \\
            --agent-id ai-agent-01 \\
            --step-type plan \\
            --content "1. Read existing code\\n2. Identify gaps\\n3. Implement changes"

        flowos ai log-step \\
            --session-id $SESSION_ID \\
            --agent-id ai-agent-01 \\
            --step-type final_answer \\
            --content "Implementation complete. See proposal for changes." \\
            --confidence 0.92
    """
    json_output = ctx.obj.get("json_output", False)

    step_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    event_emitted = False

    step_data = {
        "step_id": step_id,
        "session_id": session_id,
        "step_type": step_type,
        "sequence": sequence or 1,
        "content": content,
        "model_name": model,
        "total_tokens": total_tokens,
        "confidence": confidence,
        "created_at": created_at.isoformat(),
    }

    if not no_event:
        event_emitted = _emit_ai_event(
            event_type=EventType.AI_REASONING_TRACE,
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            extra=step_data,
        )

    result = {
        **step_data,
        "agent_id": agent_id,
        "task_id": task_id,
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    step_color = {
        "thought": "cyan",
        "tool_call": "yellow",
        "observation": "blue",
        "plan": "green",
        "decision": "magenta",
        "proposal": "bright_green",
        "reflection": "dim",
        "human_input": "white",
        "final_answer": "bold green",
        "error": "red",
    }.get(step_type, "white")

    console.print(
        f"[{step_color}]{step_type.upper()}[/{step_color}] "
        f"Step logged to session {session_id[:8]}..."
    )
    if len(content) > 80:
        console.print(f"  [dim]{content[:80]}...[/dim]")
    else:
        console.print(f"  [dim]{content}[/dim]")
    if confidence is not None:
        console.print(f"  [dim]Confidence:[/dim] {confidence:.0%}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos ai reason``
# ---------------------------------------------------------------------------


@ai_group.command("reason")
@click.option(
    "--task-id",
    required=True,
    help="Task ID to reason about.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="AI agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--prompt",
    required=True,
    help="Reasoning prompt / task description.",
    metavar="TEXT",
)
@click.option(
    "--model",
    default=None,
    envvar="FLOWOS_AI_MODEL",
    help="LLM model to use.  Defaults to $FLOWOS_AI_MODEL.",
    metavar="MODEL",
)
@click.option(
    "--session-id",
    default=None,
    help="Existing session ID to continue.  Creates a new session if omitted.",
    metavar="UUID",
)
@click.option(
    "--max-steps",
    default=10,
    show_default=True,
    type=int,
    help="Maximum number of reasoning steps.",
    metavar="N",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit AI reasoning Kafka events.",
)
@click.pass_context
def ai_reason(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    prompt: str,
    model: str | None,
    session_id: str | None,
    max_steps: int,
    no_event: bool,
) -> None:
    """
    Start an AI reasoning session for a task.

    Creates a reasoning session, logs the initial prompt as a THOUGHT step,
    and emits the appropriate Kafka events.  This command is the entry point
    for AI agents to begin working on a task.

    In a full deployment, this command would invoke the LangGraph agent loop.
    In the current implementation, it creates the session record and emits
    events so the orchestrator and UI can track AI activity.

    Examples::

        flowos ai reason \\
            --task-id abc123 \\
            --agent-id ai-agent-01 \\
            --prompt "Implement the user authentication module" \\
            --model gpt-4o

        flowos ai reason \\
            --task-id abc123 \\
            --agent-id ai-agent-01 \\
            --prompt "Review the PR and suggest improvements" \\
            --session-id existing-session-id
    """
    json_output = ctx.obj.get("json_output", False)

    sid = session_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    # Start the session
    if not no_event:
        _emit_ai_event(
            event_type=EventType.AI_TASK_STARTED,
            agent_id=agent_id,
            task_id=task_id,
            session_id=sid,
            extra={
                "model_name": model,
                "objective": prompt,
                "started_at": started_at.isoformat(),
                "status": ReasoningStatus.RUNNING,
                "max_steps": max_steps,
            },
        )

    # Log the initial prompt as a THOUGHT step
    if not no_event:
        _emit_ai_event(
            event_type=EventType.AI_REASONING_TRACE,
            agent_id=agent_id,
            task_id=task_id,
            session_id=sid,
            extra={
                "step_id": str(uuid.uuid4()),
                "step_type": ReasoningStepType.THOUGHT,
                "sequence": 1,
                "content": f"Starting reasoning for task: {prompt}",
                "model_name": model,
                "created_at": started_at.isoformat(),
            },
        )

    result = {
        "session_id": sid,
        "task_id": task_id,
        "agent_id": agent_id,
        "model": model,
        "prompt": prompt,
        "status": "running",
        "max_steps": max_steps,
        "started_at": started_at.isoformat(),
        "note": (
            "Reasoning session started. Use 'flowos ai log-step' to record "
            "reasoning steps, and 'flowos ai propose' to submit proposals."
        ),
    }

    if json_output:
        _output_json(result)
        return

    console.print()
    console.print(
        Panel.fit(
            f"[bold cyan]AI Reasoning Session Started[/bold cyan]\n\n"
            f"[dim]Session ID:[/dim] {sid}\n"
            f"[dim]Task ID:[/dim]    {task_id}\n"
            f"[dim]Agent ID:[/dim]   {agent_id}\n"
            f"[dim]Model:[/dim]      {model or '(default)'}\n"
            f"[dim]Max Steps:[/dim]  {max_steps}\n\n"
            f"[dim]Prompt:[/dim]\n{prompt[:200]}{'...' if len(prompt) > 200 else ''}",
            title="[bold]flowos ai reason[/bold]",
            border_style="cyan",
        )
    )
    console.print()
    console.print(
        "[dim]Use [bold]flowos ai log-step --session-id[/bold] to record reasoning steps.[/dim]"
    )
    console.print(
        "[dim]Use [bold]flowos ai propose[/bold] to submit a code proposal.[/dim]"
    )


# ---------------------------------------------------------------------------
# ``flowos ai propose``
# ---------------------------------------------------------------------------


@ai_group.command("propose")
@click.option(
    "--task-id",
    required=True,
    help="Task ID this proposal is for.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="AI agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--session-id",
    default=None,
    help="Reasoning session ID that produced this proposal.",
    metavar="UUID",
)
@click.option(
    "--patch-file",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True),
    help="Path to a unified diff patch file.",
    metavar="PATH",
)
@click.option(
    "--description",
    default=None,
    help="Human-readable description of the proposed changes.",
    metavar="TEXT",
)
@click.option(
    "--confidence",
    default=None,
    type=click.FloatRange(0.0, 1.0),
    help="AI confidence in this proposal (0.0-1.0).",
    metavar="FLOAT",
)
@click.option(
    "--requires-review",
    "requires_review",
    is_flag=True,
    default=True,
    help="Whether this proposal requires human review before applying.",
)
@click.option(
    "--workspace-dir",
    default=None,
    help="Workspace directory where the patch should be applied.",
    metavar="DIR",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AI_PROPOSAL_CREATED Kafka event.",
)
@click.pass_context
def ai_propose(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    session_id: str | None,
    patch_file: str | None,
    description: str | None,
    confidence: float | None,
    requires_review: bool,
    workspace_dir: str | None,
    no_event: bool,
) -> None:
    """
    Submit an AI-generated code proposal for review.

    Creates a proposal record and emits an AI_PROPOSAL_CREATED event.
    The proposal can be a unified diff patch file or a description of
    changes to be made.

    Examples::

        flowos ai propose \\
            --task-id abc123 \\
            --agent-id ai-agent-01 \\
            --patch-file /tmp/auth_changes.patch \\
            --description "Add JWT authentication to the API" \\
            --confidence 0.85

        flowos ai propose \\
            --task-id abc123 \\
            --agent-id ai-agent-01 \\
            --description "Refactor the database connection pool" \\
            --requires-review
    """
    json_output = ctx.obj.get("json_output", False)

    proposal_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)

    # Read patch content if provided
    patch_content: str | None = None
    if patch_file:
        try:
            patch_content = Path(patch_file).read_text(encoding="utf-8")
        except OSError as exc:
            _error(f"Cannot read patch file: {exc}")
            return

    # Save patch to workspace patches directory if workspace_dir is provided
    patch_saved_path: str | None = None
    if patch_content and workspace_dir:
        patches_dir = Path(workspace_dir) / "patches"
        patches_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patches_dir / f"proposal-{proposal_id[:8]}.patch"
        try:
            patch_path.write_text(patch_content, encoding="utf-8")
            patch_saved_path = str(patch_path)
        except OSError as exc:
            _warn(f"Could not save patch to workspace: {exc}")

    event_emitted = False
    if not no_event:
        event_emitted = _emit_ai_event(
            event_type=EventType.AI_PATCH_PROPOSED,
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            extra={
                "proposal_id": proposal_id,
                "description": description,
                "confidence": confidence,
                "requires_review": requires_review,
                "has_patch": patch_content is not None,
                "patch_size_bytes": len(patch_content.encode()) if patch_content else 0,
                "patch_saved_path": patch_saved_path,
                "created_at": created_at.isoformat(),
            },
        )

    result = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "description": description,
        "confidence": confidence,
        "requires_review": requires_review,
        "has_patch": patch_content is not None,
        "patch_saved_path": patch_saved_path,
        "status": "pending_review" if requires_review else "ready_to_apply",
        "created_at": created_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Proposal created: {proposal_id[:8]}...")
    console.print(f"  [dim]Task ID:[/dim]        {task_id}")
    if description:
        console.print(f"  [dim]Description:[/dim]   {description[:80]}")
    if confidence is not None:
        console.print(f"  [dim]Confidence:[/dim]    {confidence:.0%}")
    console.print(
        f"  [dim]Review required:[/dim] {'yes' if requires_review else 'no'}"
    )
    if patch_saved_path:
        console.print(f"  [dim]Patch saved:[/dim]    {patch_saved_path}")
    if patch_content:
        console.print()
        console.print("[dim]Patch preview (first 20 lines):[/dim]")
        preview_lines = patch_content.split("\n")[:20]
        preview = "\n".join(preview_lines)
        try:
            syntax = Syntax(preview, "diff", theme="monokai", line_numbers=True)
            console.print(syntax)
        except Exception:
            console.print(preview)
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos ai apply``
# ---------------------------------------------------------------------------


@ai_group.command("apply")
@click.option(
    "--task-id",
    required=True,
    help="Task ID this proposal belongs to.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID applying the proposal.",
    metavar="UUID",
)
@click.option(
    "--proposal-id",
    required=True,
    help="Proposal ID to apply.",
    metavar="UUID",
)
@click.option(
    "--patch-file",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True),
    help="Path to the patch file to apply (if not already saved in workspace).",
    metavar="PATH",
)
@click.option(
    "--workspace-dir",
    default=None,
    help="Workspace directory to apply the patch in.",
    metavar="DIR",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AI_PROPOSAL_APPLIED Kafka event.",
)
@click.pass_context
def ai_apply(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    proposal_id: str,
    patch_file: str | None,
    workspace_dir: str | None,
    no_event: bool,
) -> None:
    """
    Apply an AI-generated proposal (patch) to the workspace.

    Applies the patch file associated with the proposal and emits an
    AI_PROPOSAL_APPLIED event.  After applying, create a checkpoint with
    ``flowos workspace checkpoint`` to record the change.

    Examples::

        flowos ai apply \\
            --task-id abc123 \\
            --agent-id human-reviewer \\
            --proposal-id prop-456 \\
            --workspace-dir /flowos/workspaces/my-workspace

        # Apply a specific patch file
        flowos ai apply \\
            --task-id abc123 \\
            --agent-id human-reviewer \\
            --proposal-id prop-456 \\
            --patch-file /tmp/changes.patch \\
            --workspace-dir /flowos/workspaces/my-workspace
    """
    json_output = ctx.obj.get("json_output", False)

    applied_at = datetime.now(timezone.utc)

    # Determine patch file path
    effective_patch_file = patch_file
    if not effective_patch_file and workspace_dir:
        # Look for the patch in the workspace patches directory
        patches_dir = Path(workspace_dir) / "patches"
        candidate = patches_dir / f"proposal-{proposal_id[:8]}.patch"
        if candidate.exists():
            effective_patch_file = str(candidate)

    # Apply the patch if we have a file
    apply_result: dict[str, Any] = {"applied": False, "error": None}
    if effective_patch_file:
        import subprocess
        cwd = workspace_dir or "."
        try:
            result = subprocess.run(
                ["git", "apply", "--check", effective_patch_file],
                capture_output=True,
                text=True,
                cwd=cwd,
            )
            if result.returncode == 0:
                # Patch applies cleanly, apply it
                result2 = subprocess.run(
                    ["git", "apply", effective_patch_file],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                )
                if result2.returncode == 0:
                    apply_result = {"applied": True, "error": None}
                else:
                    apply_result = {"applied": False, "error": result2.stderr}
            else:
                apply_result = {
                    "applied": False,
                    "error": f"Patch does not apply cleanly: {result.stderr}",
                }
        except FileNotFoundError:
            apply_result = {
                "applied": False,
                "error": "git not found in PATH",
            }
        except Exception as exc:
            apply_result = {"applied": False, "error": str(exc)}
    else:
        apply_result = {
            "applied": False,
            "error": "No patch file found. Provide --patch-file or ensure the patch is in the workspace.",
        }

    event_emitted = False
    if not no_event:
        event_emitted = _emit_ai_event(
            event_type=EventType.AI_REVIEW_COMPLETED,
            agent_id=agent_id,
            task_id=task_id,
            extra={
                "proposal_id": proposal_id,
                "applied": apply_result["applied"],
                "error": apply_result.get("error"),
                "patch_file": effective_patch_file,
                "applied_at": applied_at.isoformat(),
            },
        )

    result_data = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "applied": apply_result["applied"],
        "error": apply_result.get("error"),
        "patch_file": effective_patch_file,
        "applied_at": applied_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result_data)
        return

    if apply_result["applied"]:
        _success(f"Proposal {proposal_id[:8]}... applied successfully.")
        console.print(
            "[dim]Run 'flowos workspace checkpoint' to record this change.[/dim]"
        )
    else:
        _error(
            f"Failed to apply proposal {proposal_id[:8]}...: "
            f"{apply_result.get('error', 'Unknown error')}"
        )


# ---------------------------------------------------------------------------
# ``flowos ai reject``
# ---------------------------------------------------------------------------


@ai_group.command("reject")
@click.option(
    "--task-id",
    required=True,
    help="Task ID this proposal belongs to.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID rejecting the proposal.",
    metavar="UUID",
)
@click.option(
    "--proposal-id",
    required=True,
    help="Proposal ID to reject.",
    metavar="UUID",
)
@click.option(
    "--reason",
    default=None,
    help="Human-readable reason for rejection.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AI_PROPOSAL_REJECTED Kafka event.",
)
@click.pass_context
def ai_reject(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    proposal_id: str,
    reason: str | None,
    no_event: bool,
) -> None:
    """
    Reject an AI-generated proposal.

    Emits an AI_PROPOSAL_REJECTED event so the AI agent can revise its
    approach.

    Example::

        flowos ai reject \\
            --task-id abc123 \\
            --agent-id human-reviewer \\
            --proposal-id prop-456 \\
            --reason "The approach is correct but the implementation has a bug in line 42"
    """
    json_output = ctx.obj.get("json_output", False)

    rejected_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_ai_event(
            event_type=EventType.AI_SUGGESTION_CREATED,
            agent_id=agent_id,
            task_id=task_id,
            extra={
                "proposal_id": proposal_id,
                "reason": reason,
                "rejected_at": rejected_at.isoformat(),
            },
        )

    result = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "status": "rejected",
        "reason": reason,
        "rejected_at": rejected_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    console.print(
        f"[bold yellow]REJECTED[/bold yellow] Proposal {proposal_id[:8]}... rejected."
    )
    if reason:
        console.print(f"  [dim]Reason:[/dim] {reason}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")
