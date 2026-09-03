#!/usr/bin/env python3
"""
Ingest run-reports (data/runs.jsonl) into the `runs` table.

Producers append one JSON line per run (central-bank-corpus writes the file;
data-orchestrator writes the table directly). This ingester is APPEND-ONLY:
`INSERT ... ON CONFLICT (run_id) DO NOTHING`, so re-ingesting the same file is
a no-op and file rotation on the producer side is always safe.

A missing file is a no-op (deployments without a producer). Corrupt lines are
skipped with a warning. Unknown fields land in `extra` (JSONB).
"""

import json
import logging
import os
import re

from psycopg2.extras import execute_values

log = logging.getLogger("ingest_runs")

KNOWN_FIELDS = {
    "run_id", "tool", "command", "started_at", "finished_at",
    "outcome", "exit_code", "totals", "sources",
}

# Liberal ISO-8601 prefix: "YYYY-MM-DD" + ("T" or " ") + "HH:MM:SS". A prefix
# match is intentionally enough — we're rejecting garbage like "never
# o'clock", not fully validating a timestamp.
ISO8601_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _shape_error(obj):
    """Return a reason string if `obj` doesn't match the expected run-report
    shape, else None.

    This is deliberately independent of what Postgres itself would accept:
    Postgres's timestamptz parser is permissive (it will happily take
    "yesterday"), so relying on it to catch type-corrupt fields means a
    JSON-valid-but-corrupt line only fails once it reaches execute_values —
    at which point the whole batch (good rows included) rolls back. And
    since runs.jsonl is append-only, that same bad line would poison every
    future ingestion cycle. Catching the corruption here, before any row is
    built, keeps a single bad line from ever reaching the batch insert.
    """
    if not isinstance(obj, dict):
        return "line is not a JSON object"

    run_id = obj.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return "run_id must be a non-empty string"

    tool = obj.get("tool")
    if not isinstance(tool, str) or not tool:
        return "tool must be a non-empty string"

    exit_code = obj.get("exit_code")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        return "exit_code must be an int or null"

    for field in ("started_at", "finished_at"):
        value = obj.get(field)
        if value is not None and (
            not isinstance(value, str) or not ISO8601_PREFIX_RE.match(value)
        ):
            return f"{field} must be null or an ISO-8601 timestamp string"

    outcome = obj.get("outcome")
    if outcome is not None and not isinstance(outcome, str):
        return "outcome must be a string or null"

    totals = obj.get("totals")
    if totals is not None and not isinstance(totals, dict):
        return "totals must be null or an object"

    sources = obj.get("sources")
    if sources is not None and not isinstance(sources, list):
        return "sources must be null or an array"

    return None


# Run telemetry table, owned and populated by this module. Included here too
# (verbatim, index included) so ingest_runs.run() stays usable standalone,
# mirroring ingest.py's DDL train — the duplication between the two is
# deliberate, not drift (see ingest_cadence.py for the same pattern).
CREATE_RUNS_SQL = """
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

INSERT_RUNS_SQL = """
INSERT INTO runs (
    run_id, tool, command, started_at, finished_at,
    outcome, exit_code, totals, sources, extra
) VALUES %s
ON CONFLICT (run_id) DO NOTHING
"""


def parse_run_line(line, source_file, line_num):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        return None
    run_id = obj.get("run_id") if isinstance(obj, dict) else None
    tool = obj.get("tool") if isinstance(obj, dict) else None
    if not run_id or not tool:
        log.warning(f"Skipping line without run_id/tool ({source_file}:{line_num})")
        return None
    shape_error = _shape_error(obj)
    if shape_error:
        log.warning(
            f"Skipping invalid line ({source_file}:{line_num}): {shape_error}"
        )
        return None
    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}
    return (
        run_id,
        tool,
        obj.get("command"),
        obj.get("started_at") or None,
        obj.get("finished_at") or None,
        obj.get("outcome"),
        obj.get("exit_code"),
        json.dumps(obj.get("totals")) if obj.get("totals") is not None else None,
        json.dumps(obj.get("sources")) if obj.get("sources") is not None else None,
        json.dumps(extra) if extra else None,
    )


def load_run_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            row = parse_run_line(line, path, i)
            if row:
                rows.append(row)
    return rows


def run(conn, data_dir):
    """Ingest the runs file append-only. Returns rows offered (dupes are no-ops)."""
    path = os.environ.get("RUNS_PATH") or os.path.join(data_dir, "runs.jsonl")
    if not os.path.exists(path):
        log.info(f"No runs file at {path} — skipping")
        return 0
    rows = load_run_rows(path)
    if not rows:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_RUNS_SQL)
            execute_values(cur, INSERT_RUNS_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info(f"runs: offered {len(rows)} row(s) (append-only, dupes ignored)")
    return len(rows)
