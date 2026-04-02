"""
api/routers/agents.py — FlowOS Agent REST Endpoints

Provides registration and management endpoints for FlowOS agents.

Endpoints:
    POST   /agents                     — Register a new agent
    GET    /agents                     — List agents (paginated, filterable)
    GET    /agents/{agent_id}          — Get a single agent
    PATCH  /agents/{agent_id}          — Update agent metadata
    POST   /agents/{agent_id}/heartbeat — Agent heartbeat / status update
    POST   /agents/{agent_id}/deregister — Deregister an agent
    GET    /agents/{agent_id}/tasks    — List tasks assigned to an agent
    GET    /agents/{agent_id}/capabilities — List agent capabilities
    POST   /agents/{agent_id}/capabilities — Add/update a capability
    DELETE /agents/{agent_id}/capabilities/{capability_id} — Remove a capability
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import DBSession, KafkaProducer, OptionalAgentID, Pagination
from shared.db.models import AgentCapabilityORM, AgentORM, TaskORM
from shared.kafka.schemas import (
    AgentBusyPayload,
    AgentCapabilityUpdatedPayload,
    AgentIdlePayload,
    AgentOfflinePayload,
    AgentOnlinePayload,
    AgentRegisteredPayload,
    build_event,
)
from shared.models.agent import AgentStatus, AgentType
from shared.models.event import EventSource, EventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────


class AgentCapabilityRequest(BaseModel):
    """Schema for a capability declaration."""

    name: str = Field(min_length=1, max_length=100, description="Capability identifier.")
    version: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None)
    parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = Field(default=True)


class AgentRegisterRequest(BaseModel):
    """Request body for registering a new agent."""

    name: str = Field(min_length=1, max_length=255, description="Human-readable display name.")
    agent_type: AgentType = Field(description="Kind of participant.")
    email: str | None = Field(default=None, description="Contact email (for human agents).")
    display_name: str | None = Field(default=None)
    avatar_url: str | None = Field(default=None)
    timezone: str = Field(default="UTC")
    kafka_group_id: str | None = Field(default=None)
    max_concurrent_tasks: int = Field(default=1, ge=1, le=100)
    tags: list[str] = Field(default_factory=list)
    agent_metadata: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[AgentCapabilityRequest] = Field(default_factory=list)
    agent_id: str | None = Field(
        default=None,
        description="Explicit agent ID (UUID). Auto-generated if omitted.",
    )


class AgentUpdateRequest(BaseModel):
    """Request body for updating agent metadata."""

    display_name: str | None = Field(default=None)
    avatar_url: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    max_concurrent_tasks: int | None = Field(default=None, ge=1, le=100)
    tags: list[str] | None = Field(default=None)
    agent_metadata: dict[str, Any] | None = Field(default=None)


class AgentHeartbeatRequest(BaseModel):
    """Request body for an agent heartbeat."""

    status: AgentStatus = Field(description="Current operational status.")
    current_task_id: str | None = Field(default=None, description="Task currently being executed.")


class AgentCapabilityResponse(BaseModel):
    """API response schema for a capability record."""

    capability_id: str
    agent_id: str
    name: str
    version: str | None
    description: str | None
    parameters: dict[str, Any]
    enabled: bool

    model_config = {"from_attributes": True}


class AgentResponse(BaseModel):
    """API response schema for an agent record."""

    agent_id: str
    name: str
    agent_type: str
    status: str
    current_task_id: str | None
    workspace_id: str | None
    email: str | None
    display_name: str | None
    avatar_url: str | None
    timezone: str
    kafka_group_id: str | None
    max_concurrent_tasks: int
    tags: list[str]
    agent_metadata: dict[str, Any]
    last_heartbeat_at: datetime | None
    registered_at: datetime
    updated_at: datetime
    deregistered_at: datetime | None

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    """Paginated list of agents."""

    items: list[AgentResponse]
    total: int
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────────────────────
# Helper: ORM → response
# ─────────────────────────────────────────────────────────────────────────────


def _orm_to_response(agent: AgentORM) -> AgentResponse:
    """Convert an AgentORM instance to an AgentResponse."""
    return AgentResponse(
        agent_id=agent.agent_id,
        name=agent.name,
        agent_type=agent.agent_type,
        status=agent.status,
        current_task_id=agent.current_task_id,
        workspace_id=agent.workspace_id,
        email=agent.email,
        display_name=agent.display_name,
        avatar_url=agent.avatar_url,
        timezone=agent.timezone,
        kafka_group_id=agent.kafka_group_id,
        max_concurrent_tasks=agent.max_concurrent_tasks,
        tags=agent.tags or [],
        agent_metadata=agent.agent_metadata or {},
        last_heartbeat_at=agent.last_heartbeat_at,
        registered_at=agent.registered_at,
        updated_at=agent.updated_at,
        deregistered_at=agent.deregistered_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /agents — Register a new agent
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new agent",
    description=(
        "Registers a new agent (human, AI, build, or deploy) in the FlowOS system. "
        "Publishes an AGENT_REGISTERED event to the Kafka event bus."
    ),
)
async def register_agent(
    body: AgentRegisterRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> AgentResponse:
    """Register a new agent."""
    agent_id = body.agent_id or str(uuid.uuid4())

    if body.agent_id:
        try:
            uuid.UUID(body.agent_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"agent_id must be a valid UUID, got: {body.agent_id!r}",
            )

    # Check for duplicate
    existing = await db.get(AgentORM, agent_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent with ID {agent_id!r} already exists.",
        )

    # Check email uniqueness for human agents
    if body.email:
        email_stmt = select(AgentORM).where(AgentORM.email == body.email)
        email_result = await db.execute(email_stmt)
        if email_result.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An agent with email {body.email!r} already exists.",
            )

    agent = AgentORM(
        agent_id=agent_id,
        name=body.name,
        agent_type=body.agent_type,
        status=AgentStatus.OFFLINE,
        email=body.email,
        display_name=body.display_name,
        avatar_url=body.avatar_url,
        timezone=body.timezone,
        kafka_group_id=body.kafka_group_id,
        max_concurrent_tasks=body.max_concurrent_tasks,
        tags=body.tags,
        agent_metadata=body.agent_metadata,
    )
    db.add(agent)
    await db.flush()

    # Add capabilities
    for cap in body.capabilities:
        capability = AgentCapabilityORM(
            capability_id=str(uuid.uuid4()),
            agent_id=agent_id,
            name=cap.name,
            version=cap.version,
            description=cap.description,
            parameters=cap.parameters,
            enabled=cap.enabled,
        )
        db.add(capability)

    await db.flush()

    # Publish AGENT_REGISTERED event
    try:
        payload = AgentRegisteredPayload(
            agent_id=agent_id,
            name=body.name,
            agent_type=body.agent_type,
            capabilities=[c.name for c in body.capabilities],
        )
        event = build_event(
            event_type=EventType.AGENT_REGISTERED,
            payload=payload,
            source=EventSource.API,
            agent_id=agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish AGENT_REGISTERED event: %s", exc)

    await db.refresh(agent)
    logger.info("Registered agent %s (%s, %s)", agent_id, body.name, body.agent_type)
    return _orm_to_response(agent)


# ─────────────────────────────────────────────────────────────────────────────
# GET /agents — List agents
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=AgentListResponse,
    summary="List agents",
    description="Returns a paginated list of registered agents, optionally filtered by type or status.",
)
async def list_agents(
    db: DBSession,
    pagination: Pagination,
    agent_type: str | None = Query(default=None, description="Filter by agent type."),
    agent_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by agent status.",
    ),
    tag: str | None = Query(default=None, description="Filter by tag."),
) -> AgentListResponse:
    """List registered agents."""
    stmt = select(AgentORM).order_by(AgentORM.registered_at.desc())

    if agent_type:
        stmt = stmt.where(AgentORM.agent_type == agent_type)
    if agent_status:
        stmt = stmt.where(AgentORM.status == agent_status)
    if tag:
        stmt = stmt.where(AgentORM.tags.contains([tag]))

    # Count total
    count_stmt = stmt.order_by(None)
    total_result = await db.execute(count_stmt)
    total = len(total_result.scalars().all())

    # Apply pagination
    stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(stmt)
    agents = result.scalars().all()

    return AgentListResponse(
        items=[_orm_to_response(a) for a in agents],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /agents/{agent_id} — Get a single agent
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="Get an agent",
    description="Returns the full state of a single agent.",
)
async def get_agent(
    agent_id: str,
    db: DBSession,
) -> AgentResponse:
    """Retrieve an agent by ID."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )
    return _orm_to_response(agent)


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /agents/{agent_id} — Update agent metadata
# ─────────────────────────────────────────────────────────────────────────────


@router.patch(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="Update agent metadata",
    description="Updates mutable metadata fields on an agent.",
)
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    db: DBSession,
) -> AgentResponse:
    """Update mutable fields on an agent."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    if body.display_name is not None:
        agent.display_name = body.display_name
    if body.avatar_url is not None:
        agent.avatar_url = body.avatar_url
    if body.timezone is not None:
        agent.timezone = body.timezone
    if body.max_concurrent_tasks is not None:
        agent.max_concurrent_tasks = body.max_concurrent_tasks
    if body.tags is not None:
        agent.tags = body.tags
    if body.agent_metadata is not None:
        agent.agent_metadata = body.agent_metadata

    await db.flush()
    await db.refresh(agent)
    return _orm_to_response(agent)


# ─────────────────────────────────────────────────────────────────────────────
# POST /agents/{agent_id}/heartbeat — Agent heartbeat
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/heartbeat",
    response_model=AgentResponse,
    summary="Agent heartbeat",
    description=(
        "Called by an agent to report its current status and update the "
        "last_heartbeat_at timestamp.  Publishes AGENT_ONLINE, AGENT_BUSY, "
        "or AGENT_IDLE events as appropriate."
    ),
)
async def agent_heartbeat(
    agent_id: str,
    body: AgentHeartbeatRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> AgentResponse:
    """Process an agent heartbeat."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    if agent.status == AgentStatus.DEREGISTERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {agent_id!r} has been deregistered.",
        )

    old_status = agent.status
    agent.status = body.status
    agent.last_heartbeat_at = datetime.now(timezone.utc)
    if body.current_task_id is not None:
        agent.current_task_id = body.current_task_id

    await db.flush()

    # Publish status change event if status changed
    if old_status != body.status:
        try:
            event_type_map = {
                AgentStatus.ONLINE: EventType.AGENT_ONLINE,
                AgentStatus.IDLE: EventType.AGENT_IDLE,
                AgentStatus.BUSY: EventType.AGENT_BUSY,
                AgentStatus.OFFLINE: EventType.AGENT_OFFLINE,
            }
            event_type = event_type_map.get(body.status)
            if event_type:
                if event_type == EventType.AGENT_ONLINE:
                    payload = AgentOnlinePayload(
                        agent_id=agent_id,
                        name=agent.name,
                    )
                elif event_type == EventType.AGENT_OFFLINE:
                    payload = AgentOfflinePayload(
                        agent_id=agent_id,
                        name=agent.name,
                    )
                elif event_type == EventType.AGENT_BUSY:
                    payload = AgentBusyPayload(
                        agent_id=agent_id,
                        current_task_id=body.current_task_id,
                    )
                else:
                    # AGENT_IDLE
                    payload = AgentIdlePayload(
                        agent_id=agent_id,
                    )
                event = build_event(
                    event_type=event_type,
                    payload=payload,
                    source=EventSource.API,
                    agent_id=agent_id,
                )
                producer.produce(event)
        except Exception as exc:
            logger.warning("Failed to publish agent status event: %s", exc)

    await db.refresh(agent)
    return _orm_to_response(agent)


# ─────────────────────────────────────────────────────────────────────────────
# POST /agents/{agent_id}/deregister — Deregister an agent
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/deregister",
    response_model=AgentResponse,
    summary="Deregister an agent",
    description=(
        "Marks an agent as deregistered.  The agent record is preserved for "
        "audit purposes.  Publishes an AGENT_OFFLINE event."
    ),
)
async def deregister_agent(
    agent_id: str,
    db: DBSession,
    producer: KafkaProducer,
) -> AgentResponse:
    """Deregister an agent."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    if agent.status == AgentStatus.DEREGISTERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {agent_id!r} is already deregistered.",
        )

    now = datetime.now(timezone.utc)
    agent.status = AgentStatus.DEREGISTERED
    agent.deregistered_at = now
    await db.flush()

    try:
        payload = AgentOfflinePayload(
            agent_id=agent_id,
            name=agent.name,
        )
        event = build_event(
            event_type=EventType.AGENT_OFFLINE,
            payload=payload,
            source=EventSource.API,
            agent_id=agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish AGENT_OFFLINE event: %s", exc)

    await db.refresh(agent)
    return _orm_to_response(agent)


# ─────────────────────────────────────────────────────────────────────────────
# GET /agents/{agent_id}/tasks — List tasks assigned to an agent
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{agent_id}/tasks",
    summary="List tasks assigned to an agent",
    description="Returns all tasks currently or previously assigned to an agent.",
)
async def list_agent_tasks(
    agent_id: str,
    db: DBSession,
    pagination: Pagination,
    task_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by task status.",
    ),
) -> dict[str, Any]:
    """List tasks assigned to an agent."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    stmt = (
        select(TaskORM)
        .where(TaskORM.assigned_agent_id == agent_id)
        .order_by(TaskORM.created_at.desc())
    )
    if task_status:
        stmt = stmt.where(TaskORM.status == task_status)

    result = await db.execute(stmt)
    tasks = result.scalars().all()

    total = len(tasks)
    paginated = tasks[pagination.offset : pagination.offset + pagination.limit]

    return {
        "agent_id": agent_id,
        "items": [t.to_dict() for t in paginated],
        "total": total,
        "limit": pagination.limit,
        "offset": pagination.offset,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /agents/{agent_id}/capabilities — List agent capabilities
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{agent_id}/capabilities",
    response_model=list[AgentCapabilityResponse],
    summary="List agent capabilities",
    description="Returns all declared capabilities for an agent.",
)
async def list_agent_capabilities(
    agent_id: str,
    db: DBSession,
) -> list[AgentCapabilityResponse]:
    """List capabilities for an agent."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    stmt = (
        select(AgentCapabilityORM)
        .where(AgentCapabilityORM.agent_id == agent_id)
        .order_by(AgentCapabilityORM.name.asc())
    )
    result = await db.execute(stmt)
    capabilities = result.scalars().all()

    return [
        AgentCapabilityResponse(
            capability_id=c.capability_id,
            agent_id=c.agent_id,
            name=c.name,
            version=c.version,
            description=c.description,
            parameters=c.parameters or {},
            enabled=c.enabled,
        )
        for c in capabilities
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST /agents/{agent_id}/capabilities — Add/update a capability
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/capabilities",
    response_model=AgentCapabilityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add or update an agent capability",
    description=(
        "Adds a new capability to an agent, or updates an existing one if a "
        "capability with the same name already exists."
    ),
)
async def upsert_agent_capability(
    agent_id: str,
    body: AgentCapabilityRequest,
    db: DBSession,
    producer: KafkaProducer,
) -> AgentCapabilityResponse:
    """Add or update a capability for an agent."""
    agent = await db.get(AgentORM, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found.",
        )

    # Check if capability with this name already exists
    stmt = (
        select(AgentCapabilityORM)
        .where(AgentCapabilityORM.agent_id == agent_id)
        .where(AgentCapabilityORM.name == body.name)
    )
    result = await db.execute(stmt)
    existing_cap = result.scalar_one_or_none()

    if existing_cap is not None:
        # Update existing
        existing_cap.version = body.version
        existing_cap.description = body.description
        existing_cap.parameters = body.parameters
        existing_cap.enabled = body.enabled
        await db.flush()
        cap = existing_cap
    else:
        # Create new
        cap = AgentCapabilityORM(
            capability_id=str(uuid.uuid4()),
            agent_id=agent_id,
            name=body.name,
            version=body.version,
            description=body.description,
            parameters=body.parameters,
            enabled=body.enabled,
        )
        db.add(cap)
        await db.flush()

    # Publish AGENT_CAPABILITY_UPDATED event
    try:
        payload = AgentCapabilityUpdatedPayload(
            agent_id=agent_id,
            capabilities=[body.name],
        )
        event = build_event(
            event_type=EventType.AGENT_CAPABILITY_UPDATED,
            payload=payload,
            source=EventSource.API,
            agent_id=agent_id,
        )
        producer.produce(event)
    except Exception as exc:
        logger.warning("Failed to publish AGENT_CAPABILITY_UPDATED event: %s", exc)

    await db.refresh(cap)
    return AgentCapabilityResponse(
        capability_id=cap.capability_id,
        agent_id=cap.agent_id,
        name=cap.name,
        version=cap.version,
        description=cap.description,
        parameters=cap.parameters or {},
        enabled=cap.enabled,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /agents/{agent_id}/capabilities/{capability_id} — Remove a capability
# ─────────────────────────────────────────────────────────────────────────────


@router.delete(
    "/{agent_id}/capabilities/{capability_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an agent capability",
    description="Removes a capability from an agent.",
)
async def delete_agent_capability(
    agent_id: str,
    capability_id: str,
    db: DBSession,
) -> None:
    """Remove a capability from an agent."""
    cap = await db.get(AgentCapabilityORM, capability_id)
    if cap is None or cap.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Capability {capability_id!r} not found for agent {agent_id!r}.",
        )

    await db.delete(cap)
    await db.flush()
