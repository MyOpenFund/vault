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
        cur.execute("DROP TABLE IF EXISTS runs CASCADE")
    conn.close()
    return pg_url


def _run(pg_url, data_dir):
    import ingest_runs

    conn = psycopg2.connect(pg_url)
    try:
        # ingest_runs.run() issues its own CREATE TABLE IF NOT EXISTS, so no
        # hand-written DDL is needed here (avoids drifting from the module).
        return ingest_runs.run(conn, str(data_dir), "central-bank")
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


def test_type_corrupt_line_does_not_poison_the_batch(clean_runs, tmp_path):
    # A JSON-valid but type-corrupt line (bad exit_code + garbage timestamp)
    # must not make execute_values raise and roll back the whole batch —
    # otherwise, since runs.jsonl is append-only, this line would poison
    # every future ingestion cycle. The good row must land with no exception.
    directory = tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    bad = make_report("bad-1")
    bad["exit_code"] = "three"
    bad["started_at"] = "never o'clock"
    good = make_report("good-1")
    (directory / "runs.jsonl").write_text(
        json.dumps(bad) + "\n" + json.dumps(good) + "\n", encoding="utf-8"
    )
    offered = _run(clean_runs, directory)
    assert offered == 1
    assert _rows(clean_runs, "SELECT run_id FROM runs") == [("good-1",)]


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
        cur.execute("DROP TABLE IF EXISTS runs CASCADE; DROP TABLE IF EXISTS cadence CASCADE;")
    conn.close()

    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    write_runs(tmp_path, [make_report("r1")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _rows(clean_db, "SELECT run_id FROM runs") == [("r1",)]
    assert _rows(clean_db, "SELECT doc_id FROM documents") == [("d1",)]


def test_legacy_tool_identity_is_renamed_by_the_ddl_train(
    clean_db, tmp_path, monkeypatch
):
    # The RAG orchestrator was renamed to data-orchestrator (2026-09-02); rows
    # it already wrote under the old identity must be relabeled so telemetry
    # history isn't split across two `tool` values for the same producer.
    # Unrelated producers (e.g. central-bank-corpus) must be left untouched.
    from .conftest import run_ingest

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS runs CASCADE")
    conn.close()

    run_ingest(monkeypatch, clean_db, tmp_path)  # empty manifest set: DDL only

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, tool) VALUES (%s, %s)",
            ("legacy-1", "rag-orchestrator"),
        )
        cur.execute(
            "INSERT INTO runs (run_id, tool) VALUES (%s, %s)",
            ("cbc-1", "central-bank-corpus"),
        )
    conn.close()

    run_ingest(monkeypatch, clean_db, tmp_path)  # train runs again: migration fires

    rows = _rows(clean_db, "SELECT run_id, tool FROM runs ORDER BY run_id")
    assert rows == [
        ("cbc-1", "central-bank-corpus"),
        ("legacy-1", "data-orchestrator"),
    ]


def test_tool_identity_migration_is_idempotent(clean_db, tmp_path, monkeypatch):
    # Every service run re-executes the full DDL train, so the rename UPDATE
    # must be safe to run repeatedly: once rows are relabeled, later runs
    # must not touch row count or values again.
    from .conftest import run_ingest

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS runs CASCADE")
    conn.close()

    run_ingest(monkeypatch, clean_db, tmp_path)  # empty manifest set: DDL only

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, tool) VALUES (%s, %s)",
            ("legacy-1", "rag-orchestrator"),
        )
    conn.close()

    run_ingest(monkeypatch, clean_db, tmp_path)  # first migration
    first = _rows(clean_db, "SELECT run_id, tool FROM runs ORDER BY run_id")

    run_ingest(monkeypatch, clean_db, tmp_path)  # second run: must be a no-op
    second = _rows(clean_db, "SELECT run_id, tool FROM runs ORDER BY run_id")

    assert second == first
    assert first == [("legacy-1", "data-orchestrator")]


def test_runs_corpus_column_and_backfill(clean_runs, tmp_path):
    # A row ingested before the column existed carried corpus inside extra;
    # the train promotes it. New rows get the service corpus directly.
    conn = psycopg2.connect(clean_runs)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
            "command TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, "
            "outcome TEXT, exit_code INTEGER, totals JSONB, sources JSONB, "
            "extra JSONB, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for run_id, extra in (
            ("old-1", {"corpus": "central-bank", "host": "nas"}),
            ("old-2", {"corpus": None, "host": "nas"}),      # JSON null: nothing to promote
            ("old-3", {"corpus": 42, "host": "nas"}),        # not a string: not a corpus
        ):
            cur.execute(
                "INSERT INTO runs (run_id, tool, extra) VALUES (%s, %s, %s)",
                (run_id, "central-bank-corpus", json.dumps(extra)),
            )
    conn.close()
    write_runs(tmp_path, [make_report("new-1")])
    _run(clean_runs, tmp_path)
    rows = _rows(clean_runs, "SELECT run_id, corpus FROM runs ORDER BY run_id")
    assert rows == [
        ("new-1", "central-bank"),
        ("old-1", "central-bank"),
        ("old-2", None),
        ("old-3", None),
    ]
    assert _rows(clean_runs, "SELECT extra FROM runs WHERE run_id = 'old-1'") == [
        ({"corpus": "central-bank", "host": "nas"},)          # the backfill leaves extra alone
    ]


def test_contradicting_corpus_line_is_rejected(clean_runs, tmp_path):
    write_runs(tmp_path, [make_report("a", corpus="company"), make_report("b")])
    _run(clean_runs, tmp_path)
    assert _rows(clean_runs, "SELECT run_id, corpus FROM runs") == [("b", "central-bank")]


# --- corpus repair on conflict ----------------------------------------------
#
# The producer never emitted `corpus`, so every row ingested before the column
# existed (260 of them on the NAS) stays NULL forever under DO NOTHING: the
# file is re-offered nightly and never touched, and source_health's observed
# half — which joins on (corpus, source_code) — stays empty. DO UPDATE ...
# WHERE runs.corpus IS NULL repairs those rows once, without ever rewriting
# content, and is a no-op (no dead tuple) for every already-repaired row.


def _run_as(pg_url, data_dir, corpus):
    import ingest_runs

    conn = psycopg2.connect(pg_url)
    try:
        return ingest_runs.run(conn, str(data_dir), corpus)
    finally:
        conn.close()


def _plant_pre_column_runs(pg_url, rows):
    """Create `runs` as it was before the corpus column, then insert rows."""
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
            "command TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, "
            "outcome TEXT, exit_code INTEGER, totals JSONB, sources JSONB, "
            "extra JSONB, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for run_id, outcome, totals, extra in rows:
            cur.execute(
                "INSERT INTO runs (run_id, tool, command, outcome, exit_code, "
                "totals, sources, extra) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, "central-bank-corpus", "discover", outcome, 0,
                 json.dumps(totals), json.dumps([]), json.dumps(extra)),
            )
    conn.close()


def test_reoffered_row_gets_corpus_filled_content_untouched(clean_runs, tmp_path):
    _plant_pre_column_runs(
        clean_runs, [("old-1", "ok", {"docs_seen": 1}, {"host": "nas"})]
    )
    # The re-offered line deliberately DIFFERS from the stored row: only
    # `corpus` may move, everything else keeps its ingested value.
    write_runs(tmp_path, [make_report(
        "old-1", outcome="failed", exit_code=9,
        totals={"docs_seen": 999}, host="somewhere-else",
    )])
    _run(clean_runs, tmp_path)
    rows = _rows(
        clean_runs,
        "SELECT corpus, outcome, exit_code, totals, extra FROM runs "
        "WHERE run_id = 'old-1'",
    )
    assert rows == [
        ("central-bank", "ok", 0, {"docs_seen": 1}, {"host": "nas"})
    ]


def test_row_with_a_corpus_is_never_relabelled(clean_runs, tmp_path):
    _plant_pre_column_runs(
        clean_runs,
        [
            # Backfilled from extra by the DDL train -> corpus 'central-bank'.
            ("cb-1", "ok", {"docs_seen": 1}, {"corpus": "central-bank"}),
            ("cb-2", "ok", {"docs_seen": 2}, {"corpus": "central-bank"}),
        ],
    )
    # cb-1's line carries no corpus: it reaches the insert with the service's
    # 'company', but the WHERE runs.corpus IS NULL guard skips the update
    # since cb-1 already has 'central-bank' stored.
    # cb-2's line contradicts 'company' outright and is rejected before insert.
    write_runs(tmp_path, [
        make_report("cb-1", outcome="failed"),
        make_report("cb-2", corpus="central-bank", outcome="failed"),
    ])
    assert _run_as(clean_runs, tmp_path, "company") == 1  # cb-2 rejected
    assert _rows(
        clean_runs, "SELECT run_id, corpus, outcome FROM runs ORDER BY run_id"
    ) == [("cb-1", "central-bank", "ok"), ("cb-2", "central-bank", "ok")]


def test_duplicate_run_id_in_one_file_lands_one_row(clean_runs, tmp_path):
    # execute_values + DO UPDATE would raise "cannot affect row a second time"
    # without the in-batch dedupe.
    write_runs(tmp_path, [
        make_report("dup-1", outcome="ok"),
        make_report("dup-1", outcome="failed"),
        make_report("other-1"),
    ])
    assert _run(clean_runs, tmp_path) == 2
    assert _rows(
        clean_runs, "SELECT run_id, outcome FROM runs ORDER BY run_id"
    ) == [("dup-1", "ok"), ("other-1", "ok")]
