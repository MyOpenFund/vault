"""`clean_db` really hands each test an empty schema (Spec W4)."""
import psycopg2
import pytest

from .conftest import (
    OWNED_TABLES, fetch_all, make_doc, make_entry, run_ingest, write_cadence, write_manifest,
)

pytestmark = pytest.mark.integration

_RELATIONS = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm') ORDER BY 1
"""


def test_a_full_run_creates_only_tables_clean_db_knows(clean_db, tmp_path, monkeypatch):
    # If a future pass adds a table, this fails until clean_db drops it too.
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("d1")])
    write_cadence(tmp_path, [make_entry()])
    (tmp_path / "runs.jsonl").write_text("")
    run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    assert tables <= set(OWNED_TABLES)
    assert {"documents", "cadence", "vault_schema"} <= tables


def test_clean_db_leaves_nothing_behind(pg_url, tmp_path, monkeypatch, request):
    # Populate everything, then ask for a fresh clean_db: no relation survives.
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("d1")])
    write_cadence(tmp_path, [make_entry()])
    run_ingest(monkeypatch, pg_url, tmp_path)
    assert fetch_all(pg_url, "SELECT count(*) FROM cadence") == [(1,)]
    request.getfixturevalue("clean_db")
    assert fetch_all(pg_url, _RELATIONS) == []
