"""The DDL-train gate against a real Postgres (vault #4).

The heart of the issue: with the schema current, a whole ingestion pass must
complete while another session holds an open transaction that has read every
table and view the vault serves -- the shape of Metabase or the MCP mid-query
at night. Before the gate, the train's DROP VIEW / ALTER TABLE queued behind
that reader and every new reader queued behind the train.

Then the other side: when the train must run and cannot get its locks, the run
fails after exactly the configured attempts, names the session in its way
(never its query text), and ingests nothing.

Detecting "the train ran" from the outside: views that are dropped and
recreated get a new OID, so an unchanged OID means no DROP VIEW happened.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest

from .conftest import (
    drop_vault_roles, fetch_all, make_doc, make_entry, role_conn, run_ingest,
    write_cadence, write_manifest,
)

pytestmark = pytest.mark.integration

FIX = Path(__file__).parent.parent / "fixtures" / "discovery_errors_real.jsonl"

TABLES = ("documents", "rag_ingestions", "cadence", "runs", "discovery_errors", "vault_schema")
VIEWS = ("runs_sources", "rag_backlog", "rag_backlog_any",
         "sources_without_cadence", "source_health")

ORCH_PW = "gate-orch-pw"
RO_PW = "gate-ro-pw"

# Short enough that a blocked train fails the test in about a second, long
# enough that an uncontended statement never trips it on a loaded CI runner.
FAST_TIMEOUT = "300ms"
FAST_DELAYS = "0.05,0.05"   # three attempts


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    yield
    drop_vault_roles(pg_url)


@pytest.fixture()
def fast_gate(monkeypatch):
    monkeypatch.setenv("VAULT_DDL_LOCK_TIMEOUT", FAST_TIMEOUT)
    monkeypatch.setenv("VAULT_DDL_RETRY_DELAYS", FAST_DELAYS)


def view_oids(pg_url):
    return dict(fetch_all(
        pg_url,
        "SELECT c.relname, c.oid FROM pg_class c "
        "WHERE c.relkind = 'v' AND c.relnamespace = current_schema()::regnamespace",
    ))


def applied_at(pg_url):
    return fetch_all(pg_url, "SELECT applied_at FROM vault_schema")[0][0]


def full_data_dir(path, docs=("d1", "d2")):
    write_manifest(path / "manifest", "m.jsonl", [make_doc(d) for d in docs])
    write_cadence(path, [make_entry()])
    (path / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "tool": "central-bank-corpus", "outcome": "ok",
                    "finished_at": "2026-11-01T00:00:00Z",
                    "sources": [{"source_code": "us", "docs_seen": 2}]}) + "\n",
        encoding="utf-8",
    )
    shutil.copy(FIX, path / "discovery_errors.jsonl")
    return path


class Reader:
    """A session that has read every table and view and keeps its transaction open."""

    def __init__(self, pg_url, relations=TABLES + VIEWS, application="stuck-reader"):
        self.conn = psycopg2.connect(pg_url, application_name=application)
        with self.conn.cursor() as cur:
            for rel in relations:
                # The literal must never reach the ingester's log (query text).
                cur.execute(f"SELECT count(*) FROM {rel} WHERE 'secret-literal' IS NOT NULL")
                cur.fetchone()
        self.pid = self.conn.get_backend_pid()

    def held(self, pg_url):
        return {r[0] for r in fetch_all(
            pg_url,
            f"SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            f"WHERE l.pid = {self.pid} AND l.granted AND l.mode = 'AccessShareLock'",
        )}

    def release(self):
        if not self.conn.closed:
            self.conn.rollback()
            self.conn.close()


@pytest.fixture()
def reader_factory(clean_db):
    readers = []

    def make(**kwargs):
        r = Reader(clean_db, **kwargs)
        readers.append(r)
        return r

    yield make
    for r in readers:
        r.release()


def test_second_run_skips_the_train(clean_db, tmp_path, monkeypatch, caplog):
    run_ingest(monkeypatch, clean_db, tmp_path)
    before, stamp = view_oids(clean_db), applied_at(clean_db)
    assert set(VIEWS) <= set(before)

    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)

    assert view_oids(clean_db) == before, "a skipped train still dropped a view"
    assert applied_at(clean_db) == stamp
    assert "DDL train skipped" in caplog.text


def test_a_full_pass_completes_beside_a_reader_holding_every_relation(
    clean_db, tmp_path, monkeypatch, reader_factory, fast_gate, caplog
):
    """#4 itself. Every lock the pass takes outside the gated train must coexist
    with ACCESS SHARE. PGOPTIONS gives the WHOLE ingester session a lock_timeout,
    so a regression fails in seconds with LockNotAvailable instead of hanging."""
    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", ORCH_PW)
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)
    data = full_data_dir(tmp_path)
    run_ingest(monkeypatch, clean_db, data)        # schema now current
    oids = view_oids(clean_db)

    reader = reader_factory()
    assert set(TABLES + VIEWS) <= reader.held(clean_db), "the reader is not holding what it should"

    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=2000")
    write_manifest(data / "manifest", "m.jsonl", [make_doc(d) for d in ("d1", "d2", "d3")])
    (data / "runs.jsonl").write_text(
        (data / "runs.jsonl").read_text(encoding="utf-8")
        + json.dumps({"run_id": "r2", "tool": "central-bank-corpus", "outcome": "ok"}) + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, data)

    assert "DDL train skipped" in caplog.text
    assert reader.held(clean_db) >= set(TABLES + VIEWS), "the reader lost its locks mid-test"
    assert fetch_all(clean_db, "SELECT count(*) FROM documents")[0][0] == 3
    assert fetch_all(clean_db, "SELECT count(*) FROM runs")[0][0] == 2
    assert fetch_all(clean_db, "SELECT count(*) FROM cadence")[0][0] == 1
    assert fetch_all(clean_db, "SELECT count(*) FROM discovery_errors")[0][0] == 3
    assert view_oids(clean_db) == oids


def test_a_stale_train_behind_a_stuck_reader_fails_after_every_attempt(
    clean_db, tmp_path, monkeypatch, reader_factory, fast_gate, caplog
):
    import schema_gate

    run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE vault_schema SET fingerprint = 'stale'")
    conn.close()

    reader = reader_factory(relations=("documents",) + VIEWS)
    write_manifest(tmp_path / "manifest", "m.jsonl", [make_doc("new-doc")])
    write_cadence(tmp_path, [make_entry(bank_code="never-ingested")])
    cadence_before = fetch_all(clean_db, "SELECT * FROM cadence ORDER BY 1, 2, 3")

    with caplog.at_level(logging.INFO):
        with pytest.raises(schema_gate.TrainLockTimeout):
            run_ingest(monkeypatch, clean_db, tmp_path)

    attempts = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "DDL train attempt" in r.getMessage()]
    assert len(attempts) == 3
    assert [m.split("attempt ")[1].split(" ")[0] for m in attempts] == ["1/3", "2/3", "3/3"]
    for message in attempts:
        assert f"pid={reader.pid}" in message
        assert "'stuck-reader'" in message
        assert "idle in transaction" in message
    assert "secret-literal" not in caplog.text, "a blocker's query text reached the log"

    # nothing ingested, nothing half-applied
    assert fetch_all(clean_db, "SELECT count(*) FROM documents")[0][0] == 0
    assert fetch_all(clean_db, "SELECT * FROM cadence ORDER BY 1, 2, 3") == cadence_before
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [("stale",)]
    import ingest
    from .conftest import lock_is_free
    assert lock_is_free(clean_db, ingest.DDL_LOCK_KEY)


def test_the_run_exits_non_zero_when_the_train_cannot_lock(
    clean_db, tmp_path, monkeypatch, reader_factory, fast_gate
):
    import subprocess
    import sys

    run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE vault_schema SET fingerprint = 'stale'")
    conn.close()
    reader_factory(relations=VIEWS)

    root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": "/usr/bin:/bin", "DATABASE_URL": clean_db, "DATA_DIR": str(tmp_path),
        "VAULT_DDL_LOCK_TIMEOUT": FAST_TIMEOUT, "VAULT_DDL_RETRY_DELAYS": "none",
    }
    proc = subprocess.run([sys.executable, str(root / "ingestion" / "ingest.py")],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "TrainLockTimeout" in proc.stderr
    assert "attempt 1/1" in proc.stderr


def test_a_reader_released_between_attempts_lets_attempt_two_succeed(
    clean_db, tmp_path, monkeypatch, reader_factory, fast_gate, caplog
):
    import schema_gate

    run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE vault_schema SET fingerprint = 'stale'")
    conn.close()

    reader = reader_factory(relations=VIEWS)
    slept = []

    def release_then_sleep(delay):
        slept.append(delay)
        reader.release()

    monkeypatch.setattr(schema_gate, "_sleep", release_then_sleep)
    write_manifest(tmp_path / "manifest", "m.jsonl", [make_doc("d1")])
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)

    assert slept == [0.05]
    assert "applied on attempt 2/3" in caplog.text
    import ingest
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [(ingest.TRAIN_FINGERPRINT,)]
    assert fetch_all(clean_db, "SELECT count(*) FROM documents")[0][0] == 1


@pytest.mark.parametrize("drop", [
    "DROP VIEW source_health",
    "DROP TABLE cadence CASCADE",
    "DROP TABLE vault_schema",
])
def test_a_dropped_relation_reruns_the_train(clean_db, tmp_path, monkeypatch, caplog, drop):
    run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(drop)
    conn.close()

    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)

    assert "DDL train running" in caplog.text
    missing = [rel for rel in TABLES + VIEWS
               if fetch_all(clean_db, f"SELECT to_regclass('{rel}')") == [(None,)]]
    assert missing == []


def test_force_reruns_a_current_train(clean_db, tmp_path, monkeypatch, caplog):
    run_ingest(monkeypatch, clean_db, tmp_path)
    before = view_oids(clean_db)
    monkeypatch.setenv("VAULT_DDL_FORCE", "1")
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "VAULT_DDL_FORCE" in caplog.text
    assert view_oids(clean_db) != before


def test_invalid_settings_fail_before_touching_the_database(clean_db, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DDL_LOCK_TIMEOUT", "0")
    with pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1
    assert fetch_all(clean_db, "SELECT to_regclass('vault_schema')") == [(None,)]


def test_grants_and_rotation_apply_on_a_skipped_train(clean_db, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", "skip-rot-1")
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)
    run_ingest(monkeypatch, clean_db, tmp_path)
    before = view_oids(clean_db)

    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", "skip-rot-2")
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert view_oids(clean_db) == before, "the train was not skipped"

    role_conn(clean_db, "vault_orchestrator", "skip-rot-2").close()
    with pytest.raises(psycopg2.OperationalError) as exc:
        role_conn(clean_db, "vault_orchestrator", "skip-rot-1")
    assert "password authentication failed" in str(exc.value)
    for role, pw in (("vault_orchestrator", "skip-rot-2"), ("vault_readonly", RO_PW)):
        conn = role_conn(clean_db, role, pw)
        try:
            for view in VIEWS:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {view}")
                    cur.fetchone()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Legacy shapes: each converges in one run and stays converged.


def _exec(pg_url, sql_text):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
    finally:
        conn.close()


LEGACY_DOCUMENTS = """
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    doc_id TEXT UNIQUE NOT NULL,
    {code_columns},
    doc_type TEXT, title TEXT, pdf_url TEXT, source_url TEXT,
    date DATE, year INTEGER, language TEXT, provenance TEXT,
    mime_type TEXT, sha256 TEXT, local_path TEXT,
    created_at {created_type} DEFAULT now(),
    updated_at {updated_type} DEFAULT now(),
    last_seen_at {last_seen_type},
    deleted_at {deleted_type},
    extra JSONB
);
CREATE INDEX idx_documents_bank_code ON documents(bank_code);
"""

TS_COLUMNS = ("created_at", "updated_at", "last_seen_at", "deleted_at")


def _types(pg_url):
    return dict(fetch_all(
        pg_url,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'documents'",
    ))


@pytest.mark.parametrize("naive", [
    TS_COLUMNS,                          # every column naive
    ("last_seen_at", "deleted_at"),      # created/updated already migrated: the old guard missed these
    ("created_at",),                     # one column alone
])
def test_naive_timestamps_converge_column_by_column(clean_db, tmp_path, monkeypatch, naive):
    types = {c: ("TIMESTAMP" if c in naive else "TIMESTAMPTZ") for c in TS_COLUMNS}
    _exec(clean_db, LEGACY_DOCUMENTS.format(
        code_columns="bank_code TEXT",
        created_type=types["created_at"], updated_type=types["updated_at"],
        last_seen_type=types["last_seen_at"], deleted_type=types["deleted_at"],
    ) + "SET TIME ZONE 'UTC';"
        "INSERT INTO documents (doc_id, bank_code, created_at, updated_at, last_seen_at, deleted_at) "
        "VALUES ('gone', 'us', '2020-01-01 12:00', '2020-01-02 12:00', "
        "'2020-01-03 12:00', '2020-01-04 12:00');")

    write_manifest(tmp_path, "m.jsonl", [make_doc("fresh")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    got = _types(clean_db)
    assert {c: got[c] for c in TS_COLUMNS} == {c: "timestamp with time zone" for c in TS_COLUMNS}
    row = fetch_all(clean_db, "SELECT created_at, updated_at, last_seen_at "
                              "FROM documents WHERE doc_id = 'gone'")[0]
    # read as UTC: the naive values were written as UTC
    assert row == (datetime(2020, 1, 1, 12, tzinfo=timezone.utc),
                   datetime(2020, 1, 2, 12, tzinfo=timezone.utc),
                   datetime(2020, 1, 3, 12, tzinfo=timezone.utc))

    run_ingest(monkeypatch, clean_db, tmp_path)   # stays converged
    assert {c: _types(clean_db)[c] for c in TS_COLUMNS} == {
        c: "timestamp with time zone" for c in TS_COLUMNS}


def test_bank_code_alone_is_renamed(clean_db, tmp_path, monkeypatch):
    _exec(clean_db, LEGACY_DOCUMENTS.format(
        code_columns="bank_code TEXT", created_type="TIMESTAMPTZ", updated_type="TIMESTAMPTZ",
        last_seen_type="TIMESTAMPTZ", deleted_type="TIMESTAMPTZ",
    ) + "INSERT INTO documents (doc_id, bank_code) VALUES ('old', 'fr');")

    run_ingest(monkeypatch, clean_db, tmp_path)

    cols = _types(clean_db)
    assert "bank_code" not in cols and "source_code" in cols
    assert fetch_all(clean_db, "SELECT doc_id, source_code FROM documents") == [("old", "fr")]


def test_both_code_columns_with_data_backfill_and_keep_bank_code(
    clean_db, tmp_path, monkeypatch, caplog
):
    # Half-applied by hand, or two deploys racing: both columns exist and both
    # carry data. Renaming would fail (source_code exists), dropping would
    # lose data. source_code wins where set, bank_code fills its NULLs, and
    # bank_code stays for a human to inspect.
    _exec(clean_db, LEGACY_DOCUMENTS.format(
        code_columns="bank_code TEXT, source_code TEXT",
        created_type="TIMESTAMPTZ", updated_type="TIMESTAMPTZ",
        last_seen_type="TIMESTAMPTZ", deleted_type="TIMESTAMPTZ",
    ) + """
        INSERT INTO documents (doc_id, bank_code, source_code) VALUES
            ('only-bank', 'us', NULL),
            ('both', 'fr', 'de'),
            ('only-source', NULL, 'ecb'),
            ('neither', NULL, NULL);
    """)

    with caplog.at_level(logging.WARNING):
        run_ingest(monkeypatch, clean_db, tmp_path)

    assert fetch_all(clean_db, "SELECT doc_id, bank_code, source_code FROM documents "
                               "ORDER BY doc_id") == [
        ("both", "fr", "de"),
        ("neither", None, None),
        ("only-bank", "us", "us"),
        ("only-source", None, "ecb"),
    ]
    warning = [r.getMessage() for r in caplog.records
               if "bank_code" in r.getMessage() and "source_code" in r.getMessage()]
    assert warning, "the both-columns case was not reported"

    monkeypatch.setenv("VAULT_DDL_FORCE", "1")    # the train again, on the converged shape
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT count(*) FROM documents WHERE bank_code IS NOT NULL")[0][0] == 2


def test_runs_pass_adds_the_corpus_column_and_renames_the_old_tool(clean_db, tmp_path, monkeypatch):
    # The runs pass runs on every ingestion, train skipped or not: its DDL is
    # guarded (no lock when the column exists) and its rename is row-level.
    run_ingest(monkeypatch, clean_db, tmp_path)
    _exec(clean_db, "INSERT INTO runs (run_id, tool) VALUES ('legacy-1', 'rag-orchestrator')")
    before = view_oids(clean_db)
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "tool": "central-bank-corpus"}) + "\n", encoding="utf-8")

    run_ingest(monkeypatch, clean_db, tmp_path)

    assert view_oids(clean_db) == before, "the train was not skipped"
    assert fetch_all(clean_db, "SELECT run_id, tool, corpus FROM runs "
                               "WHERE run_id IN ('legacy-1', 'r1') ORDER BY run_id") == [
        ("legacy-1", "data-orchestrator", None),
        ("r1", "central-bank-corpus", "central-bank"),
    ]


def test_runs_standalone_ddl_adds_a_missing_corpus_column(clean_db, monkeypatch, tmp_path):
    import ingest_runs

    _exec(clean_db, "DROP TABLE IF EXISTS runs CASCADE; "
                    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, tool TEXT NOT NULL, "
                    "command TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, "
                    "outcome TEXT, exit_code INTEGER, totals JSONB, sources JSONB, "
                    "extra JSONB, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now());"
                    "INSERT INTO runs (run_id, tool, extra) VALUES "
                    "('old', 'rag-orchestrator', '{\"corpus\": \"central-bank\"}');")
    conn = psycopg2.connect(clean_db)
    try:
        with conn.cursor() as cur:
            cur.execute(ingest_runs.CREATE_RUNS_SQL)
            cur.execute(ingest_runs.CREATE_RUNS_SQL)   # twice: idempotent
        conn.commit()
    finally:
        conn.close()
    assert fetch_all(clean_db, "SELECT run_id, tool, corpus FROM runs") == [
        ("old", "data-orchestrator", "central-bank")]
