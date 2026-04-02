"""
shared/db/session.py — Database Session Context Managers

Provides ``get_db()`` and ``get_async_db()`` context managers / dependency
injectors for use in FastAPI routes, background workers, and CLI tools.

Usage in FastAPI:
    from shared.db.session import get_async_db
    from sqlalchemy.ext.asyncio import AsyncSession

    @router.get("/workflows")
    async def list_workflows(db: AsyncSession = Depends(get_async_db)):
        result = await db.execute(select(Workflow))
        return result.scalars().all()

Usage as a context manager (async):
    from shared.db.session import async_db_session

    async with async_db_session() as db:
        result = await db.execute(select(Workflow))

Usage as a context manager (sync):
    from shared.db.session import db_session

    with db_session() as db:
        workflows = db.query(Workflow).all()
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.db.base import AsyncSessionLocal, SessionLocal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Async session — FastAPI dependency injection
# ─────────────────────────────────────────────────────────────────────────────


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async SQLAlchemy session.

    The session is automatically committed on success and rolled back on
    exception.  It is always closed when the request completes.

    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_async_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Sync session — FastAPI dependency injection
# ─────────────────────────────────────────────────────────────────────────────


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a synchronous SQLAlchemy session.

    The session is automatically committed on success and rolled back on
    exception.  It is always closed when the request completes.

    Usage:
        @router.get("/items")
        def get_items(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Async context manager — background workers and scripts
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that provides a database session with automatic
    commit/rollback semantics.

    Usage:
        async with async_db_session() as db:
            workflow = await db.get(Workflow, workflow_id)
            workflow.status = "completed"
            # commit happens automatically on exit

    Raises:
        Any exception raised inside the ``async with`` block will trigger a
        rollback before being re-raised.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
            logger.debug("Async DB session committed successfully.")
        except Exception as exc:
            await session.rollback()
            logger.warning(
                "Async DB session rolled back due to exception: %s: %s",
                type(exc).__name__,
                exc,
            )
            raise
        finally:
            await session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Sync context manager — CLI tools and Alembic
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Synchronous context manager that provides a database session with
    automatic commit/rollback semantics.

    Usage:
        with db_session() as db:
            agent = db.query(Agent).filter_by(id=agent_id).first()
            agent.status = "idle"
            # commit happens automatically on exit

    Raises:
        Any exception raised inside the ``with`` block will trigger a
        rollback before being re-raised.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
        logger.debug("Sync DB session committed successfully.")
    except Exception as exc:
        session.rollback()
        logger.warning(
            "Sync DB session rolled back due to exception: %s: %s",
            type(exc).__name__,
            exc,
        )
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Transactional decorator helpers
# ─────────────────────────────────────────────────────────────────────────────


class TransactionError(Exception):
    """Raised when a database transaction cannot be completed."""


async def run_in_transaction(
    func: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Execute an async callable inside a database transaction.

    The callable receives the session as its first argument.

    Args:
        func: Async callable with signature ``async def func(db: AsyncSession, *args, **kwargs)``.
        *args: Positional arguments forwarded to ``func``.
        **kwargs: Keyword arguments forwarded to ``func``.

    Returns:
        The return value of ``func``.

    Raises:
        TransactionError: If the transaction fails after rollback.
    """
    async with async_db_session() as db:
        return await func(db, *args, **kwargs)
