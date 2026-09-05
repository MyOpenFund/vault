"""Two ingest.main() runs racing each other against the same database.

Nothing stops a second run from starting while the first is still going —
a slow nightly cron overlapping the next one, or an operator kicking off a
manual re-ingest. Both runs then want to upsert over the same rows with
their own run timestamp, and `last_seen_at` is what the soft-delete sweep
trusts to decide which documents have vanished from the share. This test
pins what an overlapping pair must never do to the registry.

Two mechanisms keep that safe, and this module exercises both at once:

* the per-corpus advisory lock (`vault-ingest-<corpus>`) serializes runs of
  the same corpus — the second run *waits*, it is never skipped — so the
  two data passes never interleave at all; and
* `last_seen_at = GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at)`
  in the upsert guards the stamp for any writer that bypasses the lock
  (an older deployment, a hand-run script, a psql session).

The two manifests overlap only partially — each run also carries one doc_id
the other has never heard of — so "no row was lost" means something beyond
"the upsert is idempotent": neither run may erase what the other contributed.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from psycopg2.extras import execute_values as real_execute_values

import ingest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

# psycopg2's own execute_values and ingest's own acquire_lock, captured at
# import time — never whatever a previous iteration left patched onto the
# module. The hooks below must delegate to these, or they nest on each other
# across the three iterations.
REAL_ACQUIRE_LOCK = ingest.acquire_lock

LATE = "ingest-late"  # the run holding the NEWER run timestamp
EARLY = "ingest-early"  # the run holding the OLDER one
CORPUS = "central-bank"  # what run_ingest() puts in the CORPUS env var
SHARED = ("d_shared1", "d_shared2")
ONLY = {LATE: "d_only_late", EARLY: "d_only_early"}
ALL_DOC_IDS = sorted(SHARED + tuple(ONLY.values()))
RUN_TIMEOUT = 60  # seconds; generous, only ever hit when a run wedges
# Seconds a rendezvous below may wait for the other thread. Deliberately well
# under RUN_TIMEOUT: when the overlap does not happen (the corpus lock gone
# from main(), say) the waits must expire early enough for both runs to still
# finish and be judged by the assertions, rather than tripping the join
# timeout and reporting "a run wedged" for what is really a lock regression.
ORDER_TIMEOUT = 15


class _PerThreadClock:
    """datetime stand-in handing each racing run its own fixed run timestamp.

    ingest.main() stamps every row with `datetime.now(timezone.utc)`. Pinning
    that per thread is what makes the race's outcome assertable instead of
    dependent on how the two wall-clock reads happened to land.
    """

    def __init__(self, stamps):
        self._stamps = stamps

    def now(self, tz=None):
        return self._stamps[threading.current_thread().name]


def _run_both_concurrently(monkeypatch, stamps, manifests):
    """Start both ingest.main() runs so that they genuinely overlap.

    Two test-side hooks are pure environment, not behaviour: each thread reads
    its own manifest (DATA_DIR is process-global, so the scan is what has to be
    per-thread), and each gets a fixed run timestamp.

    Two more only *observe* production control flow, and between them force a
    real, contended overlap instead of hoping for one:

    * `execute_values` is wrapped in a counter of runs sitting in the upsert
      (its peak over the pair is what this returns; under the per-corpus lock
      it can only ever be 1). LATE's first trip through that wrapper parks —
      inside its transaction, holding the corpus lock — until EARLY reaches
      the lock.
    * `acquire_lock` is wrapped so that EARLY, on its way into the corpus lock,
      announces itself before delegating to the real function. It is not gated
      by anything: it walks straight into `pg_try_advisory_lock`.

    So the interleaving is deterministic without any test-side choreography of
    the *outcome*: EARLY starts only once LATE is parked mid-upsert with the
    lock held, EARLY's try then fails and it blocks in `pg_advisory_lock`,
    which releases LATE, which finishes and unlocks; EARLY runs second. The
    contention is real — the database, not the test, is what makes EARLY wait.

    Delete the corpus lock from main() and the whole thing degrades exactly
    the way it should: EARLY's wrapper is never reached, LATE's park expires on
    ORDER_TIMEOUT, and by then EARLY has been upserting alongside it — peak 2.
    """
    in_flight = 0
    peak = 0
    guard = threading.Lock()
    corpus_lock = ingest.corpus_lock_key(CORPUS)
    late_at_upsert = threading.Event()  # LATE holds the lock and is mid-upsert
    early_at_corpus_lock = threading.Event()  # EARLY is about to reach for it

    def per_thread_manifests(_root):
        return [str(manifests[threading.current_thread().name])]

    def announcing_acquire_lock(conn, key):
        if threading.current_thread().name == EARLY and key == corpus_lock:
            early_at_corpus_lock.set()
        return REAL_ACQUIRE_LOCK(conn, key)

    def probed_execute_values(cur, sql, rows, **kwargs):
        nonlocal in_flight, peak
        # Counted from the moment a run enters the upsert, park included: a
        # second run reaching this point while LATE is parked is exactly the
        # concurrency the lock is supposed to make impossible, and counting
        # only the psycopg2 call would let it slip through unseen.
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            if threading.current_thread().name == LATE:
                late_at_upsert.set()
                # No assert on the result: an expired wait is a legitimate
                # outcome that the peak assertion is there to report.
                early_at_corpus_lock.wait(timeout=ORDER_TIMEOUT)
            real_execute_values(cur, sql, rows, **kwargs)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(ingest, "datetime", _PerThreadClock(stamps))
    monkeypatch.setattr(ingest, "find_jsonl_files", per_thread_manifests)
    monkeypatch.setattr(ingest, "execute_values", probed_execute_values)
    monkeypatch.setattr(ingest, "acquire_lock", announcing_acquire_lock)

    failures = {}

    def target():
        try:
            ingest.main()
        except BaseException as exc:  # noqa: BLE001 — surfaced in the main thread
            failures[threading.current_thread().name] = exc

    threads = {
        name: threading.Thread(target=target, name=name, daemon=True)
        for name in (LATE, EARLY)
    }
    threads[LATE].start()
    # Hold EARLY back only until LATE is demonstrably inside the lock. Waiting
    # on the event rather than sleeping a guessed interval is what makes "LATE
    # goes first" a fact instead of a hope; if LATE never gets there the wait
    # expires and EARLY starts anyway, so a broken LATE surfaces as its own
    # failure below rather than as a hang.
    late_at_upsert.wait(timeout=ORDER_TIMEOUT)
    threads[EARLY].start()
    for t in threads.values():
        t.join(timeout=RUN_TIMEOUT)
        assert not t.is_alive(), f"{t.name} did not finish within {RUN_TIMEOUT}s"
    assert not failures, f"a racing run raised: {failures}"
    return peak


def test_concurrent_ingest_runs_do_not_lose_or_duplicate_rows(
    clean_db, tmp_path, monkeypatch, caplog
):
    """Overlapping runs must serialize, and keep every document, once, fresh.

    The per-corpus lock is the first guarantee: no two runs of the same
    corpus may be upserting at the same moment, so the in-flight probe's peak
    must be 1, and the run that lost the race must say so — the "waiting for
    it" warning is the direct evidence that the two really did contend rather
    than politely following one another. The registry afterwards must hold
    each doc_id exactly once — including the one only the other run saw —
    none of them tombstoned, and every shared document stamped with the LATEST
    of the two run timestamps. A row rewound to an older stamp looks stale to
    the next run's sweep and gets soft-deleted while still very much present
    on the share.

    The order is deterministic here (see `_run_both_concurrently`): LATE
    upserts first with the newer stamp, EARLY second with the older one. That
    is the ordering the GREATEST guard exists for, and the only one of the two
    that tests it — the shared rows already carry LATE's stamp when EARLY's
    upsert lands on them, so without GREATEST they are rewound to EARLY's and
    the last assertion below fires.

    SWEEP_MAX_DELETE_FRACTION is left at its production default here, and the
    partially overlapping manifests put both sweeps on record. LATE, going
    first, sees `d_only_early` still carrying the previous run's older stamp:
    1 sweep candidate out of 4 live rows = 25%, far above the 5% default, so
    the torn-share guard blocks the sweep. EARLY, going second, has no
    candidates at all — `d_only_late` was just stamped with LATE's newer
    timestamp, and the shared rows with it. Neither run may tombstone anything.
    """
    manifests = {
        name: write_manifest(
            tmp_path / name,
            "corpus.jsonl",
            [make_doc(d) for d in SHARED + (only,)],
        )
        for name, only in ONLY.items()
    }
    # Seed schema and rows first, so the race is a pure update race and not
    # two copies of the DDL train fighting over ACCESS EXCLUSIVE locks.
    write_manifest(
        tmp_path / "seed", "corpus.jsonl", [make_doc(d) for d in ALL_DOC_IDS]
    )
    run_ingest(monkeypatch, clean_db, tmp_path / "seed", corpus=CORPUS)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "0.05")

    contention = f"lock {ingest.corpus_lock_key(CORPUS)!r} — waiting for it"
    t0 = datetime.now(timezone.utc)
    for iteration in range(3):
        stamps = {
            EARLY: t0 + timedelta(seconds=2 * iteration + 1),
            LATE: t0 + timedelta(seconds=2 * iteration + 2),
        }
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="ingest"):
            peak = _run_both_concurrently(monkeypatch, stamps, manifests)

        rows = fetch_all(
            clean_db,
            "SELECT doc_id, deleted_at, last_seen_at FROM documents "
            "ORDER BY doc_id",
        )
        where = f"iteration {iteration}"
        assert peak == 1, (
            f"{where}: two runs upserted concurrently — the per-corpus lock "
            "is not held"
        )
        assert any(contention in r.getMessage() for r in caplog.records), (
            f"{where}: no run ever waited on the corpus lock — the two passes "
            "did not overlap, so nothing here was actually contended"
        )
        assert [r[0] for r in rows] == ALL_DOC_IDS, (
            f"{where}: a doc_id was lost or duplicated"
        )
        assert all(r[1] is None for r in rows), (
            f"{where}: a racing sweep tombstoned a live document"
        )

        seen = {r[0]: r[2] for r in rows}
        assert seen[ONLY[LATE]] == stamps[LATE], (
            f"{where}: the doc only LATE saw lost its stamp"
        )
        assert seen[ONLY[EARLY]] == stamps[EARLY], (
            f"{where}: the doc only EARLY saw lost its stamp"
        )
        assert {seen[d] for d in SHARED} == {stamps[LATE]}, (
            f"{where}: a shared doc's last_seen_at is not the later run's stamp"
        )
