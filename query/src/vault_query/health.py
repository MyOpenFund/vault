"""Run telemetry and pipeline health, read from the views the DDL train owns."""
from psycopg2.extras import RealDictCursor

from .filters import InvalidQuery

RUN_COLUMNS = (
    "run_id", "tool", "command", "corpus", "source_code", "started_at", "finished_at",
    "outcome", "exit_code", "docs_seen", "docs_new", "docs_failed", "fetch_errors",
    "truncated", "error_samples",
)
ERROR_COLUMNS = (
    "fingerprint", "corpus", "source_code", "doc_type", "context", "url", "error_class",
    "error", "http_status", "first_seen_at", "last_seen_at", "occurrences",
)


def _where(corpus=None, source_code=None):
    clauses, params = [], []
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


def runs_status(conn, *, corpus=None, source_code=None, since=None, limit=50):
    """One row per (run, source), newest first. `since` bounds finished_at."""
    if limit < 1:
        raise InvalidQuery("limit must be >= 1")
    clauses, params = _where(corpus, source_code)
    if since is not None:
        clauses.append("finished_at >= %s")
        params.append(since)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _fetch(
        conn,
        f"SELECT {', '.join(RUN_COLUMNS)} FROM runs_sources{where} "
        f"ORDER BY finished_at DESC NULLS LAST, run_id DESC, source_code LIMIT %s",
        params + [limit],
    )


def source_health(conn, *, corpus=None, source_code=None):
    """Expected (cadence) x observed (runs) per series — the `source_health` view as is."""
    clauses, params = _where(corpus, source_code)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _fetch(conn, f"SELECT * FROM source_health{where} ORDER BY corpus, source_code, doc_type", params)


def open_discovery_errors(conn, *, corpus=None, source_code=None, limit=50):
    """Unresolved discovery failures, most recently seen first."""
    if limit < 1:
        raise InvalidQuery("limit must be >= 1")
    clauses, params = _where(corpus, source_code)
    clauses.append("resolved_at IS NULL")
    return _fetch(
        conn,
        f"SELECT {', '.join(ERROR_COLUMNS)} FROM discovery_errors WHERE {' AND '.join(clauses)} "
        f"ORDER BY last_seen_at DESC, fingerprint LIMIT %s",
        params + [limit],
    )


def freshness(conn):
    """How current the vault is: live count, last ingest sighting, last update, last run."""
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
