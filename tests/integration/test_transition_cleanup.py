"""The documents-contract transition cleanup (PR 6) against a real Postgres.

The deploy-day shape: a database built and fingerprinted by master eff1aec
(the train deployed on the NAS on 2026-11-20, frozen in
tests/fixtures/pre_cleanup_train.sql), roles provisioned, documents and
rag_ingestions rows. Its train created the transitional unique index
`rag_ingestions_doc_collection_transition (doc_id, collection)` that kept the
pre-contract data-orchestrator's `ON CONFLICT (doc_id, collection)` working.
The first run of this code re-runs the train once (its text changed): the
index is dropped, no value is touched, the views stay readable; the next run
skips the train and a forced one is a no-op. From then on only the contract's
key `(corpus, doc_id, collection)` serves an upsert.
"""

import logging
from pathlib import Path

import psycopg2
import pytest

from .conftest import drop_vault_roles, fetch_all, role_conn, run_ingest

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pre_cleanup_train.sql"
_HEADER_LINES = 4
PRE_CLEANUP_TRAIN = "".join(
    FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)[_HEADER_LINES:])
# sha256 of master eff1aec's CREATE_TABLE_SQL: what the NAS vault_schema holds
# since the chantier 4 deploy (step 1, 2026-11-20).
PRE_CLEANUP_FINGERPRINT = "d96f2c37d33a125965863bd8353224c46634f922362b86f8c897122730923390"
TRANSITION_INDEX = "rag_ingestions_doc_collection_transition"

ORCH_PW = "cleanup-orch-pw"
RO_PW = "cleanup-ro-pw"
CB = "central-bank"

# The pre-contract data-orchestrator's statement (master 81ead10), and the
# contract's (data-orchestrator 2695b26, the one deployed).
OLD_ORCHESTRATOR_UPSERT = """
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
NEW_ORCHESTRATOR_UPSERT = OLD_ORCHESTRATOR_UPSERT.replace(
    "ON CONFLICT (doc_id, collection)", "ON CONFLICT (corpus, doc_id, collection)")


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    yield
    drop_vault_roles(pg_url)


def _exec(pg_url, sql, params=None):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _indexes(pg_url):
    return {name for (name,) in fetch_all(
        pg_url, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}


def _data(pg_url):
    return (
        fetch_all(pg_url, "SELECT id, corpus, doc_id, source_code, title, local_path, "
                          "date_precision, date_kind, created_at, updated_at, last_seen_at, "
                          "deleted_at, extra FROM documents ORDER BY corpus, doc_id"),
        fetch_all(pg_url, "SELECT corpus, doc_id, collection, chunk_count, ingested_at "
                          "FROM rag_ingestions ORDER BY corpus, doc_id, collection"),
    )


def _build_pre_cleanup_database(pg_url):
    """Schema, fingerprint, roles and data as master eff1aec leaves them."""
    import roles
    import schema_gate

    assert schema_gate.fingerprint(PRE_CLEANUP_TRAIN) == PRE_CLEANUP_FINGERPRINT, \
        "the fixture is no longer a verbatim copy of the eff1aec train"
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(PRE_CLEANUP_TRAIN)
            cur.execute("INSERT INTO vault_schema (singleton, fingerprint) VALUES (TRUE, %s)",
                        (PRE_CLEANUP_FINGERPRINT,))
            roles.ensure_roles(cur, {"vault_orchestrator": ORCH_PW, "vault_readonly": RO_PW})
            cur.execute(
                "INSERT INTO documents (doc_id, corpus, source_code, doc_type, title, date, year, "
                "date_precision, date_kind, language, provenance, local_path, last_seen_at) "
                "VALUES "
                "('cb-1', 'central-bank', 'ecb', 'A1', 't1', '2022-07-21', 2022, 'day', "
                " 'meeting', 'en', 'bank_site', 'raw/ecb/A1/2022/cb-1.pdf', now()), "
                "('cb-2', 'central-bank', 'us', 'C1', 't2', NULL, NULL, NULL, NULL, 'en', "
                " 'bis_index', NULL, now()), "
                "('cb-1', 'company', 'sec', 'A1', 'twin', NULL, NULL, NULL, NULL, 'en', "
                " 'sec_api', NULL, now())")
            cur.execute(
                "INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code, "
                "embedding_model, chunk_count) VALUES "
                "('cb-1', 'c1', 'central-bank', 'ecb', 'e5', 3)")
        conn.commit()
    finally:
        conn.close()


def _set_roles(monkeypatch):
    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", ORCH_PW)
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)


def test_the_first_run_drops_the_transitional_index_once_with_data_intact(
    clean_db, tmp_path, monkeypatch, caplog
):
    import ingest

    _build_pre_cleanup_database(clean_db)
    assert TRANSITION_INDEX in _indexes(clean_db)
    before = _data(clean_db)

    _set_roles(monkeypatch)
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)          # empty data dir: no upsert, no sweep
    assert f"DDL train running: train changed (recorded {PRE_CLEANUP_FINGERPRINT[:12]}" \
        in caplog.text
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [
        (ingest.TRAIN_FINGERPRINT,)]
    assert TRANSITION_INDEX not in _indexes(clean_db)
    assert _data(clean_db) == before, "the train touched a value"
    # The views the train recreated still read both keys.
    assert fetch_all(clean_db, "SELECT corpus, doc_id FROM rag_backlog_any "
                               "ORDER BY corpus, doc_id") == [(CB, "cb-2"), ("company", "cb-1")]

    # The readonly role still reads the views; the orchestrator still upserts
    # under the contract's key.
    ro = role_conn(clean_db, "vault_readonly", RO_PW)
    try:
        with ro.cursor() as cur:
            cur.execute("SELECT count(*) FROM rag_backlog")
            assert cur.fetchone() == (2,)
    finally:
        ro.close()
    orch = role_conn(clean_db, "vault_orchestrator", ORCH_PW)
    try:
        with orch.cursor() as cur:
            for chunks in (4, 5):      # insert branch, then the DO UPDATE branch
                cur.execute(NEW_ORCHESTRATOR_UPSERT, ("cb-2", "c1", CB, "us", "e5", "v1", chunks))
        orch.commit()
    finally:
        orch.close()
    assert fetch_all(clean_db, "SELECT corpus, doc_id, chunk_count FROM rag_ingestions "
                               "ORDER BY doc_id") == [(CB, "cb-1", 3), (CB, "cb-2", 5)]

    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "DDL train skipped" in caplog.text


def test_a_forced_train_rerun_is_a_no_op(clean_db, tmp_path, monkeypatch, caplog):
    _build_pre_cleanup_database(clean_db)
    run_ingest(monkeypatch, clean_db, tmp_path)
    after_first = (_data(clean_db), _indexes(clean_db))
    monkeypatch.setenv("VAULT_DDL_FORCE", "1")
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "DDL train running: forced" in caplog.text
    assert (_data(clean_db), _indexes(clean_db)) == after_first


def test_a_fresh_database_never_gets_the_transitional_index(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert TRANSITION_INDEX not in _indexes(clean_db)


def test_the_pre_contract_orchestrator_upsert_now_fails_with_no_matching_constraint(
    clean_db, tmp_path, monkeypatch
):
    """Its ON CONFLICT (doc_id, collection) needs a unique index on exactly
    those columns; with the transitional one gone Postgres refuses the
    statement and nothing is written."""
    _build_pre_cleanup_database(clean_db)
    _exec(clean_db, OLD_ORCHESTRATOR_UPSERT, ("cb-2", "c1", CB, "us", "e5", "v1", 1))  # works today
    run_ingest(monkeypatch, clean_db, tmp_path)
    with pytest.raises(psycopg2.errors.InvalidColumnReference,
                       match="no unique or exclusion constraint matching the ON CONFLICT"):
        _exec(clean_db, OLD_ORCHESTRATOR_UPSERT, ("cb-2", "c1", CB, "us", "e5", "v1", 2))
    assert fetch_all(clean_db, "SELECT doc_id, chunk_count FROM rag_ingestions "
                               "ORDER BY doc_id") == [("cb-1", 3), ("cb-2", 1)]
