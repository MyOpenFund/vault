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

from .conftest import fetch_all, lock_is_free, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

# `real_execute_values` above is psycopg2's own, imported here at module import
# time — never whatever a previous iteration left patched onto ingest. The hook
# below must delegate to that one, or the three iterations nest on each other.

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


class _WaitingForTheCorpusLock(logging.Filter):
    """Rendezvous that fires only once EARLY's try for the lock has FAILED.

    Attached to the `ingest` logger, it watches for the one WARNING
    `acquire_lock` emits between `pg_try_advisory_lock` returning False and
    the blocking `pg_advisory_lock` that follows — and only for the corpus
    key, and only from the EARLY thread. Every filter on a logger runs for
    every record, so this returns True and changes nothing about what is
    logged (caplog still sees the warning; the assertion on it stands).

    Watching the log rather than the *entry* into `acquire_lock` is what makes
    the interleaving a fact: an entry proves only that EARLY asked, and EARLY
    could still have won the lock outright had LATE not held it. This record
    exists only on the path where the database refused it.
    """

    def __init__(self, event, key):
        super().__init__()
        self._event = event
        self._needle = f"lock {key!r} — waiting for it"

    def filter(self, record):
        if (
            record.levelno >= logging.WARNING
            and record.threadName == EARLY
            and self._needle in record.getMessage()
        ):
            self._event.set()
        return True


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
      it can only ever be 1). LATE parks there, holding the corpus lock, until
      EARLY is blocked on that same lock.
    * a `logging.Filter` on the `ingest` logger releases that park when — and
      only when — EARLY logs that `pg_try_advisory_lock` came back False.
      Nothing gates EARLY itself: it walks straight at the lock.

    So the interleaving is deterministic in the literal sense, with no
    test-side choreography of the *outcome*: EARLY starts only once LATE holds
    the lock, and LATE resumes only once the database has demonstrably refused
    EARLY the lock and parked it in `pg_advisory_lock`. LATE then finishes and
    unlocks; EARLY runs second. The contention is real — the database, not the
    test, is what makes EARLY wait, and the log line proving it is the same one
    the caller asserts on.

    Delete the corpus lock from main() and the whole thing degrades exactly
    the way it should: EARLY never logs the wait, LATE's park expires on
    ORDER_TIMEOUT, and by then EARLY has been upserting alongside it — peak 2.
    """
    in_flight = 0
    peak = 0
    guard = threading.Lock()
    corpus_lock = ingest.corpus_lock_key(CORPUS)
    late_holds_the_lock = threading.Event()  # LATE has it and is at the upsert
    early_blocked_on_lock = threading.Event()  # the database refused EARLY

    def per_thread_manifests(_root):
        return [str(manifests[threading.current_thread().name])]

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
                # Park BEFORE delegating, for two reasons. It is the only
                # point where LATE demonstrably holds the corpus lock and has
                # not yet written anything — so the peak below counts a real
                # overlap rather than one manufactured after the fact. And
                # LATE's connection must carry no open transaction while it
                # sits here: acquire_lock committed, and psycopg2 opens the
                # next transaction lazily on the first statement, which is
                # the delegation below. Park *after* it and LATE would be
                # holding row locks on `documents` while EARLY's DDL train
                # asks for ACCESS EXCLUSIVE on the same table (`ALTER TABLE
                # documents ... ADD COLUMN IF NOT EXISTS` takes it even when
                # it adds nothing) — EARLY would then be stuck behind LATE
                # while LATE waits for EARLY, for a 15 s ORDER_TIMEOUT stall
                # on every iteration, and the deadlock would look like a
                # slow test rather than the wiring mistake it is.
                late_holds_the_lock.set()
                # No assert on the result: an expired wait is a legitimate
                # outcome that the peak assertion is there to report.
                early_blocked_on_lock.wait(timeout=ORDER_TIMEOUT)
            real_execute_values(cur, sql, rows, **kwargs)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(ingest, "datetime", _PerThreadClock(stamps))
    monkeypatch.setattr(ingest, "find_jsonl_files", per_thread_manifests)
    monkeypatch.setattr(ingest, "execute_values", probed_execute_values)

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
    rendezvous = _WaitingForTheCorpusLock(early_blocked_on_lock, corpus_lock)
    ingest.log.addFilter(rendezvous)
    try:
        threads[LATE].start()
        # Hold EARLY back only until LATE is demonstrably inside the lock.
        # Waiting on the event rather than sleeping a guessed interval is what
        # makes "LATE goes first" a fact instead of a hope; if LATE never gets
        # there the wait expires and EARLY starts anyway, so a broken LATE
        # surfaces as its own failure below rather than as a hang.
        late_holds_the_lock.wait(timeout=ORDER_TIMEOUT)
        threads[EARLY].start()
        # Join every thread before judging any of them: a first thread that
        # wedged must not leave the second one running (holding a connection
        # and the lock) into the next iteration's assertions.
        for t in threads.values():
            t.join(timeout=RUN_TIMEOUT)
    finally:
        ingest.log.removeFilter(rendezvous)
    alive = [t.name for t in threads.values() if t.is_alive()]
    assert not alive, (
        f"{alive} did not finish within {RUN_TIMEOUT}s "
        f"(failures so far: {failures})"
    )
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

    # Both runs returned, so neither may still be holding the key on the
    # success path — a lock leaked here would only surface much later, as the
    # next hourly run blocking forever on a session nobody is using.
    assert lock_is_free(clean_db, ingest.corpus_lock_key(CORPUS)), (
        "the corpus lock is still held after both runs finished"
    )
    assert lock_is_free(clean_db, ingest.DDL_LOCK_KEY), (
        "the DDL lock is still held after both runs finished"
    )
