#!/usr/bin/env python3
"""
Ingest .jsonl document inventories into PostgreSQL.

Finds every .jsonl under DATA_DIR (recursively), parses each line and
upserts it into the `documents` table keyed on doc_id. Documents belong
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

After the documents pass, the run also ingests DATA_DIR/cadence.jsonl into
the `cadence` table as a full replace (see ingest_cadence.py for the
transactional semantics).
"""

import json
import os
import sys
import glob
import logging
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values
import ingest_cadence
import ingest_runs

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

-- Facts feeding the RAG OCR policy. Filled by the orchestrator's probe pass
-- (its UPDATE is their only writer). Deliberately absent from KNOWN_FIELDS and
-- from the manifest upsert: manifests never carry them, and upserting them
-- would null probed values on every nightly run.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS has_text_layer BOOLEAN;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS page_count INTEGER;

-- RAG ingestion state: current-state row per (doc_id, collection), upserted by
-- the RAG orchestrator over plain SQL (INSERT ... ON CONFLICT DO UPDATE).
-- Cross-model history lives in the collection dimension (one fresh collection
-- per re-embed campaign). The vault owns this DDL; the orchestrator only needs
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
-- data/runs.jsonl handoff; the RAG orchestrator writes directly). Append-only:
-- ingestion is INSERT ... ON CONFLICT (run_id) DO NOTHING, so producer-side
-- file rotation is always safe.
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
    updated_at = EXCLUDED.updated_at,
    last_seen_at = EXCLUDED.last_seen_at,
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


# Files the documents scan must never treat as manifests: the cadence report
# (ingested by ingest_cadence.py into its own table), the cadence watchdog's
# private state file (never enters the vault), and the runs telemetry report
# (ingested by ingest_runs.py into its own table).
EXCLUDED_BASENAMES = frozenset({"cadence.jsonl", "cadence_state.jsonl", "runs.jsonl"})


def find_jsonl_files(root):
    return sorted(
        p
        for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
        if os.path.basename(p) not in EXCLUDED_BASENAMES
    )


def resolve_corpus(manifest_value, default_corpus):
    """Resolve a line's corpus against the service's expectation.

    Absent manifest field -> the service default applies. Present and
    equal -> accepted (self-describing manifest; the env var acts as a
    consistency check). Present and different -> None, meaning the line
    must be rejected: this service is being fed another corpus's
    manifests, and guessing would corrupt the registry.
    """
    if manifest_value is None:
        return default_corpus
    if manifest_value == default_corpus:
        return manifest_value
    return None


def parse_line(line, source_file, line_num, run_ts, default_corpus, counters=None):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        return None

    doc_id = obj.get("doc_id")
    if not doc_id:
        log.warning(f"Skipping line without doc_id ({source_file}:{line_num})")
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


def main():
    database_url = os.environ.get("DATABASE_URL")
    data_dir = os.environ.get("DATA_DIR", "/data")
    default_corpus = os.environ.get("CORPUS", "central-bank")
    sweep_max_fraction = float(os.environ.get("SWEEP_MAX_DELETE_FRACTION", "0.05"))

    if not database_url:
        log.error("DATABASE_URL is not set")
        sys.exit(1)

    jsonl_files = find_jsonl_files(data_dir)
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
    counters = {"corpus_conflict": 0}

    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        total_rows = 0
        for filepath in jsonl_files:
            rows = []
            with open(filepath, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, start=1):
                    row = parse_line(line, filepath, i, run_ts, default_corpus, counters)
                    if row:
                        rows.append(row)

            if not rows:
                log.info(f"{filepath}: 0 valid lines")
                continue

            with conn.cursor() as cur:
                execute_values(cur, UPSERT_SQL, rows, page_size=500)
            conn.commit()

            total_rows += len(rows)
            log.info(f"{filepath}: upserted {len(rows)} rows")

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
                log.warning(
                    f"SWEEP BLOCKED: {candidates}/{live} live rows "
                    f"({candidates / live:.1%}) would be soft-deleted, above "
                    f"SWEEP_MAX_DELETE_FRACTION={sweep_max_fraction:.0%} "
                    "— possible torn/partial share sync. If this deletion "
                    "wave is legitimate, re-run with a higher fraction."
                )
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
        runs_rows = ingest_runs.run(conn, data_dir)

        log.info(
            f"Done — processed {total_rows} document rows, "
            f"{cadence_rows} cadence rows, and {runs_rows} run-report rows"
        )

    except Exception:
        conn.rollback()
        log.exception(
            "Run failed; uncommitted work rolled back "
            "(committed document batches are unaffected)"
        )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
