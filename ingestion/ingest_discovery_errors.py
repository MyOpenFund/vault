#!/usr/bin/env python3
"""
Ingest the discovery-error trail (data/discovery_errors.jsonl) into the
`discovery_errors` table.

The producer (central-bank-corpus, cb_corpus/adapters/base.py) appends one
JSON line per failed discovery fetch — {"bank", "context", "url", "error"} —
and NEVER truncates or rotates the file. It is therefore a full-history
snapshot, not a delta, and this ingester is snapshot-shaped: the whole file
is folded into one row per fingerprint and `occurrences` is ASSIGNED (the
count in the file), never incremented, so re-ingesting an unchanged file is
a no-op. A producer that later emits `ts`, `run_id`, `http_status`,
`error_class` or `doc_type` is picked up without a schema change.

Guards (same doctrine as ingest_cadence.py): a missing file is a no-op; a
file yielding zero valid rows leaves the table untouched; a file that
SHRANK below DISCOVERY_ERRORS_MIN_RETAIN_FRACTION of the rows held for this
corpus leaves the table untouched (the producer never truncates, so a
shrink is a torn read, not a fix — set the fraction to 0.0 to accept a
legitimate rotation). Corrupt lines are skipped with a warning and counted,
including type-corrupt ones (a dict where a TEXT column expects a string):
the file is append-only, so one unadaptable value would otherwise fail the
service every night forever. Both tunables are read from the environment at
call time, so a `docker compose run -e ...` override actually applies.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from psycopg2.extras import execute_values

from common import resolve_corpus

log = logging.getLogger("ingest_discovery_errors")

# Producer-contract fields (today's four plus the ones chantier T may add);
# everything else goes into `extra`. `bank` maps to source_code, `corpus`
# is a column on every table and never extra.
KNOWN_FIELDS = {
    "bank", "source_code", "corpus", "doc_type", "context", "url", "error",
    "error_class", "http_status", "run_id", "ts",
}

# Fields that land in a TEXT column verbatim. A producer bug emitting a dict or
# a list here would reach execute_values as an unadaptable Python object ("can't
# adapt type 'dict'"), blow up the batch and make run() re-raise — and because
# the trail file is append-only, that one line would fail the service every
# night forever. They are type-checked before any row is built (`corpus` is not
# listed: resolve_corpus already rejects anything that is not the expected
# string, `url` has its own non-empty-string check).
STRING_FIELDS = ("bank", "source_code", "context", "doc_type", "error_class")

# "ReadTimeout: pool timed out" -> "ReadTimeout"; dotted paths allowed.
ERROR_CLASS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s")
FINGERPRINT_FIELDS = ("corpus", "source_code", "context", "url", "error_class")

# Tunables — documented defaults, overridable per deployment. The environment
# is read at CALL time, never at import time: the runbook's
# `docker compose run --rm -e DISCOVERY_ERRORS_MIN_RETAIN_FRACTION=0.0 ingestion`
# override must reach the guard, and an import-time constant would freeze the
# value the container started with.
DEFAULT_MAX_ERROR_CHARS = 2000
DEFAULT_MIN_RETAIN_FRACTION = 0.5


def _max_error_chars():
    # Floored at 1: a truncation tunable must never be able to blank the column
    # it truncates (DISCOVERY_ERROR_MAX_CHARS=0 would empty every message, and
    # the trail is the only record of what failed). A non-numeric value is
    # still a hard error — a typo must not silently fall back to the default —
    # but the message names the variable so the operator knows what to fix.
    value = os.environ.get("DISCOVERY_ERROR_MAX_CHARS") or DEFAULT_MAX_ERROR_CHARS
    try:
        return max(1, int(value))
    except ValueError:
        raise ValueError(
            f"DISCOVERY_ERROR_MAX_CHARS must be an integer, got {value!r}"
        ) from None


def _min_retain_fraction():
    return float(
        os.environ.get("DISCOVERY_ERRORS_MIN_RETAIN_FRACTION") or DEFAULT_MIN_RETAIN_FRACTION
    )

# Discovery-failure table, owned and populated by this module. Included here
# too (verbatim, indexes included) so this module stays usable standalone,
# mirroring ingest.py's DDL train — the duplication between the two is
# deliberate, not drift (see ingest_runs.py for the same pattern).
CREATE_ERRORS_SQL = """
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
"""

UPSERT_ERRORS_SQL = """
INSERT INTO discovery_errors (
    fingerprint, corpus, source_code, doc_type, context, url, error_class,
    error, http_status, first_run_id, last_run_id, first_seen_at,
    last_seen_at, seen_at_is_ingestion_time, occurrences, extra
) VALUES %s
ON CONFLICT (fingerprint) DO UPDATE SET
    doc_type      = EXCLUDED.doc_type,
    error         = EXCLUDED.error,
    http_status   = EXCLUDED.http_status,
    first_run_id  = coalesce(discovery_errors.first_run_id, EXCLUDED.first_run_id),
    last_run_id   = coalesce(EXCLUDED.last_run_id, discovery_errors.last_run_id),
    first_seen_at = LEAST(discovery_errors.first_seen_at, EXCLUDED.first_seen_at),
    -- (last_seen_at, seen_at_is_ingestion_time) MOVE TOGETHER, ranked by the
    -- same key as the Python _latest_key: `NOT flag` is "has a producer
    -- stamp", so a producer stamp beats an ingestion-time fallback whatever
    -- the two values are, and otherwise the later stamp wins (Postgres row
    -- comparison, left to right). Taking the max of each column separately
    -- would keep a fallback stamp while flipping the flag to FALSE — an
    -- ingestion time labelled as producer time. Consequence: last_seen_at may
    -- move BACKWARDS exactly once, fallback -> real ts, which is a correction.
    last_seen_at = CASE WHEN (NOT EXCLUDED.seen_at_is_ingestion_time, EXCLUDED.last_seen_at)
                          >= (NOT discovery_errors.seen_at_is_ingestion_time, discovery_errors.last_seen_at)
                   THEN EXCLUDED.last_seen_at ELSE discovery_errors.last_seen_at END,
    seen_at_is_ingestion_time = CASE WHEN (NOT EXCLUDED.seen_at_is_ingestion_time, EXCLUDED.last_seen_at)
                          >= (NOT discovery_errors.seen_at_is_ingestion_time, discovery_errors.last_seen_at)
                   THEN EXCLUDED.seen_at_is_ingestion_time ELSE discovery_errors.seen_at_is_ingestion_time END,
    occurrences   = EXCLUDED.occurrences,
    extra         = EXCLUDED.extra
"""

HELD_ROWS_SQL = "SELECT count(*) FROM discovery_errors WHERE corpus = %s"


def split_error(raw):
    """('ReadTimeout: pool timed out') -> ('ReadTimeout', full message).
    An unparseable or empty message keeps error_class None — never guess."""
    if not raw:
        return None, ""
    m = ERROR_CLASS_RE.match(raw)
    return (m.group(1) if m else None), raw


def fingerprint(corpus, source_code, context, url, error_class):
    """sha256 hex of the five fields joined by '|' (None -> ''). Stable
    across runs and machines; the message text is deliberately excluded so
    retry rounds with varying detail collapse into one row."""
    parts = [corpus, source_code, context, url, error_class]
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()


def _parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _count(counters, key):
    if counters is not None:
        counters[key] = counters.get(key, 0) + 1


def parse_error_line(line, source_file, line_num, run_ts, default_corpus,
                     counters=None, max_error_chars=None):
    """One JSONL line -> one aggregation-ready record, or None (logged, counted).

    max_error_chars defaults to DISCOVERY_ERROR_MAX_CHARS resolved at call time.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        _count(counters, "invalid_lines")
        return None
    if not isinstance(obj, dict):
        log.warning(f"Skipping non-object line ({source_file}:{line_num})")
        _count(counters, "invalid_lines")
        return None
    url = obj.get("url")
    if not isinstance(url, str) or not url:
        log.warning(f"Skipping line without url ({source_file}:{line_num})")
        _count(counters, "invalid_lines")
        return None
    for field in STRING_FIELDS:
        value = obj.get(field)
        if value is not None and not isinstance(value, str):
            log.warning(
                f"Skipping line ({source_file}:{line_num}): {field} must be a "
                f"string or null, got {type(value).__name__}"
            )
            _count(counters, "invalid_lines")
            return None
    corpus = resolve_corpus(obj.get("corpus"), default_corpus)
    if corpus is None:
        log.warning(
            f"Skipping line ({source_file}:{line_num}): corpus "
            f"{obj.get('corpus')!r} contradicts this service's {default_corpus!r}"
        )
        _count(counters, "corpus_conflict")
        return None

    raw_error = obj.get("error")
    if raw_error is None or isinstance(raw_error, str):
        error_class, message = split_error(raw_error)
    else:
        # A structured error (the producer switching to an object, a bare
        # number, ...) must not collapse to "": keep it verbatim as JSON.
        # The class stays None — it is never guessed from a non-string.
        error_class, message = None, json.dumps(raw_error, ensure_ascii=False)
    if isinstance(obj.get("error_class"), str) and obj["error_class"]:
        error_class = obj["error_class"]
    if max_error_chars is None:
        max_error_chars = _max_error_chars()
    message = message[:max_error_chars]
    raw_ts = obj.get("ts")
    event_ts = _parse_ts(raw_ts)
    if raw_ts is not None and event_ts is None:
        # Present but unusable. Silently treating it as absent would hide a
        # producer regression behind a plausible-looking ingestion timestamp.
        log.warning(
            f"Unparseable ts {raw_ts!r} ({source_file}:{line_num}) — "
            "falling back to ingestion time"
        )
        _count(counters, "bad_ts")
    http_status = obj.get("http_status")
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        http_status = None
    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}
    return {
        "corpus": corpus,
        "source_code": obj.get("source_code") or obj.get("bank"),
        "doc_type": obj.get("doc_type"),
        "context": obj.get("context"),
        "url": url,
        "error_class": error_class,
        "error": message,
        "http_status": http_status,
        "run_id": obj.get("run_id") if isinstance(obj.get("run_id"), str) else None,
        "event_ts": event_ts or run_ts,
        "seen_at_is_ingestion_time": event_ts is None,
        "line_index": line_num,
        "extra": extra or None,
    }


def _latest_key(rec):
    """Ordering of "which record describes this fingerprint best".

    (ts_present, event_ts, line_index), highest wins:

    1. ts_present first, because a ts-less record's event_ts is INGESTION time
       (now) and would otherwise beat every real producer timestamp forever.
       At cutover — the producer starting to emit `ts` mid-file — the
       ts-carrying lines must win over the legacy ones they follow.
    2. event_ts among records of the same kind: the newest event wins.
    3. line_index breaks the remaining ties, which is every tie in a fully
       legacy file (all its records share run_ts): last line in the file wins.
    """
    return (not rec["seen_at_is_ingestion_time"], rec["event_ts"], rec.get("line_index", 0))


def aggregate(records):
    """Fold the WHOLE FILE into one row per fingerprint. occurrences = count
    in file (assigned), first_seen_at = min over all records; error,
    error_class, http_status, doc_type, extra, last_seen_at, last_run_id and
    seen_at_is_ingestion_time come from the latest record (see _latest_key),
    first_run_id from the earliest."""
    folded = {}
    for rec in records:
        fp = fingerprint(*(rec[f] for f in FINGERPRINT_FIELDS))
        cur = folded.get(fp)
        if cur is None:
            folded[fp] = {**rec, "fingerprint": fp, "occurrences": 1,
                          "first_seen_at": rec["event_ts"], "last_seen_at": rec["event_ts"],
                          "first_run_id": rec["run_id"], "last_run_id": rec["run_id"],
                          "latest_key": _latest_key(rec)}
            continue
        cur["occurrences"] += 1
        cur["first_seen_at"] = min(cur["first_seen_at"], rec["event_ts"])
        key = _latest_key(rec)
        if key >= cur["latest_key"]:
            cur["latest_key"] = key
            cur["last_seen_at"] = rec["event_ts"]
            for k in ("doc_type", "error", "http_status", "extra",
                      "seen_at_is_ingestion_time"):
                cur[k] = rec[k]
            cur["last_run_id"] = rec["run_id"] or cur["last_run_id"]
        if cur["first_run_id"] is None:
            cur["first_run_id"] = rec["run_id"]
    return [
        (r["fingerprint"], r["corpus"], r["source_code"], r["doc_type"], r["context"],
         r["url"], r["error_class"], r["error"], r["http_status"], r["first_run_id"],
         r["last_run_id"], r["first_seen_at"], r["last_seen_at"],
         r["seen_at_is_ingestion_time"], r["occurrences"],
         json.dumps(r["extra"]) if r["extra"] else None)
        for r in folded.values()
    ]


def load_error_rows(path, run_ts, default_corpus, counters=None, max_error_chars=None):
    if max_error_chars is None:
        max_error_chars = _max_error_chars()
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            rec = parse_error_line(line, str(path), i, run_ts, default_corpus,
                                   counters, max_error_chars)
            if rec:
                records.append(rec)
    return aggregate(records)


def should_replace(file_rows, held_rows, min_retain_fraction):
    """The shrink guard: refuse to overwrite when the file holds fewer than
    min_retain_fraction of the rows already stored for this corpus. 0.0
    disables the guard (operator override for a legitimate rotation)."""
    if held_rows == 0 or min_retain_fraction <= 0.0:
        return True
    return file_rows >= held_rows * min_retain_fraction


def run(conn, data_dir, default_corpus, run_ts):
    """Upsert the aggregated snapshot. Returns rows written (0 when skipped)."""
    path = os.environ.get("DISCOVERY_ERRORS_PATH") or os.path.join(data_dir, "discovery_errors.jsonl")
    if not os.path.exists(path):
        log.info(f"No discovery-error trail at {path} — skipping")
        return 0
    counters = {}
    rows = load_error_rows(path, run_ts, default_corpus, counters, _max_error_chars())
    for key, label in (("invalid_lines", "invalid"), ("corpus_conflict", "corpus-contradicting")):
        if counters.get(key):
            log.warning(f"discovery_errors: skipped {counters[key]} {label} line(s) in {path}")
    if counters.get("bad_ts"):
        log.warning(
            f"discovery_errors: {counters['bad_ts']} line(s) in {path} carry an "
            "unparseable ts — kept, with ingestion time as event time"
        )
    if not rows:
        log.warning(
            f"{path} yielded 0 valid rows — leaving discovery_errors untouched "
            "(possible torn/partial read)"
        )
        return 0
    min_retain_fraction = _min_retain_fraction()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_ERRORS_SQL)
            cur.execute(HELD_ROWS_SQL, (default_corpus,))
            (held,) = cur.fetchone()
            if not should_replace(len(rows), held, min_retain_fraction):
                log.warning(
                    f"discovery_errors: file holds {len(rows)} fingerprint(s) against "
                    f"{held} stored for '{default_corpus}' (below "
                    f"DISCOVERY_ERRORS_MIN_RETAIN_FRACTION={min_retain_fraction}) — "
                    "leaving the table untouched (the producer never truncates, so a "
                    "shrink is a torn read; set the fraction to 0.0 to accept a rotation)"
                )
                conn.rollback()
                return 0
            execute_values(cur, UPSERT_ERRORS_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info(f"discovery_errors: upserted {len(rows)} fingerprint(s) from {path}")
    return len(rows)
