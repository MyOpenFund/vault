"""documents.pdf_url -> document_url (2026-11 rename) against a real Postgres.

The deploy-day shape: a database built and fingerprinted by the previous train
(master 7b6e1b1, frozen in tests/fixtures/pre_document_url_train.sql), roles
provisioned, rows carrying URLs. The first run of the new code re-runs the
train once (its text changed), renames the column in place without touching a
value, recreates the views on the new name and re-issues the grants; the next
run skips the train. Manifests not yet migrated still say `pdf_url`: the
ingester maps it to document_url and never lets it fall into `extra`.
"""

import logging
from pathlib import Path

import psycopg2
import pytest

from .conftest import (
    drop_vault_roles, fetch_all, make_doc, role_conn, run_ingest, write_manifest,
)

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pre_document_url_train.sql"
_HEADER_LINES = 4
PRE_RENAME_TRAIN = "".join(
    FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)[_HEADER_LINES:])
# sha256 of master 7b6e1b1's CREATE_TABLE_SQL: what a deployed vault_schema holds.
PRE_RENAME_FINGERPRINT = "4ba299ebe8750d6f47b0edbd1bfe846ca787a06320ac6d7d97f18fd1b0d2a3b8"

ORCH_PW = "rename-orch-pw"
RO_PW = "rename-ro-pw"
VIEWS_WITH_URL = ("rag_backlog", "rag_backlog_any")


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    yield
    drop_vault_roles(pg_url)


def _columns(pg_url, relation):
    return {c for (c,) in fetch_all(
        pg_url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = current_schema() AND table_name = '{relation}'")}


def _build_pre_rename_database(pg_url):
    """Schema, fingerprint, roles and data exactly as the previous code left them."""
    import roles
    import schema_gate

    assert schema_gate.fingerprint(PRE_RENAME_TRAIN) == PRE_RENAME_FINGERPRINT, \
        "the fixture is no longer a verbatim copy of the previous train"
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(PRE_RENAME_TRAIN)
            cur.execute("INSERT INTO vault_schema (singleton, fingerprint) VALUES (TRUE, %s)",
                        (PRE_RENAME_FINGERPRINT,))
            roles.ensure_roles(cur, {"vault_orchestrator": ORCH_PW, "vault_readonly": RO_PW})
            cur.execute(
                "INSERT INTO documents (doc_id, corpus, source_code, title, pdf_url, "
                "source_url, last_seen_at, has_text_layer, page_count) VALUES "
                "('keep-1', 'central-bank', 'us', 't1', 'https://x.invalid/1.pdf', NULL, now(), true, 3), "
                "('keep-2', 'central-bank', 'fr', 't2', NULL, 'https://x.invalid/2', now(), NULL, NULL), "
                "('keep-3', 'central-bank', 'fr', 't3', 'https://x.invalid/3.pdf', NULL, now(), NULL, NULL)")
            cur.execute(
                "INSERT INTO rag_ingestions (doc_id, collection, corpus, embedding_model, chunk_count) "
                "VALUES ('keep-1', 'c1', 'central-bank', 'e1', 3)")
        conn.commit()
    finally:
        conn.close()


def _rows(pg_url):
    return fetch_all(pg_url, "SELECT id, doc_id, title, document_url, source_url, created_at, "
                             "updated_at, has_text_layer, page_count, extra "
                             "FROM documents ORDER BY doc_id")


def test_a_pre_rename_database_is_renamed_once_with_data_preserved(
    clean_db, tmp_path, monkeypatch, caplog
):
    import ingest
    import vault_query as vq

    _build_pre_rename_database(clean_db)
    before = fetch_all(clean_db, "SELECT id, doc_id, title, pdf_url, source_url, created_at, "
                                 "updated_at, has_text_layer, page_count, extra "
                                 "FROM documents ORDER BY doc_id")
    assert "pdf_url" in _columns(clean_db, "rag_backlog")

    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", ORCH_PW)
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)      # empty data dir: no upsert, no sweep
    assert "DDL train running: train changed (recorded 4ba299ebe875" in caplog.text
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [
        (ingest.TRAIN_FINGERPRINT,)]

    cols = _columns(clean_db, "documents")
    assert "document_url" in cols and "pdf_url" not in cols
    assert _rows(clean_db) == before, "the rename touched a value"
    for view in VIEWS_WITH_URL:
        vcols = _columns(clean_db, view)
        assert "document_url" in vcols and "pdf_url" not in vcols
    assert fetch_all(clean_db, "SELECT doc_id, document_url FROM rag_backlog ORDER BY doc_id") == [
        ("keep-2", None), ("keep-3", "https://x.invalid/3.pdf")]

    # Grants follow: both roles read the renamed column, through the table and
    # the recreated views, and the orchestrator keeps its column-level UPDATE.
    for role, pw in (("vault_orchestrator", ORCH_PW), ("vault_readonly", RO_PW)):
        c = role_conn(clean_db, role, pw)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT document_url FROM documents WHERE doc_id = 'keep-1'")
                assert cur.fetchone() == ("https://x.invalid/1.pdf",)
                for view in VIEWS_WITH_URL:
                    cur.execute(f"SELECT count(document_url) FROM {view}")
                    cur.fetchone()
                if role == "vault_orchestrator":
                    cur.execute("UPDATE documents SET page_count = 4 WHERE doc_id = 'keep-1'")
            c.rollback()
        finally:
            c.close()

    # vault-query's public key is document_url.
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    try:
        assert vq.get_document(conn, "keep-1")["document_url"] == "https://x.invalid/1.pdf"
        _, rows = vq.search_documents(conn, {"source_code": "us"}, columns=vq.SEARCH_COLUMNS)
        assert rows[0]["document_url"] == "https://x.invalid/1.pdf" and "pdf_url" not in rows[0]
    finally:
        conn.close()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "DDL train skipped" in caplog.text
    assert _rows(clean_db) == before


def test_the_train_rerun_on_a_renamed_database_is_a_no_op(clean_db, tmp_path, monkeypatch):
    _build_pre_rename_database(clean_db)
    run_ingest(monkeypatch, clean_db, tmp_path)
    after_first = _rows(clean_db)

    monkeypatch.setenv("VAULT_DDL_FORCE", "1")           # the rename block runs again
    run_ingest(monkeypatch, clean_db, tmp_path)
    cols = _columns(clean_db, "documents")
    assert "document_url" in cols and "pdf_url" not in cols
    assert _rows(clean_db) == after_first


def test_a_fresh_database_has_document_url_only(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "m.jsonl", [make_doc("d1", document_url="https://x.invalid/d1.pdf")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    cols = _columns(clean_db, "documents")
    assert "document_url" in cols and "pdf_url" not in cols
    assert fetch_all(clean_db, "SELECT document_url, extra FROM documents") == [
        ("https://x.invalid/d1.pdf", None)]


def test_legacy_pdf_url_manifest_rows_fill_document_url(clean_db, tmp_path, monkeypatch):
    """Ingested onto a renamed pre-rename database: the NAS between the vault
    deploy and the manifest migration."""
    _build_pre_rename_database(clean_db)
    write_manifest(tmp_path, "m.jsonl", [
        make_doc("keep-1", pdf_url="https://x.invalid/1-moved.pdf"),            # legacy key only
        make_doc("keep-2", document_url="https://x.invalid/2.html",
                 pdf_url="https://x.invalid/2-stale.pdf"),                     # new key wins
        make_doc("keep-3", document_url="https://x.invalid/3.pdf"),            # migrated row
        make_doc("new-1"),                                                     # no URL at all
    ])
    run_ingest(monkeypatch, clean_db, tmp_path)

    assert fetch_all(clean_db, "SELECT doc_id, document_url, extra FROM documents "
                               "ORDER BY doc_id") == [
        ("keep-1", "https://x.invalid/1-moved.pdf", None),
        ("keep-2", "https://x.invalid/2.html", None),
        ("keep-3", "https://x.invalid/3.pdf", None),
        ("new-1", None, None),
    ]


def test_both_url_columns_backfill_and_keep_pdf_url(clean_db, tmp_path, monkeypatch, caplog):
    # A document_url added by hand beside pdf_url: renaming would fail and
    # dropping would lose data. document_url wins where set, pdf_url fills its
    # NULLs, and pdf_url stays for a human to inspect -- reported every pass.
    _build_pre_rename_database(clean_db)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE documents ADD COLUMN document_url TEXT")
            cur.execute("UPDATE documents SET document_url = 'https://x.invalid/3.html' "
                        "WHERE doc_id = 'keep-3'")
    finally:
        conn.close()

    with caplog.at_level(logging.WARNING):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT doc_id, pdf_url, document_url FROM documents "
                               "ORDER BY doc_id") == [
        ("keep-1", "https://x.invalid/1.pdf", "https://x.invalid/1.pdf"),
        ("keep-2", None, None),
        ("keep-3", "https://x.invalid/3.pdf", "https://x.invalid/3.html"),
    ]
    warned = [r for r in caplog.records
              if "pdf_url" in r.getMessage() and "document_url" in r.getMessage()]
    assert warned

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run_ingest(monkeypatch, clean_db, tmp_path)       # train skipped, warning repeats
    assert any("DROP COLUMN pdf_url" in r.getMessage() for r in caplog.records)
