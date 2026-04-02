"""
api/routers/events.py — FlowOS Event REST Endpoints

Provides read-only access to the Kafka event audit log stored in the database.

Endpoints:
    GET    /events                     — List events (paginated, filterable)
    GET    /events/{event_id}          — Get a single event
    GET    /events/stream              — SSE stream of live events (tails Kafka)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.dependencies import DBSession, Pagination
from shared.db.models import EventRecordORM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


# ─────────────────────────────────────────────────────────────────────────────
# Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class EventResponse(BaseModel):
    """API response schema for an event record."""

    event_id: str
    event_type: str
    topic: str
    source: str
    workflow_id: str | None
    task_id: str | None
    agent_id: str | None
    correlation_id: str | None
    causation_id: str | None
    severity: str
    payload: dict[str, Any]
    event_metadata: dict[str, str]
    occurred_at: datetime
    schema_version: str

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    """Paginated list of events."""

    items: list[EventResponse]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(ev: EventRecordORM) -> EventResponse:
    """Convert an EventRecordORM instance to an EventResponse."""
    return EventResponse(
        event_id=ev.event_id,
        event_type=ev.event_type,
        topic=ev.topic,
        source=ev.source,
        workflow_id=ev.workflow_id,
        task_id=ev.task_id,
        agent_id=ev.agent_id,
        correlation_id=ev.correlation_id,
        causation_id=ev.causation_id,
        severity=ev.severity,
        payload=ev.payload or {},
        event_metadata=ev.event_metadata or {},
        occurred_at=ev.occurred_at,
        schema_version=ev.schema_version,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /events — List events
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=EventListResponse,
    summary="List events",
    description=(
        "Returns a paginated list of events from the audit log, ordered by "
        "occurred_at descending.  Supports filtering by event type, topic, "
        "source, workflow, task, or agent."
    ),
)
async def list_events(
    db: DBSession,
    pagination: Pagination,
    event_type: str | None = Query(default=None, description="Filter by event type."),
    topic: str | None = Query(default=None, description="Filter by Kafka topic."),
    source: str | None = Query(default=None, description="Filter by event source."),
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID."),
    task_id: str | None = Query(default=None, description="Filter by task ID."),
    agent_id: str | None = Query(default=None, description="Filter by agent ID."),
    severity: str | None = Query(default=None, description="Filter by severity level."),
    since: datetime | None = Query(
        default=None,
        description="Return events that occurred after this UTC timestamp.",
    ),
    until: datetime | None = Query(
        default=None,
        description="Return events that occurred before this UTC timestamp.",
    ),
) -> EventListResponse:
    """List events from the audit log."""
    stmt = select(EventRecordORM).order_by(EventRecordORM.occurred_at.desc())

    if event_type:
        stmt = stmt.where(EventRecordORM.event_type == event_type)
    if topic:
        stmt = stmt.where(EventRecordORM.topic == topic)
    if source:
        stmt = stmt.where(EventRecordORM.source == source)
    if workflow_id:
        stmt = stmt.where(EventRecordORM.workflow_id == workflow_id)
    if task_id:
        stmt = stmt.where(EventRecordORM.task_id == task_id)
    if agent_id:
        stmt = stmt.where(EventRecordORM.agent_id == agent_id)
    if severity:
        stmt = stmt.where(EventRecordORM.severity == severity)
    if since:
        stmt = stmt.where(EventRecordORM.occurred_at >= since)
    if until:
        stmt = stmt.where(EventRecordORM.occurred_at <= until)

    # Count total
    count_stmt = stmt.order_by(None)
    total_result = await db.execute(count_stmt)
    total = len(total_result.scalars().all())

    # Apply pagination
    stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(stmt)
    events = result.scalars().all()

    return EventListResponse(
        items=[_orm_to_response(ev) for ev in events],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /events/{event_id} — Get a single event
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{event_id}",
    response_model=EventResponse,
    summary="Get an event",
    description="Returns the full details of a single event from the audit log.",
)
async def get_event(
    event_id: str,
    db: DBSession,
) -> EventResponse:
    """Retrieve an event by ID."""
    ev = await db.get(EventRecordORM, event_id)
    if ev is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id!r} not found.",
        )
    return _orm_to_response(ev)


# ─────────────────────────────────────────────────────────────────────────────
# GET /events/stream — SSE stream of live events
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/stream",
    summary="Stream live events via SSE",
    description=(
        "Opens a Server-Sent Events (SSE) stream that delivers real-time "
        "events from the Kafka event bus.  The stream is backed by the "
        "WebSocket server's broadcast queue.  Clients receive events as "
        "JSON-encoded SSE data frames.\n\n"
        "Optionally filter by event_type, workflow_id, task_id, or agent_id."
    ),
    response_class=StreamingResponse,
)
async def stream_events(
    request: Request,
    event_type: str | None = Query(default=None, description="Filter by event type."),
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID."),
    task_id: str | None = Query(default=None, description="Filter by task ID."),
    agent_id: str | None = Query(default=None, description="Filter by agent ID."),
) -> StreamingResponse:
    """
    SSE endpoint that streams live Kafka events to the client.

    The stream is backed by the application-level broadcast queue populated
    by the WebSocket server's Kafka consumer.  Each event is delivered as
    an SSE data frame containing a JSON-encoded event object.

    The connection is kept alive with periodic heartbeat comments.
    """
    broadcast_queue: asyncio.Queue | None = getattr(
        request.app.state, "broadcast_queue", None
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        """Generate SSE frames from the broadcast queue."""
        # Create a per-client queue by subscribing to the broadcast
        client_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        # Register this client's queue with the broadcast manager
        broadcast_manager = getattr(request.app.state, "broadcast_manager", None)
        if broadcast_manager is not None:
            broadcast_manager.subscribe(client_queue)

        try:
            # Send an initial connection confirmation
            yield "event: connected\ndata: {\"status\": \"connected\"}\n\n"

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected")
                    break

                try:
                    # Wait for next event with timeout for heartbeat
                    event_data = await asyncio.wait_for(
                        client_queue.get(),
                        timeout=15.0,
                    )

                    # Apply filters
                    if event_type and event_data.get("event_type") != event_type:
                        continue
                    if workflow_id and event_data.get("workflow_id") != workflow_id:
                        continue
                    if task_id and event_data.get("task_id") != task_id:
                        continue
                    if agent_id and event_data.get("agent_id") != agent_id:
                        continue

                    # Emit SSE data frame
                    json_data = json.dumps(event_data, default=str)
                    yield f"data: {json_data}\n\n"

                except asyncio.TimeoutError:
                    # Send heartbeat comment to keep connection alive
                    yield ": heartbeat\n\n"

        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled")
        finally:
            # Unsubscribe this client's queue
            if broadcast_manager is not None:
                broadcast_manager.unsubscribe(client_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
