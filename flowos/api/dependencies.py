"""
api/dependencies.py — FlowOS FastAPI Dependency Injection

Provides reusable FastAPI dependencies for:
- Database session management (async SQLAlchemy)
- Kafka producer access (singleton)
- Temporal client access
- Authentication / current-agent resolution
- Pagination helpers
- Request ID tracking

Usage in routers::

    from api.dependencies import get_db, get_producer, get_current_agent

    @router.get("/workflows")
    async def list_workflows(
        db: AsyncSession = Depends(get_db),
        producer: FlowOSProducer = Depends(get_producer),
        agent: AgentORM = Depends(get_current_agent),
    ):
        ...
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.db.session import get_async_db
from shared.kafka.producer import FlowOSProducer

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Database session dependency
# ─────────────────────────────────────────────────────────────────────────────


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async SQLAlchemy session.

    The session is committed on success and rolled back on exception.
    Always closed when the request completes.

    Usage::

        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async for session in get_async_db():
        yield session


# Type alias for cleaner route signatures
DBSession = Annotated[AsyncSession, Depends(get_db)]


# ─────────────────────────────────────────────────────────────────────────────
# Kafka producer dependency
# ─────────────────────────────────────────────────────────────────────────────


def get_producer(request: Request) -> FlowOSProducer:
    """
    FastAPI dependency that returns the application-level Kafka producer.

    The producer is stored on ``app.state.producer`` during startup and
    shared across all requests (thread-safe for produce() calls).

    Raises:
        HTTPException 503: If the producer is not available (startup failure).

    Use this dependency on endpoints that REQUIRE Kafka (write operations that
    must publish events).  For read-only endpoints, use ``get_optional_producer``
    which returns ``None`` instead of raising.
    """
    producer: FlowOSProducer | None = getattr(request.app.state, "producer", None)
    if producer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kafka producer is not available. The service may still be starting.",
        )
    return producer


def get_optional_producer(request: Request) -> FlowOSProducer | None:
    """
    FastAPI dependency that returns the application-level Kafka producer,
    or ``None`` if Kafka is unavailable.

    Unlike ``get_producer``, this dependency does NOT raise an exception when
    Kafka is unavailable.  Use this for endpoints where Kafka is optional
    (e.g. write endpoints that should still persist to DB even if Kafka is down).

    Returns:
        The Kafka producer instance, or ``None`` if unavailable.
    """
    return getattr(request.app.state, "producer", None)


# Type aliases
KafkaProducer = Annotated[FlowOSProducer, Depends(get_producer)]
OptionalKafkaProducer = Annotated[FlowOSProducer | None, Depends(get_optional_producer)]


# ─────────────────────────────────────────────────────────────────────────────
# Temporal client dependency
# ─────────────────────────────────────────────────────────────────────────────


async def get_temporal_client(request: Request):
    """
    FastAPI dependency that returns the application-level Temporal client.

    The client is stored on ``app.state.temporal_client`` during startup.

    Returns:
        The Temporal client, or None if Temporal is not available.
    """
    return getattr(request.app.state, "temporal_client", None)


# Type alias
TemporalClient = Annotated[object, Depends(get_temporal_client)]


# ─────────────────────────────────────────────────────────────────────────────
# Authentication / current agent
# ─────────────────────────────────────────────────────────────────────────────


async def get_current_agent_id(
    x_agent_id: Annotated[str | None, Header(alias="X-Agent-ID")] = None,
) -> str | None:
    """
    Extract the calling agent's ID from the ``X-Agent-ID`` request header.

    This is a lightweight identity mechanism for the FlowOS API.  In
    production, this would be replaced by JWT validation.

    Returns:
        The agent ID string, or None if the header is absent.
    """
    if x_agent_id is not None:
        # Validate it looks like a UUID
        try:
            uuid.UUID(x_agent_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"X-Agent-ID header must be a valid UUID, got: {x_agent_id!r}",
            )
    return x_agent_id


async def require_agent_id(
    agent_id: Annotated[str | None, Depends(get_current_agent_id)],
) -> str:
    """
    Like ``get_current_agent_id`` but raises 401 if the header is absent.

    Use this dependency on routes that require an authenticated agent.
    """
    if agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Agent-ID header is required for this endpoint.",
            headers={"WWW-Authenticate": "AgentID"},
        )
    return agent_id


# Type aliases
OptionalAgentID = Annotated[str | None, Depends(get_current_agent_id)]
RequiredAgentID = Annotated[str, Depends(require_agent_id)]


# ─────────────────────────────────────────────────────────────────────────────
# Pagination helpers
# ─────────────────────────────────────────────────────────────────────────────


class PaginationParams:
    """
    Standard pagination parameters for list endpoints.

    Attributes:
        limit:  Maximum number of records to return (1–500, default 50).
        offset: Number of records to skip (default 0).
    """

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=500, description="Maximum records to return.")] = 50,
        offset: Annotated[int, Query(ge=0, description="Number of records to skip.")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


Pagination = Annotated[PaginationParams, Depends(PaginationParams)]


# ─────────────────────────────────────────────────────────────────────────────
# Request ID tracking
# ─────────────────────────────────────────────────────────────────────────────


def get_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str:
    """
    Return the request ID from the ``X-Request-ID`` header, or generate one.

    The request ID is used for distributed tracing and log correlation.
    """
    return x_request_id or str(uuid.uuid4())


RequestID = Annotated[str, Depends(get_request_id)]
