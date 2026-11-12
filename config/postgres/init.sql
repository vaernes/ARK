-- =============================================================================
-- ARK PostgreSQL 16 Initialization Script
-- Executed automatically on first container startup against POSTGRES_DB.
-- =============================================================================

-- Enable extensions useful for full-text search and UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Tune current database defaults for multi-user Open WebUI session & RBAC concurrency
SELECT format('ALTER DATABASE %I SET timezone TO ''UTC''', current_database()) \gexec
SELECT format('ALTER DATABASE %I SET lock_timeout TO ''10s''', current_database()) \gexec
SELECT format('ALTER DATABASE %I SET idle_in_transaction_session_timeout TO ''60s''', current_database()) \gexec

-- Create dedicated databases for Keycloak (Identity Broker) and ark-admin (Control Plane & Audit)
SELECT 'CREATE DATABASE keycloak'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec

SELECT 'CREATE DATABASE ark_admin'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ark_admin')\gexec
