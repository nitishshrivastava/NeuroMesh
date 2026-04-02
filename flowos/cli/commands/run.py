"""
cli/commands/run.py -- FlowOS Machine-Oriented Run Commands

Provides the ``flowos run`` command group for machine agents (build runners,
deployment agents, CI/CD systems) to interact with FlowOS.

Commands::

    flowos run build    --task-id <id> --agent-id <id> [--command CMD] [--artifact-dir DIR]
    flowos run test     --task-id <id> --agent-id <id> [--command CMD]
    flowos run deploy   --task-id <id> --agent-id <id> --environment <env>
    flowos run report   --task-id <id> --agent-id <id> --status <status> [--artifact PATH]
    flowos run register --agent-id <id> --agent-type <type> [--capability CAP ...]

Machine agents use these commands to:
- Report build/test/deploy results back to the orchestrator via Kafka events
- Register themselves as available agents with declared capabilities
- Upload artifacts to S3-compatible storage
- Signal task completion or failure

All commands emit Kafka events to the appropriate topic so the orchestrator
and observability layer can track execution.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shared.models.event import EventSource, EventType

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


def _emit_event(
    event_type: EventType,
    task_id: str,
    agent_id: str,
    workflow_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Emit an event to Kafka.

    Returns True on success, False if Kafka is unavailable (non-fatal).
    """
    try:
        from shared.kafka.producer import FlowOSProducer
        from shared.models.event import EventRecord, EventTopic

        payload_data: dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "workflow_id": workflow_id or "",
        }
        if extra:
            payload_data.update(extra)

        # Determine topic from event type
        topic = EventTopic.TASK_EVENTS
        if event_type.value.startswith("BUILD_") or event_type.value.startswith("TEST_"):
            topic = EventTopic.BUILD_EVENTS
        elif event_type.value.startswith("AGENT_"):
            topic = EventTopic.AGENT_EVENTS

        event = EventRecord(
            event_type=event_type,
            topic=topic,
            source=EventSource.CLI,
            payload=payload_data,
            metadata={"agent_id": agent_id, "task_id": task_id},
        )

        producer = FlowOSProducer()
        producer.produce(event)
        producer.flush(timeout=5.0)
        return True
    except Exception as exc:
        logger.debug("Failed to emit Kafka event %s: %s", event_type, exc)
        return False


def _run_command(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
) -> tuple[int, str, str]:
    """
    Execute a shell command and return (returncode, stdout, stderr).

    Args:
        command:  Shell command string to execute.
        cwd:      Working directory for the command.
        env:      Additional environment variables.
        timeout:  Maximum execution time in seconds.

    Returns:
        Tuple of (return_code, stdout, stderr).
    """
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=merged_env,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# ``flowos run`` group
# ---------------------------------------------------------------------------


@click.group("run")
@click.pass_context
def run_group(ctx: click.Context) -> None:
    """Machine-oriented execution commands (build, test, deploy, report)."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# ``flowos run build``
# ---------------------------------------------------------------------------


@run_group.command("build")
@click.option(
    "--task-id",
    required=True,
    help="Task ID for this build execution.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Build agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this build belongs to.",
    metavar="UUID",
)
@click.option(
    "--command",
    default=None,
    help="Build command to execute (e.g. 'make build', 'docker build .').",
    metavar="CMD",
)
@click.option(
    "--artifact-dir",
    default=None,
    help="Directory containing build artifacts to report.",
    metavar="DIR",
)
@click.option(
    "--workspace-dir",
    default=None,
    help="Working directory for the build command.  Defaults to CWD.",
    metavar="DIR",
)
@click.option(
    "--timeout",
    default=3600,
    show_default=True,
    type=int,
    help="Build timeout in seconds.",
    metavar="SECS",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit BUILD_* Kafka events.",
)
@click.pass_context
def run_build(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    command: str | None,
    artifact_dir: str | None,
    workspace_dir: str | None,
    timeout: int,
    no_event: bool,
) -> None:
    """
    Execute a build command and report results to the orchestrator.

    Runs the specified build command, captures output, and emits
    BUILD_STARTED / BUILD_SUCCEEDED / BUILD_FAILED events to Kafka.

    Examples::

        flowos run build \\
            --task-id abc123 \\
            --agent-id build-agent-01 \\
            --command "make build" \\
            --artifact-dir ./dist

        flowos run build \\
            --task-id abc123 \\
            --agent-id build-agent-01 \\
            --command "docker build -t myapp:latest ." \\
            --timeout 600
    """
    json_output = ctx.obj.get("json_output", False)

    started_at = datetime.now(timezone.utc)

    # Emit BUILD_TRIGGERED
    if not no_event:
        _emit_event(
            event_type=EventType.BUILD_TRIGGERED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={"command": command, "started_at": started_at.isoformat()},
        )

    if not json_output:
        console.print(f"[dim]Build started at {started_at.strftime('%H:%M:%S')}[/dim]")
        if command:
            console.print(f"[dim]Command: {command}[/dim]")

    # Emit BUILD_STARTED
    if not no_event:
        _emit_event(
            event_type=EventType.BUILD_STARTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={"started_at": started_at.isoformat()},
        )

    # Execute the build command if provided
    returncode = 0
    stdout = ""
    stderr = ""
    duration_seconds = 0.0

    if command:
        t0 = time.monotonic()
        returncode, stdout, stderr = _run_command(
            command=command,
            cwd=workspace_dir,
            timeout=timeout,
        )
        duration_seconds = time.monotonic() - t0

        if not json_output:
            if stdout:
                console.print("[dim]--- stdout ---[/dim]")
                click.echo(stdout)
            if stderr:
                console.print("[dim]--- stderr ---[/dim]")
                click.echo(stderr, err=True)

    # Collect artifacts
    artifacts: list[dict[str, Any]] = []
    if artifact_dir:
        artifact_path = Path(artifact_dir)
        if artifact_path.exists():
            for f in artifact_path.rglob("*"):
                if f.is_file():
                    artifacts.append({
                        "name": f.name,
                        "path": str(f),
                        "size_bytes": f.stat().st_size,
                    })

    completed_at = datetime.now(timezone.utc)
    success = returncode == 0

    # Emit BUILD_SUCCEEDED or BUILD_FAILED
    if not no_event:
        event_type = EventType.BUILD_SUCCEEDED if success else EventType.BUILD_FAILED
        _emit_event(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "returncode": returncode,
                "duration_seconds": duration_seconds,
                "artifact_count": len(artifacts),
                "completed_at": completed_at.isoformat(),
                "error_message": stderr[:500] if not success else None,
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "succeeded" if success else "failed",
        "returncode": returncode,
        "command": command,
        "duration_seconds": round(duration_seconds, 2),
        "artifact_count": len(artifacts),
        "artifacts": artifacts[:10],  # Limit to first 10 in response
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
    }

    if json_output:
        _output_json(result)
        return

    if success:
        _success(
            f"Build succeeded in {duration_seconds:.1f}s "
            f"({len(artifacts)} artifact(s))"
        )
    else:
        err_console.print(
            f"[bold red]FAIL[/bold red] Build failed (exit code {returncode}) "
            f"after {duration_seconds:.1f}s"
        )
        sys.exit(returncode)


# ---------------------------------------------------------------------------
# ``flowos run test``
# ---------------------------------------------------------------------------


@run_group.command("test")
@click.option(
    "--task-id",
    required=True,
    help="Task ID for this test execution.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Test runner agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this test run belongs to.",
    metavar="UUID",
)
@click.option(
    "--command",
    default=None,
    help="Test command to execute (e.g. 'pytest', 'npm test').",
    metavar="CMD",
)
@click.option(
    "--workspace-dir",
    default=None,
    help="Working directory for the test command.  Defaults to CWD.",
    metavar="DIR",
)
@click.option(
    "--timeout",
    default=1800,
    show_default=True,
    type=int,
    help="Test timeout in seconds.",
    metavar="SECS",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit TEST_* Kafka events.",
)
@click.pass_context
def run_test(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    command: str | None,
    workspace_dir: str | None,
    timeout: int,
    no_event: bool,
) -> None:
    """
    Execute a test suite and report results to the orchestrator.

    Runs the specified test command, captures output, and emits
    TEST_STARTED / TEST_PASSED / TEST_FAILED events to Kafka.

    Examples::

        flowos run test \\
            --task-id abc123 \\
            --agent-id test-runner-01 \\
            --command "pytest tests/ -v --tb=short"

        flowos run test \\
            --task-id abc123 \\
            --agent-id test-runner-01 \\
            --command "npm test -- --ci"
    """
    json_output = ctx.obj.get("json_output", False)

    started_at = datetime.now(timezone.utc)

    if not no_event:
        _emit_event(
            event_type=EventType.TEST_STARTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={"command": command, "started_at": started_at.isoformat()},
        )

    if not json_output:
        console.print(f"[dim]Test run started at {started_at.strftime('%H:%M:%S')}[/dim]")
        if command:
            console.print(f"[dim]Command: {command}[/dim]")

    returncode = 0
    stdout = ""
    stderr = ""
    duration_seconds = 0.0

    if command:
        t0 = time.monotonic()
        returncode, stdout, stderr = _run_command(
            command=command,
            cwd=workspace_dir,
            timeout=timeout,
        )
        duration_seconds = time.monotonic() - t0

        if not json_output:
            if stdout:
                click.echo(stdout)
            if stderr:
                click.echo(stderr, err=True)

    completed_at = datetime.now(timezone.utc)
    success = returncode == 0

    if not no_event:
        event_type = EventType.TEST_PASSED if success else EventType.TEST_FAILED
        _emit_event(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "returncode": returncode,
                "duration_seconds": duration_seconds,
                "completed_at": completed_at.isoformat(),
                "error_message": stderr[:500] if not success else None,
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": "passed" if success else "failed",
        "returncode": returncode,
        "command": command,
        "duration_seconds": round(duration_seconds, 2),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
    }

    if json_output:
        _output_json(result)
        return

    if success:
        _success(f"Tests passed in {duration_seconds:.1f}s")
    else:
        err_console.print(
            f"[bold red]FAIL[/bold red] Tests failed (exit code {returncode}) "
            f"after {duration_seconds:.1f}s"
        )
        sys.exit(returncode)


# ---------------------------------------------------------------------------
# ``flowos run deploy``
# ---------------------------------------------------------------------------


@run_group.command("deploy")
@click.option(
    "--task-id",
    required=True,
    help="Task ID for this deployment.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Deploy agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this deployment belongs to.",
    metavar="UUID",
)
@click.option(
    "--environment",
    required=True,
    type=click.Choice(["development", "staging", "production", "preview"], case_sensitive=False),
    help="Target deployment environment.",
)
@click.option(
    "--command",
    default=None,
    help="Deploy command to execute (e.g. 'kubectl apply -f k8s/').",
    metavar="CMD",
)
@click.option(
    "--workspace-dir",
    default=None,
    help="Working directory for the deploy command.  Defaults to CWD.",
    metavar="DIR",
)
@click.option(
    "--timeout",
    default=1800,
    show_default=True,
    type=int,
    help="Deploy timeout in seconds.",
    metavar="SECS",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit TASK_* Kafka events.",
)
@click.pass_context
def run_deploy(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    environment: str,
    command: str | None,
    workspace_dir: str | None,
    timeout: int,
    no_event: bool,
) -> None:
    """
    Execute a deployment and report results to the orchestrator.

    Runs the specified deploy command and emits TASK_STARTED / TASK_COMPLETED
    / TASK_FAILED events to Kafka.

    Examples::

        flowos run deploy \\
            --task-id abc123 \\
            --agent-id deploy-agent-01 \\
            --environment staging \\
            --command "kubectl apply -f k8s/staging/"

        flowos run deploy \\
            --task-id abc123 \\
            --agent-id deploy-agent-01 \\
            --environment production \\
            --command "helm upgrade myapp ./chart --values prod.yaml"
    """
    json_output = ctx.obj.get("json_output", False)

    started_at = datetime.now(timezone.utc)

    if not no_event:
        _emit_event(
            event_type=EventType.TASK_STARTED,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "environment": environment,
                "command": command,
                "started_at": started_at.isoformat(),
            },
        )

    if not json_output:
        console.print(
            f"[dim]Deploying to {environment} at {started_at.strftime('%H:%M:%S')}[/dim]"
        )
        if command:
            console.print(f"[dim]Command: {command}[/dim]")

    returncode = 0
    stdout = ""
    stderr = ""
    duration_seconds = 0.0

    if command:
        t0 = time.monotonic()
        returncode, stdout, stderr = _run_command(
            command=command,
            cwd=workspace_dir,
            timeout=timeout,
        )
        duration_seconds = time.monotonic() - t0

        if not json_output:
            if stdout:
                click.echo(stdout)
            if stderr:
                click.echo(stderr, err=True)

    completed_at = datetime.now(timezone.utc)
    success = returncode == 0

    if not no_event:
        event_type = EventType.TASK_COMPLETED if success else EventType.TASK_FAILED
        _emit_event(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "environment": environment,
                "returncode": returncode,
                "duration_seconds": duration_seconds,
                "completed_at": completed_at.isoformat(),
                "error_message": stderr[:500] if not success else None,
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "environment": environment,
        "status": "deployed" if success else "failed",
        "returncode": returncode,
        "command": command,
        "duration_seconds": round(duration_seconds, 2),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
    }

    if json_output:
        _output_json(result)
        return

    if success:
        _success(f"Deployed to {environment} in {duration_seconds:.1f}s")
    else:
        err_console.print(
            f"[bold red]FAIL[/bold red] Deployment to {environment} failed "
            f"(exit code {returncode}) after {duration_seconds:.1f}s"
        )
        sys.exit(returncode)


# ---------------------------------------------------------------------------
# ``flowos run report``
# ---------------------------------------------------------------------------


@run_group.command("report")
@click.option(
    "--task-id",
    required=True,
    help="Task ID to report on.",
    metavar="UUID",
)
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Reporting agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this task belongs to.",
    metavar="UUID",
)
@click.option(
    "--status",
    required=True,
    type=click.Choice(
        ["succeeded", "failed", "skipped", "cancelled"],
        case_sensitive=False,
    ),
    help="Execution result status.",
)
@click.option(
    "--message",
    default=None,
    help="Human-readable result message.",
    metavar="TEXT",
)
@click.option(
    "--artifact",
    "artifacts",
    multiple=True,
    help="Artifact path to report (can be repeated).",
    metavar="PATH",
)
@click.option(
    "--duration",
    "duration_seconds",
    default=None,
    type=float,
    help="Execution duration in seconds.",
    metavar="SECS",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a Kafka event.",
)
@click.pass_context
def run_report(
    ctx: click.Context,
    task_id: str,
    agent_id: str,
    workflow_id: str | None,
    status: str,
    message: str | None,
    artifacts: tuple[str, ...],
    duration_seconds: float | None,
    no_event: bool,
) -> None:
    """
    Report a task execution result to the orchestrator.

    Used by machine agents to report the outcome of an execution without
    running a command directly (e.g. when the execution happened externally).

    Examples::

        flowos run report \\
            --task-id abc123 \\
            --agent-id build-agent-01 \\
            --status succeeded \\
            --message "Build completed successfully" \\
            --artifact ./dist/app.tar.gz \\
            --duration 45.3

        flowos run report \\
            --task-id abc123 \\
            --agent-id build-agent-01 \\
            --status failed \\
            --message "Compilation error in src/main.py"
    """
    json_output = ctx.obj.get("json_output", False)

    reported_at = datetime.now(timezone.utc)
    event_emitted = False

    # Map status to event type
    event_type_map = {
        "succeeded": EventType.TASK_COMPLETED,
        "failed": EventType.TASK_FAILED,
        "skipped": EventType.TASK_COMPLETED,
        "cancelled": EventType.TASK_FAILED,
    }
    event_type = event_type_map.get(status.lower(), EventType.TASK_UPDATED)

    # Collect artifact info
    artifact_list = []
    for artifact_path in artifacts:
        p = Path(artifact_path)
        artifact_list.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": p.stat().st_size if p.exists() else None,
        })

    if not no_event:
        event_emitted = _emit_event(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            workflow_id=workflow_id,
            extra={
                "status": status,
                "message": message,
                "artifacts": artifact_list,
                "duration_seconds": duration_seconds,
                "reported_at": reported_at.isoformat(),
            },
        )

    result = {
        "task_id": task_id,
        "agent_id": agent_id,
        "workflow_id": workflow_id,
        "status": status,
        "message": message,
        "artifacts": artifact_list,
        "duration_seconds": duration_seconds,
        "reported_at": reported_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    status_color = {
        "succeeded": "green",
        "failed": "red",
        "skipped": "yellow",
        "cancelled": "yellow",
    }.get(status.lower(), "white")

    console.print(
        f"[{status_color}]{status.upper()}[/{status_color}] "
        f"Task {task_id[:8]}... reported as {status}."
    )
    if message:
        console.print(f"  [dim]Message:[/dim]   {message}")
    if artifact_list:
        console.print(f"  [dim]Artifacts:[/dim]  {len(artifact_list)} file(s)")
    if duration_seconds is not None:
        console.print(f"  [dim]Duration:[/dim]   {duration_seconds:.1f}s")
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")


# ---------------------------------------------------------------------------
# ``flowos run register``
# ---------------------------------------------------------------------------


@run_group.command("register")
@click.option(
    "--agent-id",
    required=True,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID to register.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--agent-type",
    required=True,
    type=click.Choice(["human", "ai", "build", "deploy", "system"], case_sensitive=False),
    help="Type of agent.",
)
@click.option(
    "--name",
    default=None,
    help="Human-readable agent name.",
    metavar="NAME",
)
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    help="Capability to declare (can be repeated, e.g. 'python_build', 'docker_build').",
    metavar="CAP",
)
@click.option(
    "--hostname",
    default=None,
    help="Hostname of the machine running this agent.",
    metavar="HOST",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit an AGENT_REGISTERED Kafka event.",
)
@click.pass_context
def run_register(
    ctx: click.Context,
    agent_id: str,
    agent_type: str,
    name: str | None,
    capabilities: tuple[str, ...],
    hostname: str | None,
    no_event: bool,
) -> None:
    """
    Register a machine agent with the FlowOS platform.

    Emits an AGENT_REGISTERED event to Kafka so the orchestrator knows this
    agent is available for task assignment.

    Examples::

        flowos run register \\
            --agent-id build-agent-01 \\
            --agent-type build \\
            --name "Build Runner 01" \\
            --capability python_build \\
            --capability docker_build \\
            --hostname build-server-01

        FLOWOS_AGENT_ID=deploy-01 flowos run register \\
            --agent-type deploy \\
            --capability kubernetes \\
            --capability helm
    """
    json_output = ctx.obj.get("json_output", False)

    import socket
    resolved_hostname = hostname or socket.gethostname()
    registered_at = datetime.now(timezone.utc)
    event_emitted = False

    capability_list = [
        {"name": cap, "enabled": True}
        for cap in capabilities
    ]

    if not no_event:
        try:
            from shared.kafka.producer import FlowOSProducer
            from shared.models.event import EventRecord, EventTopic

            event = EventRecord(
                event_type=EventType.AGENT_REGISTERED,
                topic=EventTopic.AGENT_EVENTS,
                source=EventSource.CLI,
                payload={
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "name": name or agent_id,
                    "capabilities": capability_list,
                    "hostname": resolved_hostname,
                    "registered_at": registered_at.isoformat(),
                },
                metadata={"agent_id": agent_id},
            )
            producer = FlowOSProducer()
            producer.produce(event)
            producer.flush(timeout=5.0)
            event_emitted = True
        except Exception as exc:
            logger.debug("Failed to emit AGENT_REGISTERED event: %s", exc)

    result = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "name": name or agent_id,
        "capabilities": capability_list,
        "hostname": resolved_hostname,
        "registered_at": registered_at.isoformat(),
        "event_emitted": event_emitted,
    }

    if json_output:
        _output_json(result)
        return

    _success(f"Agent registered: {agent_id} ({agent_type})")
    console.print(f"  [dim]Name:[/dim]         {name or agent_id}")
    console.print(f"  [dim]Hostname:[/dim]     {resolved_hostname}")
    if capability_list:
        caps = ", ".join(c["name"] for c in capability_list)
        console.print(f"  [dim]Capabilities:[/dim] {caps}")
    console.print(
        f"  [dim]Registered at:[/dim] {registered_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    if not event_emitted and not no_event:
        _warn("Kafka event could not be emitted (Kafka may be unavailable).")
