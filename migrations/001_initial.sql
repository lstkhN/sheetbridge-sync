-- Target durable repository schema (PostgreSQL).
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TYPE sync_job_status AS ENUM ('pending', 'processing', 'done', 'dlq');
CREATE TABLE sync_jobs (
  id UUID PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL,
  status sync_job_status NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  lease_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sync_jobs_ready_idx ON sync_jobs (run_at, created_at) WHERE status = 'pending';
CREATE INDEX sync_jobs_lease_idx ON sync_jobs (lease_until) WHERE status = 'processing';
-- Workers claim atomically using SELECT ... FOR UPDATE SKIP LOCKED in a transaction;
-- expired leases are returned to pending before the next claim.
