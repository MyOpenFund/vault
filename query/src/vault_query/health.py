"""Run telemetry and pipeline health, read from the views the DDL train owns."""
import datetime
import re

from psycopg2.extras import RealDictCursor

from .filters import InvalidQuery, check_int, check_text

RUN_COLUMNS = (
    "run_id", "tool", "command", "corpus", "source_code", "started_at", "finished_at",
    "outcome", "exit_code", "docs_seen", "docs_new", "docs_failed", "fetch_errors",
    "truncated", "error_samples",
)
ERROR_COLUMNS = (
    "fingerprint", "corpus", "source_code", "doc_type", "context", "url", "error_class",
    "error", "http_status", "first_seen_at", "last_seen_at", "seen_at_is_ingestion_time",
    "last_run_id", "occurrences",
)
# Run grain, straight from `runs`.
RECENT_RUN_COLUMNS = (
    "run_id", "tool", "command", "corpus", "started_at", "finished_at", "outcome",
    "exit_code", "totals",
)

# Documented defaults of the text bounds: a producer can write any number of
# error samples of any size, a reader (an MCP tool result) must stay bounded.
DEFAULT_MAX_ERROR_SAMPLES = 5
DEFAULT_MAX_TEXT_CHARS = 2000


def _where(corpus=None, source_code=None):
    clauses, params = [], []
    check_text("corpus", corpus)
    check_text("source_code", source_code)
    if corpus is not None:
        clauses.append("corpus = %s")
        params.append(corpus)
    if source_code is not None:
        clauses.append("source_code = %s")
        params.append(source_code)
    return clauses, params


def _fetch(conn, sql, params):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _check_at_least_one(**params):
    for name, value in params.items():
        check_int(name, value, 1)


# The `since` grammar, ASCII only: YYYY-MM-DD[THH:MM[:SS[.f{1,6}]][Z|+HH:MM|-HH:MM]],
# an offset only after a time. `datetime.fromisoformat` alone also takes week
# dates ("2026-W40-1"), offsets past 15:59 and offset minutes 60-99 (it
# normalises "+05:60" to +06:00), all of which Postgres rejects with a
# DataError. The regex carries every time-of-day and offset range itself (hour
# 00-23, minute and second 00-59, offset hour 00-19, offset minute 00-59), so
# the accepted set does not depend on a Python version; fromisoformat then
# only rules on the calendar (month 13, Feb 29 of a common year) and the offset
# magnitude is checked against POSTGRES_MAX_UTC_OFFSET.
_SINCE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(T([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]{1,6})?)?"
    r"(Z|[+-][01][0-9]:[0-5][0-9])?)?"
)
SINCE_GRAMMAR = "YYYY-MM-DD[THH:MM[:SS[.ffffff]][Z|+HH:MM|-HH:MM]]"

# Largest UTC offset Postgres accepts in a timestamptz literal: a fact of the
# server, not a setting. Measured on Postgres 16: "+15:59" and "+15:59:59" parse,
# "+16:00" raises InvalidTimeZoneDisplacementValue ("time zone displacement out
# of range"). A string offset is HH:MM, so for strings the bound is +-15:59.
POSTGRES_MAX_UTC_OFFSET = datetime.timedelta(hours=15, minutes=59, seconds=59)


def _offset_fits_postgres(value):
    """True if `value` (a datetime) is naive or has an offset Postgres can parse.

    psycopg2 renders an aware datetime's offset as +HH:MM[:SS[.ffffff]]: the
    magnitude must stay within POSTGRES_MAX_UTC_OFFSET and a sub-second part is
    a syntax error on the server (measured on Postgres 16, InvalidDatetimeFormat).
    """
    offset = value.utcoffset()
    if offset is None:
        return True
    return abs(offset) <= POSTGRES_MAX_UTC_OFFSET and offset.microseconds == 0


def _check_since(since):
    """None, a date or datetime, or a str in the `since` grammar; else InvalidQuery.

    A str must match SINCE_GRAMMAR exactly (ASCII digits, upper-case `T` and
    `Z`, no surrounding whitespace), name a real instant, and carry an offset of
    at most +-15:59. A `datetime.date` passes; an aware `datetime.datetime` must
    have an offset Postgres can parse (at most POSTGRES_MAX_UTC_OFFSET, whole
    seconds). Every value this accepts runs on Postgres without a DataError.

    Only validates: the caller's original value is what reaches SQL, so an ISO
    string keeps Postgres's own timestamptz parsing (offset, `Z`, naive = session
    time zone); a `datetime.date` is read as midnight in the session time zone.
    """
    if since is None:
        return
    if isinstance(since, datetime.datetime):
        if _offset_fits_postgres(since):
            return
    elif isinstance(since, datetime.date):
        return
    elif isinstance(since, str) and _SINCE.fullmatch(since):
        try:
            parsed = datetime.datetime.fromisoformat(since)
        except ValueError:
            pass
        else:
            if _offset_fits_postgres(parsed):
                return
    raise InvalidQuery(
        f"since must be an ISO-8601 timestamp {SINCE_GRAMMAR} with a UTC offset of "
        f"at most +-15:59, or a date / datetime"
    )


def _since_clause(clauses, params, since):
    if since is not None:
        clauses.append("finished_at >= %s")
        params.append(since)


def _bound_samples(samples, max_samples, max_chars):
    """First `max_samples` samples; a string sample cut to `max_chars`, others kept whole.

    `samples` is what the runs_sources view guarantees for `error_samples`: a
    list (a JSON array) or None. Only top-level string samples are cut; a
    nested value inside a sample (an object or array) is kept whole, only
    counted against `max_samples`.
    """
    return [s[:max_chars] if isinstance(s, str) else s for s in (samples or [])[:max_samples]]


def runs_status(conn, *, corpus=None, source_code=None, since=None, limit=50,
                max_error_samples=DEFAULT_MAX_ERROR_SAMPLES, max_text_chars=DEFAULT_MAX_TEXT_CHARS):
    """One row per (run, source), newest first.

    `since` bounds finished_at: a date, a datetime or a string in the grammar
    YYYY-MM-DD[THH:MM[:SS[.ffffff]][Z|+HH:MM|-HH:MM]] (offset at most +-15:59; a
    naive value is read in the session time zone). Anything else (a week date, a
    space separator, an out-of-range offset, a number) is InvalidQuery before
    SQL; an accepted value reaches SQL unchanged.
    Runs still in flight (NULL finished_at) are listed last, and excluded
    whenever `since` is given.

    A run with no source entry (`sources` NULL, empty or not an array) has no
    row here: use recent_runs for the run grain.

    `error_samples` is bounded: at most `max_error_samples` elements, each JSON
    string cut to `max_text_chars` (objects are kept whole, only counted). The
    bounding is applied in Python after the fetch.
    """
    _check_at_least_one(limit=limit, max_error_samples=max_error_samples,
                        max_text_chars=max_text_chars)
    _check_since(since)
    clauses, params = _where(corpus, source_code)
    _since_clause(clauses, params, since)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _fetch(
        conn,
        f"SELECT {', '.join(RUN_COLUMNS)} FROM runs_sources{where} "
        f"ORDER BY finished_at DESC NULLS LAST, run_id DESC, source_code LIMIT %s",
        params + [limit],
    )
    for r in rows:
        r["error_samples"] = _bound_samples(r["error_samples"], max_error_samples, max_text_chars)
    return rows


def recent_runs(conn, *, corpus=None, since=None, limit=50):
    """One row per run, newest first, straight from `runs`.

    Run grain on purpose: a run whose `sources` is NULL, empty or not an array
    (a producer that crashed before reaching any source) never appears in
    runs_sources, so a failed run would be invisible to runs_status.

    `since` bounds finished_at, same grammar and rules as runs_status (naive =
    session time zone); in-flight runs (NULL finished_at) come last and are
    excluded when `since` is given.
    """
    _check_at_least_one(limit=limit)
    _check_since(since)
    clauses, params = _where(corpus)
    _since_clause(clauses, params, since)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _fetch(
        conn,
        f"SELECT {', '.join(RECENT_RUN_COLUMNS)} FROM runs{where} "
        f"ORDER BY finished_at DESC NULLS LAST, run_id DESC LIMIT %s",
        params + [limit],
    )


def source_health(conn, *, corpus=None, source_code=None):
    """Expected (cadence) x observed (runs) per series — the `source_health` view as is."""
    clauses, params = _where(corpus, source_code)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _fetch(conn, f"SELECT * FROM source_health{where} ORDER BY corpus, source_code, doc_type", params)


def open_discovery_errors(conn, *, corpus=None, source_code=None, limit=50,
                          max_text_chars=DEFAULT_MAX_TEXT_CHARS):
    """Unresolved discovery failures, most recently seen first.

    `error` is cut to `max_text_chars` in SQL. When `seen_at_is_ingestion_time`
    is true, `last_seen_at` is the ingestion time, an approximation of when the
    failure was last observed.
    """
    _check_at_least_one(limit=limit, max_text_chars=max_text_chars)
    clauses, params = _where(corpus, source_code)
    clauses.append("resolved_at IS NULL")
    columns = ", ".join("left(error, %s) AS error" if c == "error" else c for c in ERROR_COLUMNS)
    return _fetch(
        conn,
        f"SELECT {columns} FROM discovery_errors WHERE {' AND '.join(clauses)} "
        f"ORDER BY last_seen_at DESC, fingerprint LIMIT %s",
        [max_text_chars] + params + [limit],
    )


def freshness(conn):
    """How current the vault is: live count, last ingest sighting, last update, last run.

    `live_documents` excludes tombstones, but the last_* columns are maxima over
    every row, tombstoned documents included.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL) AS live_documents, "
            "max(last_seen_at) AS last_ingest_seen_at, max(updated_at) AS last_document_update "
            "FROM documents"
        )
        row = dict(cur.fetchone())
        cur.execute("SELECT max(finished_at) AS last_run_finished_at FROM runs")
        row.update(cur.fetchone())
    return row
