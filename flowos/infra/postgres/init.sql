-- =============================================================================
-- FlowOS — PostgreSQL Initialisation Script
-- =============================================================================
-- This script runs once when the PostgreSQL container is first created.
-- It sets up the database, roles, extensions, and baseline schema objects.
--
-- Executed by: docker-entrypoint-initdb.d/init.sql
-- PostgreSQL version: 16+
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Extensions
-- -----------------------------------------------------------------------------
-- Enable UUID generation (used as primary keys throughout FlowOS)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pgcrypto for password hashing utilities
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enable pg_trgm for fuzzy text search on workflow/task names
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Enable btree_gin for composite GIN indexes
CREATE EXTENSION IF NOT EXISTS "btree_gin";

-- -----------------------------------------------------------------------------
-- 2. Roles
-- -----------------------------------------------------------------------------

-- Application role — used by the FlowOS API and worker services.
-- Has full DML access but cannot modify schema.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flowos_app') THEN
        CREATE ROLE flowos_app
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION;
        RAISE NOTICE 'Role flowos_app created.';
    ELSE
        RAISE NOTICE 'Role flowos_app already exists — skipping.';
    END IF;
END
$$;

-- Migration role — used by Alembic to apply schema changes.
-- Has DDL privileges in addition to DML.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flowos_migration') THEN
        CREATE ROLE flowos_migration
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION;
        RAISE NOTICE 'Role flowos_migration created.';
    ELSE
        RAISE NOTICE 'Role flowos_migration already exists — skipping.';
    END IF;
END
$$;

-- Read-only role — used by analytics, reporting, and observability tools.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flowos_readonly') THEN
        CREATE ROLE flowos_readonly
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION;
        RAISE NOTICE 'Role flowos_readonly created.';
    ELSE
        RAISE NOTICE 'Role flowos_readonly already exists — skipping.';
    END IF;
END
$$;

-- Grant the application role to the main flowos user so it inherits privileges.
GRANT flowos_app TO flowos;
GRANT flowos_migration TO flowos;

-- -----------------------------------------------------------------------------
-- 3. Database configuration
-- -----------------------------------------------------------------------------

-- Set default search path for the flowos user
ALTER USER flowos SET search_path TO public;

-- Set timezone to UTC for all connections
ALTER DATABASE flowos SET timezone TO 'UTC';

-- Set default transaction isolation level
ALTER DATABASE flowos SET default_transaction_isolation TO 'read committed';

-- Optimise for OLTP workloads
ALTER DATABASE flowos SET synchronous_commit TO 'on';

-- -----------------------------------------------------------------------------
-- 4. Schema
-- -----------------------------------------------------------------------------

-- Use the public schema (default). In production you may want a dedicated
-- schema (e.g. CREATE SCHEMA flowos;) but public keeps Alembic simple.

-- Grant schema usage to roles
GRANT USAGE ON SCHEMA public TO flowos_app;
GRANT USAGE ON SCHEMA public TO flowos_readonly;
GRANT ALL ON SCHEMA public TO flowos_migration;

-- Default privileges: any table created by flowos_migration is accessible
-- to flowos_app (DML) and flowos_readonly (SELECT).
ALTER DEFAULT PRIVILEGES FOR ROLE flowos_migration IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO flowos_app;

ALTER DEFAULT PRIVILEGES FOR ROLE flowos_migration IN SCHEMA public
    GRANT SELECT ON TABLES TO flowos_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE flowos_migration IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO flowos_app;

ALTER DEFAULT PRIVILEGES FOR ROLE flowos_migration IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO flowos_readonly;

-- -----------------------------------------------------------------------------
-- 5. Baseline utility functions
-- -----------------------------------------------------------------------------

-- Function: updated_at trigger
-- Automatically updates the `updated_at` column on row modification.
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW() AT TIME ZONE 'UTC';
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION set_updated_at() IS
    'Trigger function that sets updated_at to the current UTC timestamp on UPDATE.';

-- Function: generate_flowos_id(prefix)
-- Generates a human-readable prefixed ID (e.g. wf_<uuid>, task_<uuid>).
CREATE OR REPLACE FUNCTION generate_flowos_id(prefix TEXT)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN prefix || '_' || replace(gen_random_uuid()::TEXT, '-', '');
END;
$$;

COMMENT ON FUNCTION generate_flowos_id(TEXT) IS
    'Generates a prefixed unique ID string. Example: generate_flowos_id(''wf'') → wf_<uuid>.';

-- -----------------------------------------------------------------------------
-- 6. Alembic version table (pre-create to avoid permission issues)
-- -----------------------------------------------------------------------------
-- Alembic will manage this table, but we ensure it can be created by the
-- flowos user without needing superuser privileges.

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

COMMENT ON TABLE alembic_version IS
    'Alembic migration version tracking table. Managed by Alembic — do not modify manually.';

-- Grant access to the app role
GRANT SELECT, INSERT, UPDATE, DELETE ON alembic_version TO flowos_app;

-- -----------------------------------------------------------------------------
-- 7. Verification
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    ext_count INT;
    role_count INT;
BEGIN
    SELECT COUNT(*) INTO ext_count
    FROM pg_extension
    WHERE extname IN ('uuid-ossp', 'pgcrypto', 'pg_trgm', 'btree_gin');

    SELECT COUNT(*) INTO role_count
    FROM pg_roles
    WHERE rolname IN ('flowos_app', 'flowos_migration', 'flowos_readonly');

    RAISE NOTICE '=== FlowOS PostgreSQL Initialisation Complete ===';
    RAISE NOTICE 'Extensions installed: %/4', ext_count;
    RAISE NOTICE 'Roles created: %/3', role_count;
    RAISE NOTICE 'Database: flowos | Timezone: UTC | Encoding: UTF8';
    RAISE NOTICE '=================================================';
END
$$;
