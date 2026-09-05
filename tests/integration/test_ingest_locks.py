"""Advisory locks: what two overlapping ingestion runs may and may not do.

vault #3. Nothing stops a second run from starting while the first is still
going. Two guarantees are pinned here: the DDL train never runs twice at
once (it drops and recreates views, and CREATE TABLE IF NOT EXISTS is not
race-safe on a fresh database), and two runs of the same corpus serialize —
while two runs of *different* corpora must stay free to overlap, which is
the whole reason the corpus lock is per-corpus rather than global.
"""

import threading

import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration
TIMEOUT = 60

TABLES = ("documents", "rag_ingestions", "cadence", "runs", "discovery_errors")
VIEWS = (
    "runs_sources", "rag_backlog", "rag_backlog_any",
    "sources_without_cadence", "source_health",
)


def test_lock_keys_block_same_corpus_only(clean_db, tmp_path, monkeypatch):
    # I2 — the helpers themselves, on two independent connections.
    import ingest
    run_ingest(monkeypatch, clean_db, tmp_path)  # DDL only
    a, b = psycopg2.connect(clean_db), psycopg2.connect(clean_db)
    try:
        ingest.acquire_lock(a, ingest.corpus_lock_key("x"))
        with b.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ingest.corpus_lock_key("x"),))
            assert cur.fetchone()[0] is False, "a second run of the same corpus got the lock"
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ingest.corpus_lock_key("y"),))
            assert cur.fetchone()[0] is True, "another corpus was blocked"
        b.commit()
        ingest.release_lock(a, ingest.corpus_lock_key("x"))
        with b.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ingest.corpus_lock_key("x"),))
            assert cur.fetchone()[0] is True, "the lock was not released"
        b.commit()
    finally:
        a.close()
        b.close()


def _run_threads(corpora_to_dirs):
    """Run one ingest.main() per (corpus, data_dir) concurrently.

    Goes through main()'s keyword seam rather than the env vars: DATA_DIR
    and CORPUS are process-global, so two threads cannot disagree on them.
    DATABASE_URL is the same for every thread, so it stays in the env.
    """
    import ingest
    failures = {}

    def target(corpus, data_dir):
        try:
            ingest.main(corpus=corpus, data_dir=str(data_dir))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            failures[threading.current_thread().name] = exc

    threads = [
        threading.Thread(target=target, args=(corpus, data_dir), name=f"{corpus}-{i}",
                         daemon=True)
        for i, (corpus, data_dir) in enumerate(corpora_to_dirs)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT)
        assert not t.is_alive(), f"{t.name} did not finish within {TIMEOUT}s"
    assert not failures, f"a concurrent run raised: {failures}"


def test_concurrent_bootstrap_builds_the_schema_once(clean_db, tmp_path, monkeypatch):
    """I4 — two runs bootstrapping an empty database at the same time.

    The DDL train drops the five views at its head and recreates them at its
    tail; two trains interleaving there can leave a view missing, or raise on
    a duplicate object. Serialized, the schema comes out whole either way.
    """
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    conn.close()

    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "1.0")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run_threads([("central-bank", a), ("central-bank", b)])

    tables = {r[0] for r in fetch_all(
        clean_db,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()",
    )}
    assert set(TABLES) <= tables
    views = {r[0] for r in fetch_all(
        clean_db,
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = current_schema()",
    )}
    assert set(VIEWS) <= views


def test_concurrent_runs_of_different_corpora_both_land(clean_db, tmp_path, monkeypatch):
    """I5 — the corpus lock must not turn into a global one.

    Two corpora ingesting side by side is the normal production shape (one
    service per corpus, same registry). Both runs must complete and both
    documents must be in the table.
    """
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "1.0")
    dirs = {}
    for corpus in ("a", "b"):
        d = tmp_path / corpus
        # The `corpus` field must be on the line too: resolve_corpus rejects a
        # line whose corpus contradicts the service's.
        write_manifest(d, "manifest.jsonl", [make_doc(f"{corpus}1", corpus=corpus)])
        dirs[corpus] = d

    _run_threads([("a", dirs["a"]), ("b", dirs["b"])])

    assert fetch_all(
        clean_db, "SELECT corpus, count(*) FROM documents GROUP BY 1 ORDER BY 1"
    ) == [("a", 1), ("b", 1)]
