"""
workers — FlowOS Machine Worker Package

Provides the machine worker and supporting infrastructure for executing
build/test tasks via Argo Workflows (Kubernetes) or local subprocess.

Components:
    machine_worker    — Main worker process: consumes TASK_ASSIGNED events,
                        orchestrates builds, publishes BUILD_* events.
    argo_client       — Argo Workflows REST client + local subprocess fallback.
    artifact_uploader — S3/MinIO artifact uploader with Kafka event publishing.
"""

from workers.argo_client import (
    ArgoClient,
    BuildResult,
    BuildRunner,
    BuildSpec,
    BuildStatus,
    LocalBuildRunner,
)
from workers.artifact_uploader import ArtifactUploader, UploadResult
from workers.machine_worker import MachineWorker

__all__ = [
    "ArgoClient",
    "ArtifactUploader",
    "BuildResult",
    "BuildRunner",
    "BuildSpec",
    "BuildStatus",
    "LocalBuildRunner",
    "MachineWorker",
    "UploadResult",
]
