"""Spec W5 end to end: which files reach `documents` in each layout."""
import psycopg2
import pytest

from .conftest import make_doc, make_entry, run_ingest, write_cadence, write_manifest

pytestmark = pytest.mark.integration


def _doc_ids(pg_url):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT doc_id FROM documents ORDER BY doc_id")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_root_layout_ignores_an_unknown_root_file_carrying_doc_ids(clean_db, tmp_path, monkeypatch):
    # An unknown producer file at the data/ root whose lines LOOK like
    # manifest lines: the old denylist would have ingested them.
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("in-manifest-dir")])
    write_manifest(tmp_path, "manifest.jsonl", [make_doc("legacy-root")])
    write_manifest(tmp_path, "foo.jsonl", [make_doc("must-not-appear")])
    write_cadence(tmp_path, [make_entry()])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _doc_ids(clean_db) == ["in-manifest-dir", "legacy-root"]


def test_manifest_dir_layout_reads_the_whole_mount(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "us.jsonl", [make_doc("u1")])
    write_manifest(tmp_path / "ecb", "D1.jsonl", [make_doc("e1")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _doc_ids(clean_db) == ["e1", "u1"]


def test_manifest_globs_override(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("u1")])
    write_manifest(tmp_path / "manifest", "fr.jsonl", [make_doc("f1")])
    monkeypatch.setenv("MANIFEST_GLOBS", "manifest/fr.jsonl")
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _doc_ids(clean_db) == ["f1"]


def _live(pg_url):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT doc_id FROM documents WHERE deleted_at IS NULL ORDER BY doc_id")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _seed_then_break(clean_db, tmp_path, monkeypatch, break_file):
    # Run 1: two manifests, four live rows. Run 2: fr.jsonl cannot be read.
    write_manifest(tmp_path, "us.jsonl", [make_doc("u1"), make_doc("u2")])
    fr = write_manifest(tmp_path, "fr.jsonl", [make_doc("f1"), make_doc("f2")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _live(clean_db) == ["f1", "f2", "u1", "u2"]
    break_file(fr)
    # sweep="1.0": the fraction guard is off, so only the failed run stands
    # between an unreadable manifest and the soft-deletion of its rows.
    with pytest.raises(OSError):
        run_ingest(monkeypatch, clean_db, tmp_path, sweep="1.0")
    assert _live(clean_db) == ["f1", "f2", "u1", "u2"]


def test_a_dangling_manifest_symlink_fails_the_run_and_sweeps_nothing(
        clean_db, tmp_path, monkeypatch):
    def dangle(path):
        path.unlink()
        path.symlink_to(tmp_path / "vanished-on-the-share.jsonl")

    _seed_then_break(clean_db, tmp_path, monkeypatch, dangle)


@pytest.mark.skipif(__import__("os").geteuid() == 0,
                    reason="root reads a chmod 000 file anyway")
def test_an_unreadable_manifest_fails_the_run_and_sweeps_nothing(
        clean_db, tmp_path, monkeypatch):
    def lock(path):
        path.chmod(0)

    try:
        _seed_then_break(clean_db, tmp_path, monkeypatch, lock)
    finally:
        (tmp_path / "fr.jsonl").chmod(0o644)
