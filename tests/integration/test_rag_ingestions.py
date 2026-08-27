import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


UPSERT_RAG_SQL = """
INSERT INTO rag_ingestions (
    doc_id, collection, corpus, source_code,
    embedding_model, embedding_version, chunk_count
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (doc_id, collection) DO UPDATE SET
    corpus = EXCLUDED.corpus,
    source_code = EXCLUDED.source_code,
    embedding_model = EXCLUDED.embedding_model,
    embedding_version = EXCLUDED.embedding_version,
    chunk_count = EXCLUDED.chunk_count,
    ingested_at = now();
"""


def _exec(pg_url, sql, params):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_rag_ingestions_upsert_contract(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    _exec(clean_db, UPSERT_RAG_SQL,
          ("d1", "cb_e5", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 12))
    # Re-ingest updates in place (current-state semantics).
    _exec(clean_db, UPSERT_RAG_SQL,
          ("d1", "cb_e5", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 15))

    rows = fetch_all(
        clean_db,
        "SELECT doc_id, collection, chunk_count FROM rag_ingestions",
    )
    assert rows == [("d1", "cb_e5", 15)]


def test_rag_ingestions_fk_rejects_unknown_doc(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _exec(clean_db, UPSERT_RAG_SQL,
              ("ghost", "cb_e5", "central-bank", "us",
               "intfloat/multilingual-e5-base", "v1", 3))


def test_fact_columns_exist_and_survive_manifest_reupsert(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    _exec(clean_db,
          "UPDATE documents SET has_text_layer = %s, page_count = %s "
          "WHERE doc_id = %s", (True, 42, "d1"))

    # A nightly manifest re-upsert must not null the probed values.
    run_ingest(monkeypatch, clean_db, tmp_path)
    rows = fetch_all(
        clean_db,
        "SELECT has_text_layer, page_count FROM documents WHERE doc_id = 'd1'",
    )
    assert rows == [(True, 42)]
