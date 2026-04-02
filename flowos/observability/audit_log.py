"""
observability/audit_log.py — FlowOS Audit Log Writer

Persists every Kafka event consumed by the observability consumer into the
``event_records`` PostgreSQL table (via the ``EventRecordORM`` model).  This
provides a durable, queryable audit trail of all system activity.

Design:
    - ``AuditLogWriter`` is the primary class.  It accepts ``EventRecord``
      objects and writes them to the database using SQLAlchemy.
    - Writes are performed synchronously (using the sync session) to keep the
      consumer loop simple and avoid async complexity in a background thread.
    - Duplicate events (same ``event_id``) are silently ignored via
      ``INSERT ... ON CONFLICT DO NOTHING`` semantics.
    - Batch writes are supported for high-throughput scenarios.
    - All write outcomes are reflected in the Prometheus metrics defined in
      ``observability.metrics``.

Usage::

    from observability.audit_log import AuditLogWriter
    from shared.models.event import EventRecord

    writer = AuditLogWriter()

    # Write a single event
    writer.write(event)

    # Write a batch
    writer.write_batch([event1, event2, event3])

    # Query recent events for a workflow
    events = writer.query_by_workflow("wf-123", limit=50)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from shared.db.base import SessionLocal
from shared.db.models import EventRecordORM
from shared.models.event import EventRecord

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Audit Log Writer
# ─────────────────────────────────────────────────────────────────────────────


class AuditLogWriter:
    """
    Writes ``EventRecord`` objects to the ``event_records`` database table.

    The writer uses a synchronous SQLAlchemy session and is designed to be
    used from a single thread (the Kafka consumer loop).  For async contexts,
    use ``AuditLogWriter`` in a thread pool executor.

    Attributes:
        batch_size:     Maximum number of events to buffer before flushing.
        _pending:       Internal buffer of pending ``EventRecordORM`` objects.
    """

    def __init__(self, batch_size: int = 100) -> None:
        """
        Initialise the audit log writer.

        Args:
            batch_size: Number of events to accumulate before a batch flush.
                        Set to 1 to disable batching (write immediately).
        """
        self.batch_size = batch_size
        self._pending: list[EventRecordORM] = []
        logger.info(
            "AuditLogWriter initialised | batch_size=%d",
            batch_size,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def write(self, event: EventRecord) -> bool:
        """
        Write a single event to the audit log immediately.

        Duplicate events (same ``event_id``) are silently ignored.

        Args:
            event: The ``EventRecord`` to persist.

        Returns:
            ``True`` if the event was written, ``False`` if it was a duplicate
            or if the write failed (error is logged but not re-raised).
        """
        from observability.metrics import (
            AUDIT_LOG_WRITES_TOTAL,
            AUDIT_LOG_WRITE_LATENCY_SECONDS,
        )

        orm_record = self._to_orm(event)
        start = time.monotonic()

        try:
            with SessionLocal() as session:
                self._upsert_record(session, orm_record)
                session.commit()

            latency = time.monotonic() - start
            AUDIT_LOG_WRITES_TOTAL.labels(status="success").inc()
            AUDIT_LOG_WRITE_LATENCY_SECONDS.observe(latency)

            logger.debug(
                "Audit log write | event_id=%s event_type=%s latency=%.3fs",
                event.event_id,
                event.event_type,
                latency,
            )
            return True

        except IntegrityError:
            # Duplicate event_id — silently ignore
            logger.debug(
                "Duplicate event_id skipped | event_id=%s",
                event.event_id,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="duplicate").inc()
            return False

        except OperationalError as exc:
            logger.error(
                "Database connection error writing audit log | event_id=%s error=%s",
                event.event_id,
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc()
            return False

        except SQLAlchemyError as exc:
            logger.error(
                "SQLAlchemy error writing audit log | event_id=%s error=%s",
                event.event_id,
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc()
            return False

        except Exception as exc:
            logger.error(
                "Unexpected error writing audit log | event_id=%s error=%s",
                event.event_id,
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc()
            return False

    def write_batch(self, events: list[EventRecord]) -> tuple[int, int]:
        """
        Write a batch of events to the audit log in a single transaction.

        Duplicate events within the batch or already in the database are
        silently skipped.

        Args:
            events: List of ``EventRecord`` objects to persist.

        Returns:
            A tuple of ``(written_count, skipped_count)``.
        """
        from observability.metrics import (
            AUDIT_LOG_WRITES_TOTAL,
            AUDIT_LOG_WRITE_LATENCY_SECONDS,
        )

        if not events:
            return 0, 0

        orm_records = [self._to_orm(e) for e in events]
        start = time.monotonic()
        written = 0
        skipped = 0

        try:
            with SessionLocal() as session:
                for record in orm_records:
                    try:
                        self._upsert_record(session, record)
                        written += 1
                    except IntegrityError:
                        session.rollback()
                        skipped += 1
                        logger.debug(
                            "Duplicate event_id skipped in batch | event_id=%s",
                            record.event_id,
                        )
                session.commit()

            latency = time.monotonic() - start
            AUDIT_LOG_WRITES_TOTAL.labels(status="success").inc(written)
            if skipped:
                AUDIT_LOG_WRITES_TOTAL.labels(status="duplicate").inc(skipped)
            AUDIT_LOG_WRITE_LATENCY_SECONDS.observe(latency)

            logger.info(
                "Audit log batch write | written=%d skipped=%d latency=%.3fs",
                written,
                skipped,
                latency,
            )
            return written, skipped

        except SQLAlchemyError as exc:
            logger.error(
                "SQLAlchemy error in batch audit log write | count=%d error=%s",
                len(events),
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc(len(events))
            return 0, len(events)

        except Exception as exc:
            logger.error(
                "Unexpected error in batch audit log write | count=%d error=%s",
                len(events),
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc(len(events))
            return 0, len(events)

    def buffer(self, event: EventRecord) -> None:
        """
        Add an event to the internal buffer.

        Automatically flushes when the buffer reaches ``batch_size``.

        Args:
            event: The ``EventRecord`` to buffer.
        """
        self._pending.append(self._to_orm(event))
        if len(self._pending) >= self.batch_size:
            self.flush_buffer()

    def flush_buffer(self) -> tuple[int, int]:
        """
        Flush all buffered events to the database.

        Returns:
            A tuple of ``(written_count, skipped_count)``.
        """
        if not self._pending:
            return 0, 0

        from observability.metrics import (
            AUDIT_LOG_WRITES_TOTAL,
            AUDIT_LOG_WRITE_LATENCY_SECONDS,
        )

        records = self._pending[:]
        self._pending.clear()
        start = time.monotonic()
        written = 0
        skipped = 0

        try:
            with SessionLocal() as session:
                for record in records:
                    try:
                        self._upsert_record(session, record)
                        written += 1
                    except IntegrityError:
                        session.rollback()
                        skipped += 1
                session.commit()

            latency = time.monotonic() - start
            AUDIT_LOG_WRITES_TOTAL.labels(status="success").inc(written)
            if skipped:
                AUDIT_LOG_WRITES_TOTAL.labels(status="duplicate").inc(skipped)
            AUDIT_LOG_WRITE_LATENCY_SECONDS.observe(latency)

            logger.debug(
                "Audit log buffer flush | written=%d skipped=%d latency=%.3fs",
                written,
                skipped,
                latency,
            )
            return written, skipped

        except SQLAlchemyError as exc:
            logger.error(
                "SQLAlchemy error flushing audit log buffer | count=%d error=%s",
                len(records),
                exc,
            )
            AUDIT_LOG_WRITES_TOTAL.labels(status="failure").inc(len(records))
            return 0, len(records)

    # ─────────────────────────────────────────────────────────────────────────
    # Query helpers
    # ─────────────────────────────────────────────────────────────────────────

    def query_by_workflow(
        self,
        workflow_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventRecordORM]:
        """
        Query audit log entries for a specific workflow.

        Args:
            workflow_id: The workflow UUID to filter by.
            limit:       Maximum number of records to return.
            offset:      Number of records to skip (for pagination).

        Returns:
            List of ``EventRecordORM`` instances ordered by ``occurred_at`` ASC.
        """
        with SessionLocal() as session:
            stmt = (
                select(EventRecordORM)
                .where(EventRecordORM.workflow_id == workflow_id)
                .order_by(EventRecordORM.occurred_at.asc())
                .limit(limit)
                .offset(offset)
            )
            result = session.execute(stmt)
            return list(result.scalars().all())

    def query_by_task(
        self,
        task_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventRecordORM]:
        """
        Query audit log entries for a specific task.

        Args:
            task_id: The task UUID to filter by.
            limit:   Maximum number of records to return.
            offset:  Number of records to skip.

        Returns:
            List of ``EventRecordORM`` instances ordered by ``occurred_at`` ASC.
        """
        with SessionLocal() as session:
            stmt = (
                select(EventRecordORM)
                .where(EventRecordORM.task_id == task_id)
                .order_by(EventRecordORM.occurred_at.asc())
                .limit(limit)
                .offset(offset)
            )
            result = session.execute(stmt)
            return list(result.scalars().all())

    def query_by_agent(
        self,
        agent_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventRecordORM]:
        """
        Query audit log entries for a specific agent.

        Args:
            agent_id: The agent UUID to filter by.
            limit:    Maximum number of records to return.
            offset:   Number of records to skip.

        Returns:
            List of ``EventRecordORM`` instances ordered by ``occurred_at`` ASC.
        """
        with SessionLocal() as session:
            stmt = (
                select(EventRecordORM)
                .where(EventRecordORM.agent_id == agent_id)
                .order_by(EventRecordORM.occurred_at.asc())
                .limit(limit)
                .offset(offset)
            )
            result = session.execute(stmt)
            return list(result.scalars().all())

    def query_by_event_type(
        self,
        event_type: str,
        *,
        limit: int = 100,
        offset: int = 0,
        since: datetime | None = None,
    ) -> list[EventRecordORM]:
        """
        Query audit log entries by event type.

        Args:
            event_type: The event type discriminator string.
            limit:      Maximum number of records to return.
            offset:     Number of records to skip.
            since:      Optional lower bound on ``occurred_at``.

        Returns:
            List of ``EventRecordORM`` instances ordered by ``occurred_at`` DESC.
        """
        with SessionLocal() as session:
            stmt = select(EventRecordORM).where(
                EventRecordORM.event_type == event_type
            )
            if since is not None:
                stmt = stmt.where(EventRecordORM.occurred_at >= since)
            stmt = (
                stmt.order_by(EventRecordORM.occurred_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = session.execute(stmt)
            return list(result.scalars().all())

    def count_events(
        self,
        *,
        event_type: str | None = None,
        topic: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """
        Count audit log entries matching optional filters.

        Args:
            event_type: Optional event type filter.
            topic:      Optional topic filter.
            since:      Optional lower bound on ``occurred_at``.

        Returns:
            Integer count of matching records.
        """
        from sqlalchemy import func

        with SessionLocal() as session:
            stmt = select(func.count(EventRecordORM.event_id))
            if event_type is not None:
                stmt = stmt.where(EventRecordORM.event_type == event_type)
            if topic is not None:
                stmt = stmt.where(EventRecordORM.topic == topic)
            if since is not None:
                stmt = stmt.where(EventRecordORM.occurred_at >= since)
            result = session.execute(stmt)
            return result.scalar_one() or 0

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_orm(event: EventRecord) -> EventRecordORM:
        """
        Convert an ``EventRecord`` Pydantic model to an ``EventRecordORM`` instance.

        Args:
            event: The domain event to convert.

        Returns:
            An ``EventRecordORM`` ready for database insertion.
        """
        return EventRecordORM(
            event_id=event.event_id,
            event_type=str(event.event_type),
            topic=str(event.topic),
            source=str(event.source),
            workflow_id=event.workflow_id,
            task_id=event.task_id,
            agent_id=event.agent_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            severity=str(event.severity),
            payload=dict(event.payload),
            event_metadata=dict(event.metadata),
            occurred_at=event.occurred_at,
            schema_version=event.schema_version,
        )

    @staticmethod
    def _upsert_record(session: Session, record: EventRecordORM) -> None:
        """
        Insert a record, ignoring duplicates (idempotent upsert).

        Uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` when available,
        falling back to a plain ``session.merge()`` for other databases.

        Args:
            session: Active SQLAlchemy session.
            record:  The ORM record to insert.
        """
        try:
            # Try PostgreSQL-specific upsert first
            stmt = pg_insert(EventRecordORM).values(
                event_id=record.event_id,
                event_type=record.event_type,
                topic=record.topic,
                source=record.source,
                workflow_id=record.workflow_id,
                task_id=record.task_id,
                agent_id=record.agent_id,
                correlation_id=record.correlation_id,
                causation_id=record.causation_id,
                severity=record.severity,
                payload=record.payload,
                event_metadata=record.event_metadata,
                occurred_at=record.occurred_at,
                schema_version=record.schema_version,
            ).on_conflict_do_nothing(index_elements=["event_id"])
            session.execute(stmt)
        except Exception:
            # Fallback: use merge for non-PostgreSQL databases (e.g. SQLite in tests)
            session.merge(record)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

#: Module-level singleton writer instance.
_writer: AuditLogWriter | None = None


def get_audit_log_writer(batch_size: int = 1) -> AuditLogWriter:
    """
    Return the module-level singleton ``AuditLogWriter``.

    Creates the instance on first call.  Subsequent calls return the same
    instance regardless of the ``batch_size`` argument.

    Args:
        batch_size: Batch size for the writer (only used on first call).

    Returns:
        The singleton ``AuditLogWriter`` instance.
    """
    global _writer
    if _writer is None:
        _writer = AuditLogWriter(batch_size=batch_size)
    return _writer
