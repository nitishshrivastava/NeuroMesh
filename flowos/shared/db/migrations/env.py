"""
shared/db/migrations/env.py — Alembic Migration Environment

This module is loaded by Alembic when running migration commands.  It
configures the migration context with:

- The synchronous database URL from ``shared.config.settings``
- The shared ``Base.metadata`` so Alembic can auto-detect model changes
- Both online (live DB) and offline (SQL script generation) migration modes

All FlowOS ORM models must be imported here so that Alembic can detect
schema changes via ``--autogenerate``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ─────────────────────────────────────────────────────────────────────────────
# Alembic Config object — provides access to alembic.ini values
# ─────────────────────────────────────────────────────────────────────────────
config = context.config

# Set up Python logging from the alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# ─────────────────────────────────────────────────────────────────────────────
# Import shared Base and all ORM models
# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: We import Base directly from the module file (not via __init__.py)
# to avoid triggering the async engine creation at module load time.
# The shared.db.__init__.py imports the async engine which requires asyncpg
# and cannot be used in sync-only Alembic migration contexts.
#
# We use importlib to load the base module directly, bypassing __init__.py.

import importlib.util as _importlib_util

def _load_base_directly() -> type:
    """
    Load the Base class directly from shared/db/base.py without triggering
    the shared.db package __init__.py (which creates async engines).
    """
    # Find the base.py file relative to this env.py
    # env.py is at: shared/db/migrations/env.py
    # base.py is at: shared/db/base.py
    import pathlib
    migrations_dir = pathlib.Path(__file__).parent
    db_dir = migrations_dir.parent
    base_path = db_dir / "base.py"

    spec = _importlib_util.spec_from_file_location("shared.db.base_direct", base_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load base.py from {base_path}")

    module = _importlib_util.module_from_spec(spec)
    # Register in sys.modules to avoid double-loading
    sys.modules["shared.db.base_direct"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.Base  # type: ignore[attr-defined]


# Load Base — this may fail if asyncpg is not installed (async engine creation).
# In that case, fall back to a minimal Base that only has the metadata.
try:
    from shared.db.base import Base  # noqa: E402
except (ImportError, Exception) as _base_exc:
    logger.warning(
        "Could not import Base via shared.db (error: %s). "
        "Attempting direct module load to bypass async engine creation.",
        _base_exc,
    )
    try:
        Base = _load_base_directly()
    except Exception as _direct_exc:
        logger.error("Direct Base load also failed: %s", _direct_exc)
        # Last resort: create a minimal Base with empty metadata
        from sqlalchemy import MetaData
        from sqlalchemy.orm import DeclarativeBase

        _NAMING_CONVENTION = {
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }

        class Base(DeclarativeBase):  # type: ignore[no-redef]
            metadata = MetaData(naming_convention=_NAMING_CONVENTION)

# ── Core ORM models ──────────────────────────────────────────────────────────
# Import all ORM model classes so their tables are registered in Base.metadata.
# These imports must happen AFTER Base is defined.
from shared.db.models import (  # noqa: F401, E402
    AgentCapabilityORM,
    AgentORM,
    CheckpointFileORM,
    CheckpointORM,
    EventRecordORM,
    HandoffORM,
    ReasoningSessionORM,
    ReasoningStepORM,
    TaskAttemptORM,
    TaskORM,
    ToolCallORM,
    WorkflowORM,
    WorkspaceORM,
)

target_metadata = Base.metadata

# ─────────────────────────────────────────────────────────────────────────────
# Database URL resolution
# ─────────────────────────────────────────────────────────────────────────────


def get_database_url() -> str:
    """
    Resolve the database URL for Alembic migrations.

    Priority order:
    1. DATABASE_SYNC_URL environment variable (preferred for CI/CD)
    2. DATABASE_URL environment variable (stripped of async driver prefix)
    3. sqlalchemy.url from alembic.ini (fallback)

    Returns:
        A synchronous PostgreSQL connection URL string.
    """
    # 1. Explicit sync URL
    sync_url = os.environ.get("DATABASE_SYNC_URL")
    if sync_url:
        logger.debug("Using DATABASE_SYNC_URL from environment.")
        return sync_url

    # 2. Async URL — strip the asyncpg driver to get a sync URL
    async_url = os.environ.get("DATABASE_URL")
    if async_url:
        sync_url = async_url.replace(
            "postgresql+asyncpg://", "postgresql://"
        ).replace(
            "postgresql+psycopg://", "postgresql://"
        )
        logger.debug("Derived sync URL from DATABASE_URL.")
        return sync_url

    # 3. Try to load from shared settings (may fail if deps not installed)
    try:
        from shared.config import settings  # noqa: PLC0415

        logger.debug("Using DATABASE_SYNC_URL from shared.config.settings.")
        return settings.database.sync_url
    except ImportError:
        pass

    # 4. Fall back to alembic.ini value
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        logger.debug("Using sqlalchemy.url from alembic.ini.")
        return ini_url

    raise RuntimeError(
        "No database URL found. Set DATABASE_SYNC_URL or DATABASE_URL "
        "environment variable, or configure sqlalchemy.url in alembic.ini."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Offline migration mode
# ─────────────────────────────────────────────────────────────────────────────


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    In offline mode, Alembic generates SQL scripts without connecting to the
    database.  This is useful for reviewing changes before applying them or
    for generating SQL to run in restricted environments.

    Usage:
        alembic -c shared/db/alembic.ini upgrade head --sql > migration.sql
    """
    url = get_database_url()
    logger.info("Running offline migrations against: %s", url)

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include schema in migration scripts
        include_schemas=False,
        # Compare server defaults to detect changes
        compare_server_default=True,
        # Compare type changes
        compare_type=True,
        # Render item types as module-qualified names
        render_as_batch=False,
        # Transaction per migration
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ─────────────────────────────────────────────────────────────────────────────
# Online migration mode
# ─────────────────────────────────────────────────────────────────────────────


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In online mode, Alembic connects to the database and applies migrations
    directly.  This is the standard mode for ``alembic upgrade head``.
    """
    url = get_database_url()
    logger.info("Running online migrations against: %s", url)

    # Override the sqlalchemy.url in the config so engine_from_config uses it
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # Use NullPool for migrations to avoid connection leaks
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare server defaults to detect changes
            compare_server_default=True,
            # Compare type changes (e.g. VARCHAR(50) → VARCHAR(100))
            compare_type=True,
            # Include all schemas
            include_schemas=False,
            # Render item types as module-qualified names for readability
            render_as_batch=False,
            # Transaction per migration for safety
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    logger.info("Online migrations completed successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
