"""The `vault_query` package exercised over a real Postgres.

`vault_query` is the one implementation of the corpus reads behind both the
read API and the MCP server, so its paging, projection and tombstone rules are
what every consumer sees. The seed goes through the real ingester (the DDL
train included) and is deliberately uneven: two documents share a date, one has
no date at all, one is html, and one is tombstoned by a second run's sweep.
"""

import json

import psycopg2
import pytest

import vault_query as vq

from .conftest import make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

# us2 is listed BEFORE us1: insertion order must differ from doc_id order,
# otherwise the paging walks pass even without the `, doc_id` tie-break.
SEED = [
    make_doc("us2", source="us", date="2010-01-13", year=2010),  # same date as us1: tie-break
    make_doc("us1", source="us", date="2010-01-13", year=2010),
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
        # runs and cadence survive clean_db and the container is module-scoped:
        # clear them so fixed run_ids and cadence keys never collide.
        # Isolation of the run-health tests: clean_db does not drop these two tables.
        cur.execute("DELETE FROM runs")
        cur.execute("DELETE FROM cadence")
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


def _walk(conn, sort_dir):
    seen, offset = [], 0
    while True:
        _, rows = vq.search_documents(conn, {}, sort_by="date", sort_dir=sort_dir,
                                      limit=1, offset=offset)
        if not rows:
            return seen
        seen += [r["doc_id"] for r in rows]
        offset += len(rows)


def test_search_desc_walk_keeps_nulls_last_and_doc_id_ascending(conn):
    """Under DESC the NULL date still comes last and the tie stays doc_id ascending.

    The tie-break is `doc_id` ascending whatever `sort_dir` is: us1 before us2
    in both directions.
    """
    assert _walk(conn, "desc") == ["fr1", "us1", "us2", "us3"]


def test_search_offset_past_the_end_is_empty_with_the_true_total(conn):
    """Paging past the last row returns no rows, and still the full total."""
    assert vq.search_documents(conn, {}, offset=99) == (4, [])


def test_all_columns_matches_the_documents_table(conn):
    """ALL_COLUMNS is the injection guard and the projection allowlist: drift guard.

    After the DDL train, it must equal the real column set of `documents`; a
    column added by a migration and not listed here would be unreachable, a
    column dropped would turn a valid projection into a 500.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'documents'"
        )
        actual = {r[0] for r in cur.fetchall()}
    assert vq.ALL_COLUMNS == actual


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


def test_corpus_stats_counts_live_documents_only(conn):
    """Totals and groups count live documents: the tombstoned de1 is absent.

    A swept document left in the counts would make every "verifiable number"
    the MCP quotes overstate the corpus.
    """
    stats = vq.corpus_stats(conn, "source_code")
    assert stats["total"] == 4
    assert stats["group_by"] == "source_code"
    assert {g["key"]: g["count"] for g in stats["groups"]} == {"us": 3, "fr": 1}
    assert [g["key"] for g in stats["groups"]] == ["us", "fr"]  # largest first


def test_corpus_stats_keeps_null_keys_and_filters_by_corpus(conn):
    """A NULL key is a real group; the corpus filter narrows the total too.

    us3 has no year: dropping its group would leave the groups summing to
    less than the total. The seed is all 'central-bank', so another corpus
    counts nothing.
    """
    stats = vq.corpus_stats(conn, "year", corpus="central-bank")
    assert {g["key"]: g["count"] for g in stats["groups"]} == {2010: 2, 2011: 1, None: 1}
    assert sum(g["count"] for g in stats["groups"]) == stats["total"] == 4
    # Largest first; ties (2011 and None, one each) by key ascending, NULL last.
    assert [g["key"] for g in stats["groups"]] == [2010, 2011, None]
    empty = vq.corpus_stats(conn, "year", corpus="company")
    assert empty["total"] == 0 and empty["groups"] == []


def test_corpus_stats_orders_many_tied_groups_by_key_null_last(conn):
    """Every group tied on count: the order is the key ascending, NULL last.

    With us2 moved to 2009 each year group (2009, 2010, 2011, NULL) counts one
    document, so the order rests on the tie-break alone. `enable_sort = off`
    steers the planner to a HashAggregate: a sort-based GroupAggregate already
    emits keys ascending with NULL last, which would let a query without the
    `key NULLS LAST` tie-break pass by luck.
    `total` and `count` are plain ints, not the Decimal a bare SUM(bigint) gives.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET year = 2009 WHERE doc_id = 'us2'")
        cur.execute("SET enable_sort = off")
    try:
        stats = vq.corpus_stats(conn, "year")
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET enable_sort")
    groups = stats["groups"]
    assert [g["key"] for g in groups] == [2009, 2010, 2011, None]
    assert [g["count"] for g in groups] == [1, 1, 1, 1]
    assert stats["total"] == 4
    assert type(stats["total"]) is int
    assert type(groups[0]["count"]) is int


def test_corpus_stats_limit_drops_groups_but_not_the_total(conn):
    """Groups beyond `limit` are dropped; `total` still counts every live document.

    The groups may then sum to less than the total: that is the documented
    truncation, not a lost row.
    """
    stats = vq.corpus_stats(conn, "year", limit=1)
    assert stats["groups"] == [{"key": 2010, "count": 2}]
    assert stats["total"] == 4


def _insert_run(conn, run_id, finished_at, sources, outcome="ok", corpus="central-bank"):
    """`sources=None` stores SQL NULL (a producer that crashed before any source)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, tool, command, corpus, started_at, finished_at, outcome, "
            "exit_code, sources) VALUES (%s, 'central-bank-corpus', 'sync', %s, %s, %s, %s, 0, %s)",
            (run_id, corpus, finished_at, finished_at, outcome,
             None if sources is None else json.dumps(sources)),
        )


def _insert_error(conn, fingerprint, resolved_at=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO discovery_errors (fingerprint, corpus, source_code, url, error_class, error, "
            "first_seen_at, last_seen_at, resolved_at) VALUES (%s, 'central-bank', 'us', 'https://x', "
            "'HTTP404', 'not found', now(), now(), %s)",
            (fingerprint, resolved_at),
        )


def test_runs_status_is_per_source_newest_first_and_filters(conn):
    """One row per (run, source), newest run first, then source_code.

    The second run carries a non-integer counter: the view casts it to NULL,
    so a producer bug never turns into an exception for the reader.
    """
    _insert_run(conn, "r1", "2026-10-01T02:00:00Z", [{"source_code": "us", "docs_new": 3}])
    _insert_run(conn, "r2", "2026-10-02T02:00:00Z",
                [{"source_code": "us", "docs_new": "garbage"}, {"source_code": "fr", "fetch_errors": 2}],
                outcome="degraded")
    rows = vq.runs_status(conn, limit=10)
    assert [(r["run_id"], r["source_code"]) for r in rows] == [("r2", "fr"), ("r2", "us"), ("r1", "us")]
    assert set(rows[0]) == set(vq.health.RUN_COLUMNS)
    us = vq.runs_status(conn, source_code="us")
    assert [r["run_id"] for r in us] == ["r2", "r1"]
    assert us[0]["docs_new"] is None  # non-integer counter: NULL, never an exception
    assert us[1]["docs_new"] == 3
    assert vq.runs_status(conn, since="2026-10-02T00:00:00Z", source_code="us")[0]["run_id"] == "r2"
    assert len(vq.runs_status(conn, since="2026-10-02T00:00:00Z", source_code="us")) == 1
    assert len(vq.runs_status(conn, limit=1)) == 1
    assert vq.runs_status(conn, corpus="company") == []


def test_recent_runs_lists_runs_with_no_sources(conn):
    """A run whose `sources` is NULL or empty is invisible per source, visible per run.

    A producer that crashed before reaching any source writes no source entry:
    runs_sources (an unnest of `sources`) has no row for it, so runs_status
    alone would hide exactly the failures a reader is looking for.
    """
    _insert_run(conn, "ok1", "2026-10-01T02:00:00Z", [{"source_code": "us"}])
    _insert_run(conn, "nul", "2026-10-02T02:00:00Z", None, outcome="failed")
    _insert_run(conn, "emp", "2026-10-03T02:00:00Z", [], outcome="failed")
    runs = vq.recent_runs(conn)
    assert [r["run_id"] for r in runs] == ["emp", "nul", "ok1"]
    assert set(runs[0]) == set(vq.health.RECENT_RUN_COLUMNS)
    assert runs[0]["outcome"] == "failed"
    assert {r["run_id"] for r in vq.runs_status(conn)} == {"ok1"}
    assert [r["run_id"] for r in vq.recent_runs(conn, since="2026-10-02T00:00:00Z")] == ["emp", "nul"]
    assert [r["run_id"] for r in vq.recent_runs(conn, limit=1)] == ["emp"]
    assert vq.recent_runs(conn, corpus="company") == []


def test_in_flight_runs_come_last_and_drop_out_under_since(conn):
    """A NULL finished_at (run in flight) is listed last, and excluded when `since` is given."""
    _insert_run(conn, "done", "2026-10-01T02:00:00Z", [{"source_code": "us"}])
    _insert_run(conn, "live", None, [{"source_code": "us"}])
    assert [r["run_id"] for r in vq.runs_status(conn)] == ["done", "live"]
    assert [r["run_id"] for r in vq.recent_runs(conn)] == ["done", "live"]
    since = "2000-01-01T00:00:00Z"
    assert [r["run_id"] for r in vq.runs_status(conn, since=since)] == ["done"]
    assert [r["run_id"] for r in vq.recent_runs(conn, since=since)] == ["done"]


def test_runs_status_bounds_error_samples(conn):
    """At most `max_error_samples` samples, each string cut to `max_text_chars`.

    A producer can write any number of samples of any size; the reader (an MCP
    tool result) must stay bounded. Non-string samples are kept whole.
    """
    samples = ["x" * 10_000 for _ in range(50)]
    _insert_run(conn, "big", "2026-10-01T02:00:00Z",
                [{"source_code": "us", "error_samples": samples}])
    row = vq.runs_status(conn)[0]
    assert len(row["error_samples"]) == 5
    assert all(len(e) <= 2000 for e in row["error_samples"])
    row = vq.runs_status(conn, max_error_samples=2, max_text_chars=10)[0]
    assert row["error_samples"] == ["x" * 10, "x" * 10]
    _insert_run(conn, "obj", "2026-10-02T02:00:00Z",
                [{"source_code": "us", "error_samples": [{"url": "u" * 50}, "short"]}])
    row = vq.runs_status(conn, max_text_chars=10)[0]
    assert row["error_samples"] == [{"url": "u" * 50}, "short"]


def test_open_discovery_errors_bounds_error_text(conn):
    """A huge error text comes back cut to `max_text_chars`."""
    _insert_error(conn, "huge")
    with conn.cursor() as cur:
        cur.execute("UPDATE discovery_errors SET error = repeat('e', 200000) WHERE fingerprint = 'huge'")
    assert len(vq.open_discovery_errors(conn)[0]["error"]) <= 2000
    assert vq.open_discovery_errors(conn, max_text_chars=3)[0]["error"] == "eee"


def test_open_discovery_errors_and_freshness(conn):
    """Open errors are listed; freshness reports live docs and no runs as None."""
    _insert_error(conn, "f1")
    errs = vq.open_discovery_errors(conn, source_code="us")
    assert [e["fingerprint"] for e in errs] == ["f1"]
    assert set(errs[0]) == set(vq.health.ERROR_COLUMNS)
    # The approximation flag travels with last_seen_at; last_run_id is the provenance.
    assert errs[0]["seen_at_is_ingestion_time"] is True and "last_run_id" in errs[0]
    fresh = vq.freshness(conn)
    assert fresh["live_documents"] == 4
    assert fresh["last_ingest_seen_at"] is not None
    assert fresh["last_run_finished_at"] is None  # no runs in this test: None, not an error


def test_open_discovery_errors_skips_resolved(conn):
    """An error with resolved_at set is history, not an open failure."""
    _insert_error(conn, "open")
    _insert_error(conn, "closed", resolved_at="2026-10-01T00:00:00Z")
    assert [e["fingerprint"] for e in vq.open_discovery_errors(conn)] == ["open"]


def test_source_health_reads_the_view(conn):
    """The view is wired: a cadence series comes back with its observed counts.

    One cadence row for (central-bank, us, C1) and one recent run for 'us'
    (plus one for 'fr', which has no cadence row and so no health row). The
    source_code filter and a non-matching corpus are exercised too.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cadence (corpus, source_code, doc_type, interval_days, days_until, status, "
            "updated_at) VALUES ('central-bank', 'us', 'C1', 30, -2, 'overdue', now())"
        )
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
    _insert_run(conn, "h1", now, [{"source_code": "us", "docs_seen": 5, "docs_new": 2},
                                  {"source_code": "fr", "docs_seen": 1, "docs_new": 1}])
    rows = vq.source_health(conn, corpus="central-bank")
    assert [(r["corpus"], r["source_code"], r["doc_type"]) for r in rows] == [("central-bank", "us", "C1")]
    us = rows[0]
    assert us["source_runs_7d"] == 1
    assert us["source_docs_seen_7d"] == 5 and us["source_docs_new_7d"] == 2
    assert us["last_run_id"] == "h1" and us["days_late"] == 2
    assert vq.source_health(conn, source_code="us") == rows
    assert vq.source_health(conn, source_code="fr") == []
    assert vq.source_health(conn, corpus="company") == []
