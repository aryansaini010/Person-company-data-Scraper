-- Entity Store (PostgreSQL). Professional public info ONLY — no inferred attributes.
-- Parity target: prospect_intel/store.py SCHEMA (SQLite zero-ops stand-in).
-- SQLite->Postgres mapping: TEXT stays TEXT, REAL epoch -> TIMESTAMPTZ,
-- JSON TEXT -> JSONB, FTS5 docs_fts -> tsvector column + GIN index (see below).
CREATE TABLE IF NOT EXISTS persons (
  id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  company TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  human_confirmed BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS firmographics (
  company TEXT PRIMARY KEY,
  profile JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Fetched docs (DMZ intake). text is full scrubbed body; search via tsvector.
CREATE TABLE IF NOT EXISTS docs (
  doc_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_class TEXT NOT NULL,
  text TEXT NOT NULL,
  search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS docs_search_idx ON docs USING GIN (search_tsv);
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  brief_id TEXT NOT NULL,
  text TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  section_index INT NOT NULL,
  char_start INT NOT NULL,
  char_end INT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('supported','partially_supported','unsupported')),
  note TEXT NOT NULL DEFAULT ''
);
-- One-way DMZ->core queue. received=FALSE = pending; UNIQUE prevents dupes.
CREATE TABLE IF NOT EXISTS queue (
  seq BIGSERIAL PRIMARY KEY,
  doc_id TEXT NOT NULL,
  received BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (doc_id, received)
);
CREATE INDEX IF NOT EXISTS queue_received_idx ON queue (received);
-- Pitch collateral (Pass 4 input). id is slug/stable key.
CREATE TABLE IF NOT EXISTS collateral (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL
);
-- Rendered briefs (JSONB Brief). claims rows mirror strategy_signals.
CREATE TABLE IF NOT EXISTS briefs (
  id TEXT PRIMARY KEY,
  data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS briefs_created_idx ON briefs (created_at);
-- Human-gate sessions (multi-worker safe): JSONB session payload + expiry.
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 minutes'
);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);
-- Participant uploads (CSV/XLSX parsed rows, Phone/Mobile dropped on parse).
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL DEFAULT '',
  rows_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '1 hour'
);
CREATE INDEX IF NOT EXISTS uploads_expires_idx ON uploads (expires_at);
-- Tier-1 enrichment cache (30d TTL enforced in app) + usage quotas.
CREATE TABLE IF NOT EXISTS enrich_cache (
  source TEXT NOT NULL,
  ckey TEXT NOT NULL,
  payload JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, ckey)
);
CREATE TABLE IF NOT EXISTS enrich_usage (
  source TEXT NOT NULL,
  bucket TEXT NOT NULL,
  calls INT NOT NULL DEFAULT 0,
  PRIMARY KEY (source, bucket)
);
-- Immutable audit log mirrored from run_log.jsonl for SQL traceability.
-- Hash-chained: each row links prev_hash, tamper-evident (see store.audit_chain).
CREATE TABLE IF NOT EXISTS audit_log (
  seq BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  event TEXT NOT NULL,
  payload JSONB NOT NULL,
  prev_hash TEXT NOT NULL DEFAULT 'GENESIS',
  hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_event_idx ON audit_log (event);
