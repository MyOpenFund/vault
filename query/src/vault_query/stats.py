"""Corpus counts — the numbers behind "verifiable numbers"."""
from psycopg2.extras import RealDictCursor

from .filters import InvalidQuery

# Interpolated into GROUP BY: this set is the injection guard.
GROUPABLE_FIELDS = frozenset({
    "corpus", "source_code", "doc_type", "language", "year", "provenance", "mime_type",
})


def corpus_stats(conn, group_by, *, corpus=None, limit=200):
    """Live-document total and per-`group_by` counts, largest first.

    A NULL key is a real group (e.g. documents with no year) and is kept.
    """
    if group_by not in GROUPABLE_FIELDS:
        raise InvalidQuery(f"group_by must be one of {sorted(GROUPABLE_FIELDS)}")
    if limit < 1:
        raise InvalidQuery("limit must be >= 1")
    where, params = "WHERE deleted_at IS NULL", []
    if corpus is not None:
        where += " AND corpus = %s"
        params.append(corpus)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM documents {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT {group_by} AS key, COUNT(*) AS count FROM documents {where} "
            f"GROUP BY {group_by} ORDER BY count DESC, key NULLS LAST LIMIT %s",
            params + [limit],
        )
        groups = [dict(r) for r in cur.fetchall()]
    return {"total": total, "group_by": group_by, "groups": groups}
