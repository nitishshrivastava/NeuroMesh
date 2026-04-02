"""
orchestrator/worker.py — FlowOS Temporal Worker

The Temporal worker is the execution engine for FlowOS workflows and activities.
It connects to the Temporal server, registers all workflow and activity
implementations, and polls the task queue for work.

Architecture:
- One worker process per deployment (can be scaled horizontally)
- Registers all workflow types: FeatureDeliveryWorkflow, BuildAndReviewWorkflow
- Registers all activity functions from all activity modules
- Uses async execution for activities (non-blocking I/O)
- Graceful shutdown on SIGTERM/SIGINT

Configuration (via environment variables):
    TEMPORAL_HOST          — Temporal server hostname (default: localhost)
    TEMPORAL_PORT          — Temporal gRPC port (default: 7233)
    TEMPORAL_NAMESPACE     — Temporal namespace (default: default)
    TEMPORAL_TASK_QUEUE    — Task queue name (default: flowos-main)
    TEMPORAL_MAX_CONCURRENT_ACTIVITIES — Max concurrent activities (default: 100)
    TEMPORAL_MAX_CONCURRENT_WORKFLOWS  — Max concurrent workflows (default: 500)

Usage::

    # Start the worker (blocking)
    python -m orchestrator.worker

    # Or with uvicorn-style hot reload for development
    python orchestrator/worker.py

    # With custom configuration
    TEMPORAL_HOST=temporal.prod.example.com \\
    TEMPORAL_NAMESPACE=flowos-prod \\
    python -m orchestrator.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import structlog
from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from shared.config import settings
from shared.kafka.admin import KafkaAdminClient

# Import all workflow classes
from orchestrator.workflows.feature_delivery import FeatureDeliveryWorkflow
from orchestrator.workflows.build_and_review import BuildAndReviewWorkflow

# Import all activity functions
from orchestrator.activities.task_activities import (
    create_task,
    assign_task,
    wait_for_task_completion,
    update_task_status,
    get_task_status,
    cancel_task,
)
from orchestrator.activities.checkpoint_activities import (
    create_checkpoint_record,
    verify_checkpoint,
    revert_to_checkpoint,
    list_task_checkpoints,
)
from orchestrator.activities.handoff_activities import (
    initiate_handoff,
    wait_for_handoff_acceptance,
    complete_handoff,
    cancel_handoff,
)
from orchestrator.activities.workspace_activities import (
    provision_workspace,
    sync_workspace,
    archive_workspace,
    get_workspace_status,
)
from orchestrator.activities.kafka_activities import (
    publish_workflow_event,
    publish_task_event,
    publish_workspace_event,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer() if settings.is_development else structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.DEBUG if settings.is_development else logging.INFO,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Activity and workflow registrations
# ─────────────────────────────────────────────────────────────────────────────

#: All workflow classes to register with the Temporal worker.
WORKFLOW_CLASSES = [
    FeatureDeliveryWorkflow,
    BuildAndReviewWorkflow,
]

#: All activity functions to register with the Temporal worker.
ACTIVITY_FUNCTIONS = [
    # Task activities
    create_task,
    assign_task,
    wait_for_task_completion,
    update_task_status,
    get_task_status,
    cancel_task,
    # Checkpoint activities
    create_checkpoint_record,
    verify_checkpoint,
    revert_to_checkpoint,
    list_task_checkpoints,
    # Handoff activities
    initiate_handoff,
    wait_for_handoff_acceptance,
    complete_handoff,
    cancel_handoff,
    # Workspace activities
    provision_workspace,
    sync_workspace,
    archive_workspace,
    get_workspace_status,
    # Kafka activities
    publish_workflow_event,
    publish_task_event,
    publish_workspace_event,
]


# ─────────────────────────────────────────────────────────────────────────────
# Temporal client factory
# ─────────────────────────────────────────────────────────────────────────────


async def create_temporal_client() -> Client:
    """
    Create and return a connected Temporal client.

    Reads connection settings from ``shared.config.settings.temporal``.
    Supports optional TLS/mTLS configuration.

    Returns:
        A connected ``temporalio.client.Client`` instance.

    Raises:
        RuntimeError: If the connection cannot be established.
    """
    temporal_cfg = settings.temporal

    tls_config: TLSConfig | None = None
    if temporal_cfg.tls_enabled:
        tls_kwargs: dict[str, Any] = {}
        if temporal_cfg.tls_cert_path and temporal_cfg.tls_key_path:
            with open(temporal_cfg.tls_cert_path, "rb") as f:
                client_cert = f.read()
            with open(temporal_cfg.tls_key_path, "rb") as f:
                client_key = f.read()
            tls_kwargs["client_cert"] = client_cert
            tls_kwargs["client_private_key"] = client_key
        if temporal_cfg.tls_ca_path:
            with open(temporal_cfg.tls_ca_path, "rb") as f:
                server_root_ca_cert = f.read()
            tls_kwargs["server_root_ca_cert"] = server_root_ca_cert
        tls_config = TLSConfig(**tls_kwargs)

    logger.info(
        "Connecting to Temporal",
        address=temporal_cfg.address,
        namespace=temporal_cfg.namespace,
        tls_enabled=temporal_cfg.tls_enabled,
    )

    client = await Client.connect(
        temporal_cfg.address,
        namespace=temporal_cfg.namespace,
        tls=tls_config,
    )

    logger.info(
        "Connected to Temporal",
        address=temporal_cfg.address,
        namespace=temporal_cfg.namespace,
    )
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Kafka topic initialisation
# ─────────────────────────────────────────────────────────────────────────────


async def ensure_kafka_topics() -> None:
    """
    Ensure all required Kafka topics exist before starting the worker.

    Uses the KafkaAdminClient to create topics that don't exist yet.
    This is idempotent — existing topics are not modified.
    """
    try:
        admin = KafkaAdminClient()
        admin.ensure_topics()
        logger.info("Kafka topics verified/created")
    except Exception as exc:
        logger.warning(
            "Failed to verify Kafka topics (non-fatal)",
            error=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Worker lifecycle
# ─────────────────────────────────────────────────────────────────────────────


async def run_worker() -> None:
    """
    Start the Temporal worker and run until shutdown.

    This is the main entry point for the worker process.  It:
    1. Ensures Kafka topics exist
    2. Connects to the Temporal server
    3. Creates and starts the worker
    4. Waits for shutdown signal (SIGTERM/SIGINT)
    5. Gracefully shuts down the worker

    The worker polls the configured task queue for workflow and activity tasks,
    executing them in the current process.
    """
    temporal_cfg = settings.temporal

    logger.info(
        "Starting FlowOS Temporal Worker",
        task_queue=temporal_cfg.task_queue,
        namespace=temporal_cfg.namespace,
        max_concurrent_activities=temporal_cfg.max_concurrent_activities,
        max_concurrent_workflows=temporal_cfg.max_concurrent_workflows,
        workflow_count=len(WORKFLOW_CLASSES),
        activity_count=len(ACTIVITY_FUNCTIONS),
    )

    # Ensure Kafka topics exist
    await ensure_kafka_topics()

    # Connect to Temporal
    client = await create_temporal_client()

    # Create the worker
    worker = Worker(
        client,
        task_queue=temporal_cfg.task_queue,
        workflows=WORKFLOW_CLASSES,
        activities=ACTIVITY_FUNCTIONS,
        max_concurrent_activities=temporal_cfg.max_concurrent_activities,
        max_concurrent_workflow_tasks=temporal_cfg.max_concurrent_workflows,
    )

    # Set up graceful shutdown
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Received shutdown signal", signal=sig.name)
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    logger.info(
        "Worker started, polling for tasks",
        task_queue=temporal_cfg.task_queue,
    )

    # Run worker until shutdown signal
    async with worker:
        await shutdown_event.wait()

    logger.info("Worker shut down gracefully")


# ─────────────────────────────────────────────────────────────────────────────
# Health check endpoint (optional, for Kubernetes probes)
# ─────────────────────────────────────────────────────────────────────────────


async def health_check_server(port: int = 8080) -> None:
    """
    Start a minimal HTTP health check server for Kubernetes liveness probes.

    Responds to GET /health with 200 OK when the worker is running.

    Args:
        port: Port to listen on (default: 8080).
    """
    from aiohttp import web

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "healthy", "service": "flowos-orchestrator"})

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server started", port=port)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    """
    Main entry point for the FlowOS orchestrator worker.

    Starts the Temporal worker and optionally the health check server.
    """
    import os

    # Optionally start health check server
    health_port = int(os.environ.get("HEALTH_CHECK_PORT", "0"))
    if health_port > 0:
        try:
            await health_check_server(health_port)
        except ImportError:
            logger.warning(
                "aiohttp not installed, health check server disabled. "
                "Install aiohttp to enable health checks."
            )

    # Run the worker (blocking until shutdown)
    await run_worker()


if __name__ == "__main__":
    asyncio.run(main())
