import json

import psycopg2
import pytest

pytestmark = pytest.mark.integration


def make_report(run_id, **over):
    obj = {
        "run_id": run_id, "tool": "central-bank-corpus", "command": "discover",
        "started_at": "2026-08-31T02:00:00+00:00",
        "finished_at": "2026-08-31T02:10:00+00:00",
        "outcome": "ok", "exit_code": 0,
        "totals": {"docs_seen": 1, "docs_new": 1, "docs_failed": 0},
        "sources": [],
    }
    obj.update(over)
    return obj


def write_runs(directory, reports):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "runs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in reports), encoding="utf-8"
    )


@pytest.fixture()
def clean_runs(pg_url):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS runs")
    conn.close()
    return pg_url


def _run(pg_url, data_dir):
    import ingest_runs

    conn = psycopg2.connect(pg_url)
    try:
        # DDL train normally runs in ingest.main(); create here for isolation.
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, "
                "tool TEXT NOT NULL, command TEXT, started_at TIMESTAMPTZ, "
                "finished_at TIMESTAMPTZ, outcome TEXT, exit_code INTEGER, "
                "totals JSONB, sources JSONB, extra JSONB, "
                "ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        conn.commit()
        return ingest_runs.run(conn, str(data_dir))
    finally:
        conn.close()


def _rows(pg_url, sql):
    conn = psycopg2.connect(pg_url)
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    conn.close()
    return rows


def test_append_only_reingest_is_idempotent(clean_runs, tmp_path):
    write_runs(tmp_path, [make_report("r1"), make_report("r2", outcome="degraded", exit_code=3)])
    assert _run(clean_runs, tmp_path) == 2
    assert _run(clean_runs, tmp_path) == 2  # offered again, but…
    rows = _rows(clean_runs, "SELECT run_id, outcome FROM runs ORDER BY run_id")
    assert rows == [("r1", "ok"), ("r2", "degraded")]  # …no duplicates


def test_corrupt_line_skipped_good_kept(clean_runs, tmp_path):
    write_runs(tmp_path, [make_report("r1")])
    with (tmp_path / "runs.jsonl").open("a") as fh:
        fh.write("{torn\n")
        fh.write(json.dumps(make_report("r2")) + "\n")
    _run(clean_runs, tmp_path)
    assert len(_rows(clean_runs, "SELECT run_id FROM runs")) == 2


def test_unknown_fields_land_in_extra(clean_runs, tmp_path):
    write_runs(tmp_path, [make_report("r1", host="nas-01")])
    _run(clean_runs, tmp_path)
    rows = _rows(clean_runs, "SELECT extra FROM runs WHERE run_id = 'r1'")
    assert rows[0][0] == {"host": "nas-01"}


def test_service_run_chains_documents_cadence_runs(clean_db, tmp_path, monkeypatch):
    # end-to-end through ingest.main(): manifest + cadence + runs together
    from .conftest import make_doc, run_ingest, write_manifest

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS cadence;")
    conn.close()

    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    write_runs(tmp_path, [make_report("r1")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _rows(clean_db, "SELECT run_id FROM runs") == [("r1",)]
    assert _rows(clean_db, "SELECT doc_id FROM documents") == [("d1",)]
