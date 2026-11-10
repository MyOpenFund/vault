-- Frozen copy of the DDL train as of master 7642aa5 (before the #4 gate).
-- Used to build a pre-gate database; never edit it.
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
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'documents' AND column_name = 'bank_code'
    ) THEN
        ALTER TABLE documents RENAME COLUMN bank_code TO source_code;
    END IF;
END $$;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS corpus TEXT;
UPDATE documents SET corpus = 'central-bank' WHERE corpus IS NULL;
ALTER TABLE documents ALTER COLUMN corpus SET NOT NULL;

-- One-time upgrade of pre-existing naive TIMESTAMP columns to TIMESTAMPTZ
-- (values were written as UTC); no-op once migrated or on fresh databases.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'documents' AND column_name = 'updated_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE documents
            ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC',
            ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC';
    END IF;
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
