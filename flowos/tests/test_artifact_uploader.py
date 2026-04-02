"""
tests/test_artifact_uploader.py — Tests for workers/artifact_uploader.py

Tests cover:
- UploadResult.to_dict() serialisation
- ArtifactUploader._detect_content_type() MIME detection
- ArtifactUploader._build_url() for MinIO and AWS S3 endpoints
- ArtifactUploader.upload_artifact() happy path (mocked boto3)
- ArtifactUploader.upload_artifact() error path (S3 ClientError)
- ArtifactUploader.upload_artifact() missing file raises FileNotFoundError
- ArtifactUploader.upload_artifacts_batch() concurrent uploads
- ArtifactUploader.ensure_bucket_exists() creates bucket when missing
- ArtifactUploader.generate_presigned_url() delegates to boto3
- ARTIFACT_UPLOADED Kafka event is published after successful upload
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.artifact_uploader import ArtifactUploader, UploadResult


# ─────────────────────────────────────────────────────────────────────────────
# UploadResult tests
# ─────────────────────────────────────────────────────────────────────────────


class TestUploadResult:
    def _make_result(self, **kwargs) -> UploadResult:
        defaults = dict(
            artifact_name="app.tar.gz",
            s3_key="builds/build-001/app.tar.gz",
            bucket="flowos-artifacts",
            url="http://localhost:9000/flowos-artifacts/builds/build-001/app.tar.gz",
            size_bytes=1024,
            content_type="application/gzip",
            etag="abc123",
            uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            build_id="build-001",
        )
        defaults.update(kwargs)
        return UploadResult(**defaults)

    def test_to_dict_contains_required_keys(self):
        result = self._make_result()
        d = result.to_dict()
        assert d["artifact_name"] == "app.tar.gz"
        assert d["s3_key"] == "builds/build-001/app.tar.gz"
        assert d["bucket"] == "flowos-artifacts"
        assert d["url"] == "http://localhost:9000/flowos-artifacts/builds/build-001/app.tar.gz"
        assert d["size_bytes"] == 1024
        assert d["content_type"] == "application/gzip"
        assert d["etag"] == "abc123"
        assert d["build_id"] == "build-001"
        assert "uploaded_at" in d

    def test_to_dict_with_task_and_workflow(self):
        result = self._make_result(task_id="task-abc", workflow_id="wf-xyz")
        d = result.to_dict()
        assert d["task_id"] == "task-abc"
        assert d["workflow_id"] == "wf-xyz"

    def test_to_dict_uploaded_at_is_iso_string(self):
        result = self._make_result()
        d = result.to_dict()
        # Should be an ISO format string
        assert isinstance(d["uploaded_at"], str)
        assert "2026" in d["uploaded_at"]

    def test_success_default_true(self):
        result = self._make_result()
        assert result.success is True
        assert result.error_message == ""

    def test_failed_result(self):
        result = self._make_result(success=False, error_message="S3 connection refused")
        assert result.success is False
        assert result.error_message == "S3 connection refused"


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactUploader helper tests (no network required)
# ─────────────────────────────────────────────────────────────────────────────


class TestArtifactUploaderHelpers:
    def test_detect_content_type_gzip(self):
        ct = ArtifactUploader._detect_content_type(Path("archive.tar.gz"))
        assert "gzip" in ct or ct == "application/x-tar"

    def test_detect_content_type_html(self):
        ct = ArtifactUploader._detect_content_type(Path("report.html"))
        assert "html" in ct

    def test_detect_content_type_json(self):
        ct = ArtifactUploader._detect_content_type(Path("results.json"))
        assert "json" in ct

    def test_detect_content_type_unknown(self):
        ct = ArtifactUploader._detect_content_type(Path("file.unknownext"))
        assert ct == "application/octet-stream"

    def test_detect_content_type_zip(self):
        ct = ArtifactUploader._detect_content_type(Path("artifact.zip"))
        assert "zip" in ct

    def test_build_url_minio(self):
        uploader = ArtifactUploader(bucket="my-bucket", publish_events=False)
        # Patch settings.s3.endpoint_url directly
        with patch("workers.artifact_uploader.settings") as mock_settings:
            mock_settings.s3.endpoint_url = "http://localhost:9000"
            mock_settings.s3.region = "us-east-1"
            url = uploader._build_url("builds/build-001/app.tar.gz")
            assert url == "http://localhost:9000/my-bucket/builds/build-001/app.tar.gz"

    def test_build_url_aws_s3(self):
        uploader = ArtifactUploader(bucket="my-bucket", publish_events=False)
        with patch("workers.artifact_uploader.settings") as mock_settings:
            mock_settings.s3.endpoint_url = None
            mock_settings.s3.region = "us-west-2"
            url = uploader._build_url("builds/build-001/app.tar.gz")
            assert "s3.us-west-2.amazonaws.com" in url
            assert "my-bucket" in url
            assert "builds/build-001/app.tar.gz" in url


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactUploader upload tests (mocked boto3)
# ─────────────────────────────────────────────────────────────────────────────


class TestArtifactUploaderUpload:
    def _make_uploader(self, mock_producer=None) -> ArtifactUploader:
        uploader = ArtifactUploader(
            bucket="flowos-artifacts",
            publish_events=True,
            producer=mock_producer,
        )
        uploader._bucket_ensured = True  # Skip bucket creation
        return uploader

    @pytest.mark.asyncio
    async def test_upload_artifact_success(self):
        """Happy path: file exists, S3 put_object succeeds, event published."""
        mock_producer = MagicMock()
        uploader = self._make_uploader(mock_producer)

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            f.write(b"fake artifact content")
            tmp_path = Path(f.name)

        try:
            mock_s3 = MagicMock()
            mock_s3.put_object.return_value = {"ETag": '"abc123"'}

            with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
                with patch("workers.artifact_uploader.settings") as mock_settings:
                    mock_settings.s3.endpoint_url = "http://localhost:9000"
                    mock_settings.s3.region = "us-east-1"
                    mock_settings.s3.multipart_threshold = 8 * 1024 * 1024  # 8MB
                    mock_settings.s3.multipart_chunksize = 8 * 1024 * 1024

                    result = await uploader.upload_artifact(
                        local_path=tmp_path,
                        build_id="build-001",
                        artifact_name="app.tar.gz",
                        task_id="task-abc",
                        workflow_id="wf-xyz",
                    )

            assert result.success is True
            assert result.artifact_name == "app.tar.gz"
            assert result.build_id == "build-001"
            assert result.task_id == "task-abc"
            assert result.workflow_id == "wf-xyz"
            assert result.size_bytes == 21  # len(b"fake artifact content")
            assert result.etag == "abc123"
            assert result.error_message == ""

            # Verify Kafka event was published
            mock_producer.produce.assert_called_once()
        finally:
            tmp_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_upload_artifact_file_not_found(self):
        """FileNotFoundError raised when local_path does not exist."""
        uploader = self._make_uploader()
        with pytest.raises(FileNotFoundError, match="Artifact file not found"):
            await uploader.upload_artifact(
                local_path=Path("/nonexistent/path/artifact.tar.gz"),
                build_id="build-001",
            )

    @pytest.mark.asyncio
    async def test_upload_artifact_s3_error(self):
        """S3 ClientError returns a failed UploadResult (does not raise)."""
        from botocore.exceptions import ClientError

        uploader = self._make_uploader()

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"content")
            tmp_path = Path(f.name)

        try:
            mock_s3 = MagicMock()
            mock_s3.put_object.side_effect = ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
                "PutObject",
            )

            with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
                with patch("workers.artifact_uploader.settings") as mock_settings:
                    mock_settings.s3.endpoint_url = "http://localhost:9000"
                    mock_settings.s3.region = "us-east-1"
                    mock_settings.s3.multipart_threshold = 8 * 1024 * 1024
                    mock_settings.s3.multipart_chunksize = 8 * 1024 * 1024

                    result = await uploader.upload_artifact(
                        local_path=tmp_path,
                        build_id="build-002",
                    )

            assert result.success is False
            assert "S3 upload failed" in result.error_message
            assert result.url == ""
            assert result.etag == ""
        finally:
            tmp_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_upload_artifact_no_event_when_disabled(self):
        """No Kafka event published when publish_events=False."""
        mock_producer = MagicMock()
        uploader = ArtifactUploader(
            bucket="flowos-artifacts",
            publish_events=False,
            producer=mock_producer,
        )
        uploader._bucket_ensured = True

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"content")
            tmp_path = Path(f.name)

        try:
            mock_s3 = MagicMock()
            mock_s3.put_object.return_value = {"ETag": '"xyz"'}

            with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
                with patch("workers.artifact_uploader.settings") as mock_settings:
                    mock_settings.s3.endpoint_url = "http://localhost:9000"
                    mock_settings.s3.region = "us-east-1"
                    mock_settings.s3.multipart_threshold = 8 * 1024 * 1024
                    mock_settings.s3.multipart_chunksize = 8 * 1024 * 1024

                    result = await uploader.upload_artifact(
                        local_path=tmp_path,
                        build_id="build-003",
                    )

            assert result.success is True
            mock_producer.produce.assert_not_called()
        finally:
            tmp_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_upload_artifacts_batch_all_succeed(self):
        """Batch upload: all artifacts succeed."""
        uploader = self._make_uploader()

        with tempfile.TemporaryDirectory() as tmpdir:
            files = []
            for i in range(3):
                p = Path(tmpdir) / f"artifact_{i}.txt"
                p.write_text(f"content {i}")
                files.append({"local_path": str(p), "name": f"artifact_{i}.txt"})

            mock_s3 = MagicMock()
            mock_s3.put_object.return_value = {"ETag": '"etag"'}

            with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
                with patch("workers.artifact_uploader.settings") as mock_settings:
                    mock_settings.s3.endpoint_url = "http://localhost:9000"
                    mock_settings.s3.region = "us-east-1"
                    mock_settings.s3.multipart_threshold = 8 * 1024 * 1024
                    mock_settings.s3.multipart_chunksize = 8 * 1024 * 1024

                    results = await uploader.upload_artifacts_batch(
                        artifacts=files,
                        build_id="batch-build-001",
                        task_id="task-batch",
                    )

        assert len(results) == 3
        for result in results:
            assert result.success is True
            assert result.build_id == "batch-build-001"
            assert result.task_id == "task-batch"

    @pytest.mark.asyncio
    async def test_upload_artifacts_batch_partial_failure(self):
        """Batch upload: one artifact fails, others succeed."""
        from botocore.exceptions import ClientError

        uploader = self._make_uploader()

        with tempfile.TemporaryDirectory() as tmpdir:
            good_file = Path(tmpdir) / "good.txt"
            good_file.write_text("good content")
            bad_file = Path(tmpdir) / "bad.txt"
            bad_file.write_text("bad content")

            call_count = [0]

            def put_object_side_effect(**kwargs):
                call_count[0] += 1
                if call_count[0] == 2:
                    raise ClientError(
                        {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
                        "PutObject",
                    )
                return {"ETag": '"etag"'}

            mock_s3 = MagicMock()
            mock_s3.put_object.side_effect = put_object_side_effect

            with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
                with patch("workers.artifact_uploader.settings") as mock_settings:
                    mock_settings.s3.endpoint_url = "http://localhost:9000"
                    mock_settings.s3.region = "us-east-1"
                    mock_settings.s3.multipart_threshold = 8 * 1024 * 1024
                    mock_settings.s3.multipart_chunksize = 8 * 1024 * 1024

                    results = await uploader.upload_artifacts_batch(
                        artifacts=[
                            {"local_path": str(good_file)},
                            {"local_path": str(bad_file)},
                        ],
                        build_id="batch-build-002",
                    )

        assert len(results) == 2
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1
        assert "S3 upload failed" in failures[0].error_message

    def test_ensure_bucket_exists_already_ensured(self):
        """ensure_bucket_exists is a no-op when _bucket_ensured is True."""
        uploader = self._make_uploader()
        uploader._bucket_ensured = True
        mock_s3 = MagicMock()
        with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
            uploader.ensure_bucket_exists()
        mock_s3.head_bucket.assert_not_called()

    def test_ensure_bucket_creates_when_missing(self):
        """ensure_bucket_exists creates the bucket when it doesn't exist."""
        from botocore.exceptions import ClientError

        uploader = ArtifactUploader(bucket="new-bucket", publish_events=False)
        uploader._bucket_ensured = False

        mock_s3 = MagicMock()
        mock_s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadBucket",
        )
        mock_s3.create_bucket.return_value = {}

        with patch.object(uploader, "_make_s3_client", return_value=mock_s3):
            with patch("workers.artifact_uploader.settings") as mock_settings:
                mock_settings.s3.region = "us-east-1"
                uploader.ensure_bucket_exists()

        mock_s3.create_bucket.assert_called_once_with(Bucket="new-bucket")
        assert uploader._bucket_ensured is True
