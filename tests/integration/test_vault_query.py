"""The `vault_query` package exercised over a real Postgres.

`vault_query` is the one implementation of the corpus reads behind both the
read API and the MCP server, so its paging, projection and tombstone rules are
what every consumer sees. The seed goes through the real ingester (the DDL
train included) and is deliberately uneven: two documents share a date, one has
no date at all, one is html, and one is tombstoned by a second run's sweep.
"""

import psycopg2
import pytest

import vault_query as vq

from .conftest import make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

SEED = [
    make_doc("us1", source="us", date="2010-01-13", year=2010),
    make_doc("us2", source="us", date="2010-01-13", year=2010),  # same date as us1: tie-break
    make_doc("us3", source="us", date=None, year=None),          # NULL date sorts last
    make_doc("fr1", source="fr", date="2011-05-01", year=2011, mime_type="text/html"),
]
TOMBSTONED = make_doc("de1", source="de", date="2012-01-01", year=2012)


@pytest.fixture()
def conn(clean_db, tmp_path, monkeypatch):
    """Seed through the real ingester, hand back an autocommit connection.

    Two runs: the second drops de1 from the manifests so the soft-delete sweep
    tombstones it. Autocommit so no read transaction outlives a test and blocks
    the next `clean_db` DROP TABLE.
    """
    manifests = tmp_path / "manifests"
    write_manifest(manifests, "corpus.jsonl", SEED + [TOMBSTONED])
    run_ingest(monkeypatch, clean_db, manifests)
    write_manifest(manifests, "corpus.jsonl", SEED)
    run_ingest(monkeypatch, clean_db, manifests)
    c = psycopg2.connect(clean_db)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DELETE FROM runs")
    yield c
    c.close()


def test_search_pages_cover_the_filtered_set_exactly_once(conn):
    """One-row pages walk the whole filtered set, no overlap, no gap.

    us1 and us2 share a date: without a unique tie-breaker Postgres may return
    them in either order on each query, so a pager would see one twice and
    miss the other. The undated row comes last (NULLS LAST, in either
    direction: Postgres would otherwise put it first under DESC, ahead of
    every dated document a reader is after), and the walk still reaches it.
    """
    seen, offset = [], 0
    while True:
        total, rows = vq.search_documents(conn, {}, sort_by="date", sort_dir="asc",
                                          limit=1, offset=offset)
        if not rows:
            break
        seen += [r["doc_id"] for r in rows]
        offset += len(rows)
    assert total == 4
    assert seen == ["us1", "us2", "fr1", "us3"]  # tie broken by doc_id, NULL date last


def test_search_projection_and_filters(conn):
    """A filter narrows both the count and the rows; the projection is exact.

    SEARCH_COLUMNS is the result-list shape the MCP returns: it must carry no
    free-form `extra` and no host-layout `local_path`.
    """
    total, rows = vq.search_documents(conn, {"source_code": "us"}, columns=vq.SEARCH_COLUMNS)
    assert total == 3
    assert set(rows[0]) == set(vq.SEARCH_COLUMNS)
    assert "extra" not in rows[0] and "local_path" not in rows[0]


def test_search_excludes_tombstones_unless_asked(conn):
    """A swept document is invisible by default and reachable on request."""
    assert vq.search_documents(conn, {"source_code": "de"})[0] == 0
    assert vq.search_documents(conn, {"source_code": "de"}, include_deleted=True)[0] == 1


def test_get_document_known_unknown_and_tombstoned(conn):
    """Lookup by doc_id returns the stored row, None when unknown, tombstones too.

    A citation to a document that was later swept must still resolve: the
    lookup ignores `deleted_at` and lets the caller see it.
    """
    assert vq.get_document(conn, "us2")["year"] == 2010
    assert vq.get_document(conn, "nope") is None
    assert vq.get_document(conn, "de1")["deleted_at"] is not None


def test_null_date_row_is_stored_with_no_date(conn):
    """The adversarial NULL-date seed really reached the table as NULL.

    Guards the paging test above: if the ingester ever coerced a missing date
    into a value, that test would stop exercising NULLS LAST without failing.
    """
    doc = vq.get_document(conn, "us3")
    assert doc["date"] is None and doc["year"] is None
