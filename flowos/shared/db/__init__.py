"""
shared.db — Database access layer for FlowOS.

Exports:
    Base           — SQLAlchemy declarative base
    engine         — Synchronous SQLAlchemy engine
    async_engine   — Asynchronous SQLAlchemy engine
    SessionLocal   — Synchronous session factory
    AsyncSessionLocal — Asynchronous session factory
    get_db         — Sync FastAPI dependency
    get_async_db   — Async FastAPI dependency
    db_session     — Sync context manager
    async_db_session — Async context manager
"""

from shared.db.base import (
    AsyncSessionLocal,
    Base,
    SessionLocal,
    async_engine,
    check_async_db_connection,
    check_sync_db_connection,
    engine,
    metadata,
)
from shared.db.session import (
    async_db_session,
    db_session,
    get_async_db,
    get_db,
    run_in_transaction,
)

__all__ = [
    # Base and metadata
    "Base",
    "metadata",
    # Engines
    "engine",
    "async_engine",
    # Session factories
    "SessionLocal",
    "AsyncSessionLocal",
    # FastAPI dependencies
    "get_db",
    "get_async_db",
    # Context managers
    "db_session",
    "async_db_session",
    # Helpers
    "run_in_transaction",
    "check_sync_db_connection",
    "check_async_db_connection",
]
