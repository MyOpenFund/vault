import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


def test_fresh_ingest_populates_corpus_and_source_code(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1"), make_doc("d2", source="fr")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    rows = fetch_all(
        clean_db,
        "SELECT doc_id, corpus, source_code, deleted_at FROM documents ORDER BY doc_id",
    )
    assert rows == [
        ("d1", "central-bank", "us", None),
        ("d2", "central-bank", "fr", None),
    ]


def test_legacy_schema_is_migrated_in_place(clean_db, tmp_path, monkeypatch):
    # Pre-create the pre-generalization, pre-lifecycle shape.
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE documents (
                id SERIAL PRIMARY KEY,
                doc_id TEXT UNIQUE NOT NULL,
                bank_code TEXT,
                doc_type TEXT, title TEXT, pdf_url TEXT, source_url TEXT,
                date DATE, year INTEGER, language TEXT, provenance TEXT,
                mime_type TEXT, sha256 TEXT, local_path TEXT,
                created_at TIMESTAMP DEFAULT now(),
                updated_at TIMESTAMP DEFAULT now(),
                extra JSONB
            );
            CREATE INDEX idx_documents_bank_code ON documents(bank_code);
            INSERT INTO documents (doc_id, bank_code) VALUES ('legacy1', 'us');
            """
        )
    conn.close()

    write_manifest(tmp_path, "us.jsonl", [make_doc("legacy1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    rows = fetch_all(
        clean_db, "SELECT doc_id, corpus, source_code FROM documents"
    )
    assert rows == [("legacy1", "central-bank", "us")]

    types = dict(
        fetch_all(
            clean_db,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'documents' "
            "AND column_name IN ('updated_at', 'created_at')",
        )
    )
    assert types == {
        "updated_at": "timestamp with time zone",
        "created_at": "timestamp with time zone",
    }


def test_removed_document_is_soft_deleted_then_resurrected(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1"), make_doc("d2")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])  # d2 gone
    run_ingest(monkeypatch, clean_db, tmp_path)
    rows = dict(
        fetch_all(clean_db, "SELECT doc_id, deleted_at FROM documents")
    )
    assert rows["d1"] is None
    assert rows["d2"] is not None  # soft-deleted, still present

    write_manifest(tmp_path, "us.jsonl", [make_doc("d1"), make_doc("d2")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    rows = dict(
        fetch_all(clean_db, "SELECT doc_id, deleted_at FROM documents")
    )
    assert rows["d2"] is None  # resurrected


def test_partial_sync_guard_blocks_mass_soft_delete(
    clean_db, tmp_path, monkeypatch
):
    docs = [make_doc(f"d{i}") for i in range(20)]
    write_manifest(tmp_path, "us.jsonl", docs)
    run_ingest(monkeypatch, clean_db, tmp_path)

    # Torn sync: only 2 of 20 docs visible; 90% would vanish.
    write_manifest(tmp_path, "us.jsonl", docs[:2])
    run_ingest(monkeypatch, clean_db, tmp_path, sweep="0.05")

    (count,) = fetch_all(
        clean_db, "SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"
    )[0]
    assert count == 20  # sweep blocked, nothing soft-deleted


def test_sweep_is_scoped_to_the_service_corpus(clean_db, tmp_path, monkeypatch):
    cb_dir = tmp_path / "cb"
    co_dir = tmp_path / "co"
    write_manifest(cb_dir, "us.jsonl", [make_doc("cb1")])
    write_manifest(
        co_dir, "aapl.jsonl", [make_doc("co1", source="aapl", corpus="company")]
    )
    run_ingest(monkeypatch, clean_db, cb_dir, corpus="central-bank")
    run_ingest(monkeypatch, clean_db, co_dir, corpus="company")

    # Re-run central-bank alone: company rows must stay untouched.
    run_ingest(monkeypatch, clean_db, cb_dir, corpus="central-bank")

    rows = dict(fetch_all(clean_db, "SELECT doc_id, deleted_at FROM documents"))
    assert rows == {"cb1": None, "co1": None}


def test_contradicting_corpus_line_is_rejected(clean_db, tmp_path, monkeypatch):
    write_manifest(
        tmp_path,
        "mixed.jsonl",
        [make_doc("ok1"), make_doc("bad1", corpus="company")],
    )
    run_ingest(monkeypatch, clean_db, tmp_path, corpus="central-bank")

    rows = fetch_all(clean_db, "SELECT doc_id FROM documents")
    assert rows == [("ok1",)]
