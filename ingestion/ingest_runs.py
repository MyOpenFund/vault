#!/usr/bin/env python3
"""
Ingest run-reports (data/runs.jsonl) into the `runs` table.

Producers append one JSON line per run (central-bank-corpus writes the file;
the RAG orchestrator writes the table directly). This ingester is APPEND-ONLY:
`INSERT ... ON CONFLICT (run_id) DO NOTHING`, so re-ingesting the same file is
a no-op and file rotation on the producer side is always safe.

A missing file is a no-op (deployments without a producer). Corrupt lines are
skipped with a warning. Unknown fields land in `extra` (JSONB).
"""

import json
import logging
import os

from psycopg2.extras import execute_values

log = logging.getLogger("ingest_runs")

KNOWN_FIELDS = {
    "run_id", "tool", "command", "started_at", "finished_at",
    "outcome", "exit_code", "totals", "sources",
}

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
    run_id = obj.get("run_id")
    tool = obj.get("tool")
    if not run_id or not tool:
        log.warning(f"Skipping line without run_id/tool ({source_file}:{line_num})")
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
            execute_values(cur, INSERT_RUNS_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info(f"runs: offered {len(rows)} row(s) (append-only, dupes ignored)")
    return len(rows)
