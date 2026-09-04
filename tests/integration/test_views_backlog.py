import json

import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


def _exec(pg_url, sql, params=None):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def _ingest(monkeypatch, pg_url, tmp_path, docs):
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    run_ingest(monkeypatch, pg_url, tmp_path)


def _mark(pg_url, doc_id, collection):
    _exec(pg_url,
          "INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code, "
          "embedding_model, embedding_version, chunk_count) VALUES (%s, %s, 'central-bank', 'us', 'e5', 'v1', 3)",
          (doc_id, collection))


def test_backlog_matrix_zero_one_two_collections(clean_db, tmp_path, monkeypatch):
    # I7 / I8 — the A/B/C matrix: rag_backlog enumerates collections seen in
    # rag_ingestions, so with none it is EMPTY while rag_backlog_any lists all.
    _ingest(monkeypatch, clean_db, tmp_path, [make_doc("d1"), make_doc("d2"), make_doc("d3")])
    assert fetch_all(clean_db, "SELECT count(*) FROM rag_backlog")[0][0] == 0
    assert fetch_all(clean_db, "SELECT count(*) FROM rag_backlog_any")[0][0] == 3

    _mark(clean_db, "d1", "cb_v1")
    assert fetch_all(clean_db, "SELECT collection, doc_id FROM rag_backlog ORDER BY 1, 2") == [
        ("cb_v1", "d2"), ("cb_v1", "d3")]
    assert fetch_all(clean_db, "SELECT count(*) FROM rag_backlog_any")[0][0] == 2

    _mark(clean_db, "d2", "cb_v2")  # re-embed campaign into a fresh collection
    rows = fetch_all(clean_db, "SELECT collection, count(*) FROM rag_backlog GROUP BY 1 ORDER BY 1")
    assert rows == [("cb_v1", 2), ("cb_v2", 2)]
    assert fetch_all(clean_db, "SELECT doc_id FROM rag_backlog_any")[0] == ("d3",)


def test_soft_deleted_document_leaves_both_views_and_returns(clean_db, tmp_path, monkeypatch):
    # I7 D — deletion and resurrection are reflected immediately.
    _ingest(monkeypatch, clean_db, tmp_path, [make_doc("d1"), make_doc("d2")])
    _mark(clean_db, "d2", "cb_v1")
    _ingest(monkeypatch, clean_db, tmp_path, [make_doc("d2")])  # d1 swept
    assert fetch_all(clean_db, "SELECT count(*) FROM rag_backlog")[0][0] == 0
    assert fetch_all(clean_db, "SELECT count(*) FROM rag_backlog_any")[0][0] == 0
    _ingest(monkeypatch, clean_db, tmp_path, [make_doc("d1"), make_doc("d2")])  # resurrected
    assert fetch_all(clean_db, "SELECT doc_id FROM rag_backlog") == [("d1",)]


def test_runs_sources_never_raises_on_adversarial_sources(clean_db, tmp_path, monkeypatch):
    # I13 — garbage counters -> NULL, non-boolean truncated -> FALSE,
    # non-array error_samples -> [], sources as object/NULL -> zero rows.
    run_ingest(monkeypatch, clean_db, tmp_path)  # DDL only
    # clean_db does not truncate `runs` and the container is module-scoped, so
    # start from an empty table to keep the assertion below exact.
    _exec(clean_db, "DELETE FROM runs")
    bad = [{"source_code": "us", "docs_seen": "many", "docs_new": 2,
            "truncated": "yes", "error_samples": "oops"}]
    _exec(clean_db, "INSERT INTO runs (run_id, tool, sources) VALUES ('r1', 't', %s)", (json.dumps(bad),))
    _exec(clean_db, "INSERT INTO runs (run_id, tool, sources) VALUES ('r2', 't', %s)", (json.dumps({"a": 1}),))
    _exec(clean_db, "INSERT INTO runs (run_id, tool, sources) VALUES ('r3', 't', NULL)")
    _exec(clean_db, "INSERT INTO runs (run_id, tool, sources) VALUES ('r4', 't', '[]')")
    rows = fetch_all(clean_db, "SELECT run_id, source_code, docs_seen, docs_new, truncated, error_samples FROM runs_sources")
    assert rows == [("r1", "us", None, 2, False, [])]
