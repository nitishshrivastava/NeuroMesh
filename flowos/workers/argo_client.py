"""
workers/argo_client.py — FlowOS Argo Workflows Client

Provides a high-level client for submitting and monitoring build/test jobs
on Argo Workflows (Kubernetes-native workflow engine).  In local/dev mode
(when no Kubernetes cluster is available), the client falls back to running
build commands directly in a subprocess, making the machine worker fully
functional without a Kubernetes cluster.

Architecture:
- ``ArgoClient`` wraps the Argo Workflows REST API (v3.x) for job submission
  and status polling.
- ``LocalBuildRunner`` provides a subprocess-based fallback for development.
- ``BuildRunner`` is the unified interface that selects the appropriate backend
  based on configuration.

Build lifecycle:
    PENDING → RUNNING → SUCCEEDED | FAILED | ERROR | TIMEOUT

Configuration (via environment variables):
    ARGO_SERVER_URL         — Argo server URL (default: http://localhost:2746)
    ARGO_NAMESPACE          — Kubernetes namespace (default: argo)
    ARGO_SERVICE_ACCOUNT    — Service account for workflow pods (default: argo)
    ARGO_TOKEN              — Bearer token for Argo API auth (optional)
    ARGO_VERIFY_SSL         — Verify SSL certificates (default: true)
    ARGO_POLL_INTERVAL_SECS — Status poll interval in seconds (default: 5)
    ARGO_MAX_WAIT_SECS      — Maximum wait time for a build (default: 3600)
    BUILD_BACKEND           — "argo" | "local" (default: "local")
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Build status enum
# ─────────────────────────────────────────────────────────────────────────────


class BuildStatus(StrEnum):
    """Lifecycle states of a build/test job."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    TIMEOUT = "Timeout"
    UNKNOWN = "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BuildSpec:
    """
    Specification for a build/test job.

    Attributes:
        build_id:       Globally unique build identifier.
        task_id:        FlowOS task that triggered this build.
        workflow_id:    FlowOS workflow this build belongs to.
        repository:     Repository URL or local path.
        branch:         Branch to build.
        commit_sha:     Specific commit SHA (optional).
        build_command:  Shell command to execute (for local runner).
        image:          Container image for Argo workflow pods.
        entrypoint:     Entrypoint command for the container.
        env_vars:       Environment variables to inject.
        resource_cpu:   CPU request/limit (e.g. "500m").
        resource_memory: Memory request/limit (e.g. "512Mi").
        timeout_secs:   Maximum build duration in seconds.
        artifacts_path: Path inside the container where artifacts are written.
        labels:         Kubernetes labels for the workflow.
    """

    build_id: str
    task_id: str | None = None
    workflow_id: str | None = None
    repository: str = ""
    branch: str = "main"
    commit_sha: str | None = None
    build_command: str = "echo 'No build command specified'"
    image: str = "alpine:3.19"
    entrypoint: list[str] = field(default_factory=lambda: ["sh", "-c"])
    env_vars: dict[str, str] = field(default_factory=dict)
    resource_cpu: str = "500m"
    resource_memory: str = "512Mi"
    timeout_secs: int = 3600
    artifacts_path: str = "/workspace/artifacts"
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class BuildResult:
    """
    Result of a completed build/test job.

    Attributes:
        build_id:           Build identifier.
        status:             Final build status.
        exit_code:          Process exit code (0 = success).
        stdout:             Captured standard output.
        stderr:             Captured standard error.
        duration_seconds:   Total build duration.
        artifacts:          List of artifact descriptors produced.
        error_message:      Human-readable error description (if failed).
        argo_workflow_name: Argo workflow name (if using Argo backend).
        started_at:         Unix timestamp when the build started.
        finished_at:        Unix timestamp when the build finished.
    """

    build_id: str
    status: BuildStatus
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    argo_workflow_name: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        """Return True if the build succeeded."""
        return self.status == BuildStatus.SUCCEEDED

    @property
    def failed(self) -> bool:
        """Return True if the build failed or errored."""
        return self.status in (BuildStatus.FAILED, BuildStatus.ERROR, BuildStatus.TIMEOUT)


# ─────────────────────────────────────────────────────────────────────────────
# Argo Workflows client
# ─────────────────────────────────────────────────────────────────────────────


class ArgoClient:
    """
    Client for the Argo Workflows REST API (v3.x).

    Submits build/test jobs as Argo Workflow resources and polls for
    completion.  Requires a running Argo Workflows server accessible at
    ``ARGO_SERVER_URL``.

    Usage::

        client = ArgoClient()
        result = await client.run_build(spec)
        if result.succeeded:
            print(f"Build {result.build_id} succeeded in {result.duration_seconds:.1f}s")
    """

    def __init__(
        self,
        server_url: str | None = None,
        namespace: str | None = None,
        service_account: str | None = None,
        token: str | None = None,
        verify_ssl: bool | None = None,
        poll_interval_secs: float | None = None,
        max_wait_secs: float | None = None,
    ) -> None:
        self.server_url = (
            server_url
            or os.environ.get("ARGO_SERVER_URL", "http://localhost:2746")
        ).rstrip("/")
        self.namespace = namespace or os.environ.get("ARGO_NAMESPACE", "argo")
        self.service_account = service_account or os.environ.get(
            "ARGO_SERVICE_ACCOUNT", "argo"
        )
        self.token = token or os.environ.get("ARGO_TOKEN")
        self.verify_ssl = (
            verify_ssl
            if verify_ssl is not None
            else os.environ.get("ARGO_VERIFY_SSL", "true").lower() != "false"
        )
        self.poll_interval_secs = poll_interval_secs or float(
            os.environ.get("ARGO_POLL_INTERVAL_SECS", "5")
        )
        self.max_wait_secs = max_wait_secs or float(
            os.environ.get("ARGO_MAX_WAIT_SECS", "3600")
        )

        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            self._headers["Authorization"] = f"Bearer {self.token}"

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def run_build(self, spec: BuildSpec) -> BuildResult:
        """
        Submit a build job to Argo Workflows and wait for completion.

        Args:
            spec: Build specification.

        Returns:
            BuildResult with final status and captured output.
        """
        started_at = time.time()
        workflow_name = self._make_workflow_name(spec.build_id)

        logger.info(
            "Submitting Argo workflow | build_id=%s workflow=%s namespace=%s",
            spec.build_id,
            workflow_name,
            self.namespace,
        )

        manifest = self._build_workflow_manifest(spec, workflow_name)

        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
                # Submit the workflow
                submit_url = (
                    f"{self.server_url}/api/v1/workflows/{self.namespace}"
                )
                response = await client.post(
                    submit_url,
                    json=manifest,
                    headers=self._headers,
                )
                response.raise_for_status()
                submitted = response.json()
                actual_name = submitted.get("metadata", {}).get("name", workflow_name)

                logger.info(
                    "Argo workflow submitted | build_id=%s workflow=%s",
                    spec.build_id,
                    actual_name,
                )

                # Poll for completion
                result = await self._poll_workflow(
                    client=client,
                    workflow_name=actual_name,
                    build_id=spec.build_id,
                    started_at=started_at,
                    timeout_secs=spec.timeout_secs,
                )
                result.argo_workflow_name = actual_name
                return result

        except httpx.HTTPStatusError as exc:
            error_msg = f"Argo API error: {exc.response.status_code} {exc.response.text}"
            logger.error(
                "Argo workflow submission failed | build_id=%s error=%s",
                spec.build_id,
                error_msg,
            )
            return BuildResult(
                build_id=spec.build_id,
                status=BuildStatus.ERROR,
                exit_code=1,
                error_message=error_msg,
                duration_seconds=time.time() - started_at,
                started_at=started_at,
                finished_at=time.time(),
            )
        except httpx.RequestError as exc:
            error_msg = f"Argo server unreachable: {exc}"
            logger.error(
                "Argo connection failed | build_id=%s error=%s",
                spec.build_id,
                error_msg,
            )
            return BuildResult(
                build_id=spec.build_id,
                status=BuildStatus.ERROR,
                exit_code=1,
                error_message=error_msg,
                duration_seconds=time.time() - started_at,
                started_at=started_at,
                finished_at=time.time(),
            )

    async def get_workflow_status(self, workflow_name: str) -> dict[str, Any]:
        """
        Fetch the current status of an Argo workflow.

        Args:
            workflow_name: Argo workflow resource name.

        Returns:
            Raw Argo workflow status dict.

        Raises:
            httpx.HTTPStatusError: If the API returns an error response.
        """
        url = f"{self.server_url}/api/v1/workflows/{self.namespace}/{workflow_name}"
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=15.0) as client:
            response = await client.get(url, headers=self._headers)
            response.raise_for_status()
            return response.json()

    async def delete_workflow(self, workflow_name: str) -> None:
        """
        Delete an Argo workflow resource.

        Args:
            workflow_name: Argo workflow resource name.
        """
        url = f"{self.server_url}/api/v1/workflows/{self.namespace}/{workflow_name}"
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=15.0) as client:
            response = await client.delete(url, headers=self._headers)
            if response.status_code not in (200, 204, 404):
                response.raise_for_status()
            logger.debug("Deleted Argo workflow | name=%s", workflow_name)

    async def list_workflows(
        self,
        label_selector: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List Argo workflows in the configured namespace.

        Args:
            label_selector: Optional Kubernetes label selector string.
            limit:          Maximum number of workflows to return.

        Returns:
            List of workflow resource dicts.
        """
        url = f"{self.server_url}/api/v1/workflows/{self.namespace}"
        params: dict[str, Any] = {"listOptions.limit": limit}
        if label_selector:
            params["listOptions.labelSelector"] = label_selector

        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=15.0) as client:
            response = await client.get(url, headers=self._headers, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("items") or []

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _poll_workflow(
        self,
        client: httpx.AsyncClient,
        workflow_name: str,
        build_id: str,
        started_at: float,
        timeout_secs: int,
    ) -> BuildResult:
        """Poll an Argo workflow until it reaches a terminal state."""
        effective_timeout = min(timeout_secs, int(self.max_wait_secs))
        deadline = started_at + effective_timeout

        while True:
            elapsed = time.time() - started_at
            if time.time() > deadline:
                logger.warning(
                    "Build timed out | build_id=%s elapsed=%.1fs",
                    build_id,
                    elapsed,
                )
                return BuildResult(
                    build_id=build_id,
                    status=BuildStatus.TIMEOUT,
                    exit_code=1,
                    error_message=f"Build timed out after {effective_timeout}s",
                    duration_seconds=elapsed,
                    started_at=started_at,
                    finished_at=time.time(),
                )

            try:
                url = f"{self.server_url}/api/v1/workflows/{self.namespace}/{workflow_name}"
                response = await client.get(url, headers=self._headers)
                response.raise_for_status()
                wf_data = response.json()
            except httpx.RequestError as exc:
                logger.warning(
                    "Poll request failed (will retry) | build_id=%s error=%s",
                    build_id,
                    exc,
                )
                await asyncio.sleep(self.poll_interval_secs)
                continue

            phase = (
                wf_data.get("status", {}).get("phase", "Unknown")
            )
            logger.debug(
                "Workflow phase | build_id=%s workflow=%s phase=%s elapsed=%.1fs",
                build_id,
                workflow_name,
                phase,
                elapsed,
            )

            if phase in ("Succeeded", "Failed", "Error"):
                finished_at = time.time()
                duration = finished_at - started_at

                # Extract logs from node status
                stdout, stderr = self._extract_logs(wf_data)

                status = BuildStatus(phase) if phase in BuildStatus._value2member_map_ else BuildStatus.UNKNOWN
                exit_code = 0 if phase == "Succeeded" else 1
                error_message = ""
                if phase != "Succeeded":
                    error_message = (
                        wf_data.get("status", {}).get("message", f"Workflow {phase}")
                    )

                logger.info(
                    "Build finished | build_id=%s status=%s duration=%.1fs",
                    build_id,
                    status,
                    duration,
                )
                return BuildResult(
                    build_id=build_id,
                    status=status,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=duration,
                    error_message=error_message,
                    started_at=started_at,
                    finished_at=finished_at,
                )

            await asyncio.sleep(self.poll_interval_secs)

    def _build_workflow_manifest(
        self, spec: BuildSpec, workflow_name: str
    ) -> dict[str, Any]:
        """Build an Argo Workflow manifest from a BuildSpec."""
        labels = {
            "app": "flowos",
            "flowos/build-id": spec.build_id[:63],  # K8s label value limit
            **spec.labels,
        }
        if spec.task_id:
            labels["flowos/task-id"] = spec.task_id[:63]
        if spec.workflow_id:
            labels["flowos/workflow-id"] = spec.workflow_id[:63]

        env = [
            {"name": k, "value": v}
            for k, v in spec.env_vars.items()
        ]
        env.extend([
            {"name": "FLOWOS_BUILD_ID", "value": spec.build_id},
            {"name": "FLOWOS_BRANCH", "value": spec.branch},
        ])
        if spec.commit_sha:
            env.append({"name": "FLOWOS_COMMIT_SHA", "value": spec.commit_sha})

        return {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "name": workflow_name,
                "namespace": self.namespace,
                "labels": labels,
            },
            "spec": {
                "serviceAccountName": self.service_account,
                "activeDeadlineSeconds": spec.timeout_secs,
                "entrypoint": "build",
                "templates": [
                    {
                        "name": "build",
                        "container": {
                            "image": spec.image,
                            "command": spec.entrypoint,
                            "args": [spec.build_command],
                            "env": env,
                            "resources": {
                                "requests": {
                                    "cpu": spec.resource_cpu,
                                    "memory": spec.resource_memory,
                                },
                                "limits": {
                                    "cpu": spec.resource_cpu,
                                    "memory": spec.resource_memory,
                                },
                            },
                        },
                    }
                ],
            },
        }

    @staticmethod
    def _make_workflow_name(build_id: str) -> str:
        """Generate a valid Kubernetes resource name from a build ID."""
        # K8s names must be lowercase alphanumeric + hyphens, max 253 chars
        safe = build_id.lower().replace("_", "-").replace(".", "-")
        # Ensure it starts with a letter
        if safe and not safe[0].isalpha():
            safe = "build-" + safe
        return f"flowos-build-{safe}"[:253]

    @staticmethod
    def _extract_logs(wf_data: dict[str, Any]) -> tuple[str, str]:
        """Extract stdout/stderr from Argo workflow node status."""
        nodes = wf_data.get("status", {}).get("nodes", {})
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        for node in nodes.values():
            if node.get("type") == "Pod":
                outputs = node.get("outputs", {})
                for artifact in outputs.get("artifacts", []):
                    name = artifact.get("name", "")
                    if "stdout" in name.lower():
                        stdout_parts.append(f"[{node.get('displayName', 'pod')}] {artifact}")
                    elif "stderr" in name.lower():
                        stderr_parts.append(f"[{node.get('displayName', 'pod')}] {artifact}")

        return "\n".join(stdout_parts), "\n".join(stderr_parts)


# ─────────────────────────────────────────────────────────────────────────────
# Local build runner (subprocess fallback)
# ─────────────────────────────────────────────────────────────────────────────


class LocalBuildRunner:
    """
    Subprocess-based build runner for local development and testing.

    Executes build commands directly in a subprocess without requiring a
    Kubernetes cluster.  Captures stdout/stderr and produces artifact
    descriptors for any files written to the artifacts directory.

    Usage::

        runner = LocalBuildRunner()
        result = await runner.run_build(spec)
    """

    def __init__(
        self,
        poll_interval_secs: float = 1.0,
        max_wait_secs: float = 3600.0,
    ) -> None:
        self.poll_interval_secs = poll_interval_secs
        self.max_wait_secs = max_wait_secs

    async def run_build(self, spec: BuildSpec) -> BuildResult:
        """
        Execute a build command in a subprocess.

        Args:
            spec: Build specification.

        Returns:
            BuildResult with captured output and artifact list.
        """
        started_at = time.time()
        logger.info(
            "Starting local build | build_id=%s command=%r",
            spec.build_id,
            spec.build_command,
        )

        # Create a temporary artifacts directory
        with tempfile.TemporaryDirectory(prefix=f"flowos-build-{spec.build_id}-") as tmpdir:
            artifacts_dir = Path(tmpdir) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            # Prepare environment
            env = {**os.environ, **spec.env_vars}
            env["FLOWOS_BUILD_ID"] = spec.build_id
            env["FLOWOS_BRANCH"] = spec.branch
            env["FLOWOS_ARTIFACTS_DIR"] = str(artifacts_dir)
            if spec.commit_sha:
                env["FLOWOS_COMMIT_SHA"] = spec.commit_sha

            try:
                # Run the build command in a subprocess
                proc = await asyncio.create_subprocess_shell(
                    spec.build_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=tmpdir,
                )

                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=min(spec.timeout_secs, self.max_wait_secs),
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    elapsed = time.time() - started_at
                    logger.warning(
                        "Local build timed out | build_id=%s elapsed=%.1fs",
                        spec.build_id,
                        elapsed,
                    )
                    return BuildResult(
                        build_id=spec.build_id,
                        status=BuildStatus.TIMEOUT,
                        exit_code=1,
                        error_message=f"Build timed out after {spec.timeout_secs}s",
                        duration_seconds=elapsed,
                        started_at=started_at,
                        finished_at=time.time(),
                    )

                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                exit_code = proc.returncode or 0
                finished_at = time.time()
                duration = finished_at - started_at

                # Collect artifacts from the artifacts directory
                artifacts = self._collect_artifacts(artifacts_dir, spec.build_id)

                if exit_code == 0:
                    status = BuildStatus.SUCCEEDED
                    error_message = ""
                    logger.info(
                        "Local build succeeded | build_id=%s duration=%.1fs artifacts=%d",
                        spec.build_id,
                        duration,
                        len(artifacts),
                    )
                else:
                    status = BuildStatus.FAILED
                    error_message = (
                        f"Build command exited with code {exit_code}. "
                        f"stderr: {stderr[:500]}"
                    )
                    logger.warning(
                        "Local build failed | build_id=%s exit_code=%d duration=%.1fs",
                        spec.build_id,
                        exit_code,
                        duration,
                    )

                return BuildResult(
                    build_id=spec.build_id,
                    status=status,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=duration,
                    artifacts=artifacts,
                    error_message=error_message,
                    started_at=started_at,
                    finished_at=finished_at,
                )

            except Exception as exc:
                elapsed = time.time() - started_at
                error_msg = f"Build runner error: {exc}"
                logger.error(
                    "Local build error | build_id=%s error=%s",
                    spec.build_id,
                    error_msg,
                )
                return BuildResult(
                    build_id=spec.build_id,
                    status=BuildStatus.ERROR,
                    exit_code=1,
                    error_message=error_msg,
                    duration_seconds=elapsed,
                    started_at=started_at,
                    finished_at=time.time(),
                )

    @staticmethod
    def _collect_artifacts(artifacts_dir: Path, build_id: str) -> list[dict[str, Any]]:
        """
        Scan the artifacts directory and return artifact descriptors.

        Args:
            artifacts_dir: Directory to scan for artifacts.
            build_id:      Build ID for artifact naming.

        Returns:
            List of artifact descriptor dicts with name, path, and size.
        """
        artifacts: list[dict[str, Any]] = []
        if not artifacts_dir.exists():
            return artifacts

        for artifact_path in sorted(artifacts_dir.rglob("*")):
            if artifact_path.is_file():
                rel_path = artifact_path.relative_to(artifacts_dir)
                artifacts.append(
                    {
                        "name": str(rel_path),
                        "local_path": str(artifact_path),
                        "size_bytes": artifact_path.stat().st_size,
                        "build_id": build_id,
                    }
                )

        return artifacts


# ─────────────────────────────────────────────────────────────────────────────
# Unified build runner
# ─────────────────────────────────────────────────────────────────────────────


class BuildRunner:
    """
    Unified build runner that selects the appropriate backend.

    Selects between ``ArgoClient`` (Kubernetes) and ``LocalBuildRunner``
    (subprocess) based on the ``BUILD_BACKEND`` environment variable or
    explicit constructor argument.

    Usage::

        runner = BuildRunner()  # auto-selects based on BUILD_BACKEND env var
        result = await runner.run_build(spec)

        # Force local mode
        runner = BuildRunner(backend="local")
        result = await runner.run_build(spec)
    """

    def __init__(self, backend: str | None = None) -> None:
        self.backend = backend or os.environ.get("BUILD_BACKEND", "local")
        if self.backend == "argo":
            self._runner: ArgoClient | LocalBuildRunner = ArgoClient()
            logger.info("BuildRunner using Argo Workflows backend")
        else:
            self._runner = LocalBuildRunner()
            logger.info("BuildRunner using local subprocess backend")

    async def run_build(self, spec: BuildSpec) -> BuildResult:
        """
        Execute a build job using the configured backend.

        Args:
            spec: Build specification.

        Returns:
            BuildResult with final status and output.
        """
        return await self._runner.run_build(spec)

    @property
    def is_argo(self) -> bool:
        """Return True if using the Argo Workflows backend."""
        return self.backend == "argo"

    @property
    def is_local(self) -> bool:
        """Return True if using the local subprocess backend."""
        return self.backend == "local"
