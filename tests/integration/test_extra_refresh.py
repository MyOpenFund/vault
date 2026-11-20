import psycopg2
import pytest

from .conftest import make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


def _fetch(pg_url, sql, params):
    """conftest.fetch_all takes no parameters; doc_ids belong in a bind, not
    in an f-string."""
    conn = psycopg2.connect(pg_url)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return rows


def _extra(pg_url, doc_id):
    rows = _fetch(pg_url, "SELECT extra FROM documents WHERE doc_id = %s", (doc_id,))
    assert len(rows) == 1
    return rows[0][0]


def _ingest(monkeypatch, pg_url, tmp_path, doc):
    write_manifest(tmp_path / "manifest", "us.jsonl", [doc])
    run_ingest(monkeypatch, pg_url, tmp_path)


def test_extra_is_refreshed_on_reingest(clean_db, tmp_path, monkeypatch):
    # I1 — the #8/#2 regression: enrichment stamped later must reach the vault.
    # (date_precision used to be an extra; it is a column since the documents
    # contract, so it is the column that is refreshed, and it leaves extra.)
    _ingest(monkeypatch, clean_db, tmp_path,
            make_doc("d1", date="2010-01-01", date_precision="year", date_source="x"))
    assert _extra(clean_db, "d1") == {"date_source": "x"}
    _ingest(monkeypatch, clean_db, tmp_path,
            make_doc("d1", date_precision="day", repec_handle="RePEc:boj:x"))
    assert _extra(clean_db, "d1") == {"repec_handle": "RePEc:boj:x"}
    assert _fetch(clean_db, "SELECT date_precision FROM documents WHERE doc_id = %s",
                  ("d1",)) == [("day",)]


def test_conflicting_key_type_last_write_wins(clean_db, tmp_path, monkeypatch):
    # I2 — same key, different JSON type: no error, latest manifest wins.
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1", a=1))
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1", a="x"))
    assert _extra(clean_db, "d1") == {"a": "x"}


def test_dropped_key_disappears(clean_db, tmp_path, monkeypatch):
    # I3 — the explicit cost of D1 (replace): a key the producer stops
    # emitting is gone on the next run. Pinned so nobody "fixes" it into a merge.
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1", a=1, b=2))
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1", a=1))
    assert _extra(clean_db, "d1") == {"a": 1}


def test_manifest_without_unknown_fields_nulls_extra(clean_db, tmp_path, monkeypatch):
    # I4 — only known fields -> extra IS NULL (parse_line maps {} to None).
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1", a=1))
    _ingest(monkeypatch, clean_db, tmp_path, make_doc("d1"))
    assert _extra(clean_db, "d1") is None
