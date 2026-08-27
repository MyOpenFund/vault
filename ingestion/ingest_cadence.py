#!/usr/bin/env python3
"""
Ingest the cadence report (data/cadence.jsonl) into the `cadence` table.

The producer (central-bank-corpus's cadence watchdog) atomically regenerates
the file as a FULL snapshot: one line per (bank_code, doc_type) series with a
frozen field set. This ingester mirrors that semantic — each run replaces the
table's rows for its corpus in one transaction.

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
from datetime import datetime, timezone

from psycopg2.extras import execute_values

log = logging.getLogger("ingest_cadence")

# Producer-contract fields -> everything else goes into `extra` (JSONB).
# bank_code stays listed: the producer emits it (mapped to source_code),
# and it must not fall into extra.
KNOWN_FIELDS = {
    "bank_code", "source_code", "doc_type", "last", "interval_days",
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


def parse_cadence_line(line, source_file, line_num, run_ts, default_corpus):
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

    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}

    return (
        default_corpus,
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
