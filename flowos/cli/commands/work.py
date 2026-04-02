"""
cli/commands/work.py -- FlowOS Task Work Commands (Human-Oriented)

Provides the ``flowos work`` command group for human agents to interact with
tasks assigned to them: accepting, starting, updating progress, completing,
failing, and handing off tasks.

Commands::

    flowos work accept   <task-id> --agent-id <id>
    flowos work start    <task-id> --agent-id <id>
    flowos work update   <task-id> --agent-id <id> [--progress N] [--message TEXT]
    flowos work complete <task-id> --agent-id <id> [--output key=value ...]
    flowos work fail     <task-id> --agent-id <id> --reason <text>
    flowos work handoff  <task-id> --agent-id <id> --to-agent <id> [--message TEXT]
    flowos work list     [--agent-id <id>] [--status <status>]
    flowos work show     <task-id>

All commands emit Kafka events to the ``flowos.task.events`` topic so the
orchestrator and observability layer can track task lifecycle transitions.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shared.models.event import EventSource, EventType
from shared.models.task import TaskStatus, TaskType, TaskPriority

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


def _parse_output_pairs(raw: tuple[str, ...]) -> list[dict[str, Any]]:
    """
    Parse ``key=value`` pairs from CLI ``--output`` options into TaskOutput
    dicts.
    """
    outputs = []
    for item in raw:
        if "=" not in item:
            raise click.BadParameter(
                f"Output {item!r} must be in key=value format.",
                param_hint="--output",
            )
        key, _, raw_value = item.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        # Try JSON decode
        if raw_value and raw_value[0] in ("{", "[", '"'):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                value = raw_value
        elif raw_value.lower() in ("true", "false"):
            value = raw_value.lower() == "true"
        elif raw_value.isdigit():
            value = int(raw_value)
        else:
            value = raw_value
        outputs.append({"name": key, "value": value})
    return outputs


def _emit_task_event(
    event_type: EventType,
    task_id: str,
    agent_id: str,
    workflow_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Emit a task lifecycle event to Kafka.

    Returns True on success, False if Kafka is unavailable (non-fatal).
    """
    try:
        from shared.kafka.producer import FlowOSProducer
        from shared.kafka.schemas import build_event

        payload_data: dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "workflow_id": workflow_id or "",
        }
        if extra:
            payload_data.update(extra)

        # Build a minimal event record
        from shared.models.event import EventRecord, EventTopic
        from shared.kafka.topics import topic_for_event

        event = EventRecord(
            event_type=event_type,
            topic=EventTopic.TASK_EVENTS,
            source=EventSource.CLI,
            payload=payload_data,
            metadata={
                "agent_id": agent_id,
                "task_id": task_id,
            },
        )

        producer = FlowOSProducer()
        producer.produce(event)
        producer.flush(timeout=5.0)
        return True
    except Exception as exc:
        logger.debug("Failed to emit Kafka event %s: %s", event_type, exc)
        return False


# ---------------------------------------------------------------------------
# ``flowos work`` group
# ---------------------------------------------------------------------------


@click.group("work")
@click.pass_context
def work_group(ctx: click.Context) -> None:
    """Task work commands for human agents (accept, start, update, complete, etc.)."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# ``flowos work accept``
# ---------------------------------------------------------------------------


@work_group.command("accept")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to (for Kafka event routing).",
    metavar="UUID",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_ACCEPTED Kafka event.",
)
@click.pass_context
def work_accept(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    no_event: bool,
) -> None:
    """
    Accept a task assignment.

    Signals to the orchestrator that you have received and accepted the task.
    The task status transitions from ASSIGNED to ACCEPTED.

    Example::

        flowos work accept abc12345-... --agent-id $(uuidgen)
        FLOWOS_AGENT_ID=my-agent-id flowos work accept abc12345-...
    """
    json_output = ctx.obj.get("json_output", False)

    accepted_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_ACCEPTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={"accepted_at": accepted_at.isoformat()},
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "accepted",
        "accepted_at": accepted_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Task {task_id[:8]}... accepted by agent {agent_id[:8]}...")
    console.print(f"  [dim]Accepted at:[/dim] {accepted_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work start``
# ---------------------------------------------------------------------------


@work_group.command("start")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--attempt",
    "attempt_number",
    default=1,
    type=int,
    help="Attempt number (default: 1).",
    metavar="N",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_STARTED Kafka event.",
)
@click.pass_context
def work_start(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    attempt_number: int,
    no_event: bool,
) -> None:
    """
    Mark a task as started (in-progress).

    Signals to the orchestrator that you have begun executing the task.
    The task status transitions from ACCEPTED to IN_PROGRESS.

    Example::

        flowos work start abc12345-... --agent-id my-agent-id
    """
    json_output = ctx.obj.get("json_output", False)

    started_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_STARTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "started_at": started_at.isoformat(),
                "attempt_number": attempt_number,
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "in_progress",
        "started_at": started_at.isoformat(),
        "attempt_number": attempt_number,
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Task {task_id[:8]}... started (attempt #{attempt_number}).")
    console.print(f"  [dim]Started at:[/dim] {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work update``
# ---------------------------------------------------------------------------


@work_group.command("update")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--progress",
    "progress_pct",
    default=None,
    type=click.IntRange(0, 100),
    help="Task completion percentage (0-100).",
    metavar="PCT",
)
@click.option(
    "--message",
    default=None,
    help="Human-readable progress update message.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_UPDATED Kafka event.",
)
@click.pass_context
def work_update(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    progress_pct: int | None,
    message: str | None,
    no_event: bool,
) -> None:
    """
    Send a progress update for a task.

    Emits a TASK_UPDATED event with optional progress percentage and message.
    Does not change the task status.

    Examples::

        flowos work update abc12345-... --agent-id my-id --progress 50
        flowos work update abc12345-... --agent-id my-id --message "Halfway done"
        flowos work update abc12345-... --agent-id my-id --progress 75 --message "Almost there"
    """
    json_output = ctx.obj.get("json_output", False)

    if progress_pct is None and message is None:
        _error("Provide at least --progress or --message.")
        return

    event_emitted = False
    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_UPDATED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "update_type": "progress",
                "progress_pct": progress_pct,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "progress_pct": progress_pct,
        "message": message,
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    parts = []
    if progress_pct is not None:
        parts.append(f"{progress_pct}%")
    if message:
        parts.append(f'"{message}"')
    _success(f"Task {task_id[:8]}... updated: {', '.join(parts)}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work complete``
# ---------------------------------------------------------------------------


@work_group.command("complete")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--output",
    "outputs",
    multiple=True,
    help="Output parameter in key=value format (can be repeated).",
    metavar="KEY=VALUE",
)
@click.option(
    "--message",
    default=None,
    help="Optional completion message.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_COMPLETED Kafka event.",
)
@click.pass_context
def work_complete(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    outputs: tuple[str, ...],
    message: str | None,
    no_event: bool,
) -> None:
    """
    Mark a task as completed.

    Signals to the orchestrator that the task is done.  Optionally provide
    output parameters that will be passed to downstream tasks.

    Examples::

        flowos work complete abc12345-... --agent-id my-id
        flowos work complete abc12345-... --agent-id my-id \\
            --output artifact_url=s3://bucket/artifact.tar.gz \\
            --output review_status=approved
    """
    json_output = ctx.obj.get("json_output", False)

    try:
        parsed_outputs = _parse_output_pairs(outputs)
    except click.BadParameter as exc:
        _error(str(exc))
        return

    completed_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_COMPLETED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "outputs": parsed_outputs,
                "message": message,
                "completed_at": completed_at.isoformat(),
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "completed",
        "outputs": parsed_outputs,
        "message": message,
        "completed_at": completed_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Task {task_id[:8]}... completed.")
    console.print(f"  [dim]Completed at:[/dim] {completed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if parsed_outputs:
        console.print(f"  [dim]Outputs:[/dim]      {len(parsed_outputs)} parameter(s)")
        for out in parsed_outputs:
            console.print(f"    [cyan]{out['name']}[/cyan] = {out['value']}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work fail``
# ---------------------------------------------------------------------------


@work_group.command("fail")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--reason",
    required=True,
    help="Human-readable reason for the failure.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_FAILED Kafka event.",
)
@click.pass_context
def work_fail(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    reason: str,
    no_event: bool,
) -> None:
    """
    Mark a task as failed.

    Signals to the orchestrator that the task could not be completed.
    The orchestrator may retry the task or fail the workflow depending on
    the retry policy.

    Example::

        flowos work fail abc12345-... --agent-id my-id --reason "Build environment unavailable"
    """
    json_output = ctx.obj.get("json_output", False)

    failed_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_FAILED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "error_message": reason,
                "failed_at": failed_at.isoformat(),
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "failed",
        "reason": reason,
        "failed_at": failed_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    err_console.print(f"[bold red]FAIL[/bold red] Task {task_id[:8]}... marked as failed.")
    console.print(f"  [dim]Reason:[/dim]    {reason}")
    console.print(f"  [dim]Failed at:[/dim] {failed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work handoff``
# ---------------------------------------------------------------------------


@work_group.command("handoff")
@click.argument("task_id")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Your agent ID (the source agent).",
    metavar="UUID",
)
@click.option(
    "--to-agent",
    "to_agent_id",
    required=True,
    help="Target agent ID to hand off to.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--handoff-type",
    default="delegation",
    type=click.Choice(
        ["delegation", "escalation", "completion", "review", "emergency",
         "scheduled", "ai_to_human", "human_to_ai"],
        case_sensitive=False,
    ),
    help="Type of handoff.",
)
@click.option(
    "--message",
    default=None,
    help="Context message for the receiving agent.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a TASK_HANDOFF_REQUESTED Kafka event.",
)
@click.pass_context
def work_handoff(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    to_agent_id: str,
    workflow_id: str | None,
    handoff_type: str,
    message: str | None,
    no_event: bool,
) -> None:
    """
    Request a task handoff to another agent.

    Signals to the orchestrator that you want to transfer task ownership to
    another agent.  The target agent will receive a TASK_HANDOFF_REQUESTED
    event and must accept before the handoff completes.

    Examples::

        # Human delegates to AI
        flowos work handoff abc12345-... \\
            --agent-id human-agent-id \\
            --to-agent ai-agent-id \\
            --handoff-type human_to_ai \\
            --message "Please implement the auth module per the plan"

        # AI escalates to human
        flowos work handoff abc12345-... \\
            --agent-id ai-agent-id \\
            --to-agent human-agent-id \\
            --handoff-type ai_to_human \\
            --message "Need human review before proceeding"
    """
    json_output = ctx.obj.get("json_output", False)

    import uuid as _uuid
    handoff_id = str(_uuid.uuid4())
    requested_at = datetime.now(timezone.utc)
    event_emitted = False

    if not no_event:
        event_emitted = _emit_task_event(
            event_type=EventType.TASK_HANDOFF_REQUESTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "handoff_id": handoff_id,
                "from_agent_id": agent_id,
                "to_agent_id": to_agent_id,
                "handoff_type": handoff_type,
                "message": message,
                "requested_at": requested_at.isoformat(),
            },
        )

    result = {
        "task_id": task_id,
        "handoff_id": handoff_id,
        "from_agent_id": agent_id,
        "to_agent_id": to_agent_id,
        "workflow_id": workflow_id,
        "handoff_type": handoff_type,
        "message": message,
        "status": "requested",
        "requested_at": requested_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(
        f"Handoff requested: task {task_id[:8]}... "
        f"from {agent_id[:8]}... to {to_agent_id[:8]}..."
    )
    console.print(f"  [dim]Handoff ID:[/dim]   {handoff_id}")
    console.print(f"  [dim]Type:[/dim]         {handoff_type}")
    if message:
        console.print(f"  [dim]Message:[/dim]      {message}")
    console.print(
        f"  [dim]Requested at:[/dim] {requested_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos work list``
# ---------------------------------------------------------------------------


@work_group.command("list")
@click.option(
    "--agent-id",
    default=None,
    envvar="FLOWOS_AGENT_ID",
    help="Filter tasks by agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Filter tasks by workflow ID.",
    metavar="UUID",
)
@click.option(
    "--status",
    "filter_status",
    default=None,
    type=click.Choice(
        ["pending", "assigned", "accepted", "in_progress", "checkpointed",
         "completed", "failed", "cancelled", "all"],
        case_sensitive=False,
    ),
    help="Filter by task status.",
)
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of tasks to show.",
    metavar="N",
)
@click.pass_context
def work_list(
    ctx: click.Context,
    agent_id: str | None,
    workflow_id: str | None,
    filter_status: str | None,
    limit: int,
) -> None:
    """
    List tasks assigned to an agent.

    Shows tasks from the local workspace state.  For a full view of all
    tasks in the system, use the FlowOS API or Temporal UI.

    Examples::

        flowos work list --agent-id my-agent-id
        flowos work list --status in_progress
        FLOWOS_AGENT_ID=my-id flowos work list
    """
    json_output = ctx.obj.get("json_output", False)

    # This is a local-state command; in a full implementation it would query
    # the database.  Here we provide a structured response indicating the
    # query parameters and how to get full data.
    result = {
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "filter_status": filter_status,
        "limit": limit,
        "tasks": [],
        "note": (
            "Task listing requires database connectivity. "
            "Use 'flowos workflow status <id>' to check workflow state, "
            "or query the FlowOS API at http://localhost:8000/api/v1/tasks"
        ),
    }

    if json_output:
        _output_json(result)
        return

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Agent ID:[/dim]    {agent_id or '(all)'}\n"
            f"[dim]Workflow ID:[/dim] {workflow_id or '(all)'}\n"
            f"[dim]Status:[/dim]     {filter_status or 'all'}\n"
            f"[dim]Limit:[/dim]      {limit}\n\n"
            "[yellow]Task listing requires database connectivity.[/yellow]\n"
            "Use [bold]flowos workflow status <id>[/bold] to check workflow state,\n"
            "or query the FlowOS API at [cyan]http://localhost:8000/api/v1/tasks[/cyan]",
            title="[bold]flowos work list[/bold]",
            border_style="yellow",
        )
    )


# ---------------------------------------------------------------------------
# ``flowos work show``
# ---------------------------------------------------------------------------


@work_group.command("show")
@click.argument("task_id")
@click.pass_context
def work_show(
    ctx: click.Context,
    task_id: str,
) -> None:
    """
    Show details of a specific task.

    TASK_ID is the full or abbreviated task UUID.

    Example::

        flowos work show abc12345-...
    """
    json_output = ctx.obj.get("json_output", False)

    result = {
        "task_id": task_id,
        "note": (
            "Task detail lookup requires database connectivity. "
            "Use 'flowos workflow status <workflow-id>' to check workflow state, "
            "or query the FlowOS API at "
            f"http://localhost:8000/api/v1/tasks/{task_id}"
        ),
    }

    if json_output:
        _output_json(result)
        return

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Task ID:[/dim] {task_id}\n\n"
            "[yellow]Task detail lookup requires database connectivity.[/yellow]\n"
            "Query the FlowOS API at:\n"
            f"[cyan]http://localhost:8000/api/v1/tasks/{task_id}[/cyan]",
            title="[bold]flowos work show[/bold]",
            border_style="yellow",
        )
    )
