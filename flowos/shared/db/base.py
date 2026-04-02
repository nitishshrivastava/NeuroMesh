"""
shared/db/base.py — SQLAlchemy Base, Engine, and SessionLocal

Provides the shared SQLAlchemy declarative base class and both async and sync
engine/session factories used throughout FlowOS.

Usage:
    # Async (preferred for FastAPI / async workers)
    from shared.db.base import AsyncSessionLocal, async_engine

    # Sync (Alembic migrations, CLI tools)
    from shared.db.base import SessionLocal, engine

    # ORM model definition
    from shared.db.base import Base

    class MyModel(Base):
        __tablename__ = "my_table"
        ...
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import MetaData, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, QueuePool
from sqlalchemy import create_engine as _create_sync_engine

from shared.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Naming conventions
# ─────────────────────────────────────────────────────────────────────────────
# Consistent constraint naming makes Alembic migrations deterministic and
# avoids auto-generated names that differ between databases.

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ─────────────────────────────────────────────────────────────────────────────
# Declarative Base
# ─────────────────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    """
    Shared SQLAlchemy declarative base for all FlowOS ORM models.

    All models should inherit from this class:

        class Workflow(Base):
            __tablename__ = "workflows"
            ...

    The base uses the shared ``metadata`` object with consistent constraint
    naming conventions to ensure deterministic Alembic migrations.
    """

    metadata = metadata

    def to_dict(self) -> dict[str, Any]:
        """
        Return a dictionary representation of this model instance.

        Only includes columns defined on the model's table — relationships
        are not traversed.
        """
        return {
            col.name: getattr(self, col.name)
            for col in self.__table__.columns
        }

    def __repr__(self) -> str:
        """Return a readable string representation of the model instance."""
        cls_name = self.__class__.__name__
        pk_cols = [col.name for col in self.__table__.primary_key.columns]
        pk_vals = {col: getattr(self, col, None) for col in pk_cols}
        pk_str = ", ".join(f"{k}={v!r}" for k, v in pk_vals.items())
        return f"<{cls_name}({pk_str})>"


# ─────────────────────────────────────────────────────────────────────────────
# Async Engine (primary — used by FastAPI and async workers)
# ─────────────────────────────────────────────────────────────────────────────

_async_engine_kwargs: dict[str, Any] = {
    "echo": settings.database.echo,
    "pool_size": settings.database.pool_size,
    "max_overflow": settings.database.max_overflow,
    "pool_timeout": settings.database.pool_timeout,
    "pool_recycle": settings.database.pool_recycle,
    "pool_pre_ping": settings.database.pool_pre_ping,
    "poolclass": AsyncAdaptedQueuePool,  # Must use async-compatible pool class
}

# NullPool is safer for test environments to avoid connection leaks
if settings.is_test:
    _async_engine_kwargs["poolclass"] = NullPool
    _async_engine_kwargs.pop("pool_size", None)
    _async_engine_kwargs.pop("max_overflow", None)
    _async_engine_kwargs.pop("pool_timeout", None)
    _async_engine_kwargs.pop("pool_recycle", None)

try:
    async_engine: AsyncEngine = create_async_engine(
        settings.database.url,
        **_async_engine_kwargs,
    )

    # Async session factory
    AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
except Exception as _async_engine_exc:
    # Async engine creation may fail in environments without asyncpg
    # (e.g. Alembic migration context, CLI tools).
    # In these cases, only the sync engine is available.
    logger.warning(
        "Async engine creation failed (asyncpg may not be installed): %s. "
        "Only the synchronous engine will be available.",
        _async_engine_exc,
    )
    async_engine = None  # type: ignore[assignment]
    AsyncSessionLocal = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Sync Engine (used by Alembic migrations and sync CLI tools)
# ─────────────────────────────────────────────────────────────────────────────

_sync_engine_kwargs: dict[str, Any] = {
    "echo": settings.database.echo,
    "pool_size": settings.database.pool_size,
    "max_overflow": settings.database.max_overflow,
    "pool_timeout": settings.database.pool_timeout,
    "pool_recycle": settings.database.pool_recycle,
    "pool_pre_ping": settings.database.pool_pre_ping,
    "poolclass": QueuePool,
}

if settings.is_test:
    _sync_engine_kwargs["poolclass"] = NullPool
    _sync_engine_kwargs.pop("pool_size", None)
    _sync_engine_kwargs.pop("max_overflow", None)
    _sync_engine_kwargs.pop("pool_timeout", None)
    _sync_engine_kwargs.pop("pool_recycle", None)

engine = _create_sync_engine(
    settings.database.sync_url,
    **_sync_engine_kwargs,
)

# Sync session factory
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Engine event listeners
# ─────────────────────────────────────────────────────────────────────────────


@event.listens_for(engine, "connect")
def _set_sync_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    """
    No-op listener for PostgreSQL.  Kept as a hook for future connection
    setup (e.g. setting session-level parameters).
    """
    pass


if async_engine is not None:
    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_async_connection_params(dbapi_connection: Any, connection_record: Any) -> None:
        """
        Set session-level parameters on each new async connection.
        """
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Health check helper
# ─────────────────────────────────────────────────────────────────────────────


async def check_async_db_connection() -> bool:
    """
    Verify that the async database connection is healthy.

    Returns:
        True if the database is reachable, False otherwise.
    """
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Async database health check failed: %s", exc)
        return False


def check_sync_db_connection() -> bool:
    """
    Verify that the sync database connection is healthy.

    Returns:
        True if the database is reachable, False otherwise.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Sync database health check failed: %s", exc)
        return False
