#!/usr/bin/env python3
"""
Ingest the cadence report (data/cadence.jsonl) into the `cadence` table.

The producer (central-bank-corpus's cadence watchdog) atomically regenerates
the file as a FULL snapshot: one line per (bank_code, doc_type) series with a
frozen field set. This ingester mirrors that semantic — each run replaces the
table's rows for its corpus in one transaction.

Lines carrying a `corpus` field that contradicts the service's CORPUS are
skipped with a WARNING, as manifest lines are: a foreign corpus's snapshot
mounted by mistake is never relabeled as this one's.

Guards: a missing file is a no-op (normal on deployments without a cadence
producer); an existing file yielding zero valid rows does NOT replace the
table (an implausible mass disappearance is treated as a torn input, not
truth — same philosophy as ingest.py's sweep guard).

The watchdog's private state file (cadence_state.jsonl) never enters the
vault; ingest.py excludes both basenames from the documents manifest scan.
"""

import json
import logging
import os

from psycopg2.extras import execute_values

from common import resolve_corpus

log = logging.getLogger("ingest_cadence")

# Producer-contract fields -> everything else goes into `extra` (JSONB).
# bank_code stays listed: the producer emits it (mapped to source_code),
# and it must not fall into extra. corpus is checked against the service's
# CORPUS (resolve_corpus) and stored in the corpus column, never in extra.
KNOWN_FIELDS = {
    "corpus", "bank_code", "source_code", "doc_type", "last", "interval_days",
    "next_expected", "days_until", "status", "expected_per_year", "n_3y",
}

CREATE_CADENCE_SQL = """
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
"""

INSERT_CADENCE_SQL = """
INSERT INTO cadence (
    corpus, source_code, doc_type, last, interval_days, next_expected,
    days_until, status, expected_per_year, n_3y, updated_at, extra
) VALUES %s
"""


def parse_cadence_line(line, source_file, line_num, run_ts, default_corpus, counters=None):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        return None

    source_code = obj.get("source_code") or obj.get("bank_code")
    doc_type = obj.get("doc_type")
    if not source_code or not doc_type:
        log.warning(
            f"Skipping line without source_code/doc_type "
            f"({source_file}:{line_num})"
        )
        return None

    # Same rule as manifest lines (ingest.parse_line): a corpus field that
    # contradicts the service's means another corpus's snapshot is mounted
    # here by mistake -- skip the line rather than relabel it (vault #6).
    corpus = resolve_corpus(obj.get("corpus"), default_corpus)
    if corpus is None:
        log.warning(
            f"Skipping line ({source_file}:{line_num}): corpus "
            f"{obj.get('corpus')!r} contradicts this service's {default_corpus!r}"
        )
        if counters is not None:
            counters["corpus_conflict"] = counters.get("corpus_conflict", 0) + 1
        return None

    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}

    return (
        corpus,
        source_code,
        doc_type,
        obj.get("last") or None,
        obj.get("interval_days"),
        obj.get("next_expected") or None,
        obj.get("days_until"),
        obj.get("status"),
        obj.get("expected_per_year"),
        obj.get("n_3y"),
        run_ts,
        json.dumps(extra) if extra else None,
    )


def load_cadence_rows(path, run_ts, default_corpus):
    """Parse every line of a cadence snapshot file into insert-ready rows."""
    rows = []
    counters = {"corpus_conflict": 0}
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            row = parse_cadence_line(line, path, i, run_ts, default_corpus, counters)
            if row:
                rows.append(row)
    if counters["corpus_conflict"]:
        log.warning(
            f"Rejected {counters['corpus_conflict']} cadence line(s) with a "
            f"corpus contradicting this service's {default_corpus!r}"
        )
    return rows


def run(conn, data_dir, default_corpus, run_ts):
    """Replace this corpus's cadence rows with the current snapshot.

    Returns the number of rows written (0 when skipped). The DELETE and the
    INSERTs share one transaction: a failure mid-replace rolls back to the
    previous snapshot.
    """
    path = os.environ.get("CADENCE_PATH") or os.path.join(
        data_dir, "cadence.jsonl"
    )
    if not os.path.exists(path):
        log.info(f"No cadence snapshot at {path} — skipping")
        return 0

    rows = load_cadence_rows(path, run_ts, default_corpus)
    if not rows:
        log.warning(
            f"{path} yielded 0 valid rows — leaving the cadence table "
            "untouched (possible torn/partial regeneration)"
        )
        return 0

    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_CADENCE_SQL)
            cur.execute(
                "DELETE FROM cadence WHERE corpus = %s", (default_corpus,)
            )
            execute_values(cur, INSERT_CADENCE_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info(f"cadence: replaced snapshot with {len(rows)} row(s)")
    return len(rows)
