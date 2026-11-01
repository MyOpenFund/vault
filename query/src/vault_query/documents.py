"""Document search and lookup."""
from psycopg2.extras import RealDictCursor

from .filters import SORTABLE_FIELDS, InvalidQuery, build_where_clause, check_int

# Every column of `documents` after the DDL train (ingestion/ingest.py). A
# caller-chosen projection is checked against it: column names are
# interpolated into the SELECT, so this set is the injection guard.
ALL_COLUMNS = frozenset({
    "id", "doc_id", "corpus", "source_code", "doc_type", "title", "pdf_url", "source_url",
    "date", "year", "language", "provenance", "mime_type", "sha256", "local_path",
    "created_at", "updated_at", "last_seen_at", "deleted_at", "extra",
    "has_text_layer", "page_count",
})

# The projection for result lists: what a reader needs to pick a document.
# `extra` (free-form, can be large) and host-layout columns stay in get_document.
SEARCH_COLUMNS = (
    "doc_id", "corpus", "source_code", "doc_type", "title", "date", "year", "language",
    "provenance", "mime_type", "pdf_url", "source_url", "has_text_layer", "page_count",
    "deleted_at",
)


def search_documents(conn, filters, *, sort_by="date", sort_dir="desc", limit=50,
                     offset=0, include_deleted=False, columns=None):
    """Filtered, sorted page of documents and the total of the filtered set.

    Returns (total, rows). `total` ignores the window, so a caller can page to
    the end. `doc_id` breaks ties so pages never overlap or skip under a
    non-unique sort key (several documents share a date); the tie-break is
    always `doc_id` ascending, whatever `sort_dir` is. NULL sort keys come last
    in both directions.

    `limit` (>= 1) and `offset` (>= 0) are ints (not bool), at most MAX_BOUND.
    `columns`, when given, is a non-empty subset of ALL_COLUMNS; None means
    every column.

    The COUNT and the page are two statements: under READ COMMITTED a
    concurrent ingest between them can shift the page against the total by a row.
    """
    if sort_by not in SORTABLE_FIELDS:
        raise InvalidQuery(f"sort_by must be one of {sorted(SORTABLE_FIELDS)}")
    if sort_dir not in ("asc", "desc"):
        raise InvalidQuery("sort_dir must be 'asc' or 'desc'")
    check_int("limit", limit, 1)
    check_int("offset", offset, 0)
    if columns is not None:
        columns = tuple(columns)
        if not columns:
            raise InvalidQuery("columns must not be empty")
    if columns is not None and not set(columns) <= ALL_COLUMNS:
        raise InvalidQuery(f"unknown columns: {sorted(set(columns) - ALL_COLUMNS)}")

    projection = "*" if columns is None else ", ".join(columns)
    where_sql, params = build_where_clause(filters, include_deleted=include_deleted)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM documents{where_sql}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT {projection} FROM documents{where_sql} "
            f"ORDER BY {sort_by} {sort_dir} NULLS LAST, doc_id "
            f"LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return total, rows


def get_document(conn, doc_id):
    """Every stored fact of one document, soft-deleted or not; None if unknown."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
    return dict(row) if row else None
