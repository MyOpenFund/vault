"""Acceptance test for chantier A: the data-orchestrator's real SQL, run as
`vault_orchestrator` over a real role connection.

The orchestrator lives in another repo and MUST NOT be imported here (the vault
owns the schema; it does not depend on its consumers). So its statements are
copied verbatim below, each under a comment naming the file it came from. That
copy is the contract: if the orchestrator's SQL ever changes shape — a new
column, a new ON CONFLICT branch, a new table — this is the test where the
mismatch with the grant surface must surface, loudly, before production does.

Two halves, and both matter. The allowed half proves the surface is WIDE
enough: every statement the orchestrator issues really runs, its writes really
land, and the read-modify-write branches (ON CONFLICT DO UPDATE, ON CONFLICT DO
NOTHING) are each exercised. The forbidden half proves it is NARROW enough: the
database itself refuses everything else, so a bug — or a compromise — in the
off-host orchestrator cannot rewrite a title, tombstone a document, or drop a
view.
"""
import json

import psycopg2
import pytest

from .conftest import (drop_vault_roles, fetch_all, make_doc, role_conn,
                       run_ingest, write_manifest)

pytestmark = pytest.mark.integration

ORCH_PW = "orch-pw-B"
RO_PW = "ro-pw-B"

CORPUS = "central-bank"
COLLECTION = "c1"

TABLES = ("documents", "runs", "rag_ingestions", "cadence", "discovery_errors")
VIEWS = ("runs_sources", "rag_backlog", "rag_backlog_any",
         "sources_without_cadence", "source_health")

# --- copied VERBATIM from the data-orchestrator repo (cannot be imported) -----
# data_orchestrator/vault.py — VaultLedger's resume probe
RESUME_SQL = "SELECT doc_id FROM rag_ingestions WHERE collection = %s"

# data_orchestrator/vault.py — INSERT_RUN_SQL, as rendered from _RUN_COLUMNS
# (the module builds the column list and the placeholders with str.format).
INSERT_RUN_SQL = """
INSERT INTO runs (run_id, tool, command, started_at, finished_at, outcome, exit_code, totals, sources, extra)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO NOTHING
"""

# data_orchestrator/vault.py — VaultLedger.mark
UPSERT_INGESTION_SQL = """
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

# data_orchestrator/probe.py
SELECT_UNPROBED_SQL = """
SELECT doc_id, local_path FROM documents
WHERE corpus = %s AND has_text_layer IS NULL
  AND deleted_at IS NULL AND local_path IS NOT NULL
ORDER BY doc_id
"""

UPDATE_FACTS_SQL = """
UPDATE documents SET has_text_layer = %s, page_count = %s WHERE doc_id = %s
"""

# data_orchestrator/sources/vault.py — build_selection_sql with no filters
# (each optional filter only ANDs another `d.<column> = ANY(...)` clause onto
# the same shape, so the unfiltered form is the privilege-relevant one).
SELECTION_SQL = (
    "SELECT d.doc_id, d.corpus, d.source_code, d.doc_type, d.title, d.date, "
    "d.year, d.language, d.sha256, d.provenance, d.local_path, d.extra\n"
    "FROM documents d\n"
    "LEFT JOIN rag_ingestions r\n"
    "  ON r.doc_id = d.doc_id AND r.collection = %(collection)s\n"
    "WHERE d.corpus = %(corpus)s AND d.deleted_at IS NULL AND r.doc_id IS NULL\n"
    "ORDER BY d.doc_id"
)
# -----------------------------------------------------------------------------

# Everything the orchestrator must NOT be able to do. Each line is a plausible
# accident or a plausible attack, not a synthetic one: a botched sweep, a
# "fix-up" UPDATE, a migration run against the wrong database.
FORBIDDEN = [
    "UPDATE documents SET title = 'x'",
    "UPDATE documents SET deleted_at = now()",
    "UPDATE documents SET local_path = '/tmp/x'",
    "DELETE FROM documents",
    "INSERT INTO documents (doc_id, corpus) VALUES ('z', 'central-bank')",
    "TRUNCATE documents",
    "UPDATE runs SET tool = 'x'",
    "DELETE FROM runs",
    "TRUNCATE runs",
    "DELETE FROM rag_ingestions",
    "DELETE FROM cadence",
    "UPDATE discovery_errors SET resolved_at = now()",
    "CREATE TABLE t_evil (x int)",
    "DROP VIEW source_health",
    "DROP TABLE runs",
]


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    """The roles this module provisions are cluster-level; hand them back."""
    yield
    drop_vault_roles(pg_url)


@pytest.fixture()
def seeded(clean_db, tmp_path, monkeypatch):
    """A fresh schema with two documents and both roles provisioned."""
    write_manifest(tmp_path / "manifest", "m.jsonl",
                   [make_doc("d1"), make_doc("d2")])
    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", ORCH_PW)
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)
    run_ingest(monkeypatch, clean_db, tmp_path, corpus=CORPUS)
    return clean_db


@pytest.fixture()
def orch(seeded):
    """The orchestrator's own connection: transactional, like the real one.

    NOT autocommit — the orchestrator commits explicitly and wraps each probe
    UPDATE in its own SAVEPOINT, which is only legal inside a transaction
    block. Running the statements the way the orchestrator runs them is the
    point of this module.

    A fixture and not a plain helper because teardown must happen even when an
    assertion fails: a leaked open transaction keeps an ACCESS SHARE lock on
    `documents`, and the next test's `clean_db` DROP TABLE ... CASCADE then
    blocks on it forever — the suite hangs instead of reporting one red test.
    """
    conn = role_conn(seeded, "vault_orchestrator", ORCH_PW)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def readonly(seeded):
    """Same, for vault_readonly."""
    conn = role_conn(seeded, "vault_readonly", RO_PW)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_every_statement_the_orchestrator_issues_still_works(seeded, orch):
    conn = orch
    with conn.cursor() as cur:
        # 1. VaultLedger.__init__: nothing ingested into this collection yet.
        cur.execute(RESUME_SQL, (COLLECTION,))
        assert cur.fetchall() == []

        # 2. sources/vault.iter_items: both documents are new work.
        cur.execute(SELECTION_SQL, {"collection": COLLECTION, "corpus": CORPUS})
        assert [row[0] for row in cur.fetchall()] == ["d1", "d2"]

        # 3. probe.run_probe: select, then UPDATE inside its own SAVEPOINT.
        cur.execute(SELECT_UNPROBED_SQL, (CORPUS,))
        assert [row[0] for row in cur.fetchall()] == ["d1", "d2"]
        cur.execute("SAVEPOINT probe_doc")
        cur.execute(UPDATE_FACTS_SQL, (True, 12, "d1"))
        cur.execute("RELEASE SAVEPOINT probe_doc")

        # 4. VaultLedger.mark, twice: the second call takes the ON CONFLICT DO
        #    UPDATE branch, which needs UPDATE on rag_ingestions, not just
        #    INSERT. One call would pass with a grant surface that is wrong.
        cur.execute(UPSERT_INGESTION_SQL,
                    ("d1", COLLECTION, CORPUS, "us", "e5-base", "v1", 7))
        cur.execute(UPSERT_INGESTION_SQL,
                    ("d1", COLLECTION, CORPUS, "us", "e5-base", "v1", 9))

        # 5. insert_run_report, twice: the second takes ON CONFLICT DO NOTHING,
        #    which needs no UPDATE — the asymmetry with rag_ingestions is why
        #    runs is granted INSERT only.
        # `extra` is the sweep of every report key outside _RUN_COLUMNS
        # (cli._fatal_report's "error" is the one that always shows up).
        run_row = ("r1", "data-orchestrator", "ingest", None, None,
                   "ok", 0, json.dumps({"docs": 2}), json.dumps(["us"]),
                   json.dumps({"error": "boom"}))
        cur.execute(INSERT_RUN_SQL, run_row)
        cur.execute(INSERT_RUN_SQL, run_row)

        # 6. the next pass's selection really excludes what was just marked:
        #    the anti-join works under the role, it is not just permitted.
        cur.execute(SELECTION_SQL, {"collection": COLLECTION, "corpus": CORPUS})
        assert [row[0] for row in cur.fetchall()] == ["d2"]
        cur.execute(RESUME_SQL, (COLLECTION,))
        assert [row[0] for row in cur.fetchall()] == ["d1"]
    conn.commit()

    # The writes really landed, seen from the owner's connection.
    assert fetch_all(seeded, "SELECT doc_id, has_text_layer, page_count "
                             "FROM documents ORDER BY doc_id") == [
        ("d1", True, 12), ("d2", None, None)]
    # the DO UPDATE branch really updated
    assert fetch_all(seeded, "SELECT chunk_count FROM rag_ingestions "
                             "WHERE doc_id = 'd1'") == [(9,)]
    # DO NOTHING: the second insert is a no-op, one row, its payload intact
    assert fetch_all(seeded, "SELECT totals ->> 'docs', sources ->> 0, "
                             "extra ->> 'error' FROM runs "
                             "WHERE run_id = 'r1'") == [("2", "us", "boom")]


@pytest.mark.parametrize("stmt", FORBIDDEN)
def test_forbidden_statements_are_denied_by_the_database(orch, stmt):
    conn = orch
    conn.autocommit = True  # each statement stands alone; no aborted-tx cascade
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        with conn.cursor() as cur:
            cur.execute(stmt)


def test_readonly_reads_everything_and_writes_nothing(readonly):
    conn = readonly
    for relation in TABLES + VIEWS:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation}")
            assert cur.fetchone() is not None, f"cannot read {relation}"
    # The one write the orchestrator IS allowed is exactly the one Metabase,
    # the API and the future agent must not have.
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        with conn.cursor() as cur:
            cur.execute(INSERT_RUN_SQL,
                        ("r2", "data-orchestrator", "ingest", None, None,
                         "ok", 0, None, None, None))
