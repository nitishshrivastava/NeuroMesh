"""
workers/artifact_uploader.py — FlowOS Artifact Uploader

Uploads build artifacts to S3-compatible object storage (MinIO in local dev,
AWS S3 in production) and publishes ``ARTIFACT_UPLOADED`` Kafka events for
each successfully uploaded artifact.

Architecture:
- ``ArtifactUploader`` wraps boto3 for S3/MinIO operations.
- Supports single-file and batch uploads.
- Automatically creates the target bucket if it does not exist.
- Publishes ``ARTIFACT_UPLOADED`` events to ``flowos.build.events`` after
  each successful upload so that consumers (UI, orchestrator, observability)
  receive real-time artifact availability notifications.
- Generates pre-signed download URLs valid for a configurable duration.

Configuration (via ``shared.config.settings.s3``):
    S3_ENDPOINT_URL         — MinIO/S3 endpoint (e.g. http://localhost:9000)
    S3_ACCESS_KEY_ID        — Access key ID
    S3_SECRET_ACCESS_KEY    — Secret access key
    S3_REGION               — Region (default: us-east-1)
    S3_BUCKET_ARTIFACTS     — Artifacts bucket name (default: flowos-artifacts)

Usage::

    uploader = ArtifactUploader()
    result = await uploader.upload_artifact(
        local_path=Path("/tmp/build/app.tar.gz"),
        build_id="build-123",
        artifact_name="app.tar.gz",
        task_id="task-456",
        workflow_id="wf-789",
    )
    print(result.url)  # https://minio:9000/flowos-artifacts/build-123/app.tar.gz
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.config import settings
from shared.kafka.producer import FlowOSProducer
from shared.kafka.schemas import ArtifactUploadedPayload, build_event
from shared.models.event import EventSource, EventType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class UploadResult:
    """
    Result of a single artifact upload operation.

    Attributes:
        artifact_name:   Artifact file name.
        s3_key:          S3 object key (path within the bucket).
        bucket:          S3 bucket name.
        url:             Public or pre-signed URL for downloading the artifact.
        size_bytes:      Artifact file size in bytes.
        content_type:    MIME type of the artifact.
        etag:            S3 ETag (MD5 hash of the object).
        uploaded_at:     UTC timestamp of the upload.
        build_id:        Build ID this artifact belongs to.
        task_id:         Task ID this artifact belongs to (optional).
        workflow_id:     Workflow ID this artifact belongs to (optional).
        success:         Whether the upload succeeded.
        error_message:   Error description if upload failed.
    """

    artifact_name: str
    s3_key: str
    bucket: str
    url: str
    size_bytes: int
    content_type: str
    etag: str
    uploaded_at: datetime
    build_id: str
    task_id: str | None = None
    workflow_id: str | None = None
    success: bool = True
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for embedding in Kafka payloads."""
        return {
            "artifact_name": self.artifact_name,
            "s3_key": self.s3_key,
            "bucket": self.bucket,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "etag": self.etag,
            "uploaded_at": self.uploaded_at.isoformat(),
            "build_id": self.build_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Artifact uploader
# ─────────────────────────────────────────────────────────────────────────────


class ArtifactUploader:
    """
    Uploads build artifacts to S3-compatible object storage.

    Wraps boto3 for S3/MinIO operations and publishes ``ARTIFACT_UPLOADED``
    Kafka events after each successful upload.

    Thread safety:
        boto3 clients are NOT thread-safe.  Each call to ``upload_artifact``
        creates a fresh boto3 client.  For high-throughput scenarios, use
        ``upload_artifacts_batch`` which runs uploads concurrently via
        ``asyncio.gather``.

    Usage::

        uploader = ArtifactUploader()
        result = await uploader.upload_artifact(
            local_path=Path("/tmp/app.tar.gz"),
            build_id="build-123",
            artifact_name="app.tar.gz",
        )
    """

    def __init__(
        self,
        bucket: str | None = None,
        presigned_url_expiry_secs: int = 3600,
        producer: FlowOSProducer | None = None,
        publish_events: bool = True,
    ) -> None:
        """
        Initialise the uploader.

        Args:
            bucket:                    Target S3 bucket name.  Defaults to
                                       ``settings.s3.bucket_artifacts``.
            presigned_url_expiry_secs: Expiry for pre-signed download URLs.
            producer:                  Kafka producer for event publishing.
                                       If None, a new producer is created.
            publish_events:            Whether to publish ARTIFACT_UPLOADED
                                       events after each upload.
        """
        self.bucket = bucket or settings.s3.bucket_artifacts
        self.presigned_url_expiry_secs = presigned_url_expiry_secs
        self.publish_events = publish_events
        self._producer = producer
        self._s3_config = settings.s3.as_boto3_config()
        self._bucket_ensured = False

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def upload_artifact(
        self,
        local_path: Path,
        build_id: str,
        artifact_name: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        s3_prefix: str | None = None,
        extra_metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """
        Upload a single artifact file to S3/MinIO.

        The artifact is stored at ``{s3_prefix}/{artifact_name}`` within the
        configured bucket.  If ``s3_prefix`` is not provided, it defaults to
        ``builds/{build_id}``.

        After a successful upload, an ``ARTIFACT_UPLOADED`` Kafka event is
        published to ``flowos.build.events``.

        Args:
            local_path:      Path to the local file to upload.
            build_id:        Build ID this artifact belongs to.
            artifact_name:   Name for the artifact (defaults to filename).
            task_id:         Optional task ID for event scoping.
            workflow_id:     Optional workflow ID for event scoping.
            s3_prefix:       S3 key prefix (defaults to ``builds/{build_id}``).
            extra_metadata:  Additional S3 object metadata.

        Returns:
            UploadResult with URL and metadata.

        Raises:
            FileNotFoundError: If ``local_path`` does not exist.
        """
        if not local_path.exists():
            raise FileNotFoundError(f"Artifact file not found: {local_path}")

        name = artifact_name or local_path.name
        prefix = s3_prefix or f"builds/{build_id}"
        s3_key = f"{prefix}/{name}"
        content_type = self._detect_content_type(local_path)
        size_bytes = local_path.stat().st_size

        logger.info(
            "Uploading artifact | build_id=%s name=%s size=%d bucket=%s key=%s",
            build_id,
            name,
            size_bytes,
            self.bucket,
            s3_key,
        )

        # Run the blocking boto3 upload in a thread pool
        try:
            etag, url = await asyncio.get_event_loop().run_in_executor(
                None,
                self._upload_sync,
                local_path,
                s3_key,
                content_type,
                size_bytes,
                extra_metadata or {},
            )
        except (BotoCoreError, ClientError) as exc:
            error_msg = f"S3 upload failed: {exc}"
            logger.error(
                "Artifact upload failed | build_id=%s name=%s error=%s",
                build_id,
                name,
                error_msg,
            )
            return UploadResult(
                artifact_name=name,
                s3_key=s3_key,
                bucket=self.bucket,
                url="",
                size_bytes=size_bytes,
                content_type=content_type,
                etag="",
                uploaded_at=datetime.now(timezone.utc),
                build_id=build_id,
                task_id=task_id,
                workflow_id=workflow_id,
                success=False,
                error_message=error_msg,
            )

        uploaded_at = datetime.now(timezone.utc)
        result = UploadResult(
            artifact_name=name,
            s3_key=s3_key,
            bucket=self.bucket,
            url=url,
            size_bytes=size_bytes,
            content_type=content_type,
            etag=etag,
            uploaded_at=uploaded_at,
            build_id=build_id,
            task_id=task_id,
            workflow_id=workflow_id,
            success=True,
        )

        logger.info(
            "Artifact uploaded | build_id=%s name=%s url=%s",
            build_id,
            name,
            url,
        )

        # Publish ARTIFACT_UPLOADED event
        if self.publish_events:
            self._publish_artifact_event(result)

        return result

    async def upload_artifacts_batch(
        self,
        artifacts: list[dict[str, Any]],
        build_id: str,
        task_id: str | None = None,
        workflow_id: str | None = None,
    ) -> list[UploadResult]:
        """
        Upload multiple artifacts concurrently.

        Each item in ``artifacts`` must have a ``local_path`` key (str or Path)
        and optionally ``name`` (artifact name override).

        Args:
            artifacts:   List of artifact descriptors.
            build_id:    Build ID for all artifacts.
            task_id:     Optional task ID.
            workflow_id: Optional workflow ID.

        Returns:
            List of UploadResult objects (one per artifact).
        """
        tasks = []
        for artifact in artifacts:
            local_path = Path(artifact["local_path"])
            name = artifact.get("name") or local_path.name
            tasks.append(
                self.upload_artifact(
                    local_path=local_path,
                    build_id=build_id,
                    artifact_name=name,
                    task_id=task_id,
                    workflow_id=workflow_id,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        upload_results: list[UploadResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                artifact = artifacts[i]
                local_path = Path(artifact["local_path"])
                name = artifact.get("name") or local_path.name
                logger.error(
                    "Batch upload error | build_id=%s artifact=%s error=%s",
                    build_id,
                    name,
                    result,
                )
                upload_results.append(
                    UploadResult(
                        artifact_name=name,
                        s3_key=f"builds/{build_id}/{name}",
                        bucket=self.bucket,
                        url="",
                        size_bytes=0,
                        content_type="application/octet-stream",
                        etag="",
                        uploaded_at=datetime.now(timezone.utc),
                        build_id=build_id,
                        task_id=task_id,
                        workflow_id=workflow_id,
                        success=False,
                        error_message=str(result),
                    )
                )
            else:
                upload_results.append(result)  # type: ignore[arg-type]

        return upload_results

    def ensure_bucket_exists(self) -> None:
        """
        Create the artifacts bucket if it does not already exist.

        This is called automatically before the first upload.  Safe to call
        multiple times — subsequent calls are no-ops.
        """
        if self._bucket_ensured:
            return

        s3 = self._make_s3_client()
        try:
            s3.head_bucket(Bucket=self.bucket)
            logger.debug("Bucket already exists | bucket=%s", self.bucket)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                logger.info("Creating bucket | bucket=%s", self.bucket)
                try:
                    if settings.s3.region == "us-east-1":
                        s3.create_bucket(Bucket=self.bucket)
                    else:
                        s3.create_bucket(
                            Bucket=self.bucket,
                            CreateBucketConfiguration={
                                "LocationConstraint": settings.s3.region
                            },
                        )
                    logger.info("Bucket created | bucket=%s", self.bucket)
                except ClientError as create_exc:
                    logger.warning(
                        "Could not create bucket | bucket=%s error=%s",
                        self.bucket,
                        create_exc,
                    )
            else:
                logger.warning(
                    "Bucket check failed | bucket=%s error=%s",
                    self.bucket,
                    exc,
                )

        self._bucket_ensured = True

    def generate_presigned_url(
        self,
        s3_key: str,
        expiry_secs: int | None = None,
    ) -> str:
        """
        Generate a pre-signed download URL for an S3 object.

        Args:
            s3_key:      S3 object key.
            expiry_secs: URL expiry in seconds (defaults to
                         ``presigned_url_expiry_secs``).

        Returns:
            Pre-signed URL string.
        """
        s3 = self._make_s3_client()
        expiry = expiry_secs or self.presigned_url_expiry_secs
        url: str = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": s3_key},
            ExpiresIn=expiry,
        )
        return url

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _upload_sync(
        self,
        local_path: Path,
        s3_key: str,
        content_type: str,
        size_bytes: int,
        extra_metadata: dict[str, str],
    ) -> tuple[str, str]:
        """
        Synchronous S3 upload (runs in a thread pool executor).

        Returns:
            Tuple of (etag, url).
        """
        self.ensure_bucket_exists()
        s3 = self._make_s3_client()

        metadata = {
            "uploaded-by": "flowos-machine-worker",
            **extra_metadata,
        }

        # Use multipart for large files
        if size_bytes >= settings.s3.multipart_threshold:
            from boto3.s3.transfer import TransferConfig

            config = TransferConfig(
                multipart_threshold=settings.s3.multipart_threshold,
                multipart_chunksize=settings.s3.multipart_chunksize,
            )
            s3.upload_file(
                str(local_path),
                self.bucket,
                s3_key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": metadata,
                },
                Config=config,
            )
        else:
            with open(local_path, "rb") as f:
                response = s3.put_object(
                    Bucket=self.bucket,
                    Key=s3_key,
                    Body=f,
                    ContentType=content_type,
                    Metadata=metadata,
                )
            etag = response.get("ETag", "").strip('"')
            url = self._build_url(s3_key)
            return etag, url

        # For multipart uploads, fetch the ETag via head_object
        head = s3.head_object(Bucket=self.bucket, Key=s3_key)
        etag = head.get("ETag", "").strip('"')
        url = self._build_url(s3_key)
        return etag, url

    def _build_url(self, s3_key: str) -> str:
        """Build the public or endpoint URL for an S3 object."""
        endpoint = settings.s3.endpoint_url
        if endpoint:
            # MinIO-style URL: http://localhost:9000/bucket/key
            return f"{endpoint.rstrip('/')}/{self.bucket}/{s3_key}"
        # AWS S3 URL
        region = settings.s3.region
        return (
            f"https://{self.bucket}.s3.{region}.amazonaws.com/{s3_key}"
        )

    def _make_s3_client(self) -> Any:
        """Create a new boto3 S3 client from current settings."""
        return boto3.client("s3", **self._s3_config)

    def _publish_artifact_event(self, result: UploadResult) -> None:
        """Publish an ARTIFACT_UPLOADED Kafka event."""
        try:
            producer = self._get_producer()
            payload = ArtifactUploadedPayload(
                build_id=result.build_id,
                artifact_name=result.artifact_name,
                artifact_url=result.url,
                artifact_size_bytes=result.size_bytes,
                content_type=result.content_type,
                uploaded_at=result.uploaded_at,
            )
            event = build_event(
                event_type=EventType.ARTIFACT_UPLOADED,
                payload=payload,
                source=EventSource.BUILD_RUNNER,
                task_id=result.task_id,
                workflow_id=result.workflow_id,
            )
            producer.produce(event)
            logger.debug(
                "Published ARTIFACT_UPLOADED event | build_id=%s artifact=%s",
                result.build_id,
                result.artifact_name,
            )
        except Exception as exc:
            # Event publishing failure must not fail the upload
            logger.warning(
                "Failed to publish ARTIFACT_UPLOADED event | build_id=%s error=%s",
                result.build_id,
                exc,
            )

    def _get_producer(self) -> FlowOSProducer:
        """Return the Kafka producer, creating one lazily if needed."""
        if self._producer is None:
            self._producer = FlowOSProducer()
        return self._producer

    @staticmethod
    def _detect_content_type(path: Path) -> str:
        """Detect the MIME type of a file by its extension."""
        mime_type, _ = mimetypes.guess_type(str(path))
        return mime_type or "application/octet-stream"
