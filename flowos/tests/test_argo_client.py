"""
tests/test_argo_client.py — Tests for workers/argo_client.py

Tests cover:
- BuildSpec dataclass construction and defaults
- BuildResult properties (succeeded, failed)
- LocalBuildRunner: successful build, failed build, timeout, artifact collection
- BuildRunner backend selection
- ArgoClient._make_workflow_name sanitisation
- ArgoClient._build_workflow_manifest structure
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the flowos package root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.argo_client import (
    ArgoClient,
    BuildResult,
    BuildRunner,
    BuildSpec,
    BuildStatus,
    LocalBuildRunner,
)


# ─────────────────────────────────────────────────────────────────────────────
# BuildSpec tests
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildSpec:
    def test_defaults(self):
        spec = BuildSpec(build_id="build-001")
        assert spec.build_id == "build-001"
        assert spec.branch == "main"
        assert spec.image == "alpine:3.19"
        assert spec.timeout_secs == 3600
        assert spec.task_id is None
        assert spec.workflow_id is None
        assert spec.env_vars == {}
        assert spec.labels == {}

    def test_custom_fields(self):
        spec = BuildSpec(
            build_id="build-002",
            task_id="task-abc",
            workflow_id="wf-xyz",
            repository="https://github.com/org/repo",
            branch="feature/my-feature",
            commit_sha="abc123",
            build_command="make test",
            image="python:3.12-slim",
            timeout_secs=600,
            env_vars={"MY_VAR": "value"},
            labels={"team": "platform"},
        )
        assert spec.task_id == "task-abc"
        assert spec.workflow_id == "wf-xyz"
        assert spec.branch == "feature/my-feature"
        assert spec.commit_sha == "abc123"
        assert spec.build_command == "make test"
        assert spec.image == "python:3.12-slim"
        assert spec.timeout_secs == 600
        assert spec.env_vars == {"MY_VAR": "value"}
        assert spec.labels == {"team": "platform"}


# ─────────────────────────────────────────────────────────────────────────────
# BuildResult tests
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildResult:
    def test_succeeded_property(self):
        result = BuildResult(build_id="b1", status=BuildStatus.SUCCEEDED)
        assert result.succeeded is True
        assert result.failed is False

    def test_failed_property(self):
        result = BuildResult(build_id="b1", status=BuildStatus.FAILED)
        assert result.succeeded is False
        assert result.failed is True

    def test_error_property(self):
        result = BuildResult(build_id="b1", status=BuildStatus.ERROR)
        assert result.failed is True

    def test_timeout_property(self):
        result = BuildResult(build_id="b1", status=BuildStatus.TIMEOUT)
        assert result.failed is True

    def test_running_not_succeeded_or_failed(self):
        result = BuildResult(build_id="b1", status=BuildStatus.RUNNING)
        assert result.succeeded is False
        assert result.failed is False

    def test_default_artifacts(self):
        result = BuildResult(build_id="b1", status=BuildStatus.SUCCEEDED)
        assert result.artifacts == []

    def test_with_artifacts(self):
        artifacts = [{"name": "app.tar.gz", "url": "http://minio/artifacts/app.tar.gz"}]
        result = BuildResult(
            build_id="b1",
            status=BuildStatus.SUCCEEDED,
            artifacts=artifacts,
        )
        assert len(result.artifacts) == 1
        assert result.artifacts[0]["name"] == "app.tar.gz"


# ─────────────────────────────────────────────────────────────────────────────
# LocalBuildRunner tests
# ─────────────────────────────────────────────────────────────────────────────


class TestLocalBuildRunner:
    @pytest.mark.asyncio
    async def test_successful_build(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-001",
            build_command="echo 'hello from build'",
        )
        result = await runner.run_build(spec)

        assert result.build_id == "test-build-001"
        assert result.status == BuildStatus.SUCCEEDED
        assert result.succeeded is True
        assert result.exit_code == 0
        assert "hello from build" in result.stdout
        assert result.duration_seconds >= 0.0
        assert result.error_message == ""

    @pytest.mark.asyncio
    async def test_failed_build(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-002",
            build_command="exit 1",
        )
        result = await runner.run_build(spec)

        assert result.build_id == "test-build-002"
        assert result.status == BuildStatus.FAILED
        assert result.failed is True
        assert result.exit_code == 1
        assert result.error_message != ""
        assert "1" in result.error_message  # exit code mentioned

    @pytest.mark.asyncio
    async def test_build_with_stderr(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-003",
            build_command="echo 'error output' >&2; exit 2",
        )
        result = await runner.run_build(spec)

        assert result.status == BuildStatus.FAILED
        assert result.exit_code == 2
        assert "error output" in result.stderr

    @pytest.mark.asyncio
    async def test_build_timeout(self):
        runner = LocalBuildRunner(max_wait_secs=60.0)
        spec = BuildSpec(
            build_id="test-build-timeout",
            build_command="sleep 60",
            timeout_secs=1,  # 1 second timeout
        )
        result = await runner.run_build(spec)

        assert result.status == BuildStatus.TIMEOUT
        assert result.failed is True
        assert "timed out" in result.error_message.lower()
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_build_with_artifacts(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-artifacts",
            # Write a file to the artifacts directory
            build_command=(
                'mkdir -p "$FLOWOS_ARTIFACTS_DIR" && '
                'echo "artifact content" > "$FLOWOS_ARTIFACTS_DIR/output.txt"'
            ),
        )
        result = await runner.run_build(spec)

        assert result.status == BuildStatus.SUCCEEDED
        # Artifacts are collected from the temp dir — but since we use a
        # TemporaryDirectory context manager, the files are cleaned up after
        # the build. The artifacts list captures them before cleanup.
        assert isinstance(result.artifacts, list)

    @pytest.mark.asyncio
    async def test_build_env_vars_injected(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-env",
            build_command="echo $FLOWOS_BUILD_ID",
        )
        result = await runner.run_build(spec)

        assert result.status == BuildStatus.SUCCEEDED
        assert "test-build-env" in result.stdout

    @pytest.mark.asyncio
    async def test_build_custom_env_vars(self):
        runner = LocalBuildRunner()
        spec = BuildSpec(
            build_id="test-build-custom-env",
            build_command="echo $MY_CUSTOM_VAR",
            env_vars={"MY_CUSTOM_VAR": "hello-custom"},
        )
        result = await runner.run_build(spec)

        assert result.status == BuildStatus.SUCCEEDED
        assert "hello-custom" in result.stdout

    @pytest.mark.asyncio
    async def test_collect_artifacts_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir) / "artifacts"
            artifacts_dir.mkdir()
            artifacts = LocalBuildRunner._collect_artifacts(artifacts_dir, "build-x")
            assert artifacts == []

    @pytest.mark.asyncio
    async def test_collect_artifacts_with_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir) / "artifacts"
            artifacts_dir.mkdir()
            (artifacts_dir / "app.tar.gz").write_bytes(b"fake tarball content")
            (artifacts_dir / "report.html").write_text("<html>report</html>")

            artifacts = LocalBuildRunner._collect_artifacts(artifacts_dir, "build-y")
            assert len(artifacts) == 2
            names = {a["name"] for a in artifacts}
            assert "app.tar.gz" in names
            assert "report.html" in names
            for a in artifacts:
                assert a["build_id"] == "build-y"
                assert a["size_bytes"] > 0
                assert "local_path" in a

    @pytest.mark.asyncio
    async def test_collect_artifacts_nonexistent_dir(self):
        artifacts = LocalBuildRunner._collect_artifacts(
            Path("/nonexistent/path"), "build-z"
        )
        assert artifacts == []


# ─────────────────────────────────────────────────────────────────────────────
# ArgoClient helper tests (no network required)
# ─────────────────────────────────────────────────────────────────────────────


class TestArgoClientHelpers:
    def test_make_workflow_name_basic(self):
        name = ArgoClient._make_workflow_name("build-abc-123")
        assert name.startswith("flowos-build-")
        assert "build-abc-123" in name
        assert len(name) <= 253

    def test_make_workflow_name_uppercase(self):
        name = ArgoClient._make_workflow_name("BUILD-ABC")
        assert name == name.lower()

    def test_make_workflow_name_underscores(self):
        name = ArgoClient._make_workflow_name("build_with_underscores")
        assert "_" not in name
        assert "-" in name

    def test_make_workflow_name_long_id(self):
        long_id = "a" * 300
        name = ArgoClient._make_workflow_name(long_id)
        assert len(name) <= 253

    def test_build_workflow_manifest_structure(self):
        client = ArgoClient()
        spec = BuildSpec(
            build_id="build-manifest-test",
            task_id="task-001",
            workflow_id="wf-001",
            repository="https://github.com/org/repo",
            branch="main",
            build_command="make build",
            image="python:3.12-slim",
            timeout_secs=600,
            env_vars={"MY_VAR": "val"},
        )
        manifest = client._build_workflow_manifest(spec, "flowos-build-test")

        assert manifest["apiVersion"] == "argoproj.io/v1alpha1"
        assert manifest["kind"] == "Workflow"
        assert manifest["metadata"]["name"] == "flowos-build-test"
        assert manifest["metadata"]["namespace"] == client.namespace

        labels = manifest["metadata"]["labels"]
        assert labels["app"] == "flowos"
        assert "flowos/build-id" in labels
        assert "flowos/task-id" in labels
        assert "flowos/workflow-id" in labels

        spec_section = manifest["spec"]
        assert spec_section["activeDeadlineSeconds"] == 600
        assert spec_section["entrypoint"] == "build"
        assert len(spec_section["templates"]) == 1

        container = spec_section["templates"][0]["container"]
        assert container["image"] == "python:3.12-slim"
        assert container["args"] == ["make build"]

        # Check env vars are injected
        env_names = {e["name"] for e in container["env"]}
        assert "MY_VAR" in env_names
        assert "FLOWOS_BUILD_ID" in env_names
        assert "FLOWOS_BRANCH" in env_names

    def test_build_workflow_manifest_no_task_or_workflow(self):
        client = ArgoClient()
        spec = BuildSpec(build_id="build-no-scope")
        manifest = client._build_workflow_manifest(spec, "flowos-build-no-scope")

        labels = manifest["metadata"]["labels"]
        assert "flowos/task-id" not in labels
        assert "flowos/workflow-id" not in labels

    def test_extract_logs_empty(self):
        stdout, stderr = ArgoClient._extract_logs({})
        assert stdout == ""
        assert stderr == ""

    def test_extract_logs_no_nodes(self):
        wf_data = {"status": {"phase": "Succeeded"}}
        stdout, stderr = ArgoClient._extract_logs(wf_data)
        assert stdout == ""
        assert stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# BuildRunner backend selection tests
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildRunner:
    def test_default_backend_is_local(self):
        # Ensure BUILD_BACKEND env var doesn't interfere
        with patch.dict(os.environ, {"BUILD_BACKEND": "local"}):
            runner = BuildRunner()
            assert runner.is_local is True
            assert runner.is_argo is False

    def test_argo_backend_selection(self):
        runner = BuildRunner(backend="argo")
        assert runner.is_argo is True
        assert runner.is_local is False
        assert isinstance(runner._runner, ArgoClient)

    def test_local_backend_selection(self):
        runner = BuildRunner(backend="local")
        assert runner.is_local is True
        assert isinstance(runner._runner, LocalBuildRunner)

    def test_env_var_backend_selection(self):
        with patch.dict(os.environ, {"BUILD_BACKEND": "argo"}):
            runner = BuildRunner()
            assert runner.is_argo is True

    @pytest.mark.asyncio
    async def test_local_runner_executes_build(self):
        runner = BuildRunner(backend="local")
        spec = BuildSpec(
            build_id="runner-test-001",
            build_command="echo 'runner test'",
        )
        result = await runner.run_build(spec)
        assert result.succeeded is True
        assert "runner test" in result.stdout

    @pytest.mark.asyncio
    async def test_local_runner_failed_build(self):
        runner = BuildRunner(backend="local")
        spec = BuildSpec(
            build_id="runner-test-fail",
            build_command="false",  # always exits 1
        )
        result = await runner.run_build(spec)
        assert result.failed is True
        assert result.exit_code == 1
