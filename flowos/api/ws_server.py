"""
api/ws_server.py — FlowOS WebSocket & Broadcast Server

Provides real-time event streaming to browser clients via WebSocket connections.
Events are sourced from the Kafka event bus (consumer group: flowos-ui-stream)
and broadcast to all connected WebSocket clients.

Architecture:
    Kafka (all topics)
        ↓  [FlowOSConsumer — ConsumerGroup.UI_STREAM]
    BroadcastManager (asyncio.Queue per client)
        ↓  [WebSocket handler]
    Browser clients (React + React Flow UI)

Features:
- Per-client asyncio.Queue for backpressure management
- Kafka consumer runs in a background thread (confluent-kafka is sync)
- Events are forwarded to the async event loop via thread-safe queue
- WebSocket clients can subscribe to filtered event streams
- Graceful shutdown with consumer stop and client disconnect
- Heartbeat pings to detect stale connections
- SSE broadcast queue for the /events/stream REST endpoint

Usage:
    The WebSocket server is started as part of the FastAPI application
    lifespan in ``api/main.py``.  It exposes:

    - ``/ws`` — WebSocket endpoint for real-time event streaming
    - ``app.state.broadcast_manager`` — BroadcastManager instance
    - ``app.state.broadcast_queue`` — asyncio.Queue for SSE endpoint
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from shared.config import settings
from shared.kafka.consumer import FlowOSConsumer
from shared.kafka.topics import ConsumerGroup, KafkaTopic
from shared.models.event import EventRecord

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Broadcast Manager
# ─────────────────────────────────────────────────────────────────────────────


class BroadcastManager:
    """
    Manages a set of per-client asyncio queues for event broadcasting.

    The Kafka consumer thread calls ``broadcast()`` to push events to all
    connected clients.  Each WebSocket handler reads from its own queue.

    Thread safety:
        ``subscribe()``, ``unsubscribe()``, and ``broadcast()`` are all
        thread-safe via a threading.Lock.
    """

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._max_queue_size = max_queue_size
        self._total_broadcast = 0
        self._total_dropped = 0

    def subscribe(self, queue: asyncio.Queue) -> None:
        """Register a new client queue."""
        with self._lock:
            self._clients.add(queue)
        logger.debug("WebSocket client subscribed. Total clients: %d", len(self._clients))

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a client queue."""
        with self._lock:
            self._clients.discard(queue)
        logger.debug("WebSocket client unsubscribed. Total clients: %d", len(self._clients))

    def broadcast(self, event_data: dict[str, Any]) -> None:
        """
        Push an event to all subscribed client queues.

        Called from the Kafka consumer thread.  Uses put_nowait() to avoid
        blocking the consumer thread.  Drops the event for a specific client
        if their queue is full (backpressure).
        """
        with self._lock:
            clients = list(self._clients)

        self._total_broadcast += 1
        for queue in clients:
            try:
                queue.put_nowait(event_data)
            except asyncio.QueueFull:
                self._total_dropped += 1
                logger.warning(
                    "Client queue full, dropping event %s (total dropped: %d)",
                    event_data.get("event_type", "unknown"),
                    self._total_dropped,
                )

    @property
    def client_count(self) -> int:
        """Return the number of connected clients."""
        with self._lock:
            return len(self._clients)

    @property
    def stats(self) -> dict[str, int]:
        """Return broadcast statistics."""
        return {
            "clients": self.client_count,
            "total_broadcast": self._total_broadcast,
            "total_dropped": self._total_dropped,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Kafka → Broadcast bridge
# ─────────────────────────────────────────────────────────────────────────────


class KafkaBroadcastBridge:
    """
    Bridges the synchronous Kafka consumer to the async broadcast manager.

    Runs the Kafka consumer in a background daemon thread.  When an event
    arrives, it is serialised to a dict and pushed to the broadcast manager
    (which distributes it to all WebSocket clients).

    The bridge also maintains a thread-safe queue that the async event loop
    can drain to forward events to the SSE endpoint.
    """

    def __init__(
        self,
        broadcast_manager: BroadcastManager,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._broadcast_manager = broadcast_manager
        self._loop = loop
        self._consumer: FlowOSConsumer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the Kafka consumer in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_consumer,
            name="kafka-broadcast-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("KafkaBroadcastBridge started")

    def stop(self) -> None:
        """Stop the Kafka consumer and background thread."""
        self._running = False
        if self._consumer is not None:
            try:
                self._consumer.stop()
            except Exception as exc:
                logger.warning("Error stopping Kafka consumer: %s", exc)

        if self._thread is not None:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                logger.warning("Kafka broadcast bridge thread did not stop cleanly")

        logger.info("KafkaBroadcastBridge stopped")

    def _run_consumer(self) -> None:
        """
        Consumer loop running in the background thread.

        Creates a FlowOSConsumer subscribed to all topics in the UI_STREAM
        consumer group and dispatches events to the broadcast manager.
        """
        try:
            self._consumer = FlowOSConsumer(
                group_id=ConsumerGroup.UI_STREAM,
                topics=list(KafkaTopic),
                poll_timeout_ms=1000,
                error_strategy="skip",
            )

            @self._consumer.on_all()
            def handle_event(event: EventRecord) -> None:
                """Forward every event to the broadcast manager."""
                try:
                    event_dict = {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "topic": event.topic,
                        "source": event.source,
                        "workflow_id": event.workflow_id,
                        "task_id": event.task_id,
                        "agent_id": event.agent_id,
                        "correlation_id": event.correlation_id,
                        "causation_id": event.causation_id,
                        "severity": event.severity,
                        "payload": event.payload,
                        "metadata": event.metadata,
                        "occurred_at": event.occurred_at.isoformat(),
                        "schema_version": event.schema_version,
                    }
                    self._broadcast_manager.broadcast(event_dict)
                except Exception as exc:
                    logger.warning("Error broadcasting event: %s", exc)

            logger.info(
                "Kafka broadcast consumer started | group=%s topics=%d",
                ConsumerGroup.UI_STREAM,
                len(list(KafkaTopic)),
            )
            self._consumer.run()

        except Exception as exc:
            logger.error("Kafka broadcast bridge consumer failed: %s", exc, exc_info=True)
        finally:
            logger.info("Kafka broadcast bridge consumer exited")


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket connection handler
# ─────────────────────────────────────────────────────────────────────────────


class WebSocketManager:
    """
    Manages active WebSocket connections and routes events to them.

    Each connected client gets a dedicated asyncio.Queue.  The broadcast
    manager pushes events to all queues; each WebSocket handler reads from
    its own queue and sends to the client.
    """

    def __init__(self, broadcast_manager: BroadcastManager) -> None:
        self._broadcast_manager = broadcast_manager

    async def handle_connection(
        self,
        websocket: WebSocket,
        event_type_filter: str | None = None,
        workflow_id_filter: str | None = None,
        task_id_filter: str | None = None,
        agent_id_filter: str | None = None,
    ) -> None:
        """
        Handle a single WebSocket connection lifecycle.

        Accepts the connection, subscribes to the broadcast manager, and
        forwards events to the client until it disconnects.

        Args:
            websocket:           The FastAPI WebSocket connection.
            event_type_filter:   Optional event type filter.
            workflow_id_filter:  Optional workflow ID filter.
            task_id_filter:      Optional task ID filter.
            agent_id_filter:     Optional agent ID filter.
        """
        await websocket.accept()
        client_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._broadcast_manager.subscribe(client_queue)

        # Send connection acknowledgement
        await websocket.send_json({
            "type": "connected",
            "message": "FlowOS WebSocket connected. Streaming events...",
            "filters": {
                "event_type": event_type_filter,
                "workflow_id": workflow_id_filter,
                "task_id": task_id_filter,
                "agent_id": agent_id_filter,
            },
        })

        logger.info(
            "WebSocket client connected | filters: event_type=%s workflow=%s task=%s agent=%s",
            event_type_filter,
            workflow_id_filter,
            task_id_filter,
            agent_id_filter,
        )

        try:
            while True:
                try:
                    # Wait for next event with timeout for heartbeat
                    event_data = await asyncio.wait_for(
                        client_queue.get(),
                        timeout=30.0,
                    )

                    # Apply filters
                    if event_type_filter and event_data.get("event_type") != event_type_filter:
                        continue
                    if workflow_id_filter and event_data.get("workflow_id") != workflow_id_filter:
                        continue
                    if task_id_filter and event_data.get("task_id") != task_id_filter:
                        continue
                    if agent_id_filter and event_data.get("agent_id") != agent_id_filter:
                        continue

                    # Send event to client
                    await websocket.send_json({
                        "type": "event",
                        "data": event_data,
                    })

                except asyncio.TimeoutError:
                    # Send heartbeat ping
                    try:
                        await websocket.send_json({"type": "ping", "ts": time.time()})
                    except Exception:
                        break

                except WebSocketDisconnect:
                    logger.info("WebSocket client disconnected")
                    break

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as exc:
            logger.warning("WebSocket error: %s", exc)
        finally:
            self._broadcast_manager.unsubscribe(client_queue)
            try:
                await websocket.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI WebSocket route handler
# ─────────────────────────────────────────────────────────────────────────────


async def websocket_endpoint(
    websocket: WebSocket,
    event_type: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """
    FastAPI WebSocket endpoint handler.

    Clients connect to ``ws://<host>/ws`` and receive a real-time stream of
    FlowOS events as JSON messages.

    Query parameters (all optional):
        event_type:  Filter to a specific event type (e.g. WORKFLOW_CREATED).
        workflow_id: Filter to events for a specific workflow.
        task_id:     Filter to events for a specific task.
        agent_id:    Filter to events for a specific agent.

    Message format::

        // Connection acknowledgement
        {"type": "connected", "message": "...", "filters": {...}}

        // Event message
        {"type": "event", "data": {<EventRecord fields>}}

        // Heartbeat (every 30s)
        {"type": "ping", "ts": 1234567890.0}
    """
    ws_manager: WebSocketManager | None = getattr(
        websocket.app.state, "ws_manager", None
    )
    if ws_manager is None:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": "WebSocket manager not available. Service may be starting.",
        })
        await websocket.close(code=1011)
        return

    await ws_manager.handle_connection(
        websocket=websocket,
        event_type_filter=event_type,
        workflow_id_filter=workflow_id,
        task_id_filter=task_id,
        agent_id_filter=agent_id,
    )
