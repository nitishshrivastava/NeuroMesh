"""
workers/machine_worker.py — FlowOS Machine Worker

The machine worker is a long-running process that:

1. Subscribes to ``flowos.task.events`` (consumer group: ``flowos-build-runner``)
   and listens for ``TASK_ASSIGNED`` events where ``task_type == "build"``.
2. Accepts the task by publishing a ``TASK_ACCEPTED`` event.
3. Submits the build job to the configured backend (Argo Workflows or local
   subprocess) via ``BuildRunner``.
4. Publishes ``BUILD_TRIGGERED``, ``BUILD_STARTED``, and either
   ``BUILD_SUCCEEDED`` or ``BUILD_FAILED`` events to ``flowos.build.events``.
5. Uploads any produced artifacts to MinIO/S3 via ``ArtifactUploader``.
6. Publishes ``TASK_COMPLETED`` or ``TASK_FAILED`` events to close the task
   lifecycle.
7. Registers itself as an agent on startup (``AGENT_REGISTERED``, ``AGENT_ONLINE``)
   and publishes ``AGENT_OFFLINE`` on graceful shutdown.

Architecture:
- The worker runs a Kafka consumer loop in the main thread.
- Each build is executed asynchronously via ``asyncio.run()`` in a thread pool
  so that the consumer loop remains responsive.
- Signal handlers (SIGTERM, SIGINT) trigger graceful shutdown.

Configuration (via environment variables):
    MACHINE_WORKER_ID       — Unique worker ID (default: auto-generated)
    MACHINE_WORKER_CONCURRENCY — Max concurrent builds (default: 4)
    BUILD_BACKEND           — "argo" | "local" (default: "local")
    KAFKA_BOOTSTRAP_SERVERS — Kafka broker addresses
    S3_ENDPOINT_URL         — MinIO/S3 endpoint
    S3_ACCESS_KEY_ID        — S3 access key
    S3_SECRET_ACCESS_KEY    — S3 secret key

Usage::

    # Start the machine worker (blocking)
    python -m workers.machine_worker

    # With custom worker ID
    MACHINE_WORKER_ID=worker-001 python -m workers.machine_worker

    # Force local build backend
    BUILD_BACKEND=local python -m workers.machine_worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import structlog

from shared.config import settings
from shared.kafka.consumer import FlowOSConsumer
from shared.kafka.producer import FlowOSProducer
from shared.kafka.schemas import (
    AgentRegisteredPayload,
    AgentOnlinePayload,
    AgentOfflinePayload,
    BuildTriggeredPayload,
    BuildStartedPayload,
    BuildSucceededPayload,
    BuildFailedPayload,
    TaskAcceptedPayload,
    TaskStartedPayload,
    TaskCompletedPayload,
    TaskFailedPayload,
    build_event,
)
from shared.kafka.topics import ConsumerGroup, KafkaTopic
from shared.models.event import EventRecord, EventSource, EventType
from workers.argo_client import BuildRunner, BuildSpec, BuildStatus
from workers.artifact_uploader import ArtifactUploader

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
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(message)s",
    stream=sys.stdout,
)

logger = structlog.get_logger("flowos.machine_worker")


# ─────────────────────────────────────────────────────────────────────────────
# Machine Worker
# ─────────────────────────────────────────────────────────────────────────────


class MachineWorker:
    """
    FlowOS Machine Worker — executes build/test tasks via Argo or subprocess.

    Lifecycle:
        worker = MachineWorker()
        worker.start()   # blocks until shutdown signal

    The worker registers itself as a FlowOS agent on startup and deregisters
    on graceful shutdown.  It processes ``TASK_ASSIGNED`` events for tasks of
    type ``build`` and orchestrates the full build lifecycle.

    Attributes:
        worker_id:     Unique identifier for this worker instance.
        concurrency:   Maximum number of concurrent builds.
        capabilities:  List of task types this worker can handle.
    """

    CAPABILITIES = ["build", "test", "deploy"]

    def __init__(
        self,
        worker_id: str | None = None,
        concurrency: int | None = None,
        build_backend: str | None = None,
    ) -> None:
        self.worker_id = worker_id or os.environ.get(
            "MACHINE_WORKER_ID", f"machine-worker-{uuid.uuid4().hex[:8]}"
        )
        self.concurrency = concurrency or int(
            os.environ.get("MACHINE_WORKER_CONCURRENCY", "4")
        )
        self._build_backend = build_backend or os.environ.get("BUILD_BACKEND", "local")

        # Kafka components
        self._producer = FlowOSProducer()
        self._consumer = FlowOSConsumer(
            group_id=ConsumerGroup.BUILD_RUNNER,
            topics=[KafkaTopic.TASK_EVENTS],
        )

        # Build infrastructure
        self._build_runner = BuildRunner(backend=self._build_backend)
        self._artifact_uploader = ArtifactUploader(producer=self._producer)

        # Concurrency control
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="flowos-build",
        )
        self._active_builds: dict[str, str] = {}  # build_id → task_id
        self._active_builds_lock = threading.Lock()
        self._active_build_count = 0

        # Shutdown flag
        self._shutdown_event = threading.Event()
        self._running = False

        logger.info(
            "MachineWorker initialised",
            worker_id=self.worker_id,
            concurrency=self.concurrency,
            backend=self._build_backend,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the machine worker.

        Registers the worker as a FlowOS agent, sets up signal handlers,
        registers Kafka event handlers, and starts the consumer loop.

        This method blocks until a shutdown signal is received.
        """
        logger.info("Starting FlowOS Machine Worker", worker_id=self.worker_id)

        # Install signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)

        # Register as a FlowOS agent
        self._register_agent()

        # Register Kafka event handlers
        self._consumer.on(EventType.TASK_ASSIGNED)(self._handle_task_assigned)

        self._running = True
        logger.info(
            "Machine worker started — listening for build tasks",
            worker_id=self.worker_id,
            topics=[KafkaTopic.TASK_EVENTS],
        )

        try:
            self._consumer.run()
        except Exception as exc:
            logger.error(
                "Consumer loop error",
                worker_id=self.worker_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            self._shutdown()

    def stop(self) -> None:
        """
        Request a graceful shutdown.

        Signals the consumer loop to stop and waits for active builds to
        complete (up to 30 seconds).
        """
        logger.info("Shutdown requested", worker_id=self.worker_id)
        self._shutdown_event.set()
        self._consumer.stop()

    # ─────────────────────────────────────────────────────────────────────────
    # Event handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_task_assigned(self, event: EventRecord) -> None:
        """
        Handle a ``TASK_ASSIGNED`` event.

        Checks whether the task is a build task assigned to this worker.
        If so, accepts the task and submits it to the build runner in a
        background thread.
        """
        payload = event.payload
        task_type = payload.get("task_type", "")
        assigned_agent_id = payload.get("assigned_agent_id", "")

        # Only handle build tasks assigned to this worker
        if task_type not in self.CAPABILITIES:
            logger.debug(
                "Ignoring non-build task",
                task_type=task_type,
                task_id=payload.get("task_id"),
            )
            return

        if assigned_agent_id and assigned_agent_id != self.worker_id:
            logger.debug(
                "Task assigned to different agent",
                assigned_agent_id=assigned_agent_id,
                worker_id=self.worker_id,
            )
            return

        task_id = payload.get("task_id")
        workflow_id = payload.get("workflow_id")
        task_name = payload.get("name", "unnamed-task")

        if not task_id:
            logger.warning("Received TASK_ASSIGNED with no task_id", payload=payload)
            return

        # Check concurrency limit
        with self._active_builds_lock:
            if self._active_build_count >= self.concurrency:
                logger.warning(
                    "Concurrency limit reached — rejecting task",
                    task_id=task_id,
                    active_builds=self._active_build_count,
                    limit=self.concurrency,
                )
                return
            self._active_build_count += 1

        logger.info(
            "Accepting build task",
            task_id=task_id,
            workflow_id=workflow_id,
            task_name=task_name,
            worker_id=self.worker_id,
        )

        # Accept the task
        self._publish_task_accepted(task_id, workflow_id)

        # Submit build to thread pool
        self._executor.submit(
            self._run_build_sync,
            task_id=task_id,
            workflow_id=workflow_id,
            task_name=task_name,
            payload=payload,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Build execution
    # ─────────────────────────────────────────────────────────────────────────

    def _run_build_sync(
        self,
        task_id: str,
        workflow_id: str | None,
        task_name: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Synchronous wrapper that runs the async build in a new event loop.

        This method runs in a thread pool executor thread.
        """
        try:
            asyncio.run(
                self._run_build_async(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    task_name=task_name,
                    payload=payload,
                )
            )
        except Exception as exc:
            logger.error(
                "Unhandled error in build thread",
                task_id=task_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            with self._active_builds_lock:
                self._active_build_count -= 1

    async def _run_build_async(
        self,
        task_id: str,
        workflow_id: str | None,
        task_name: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Execute the full build lifecycle for a single task.

        Publishes the following events in order:
        1. TASK_STARTED
        2. BUILD_TRIGGERED
        3. BUILD_STARTED
        4. BUILD_SUCCEEDED or BUILD_FAILED
        5. ARTIFACT_UPLOADED (for each artifact)
        6. TASK_COMPLETED or TASK_FAILED
        """
        build_id = str(uuid.uuid4())
        build_config = payload.get("build_config", {})
        repository = build_config.get("repository", payload.get("repository", ""))
        branch = build_config.get("branch", payload.get("branch", "main"))
        commit_sha = build_config.get("commit_sha", payload.get("commit_sha"))
        build_command = build_config.get(
            "build_command",
            payload.get("build_command", "echo 'Build complete'"),
        )
        image = build_config.get("image", "alpine:3.19")
        timeout_secs = int(build_config.get("timeout_secs", 3600))

        logger.info(
            "Starting build",
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repository=repository,
            branch=branch,
        )

        # Track active build
        with self._active_builds_lock:
            self._active_builds[build_id] = task_id

        try:
            # 1. Publish TASK_STARTED
            self._publish_task_started(task_id, workflow_id)

            # 2. Publish BUILD_TRIGGERED
            self._publish_build_triggered(
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
                build_config=build_config,
            )

            # 3. Publish BUILD_STARTED
            self._publish_build_started(build_id=build_id)

            # 4. Execute the build
            spec = BuildSpec(
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
                build_command=build_command,
                image=image,
                timeout_secs=timeout_secs,
                env_vars={
                    "FLOWOS_TASK_ID": task_id,
                    "FLOWOS_WORKFLOW_ID": workflow_id or "",
                    "FLOWOS_WORKER_ID": self.worker_id,
                    **build_config.get("env_vars", {}),
                },
            )

            result = await self._build_runner.run_build(spec)

            # 5. Upload artifacts (if any)
            uploaded_artifacts: list[dict[str, Any]] = []
            if result.artifacts:
                upload_results = await self._artifact_uploader.upload_artifacts_batch(
                    artifacts=result.artifacts,
                    build_id=build_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                )
                uploaded_artifacts = [
                    ur.to_dict()
                    for ur in upload_results
                    if ur.success
                ]

            # 6. Publish BUILD_SUCCEEDED or BUILD_FAILED
            if result.succeeded:
                self._publish_build_succeeded(
                    build_id=build_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    artifacts=uploaded_artifacts,
                    duration_seconds=result.duration_seconds,
                )
                self._publish_task_completed(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    outputs={
                        "build_id": build_id,
                        "artifacts": uploaded_artifacts,
                        "duration_seconds": result.duration_seconds,
                        "stdout": result.stdout[:2000],  # truncate for Kafka
                    },
                )
                logger.info(
                    "Build task completed successfully",
                    build_id=build_id,
                    task_id=task_id,
                    duration=result.duration_seconds,
                    artifacts=len(uploaded_artifacts),
                )
            else:
                self._publish_build_failed(
                    build_id=build_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    error_message=result.error_message,
                    error_details={
                        "exit_code": result.exit_code,
                        "stderr": result.stderr[:2000],
                        "status": result.status,
                    },
                    duration_seconds=result.duration_seconds,
                )
                self._publish_task_failed(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    error_message=result.error_message,
                    error_details={
                        "build_id": build_id,
                        "exit_code": result.exit_code,
                        "status": result.status,
                    },
                )
                logger.warning(
                    "Build task failed",
                    build_id=build_id,
                    task_id=task_id,
                    status=result.status,
                    error=result.error_message,
                )

        except Exception as exc:
            error_msg = f"Unexpected error during build: {exc}"
            logger.error(
                "Build execution error",
                build_id=build_id,
                task_id=task_id,
                error=error_msg,
                exc_info=True,
            )
            try:
                self._publish_build_failed(
                    build_id=build_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    error_message=error_msg,
                    error_details={"exception": str(exc)},
                    duration_seconds=0.0,
                )
                self._publish_task_failed(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    error_message=error_msg,
                    error_details={"build_id": build_id},
                )
            except Exception as publish_exc:
                logger.error(
                    "Failed to publish failure events",
                    error=str(publish_exc),
                )
        finally:
            with self._active_builds_lock:
                self._active_builds.pop(build_id, None)

    # ─────────────────────────────────────────────────────────────────────────
    # Event publishing helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _publish_task_accepted(
        self, task_id: str, workflow_id: str | None
    ) -> None:
        """Publish a TASK_ACCEPTED event."""
        try:
            payload = TaskAcceptedPayload(
                task_id=task_id,
                workflow_id=workflow_id or "",
                agent_id=self.worker_id,
            )
            event = build_event(
                event_type=EventType.TASK_ACCEPTED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish TASK_ACCEPTED", task_id=task_id, error=str(exc))

    def _publish_task_started(
        self, task_id: str, workflow_id: str | None
    ) -> None:
        """Publish a TASK_STARTED event."""
        try:
            payload = TaskStartedPayload(
                task_id=task_id,
                workflow_id=workflow_id or "",
                agent_id=self.worker_id,
            )
            event = build_event(
                event_type=EventType.TASK_STARTED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish TASK_STARTED", task_id=task_id, error=str(exc))

    def _publish_task_completed(
        self,
        task_id: str,
        workflow_id: str | None,
        outputs: dict[str, Any],
    ) -> None:
        """Publish a TASK_COMPLETED event."""
        try:
            # TaskCompletedPayload.outputs is list[dict], convert from dict
            outputs_list = [{"key": k, "value": v} for k, v in outputs.items()]
            payload = TaskCompletedPayload(
                task_id=task_id,
                workflow_id=workflow_id or "",
                agent_id=self.worker_id,
                outputs=outputs_list,
            )
            event = build_event(
                event_type=EventType.TASK_COMPLETED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish TASK_COMPLETED", task_id=task_id, error=str(exc))

    def _publish_task_failed(
        self,
        task_id: str,
        workflow_id: str | None,
        error_message: str,
        error_details: dict[str, Any],
    ) -> None:
        """Publish a TASK_FAILED event."""
        try:
            payload = TaskFailedPayload(
                task_id=task_id,
                workflow_id=workflow_id or "",
                agent_id=self.worker_id,
                error_message=error_message,
                error_details=error_details,
            )
            event = build_event(
                event_type=EventType.TASK_FAILED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish TASK_FAILED", task_id=task_id, error=str(exc))

    def _publish_build_triggered(
        self,
        build_id: str,
        task_id: str | None,
        workflow_id: str | None,
        repository: str,
        branch: str,
        commit_sha: str | None,
        build_config: dict[str, Any],
    ) -> None:
        """Publish a BUILD_TRIGGERED event."""
        try:
            payload = BuildTriggeredPayload(
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
                build_config=build_config,
                triggered_by=self.worker_id,
            )
            event = build_event(
                event_type=EventType.BUILD_TRIGGERED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish BUILD_TRIGGERED", build_id=build_id, error=str(exc))

    def _publish_build_started(self, build_id: str) -> None:
        """Publish a BUILD_STARTED event."""
        try:
            payload = BuildStartedPayload(
                build_id=build_id,
                runner_id=self.worker_id,
            )
            event = build_event(
                event_type=EventType.BUILD_STARTED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish BUILD_STARTED", build_id=build_id, error=str(exc))

    def _publish_build_succeeded(
        self,
        build_id: str,
        task_id: str | None,
        workflow_id: str | None,
        artifacts: list[dict[str, Any]],
        duration_seconds: float | None,
    ) -> None:
        """Publish a BUILD_SUCCEEDED event."""
        try:
            payload = BuildSucceededPayload(
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                artifacts=artifacts,
                duration_seconds=duration_seconds,
            )
            event = build_event(
                event_type=EventType.BUILD_SUCCEEDED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
            logger.info(
                "Published BUILD_SUCCEEDED",
                build_id=build_id,
                task_id=task_id,
                artifacts=len(artifacts),
            )
        except Exception as exc:
            logger.warning("Failed to publish BUILD_SUCCEEDED", build_id=build_id, error=str(exc))

    def _publish_build_failed(
        self,
        build_id: str,
        task_id: str | None,
        workflow_id: str | None,
        error_message: str,
        error_details: dict[str, Any],
        duration_seconds: float | None,
    ) -> None:
        """Publish a BUILD_FAILED event."""
        try:
            payload = BuildFailedPayload(
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                error_message=error_message,
                error_details=error_details,
                duration_seconds=duration_seconds,
            )
            event = build_event(
                event_type=EventType.BUILD_FAILED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=task_id,
                workflow_id=workflow_id,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish BUILD_FAILED", build_id=build_id, error=str(exc))

    def _register_agent(self) -> None:
        """Publish AGENT_REGISTERED and AGENT_ONLINE events."""
        try:
            agent_name = f"Machine Worker ({self.worker_id})"
            registered_payload = AgentRegisteredPayload(
                agent_id=self.worker_id,
                name=agent_name,
                agent_type="machine",
                capabilities=self.CAPABILITIES,
            )
            registered_event = build_event(
                event_type=EventType.AGENT_REGISTERED,
                payload=registered_payload,
                source=EventSource.BUILD_RUNNER,
                agent_id=self.worker_id,
            )
            self._producer.produce(registered_event)

            online_payload = AgentOnlinePayload(
                agent_id=self.worker_id,
                name=agent_name,
            )
            online_event = build_event(
                event_type=EventType.AGENT_ONLINE,
                payload=online_payload,
                source=EventSource.BUILD_RUNNER,
                agent_id=self.worker_id,
            )
            self._producer.produce(online_event)
            self._producer.flush()

            logger.info(
                "Agent registered",
                worker_id=self.worker_id,
                capabilities=self.CAPABILITIES,
            )
        except Exception as exc:
            logger.warning("Failed to register agent", error=str(exc))

    def _deregister_agent(self) -> None:
        """Publish AGENT_OFFLINE event."""
        try:
            agent_name = f"Machine Worker ({self.worker_id})"
            payload = AgentOfflinePayload(
                agent_id=self.worker_id,
                name=agent_name,
                reason="graceful_shutdown",
            )
            event = build_event(
                event_type=EventType.AGENT_OFFLINE,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                agent_id=self.worker_id,
            )
            self._producer.produce(event)
            self._producer.flush(timeout=5.0)
            logger.info("Agent deregistered", worker_id=self.worker_id)
        except Exception as exc:
            logger.warning("Failed to deregister agent", error=str(exc))

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("Received shutdown signal", signal=sig_name, worker_id=self.worker_id)
        self.stop()

    def _shutdown(self) -> None:
        """Perform graceful shutdown: wait for active builds, deregister."""
        logger.info("Shutting down machine worker", worker_id=self.worker_id)

        # Wait for active builds to complete (up to 30 seconds)
        deadline = time.time() + 30
        while time.time() < deadline:
            with self._active_builds_lock:
                if self._active_build_count == 0:
                    break
            logger.info(
                "Waiting for active builds to complete",
                active_builds=self._active_build_count,
            )
            time.sleep(1)

        # Shutdown thread pool
        self._executor.shutdown(wait=False)

        # Deregister agent
        self._deregister_agent()

        # Close Kafka producer
        try:
            self._producer.close()
        except Exception:
            pass

        self._running = False
        logger.info("Machine worker stopped", worker_id=self.worker_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_hostname() -> str:
    """Return the current machine hostname."""
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Start the FlowOS Machine Worker."""
    worker = MachineWorker()
    worker.start()


if __name__ == "__main__":
    main()
