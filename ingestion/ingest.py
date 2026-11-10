#!/usr/bin/env python3
"""
Ingest .jsonl document inventories into PostgreSQL.

Finds the manifest .jsonl files under DATA_DIR (find_manifest_files: the
data/ root layout reads manifest/**/*.jsonl + manifest.jsonl only, a DATA_DIR
that is the manifest directory itself is read recursively, MANIFEST_GLOBS
overrides), parses each line and upserts it into the `documents` table keyed
on doc_id. A doc_id repeated inside one file keeps its last valid line, with a
WARNING (vault #6). Documents belong
to a corpus: the service-level CORPUS env var provides the default, a
"corpus" field on a manifest line is checked against it, and a
contradiction rejects the line (this service is being fed another
corpus's manifests — never guess).

Idempotent: safe to re-run as often as needed (e.g. hourly cron after
an SMB share update) without creating duplicates.

Deletion tracking: every ingested row gets `last_seen_at` stamped with
the run timestamp. After a successful full pass, rows absent from all
manifests are soft-deleted (`deleted_at` set); rows that reappear are
resurrected (`deleted_at` cleared). Rows are never hard-deleted.

Torn-share protection: the sweep is skipped when the run yields zero
valid rows, and also when the fraction of live rows that would be
soft-deleted exceeds SWEEP_MAX_DELETE_FRACTION (default 0.05) — a
partially synced share exposing only a subset of the manifests must
not mass-soft-delete the corpus. Set the variable to 1.0 to force a
legitimate large deletion wave through. The sweep only ever touches rows
of this service's corpus.

Run-level mutual exclusion: the run holds two Postgres session-level
advisory locks on its own connection — a global one around the DDL train,
then a per-corpus one for the rest of the pass. An overlapping run of the
same corpus waits (it never skips); a run of a different corpus is never
blocked. See DDL_LOCK_KEY / corpus_lock_key below.

The DDL train is gated (vault #4, schema_gate.py): it drops and recreates
views and issues ALTER TABLEs, i.e. ACCESS EXCLUSIVE locks that queue behind
any open reader and make every new reader queue behind them. It runs only
when its text changed since it was last applied (sha256 recorded in
`vault_schema`), when a relation it owns is missing, or when VAULT_DDL_FORCE
is set; and when it runs, it does so under a bounded lock_timeout with
retries (VAULT_DDL_LOCK_TIMEOUT, VAULT_DDL_RETRY_DELAYS), failing the run
loudly -- nothing ingested -- if it never gets its locks.

The two least-privilege login roles and their grants (roles.py) are re-issued
on every run, train skipped or not (measured non-blocking): that keeps
password rotation "change the variable, run once".

After the documents pass, the run also ingests DATA_DIR/cadence.jsonl into
the `cadence` table as a full replace (see ingest_cadence.py for the
transactional semantics), DATA_DIR/runs.jsonl content-append-only into `runs`
(ingest_runs.py), and DATA_DIR/discovery_errors.jsonl into `discovery_errors`
as a fingerprint-keyed snapshot upsert (ingest_discovery_errors.py).
"""

import json
import math
import os
import re
import sys
import glob
import logging
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values
import ingest_cadence
import ingest_discovery_errors
import ingest_runs
import roles
import schema_gate
from common import resolve_corpus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ingest")

# "Known" schema fields -> everything else goes into `extra` (JSONB).
# bank_code stays listed: legacy manifests emit it (mapped to source_code),
# and it must not fall into extra.
KNOWN_FIELDS = {
    "doc_id", "corpus", "source_code", "bank_code", "doc_type", "title",
    "pdf_url", "source_url", "date", "year", "language", "provenance",
    "mime_type", "sha256", "local_path",
}

CREATE_TABLE_SQL = """
-- Gated: this whole text only runs when its sha256 differs from the one
-- recorded in vault_schema, when one of its relations is missing, or when
-- VAULT_DDL_FORCE is set (schema_gate.py, vault #4). Editing ANY character of
-- it, comments included, re-runs it once on the next pass.
--
-- Views first. They are dropped at the HEAD of the train and recreated at its
-- tail, in dependency order, so every ALTER below is view-safe (an
-- ALTER COLUMN ... TYPE on a column a view reads would otherwise fail) and so
-- the train stays idempotent under schema evolution (CREATE OR REPLACE VIEW
-- cannot drop a column). No CASCADE on purpose: an object outside this train
-- depending on a view must fail loudly, not vanish. Rule: nothing outside the
-- train may depend on these views (Metabase and the agent reference them by
-- name at query time, which is fine).
DROP VIEW IF EXISTS source_health;
DROP VIEW IF EXISTS sources_without_cadence;
DROP VIEW IF EXISTS rag_backlog_any;
DROP VIEW IF EXISTS rag_backlog;
DROP VIEW IF EXISTS runs_sources;

CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    doc_id TEXT UNIQUE NOT NULL,
    corpus TEXT NOT NULL,
    source_code TEXT,
    doc_type TEXT,
    title TEXT,
    pdf_url TEXT,
    source_url TEXT,
    date DATE,
    year INTEGER,
    language TEXT,
    provenance TEXT,
    mime_type TEXT,
    sha256 TEXT,
    local_path TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    extra JSONB
);

-- Upgrade path for databases created before deletion tracking existed.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Generalization migration: bank_code -> source_code + corpus backfill.
-- Idempotent; no-ops on fresh databases and on already-migrated ones.
-- The rename only happens when source_code is absent. When BOTH columns exist
-- (a half-applied migration, a column added by hand), renaming would fail and
-- dropping would lose data: source_code is filled from bank_code where it is
-- NULL, bank_code is KEPT for a human to inspect and drop (made nullable: the
-- upsert writes source_code only, so a NOT NULL bank_code would reject every
-- new document), and a WARNING says so. ingest.py also detects this state
-- from the catalog and logs it on every pass until bank_code is dropped.
DO $$
DECLARE
    has_bank   boolean;
    has_source boolean;
    filled     bigint;
BEGIN
    SELECT coalesce(bool_or(column_name = 'bank_code'), false),
           coalesce(bool_or(column_name = 'source_code'), false)
      INTO has_bank, has_source
      FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = 'documents'
       AND column_name IN ('bank_code', 'source_code');
    IF has_bank AND NOT has_source THEN
        ALTER TABLE documents RENAME COLUMN bank_code TO source_code;
    ELSIF has_bank AND has_source THEN
        ALTER TABLE documents ALTER COLUMN bank_code DROP NOT NULL;
        UPDATE documents SET source_code = bank_code
         WHERE source_code IS NULL AND bank_code IS NOT NULL;
        GET DIAGNOSTICS filled = ROW_COUNT;
        RAISE WARNING 'documents has both bank_code and source_code: source_code filled from bank_code on % row(s) where it was NULL; bank_code kept (drop it by hand once checked)', filled;
    END IF;
END $$;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS corpus TEXT;
UPDATE documents SET corpus = 'central-bank' WHERE corpus IS NULL;
ALTER TABLE documents ALTER COLUMN corpus SET NOT NULL;

-- One-time upgrade of pre-existing naive TIMESTAMP columns to TIMESTAMPTZ
-- (values were written as UTC); no-op once migrated or on fresh databases.
-- Each column is checked on its own: a database where created_at/updated_at
-- were already migrated but last_seen_at/deleted_at were added naive (or the
-- reverse) must converge too, not hinge on updated_at alone.
DO $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY ARRAY['created_at', 'updated_at', 'last_seen_at', 'deleted_at'] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'documents' AND column_name = col
              AND data_type = 'timestamp without time zone'
        ) THEN
            EXECUTE format(
                'ALTER TABLE documents ALTER COLUMN %I TYPE TIMESTAMPTZ USING %I AT TIME ZONE ''UTC''',
                col, col);
        END IF;
    END LOOP;
END $$;

DROP INDEX IF EXISTS idx_documents_bank_code;
CREATE INDEX IF NOT EXISTS idx_documents_source_code ON documents(source_code);
CREATE INDEX IF NOT EXISTS idx_documents_corpus_source ON documents(corpus, source_code);
CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_date ON documents(date);
CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(year);
CREATE INDEX IF NOT EXISTS idx_documents_language ON documents(language);
CREATE INDEX IF NOT EXISTS idx_documents_provenance ON documents(provenance);
CREATE INDEX IF NOT EXISTS idx_documents_deleted_at ON documents(deleted_at);

-- Live-aggregation index: the drill-down shape of the Metabase coverage
-- cards. Partial on the deleted_at IS NULL every card carries. Measured:
-- index-only scan, 3.2 -> 0.52 ms on a per-source aggregation at 40k rows.
-- It does NOT help the whole-corpus rollup (97% of rows are live -> seq
-- scan is correct).
CREATE INDEX IF NOT EXISTS idx_documents_live_agg
    ON documents (corpus, source_code, doc_type, year)
    WHERE deleted_at IS NULL;

-- doc_type is free text and collides across corpora (central-bank A1 = rate
-- decision, company A1 = annual report). The corpus-scoped query must be the
-- fast path. Unused while one corpus exists; 2x once two do.
CREATE INDEX IF NOT EXISTS idx_documents_corpus_doc_type
    ON documents (corpus, doc_type);

-- Containment lookups into `extra` (entity_key, date_precision, ...).
-- jsonb_path_ops: smaller and faster than the default opclass for the @>
-- queries we issue. NOTE: it does not accelerate `extra ->> 'k' = 'v'`; the
-- documented consumer contract is @> containment.
CREATE INDEX IF NOT EXISTS idx_documents_extra_gin
    ON documents USING GIN (extra jsonb_path_ops);

-- Facts feeding the RAG OCR policy. Filled by data-orchestrator's probe pass
-- (its UPDATE is their only writer). Deliberately absent from KNOWN_FIELDS and
-- from the manifest upsert: manifests never carry them, and upserting them
-- would null probed values on every nightly run.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS has_text_layer BOOLEAN;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS page_count INTEGER;

-- RAG ingestion state: current-state row per (doc_id, collection), upserted by
-- data-orchestrator over plain SQL (INSERT ... ON CONFLICT DO UPDATE).
-- Cross-model history lives in the collection dimension (one fresh collection
-- per re-embed campaign). The vault owns this DDL; data-orchestrator only needs
-- INSERT/UPDATE/SELECT rights.
CREATE TABLE IF NOT EXISTS rag_ingestions (
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
    collection        TEXT NOT NULL,
    corpus            TEXT NOT NULL,
    source_code       TEXT,
    embedding_model   TEXT NOT NULL,
    embedding_version TEXT,
    chunk_count       INTEGER NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, collection)
);

CREATE INDEX IF NOT EXISTS idx_rag_ingestions_collection
    ON rag_ingestions(collection);
CREATE INDEX IF NOT EXISTS idx_rag_ingestions_corpus_source
    ON rag_ingestions(corpus, source_code);

-- cadence table, owned and populated by ingest_cadence.py. Included here too
-- so the full target schema exists after any service run's DDL train, even
-- on a deployment with no cadence producer (e.g. a docs-only run before
-- cadence.jsonl ever shows up). ingest_cadence.run() also issues this same
-- CREATE TABLE IF NOT EXISTS so the module stays usable standalone; the
-- duplication between the two is deliberate, not drift.
CREATE TABLE IF NOT EXISTS cadence (
    corpus            TEXT NOT NULL,
    source_code       TEXT NOT NULL,
    doc_type          TEXT NOT NULL,
    last              DATE,
    interval_days     INTEGER,
    next_expected     DATE,
    days_until        INTEGER,
    status            TEXT,
    expected_per_year INTEGER,
    n_3y              INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL,
    extra             JSONB,
    PRIMARY KEY (corpus, source_code, doc_type)
);

-- Run telemetry: one row per producer run (central-bank-corpus via
-- data/runs.jsonl handoff; data-orchestrator writes directly). Append-only
-- for content: ingestion is INSERT ... ON CONFLICT (run_id) DO UPDATE SET
-- corpus = coalesce(runs.corpus, EXCLUDED.corpus), which rewrites no stored
-- column, so producer-side file rotation is always safe; `corpus` alone is
-- filled in when NULL (one-time repair of rows ingested before the column
-- existed, which the producer has never emitted and DO NOTHING never fixed). Owned and populated by ingest_runs.py.
-- Included here too so the full target schema exists after any service run's
-- DDL train, even on a deployment with no runs producer. ingest_runs.run()
-- also issues this same CREATE TABLE IF NOT EXISTS (index included) so the
-- module stays usable standalone; the duplication between the two is
-- deliberate, not drift.
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    command     TEXT,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    outcome     TEXT,
    exit_code   INTEGER,
    totals      JSONB,
    sources     JSONB,
    extra       JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_tool_finished ON runs(tool, finished_at);

-- Corpus attribution (2026-09-04). Producers writing runs.jsonl into a
-- corpus's data/ get the ingesting service's CORPUS; the data-orchestrator
-- is corpus-agnostic and leaves it NULL. Rows ingested before the column
-- existed carried `corpus` inside extra (unknown-field rule): backfilled.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS corpus TEXT;
-- The type guard matters: `extra ->> 'corpus'` renders a JSON number or
-- object as text, so an unguarded backfill would promote `{"corpus": 42}`
-- into a corpus named '42'. The value is otherwise taken as-is -- this is a
-- one-shot legacy path, not the ingestion path, so it deliberately does not
-- run resolve_corpus's contradiction check against the service's CORPUS.
UPDATE runs SET corpus = extra ->> 'corpus'
 WHERE corpus IS NULL AND jsonb_typeof(extra -> 'corpus') = 'string';
CREATE INDEX IF NOT EXISTS idx_runs_corpus_finished ON runs(corpus, finished_at);

-- Producer rename (2026-09-02): the RAG orchestrator's `tool` identity was
-- renamed from 'rag-orchestrator' to 'data-orchestrator'; rows it already
-- wrote under the old identity are relabeled so telemetry history isn't
-- split across two `tool` values for the same producer. No-op once applied,
-- and on any deployment that never saw the old identity.
UPDATE runs SET tool = 'data-orchestrator' WHERE tool = 'rag-orchestrator';

-- Discovery failures, ingested from the producer's data/discovery_errors.jsonl
-- by ingest_discovery_errors.py. The producer's file is append-only and is
-- never truncated, so it is a FULL HISTORY snapshot: the ingester aggregates
-- per fingerprint and ASSIGNS occurrences (never increments), which is what
-- makes re-ingestion idempotent.
--
-- The nullable columns (doc_type, error_class, http_status, first/last_run_id)
-- are filled the moment the producer emits them; until then
-- seen_at_is_ingestion_time stays TRUE and last_seen_at is an approximation.
-- resolved_at is NOT populated today: an unrotated file means a fixed URL's
-- old lines never disappear, so "not seen this run" is never true.
CREATE TABLE IF NOT EXISTS discovery_errors (
    fingerprint   TEXT PRIMARY KEY,  -- sha256(corpus|source_code|context|url|error_class)
    corpus        TEXT NOT NULL,
    source_code   TEXT,
    doc_type      TEXT,
    context       TEXT,
    url           TEXT,
    error_class   TEXT,
    error         TEXT,
    http_status   INTEGER,
    first_run_id  TEXT,
    last_run_id   TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    seen_at_is_ingestion_time BOOLEAN NOT NULL DEFAULT TRUE,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    resolved_at   TIMESTAMPTZ,
    extra         JSONB
);

CREATE INDEX IF NOT EXISTS idx_discovery_errors_corpus_source
    ON discovery_errors (corpus, source_code);
CREATE INDEX IF NOT EXISTS idx_discovery_errors_open
    ON discovery_errors (corpus, source_code, last_seen_at DESC)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_discovery_errors_last_run
    ON discovery_errors (last_run_id);

-- ---------------------------------------------------------------------------
-- Views, recreated at the tail of the train in dependency order (they were
-- dropped at its head, before the first ALTER — see the block above).

-- One row per (run, source). The base view every runs-shaped card and the
-- agent detector build on; no time window, so consumers choose their own.
-- Defensive casts: a producer writing a non-integer counter, an object where
-- an array belongs, or a NULL must not make the view raise.
CREATE VIEW runs_sources AS
SELECT r.run_id, r.tool, r.command, r.corpus,
       r.started_at, r.finished_at, r.outcome, r.exit_code,
       s ->> 'source_code' AS source_code,
       CASE WHEN s ->> 'docs_seen'    ~ '^-?[0-9]+$' THEN (s ->> 'docs_seen')::int    END AS docs_seen,
       CASE WHEN s ->> 'docs_new'     ~ '^-?[0-9]+$' THEN (s ->> 'docs_new')::int     END AS docs_new,
       CASE WHEN s ->> 'docs_failed'  ~ '^-?[0-9]+$' THEN (s ->> 'docs_failed')::int  END AS docs_failed,
       CASE WHEN s ->> 'fetch_errors' ~ '^-?[0-9]+$' THEN (s ->> 'fetch_errors')::int END AS fetch_errors,
       CASE WHEN jsonb_typeof(s -> 'truncated') = 'boolean'
            THEN (s ->> 'truncated')::boolean ELSE FALSE END AS truncated,
       CASE WHEN jsonb_typeof(s -> 'error_samples') = 'array'
            THEN s -> 'error_samples' ELSE '[]'::jsonb END AS error_samples
FROM runs r
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(r.sources) = 'array' THEN r.sources ELSE '[]'::jsonb END
) AS s;

-- Per-collection RAG backlog: one row per (document, collection) gap. This is
-- the campaign view -- during a re-embed, "missing" is relative to the NEW
-- collection and presence in the old one is irrelevant. EMPTY on a database
-- with no rag_ingestions rows (no collection to enumerate): see rag_backlog_any.
CREATE VIEW rag_backlog AS
WITH collections AS (SELECT DISTINCT collection FROM rag_ingestions)
SELECT c.collection,
       d.corpus, d.source_code, d.doc_type, d.doc_id, d.title, d.date, d.year,
       d.local_path, d.mime_type, d.pdf_url, d.source_url,
       d.page_count, d.has_text_layer,
       d.updated_at AS document_updated_at
FROM documents d
CROSS JOIN collections c
LEFT JOIN rag_ingestions r ON r.doc_id = d.doc_id AND r.collection = c.collection
WHERE d.deleted_at IS NULL AND r.doc_id IS NULL;

-- Absolute backlog: live documents in NO collection at all. Correct on a
-- fresh deployment. Pair any backlog card with a live-document count:
-- backlog 0 with documents 0 means "no data", not "done".
CREATE VIEW rag_backlog_any AS
SELECT d.corpus, d.source_code, d.doc_type, d.doc_id, d.title, d.date, d.year,
       d.local_path, d.mime_type, d.pdf_url, d.source_url,
       d.page_count, d.has_text_layer,
       d.updated_at AS document_updated_at
FROM documents d
WHERE d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM rag_ingestions r WHERE r.doc_id = d.doc_id);

-- Sources that produce runs but have no cadence expectation. Empty is
-- healthy; a non-empty result means source_health cannot see part of the
-- pipeline (it is built FROM cadence).
CREATE VIEW sources_without_cadence AS
SELECT rs.corpus, rs.source_code, max(rs.finished_at) AS last_run_at
FROM runs_sources rs
WHERE rs.corpus IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM cadence c
                   WHERE c.corpus = rs.corpus AND c.source_code = rs.source_code)
GROUP BY 1, 2;

-- Expected (cadence) x observed (runs) per series.
-- Grain: (corpus, source_code, doc_type) -- the cadence grain.
-- Every source_* column is SOURCE-grain (runs.sources carries no doc_type)
-- and is therefore repeated identically across a source's doc_type rows.
-- last_run_outcome is RUN-grain: a run is degraded if ANY source failed.
-- Windows are fixed at 7d (recent) / 90d (baseline) and named in the
-- columns; use runs_sources for any other window. No anomaly threshold here
-- on purpose -- the detector owns it as a documented, testable config value.
CREATE VIEW source_health AS
WITH obs_7d AS (
    SELECT corpus, source_code,
           count(*)                                                  AS runs_7d,
           count(*) FILTER (WHERE outcome IN ('degraded', 'failed'))  AS degraded_runs_7d,
           count(*) FILTER (WHERE truncated)                          AS truncated_runs_7d,
           count(*) FILTER (WHERE coalesce(docs_seen, 0) = 0)         AS zero_yield_runs_7d,
           sum(coalesce(docs_new, 0))                                 AS docs_new_7d,
           sum(coalesce(docs_seen, 0))                                AS docs_seen_7d,
           sum(coalesce(fetch_errors, 0))                             AS fetch_errors_7d
    FROM runs_sources
    WHERE finished_at >= now() - interval '7 days'
    GROUP BY 1, 2
),
base_90d AS (
    SELECT corpus, source_code, count(*) AS runs_90d,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(docs_new, 0))
               AS docs_new_median_per_run_90d
    FROM runs_sources
    WHERE finished_at >= now() - interval '90 days'
    GROUP BY 1, 2
),
last_run AS (
    SELECT DISTINCT ON (corpus, source_code)
           corpus, source_code, run_id, finished_at, outcome, truncated
    FROM runs_sources
    WHERE finished_at IS NOT NULL
    ORDER BY corpus, source_code, finished_at DESC, run_id DESC
),
open_errors AS (
    SELECT corpus, source_code,
           count(*)          AS open_discovery_errors,
           sum(occurrences)  AS open_discovery_attempts,
           max(last_seen_at) AS last_discovery_error_at
    FROM discovery_errors
    WHERE resolved_at IS NULL
    GROUP BY 1, 2
)
SELECT c.corpus, c.source_code, c.doc_type,
       c.interval_days                                          AS expected_interval_days,
       c.expected_per_year, c.n_3y,
       c.last                                                   AS last_document_date,
       c.next_expected, c.days_until,
       CASE WHEN c.days_until < 0 THEN -c.days_until
            WHEN c.days_until IS NULL THEN NULL
            ELSE 0 END                                           AS days_late,
       c.status                                                 AS cadence_status,
       c.updated_at                                             AS cadence_updated_at,
       lr.run_id                                    AS last_run_id,
       lr.finished_at                               AS last_run_at,
       lr.outcome                                   AS last_run_outcome,
       lr.truncated                                 AS last_run_truncated,
       coalesce(o.runs_7d, 0)                       AS source_runs_7d,
       coalesce(o.degraded_runs_7d, 0)              AS source_degraded_runs_7d,
       coalesce(o.truncated_runs_7d, 0)             AS source_truncated_runs_7d,
       coalesce(o.zero_yield_runs_7d, 0)            AS source_zero_yield_runs_7d,
       coalesce(o.docs_new_7d, 0)                   AS source_docs_new_7d,
       coalesce(o.docs_seen_7d, 0)                  AS source_docs_seen_7d,
       coalesce(o.fetch_errors_7d, 0)               AS source_fetch_errors_7d,
       coalesce(b.runs_90d, 0)                      AS source_runs_90d,
       b.docs_new_median_per_run_90d                AS source_docs_new_median_per_run_90d,
       coalesce(e.open_discovery_errors, 0)         AS source_open_discovery_errors,
       coalesce(e.open_discovery_attempts, 0)       AS source_open_discovery_attempts,
       e.last_discovery_error_at                    AS source_last_discovery_error_at
FROM cadence c
LEFT JOIN obs_7d     o  ON o.corpus  = c.corpus AND o.source_code  = c.source_code
LEFT JOIN base_90d   b  ON b.corpus  = c.corpus AND b.source_code  = c.source_code
LEFT JOIN last_run   lr ON lr.corpus = c.corpus AND lr.source_code = c.source_code
LEFT JOIN open_errors e ON e.corpus  = c.corpus AND e.source_code  = c.source_code;

-- The gate's record (schema_gate.py): one row, the sha256 of this text as it
-- was last applied. Last statement of the train; main() upserts the row in the
-- same transaction, so a train that fails half-way records nothing.
CREATE TABLE IF NOT EXISTS vault_schema (
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    fingerprint TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

TRAIN_FINGERPRINT = schema_gate.fingerprint(CREATE_TABLE_SQL)
EXPECTED_RELATIONS = schema_gate.expected_relations(CREATE_TABLE_SQL)

RECORD_FINGERPRINT_SQL = """
INSERT INTO vault_schema (singleton, fingerprint) VALUES (TRUE, %s)
ON CONFLICT (singleton) DO UPDATE
    SET fingerprint = EXCLUDED.fingerprint, applied_at = now();
"""

UPSERT_SQL = """
INSERT INTO documents (
    doc_id, corpus, source_code, doc_type, title, pdf_url, source_url,
    date, year, language, provenance, mime_type, sha256, local_path,
    updated_at, last_seen_at, extra
) VALUES %s
ON CONFLICT (doc_id) DO UPDATE SET
    corpus = EXCLUDED.corpus,
    source_code = EXCLUDED.source_code,
    doc_type = EXCLUDED.doc_type,
    title = EXCLUDED.title,
    pdf_url = EXCLUDED.pdf_url,
    source_url = EXCLUDED.source_url,
    date = EXCLUDED.date,
    year = EXCLUDED.year,
    language = EXCLUDED.language,
    provenance = EXCLUDED.provenance,
    mime_type = EXCLUDED.mime_type,
    sha256 = EXCLUDED.sha256,
    local_path = EXCLUDED.local_path,
    extra = EXCLUDED.extra,
    updated_at = EXCLUDED.updated_at,
    -- never rewind: a slower run carrying an older run timestamp must not make a live row look stale to a newer run's sweep (vault #3)
    last_seen_at = GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at),
    deleted_at = NULL;
"""

# Soft-delete rows not seen by this run (including legacy rows that were
# never stamped). Only executed after a successful, non-empty full pass
# that also clears the sweep-fraction guard. The sweep is scoped to the
# service's corpus to prevent cross-corpus deletions in a multi-corpus deployment.
SWEEP_SQL = """
UPDATE documents
SET deleted_at = %s, updated_at = %s
WHERE deleted_at IS NULL
  AND (last_seen_at IS NULL OR last_seen_at < %s)
  AND corpus = %s;
"""

SWEEP_COUNT_SQL = """
SELECT
    COUNT(*) FILTER (WHERE last_seen_at IS NULL OR last_seen_at < %s) AS candidates,
    COUNT(*) AS live
FROM documents
WHERE deleted_at IS NULL
  AND corpus = %s;
"""


def should_sweep(candidates, live, max_fraction):
    """Decide whether the soft-delete sweep is safe to run.

    Blocks the sweep when the fraction of live rows about to disappear
    exceeds max_fraction: a partially synced share exposing only a subset
    of the manifests looks exactly like a mass deletion, and must not be
    trusted. max_fraction=1.0 disables the guard (operator override).
    """
    if candidates == 0 or live == 0:
        return False
    return candidates / live <= max_fraction


# Run-level mutual exclusion (vault #3). Session-level advisory locks on the
# run's own connection: released on unlock or when the connection closes, so
# a killed run never leaves one behind. Two runs of the SAME corpus
# serialize; different corpora never block each other; the DDL train is
# serialized across every service because CREATE TABLE IF NOT EXISTS races
# on a fresh database.
DDL_LOCK_KEY = "vault-ddl"
CORPUS_LOCK_PREFIX = "vault-ingest-"


def corpus_lock_key(corpus):
    return f"{CORPUS_LOCK_PREFIX}{corpus}"


def acquire_lock(conn, key):
    """Take the advisory lock for `key`; wait (never skip) if another run holds it."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,))
        (got,) = cur.fetchone()
        if not got:
            log.warning(f"another ingestion run holds lock {key!r} — waiting for it")
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
    conn.commit()
    log.info(f"lock {key!r} acquired")


def release_lock(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        (released,) = cur.fetchone()
    conn.commit()
    if not released:
        log.warning(f"lock {key!r} was not held at release time")


class IngestSettingsError(ValueError):
    """An ingestion setting is invalid; the run exits 1 before connecting."""


class ManifestGlobsError(IngestSettingsError):
    """MANIFEST_GLOBS is malformed or reaches outside DATA_DIR."""


# Torn-share guard default (see should_sweep). 1.0 is the operator override.
SWEEP_MAX_DELETE_FRACTION_DEFAULT = 0.05


# The variable's grammar: plain ASCII decimal, optional "+" and exponent.
# Narrower than float(), which also takes "0_1", non-ASCII digits, "nan",
# "infinity" and a leading "-" (so "-0" is refused here, not read as 0).
_FRACTION_RE = re.compile(
    r"\+?(?P<mantissa>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_ASCII_SPACE = " \t\r\n"


def sweep_blocked_message(candidates, live, max_fraction):
    """The SWEEP BLOCKED warning. The threshold is printed as configured
    (a 0.002 setting used to read as "0%")."""
    return (
        f"SWEEP BLOCKED: {candidates}/{live} live rows "
        f"({candidates / live:.2%}) would be soft-deleted, above "
        f"SWEEP_MAX_DELETE_FRACTION={max_fraction!r} "
        "— possible torn/partial share sync. If this deletion "
        "wave is legitimate, re-run with a higher fraction."
    )


def parse_sweep_fraction(env):
    """Read SWEEP_MAX_DELETE_FRACTION from `env` (vault #6, F12).

    Unset or empty (e.g. `SWEEP_MAX_DELETE_FRACTION=` in .env) -> the default,
    with an INFO. Anything else must be a finite number in [0, 1]: a value
    outside it used to either crash the run (empty, garbage) or silently block
    every sweep forever (negative), so it now fails the run before connecting
    with a message naming the variable and the value. 0 = never sweep;
    1.0 = the guard is off (operator override).
    """
    raw = env.get("SWEEP_MAX_DELETE_FRACTION")
    if raw is None or not raw.strip(_ASCII_SPACE):
        log.info(
            f"SWEEP_MAX_DELETE_FRACTION unset or empty — using the default "
            f"{SWEEP_MAX_DELETE_FRACTION_DEFAULT}"
        )
        return SWEEP_MAX_DELETE_FRACTION_DEFAULT
    text = raw.strip(_ASCII_SPACE)
    match = _FRACTION_RE.fullmatch(text)
    value = float(text) if match else None
    # A non-zero mantissa that underflows to 0.0 ("1e-400") would silently
    # mean "never sweep"; refused like any other unrepresentable value.
    if value == 0.0 and any(c in "123456789" for c in match.group("mantissa")):
        value = None
    if value is None or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise IngestSettingsError(
            f"SWEEP_MAX_DELETE_FRACTION={raw!r} is invalid: expected a number "
            "between 0 and 1 (default 0.05; 1.0 disables the guard)"
        )
    return value


# How many repeated doc_ids the duplicate WARNING quotes (dedupe_rows). The
# count is always complete; only the sample is capped, to keep one glitched
# manifest from flooding the log.
DUPLICATE_SAMPLE_SIZE = 5


def dedupe_rows(rows, source_file, sample_size=DUPLICATE_SAMPLE_SIZE):
    """Keep one row per doc_id within one manifest file: the LAST one (vault #6).

    Two rows sharing a doc_id in one execute_values page make Postgres raise
    CardinalityViolation (ON CONFLICT DO UPDATE cannot affect a row twice) and
    kill the run; in different pages they went through silently. Last wins is
    what two upserts issued in line order would have stored. The kept rows are
    returned in the order of their (last) occurrence. doc_ids are compared
    exactly, as the UNIQUE constraint does (parse_line only lets non-empty
    strings through, so Python and Postgres equality agree). A WARNING gives
    the number of dropped lines, the number of doc_ids concerned and up to
    `sample_size` of them (first-seen order).
    """
    last = {}
    for i, row in enumerate(rows):
        last[row[0]] = i
    if len(last) == len(rows):
        return rows
    kept = [row for i, row in enumerate(rows) if last[row[0]] == i]
    repeated = []
    seen = set()
    for i, row in enumerate(rows):
        doc_id = row[0]
        if last[doc_id] != i and doc_id not in seen:
            seen.add(doc_id)
            repeated.append(doc_id)
    sample = ", ".join(repr(d) for d in repeated[:sample_size])
    more = ", …" if len(repeated) > sample_size else ""
    log.warning(
        f"{source_file}: dropped {len(rows) - len(kept)} duplicate line(s) for "
        f"{len(repeated)} doc_id(s) repeated in this file — the last valid line "
        f"of each wins (e.g. {sample}{more})"
    )
    return kept


# Which .jsonl files the documents pass reads (Spec W5). Two layouts exist:
#
# - Root layout: DATA_DIR is the corpus data/ ROOT (the repo's compose.yaml).
#   Detected by <DATA_DIR>/manifest being a directory (a symlink to one
#   counts). Only an allowlist is read: manifest/**/*.jsonl plus the legacy
#   monolithic data/manifest.jsonl. Every other root file -- cadence.jsonl,
#   runs.jsonl, the producer's private and audit files, and any file a
#   producer adds tomorrow -- is ignored without having to be named.
# - Manifest-dir layout: DATA_DIR is the manifest directory itself (the NAS,
#   where cadence/runs/discovery_errors come from the *_PATH variables on
#   another mount). Everything under it, recursively -- with the producer
#   basenames below still skipped, so this mode finds exactly what the scan
#   found before the allowlist existed.
# In both, a match that cannot be read is kept (and fails the run, see
# _glob_files); only directories are dropped.
#
# MANIFEST_GLOBS (comma-separated glob patterns relative to DATA_DIR, `**`
# recursive) overrides both and is taken literally (no basename filter).
# Hidden files never match (glob's default), as before.
MANIFEST_SUBDIR = "manifest"
ROOT_LAYOUT_GLOBS = ("manifest/**/*.jsonl", "manifest.jsonl")
MANIFEST_DIR_LAYOUT_GLOBS = ("**/*.jsonl",)

# Producer files that are not manifests, skipped by basename in both detected
# layouts (in the root layout, for one that lands inside manifest/ -- parity
# with the pre-allowlist scan); MANIFEST_GLOBS is taken literally:
# the cadence snapshot and the watchdog's private state (ingest_cadence.py),
# the runs telemetry (ingest_runs.py), the discovery-error trail
# (ingest_discovery_errors.py), and three producer audit files.
EXCLUDED_BASENAMES = frozenset({
    "cadence.jsonl", "cadence_state.jsonl", "runs.jsonl",
    "discovery_errors.jsonl", "download_errors.jsonl",
    "download_quarantine.jsonl", "wp_dates_index.jsonl",
})


def parse_manifest_globs(raw):
    """MANIFEST_GLOBS -> tuple of patterns, or None when unset/empty.

    Refused (ManifestGlobsError): an empty entry (`a,,b`, a trailing comma), an
    absolute pattern, or a `..` component -- the override selects files inside
    DATA_DIR, it never reaches outside it.
    """
    if raw is None or not raw.strip():
        return None
    patterns = tuple(p.strip() for p in raw.split(","))
    for pattern in patterns:
        if not pattern:
            raise ManifestGlobsError(
                f"MANIFEST_GLOBS={raw!r} is invalid: empty pattern "
                "(comma-separated globs relative to DATA_DIR)"
            )
        if os.path.isabs(pattern) or ".." in pattern.replace("\\", "/").split("/"):
            raise ManifestGlobsError(
                f"MANIFEST_GLOBS={raw!r} is invalid: {pattern!r} must be "
                "relative to DATA_DIR and stay inside it"
            )
    return patterns


def _glob_files(data_dir, patterns, excluded=frozenset()):
    found = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(data_dir, pattern), recursive=True):
            if os.path.basename(path) in excluded:
                continue
            # Only a directory is skipped. Anything else that matches stays in
            # the list even if it cannot be read (a dangling symlink, a file
            # the SMB mount denies or fails on): open() then raises and the
            # run aborts BEFORE the sweep. Skipping it here would make its
            # rows look deleted and the sweep would soft-delete them.
            if os.path.isdir(path):
                log.warning(f"manifest scan: {path} matches but is a directory — skipped")
                continue
            found.add(path)
    return sorted(found)


def _warn_unread_root_files(data_dir):
    """Root layout: name the *.jsonl files at the data/ root that are not read.

    The known producer files and the legacy manifest.jsonl are expected there;
    anything else may be a manifest written to the wrong place, so it is
    named rather than silently ignored.
    """
    expected = EXCLUDED_BASENAMES | {"manifest.jsonl"}
    unread = sorted(
        os.path.basename(p)
        for p in glob.glob(os.path.join(data_dir, "*.jsonl"))
        if os.path.basename(p) not in expected
    )
    if unread:
        log.warning(
            f"manifest scan: {len(unread)} .jsonl file(s) at the root of {data_dir} "
            f"are not read in the root layout (only manifest/**/*.jsonl and "
            f"manifest.jsonl are manifests): {', '.join(unread)}"
        )


def find_manifest_files(data_dir, env):
    """The manifest files of this run, sorted; the layout used is logged."""
    patterns = parse_manifest_globs(env.get("MANIFEST_GLOBS"))
    excluded = frozenset()
    subdir = os.path.join(data_dir, MANIFEST_SUBDIR)
    if patterns is not None:
        mode = "MANIFEST_GLOBS override"
    elif os.path.isdir(subdir):
        mode = f"root layout ({subdir}/ is a directory)"
        patterns = ROOT_LAYOUT_GLOBS
        excluded = EXCLUDED_BASENAMES
        _warn_unread_root_files(data_dir)
    else:
        if os.path.lexists(subdir):
            log.warning(
                f"manifest scan: {subdir} exists but is not a directory — "
                "treating DATA_DIR as the manifest directory itself"
            )
        mode = "manifest-dir layout (DATA_DIR is the manifest directory)"
        patterns = MANIFEST_DIR_LAYOUT_GLOBS
        excluded = EXCLUDED_BASENAMES
    files = _glob_files(data_dir, patterns, excluded)
    log.info(
        f"manifest scan under {data_dir}: {mode}, patterns "
        f"{', '.join(patterns)} — {len(files)} file(s)"
    )
    return files


# How much of a rejected doc_id the warning shows: a producer can write a
# huge list or object there, and the line is logged once per manifest line.
INVALID_DOC_ID_LOG_CHARS = 80


def parse_line(line, source_file, line_num, run_ts, default_corpus, counters=None):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        return None
    if not isinstance(obj, dict):
        log.warning(
            f"Skipping line ({source_file}:{line_num}): a JSON "
            f"{type(obj).__name__}, not an object"
        )
        return None

    # doc_id is the TEXT upsert key: only a non-empty string free of NUL
    # (which Postgres TEXT cannot hold) is one. Anything else is rejected, not
    # coerced: 5 and "5" would be one key to Postgres but two to dedupe_rows
    # (vault #6 crash again), 1 and true one to Python, [1] unhashable.
    doc_id = obj.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id or "\x00" in doc_id:
        shown = repr(doc_id)
        if len(shown) > INVALID_DOC_ID_LOG_CHARS:
            shown = shown[:INVALID_DOC_ID_LOG_CHARS] + "…"
        reason = "without doc_id" if doc_id is None else f"with invalid doc_id {shown}"
        log.warning(
            f"Skipping line {reason} ({source_file}:{line_num}): "
            "doc_id must be a non-empty string"
        )
        if counters is not None:
            counters["invalid_doc_id"] = counters.get("invalid_doc_id", 0) + 1
        return None

    corpus = resolve_corpus(obj.get("corpus"), default_corpus)
    if corpus is None:
        log.warning(
            f"Skipping line with contradicting corpus "
            f"({source_file}:{line_num}): manifest says "
            f"'{obj.get('corpus')}', this service ingests '{default_corpus}'"
        )
        if counters is not None:
            counters["corpus_conflict"] = counters.get("corpus_conflict", 0) + 1
        return None

    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}

    return (
        doc_id,
        corpus,
        obj.get("source_code") or obj.get("bank_code"),
        obj.get("doc_type"),
        obj.get("title"),
        obj.get("pdf_url"),
        obj.get("source_url"),
        obj.get("date") or None,
        obj.get("year"),
        obj.get("language"),
        obj.get("provenance"),
        obj.get("mime_type"),
        obj.get("sha256"),
        obj.get("local_path"),
        run_ts,  # updated_at
        run_ts,  # last_seen_at
        json.dumps(extra) if extra else None,
    )


# Shared catalog the roles' ALTER ROLE / CREATE ROLE write to: a session
# altering a vault role holds a lock on it, so the blocker listing includes it
# whenever roles are provisioned.
ROLES_CATALOG = "pg_authid"

BOTH_CODE_COLUMNS_SQL = """
SELECT count(*) = 2 FROM information_schema.columns
 WHERE table_schema = current_schema() AND table_name = 'documents'
   AND column_name IN ('bank_code', 'source_code')
"""


def warn_if_both_code_columns(conn):
    """F8 leftover: a catalog read (no table lock), logged on every pass.

    Not taken from the train's RAISE WARNING (a server notice the driver may
    drop, and emitted only on the night the train runs): the state is checked
    directly, so the WARNING repeats until a human drops bank_code.
    """
    with conn.cursor() as cur:
        cur.execute(BOTH_CODE_COLUMNS_SQL)
        (both,) = cur.fetchone()
    conn.commit()
    if both:
        log.warning(
            "documents has both bank_code and source_code: source_code was filled "
            "from bank_code where NULL and bank_code was kept (nullable). Check it, "
            "then drop it by hand: ALTER TABLE documents DROP COLUMN bank_code"
        )


def apply_schema(conn, gate, passwords):
    """Gate check, then (train if stale) + roles, under a bounded lock wait.

    Must run under the `vault-ddl` advisory lock. The train, the fingerprint
    record and the roles share one transaction: the grants must land after
    the views a train just recreated (#5), and a train that fails half-way
    must not record its fingerprint. The roles run on every pass, train or
    not, inside the same retry wrapper: their statements were measured not to
    wait behind readers, and should anything else ever hold them up, a
    bounded wait beats an open-ended queue.
    """
    stored, missing = schema_gate.read_state(conn, EXPECTED_RELATIONS)
    reason = schema_gate.stale_reason(stored, TRAIN_FINGERPRINT, missing, gate.force)
    if reason is None:
        log.info(f"schema current (fingerprint {TRAIN_FINGERPRINT[:12]}) — DDL train skipped")
    else:
        # Forced is WARNING: left set, it brings back the nightly convoy.
        level = logging.WARNING if gate.force else logging.INFO
        log.log(level, f"DDL train running: {reason} "
                       f"(lock_timeout {gate.lock_timeout}, up to {gate.attempts} attempt(s))")

    def apply(cur):
        if reason is not None:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(RECORD_FINGERPRINT_SQL, (TRAIN_FINGERPRINT,))
        roles.ensure_roles(cur, passwords)

    label = (f"{schema_gate.DEFAULT_LABEL} (DDL train + roles)" if reason is not None
             else f"{schema_gate.DEFAULT_LABEL} (roles only, DDL train skipped)")
    relations = EXPECTED_RELATIONS + ((ROLES_CATALOG,) if passwords else ())
    schema_gate.run_with_lock_retries(conn, apply, gate, relations, label=label)
    if reason is not None:
        log.info(f"DDL train applied; fingerprint {TRAIN_FINGERPRINT[:12]} recorded")
    warn_if_both_code_columns(conn)


def main(*, corpus=None, data_dir=None):
    """Run one ingestion pass.

    `corpus` and `data_dir` are a test seam: both default to None, in which
    case the CORPUS / DATA_DIR env vars decide as in production. They exist
    because the env is process-global, so concurrency tests cannot give two
    threads different corpora through it.
    """
    database_url = os.environ.get("DATABASE_URL")
    if data_dir is None:
        data_dir = os.environ.get("DATA_DIR", "/data")
    default_corpus = corpus if corpus is not None else os.environ.get("CORPUS", "central-bank")

    if not database_url:
        log.error("DATABASE_URL is not set")
        sys.exit(1)
    try:
        gate = schema_gate.load_settings(os.environ)
        sweep_max_fraction = parse_sweep_fraction(os.environ)
        jsonl_files = find_manifest_files(data_dir, os.environ)
    except (schema_gate.SettingsError, IngestSettingsError) as exc:
        log.error(str(exc))
        sys.exit(1)

    if jsonl_files:
        log.info(f"Found {len(jsonl_files)} .jsonl file(s); corpus default: {default_corpus}")
    else:
        # No manifests this run (e.g. a deployment fed only cadence.jsonl, or
        # a torn share sync). Do NOT exit: the DDL train and the cadence pass
        # still need to run. The documents loop below naturally no-ops on an
        # empty file list, total_rows stays 0, and the existing zero-rows
        # guard further down skips the sweep — the torn-share protection is
        # unchanged, it just now also covers "zero manifests found" rather
        # than exiting before ever reaching it.
        log.warning(f"No .jsonl manifest files found under {data_dir} — skipping the documents pass")

    run_ts = datetime.now(timezone.utc)
    counters = {"corpus_conflict": 0, "invalid_doc_id": 0}

    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    corpus_lock = corpus_lock_key(default_corpus)
    held = []
    try:
        # The DDL train races across services (CREATE/DROP VIEW, CREATE TABLE
        # IF NOT EXISTS), so it is serialized globally; the corpus lock is
        # taken only after the train released it, so a run waiting for its
        # corpus never blocks another corpus's DDL.
        acquire_lock(conn, DDL_LOCK_KEY)
        held.append(DDL_LOCK_KEY)
        apply_schema(conn, gate, roles.roles_from_env(os.environ))
        release_lock(conn, DDL_LOCK_KEY)
        held.remove(DDL_LOCK_KEY)

        acquire_lock(conn, corpus_lock)
        held.append(corpus_lock)

        total_rows = 0
        for filepath in jsonl_files:
            rows = []
            with open(filepath, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, start=1):
                    row = parse_line(line, filepath, i, run_ts, default_corpus, counters)
                    if row:
                        rows.append(row)
            rows = dedupe_rows(rows, filepath)

            if not rows:
                log.info(f"{filepath}: 0 valid lines")
                continue

            with conn.cursor() as cur:
                execute_values(cur, UPSERT_SQL, rows, page_size=500)
            conn.commit()

            total_rows += len(rows)
            log.info(f"{filepath}: upserted {len(rows)} rows")

        if counters["invalid_doc_id"]:
            log.warning(
                f"Rejected {counters['invalid_doc_id']} line(s) whose doc_id is "
                "missing or not a non-empty string"
            )
        if counters["corpus_conflict"]:
            log.warning(
                f"Rejected {counters['corpus_conflict']} line(s) with a "
                f"corpus contradicting this service's '{default_corpus}'"
            )

        if total_rows == 0:
            log.warning(
                "Run yielded 0 valid rows — skipping the soft-delete sweep "
                "(possible torn/partial share sync)"
            )
        else:
            with conn.cursor() as cur:
                cur.execute(SWEEP_COUNT_SQL, (run_ts, default_corpus))
                candidates, live = cur.fetchone()

            if candidates == 0:
                log.info("No rows to soft-delete")
            elif not should_sweep(candidates, live, sweep_max_fraction):
                log.warning(sweep_blocked_message(candidates, live, sweep_max_fraction))
            else:
                with conn.cursor() as cur:
                    cur.execute(SWEEP_SQL, (run_ts, run_ts, run_ts, default_corpus))
                    swept = cur.rowcount
                conn.commit()
                log.warning(
                    f"Soft-deleted {swept} row(s) no longer present in any "
                    "manifest (kept in DB with deleted_at set)"
                )

        cadence_rows = ingest_cadence.run(conn, data_dir, default_corpus, run_ts)
        runs_rows = ingest_runs.run(conn, data_dir, default_corpus)
        errors_rows = ingest_discovery_errors.run(
            conn, data_dir, default_corpus, run_ts
        )

        log.info(
            f"Done — processed {total_rows} document rows, "
            f"{cadence_rows} cadence rows, {runs_rows} run-report rows offered, "
            f"and {errors_rows} discovery-error fingerprints"
        )

    except Exception:
        # Best effort, like the finally below: when the run failed *because*
        # the connection dropped, rollback() raises InterfaceError of its own
        # and would replace the real cause — the operator would see
        # "connection already closed" and never the error that killed the run,
        # and the log.exception line below would never run either.
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 — never mask the original failure
            log.warning("rollback failed on a broken connection; closing it instead")
        log.exception(
            "Run failed; uncommitted work rolled back "
            "(committed document batches are unaffected)"
        )
        raise
    finally:
        # Best effort: the connection close below releases session-level
        # advisory locks anyway, so a failure here is a warning, not an error.
        for key in list(held):
            try:
                if not conn.closed:
                    conn.rollback()
                    release_lock(conn, key)
            except Exception:  # noqa: BLE001 — closing releases it anyway
                log.warning(f"could not release lock {key!r}; the connection close will")
        conn.close()


if __name__ == "__main__":
    main()
