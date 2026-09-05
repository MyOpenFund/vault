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

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from psycopg2.extras import execute_values as real_execute_values

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

LATE = "ingest-late"  # the run holding the NEWER run timestamp
EARLY = "ingest-early"  # the run holding the OLDER one
SHARED = ("d_shared1", "d_shared2")
ONLY = {LATE: "d_only_late", EARLY: "d_only_early"}
ALL_DOC_IDS = sorted(SHARED + tuple(ONLY.values()))
RUN_TIMEOUT = 60  # seconds; generous, only ever hit when a run wedges
HEADSTART = 0.2  # seconds; enough for LATE to be inside the corpus lock


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


def _run_both_concurrently(monkeypatch, ingest, stamps, manifests):
    """Start both ingest.main() runs and let the corpus lock order them.

    Two test-side hooks, neither a change to production behaviour: each
    thread reads its own manifest (DATA_DIR is process-global, so the scan is
    what has to be per-thread), and each gets a fixed run timestamp. Nothing
    here choreographs the interleaving — that is the point. LATE is started
    first and EARLY a moment later, and whether EARLY then waits behind
    LATE's lock or wins the race to it is left to the database.

    A third hook only *observes*: `execute_values` is wrapped in a counter of
    in-flight upserts. Its peak over the whole pair is returned; under the
    per-corpus lock it can only ever be 1.
    """
    in_flight = 0
    peak = 0
    guard = threading.Lock()

    def per_thread_manifests(_root):
        return [str(manifests[threading.current_thread().name])]

    # psycopg2's own execute_values, never whatever a previous iteration left
    # patched onto the module — probes must not nest across iterations.
    def probed_execute_values(cur, sql, rows, **kwargs):
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
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

    threads = [
        threading.Thread(target=target, name=n, daemon=True) for n in (LATE, EARLY)
    ]
    threads[0].start()
    time.sleep(HEADSTART)
    threads[1].start()
    for t in threads:
        t.join(timeout=RUN_TIMEOUT)
        assert not t.is_alive(), f"{t.name} did not finish within {RUN_TIMEOUT}s"
    assert not failures, f"a racing run raised: {failures}"
    return peak


def test_concurrent_ingest_runs_do_not_lose_or_duplicate_rows(
    clean_db, tmp_path, monkeypatch
):
    """Overlapping runs must serialize, and keep every document, once, fresh.

    The per-corpus lock is the first guarantee: no two runs of the same
    corpus may be upserting at the same moment, so the in-flight probe's peak
    must be 1. The registry afterwards must hold each doc_id exactly once —
    including the one only the other run saw — none of them tombstoned, and
    every shared document stamped with the LATEST of the two run timestamps.
    A row rewound to an older stamp looks stale to the next run's sweep and
    gets soft-deleted while still very much present on the share.

    The shared stamp lands on LATE's value whichever order the lock hands the
    two runs, and for two different reasons: if EARLY goes second, its older
    stamp is discarded by the upsert's GREATEST; if LATE goes second, it
    simply overwrites EARLY's with its newer one. Only the second of those
    would survive dropping GREATEST — which is why the guard stays in the
    upsert even now that the lock exists.

    SWEEP_MAX_DELETE_FRACTION is left at its production default here. With
    partially overlapping manifests the run that goes second sees the other's
    exclusive doc as missing — when LATE is second, `d_only_early` still
    carries EARLY's older stamp and so is a sweep candidate, 1 of 4 live rows
    = 25% — well above the 5% default, so the torn-share guard blocks the
    sweep and the only thing left to fail is the timestamp itself.
    """
    import ingest

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
    run_ingest(monkeypatch, clean_db, tmp_path / "seed")
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "0.05")

    t0 = datetime.now(timezone.utc)
    for iteration in range(3):
        stamps = {
            EARLY: t0 + timedelta(seconds=2 * iteration + 1),
            LATE: t0 + timedelta(seconds=2 * iteration + 2),
        }
        peak = _run_both_concurrently(monkeypatch, ingest, stamps, manifests)

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
