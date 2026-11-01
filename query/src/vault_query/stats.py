"""Corpus counts — the numbers behind "verifiable numbers"."""
from psycopg2.extras import RealDictCursor

from .filters import FILTERABLE_FIELDS, InvalidQuery

# Interpolated into GROUP BY: this set is the injection guard. Kept in lock-step
# with FILTERABLE_FIELDS: whatever a caller can filter on, it can group by.
GROUPABLE_FIELDS = frozenset(FILTERABLE_FIELDS)


def corpus_stats(conn, group_by, *, corpus=None, limit=200):
    """Live-document total and per-`group_by` counts, largest first.

    A NULL key is a real group (e.g. documents with no year) and is kept. Ties
    on count are ordered by key ascending, the NULL key last. Groups beyond
    `limit` are dropped, so the returned groups may sum to less than `total`.

    Total and groups come from one statement (a window total over the grouped
    rows): one scan, one snapshot, so they always agree.
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
        cur.execute(
            f"SELECT {group_by} AS key, COUNT(*) AS count, SUM(COUNT(*)) OVER ()::bigint AS total "
            f"FROM documents {where} "
            f"GROUP BY {group_by} ORDER BY count DESC, key NULLS LAST LIMIT %s",
            params + [limit],
        )
        rows = [dict(r) for r in cur.fetchall()]
    total = rows[0]["total"] if rows else 0
    groups = [{"key": r["key"], "count": r["count"]} for r in rows]
    return {"total": total, "group_by": group_by, "groups": groups}
