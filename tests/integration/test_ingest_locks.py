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

from .conftest import (
    fetch_all, lock_is_free, make_doc, run_ingest, write_manifest,
)

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


class _ConnectionRegistry:
    """psycopg2 stand-in for ingest, remembering each thread's connection.

    Only so that a wedged thread can be prised off the database: a thread
    blocked in `pg_advisory_lock` is not interruptible from here, but its
    connection is — `cancel()` is the one psycopg2 call documented as safe to
    make from another thread, and it ends the statement the wedged thread is
    waiting on. Without that, a single wedge would hold a server-side lock for
    the rest of the session and cascade into every test that follows.
    """

    def __init__(self):
        self.by_thread = {}

    def connect(self, *args, **kwargs):
        conn = psycopg2.connect(*args, **kwargs)
        self.by_thread[threading.current_thread().name] = conn
        return conn

    def __getattr__(self, name):  # anything else ingest reaches for
        return getattr(psycopg2, name)


def _run_threads(corpora_to_dirs):
    """Run one ingest.main() per (corpus, data_dir) concurrently.

    Goes through main()'s keyword seam rather than the env vars: DATA_DIR
    and CORPUS are process-global, so two threads cannot disagree on them.
    DATABASE_URL is the same for every thread, so it stays in the env.
    """
    import ingest
    failures = {}
    registry = _ConnectionRegistry()

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
    real_psycopg2 = ingest.psycopg2
    ingest.psycopg2 = registry
    try:
        for t in threads:
            t.start()
        # Join them ALL before asserting anything: bailing out on the first
        # wedge would leave the others running, still holding connections and
        # locks, into whatever the caller does next.
        for t in threads:
            t.join(timeout=TIMEOUT)
    finally:
        ingest.psycopg2 = real_psycopg2

    wedged = [t for t in threads if t.is_alive()]
    if wedged:
        # Best effort cleanup, then report *every* thread's state — which one
        # got stuck, and behind what, is the whole diagnosis.
        for t in wedged:
            conn = registry.by_thread.get(t.name)
            if conn is None:
                continue
            for call in (conn.cancel, conn.close):
                try:
                    call()
                except Exception:  # noqa: BLE001 — cleanup, never the verdict
                    pass
        for t in wedged:
            t.join(timeout=5)
        states = {t.name: ("alive" if t.is_alive() else "freed") for t in threads}
        pytest.fail(
            f"{[t.name for t in wedged]} did not finish within {TIMEOUT}s; "
            f"thread states after cancelling their connections: {states}; "
            f"failures recorded: {failures}"
        )
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

    import ingest
    assert lock_is_free(clean_db, ingest.DDL_LOCK_KEY), "the DDL lock outlived the runs"
    assert lock_is_free(clean_db, ingest.corpus_lock_key("central-bank")), (
        "the corpus lock outlived the runs"
    )

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

    import ingest
    for corpus in ("a", "b"):
        assert lock_is_free(clean_db, ingest.corpus_lock_key(corpus)), (
            f"corpus {corpus!r}'s lock outlived its run"
        )


def test_a_failed_run_releases_its_locks(clean_db, tmp_path, monkeypatch):
    """The success path is not the one that leaks a lock — the failure one is.

    A run that dies mid-upsert has both taken the corpus lock and left its
    transaction dirty. main()'s `finally` is what has to give the lock back
    there; if it ever stopped doing so, nothing would break until the *next*
    hourly run sat forever in `pg_advisory_lock` behind a session that had
    already gone home. Asserted from a fresh connection: whether Postgres
    itself still considers the key held is the only answer that counts.
    """
    import ingest

    write_manifest(tmp_path, "manifest.jsonl", [make_doc("d1")])
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "1.0")

    def exploding_execute_values(*args, **kwargs):
        raise RuntimeError("upsert blew up mid-run")

    monkeypatch.setattr(ingest, "execute_values", exploding_execute_values)
    with pytest.raises(RuntimeError):
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))

    assert lock_is_free(clean_db, ingest.corpus_lock_key("central-bank")), (
        "a failed run kept the corpus lock — the next run would block forever"
    )
    assert lock_is_free(clean_db, ingest.DDL_LOCK_KEY), (
        "a failed run kept the DDL lock"
    )
