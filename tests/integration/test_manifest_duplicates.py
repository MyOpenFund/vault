"""vault #6: a doc_id repeated inside ONE manifest file.

Before the fix, two rows sharing a doc_id in the same 500-row
`execute_values` page made Postgres raise CardinalityViolation ("ON CONFLICT
DO UPDATE command cannot affect row a second time"), killing the run before
the sweep and the cadence pass; duplicates landing in different pages went
through silently. Now each file is deduplicated in Python first, last line
wins (what two upserts in order would have produced), with a WARNING.
"""
import json
import logging

import psycopg2
import pytest

from .conftest import make_doc, make_entry, run_ingest, write_cadence, write_manifest

pytestmark = pytest.mark.integration


def _rows(pg_url, sql, params=()):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _titles(pg_url):
    return dict(_rows(pg_url, "SELECT doc_id, title FROM documents"))


def test_duplicate_in_one_page_no_longer_aborts_the_run(clean_db, tmp_path, monkeypatch, caplog):
    # The #6 crash: both copies inside the first 500-row page. The cadence
    # pass that follows the documents pass must run too.
    docs = [make_doc("dup", title="first")] + [make_doc(f"d{i}") for i in range(10)]
    docs.append(make_doc("dup", title="second"))
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    write_cadence(tmp_path, [make_entry()])

    with caplog.at_level(logging.WARNING, logger="ingest"):
        run_ingest(monkeypatch, clean_db, tmp_path)

    titles = _titles(clean_db)
    assert len(titles) == 11
    assert titles["dup"] == "second"
    assert _rows(clean_db, "SELECT count(*) FROM cadence") == [(1,)]
    warning = [r.getMessage() for r in caplog.records if "duplicate" in r.getMessage()]
    assert warning and "1 " in warning[0] and "'dup'" in warning[0]


def test_duplicates_across_pages_last_wins(clean_db, tmp_path, monkeypatch):
    # Copies 600 lines apart: different pages, which used to "work" by luck.
    docs = [make_doc("dup", title="first")] + [make_doc(f"d{i}") for i in range(600)]
    docs.append(make_doc("dup", title="last"))
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    run_ingest(monkeypatch, clean_db, tmp_path)
    titles = _titles(clean_db)
    assert len(titles) == 601
    assert titles["dup"] == "last"


def test_three_copies_and_extra_only_difference(clean_db, tmp_path, monkeypatch):
    docs = [
        make_doc("a", title="a1"), make_doc("a", title="a2"), make_doc("a", title="a3"),
        make_doc("b", note="old"), make_doc("b", note="new"),
    ]
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _titles(clean_db)["a"] == "a3"
    assert _rows(clean_db, "SELECT extra FROM documents WHERE doc_id = 'b'") == [({"note": "new"},)]


def test_last_valid_line_wins_when_last_copy_is_invalid_json(clean_db, tmp_path, monkeypatch):
    path = tmp_path / "manifest" / "us.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(make_doc("dup", title="good-1")) + "\n"
        + json.dumps(make_doc("dup", title="good-2")) + "\n"
        + '{"doc_id": "dup", "title": "broken"\n',
        encoding="utf-8",
    )
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _titles(clean_db) == {"dup": "good-2"}


def test_same_doc_id_in_two_files_still_fine(clean_db, tmp_path, monkeypatch):
    # Separate statements, no conflict: unchanged behaviour (files in sorted order).
    write_manifest(tmp_path / "manifest", "a.jsonl", [make_doc("x", title="from-a")])
    write_manifest(tmp_path / "manifest", "b.jsonl", [make_doc("x", title="from-b")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _titles(clean_db) == {"x": "from-b"}
