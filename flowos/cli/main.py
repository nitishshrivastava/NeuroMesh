"""
cli/main.py — FlowOS CLI Entry Point

Provides the ``flowos`` Click command group and all sub-commands for
interacting with the FlowOS platform from the command line.

Usage::

    flowos workspace init --agent-id <id> [--task-id <id>] [--branch main]
    flowos workspace status
    flowos workspace checkpoint "feat: implemented auth"
    flowos workspace revert <checkpoint-id>
    flowos workspace log
    flowos workspace branch --task-id <id>
    flowos workspace handoff --handoff-id <id> --to-agent <id>
    flowos workspace archive

The CLI is designed to be used by both human operators and AI agents.  All
output is structured (JSON or Rich tables) for easy parsing by automated
consumers.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import print as rprint

from cli.workspace_manager import WorkspaceManager, _create_workspace_dirs
from shared.models.checkpoint import CheckpointType
from shared.models.workspace import WorkspaceStatus, WorkspaceType

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("flowos.cli")

console = Console()
err_console = Console(stderr=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_workspace_root(ctx: click.Context) -> str:
    """
    Resolve the workspace root from the CLI context or environment.

    Priority:
    1. ``--workspace`` option on the root command
    2. ``FLOWOS_WORKSPACE`` environment variable
    3. Current working directory
    """
    root = ctx.obj.get("workspace_root") if ctx.obj else None
    if not root:
        root = os.environ.get("FLOWOS_WORKSPACE")
    if not root:
        root = os.getcwd()
    return root


def _output_json(data: Any) -> None:
    """Print data as formatted JSON to stdout."""
    click.echo(json.dumps(data, indent=2, default=str))


def _success(message: str) -> None:
    """Print a success message with a green checkmark."""
    console.print(f"[bold green]✓[/bold green] {message}")


def _error(message: str) -> None:
    """Print an error message to stderr and exit with code 1."""
    err_console.print(f"[bold red]✗[/bold red] {message}")
    sys.exit(1)


def _warn(message: str) -> None:
    """Print a warning message."""
    console.print(f"[bold yellow]⚠[/bold yellow] {message}")


# ─────────────────────────────────────────────────────────────────────────────
# Root command group
# ─────────────────────────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--workspace",
    "-w",
    default=None,
    envvar="FLOWOS_WORKSPACE",
    help="Path to the workspace root directory.  Defaults to $FLOWOS_WORKSPACE or CWD.",
    metavar="PATH",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose (DEBUG) logging.",
)
@click.option(
    "--json-output",
    "json_output",
    is_flag=True,
    default=False,
    help="Output results as JSON (useful for scripting).",
)
@click.version_option(version="0.1.0", prog_name="flowos")
@click.pass_context
def cli(
    ctx: click.Context,
    workspace: str | None,
    verbose: bool,
    json_output: bool,
) -> None:
    """
    FlowOS — Distributed Human + Machine Orchestration Platform.

    Use ``flowos COMMAND --help`` for help on a specific command.
    """
    ctx.ensure_object(dict)
    ctx.obj["workspace_root"] = workspace
    ctx.obj["json_output"] = json_output

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("flowos").setLevel(logging.DEBUG)


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace`` sub-group
# ─────────────────────────────────────────────────────────────────────────────


@cli.group("workspace")
@click.pass_context
def workspace_group(ctx: click.Context) -> None:
    """Manage FlowOS agent workspaces."""
    ctx.ensure_object(dict)


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace init``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("init")
@click.option(
    "--agent-id",
    required=True,
    help="Agent ID that owns this workspace.",
    metavar="UUID",
)
@click.option(
    "--task-id",
    default=None,
    help="Task ID this workspace is being created for.",
    metavar="UUID",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Workflow ID this workspace is associated with.",
    metavar="UUID",
)
@click.option(
    "--workspace-id",
    default=None,
    help="Explicit workspace ID (auto-generated if omitted).",
    metavar="UUID",
)
@click.option(
    "--branch",
    default="main",
    show_default=True,
    help="Initial Git branch name.",
    metavar="BRANCH",
)
@click.option(
    "--repo-url",
    default=None,
    help="Remote Git repository URL to clone.  If omitted, a fresh local repo is created.",
    metavar="URL",
)
@click.option(
    "--workspace-type",
    default="local",
    show_default=True,
    type=click.Choice(["local", "remote", "ephemeral", "shared"], case_sensitive=False),
    help="Kind of workspace.",
)
@click.option(
    "--branch-prefix",
    default="flowos",
    show_default=True,
    help="Prefix for task branches (e.g. 'flowos' → 'flowos/task/<id>').",
    metavar="PREFIX",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Tag to attach to the workspace (can be specified multiple times).",
    metavar="TAG",
)
@click.option(
    "--author-name",
    default=WorkspaceManager.__init__.__defaults__[0] if WorkspaceManager.__init__.__defaults__ else "FlowOS Agent",
    show_default=True,
    help="Git author name for commits.",
    metavar="NAME",
)
@click.option(
    "--author-email",
    default="agent@flowos.local",
    show_default=True,
    help="Git author email for commits.",
    metavar="EMAIL",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a WORKSPACE_CREATED Kafka event.",
)
@click.pass_context
def workspace_init(
    ctx: click.Context,
    agent_id: str,
    task_id: str | None,
    workflow_id: str | None,
    workspace_id: str | None,
    branch: str,
    repo_url: str | None,
    workspace_type: str,
    branch_prefix: str,
    tags: tuple[str, ...],
    author_name: str,
    author_email: str,
    no_event: bool,
) -> None:
    """
    Initialise a new FlowOS agent workspace.

    Creates the workspace directory structure::

        <workspace>/
        ├── repo/           ← Git repository
        ├── .flowos/        ← FlowOS metadata
        │   ├── workspace.json
        │   ├── state.json
        │   ├── checkpoints.json
        │   └── config.json
        ├── artifacts/      ← Build outputs
        ├── logs/           ← Execution logs
        └── patches/        ← AI patches

    Examples::

        flowos workspace init --agent-id $(uuidgen) --branch main

        flowos -w /tmp/my-workspace workspace init \\
            --agent-id abc123 \\
            --task-id def456 \\
            --repo-url https://github.com/org/repo.git
    """
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    ws_type = WorkspaceType(workspace_type.lower())

    try:
        manager = WorkspaceManager(
            root=root,
            author_name=author_name,
            author_email=author_email,
        )

        workspace = manager.init(
            agent_id=agent_id,
            task_id=task_id,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            branch=branch,
            repo_url=repo_url,
            workspace_type=ws_type,
            branch_prefix=branch_prefix,
            tags=list(tags),
            emit_event=not no_event,
        )

    except Exception as exc:
        _error(f"Failed to initialise workspace: {exc}")
        return  # unreachable — _error calls sys.exit

    if json_output:
        _output_json(workspace.model_dump(mode="json"))
        return

    # Rich output
    console.print()
    console.print(
        Panel.fit(
            f"[bold green]Workspace initialised successfully[/bold green]\n\n"
            f"[dim]Root:[/dim]         {root}\n"
            f"[dim]Workspace ID:[/dim] {workspace.workspace_id}\n"
            f"[dim]Agent ID:[/dim]     {workspace.agent_id}\n"
            f"[dim]Branch:[/dim]       {workspace.git_state.branch}\n"
            f"[dim]Status:[/dim]       {workspace.status}",
            title="[bold]flowos workspace init[/bold]",
            border_style="green",
        )
    )

    # Show directory structure
    console.print()
    console.print("[bold]Directory structure:[/bold]")
    root_path = Path(root)
    for subdir in ("repo", ".flowos", "artifacts", "logs", "patches"):
        subdir_path = root_path / subdir
        exists_marker = "[green]✓[/green]" if subdir_path.exists() else "[red]✗[/red]"
        console.print(f"  {exists_marker} {subdir}/")

    # Show .flowos/ files
    flowos_dir = root_path / ".flowos"
    if flowos_dir.exists():
        console.print()
        console.print("[bold].flowos/ metadata files:[/bold]")
        for meta_file in sorted(flowos_dir.iterdir()):
            if meta_file.is_file():
                size = meta_file.stat().st_size
                console.print(f"  [green]✓[/green] .flowos/{meta_file.name} ({size} bytes)")

    console.print()
    _success(f"Workspace ready at: {root}")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace status``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("status")
@click.pass_context
def workspace_status(ctx: click.Context) -> None:
    """Show the current status of the workspace."""
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        status_data = manager.status()
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to get workspace status: {exc}")
        return

    if json_output:
        _output_json(status_data)
        return

    ws = status_data["workspace"]
    git = status_data["git_state"]
    checkpoints = status_data["checkpoints"]
    disk_mb = status_data["disk_usage_mb"]

    console.print()
    console.print(
        Panel.fit(
            f"[dim]Workspace ID:[/dim]  {ws['workspace_id']}\n"
            f"[dim]Agent ID:[/dim]      {ws['agent_id']}\n"
            f"[dim]Status:[/dim]        {ws['status']}\n"
            f"[dim]Root:[/dim]          {ws['root_path']}\n"
            f"[dim]Disk usage:[/dim]    {disk_mb:.1f} MB",
            title="[bold]Workspace[/bold]",
            border_style="blue",
        )
    )

    console.print()
    dirty_marker = "[yellow]dirty[/yellow]" if git["is_dirty"] else "[green]clean[/green]"
    console.print(
        Panel.fit(
            f"[dim]Branch:[/dim]   {git['branch']}\n"
            f"[dim]Commit:[/dim]   {(git.get('commit_sha') or 'none')[:8]}\n"
            f"[dim]Message:[/dim]  {git.get('commit_message') or '(none)'}\n"
            f"[dim]State:[/dim]    {dirty_marker}\n"
            f"[dim]Staged:[/dim]   {len(git.get('staged_files', []))} file(s)\n"
            f"[dim]Unstaged:[/dim] {len(git.get('unstaged_files', []))} file(s)\n"
            f"[dim]Untracked:[/dim]{len(git.get('untracked_files', []))} file(s)",
            title="[bold]Git State[/bold]",
            border_style="cyan",
        )
    )

    if checkpoints:
        console.print()
        table = Table(title="Checkpoints", show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=4)
        table.add_column("ID", style="cyan", width=12)
        table.add_column("SHA", style="yellow", width=10)
        table.add_column("Type", width=12)
        table.add_column("Status", width=10)
        table.add_column("Message", no_wrap=False)

        for ckpt in sorted(checkpoints, key=lambda c: c.get("sequence", 0)):
            sha = (ckpt.get("git_commit_sha") or "")[:8]
            table.add_row(
                str(ckpt.get("sequence", "?")),
                ckpt.get("checkpoint_id", "")[:8] + "…",
                sha,
                ckpt.get("checkpoint_type", ""),
                ckpt.get("status", ""),
                ckpt.get("message", "")[:60],
            )
        console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace checkpoint``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("checkpoint")
@click.argument("message")
@click.option(
    "--type",
    "checkpoint_type",
    default="manual",
    show_default=True,
    type=click.Choice(
        ["manual", "auto", "pre_handoff", "pre_revert", "milestone", "ai_proposal", "completion"],
        case_sensitive=False,
    ),
    help="Checkpoint type.",
)
@click.option(
    "--progress",
    "task_progress",
    default=None,
    type=click.FloatRange(0.0, 100.0),
    help="Estimated task completion percentage (0–100).",
    metavar="PERCENT",
)
@click.option(
    "--notes",
    default=None,
    help="Free-form notes to attach to this checkpoint.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a CHECKPOINT_CREATED Kafka event.",
)
@click.pass_context
def workspace_checkpoint(
    ctx: click.Context,
    message: str,
    checkpoint_type: str,
    task_progress: float | None,
    notes: str | None,
    no_event: bool,
) -> None:
    """
    Stage all changes and create a checkpoint commit.

    MESSAGE is the commit message for the checkpoint.

    Example::

        flowos workspace checkpoint "feat: implemented user auth"
        flowos workspace checkpoint "wip: halfway done" --progress 50
    """
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    ckpt_type = CheckpointType(checkpoint_type.lower())

    try:
        manager = WorkspaceManager(root=root)
        checkpoint = manager.checkpoint(
            message=message,
            checkpoint_type=ckpt_type,
            task_progress=task_progress,
            notes=notes,
            emit_event=not no_event,
        )
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to create checkpoint: {exc}")
        return

    if json_output:
        _output_json(checkpoint.model_dump(mode="json"))
        return

    _success(
        f"Checkpoint created: {checkpoint.checkpoint_id[:8]}… "
        f"(sha={checkpoint.git_commit_sha[:8] if checkpoint.git_commit_sha else '?'})"
    )
    console.print(f"  [dim]Message:[/dim] {checkpoint.message}")
    console.print(f"  [dim]Type:[/dim]    {checkpoint.checkpoint_type}")
    if task_progress is not None:
        console.print(f"  [dim]Progress:[/dim] {task_progress:.0f}%")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace revert``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("revert")
@click.argument("checkpoint_id")
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a CHECKPOINT_REVERTED Kafka event.",
)
@click.pass_context
def workspace_revert(
    ctx: click.Context,
    checkpoint_id: str,
    no_event: bool,
) -> None:
    """
    Revert the workspace to a specific checkpoint.

    CHECKPOINT_ID is the full or abbreviated checkpoint ID.

    Example::

        flowos workspace revert abc12345-...
    """
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        # Support abbreviated IDs: find the matching checkpoint
        checkpoints = manager.list_checkpoints()
        full_id: str | None = None
        for ckpt in checkpoints:
            if ckpt.checkpoint_id.startswith(checkpoint_id):
                full_id = ckpt.checkpoint_id
                break

        if full_id is None:
            _error(
                f"Checkpoint {checkpoint_id!r} not found. "
                "Run 'flowos workspace log' to see available checkpoints."
            )
            return

        reverted = manager.revert_to_checkpoint(
            checkpoint_id=full_id,
            emit_event=not no_event,
        )
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to revert to checkpoint: {exc}")
        return

    if json_output:
        _output_json(reverted.model_dump(mode="json"))
        return

    _success(
        f"Reverted to checkpoint {reverted.checkpoint_id[:8]}… "
        f"(sha={reverted.git_commit_sha[:8] if reverted.git_commit_sha else '?'})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace log``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("log")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of checkpoints to show.",
    metavar="N",
)
@click.pass_context
def workspace_log(ctx: click.Context, limit: int) -> None:
    """Show the checkpoint history for this workspace."""
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        checkpoints = manager.list_checkpoints()
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to load checkpoints: {exc}")
        return

    if json_output:
        _output_json([c.model_dump(mode="json") for c in checkpoints[:limit]])
        return

    if not checkpoints:
        console.print("[dim]No checkpoints found.[/dim]")
        return

    table = Table(
        title=f"Checkpoint History ({len(checkpoints)} total)",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Checkpoint ID", style="cyan", width=14)
    table.add_column("SHA", style="yellow", width=10)
    table.add_column("Branch", width=20)
    table.add_column("Type", width=14)
    table.add_column("Status", width=10)
    table.add_column("Progress", width=10)
    table.add_column("Message")

    for ckpt in checkpoints[:limit]:
        sha = (ckpt.git_commit_sha or "")[:8]
        progress = f"{ckpt.task_progress:.0f}%" if ckpt.task_progress is not None else "—"
        status_style = {
            "committed": "green",
            "verified": "bright_green",
            "reverted": "yellow",
            "failed": "red",
            "creating": "dim",
        }.get(ckpt.status, "white")

        table.add_row(
            str(ckpt.sequence),
            ckpt.checkpoint_id[:12] + "…",
            sha,
            ckpt.git_branch[:18],
            ckpt.checkpoint_type,
            f"[{status_style}]{ckpt.status}[/{status_style}]",
            progress,
            ckpt.message[:50],
        )

    console.print()
    console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace branch``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("branch")
@click.option(
    "--task-id",
    required=True,
    help="Task ID to create the branch for.",
    metavar="UUID",
)
@click.option(
    "--base",
    default=None,
    help="Base commit SHA or branch name.  Defaults to HEAD.",
    metavar="REF",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a BRANCH_CREATED Kafka event.",
)
@click.pass_context
def workspace_branch(
    ctx: click.Context,
    task_id: str,
    base: str | None,
    no_event: bool,
) -> None:
    """
    Create a task-scoped Git branch.

    Branch name format: ``<branch_prefix>/task/<short_task_id>``

    Example::

        flowos workspace branch --task-id abc123-...
    """
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        branch_name = manager.create_task_branch(
            task_id=task_id,
            base_ref=base,
            emit_event=not no_event,
        )
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to create branch: {exc}")
        return

    if json_output:
        _output_json({"branch_name": branch_name, "task_id": task_id})
        return

    _success(f"Created and checked out branch: {branch_name}")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace handoff``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("handoff")
@click.option(
    "--handoff-id",
    required=True,
    help="Handoff record ID.",
    metavar="UUID",
)
@click.option(
    "--to-agent",
    "to_agent_id",
    required=True,
    help="Agent ID receiving the handoff.",
    metavar="UUID",
)
@click.option(
    "--message",
    default=None,
    help="Optional checkpoint message for the handoff snapshot.",
    metavar="TEXT",
)
@click.option(
    "--no-event",
    "no_event",
    is_flag=True,
    default=False,
    help="Do not emit a HANDOFF_PREPARED Kafka event.",
)
@click.pass_context
def workspace_handoff(
    ctx: click.Context,
    handoff_id: str,
    to_agent_id: str,
    message: str | None,
    no_event: bool,
) -> None:
    """
    Prepare the workspace for a handoff to another agent.

    Creates a PRE_HANDOFF checkpoint and emits a HANDOFF_PREPARED event.

    Example::

        flowos workspace handoff --handoff-id hoff-123 --to-agent agent-def
    """
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        checkpoint = manager.prepare_handoff(
            handoff_id=handoff_id,
            to_agent_id=to_agent_id,
            message=message,
            emit_event=not no_event,
        )
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to prepare handoff: {exc}")
        return

    if json_output:
        _output_json(checkpoint.model_dump(mode="json"))
        return

    _success(
        f"Handoff prepared: checkpoint {checkpoint.checkpoint_id[:8]}… "
        f"(sha={checkpoint.git_commit_sha[:8] if checkpoint.git_commit_sha else '?'})"
    )
    console.print(f"  [dim]Handoff ID:[/dim]  {handoff_id}")
    console.print(f"  [dim]To agent:[/dim]    {to_agent_id}")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace archive``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("archive")
@click.pass_context
def workspace_archive(ctx: click.Context) -> None:
    """Mark the workspace as archived."""
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        workspace = manager.archive()
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to archive workspace: {exc}")
        return

    if json_output:
        _output_json(workspace.model_dump(mode="json"))
        return

    _success(f"Workspace archived: {workspace.workspace_id}")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace diff``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("diff")
@click.option(
    "--from-ref",
    "from_ref",
    default=None,
    help="Starting ref (commit SHA or branch).  Defaults to HEAD~1.",
    metavar="REF",
)
@click.option(
    "--to-ref",
    "to_ref",
    default=None,
    help="Ending ref.  Defaults to HEAD.",
    metavar="REF",
)
@click.option(
    "--stat",
    "stat_only",
    is_flag=True,
    default=False,
    help="Show only the diffstat (file names and change counts).",
)
@click.pass_context
def workspace_diff(
    ctx: click.Context,
    from_ref: str | None,
    to_ref: str | None,
    stat_only: bool,
) -> None:
    """Show the diff between two refs (or working tree vs HEAD)."""
    root = _get_workspace_root(ctx)

    try:
        manager = WorkspaceManager(root=root)
        diff_output = manager.git.diff(
            from_ref=from_ref,
            to_ref=to_ref,
            stat_only=stat_only,
        )
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to compute diff: {exc}")
        return

    if diff_output:
        click.echo(diff_output)
    else:
        console.print("[dim]No differences found.[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# ``flowos workspace git-log``
# ─────────────────────────────────────────────────────────────────────────────


@workspace_group.command("git-log")
@click.option(
    "--limit",
    default=10,
    show_default=True,
    type=int,
    help="Maximum number of commits to show.",
    metavar="N",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to show log for.  Defaults to current branch.",
    metavar="BRANCH",
)
@click.pass_context
def workspace_git_log(
    ctx: click.Context,
    limit: int,
    branch: str | None,
) -> None:
    """Show the Git commit log for the workspace repository."""
    root = _get_workspace_root(ctx)
    json_output = ctx.obj.get("json_output", False)

    try:
        manager = WorkspaceManager(root=root)
        commits = manager.git.get_log(branch=branch, max_count=limit)
    except FileNotFoundError as exc:
        _error(str(exc))
        return
    except Exception as exc:
        _error(f"Failed to get git log: {exc}")
        return

    if json_output:
        import dataclasses
        _output_json([dataclasses.asdict(c) for c in commits])
        return

    if not commits:
        console.print("[dim]No commits found.[/dim]")
        return

    table = Table(
        title=f"Git Log ({len(commits)} commits)",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("SHA", style="yellow", width=10)
    table.add_column("Author", width=24)
    table.add_column("Date", width=20)
    table.add_column("Message")

    for commit in commits:
        date_str = commit.committed_at.strftime("%Y-%m-%d %H:%M") if commit.committed_at else "?"
        table.add_row(
            commit.short_sha,
            commit.author[:22],
            date_str,
            commit.message.split("\n")[0][:60],
        )

    console.print()
    console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Register additional command groups from cli/commands/
# ---------------------------------------------------------------------------

from cli.commands.workflow import workflow_group
from cli.commands.work import work_group
from cli.commands.run import run_group
from cli.commands.ai import ai_group
from cli.agent import agent_group

cli.add_command(workflow_group)
cli.add_command(work_group)
cli.add_command(run_group)
cli.add_command(ai_group)
cli.add_command(agent_group)


if __name__ == "__main__":
    cli()
