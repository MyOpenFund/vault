import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


# Fields allowing an exact-match filter (?source_code=us for example)
FILTERABLE_FIELDS = {
    "corpus", "source_code", "doc_type", "language", "provenance",
    "year", "mime_type",
}

SORTABLE_FIELDS = {
    "date", "year", "corpus", "source_code", "doc_type", "title", "created_at",
}


def build_where_clause(filters: dict, include_deleted: bool = False) -> tuple[str, list]:
    """Build a parameterized WHERE clause from a dict of filters.

    - simple values -> equality
    - `date_from` / `date_to` -> bounds on the `date` column
    - `q` -> free-text search on `title` (ILIKE)
    - by default, excludes soft-deleted documents (`deleted_at IS NULL`)
    """
    clauses = []
    params = []

    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    for field in FILTERABLE_FIELDS:
        value = filters.get(field)
        if value is not None:
            clauses.append(f"{field} = %s")
            params.append(value)

    date_from = filters.get("date_from")
    if date_from:
        clauses.append("date >= %s")
        params.append(date_from)

    date_to = filters.get("date_to")
    if date_to:
        clauses.append("date <= %s")
        params.append(date_to)

    q = filters.get("q")
    if q:
        clauses.append("title ILIKE %s")
        params.append(f"%{q}%")

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params
