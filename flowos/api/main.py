"""
api/main.py — FlowOS REST API & WebSocket Server

The main FastAPI application for FlowOS.  Provides:
- REST API for workflows, tasks, agents, checkpoints, handoffs, and events
- WebSocket endpoint for real-time event streaming
- SSE endpoint for browser-compatible event streaming
- Health check and readiness endpoints
- OpenAPI documentation at /docs and /redoc

Architecture:
    Browser / CLI
        ↓  HTTP REST  →  FastAPI routers  →  PostgreSQL (SQLAlchemy async)
        ↓  WebSocket  →  ws_server.py     →  Kafka (confluent-kafka consumer)
        ↓  SSE        →  /events/stream   →  BroadcastManager

Startup sequence:
    1. Connect to PostgreSQL (async engine)
    2. Initialise Kafka producer (singleton)
    3. Connect to Temporal (optional — degrades gracefully)
    4. Start Kafka broadcast consumer (background thread)
    5. Register all routers
    6. Expose WebSocket endpoint

Usage::

    # Development
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

    # Production
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from shared.config import settings
from shared.db.base import check_async_db_connection

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Application lifespan
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    Handles startup and shutdown of all application-level resources:
    - Kafka producer
    - Temporal client
    - Kafka broadcast consumer (WebSocket/SSE backend)
    """
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("FlowOS API starting up...")

    # 1. Initialise Kafka producer
    try:
        from shared.kafka.producer import FlowOSProducer
        app.state.producer = FlowOSProducer()
        logger.info("Kafka producer initialised")
    except Exception as exc:
        logger.warning("Kafka producer initialisation failed: %s", exc)
        app.state.producer = None

    # 2. Connect to Temporal (optional)
    try:
        from temporalio.client import Client as TemporalClient
        app.state.temporal_client = await TemporalClient.connect(
            settings.temporal.address,
            namespace=settings.temporal.namespace,
        )
        logger.info("Temporal client connected to %s", settings.temporal.address)
    except Exception as exc:
        logger.warning(
            "Temporal client connection failed (non-fatal): %s. "
            "Workflow submission via API will be unavailable.",
            exc,
        )
        app.state.temporal_client = None

    # 3. Initialise broadcast manager and WebSocket manager
    from api.ws_server import BroadcastManager, KafkaBroadcastBridge, WebSocketManager

    broadcast_manager = BroadcastManager(max_queue_size=1000)
    app.state.broadcast_manager = broadcast_manager
    app.state.ws_manager = WebSocketManager(broadcast_manager)

    # 4. Start Kafka broadcast consumer in background thread
    loop = asyncio.get_event_loop()
    bridge = KafkaBroadcastBridge(broadcast_manager, loop)
    app.state.kafka_bridge = bridge
    try:
        bridge.start()
        logger.info("Kafka broadcast bridge started")
    except Exception as exc:
        logger.warning("Kafka broadcast bridge failed to start: %s", exc)

    # 5. Record startup time
    app.state.started_at = time.time()

    logger.info("FlowOS API startup complete")

    yield  # ── Application running ──────────────────────────────────────────

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("FlowOS API shutting down...")

    # Stop Kafka broadcast bridge
    bridge = getattr(app.state, "kafka_bridge", None)
    if bridge is not None:
        try:
            bridge.stop()
        except Exception as exc:
            logger.warning("Error stopping Kafka bridge: %s", exc)

    # Flush and close Kafka producer
    producer = getattr(app.state, "producer", None)
    if producer is not None:
        try:
            producer.close()
        except Exception as exc:
            logger.warning("Error closing Kafka producer: %s", exc)

    logger.info("FlowOS API shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """
    Create and configure the FlowOS FastAPI application.

    Returns:
        A fully configured FastAPI application instance.
    """
    app = FastAPI(
        title="FlowOS API",
        description=(
            "REST API and WebSocket server for the FlowOS distributed "
            "human + machine orchestration platform.\n\n"
            "**Key features:**\n"
            "- Workflow lifecycle management (start, pause, resume, cancel)\n"
            "- Task assignment and status tracking\n"
            "- Agent registration and heartbeat\n"
            "- Checkpoint and handoff management\n"
            "- Real-time event streaming via WebSocket and SSE\n"
            "- Full Kafka event bus integration\n\n"
            "**WebSocket:** Connect to `ws://<host>/ws` for real-time events.\n"
            "**SSE:** Subscribe to `GET /events/stream` for browser-compatible streaming."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── CORS middleware ───────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Agent-ID"],
    )

    # ── Request logging middleware ────────────────────────────────────────────
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log all HTTP requests with timing."""
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "%s %s → %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response

    # ── Register routers ─────────────────────────────────────────────────────
    from api.routers.workflows import router as workflows_router
    from api.routers.tasks import router as tasks_router
    from api.routers.agents import router as agents_router
    from api.routers.checkpoints import router as checkpoints_router
    from api.routers.handoffs import router as handoffs_router
    from api.routers.events import router as events_router

    # The Vite dev proxy rewrites /api/* -> /* (strips the /api prefix).
    # So the backend must serve routes at /workflows, /tasks, etc. (no prefix).
    # In production (direct access), the reverse proxy should also strip /api.
    api_prefix = ""

    app.include_router(workflows_router, prefix=api_prefix)
    app.include_router(tasks_router, prefix=api_prefix)
    app.include_router(agents_router, prefix=api_prefix)
    app.include_router(checkpoints_router, prefix=api_prefix)
    app.include_router(handoffs_router, prefix=api_prefix)
    app.include_router(events_router, prefix=api_prefix)

    # ── WebSocket endpoint ────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def websocket_route(
        websocket: WebSocket,
        event_type: str | None = Query(default=None),
        workflow_id: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
    ) -> None:
        """
        WebSocket endpoint for real-time event streaming.

        Connect to ``ws://<host>/ws`` to receive a live stream of all
        FlowOS events from the Kafka event bus.

        Optional query parameters for filtering:
        - ``event_type``: Filter to a specific event type
        - ``workflow_id``: Filter to events for a specific workflow
        - ``task_id``: Filter to events for a specific task
        - ``agent_id``: Filter to events for a specific agent

        Message format::

            // Connection acknowledgement
            {"type": "connected", "message": "...", "filters": {...}}

            // Event message
            {"type": "event", "data": {<EventRecord fields>}}

            // Heartbeat (every 30s)
            {"type": "ping", "ts": 1234567890.0}
        """
        from api.ws_server import websocket_endpoint
        await websocket_endpoint(
            websocket=websocket,
            event_type=event_type,
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=agent_id,
        )

    # ── Health & readiness endpoints ──────────────────────────────────────────
    @app.get(
        "/health",
        tags=["system"],
        summary="Health check",
        description="Returns the health status of the API and its dependencies.",
    )
    async def health_check() -> dict[str, Any]:
        """
        Health check endpoint.

        Returns the operational status of the API server and its key
        dependencies (database, Kafka, Temporal).
        """
        db_healthy = await check_async_db_connection()
        producer = getattr(app.state, "producer", None)
        temporal_client = getattr(app.state, "temporal_client", None)
        broadcast_manager = getattr(app.state, "broadcast_manager", None)

        started_at = getattr(app.state, "started_at", None)
        uptime_seconds = time.time() - started_at if started_at else 0

        health = {
            "status": "healthy" if db_healthy else "degraded",
            "version": "0.1.0",
            "uptime_seconds": round(uptime_seconds, 1),
            "dependencies": {
                "database": "healthy" if db_healthy else "unhealthy",
                "kafka_producer": "healthy" if producer is not None else "unavailable",
                "temporal": "healthy" if temporal_client is not None else "unavailable",
                "websocket_clients": (
                    broadcast_manager.client_count if broadcast_manager else 0
                ),
            },
        }

        http_status = status.HTTP_200_OK if db_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(content=health, status_code=http_status)

    @app.get(
        "/ready",
        tags=["system"],
        summary="Readiness check",
        description="Returns 200 if the API is ready to serve requests.",
    )
    async def readiness_check() -> dict[str, str]:
        """
        Kubernetes-compatible readiness probe.

        Returns 200 OK if the database is reachable and the API is ready
        to serve requests.  Returns 503 if not ready.
        """
        db_healthy = await check_async_db_connection()
        if not db_healthy:
            return JSONResponse(
                content={"status": "not_ready", "reason": "database_unavailable"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return {"status": "ready"}

    @app.get(
        "/",
        tags=["system"],
        summary="API root",
        description="Returns basic API information.",
        include_in_schema=False,
    )
    async def root() -> dict[str, str]:
        """API root endpoint."""
        return {
            "name": "FlowOS API",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
            "websocket": "ws://<host>/ws",
        }

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Application instance (for uvicorn)
# ─────────────────────────────────────────────────────────────────────────────

app = create_app()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.is_development,
        log_level="info",
    )
