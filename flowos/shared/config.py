"""
shared/config.py — FlowOS Centralised Configuration

All settings are read from environment variables (or a .env file) using
pydantic-settings.  Import the singleton ``settings`` object anywhere in the
codebase:

    from shared.config import settings

    print(settings.kafka.bootstrap_servers)
    print(settings.database.url)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ─────────────────────────────────────────────────────────────────────────────
# Kafka Settings
# ─────────────────────────────────────────────────────────────────────────────


class KafkaSettings(BaseSettings):
    """Configuration for the Apache Kafka event bus."""

    model_config = SettingsConfigDict(
        env_prefix="KAFKA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bootstrap_servers: str = Field(
        default="localhost:9092",
        description="Comma-separated list of Kafka broker addresses.",
    )
    security_protocol: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = Field(
        default="PLAINTEXT",
        description="Security protocol used to communicate with Kafka brokers.",
    )
    sasl_mechanism: str | None = Field(
        default=None,
        description="SASL mechanism (e.g. PLAIN, SCRAM-SHA-256). Required when using SASL.",
    )
    sasl_username: str | None = Field(
        default=None,
        description="SASL username for broker authentication.",
    )
    sasl_password: str | None = Field(
        default=None,
        description="SASL password for broker authentication.",
    )
    ssl_ca_location: str | None = Field(
        default=None,
        description="Path to CA certificate file for SSL verification.",
    )
    ssl_certificate_location: str | None = Field(
        default=None,
        description="Path to client certificate file for mTLS.",
    )
    ssl_key_location: str | None = Field(
        default=None,
        description="Path to client private key file for mTLS.",
    )

    # Producer defaults
    producer_acks: Literal["0", "1", "all"] = Field(
        default="all",
        description="Number of acknowledgements the producer requires.",
    )
    producer_retries: int = Field(
        default=5,
        ge=0,
        description="Number of times to retry a failed produce request.",
    )
    producer_retry_backoff_ms: int = Field(
        default=500,
        ge=0,
        description="Backoff time in milliseconds between retries.",
    )
    producer_max_block_ms: int = Field(
        default=60_000,
        ge=0,
        description="Maximum time to block on send() when the buffer is full.",
    )
    producer_compression_type: Literal["none", "gzip", "snappy", "lz4", "zstd"] = Field(
        default="snappy",
        description="Compression codec for produced messages.",
    )

    # Consumer defaults
    consumer_group_id: str = Field(
        default="flowos-default",
        description="Default consumer group ID.",
    )
    consumer_auto_offset_reset: Literal["earliest", "latest", "error"] = Field(
        default="earliest",
        description="What to do when there is no initial offset in Kafka.",
    )
    consumer_enable_auto_commit: bool = Field(
        default=False,
        description="If true, the consumer's offset will be periodically committed.",
    )
    consumer_max_poll_interval_ms: int = Field(
        default=300_000,
        ge=1,
        description="Maximum delay between invocations of poll() in milliseconds.",
    )
    consumer_session_timeout_ms: int = Field(
        default=30_000,
        ge=1,
        description="Timeout used to detect consumer failures.",
    )

    # Admin defaults
    admin_request_timeout_ms: int = Field(
        default=30_000,
        ge=1,
        description="Timeout for admin client requests in milliseconds.",
    )

    @field_validator("bootstrap_servers")
    @classmethod
    def validate_bootstrap_servers(cls, v: str) -> str:
        """Ensure at least one broker address is provided."""
        servers = [s.strip() for s in v.split(",") if s.strip()]
        if not servers:
            raise ValueError("KAFKA_BOOTSTRAP_SERVERS must contain at least one broker address.")
        return ",".join(servers)

    def as_producer_config(self) -> dict[str, object]:
        """Return a confluent-kafka producer configuration dict."""
        cfg: dict[str, object] = {
            "bootstrap.servers": self.bootstrap_servers,
            "security.protocol": self.security_protocol,
            "acks": self.producer_acks,
            "retries": self.producer_retries,
            "retry.backoff.ms": self.producer_retry_backoff_ms,
            "max.block.ms": self.producer_max_block_ms,
            "compression.type": self.producer_compression_type,
        }
        self._apply_sasl(cfg)
        self._apply_ssl(cfg)
        return cfg

    def as_consumer_config(
        self,
        group_id: str | None = None,
    ) -> dict[str, object]:
        """Return a confluent-kafka consumer configuration dict."""
        cfg: dict[str, object] = {
            "bootstrap.servers": self.bootstrap_servers,
            "security.protocol": self.security_protocol,
            "group.id": group_id or self.consumer_group_id,
            "auto.offset.reset": self.consumer_auto_offset_reset,
            "enable.auto.commit": self.consumer_enable_auto_commit,
            "max.poll.interval.ms": self.consumer_max_poll_interval_ms,
            "session.timeout.ms": self.consumer_session_timeout_ms,
        }
        self._apply_sasl(cfg)
        self._apply_ssl(cfg)
        return cfg

    def as_admin_config(self) -> dict[str, object]:
        """Return a confluent-kafka admin client configuration dict."""
        cfg: dict[str, object] = {
            "bootstrap.servers": self.bootstrap_servers,
            "security.protocol": self.security_protocol,
        }
        self._apply_sasl(cfg)
        self._apply_ssl(cfg)
        return cfg

    def _apply_sasl(self, cfg: dict[str, object]) -> None:
        if self.sasl_mechanism:
            cfg["sasl.mechanism"] = self.sasl_mechanism
        if self.sasl_username:
            cfg["sasl.username"] = self.sasl_username
        if self.sasl_password:
            cfg["sasl.password"] = self.sasl_password

    def _apply_ssl(self, cfg: dict[str, object]) -> None:
        if self.ssl_ca_location:
            cfg["ssl.ca.location"] = self.ssl_ca_location
        if self.ssl_certificate_location:
            cfg["ssl.certificate.location"] = self.ssl_certificate_location
        if self.ssl_key_location:
            cfg["ssl.key.location"] = self.ssl_key_location


# ─────────────────────────────────────────────────────────────────────────────
# Temporal Settings
# ─────────────────────────────────────────────────────────────────────────────


class TemporalSettings(BaseSettings):
    """Configuration for the Temporal durable workflow orchestration service."""

    model_config = SettingsConfigDict(
        env_prefix="TEMPORAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(
        default="localhost",
        description="Temporal server hostname.",
    )
    port: int = Field(
        default=7233,
        ge=1,
        le=65535,
        description="Temporal gRPC frontend port.",
    )
    namespace: str = Field(
        default="default",
        description="Temporal namespace to use for all workflows.",
    )
    task_queue: str = Field(
        default="flowos-main",
        description="Default Temporal task queue name.",
    )

    # TLS / mTLS
    tls_enabled: bool = Field(
        default=False,
        description="Enable TLS for the Temporal gRPC connection.",
    )
    tls_cert_path: str | None = Field(
        default=None,
        description="Path to the client TLS certificate file.",
    )
    tls_key_path: str | None = Field(
        default=None,
        description="Path to the client TLS private key file.",
    )
    tls_ca_path: str | None = Field(
        default=None,
        description="Path to the CA certificate file for server verification.",
    )

    # Worker tuning
    max_concurrent_activities: int = Field(
        default=100,
        ge=1,
        description="Maximum number of concurrent activity task executions per worker.",
    )
    max_concurrent_workflows: int = Field(
        default=500,
        ge=1,
        description="Maximum number of concurrent workflow task executions per worker.",
    )

    @property
    def address(self) -> str:
        """Return the full Temporal server address (host:port)."""
        return f"{self.host}:{self.port}"


# ─────────────────────────────────────────────────────────────────────────────
# Database Settings
# ─────────────────────────────────────────────────────────────────────────────


class DatabaseSettings(BaseSettings):
    """Configuration for the PostgreSQL metadata store."""

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = Field(
        default="postgresql+asyncpg://flowos:flowos_secret@localhost:5432/flowos",
        description=(
            "Async SQLAlchemy database URL. "
            "Use postgresql+asyncpg:// for async operations."
        ),
    )
    sync_url: str = Field(
        default="postgresql://flowos:flowos_secret@localhost:5432/flowos",
        description=(
            "Synchronous SQLAlchemy database URL. "
            "Used by Alembic migrations and sync contexts."
        ),
    )

    # Connection pool settings
    pool_size: int = Field(
        default=10,
        ge=1,
        description="Number of connections to maintain in the pool.",
    )
    max_overflow: int = Field(
        default=20,
        ge=0,
        description="Maximum number of connections to create above pool_size.",
    )
    pool_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds to wait before giving up on getting a connection from the pool.",
    )
    pool_recycle: int = Field(
        default=1800,
        ge=-1,
        description="Seconds after which a connection is recycled. -1 disables recycling.",
    )
    pool_pre_ping: bool = Field(
        default=True,
        description="If True, test connections for liveness before using them.",
    )
    echo: bool = Field(
        default=False,
        description="If True, SQLAlchemy will log all SQL statements (verbose).",
    )

    @field_validator("url", "sync_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Ensure the URL starts with a recognised PostgreSQL scheme."""
        valid_schemes = (
            "postgresql://",
            "postgresql+asyncpg://",
            "postgresql+psycopg2://",
            "postgresql+psycopg://",
        )
        if not any(v.startswith(scheme) for scheme in valid_schemes):
            raise ValueError(
                f"DATABASE_URL must start with one of: {valid_schemes}. Got: {v!r}"
            )
        return v


# ─────────────────────────────────────────────────────────────────────────────
# S3 / Object Storage Settings
# ─────────────────────────────────────────────────────────────────────────────


class S3Settings(BaseSettings):
    """Configuration for S3-compatible object storage (AWS S3 or MinIO)."""

    model_config = SettingsConfigDict(
        env_prefix="S3_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    endpoint_url: str | None = Field(
        default=None,
        description=(
            "Custom S3 endpoint URL. Set to MinIO address for local dev "
            "(e.g. http://localhost:9000). Leave None for AWS S3."
        ),
    )
    access_key_id: str = Field(
        default="",
        description="AWS access key ID or MinIO root user.",
    )
    secret_access_key: str = Field(
        default="",
        description="AWS secret access key or MinIO root password.",
    )
    region: str = Field(
        default="us-east-1",
        description="AWS region or MinIO region.",
    )

    # Bucket names
    bucket_artifacts: str = Field(
        default="flowos-artifacts",
        description="Bucket for build artifacts and generated outputs.",
    )
    bucket_snapshots: str = Field(
        default="flowos-snapshots",
        description="Bucket for workspace snapshots.",
    )
    bucket_checkpoints: str = Field(
        default="flowos-checkpoints",
        description="Bucket for checkpoint metadata and associated files.",
    )
    bucket_logs: str = Field(
        default="flowos-logs",
        description="Bucket for execution logs.",
    )

    # Upload settings
    multipart_threshold_mb: int = Field(
        default=8,
        ge=5,
        description="File size threshold in MB above which multipart upload is used.",
    )
    multipart_chunksize_mb: int = Field(
        default=8,
        ge=5,
        description="Chunk size in MB for multipart uploads.",
    )

    @property
    def multipart_threshold(self) -> int:
        """Return multipart threshold in bytes."""
        return self.multipart_threshold_mb * 1024 * 1024

    @property
    def multipart_chunksize(self) -> int:
        """Return multipart chunk size in bytes."""
        return self.multipart_chunksize_mb * 1024 * 1024

    def as_boto3_config(self) -> dict[str, object]:
        """Return a dict suitable for boto3.client() / boto3.resource() kwargs."""
        cfg: dict[str, object] = {
            "region_name": self.region,
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.endpoint_url:
            cfg["endpoint_url"] = self.endpoint_url
        return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Application Settings
# ─────────────────────────────────────────────────────────────────────────────


class AppSettings(BaseSettings):
    """Top-level application settings that compose all sub-settings."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application identity
    name: str = Field(
        default="FlowOS",
        description="Human-readable application name.",
    )
    version: str = Field(
        default="0.1.0",
        description="Application version string.",
    )
    env: Literal["development", "staging", "production", "test"] = Field(
        default="development",
        description="Deployment environment.",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug mode (verbose logging, stack traces, etc.).",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Root log level.",
    )

    # API server
    api_host: str = Field(
        default="0.0.0.0",
        description="Host address for the FastAPI server.",
    )
    api_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Port for the FastAPI server.",
    )
    api_workers: int = Field(
        default=1,
        ge=1,
        description="Number of uvicorn worker processes.",
    )
    api_cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"],
        description="Allowed CORS origins for the API.",
    )

    # Security
    secret_key: str = Field(
        default="change-me-in-production-use-a-long-random-string",
        description="Secret key used for JWT signing and other cryptographic operations.",
    )
    access_token_expire_minutes: int = Field(
        default=60,
        ge=1,
        description="JWT access token expiry in minutes.",
    )
    refresh_token_expire_days: int = Field(
        default=30,
        ge=1,
        description="JWT refresh token expiry in days.",
    )

    # Workspace
    workspace_root: str = Field(
        default="/flowos/workspaces",
        description="Root directory for all agent workspaces.",
    )

    # Sub-settings (composed from their own env prefixes)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    temporal: TemporalSettings = Field(default_factory=TemporalSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    s3: S3Settings = Field(default_factory=S3Settings)

    @model_validator(mode="after")
    def warn_insecure_secret_key(self) -> "AppSettings":
        """Warn if the default secret key is used in a non-development environment."""
        if (
            self.env != "development"
            and self.secret_key == "change-me-in-production-use-a-long-random-string"
        ):
            import warnings

            warnings.warn(
                "APP_SECRET_KEY is set to the default insecure value. "
                "Set a strong random secret key for non-development environments.",
                stacklevel=2,
            )
        return self

    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_test(self) -> bool:
        return self.env == "test"


# ─────────────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """
    Return the cached application settings singleton.

    The first call reads from environment variables / .env file.
    Subsequent calls return the cached instance.

    To force a reload (e.g. in tests), call ``get_settings.cache_clear()``
    before calling ``get_settings()`` again.
    """
    return AppSettings()


# Convenience alias — use ``from shared.config import settings`` everywhere.
settings: AppSettings = get_settings()
