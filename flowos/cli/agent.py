"""
cli/agent.py -- FlowOS CLI Agent Loop

Provides the ``FlowOSAgent`` class, which implements the event-driven agent
loop for FlowOS participants.  The agent loop:

1. Registers the agent with the platform (emits AGENT_ONLINE event)
2. Subscribes to the Kafka task events topic for its agent ID
3. Dispatches incoming TASK_ASSIGNED events to registered handlers
4. Emits heartbeat AGENT_IDLE / AGENT_BUSY events
5. Handles graceful shutdown on SIGTERM/SIGINT

The agent loop is the foundation for both human-interactive agents (which
display Rich prompts and wait for user input) and autonomous machine/AI
agents (which process tasks programmatically).

Usage::

    from cli.agent import FlowOSAgent, AgentConfig

    # Human agent
    config = AgentConfig(
        agent_id="human-agent-01",
        agent_type="human",
        name="Alice",
        capabilities=["code_review", "architecture"],
    )
    agent = FlowOSAgent(config)

    @agent.on_task_assigned
    def handle_task(task_payload: dict) -> None:
        print(f"New task: {task_payload['name']}")
        # ... do work ...

    agent.run()  # Blocks until SIGTERM/SIGINT

    # Or use as a context manager:
    with FlowOSAgent(config) as agent:
        agent.run()
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.text import Text

from shared.config import settings
from shared.models.event import EventRecord, EventSource, EventTopic, EventType

logger = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)

# Type alias for task handler functions
TaskHandler = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """
    Configuration for a FlowOS agent.

    Attributes:
        agent_id:       Unique agent identifier (UUID).
        agent_type:     Type of agent (human, ai, build, deploy, system).
        name:           Human-readable agent name.
        capabilities:   List of capability names this agent declares.
        workspace_root: Root directory for agent workspaces.
        heartbeat_interval: Seconds between heartbeat events.
        task_queue:     Kafka consumer group suffix for task routing.
        auto_accept:    If True, automatically accept assigned tasks.
    """

    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: str = "human"
    name: str = "FlowOS Agent"
    capabilities: list[str] = field(default_factory=list)
    workspace_root: str = field(
        default_factory=lambda: os.environ.get(
            "FLOWOS_WORKSPACE_ROOT",
            settings.workspace_root,
        )
    )
    heartbeat_interval: int = 30
    task_queue: str = "flowos-main"
    auto_accept: bool = False

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """
        Create an AgentConfig from environment variables.

        Environment variables:
            FLOWOS_AGENT_ID       -- Agent UUID
            FLOWOS_AGENT_TYPE     -- Agent type (human, ai, build, deploy)
            FLOWOS_AGENT_NAME     -- Human-readable name
            FLOWOS_AGENT_CAPS     -- Comma-separated capabilities
            FLOWOS_WORKSPACE_ROOT -- Workspace root directory
            FLOWOS_AUTO_ACCEPT    -- Auto-accept tasks (true/false)
        """
        caps_raw = os.environ.get("FLOWOS_AGENT_CAPS", "")
        capabilities = [c.strip() for c in caps_raw.split(",") if c.strip()]

        return cls(
            agent_id=os.environ.get("FLOWOS_AGENT_ID", str(uuid.uuid4())),
            agent_type=os.environ.get("FLOWOS_AGENT_TYPE", "human"),
            name=os.environ.get("FLOWOS_AGENT_NAME", "FlowOS Agent"),
            capabilities=capabilities,
            workspace_root=os.environ.get(
                "FLOWOS_WORKSPACE_ROOT", settings.workspace_root
            ),
            auto_accept=os.environ.get("FLOWOS_AUTO_ACCEPT", "false").lower() == "true",
        )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


class FlowOSAgent:
    """
    Event-driven FlowOS agent loop.

    Connects to Kafka, listens for task assignments, and dispatches them to
    registered handlers.  Emits lifecycle events (AGENT_ONLINE, AGENT_IDLE,
    AGENT_BUSY, AGENT_OFFLINE) to the agent events topic.

    Thread safety: The agent loop runs in the calling thread.  Handlers are
    called synchronously in the consumer loop thread.  Use threading or
    asyncio in handlers if you need concurrent execution.
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._task_handlers: list[TaskHandler] = []
        self._handoff_handlers: list[TaskHandler] = []
        self._stop_event = threading.Event()
        self._current_task: dict[str, Any] | None = None
        self._consumer = None
        self._producer = None
        self._started_at: datetime | None = None
        self._tasks_processed: int = 0
        self._tasks_accepted: int = 0

    # -----------------------------------------------------------------------
    # Handler registration
    # -----------------------------------------------------------------------

    def on_task_assigned(self, handler: TaskHandler) -> TaskHandler:
        """
        Register a handler for TASK_ASSIGNED events.

        The handler receives the event payload dict and should:
        - Accept the task (emit TASK_ACCEPTED)
        - Start work (emit TASK_STARTED)
        - Complete or fail the task (emit TASK_COMPLETED / TASK_FAILED)

        Can be used as a decorator::

            @agent.on_task_assigned
            def handle_task(payload: dict) -> None:
                print(f"Task: {payload['name']}")
        """
        self._task_handlers.append(handler)
        return handler

    def on_handoff_requested(self, handler: TaskHandler) -> TaskHandler:
        """
        Register a handler for TASK_HANDOFF_REQUESTED events.

        Can be used as a decorator::

            @agent.on_handoff_requested
            def handle_handoff(payload: dict) -> None:
                print(f"Handoff: {payload['handoff_id']}")
        """
        self._handoff_handlers.append(handler)
        return handler

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """
        Initialise the agent: connect to Kafka and emit AGENT_ONLINE.

        Called automatically by ``run()``.  Can be called manually if you
        want to start the agent without blocking.
        """
        self._started_at = datetime.now(timezone.utc)
        logger.info(
            "Starting FlowOS agent | id=%s type=%s name=%s",
            self.config.agent_id,
            self.config.agent_type,
            self.config.name,
        )

        # Initialise Kafka producer
        try:
            from shared.kafka.producer import FlowOSProducer
            self._producer = FlowOSProducer()
        except Exception as exc:
            logger.warning("Kafka producer unavailable: %s", exc)
            self._producer = None

        # Emit AGENT_ONLINE
        self._emit_agent_event(EventType.AGENT_ONLINE, {
            "name": self.config.name,
            "agent_type": self.config.agent_type,
            "capabilities": [
                {"name": cap, "enabled": True}
                for cap in self.config.capabilities
            ],
            "started_at": self._started_at.isoformat(),
        })

        # Initialise Kafka consumer
        try:
            from shared.kafka.consumer import FlowOSConsumer
            from shared.kafka.topics import KafkaTopic

            # Use agent-specific consumer group for task routing
            group_id = f"flowos-agent-{self.config.agent_id[:8]}"

            self._consumer = FlowOSConsumer(
                group_id=group_id,
                topics=[KafkaTopic.TASK_EVENTS],
            )

            # Register event handlers
            self._consumer.on(EventType.TASK_ASSIGNED)(self._handle_task_assigned)
            self._consumer.on(EventType.TASK_HANDOFF_REQUESTED)(
                self._handle_handoff_requested
            )

        except Exception as exc:
            logger.warning("Kafka consumer unavailable: %s", exc)
            self._consumer = None

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        console.print()
        console.print(
            Panel.fit(
                f"[bold green]Agent Online[/bold green]\n\n"
                f"[dim]Agent ID:[/dim]    {self.config.agent_id}\n"
                f"[dim]Type:[/dim]        {self.config.agent_type}\n"
                f"[dim]Name:[/dim]        {self.config.name}\n"
                f"[dim]Capabilities:[/dim] {', '.join(self.config.capabilities) or '(none)'}\n"
                f"[dim]Kafka:[/dim]       {'connected' if self._consumer else 'unavailable'}\n"
                f"[dim]Started:[/dim]     {self._started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                title="[bold]FlowOS Agent[/bold]",
                border_style="green",
            )
        )

    def stop(self) -> None:
        """
        Stop the agent gracefully.

        Emits AGENT_OFFLINE, stops the Kafka consumer, and flushes the
        producer.
        """
        logger.info("Stopping FlowOS agent: %s", self.config.agent_id)
        self._stop_event.set()

        self._emit_agent_event(EventType.AGENT_OFFLINE, {
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "tasks_processed": self._tasks_processed,
        })

        if self._consumer:
            try:
                self._consumer.stop()
            except Exception as exc:
                logger.debug("Error stopping consumer: %s", exc)

        if self._producer:
            try:
                self._producer.flush(timeout=5.0)
            except Exception as exc:
                logger.debug("Error flushing producer: %s", exc)

        console.print()
        console.print(
            f"[dim]Agent {self.config.agent_id[:8]}... stopped. "
            f"Processed {self._tasks_processed} task(s).[/dim]"
        )

    def run(self) -> None:
        """
        Start the agent and block until stopped.

        Runs the Kafka consumer loop in the current thread.  Call ``stop()``
        from another thread or send SIGTERM/SIGINT to stop.
        """
        self.start()

        if self._consumer:
            # Run the consumer loop (blocks until stop() is called)
            try:
                self._consumer.run()
            except Exception as exc:
                logger.error("Consumer loop error: %s", exc)
        else:
            # No Kafka: run a simple heartbeat loop
            console.print(
                "[yellow]WARN[/yellow] Kafka unavailable. Running in offline mode "
                "(heartbeat only)."
            )
            self._run_heartbeat_loop()

        self.stop()

    def _run_heartbeat_loop(self) -> None:
        """Run a simple heartbeat loop when Kafka is unavailable."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.config.heartbeat_interval)
            if not self._stop_event.is_set():
                logger.debug(
                    "Agent heartbeat | id=%s tasks=%d",
                    self.config.agent_id,
                    self._tasks_processed,
                )

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    def _handle_task_assigned(self, event: EventRecord) -> None:
        """Handle an incoming TASK_ASSIGNED event."""
        payload = event.payload
        task_id = payload.get("task_id", "")
        assigned_agent_id = payload.get("assigned_agent_id", "")

        # Only process tasks assigned to this agent
        if assigned_agent_id and assigned_agent_id != self.config.agent_id:
            logger.debug(
                "Ignoring task %s assigned to %s (we are %s)",
                task_id,
                assigned_agent_id,
                self.config.agent_id,
            )
            return

        self._tasks_processed += 1
        logger.info(
            "Task assigned | task_id=%s name=%s",
            task_id,
            payload.get("name", "?"),
        )

        # Emit AGENT_BUSY
        self._emit_agent_event(EventType.AGENT_BUSY, {
            "task_id": task_id,
            "task_name": payload.get("name"),
        })
        self._current_task = payload

        # Auto-accept if configured
        if self.config.auto_accept:
            self._emit_task_event(EventType.TASK_ACCEPTED, task_id, {
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            })
            self._tasks_accepted += 1

        # Dispatch to registered handlers
        for handler in self._task_handlers:
            try:
                handler(payload)
            except Exception as exc:
                logger.error(
                    "Task handler error | task_id=%s handler=%s error=%s",
                    task_id,
                    handler.__name__,
                    exc,
                )

        self._current_task = None

        # Emit AGENT_IDLE
        self._emit_agent_event(EventType.AGENT_IDLE, {
            "task_id": task_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    def _handle_handoff_requested(self, event: EventRecord) -> None:
        """Handle an incoming TASK_HANDOFF_REQUESTED event."""
        payload = event.payload
        to_agent_id = payload.get("to_agent_id", "")

        # Only process handoffs directed to this agent
        if to_agent_id and to_agent_id != self.config.agent_id:
            return

        logger.info(
            "Handoff requested | task_id=%s from=%s",
            payload.get("task_id", "?"),
            payload.get("from_agent_id", "?"),
        )

        for handler in self._handoff_handlers:
            try:
                handler(payload)
            except Exception as exc:
                logger.error(
                    "Handoff handler error | handler=%s error=%s",
                    handler.__name__,
                    exc,
                )

    # -----------------------------------------------------------------------
    # Event emission helpers
    # -----------------------------------------------------------------------

    def _emit_agent_event(
        self,
        event_type: EventType,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit an agent lifecycle event."""
        if not self._producer:
            return
        try:
            payload: dict[str, Any] = {
                "agent_id": self.config.agent_id,
                "agent_type": self.config.agent_type,
                "name": self.config.name,
            }
            if extra:
                payload.update(extra)

            event = EventRecord(
                event_type=event_type,
                topic=EventTopic.AGENT_EVENTS,
                source=EventSource.CLI,
                payload=payload,
                metadata={"agent_id": self.config.agent_id},
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.debug("Failed to emit agent event %s: %s", event_type, exc)

    def _emit_task_event(
        self,
        event_type: EventType,
        task_id: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a task lifecycle event."""
        if not self._producer:
            return
        try:
            payload: dict[str, Any] = {
                "task_id": task_id,
                "agent_id": self.config.agent_id,
            }
            if extra:
                payload.update(extra)

            event = EventRecord(
                event_type=event_type,
                topic=EventTopic.TASK_EVENTS,
                source=EventSource.CLI,
                payload=payload,
                metadata={
                    "agent_id": self.config.agent_id,
                    "task_id": task_id,
                },
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.debug("Failed to emit task event %s: %s", event_type, exc)

    # -----------------------------------------------------------------------
    # Signal handling
    # -----------------------------------------------------------------------

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received signal %s, stopping agent...", sig_name)
        console.print(f"\n[yellow]Received {sig_name}, shutting down...[/yellow]")
        self._stop_event.set()

    # -----------------------------------------------------------------------
    # Context manager support
    # -----------------------------------------------------------------------

    def __enter__(self) -> "FlowOSAgent":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self._stop_event.is_set():
            self.stop()


# ---------------------------------------------------------------------------
# ``flowos agent`` CLI command group
# ---------------------------------------------------------------------------


import click


@click.group("agent")
@click.pass_context
def agent_group(ctx: click.Context) -> None:
    """Agent lifecycle commands (start, register, status)."""
    ctx.ensure_object(dict)


@agent_group.command("start")
@click.option(
    "--agent-id",
    default=None,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID.  Auto-generated if omitted.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.option(
    "--agent-type",
    default="human",
    envvar="FLOWOS_AGENT_TYPE",
    type=click.Choice(["human", "ai", "build", "deploy", "system"], case_sensitive=False),
    help="Type of agent.",
)
@click.option(
    "--name",
    default=None,
    envvar="FLOWOS_AGENT_NAME",
    help="Human-readable agent name.",
    metavar="NAME",
)
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    help="Capability to declare (can be repeated).",
    metavar="CAP",
)
@click.option(
    "--auto-accept",
    is_flag=True,
    default=False,
    envvar="FLOWOS_AUTO_ACCEPT",
    help="Automatically accept assigned tasks.",
)
@click.pass_context
def agent_start(
    ctx: click.Context,
    agent_id: str | None,
    agent_type: str,
    name: str | None,
    capabilities: tuple[str, ...],
    auto_accept: bool,
) -> None:
    """
    Start the FlowOS agent loop.

    Connects to Kafka, registers the agent, and listens for task assignments.
    Blocks until SIGTERM/SIGINT is received.

    Examples::

        # Start a human agent
        flowos agent start --agent-type human --name "Alice"

        # Start an AI agent with capabilities
        flowos agent start \\
            --agent-type ai \\
            --name "AI Planner" \\
            --capability code_review \\
            --capability test_generation \\
            --auto-accept

        # Use environment variables
        FLOWOS_AGENT_ID=my-agent-id \\
        FLOWOS_AGENT_TYPE=build \\
        flowos agent start
    """
    config = AgentConfig(
        agent_id=agent_id or str(uuid.uuid4()),
        agent_type=agent_type,
        name=name or f"FlowOS {agent_type.capitalize()} Agent",
        capabilities=list(capabilities),
        auto_accept=auto_accept,
    )

    agent = FlowOSAgent(config)

    # Default handler: print task info to console
    @agent.on_task_assigned
    def default_task_handler(payload: dict[str, Any]) -> None:
        console.print()
        console.print(
            Panel.fit(
                f"[bold yellow]New Task Assigned[/bold yellow]\n\n"
                f"[dim]Task ID:[/dim]   {payload.get('task_id', '?')}\n"
                f"[dim]Name:[/dim]      {payload.get('name', '?')}\n"
                f"[dim]Type:[/dim]      {payload.get('task_type', '?')}\n"
                f"[dim]Priority:[/dim]  {payload.get('priority', '?')}\n"
                f"[dim]Workflow:[/dim]  {payload.get('workflow_id', '?')}",
                title="[bold]Task Assignment[/bold]",
                border_style="yellow",
            )
        )
        console.print(
            "[dim]Use [bold]flowos work accept[/bold] to accept this task.[/dim]"
        )

    agent.run()


@agent_group.command("info")
@click.option(
    "--agent-id",
    default=None,
    envvar="FLOWOS_AGENT_ID",
    help="Agent ID.  Defaults to $FLOWOS_AGENT_ID.",
    metavar="UUID",
)
@click.pass_context
def agent_info(
    ctx: click.Context,
    agent_id: str | None,
) -> None:
    """
    Show information about the current agent configuration.

    Reads from environment variables and displays the effective configuration.

    Example::

        FLOWOS_AGENT_ID=my-id flowos agent info
    """
    json_output = ctx.obj.get("json_output", False)

    config = AgentConfig.from_env()
    if agent_id:
        config.agent_id = agent_id

    result = {
        "agent_id": config.agent_id,
        "agent_type": config.agent_type,
        "name": config.name,
        "capabilities": config.capabilities,
        "workspace_root": config.workspace_root,
        "auto_accept": config.auto_accept,
        "heartbeat_interval": config.heartbeat_interval,
    }

    if json_output:
        import json
        click.echo(json.dumps(result, indent=2))
        return

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Agent ID:[/dim]       {config.agent_id}\n"
            f"[dim]Type:[/dim]           {config.agent_type}\n"
            f"[dim]Name:[/dim]           {config.name}\n"
            f"[dim]Capabilities:[/dim]   {', '.join(config.capabilities) or '(none)'}\n"
            f"[dim]Workspace Root:[/dim] {config.workspace_root}\n"
            f"[dim]Auto Accept:[/dim]    {config.auto_accept}\n"
            f"[dim]Heartbeat:[/dim]      {config.heartbeat_interval}s",
            title="[bold]Agent Configuration[/bold]",
            border_style="cyan",
        )
    )
