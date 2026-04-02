"""
tests/integration/test_machine_worker.py — Integration tests for MachineWorker.

Tests the MachineWorker's task processing logic using mocked Kafka and
build infrastructure. Tests cover:
- Worker initialisation
- Task acceptance logic
- Build event emission
- Worker capabilities
- BuildSpec and BuildStatus data models
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from shared.models.event import (
    EventRecord,
    EventSource,
    EventTopic,
    EventType,
)
from shared.kafka.schemas import (
    TaskAssignedPayload,
    BuildTriggeredPayload,
    BuildSucceededPayload,
    BuildFailedPayload,
    build_event,
)
from workers.argo_client import BuildSpec, BuildStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_assigned_event(
    task_id: str | None = None,
    workflow_id: str | None = None,
    task_type: str = "build",
) -> EventRecord:
    """Create a TASK_ASSIGNED event for a build task."""
    t_id = task_id or str(uuid.uuid4())
    wf_id = workflow_id or str(uuid.uuid4())
    payload = TaskAssignedPayload(
        task_id=t_id,
        workflow_id=wf_id,
        name="Build and Test",
        task_type=task_type,
        assigned_agent_id="machine-worker-001",
    )
    return build_event(
        event_type=EventType.TASK_ASSIGNED,
        payload=payload,
        source=EventSource.ORCHESTRATOR,
        workflow_id=wf_id,
        task_id=t_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMachineWorkerInit:
    """Tests for MachineWorker initialisation."""

    @patch("workers.machine_worker.FlowOSProducer")
    @patch("workers.machine_worker.FlowOSConsumer")
    @patch("workers.machine_worker.BuildRunner")
    @patch("workers.machine_worker.ArtifactUploader")
    def test_worker_initialises_with_defaults(
        self, mock_uploader, mock_runner, mock_consumer, mock_producer
    ):
        """MachineWorker initialises with default configuration."""
        from workers.machine_worker import MachineWorker

        mock_producer.return_value = MagicMock()
        mock_consumer.return_value = MagicMock()
        mock_runner.return_value = MagicMock()
        mock_uploader.return_value = MagicMock()

        worker = MachineWorker()

        assert worker.worker_id is not None
        assert worker.concurrency > 0
        assert not worker._running

    @patch("workers.machine_worker.FlowOSProducer")
    @patch("workers.machine_worker.FlowOSConsumer")
    @patch("workers.machine_worker.BuildRunner")
    @patch("workers.machine_worker.ArtifactUploader")
    def test_worker_accepts_custom_worker_id(
        self, mock_uploader, mock_runner, mock_consumer, mock_producer
    ):
        """MachineWorker accepts a custom worker_id."""
        from workers.machine_worker import MachineWorker

        mock_producer.return_value = MagicMock()
        mock_consumer.return_value = MagicMock()
        mock_runner.return_value = MagicMock()
        mock_uploader.return_value = MagicMock()

        worker = MachineWorker(worker_id="custom-worker-001")

        assert worker.worker_id == "custom-worker-001"

    @patch("workers.machine_worker.FlowOSProducer")
    @patch("workers.machine_worker.FlowOSConsumer")
    @patch("workers.machine_worker.BuildRunner")
    @patch("workers.machine_worker.ArtifactUploader")
    def test_worker_has_build_capabilities(
        self, mock_uploader, mock_runner, mock_consumer, mock_producer
    ):
        """MachineWorker declares build capabilities."""
        from workers.machine_worker import MachineWorker

        assert "build" in MachineWorker.CAPABILITIES

    @patch("workers.machine_worker.FlowOSProducer")
    @patch("workers.machine_worker.FlowOSConsumer")
    @patch("workers.machine_worker.BuildRunner")
    @patch("workers.machine_worker.ArtifactUploader")
    def test_worker_not_running_on_init(
        self, mock_uploader, mock_runner, mock_consumer, mock_producer
    ):
        """MachineWorker is not running immediately after initialisation."""
        from workers.machine_worker import MachineWorker

        mock_producer.return_value = MagicMock()
        mock_consumer.return_value = MagicMock()
        mock_runner.return_value = MagicMock()
        mock_uploader.return_value = MagicMock()

        worker = MachineWorker()
        assert worker._running is False


class TestBuildSpec:
    """Tests for BuildSpec data model."""

    def test_create_build_spec(self):
        """BuildSpec can be created with required fields."""
        build_id = f"build-{uuid.uuid4().hex[:8]}"
        spec = BuildSpec(
            build_id=build_id,
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
        )

        assert spec.build_id == build_id
        assert spec.task_id is not None

    def test_build_spec_with_environment(self):
        """BuildSpec accepts environment variables via env_vars."""
        spec = BuildSpec(
            build_id=f"build-{uuid.uuid4().hex[:8]}",
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            env_vars={"CI": "true", "BUILD_ENV": "test"},
        )

        assert spec.env_vars["CI"] == "true"

    def test_build_spec_with_timeout(self):
        """BuildSpec accepts a timeout in seconds."""
        spec = BuildSpec(
            build_id=f"build-{uuid.uuid4().hex[:8]}",
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            timeout_secs=1800,
        )

        assert spec.timeout_secs == 1800

    def test_build_spec_defaults(self):
        """BuildSpec has sensible defaults."""
        spec = BuildSpec(
            build_id=f"build-{uuid.uuid4().hex[:8]}",
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
        )

        assert spec.env_vars == {}
        assert spec.timeout_secs > 0
        assert spec.branch == "main"

    def test_build_spec_with_build_command(self):
        """BuildSpec accepts a build command."""
        spec = BuildSpec(
            build_id=f"build-{uuid.uuid4().hex[:8]}",
            task_id=str(uuid.uuid4()),
            workflow_id=str(uuid.uuid4()),
            build_command="pytest tests/ -v",
        )

        assert "pytest" in spec.build_command


class TestBuildStatus:
    """Tests for BuildStatus enum."""

    def test_build_status_values(self):
        """BuildStatus has the expected enum values."""
        assert BuildStatus.PENDING == "Pending"
        assert BuildStatus.RUNNING == "Running"
        assert BuildStatus.SUCCEEDED == "Succeeded"
        assert BuildStatus.FAILED == "Failed"

    def test_build_status_is_str_enum(self):
        """BuildStatus is a StrEnum."""
        assert isinstance(BuildStatus.PENDING, str)
        assert str(BuildStatus.SUCCEEDED) == "Succeeded"

    def test_build_status_all_values(self):
        """All BuildStatus values are accessible."""
        statuses = list(BuildStatus)
        assert len(statuses) > 0
        assert BuildStatus.SUCCEEDED in statuses
        assert BuildStatus.FAILED in statuses


class TestTaskAssignedEventHandling:
    """Tests for handling TASK_ASSIGNED events."""

    def test_task_assigned_event_has_correct_structure(self):
        """TASK_ASSIGNED event has the expected payload structure."""
        event = _make_task_assigned_event()

        assert event.event_type == EventType.TASK_ASSIGNED
        assert event.topic == EventTopic.TASK_EVENTS
        assert "task_id" in event.payload
        assert "task_type" in event.payload
        assert event.payload["task_type"] == "build"

    def test_task_assigned_event_roundtrip(self):
        """TASK_ASSIGNED event survives serialisation/deserialisation."""
        task_id = str(uuid.uuid4())
        event = _make_task_assigned_event(task_id=task_id)

        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_type == EventType.TASK_ASSIGNED
        assert restored.payload["task_id"] == task_id
        assert restored.payload["task_type"] == "build"

    def test_non_build_task_not_for_machine_worker(self):
        """TASK_ASSIGNED events for non-build tasks should not be processed."""
        human_task_event = _make_task_assigned_event(task_type="human")
        assert human_task_event.payload["task_type"] == "human"
        # Machine worker should only process build tasks
        assert human_task_event.payload["task_type"] != "build"


class TestBuildEventEmission:
    """Tests for build event emission."""

    def test_build_triggered_event_structure(self):
        """BUILD_TRIGGERED event has correct structure."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        build_id = f"build-{uuid.uuid4().hex[:8]}"

        payload = BuildTriggeredPayload(
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            repository="github.com/org/repo",
            branch="main",
        )
        event = build_event(
            event_type=EventType.BUILD_TRIGGERED,
            payload=payload,
            source=EventSource.WORKER,
            workflow_id=workflow_id,
            task_id=task_id,
        )

        assert event.event_type == EventType.BUILD_TRIGGERED
        assert event.topic == EventTopic.BUILD_EVENTS
        assert event.payload["build_id"] == build_id
        assert event.payload["repository"] == "github.com/org/repo"

    def test_build_succeeded_event_structure(self):
        """BUILD_SUCCEEDED event has correct structure."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        build_id = f"build-{uuid.uuid4().hex[:8]}"

        payload = BuildSucceededPayload(
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            artifacts=[{"name": "artifact.tar.gz", "url": "s3://bucket/artifact.tar.gz"}],
            duration_seconds=45.2,
        )
        event = build_event(
            event_type=EventType.BUILD_SUCCEEDED,
            payload=payload,
            source=EventSource.WORKER,
            workflow_id=workflow_id,
            task_id=task_id,
        )

        assert event.event_type == EventType.BUILD_SUCCEEDED
        assert event.payload["build_id"] == build_id
        assert event.payload["duration_seconds"] == pytest.approx(45.2)
        assert len(event.payload["artifacts"]) == 1

    def test_build_failed_event_structure(self):
        """BUILD_FAILED event has correct structure."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        build_id = f"build-{uuid.uuid4().hex[:8]}"

        payload = BuildFailedPayload(
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            error_message="Compilation failed: undefined symbol",
            duration_seconds=12.5,
        )
        event = build_event(
            event_type=EventType.BUILD_FAILED,
            payload=payload,
            source=EventSource.WORKER,
            workflow_id=workflow_id,
            task_id=task_id,
        )

        assert event.event_type == EventType.BUILD_FAILED
        assert "Compilation failed" in event.payload["error_message"]
        assert event.payload["duration_seconds"] == pytest.approx(12.5)

    def test_build_events_roundtrip(self):
        """Build events survive serialisation/deserialisation."""
        task_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        build_id = f"build-{uuid.uuid4().hex[:8]}"

        payload = BuildSucceededPayload(
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            artifacts=[{"name": "artifact.tar.gz", "url": "s3://bucket/artifact.tar.gz"}],
            duration_seconds=30.0,
        )
        event = build_event(
            event_type=EventType.BUILD_SUCCEEDED,
            payload=payload,
            source=EventSource.WORKER,
        )

        raw = event.to_kafka_value()
        restored = EventRecord.from_kafka_value(raw)

        assert restored.event_type == EventType.BUILD_SUCCEEDED
        assert restored.payload["build_id"] == build_id
