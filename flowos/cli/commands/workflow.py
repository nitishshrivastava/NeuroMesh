"""
cli/commands/workflow.py -- FlowOS Workflow Management Commands

Provides the ``flowos workflow`` command group for starting, stopping,
pausing, resuming, and inspecting FlowOS workflows.

Commands::

    flowos workflow start --definition <path.yaml> [--input key=value ...]
    flowos workflow stop  <workflow-id> [--reason <text>]
    flowos workflow pause <workflow-id> [--reason <text>]
    flowos workflow resume <workflow-id>
    flowos workflow status <workflow-id>
    flowos workflow list   [--status running|pending|...] [--limit N]
    flowos workflow validate --definition <path.yaml>

All commands that interact with Temporal connect to the server configured via
``TEMPORAL_HOST`` / ``TEMPORAL_PORT`` environment variables (or defaults).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shared.config import settings
from shared.models.workflow import WorkflowTrigger
from orchestrator.dsl.parser import DSLParser, DSLParseError
from orchestrator.dsl.validator import DSLValidator, ValidationResult

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


def _parse_key_value_inputs(raw: tuple[str, ...]) -> dict[str, Any]:
    """
    Parse ``key=value`` pairs from CLI ``--input`` options.

    Values that look like JSON (start with ``{``, ``[``, or are quoted) are
    decoded as JSON; everything else is treated as a plain string.
    """
    result: dict[str, Any] = {}
    for item in raw:
        if "=" not in item:
            raise click.BadParameter(
                f"Input {item!r} must be in key=value format.",
                param_hint="--input",
            )
        key, _, raw_value = item.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        # Try JSON decode for structured values
        if raw_value and raw_value[0] in ("{", "[", '"', "'"):
            try:
                result[key] = json.loads(raw_value)
                continue
            except json.JSONDecodeError:
                pass
        # Booleans
        if raw_value.lower() in ("true", "false"):
            result[key] = raw_value.lower() == "true"
        # Integers
        elif raw_value.isdigit():
            result[key] = int(raw_value)
        else:
            result[key] = raw_value
    return result


async def _connect_temporal():
    """
    Create and return a connected Temporal client.

    Returns None and logs a warning if the connection fails (so that
    commands can degrade gracefully when Temporal is not running).
    """
    try:
        from temporalio.client import Client
        client = await Client.connect(
            settings.temporal.address,
            namespace=settings.temporal.namespace,
        )
        return client
    except Exception as exc:
        logger.debug("Temporal connection failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ``flowos workflow`` group
# ---------------------------------------------------------------------------


@click.group("workflow")
@click.pass_context
def workflow_group(ctx: click.Context) -> None:
    """Manage FlowOS workflows (start, stop, pause, resume, inspect)."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# ``flowos workflow start``
# ---------------------------------------------------------------------------


@workflow_group.command("start")
@click.option(
    "--definition",
    "-d",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True),
    help="Path to the workflow YAML definition file.",
    metavar="PATH",
)
@click.option(
    "--input",
    "inputs",
    multiple=True,
    help="Runtime input parameter in key=value format (can be repeated).",
    metavar="KEY=VALUE",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Explicit workflow ID (auto-generated UUID if omitted).",
    metavar="UUID",
)
@click.option(
    "--owner",
    "owner_agent_id",
    default=None,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID that owns this workflow run.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--project",
    default=None,
    help="Project / namespace for this workflow run.",
    metavar="NAME",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Tag to attach to the workflow run (can be repeated).",
    metavar="TAG",
)
@click.option(
    "--task-queue",
    default=None,
    envvar="TEMPORAL_TASK_QUEUE",
    help="Temporal task queue to submit the workflow to.  Defaults to $TEMPORAL_TASK_QUEUE or 'flowos-main'.",
    metavar="QUEUE",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Parse and validate the definition without actually starting the workflow.",
)
@click.pass_context
def workflow_start(
    ctx: click.Context,
    definition: str,
    inputs: tuple[str, ...],
    workflow_id: str | None,
    owner_agent_id: str | None,
    project: str | None,
    tags: tuple[str, ...],
    task_queue: str | None,
    dry_run: bool,
) -> None:
    """
    Start a new workflow from a YAML definition file.

    Parses and validates the DSL, then submits the workflow to Temporal for
    durable execution.  The workflow ID is printed on success so it can be
    used with other commands.

    Examples::

        flowos workflow start --definition examples/feature_delivery_pipeline.yaml

        flowos workflow start \\
            --definition examples/feature_delivery_pipeline.yaml \\
            --input feature_branch=feat/my-feature \\
            --input ticket_id=JIRA-123 \\
            --owner $(uuidgen)

        # Validate only (no Temporal submission)
        flowos workflow start --definition my_workflow.yaml --dry-run
    """
    json_output = ctx.obj.get("json_output", False)

    # -- 1. Parse the DSL --------------------------------------------------
    definition_path = Path(definition)
    parser = DSLParser()
    try:
        wf_definition = parser.parse_file(definition_path)
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except DSLParseError as exc:
        _error(f"DSL parse error: {exc}")
        return

    # -- 2. Validate the DSL -----------------------------------------------
    validator = DSLValidator()
    validation_result: ValidationResult = validator.validate(wf_definition)
    if not validation_result.is_valid:
        for err in validation_result.errors:
            err_console.print(f"[bold red]ERROR[/bold red] {err}")
        _error(
            f"Workflow definition has {validation_result.error_count} validation error(s)."
        )
        return

    # -- 3. Parse runtime inputs -------------------------------------------
    try:
        runtime_inputs = _parse_key_value_inputs(inputs)
    except click.BadParameter as exc:
        _error(str(exc))
        return

    # Merge DSL-level defaults with runtime overrides
    merged_inputs = {**wf_definition.inputs, **runtime_inputs}

    # -- 4. Dry-run: just report success -----------------------------------
    if dry_run:
        if json_output:
            _output_json({
                "status": "valid",
                "name": wf_definition.name,
                "version": wf_definition.version,
                "steps": len(wf_definition.steps),
                "inputs": merged_inputs,
            })
            return

        console.print()
        console.print(
            Panel.fit(
                f"[bold green]Workflow definition is valid[/bold green]\n\n"
                f"[dim]Name:[/dim]        {wf_definition.name}\n"
                f"[dim]Version:[/dim]     {wf_definition.version}\n"
                f"[dim]Description:[/dim] {wf_definition.description or '(none)'}\n"
                f"[dim]Steps:[/dim]       {len(wf_definition.steps)}\n"
                f"[dim]Tags:[/dim]        {', '.join(wf_definition.tags) or '(none)'}",
                title="[bold]flowos workflow start --dry-run[/bold]",
                border_style="green",
            )
        )
        _print_steps_table(wf_definition.steps)
        return

    # -- 5. Build workflow run parameters ----------------------------------
    wf_id = workflow_id or str(uuid.uuid4())
    queue = task_queue or settings.temporal.task_queue

    # Map DSL workflow name to Temporal workflow type name
    temporal_workflow_type = _resolve_temporal_workflow_type(wf_definition.name)

    # -- 6. Submit to Temporal ---------------------------------------------
    result = asyncio.run(
        _start_workflow_async(
            workflow_type=temporal_workflow_type,
            workflow_id=wf_id,
            definition=wf_definition,
            inputs=merged_inputs,
            owner_agent_id=owner_agent_id,
            project=project,
            tags=list(tags),
            task_queue=queue,
        )
    )

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _warn(
            f"Workflow submitted but Temporal connection failed: {result['error']}\n"
            "  The workflow ID has been generated but execution may not have started.\n"
            "  Ensure the Temporal worker is running: make worker"
        )
    else:
        _success(f"Workflow started: {result['workflow_id']}")

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Workflow ID:[/dim]   {result['workflow_id']}\n"
            f"[dim]Name:[/dim]          {wf_definition.name}\n"
            f"[dim]Version:[/dim]       {wf_definition.version}\n"
            f"[dim]Task Queue:[/dim]    {queue}\n"
            f"[dim]Temporal ID:[/dim]   {result.get('temporal_workflow_id', wf_id)}\n"
            f"[dim]Run ID:[/dim]        {result.get('temporal_run_id', '(pending)')}\n"
            f"[dim]Status:[/dim]        {result.get('status', 'submitted')}\n"
            f"[dim]Steps:[/dim]         {len(wf_definition.steps)}\n"
            f"[dim]Inputs:[/dim]        {json.dumps(merged_inputs, default=str)[:80]}",
            title="[bold]Workflow Started[/bold]",
            border_style="green" if not result.get("error") else "yellow",
        )
    )

    if not result.get("error"):
        console.print()
        console.print(
            "[dim]View in Temporal UI:[/dim] "
            f"http://localhost:8080/namespaces/{settings.temporal.namespace}"
            f"/workflows/{result.get('temporal_workflow_id', wf_id)}"
        )


async def _start_workflow_async(
    workflow_type: str,
    workflow_id: str,
    definition: Any,
    inputs: dict[str, Any],
    owner_agent_id: str | None,
    project: str | None,
    tags: list[str],
    task_queue: str,
) -> dict[str, Any]:
    """
    Submit a workflow to Temporal asynchronously.

    Returns a dict with workflow_id, temporal_workflow_id, temporal_run_id,
    and status.  On connection failure or missing dependencies, returns a
    dict with an 'error' key so the CLI can degrade gracefully.
    """
    temporal_wf_id = f"flowos-{workflow_id[:8]}"

    # Check Temporal connectivity first
    client = await _connect_temporal()
    if client is None:
        return {
            "workflow_id": workflow_id,
            "temporal_workflow_id": temporal_wf_id,
            "temporal_run_id": None,
            "status": "submitted_offline",
            "error": (
                f"Cannot connect to Temporal at {settings.temporal.address}. "
                "Ensure the Temporal server is running."
            ),
        }

    # Import workflow classes (may fail if temporalio not installed)
    try:
        from orchestrator.workflows.feature_delivery import (
            FeatureDeliveryWorkflow,
            FeatureDeliveryInput,
        )
        from orchestrator.workflows.base_workflow import WorkflowInput
    except ImportError as exc:
        return {
            "workflow_id": workflow_id,
            "temporal_workflow_id": temporal_wf_id,
            "temporal_run_id": None,
            "status": "error",
            "error": (
                f"Cannot import workflow classes: {exc}. "
                "Ensure temporalio is installed: pip install temporalio"
            ),
        }

    try:
        if workflow_type == "feature-delivery":
            wf_input = FeatureDeliveryInput(
                workflow_id=workflow_id,
                name=definition.name,
                trigger=WorkflowTrigger.MANUAL,
                owner_agent_id=owner_agent_id,
                project=project,
                tags=tags,
                inputs=inputs,
                feature_branch=inputs.get("feature_branch", "main"),
                ticket_id=inputs.get("ticket_id"),
                ai_agent_id=inputs.get("ai_agent_id"),
                human_agent_id=inputs.get("human_agent_id"),
                build_agent_id=inputs.get("build_agent_id"),
                require_approval=bool(inputs.get("require_approval", False)),
                auto_assign=bool(inputs.get("auto_assign", True)),
            )
            handle = await client.start_workflow(
                FeatureDeliveryWorkflow.run,
                wf_input,
                id=temporal_wf_id,
                task_queue=task_queue,
            )
        else:
            # Generic workflow: use base WorkflowInput
            wf_input = WorkflowInput(
                workflow_id=workflow_id,
                name=definition.name,
                trigger=WorkflowTrigger.MANUAL,
                owner_agent_id=owner_agent_id,
                project=project,
                tags=tags,
                inputs=inputs,
            )
            # Try to start as feature-delivery (most common)
            handle = await client.start_workflow(
                FeatureDeliveryWorkflow.run,
                wf_input,
                id=temporal_wf_id,
                task_queue=task_queue,
            )

        return {
            "workflow_id": workflow_id,
            "temporal_workflow_id": handle.id,
            "temporal_run_id": handle.result_run_id,
            "status": "running",
            "task_queue": task_queue,
        }

    except Exception as exc:
        return {
            "workflow_id": workflow_id,
            "temporal_workflow_id": temporal_wf_id,
            "temporal_run_id": None,
            "status": "error",
            "error": str(exc),
        }


def _resolve_temporal_workflow_type(dsl_name: str) -> str:
    """
    Map a DSL workflow name to its registered Temporal workflow type name.

    The Temporal workflow type is the ``name`` argument passed to
    ``@workflow.defn(name=...)``.
    """
    name_lower = dsl_name.lower().replace(" ", "-").replace("_", "-")
    known_types = {
        "feature-delivery": "feature-delivery",
        "feature-delivery-pipeline": "feature-delivery",
        "build-and-review": "build-and-review",
        "build-review": "build-and-review",
    }
    return known_types.get(name_lower, "feature-delivery")


def _print_steps_table(steps: list[Any]) -> None:
    """Print a Rich table of workflow steps."""
    if not steps:
        return
    table = Table(title="Workflow Steps", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Step ID", style="cyan", width=20)
    table.add_column("Name", width=30)
    table.add_column("Agent Type", width=12)
    table.add_column("Depends On", width=20)
    table.add_column("Timeout", width=10)

    for i, step in enumerate(steps, 1):
        timeout = f"{step.timeout_secs}s" if step.timeout_secs else "inf"
        deps = ", ".join(step.depends_on) if step.depends_on else "-"
        table.add_row(
            str(i),
            step.step_id[:18],
            step.name[:28],
            step.agent_type,
            deps[:18],
            timeout,
        )
    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# ``flowos workflow stop``
# ---------------------------------------------------------------------------


@workflow_group.command("stop")
@click.argument("workflow_id")
@click.option(
    "--reason",
    default=None,
    help="Human-readable reason for stopping the workflow.",
    metavar="TEXT",
)
@click.pass_context
def workflow_stop(
    ctx: click.Context,
    workflow_id: str,
    reason: str | None,
) -> None:
    """
    Cancel a running workflow.

    WORKFLOW_ID is the FlowOS workflow ID (UUID) or the Temporal workflow ID.

    Example::

        flowos workflow stop abc12345-...
        flowos workflow stop abc12345-... --reason "Requirements changed"
    """
    json_output = ctx.obj.get("json_output", False)

    result = asyncio.run(_cancel_workflow_async(workflow_id, reason))

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _error(f"Failed to stop workflow: {result['error']}")
        return

    _success(f"Workflow {workflow_id[:8]}... cancelled.")
    if reason:
        console.print(f"  [dim]Reason:[/dim] {reason}")


async def _cancel_workflow_async(workflow_id: str, reason: str | None) -> dict[str, Any]:
    """Cancel a Temporal workflow by ID."""
    client = await _connect_temporal()
    if client is None:
        return {
            "workflow_id": workflow_id,
            "error": f"Cannot connect to Temporal at {settings.temporal.address}.",
        }

    try:
        # Try both the raw ID and the flowos-prefixed ID
        for wf_id in [workflow_id, f"flowos-{workflow_id[:8]}"]:
            try:
                handle = client.get_workflow_handle(wf_id)
                await handle.cancel()
                return {
                    "workflow_id": workflow_id,
                    "temporal_workflow_id": wf_id,
                    "status": "cancelled",
                }
            except Exception:
                continue
        return {"workflow_id": workflow_id, "error": "Workflow not found in Temporal."}
    except Exception as exc:
        return {"workflow_id": workflow_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# ``flowos workflow pause``
# ---------------------------------------------------------------------------


@workflow_group.command("pause")
@click.argument("workflow_id")
@click.option(
    "--reason",
    default=None,
    help="Human-readable reason for pausing.",
    metavar="TEXT",
)
@click.pass_context
def workflow_pause(
    ctx: click.Context,
    workflow_id: str,
    reason: str | None,
) -> None:
    """
    Pause a running workflow.

    Sends a ``pause`` signal to the Temporal workflow.  The workflow will
    complete its current activity and then wait until resumed.

    Example::

        flowos workflow pause abc12345-...
    """
    json_output = ctx.obj.get("json_output", False)

    result = asyncio.run(_signal_workflow_async(workflow_id, "pause", {"reason": reason}))

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _error(f"Failed to pause workflow: {result['error']}")
        return

    _success(f"Pause signal sent to workflow {workflow_id[:8]}...")


# ---------------------------------------------------------------------------
# ``flowos workflow resume``
# ---------------------------------------------------------------------------


@workflow_group.command("resume")
@click.argument("workflow_id")
@click.pass_context
def workflow_resume(
    ctx: click.Context,
    workflow_id: str,
) -> None:
    """
    Resume a paused workflow.

    Sends a ``resume`` signal to the Temporal workflow.

    Example::

        flowos workflow resume abc12345-...
    """
    json_output = ctx.obj.get("json_output", False)

    result = asyncio.run(_signal_workflow_async(workflow_id, "resume", {}))

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _error(f"Failed to resume workflow: {result['error']}")
        return

    _success(f"Resume signal sent to workflow {workflow_id[:8]}...")


async def _signal_workflow_async(
    workflow_id: str,
    signal_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a signal to a Temporal workflow."""
    client = await _connect_temporal()
    if client is None:
        return {
            "workflow_id": workflow_id,
            "error": f"Cannot connect to Temporal at {settings.temporal.address}.",
        }

    try:
        for wf_id in [workflow_id, f"flowos-{workflow_id[:8]}"]:
            try:
                handle = client.get_workflow_handle(wf_id)
                await handle.signal(signal_name, payload)
                return {
                    "workflow_id": workflow_id,
                    "temporal_workflow_id": wf_id,
                    "signal": signal_name,
                    "status": "signal_sent",
                }
            except Exception:
                continue
        return {"workflow_id": workflow_id, "error": "Workflow not found in Temporal."}
    except Exception as exc:
        return {"workflow_id": workflow_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# ``flowos workflow status``
# ---------------------------------------------------------------------------


@workflow_group.command("status")
@click.argument("workflow_id")
@click.pass_context
def workflow_status(
    ctx: click.Context,
    workflow_id: str,
) -> None:
    """
    Show the current status of a workflow.

    WORKFLOW_ID is the FlowOS workflow ID (UUID) or the Temporal workflow ID.

    Example::

        flowos workflow status abc12345-...
    """
    json_output = ctx.obj.get("json_output", False)

    result = asyncio.run(_describe_workflow_async(workflow_id))

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _error(f"Failed to get workflow status: {result['error']}")
        return

    status = result.get("status", "unknown")
    status_color = {
        "Running": "green",
        "Completed": "bright_green",
        "Failed": "red",
        "Cancelled": "yellow",
        "TimedOut": "red",
        "ContinuedAsNew": "cyan",
        "Terminated": "red",
    }.get(status, "white")

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Workflow ID:[/dim]   {workflow_id}\n"
            f"[dim]Temporal ID:[/dim]   {result.get('temporal_workflow_id', '?')}\n"
            f"[dim]Run ID:[/dim]        {result.get('run_id', '?')}\n"
            f"[dim]Status:[/dim]        [{status_color}]{status}[/{status_color}]\n"
            f"[dim]Type:[/dim]          {result.get('workflow_type', '?')}\n"
            f"[dim]Task Queue:[/dim]    {result.get('task_queue', '?')}\n"
            f"[dim]Started:[/dim]       {result.get('start_time', '?')}\n"
            f"[dim]Closed:[/dim]        {result.get('close_time', '(running)')}",
            title="[bold]Workflow Status[/bold]",
            border_style=status_color,
        )
    )


async def _describe_workflow_async(workflow_id: str) -> dict[str, Any]:
    """Describe a Temporal workflow execution."""
    client = await _connect_temporal()
    if client is None:
        return {
            "workflow_id": workflow_id,
            "error": f"Cannot connect to Temporal at {settings.temporal.address}.",
        }

    try:
        for wf_id in [workflow_id, f"flowos-{workflow_id[:8]}"]:
            try:
                handle = client.get_workflow_handle(wf_id)
                desc = await handle.describe()
                return {
                    "workflow_id": workflow_id,
                    "temporal_workflow_id": desc.id,
                    "run_id": desc.run_id,
                    "status": str(desc.status.name) if desc.status else "unknown",
                    "workflow_type": desc.workflow_type,
                    "task_queue": desc.task_queue,
                    "start_time": str(desc.start_time) if desc.start_time else None,
                    "close_time": str(desc.close_time) if desc.close_time else None,
                }
            except Exception:
                continue
        return {"workflow_id": workflow_id, "error": "Workflow not found in Temporal."}
    except Exception as exc:
        return {"workflow_id": workflow_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# ``flowos workflow list``
# ---------------------------------------------------------------------------


@workflow_group.command("list")
@click.option(
    "--status",
    "filter_status",
    default=None,
    type=click.Choice(
        ["running", "completed", "failed", "cancelled", "all"],
        case_sensitive=False,
    ),
    help="Filter by workflow status.",
)
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of workflows to show.",
    metavar="N",
)
@click.pass_context
def workflow_list(
    ctx: click.Context,
    filter_status: str | None,
    limit: int,
) -> None:
    """
    List recent workflow executions.

    Queries Temporal for recent workflow executions and displays them in a
    table.

    Examples::

        flowos workflow list
        flowos workflow list --status running
        flowos workflow list --limit 50
    """
    json_output = ctx.obj.get("json_output", False)

    result = asyncio.run(_list_workflows_async(filter_status, limit))

    if json_output:
        _output_json(result)
        return

    if result.get("error"):
        _error(f"Failed to list workflows: {result['error']}")
        return

    workflows = result.get("workflows", [])
    if not workflows:
        console.print("[dim]No workflows found.[/dim]")
        return

    table = Table(
        title=f"Workflows ({len(workflows)} shown)",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Temporal ID", style="cyan", width=30)
    table.add_column("Run ID", style="dim", width=14)
    table.add_column("Type", width=20)
    table.add_column("Status", width=12)
    table.add_column("Task Queue", width=16)
    table.add_column("Started", width=20)

    for wf in workflows:
        status = wf.get("status", "?")
        status_color = {
            "Running": "green",
            "Completed": "bright_green",
            "Failed": "red",
            "Cancelled": "yellow",
        }.get(status, "white")
        table.add_row(
            (wf.get("id") or "")[:28],
            (wf.get("run_id") or "")[:12] + "...",
            (wf.get("workflow_type") or "")[:18],
            f"[{status_color}]{status}[/{status_color}]",
            (wf.get("task_queue") or "")[:14],
            str(wf.get("start_time") or "?")[:18],
        )

    console.print()
    console.print(table)


async def _list_workflows_async(
    filter_status: str | None,
    limit: int,
) -> dict[str, Any]:
    """List Temporal workflow executions."""
    client = await _connect_temporal()
    if client is None:
        return {
            "error": f"Cannot connect to Temporal at {settings.temporal.address}.",
            "workflows": [],
        }

    try:
        # Build query
        query_parts: list[str] = []
        if filter_status and filter_status != "all":
            status_map = {
                "running": "Running",
                "completed": "Completed",
                "failed": "Failed",
                "cancelled": "Cancelled",
            }
            temporal_status = status_map.get(filter_status.lower())
            if temporal_status:
                query_parts.append(f"ExecutionStatus = '{temporal_status}'")

        query = " AND ".join(query_parts) if query_parts else ""

        workflows = []
        async for wf in client.list_workflows(query=query):
            workflows.append({
                "id": wf.id,
                "run_id": wf.run_id,
                "workflow_type": wf.workflow_type,
                "status": str(wf.status.name) if wf.status else "unknown",
                "task_queue": wf.task_queue,
                "start_time": str(wf.start_time) if wf.start_time else None,
                "close_time": str(wf.close_time) if wf.close_time else None,
            })
            if len(workflows) >= limit:
                break

        return {"workflows": workflows}
    except Exception as exc:
        return {"error": str(exc), "workflows": []}


# ---------------------------------------------------------------------------
# ``flowos workflow validate``
# ---------------------------------------------------------------------------


@workflow_group.command("validate")
@click.option(
    "--definition",
    "-d",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True),
    help="Path to the workflow YAML definition file.",
    metavar="PATH",
)
@click.pass_context
def workflow_validate(
    ctx: click.Context,
    definition: str,
) -> None:
    """
    Validate a workflow YAML definition without starting it.

    Performs full DSL parsing and semantic validation, then prints a summary
    of the workflow structure.

    Example::

        flowos workflow validate --definition examples/feature_delivery_pipeline.yaml
    """
    json_output = ctx.obj.get("json_output", False)

    definition_path = Path(definition)
    parser = DSLParser()
    validator = DSLValidator()

    try:
        wf_definition = parser.parse_file(definition_path)
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except DSLParseError as exc:
        if json_output:
            _output_json({"valid": False, "errors": [str(exc)], "warnings": []})
            return
        _error(f"DSL parse error: {exc}")
        return

    validation_result: ValidationResult = validator.validate(wf_definition)
    is_valid = validation_result.is_valid

    if json_output:
        _output_json({
            "valid": is_valid,
            "name": wf_definition.name,
            "version": wf_definition.version,
            "steps": len(wf_definition.steps),
            "errors": [str(e) for e in validation_result.errors],
            "warnings": [str(w) for w in validation_result.warnings],
            "execution_order": validation_result.execution_order,
        })
        return

    if is_valid:
        _success(f"Workflow definition is valid: {wf_definition.name} v{wf_definition.version}")
        for w in validation_result.warnings:
            _warn(str(w))
    else:
        for err in validation_result.errors:
            err_console.print(f"[bold red]ERROR[/bold red] {err}")
        sys.exit(1)

    _print_steps_table(wf_definition.steps)
