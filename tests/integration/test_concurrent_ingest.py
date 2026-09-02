"""Two ingest.main() runs racing each other against the same database.

Nothing stops a second run from starting while the first is still going —
a slow nightly cron overlapping the next one, or an operator kicking off a
manual re-ingest. Both runs then upsert over the same rows with their own
run timestamp, and `last_seen_at` is what the soft-delete sweep trusts to
decide which documents have vanished from the share. This test pins what an
overlapping pair must never do to the registry.

The two manifests overlap only partially — each run also carries one doc_id
the other has never heard of — so "no row was lost" means something beyond
"the upsert is idempotent": neither run may erase what the other contributed.
"""

import threading
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
GATE_TIMEOUT = 30  # seconds; generous, only ever hit when a run wedges


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
    """Run two ingest.main() in parallel, EARLY committing its batch last.

    Three test-side hooks, none of them a change to production behaviour:
    each thread reads its own manifest (DATA_DIR is process-global, so the
    scan is what has to be per-thread), each gets a fixed run timestamp, and
    the gate around ingest's own execute_values makes the interleaving the
    damaging one rather than a coin flip. Both runs first meet at a barrier —
    each has finished its DDL and is inside its own open transaction — then
    LATE upserts and commits while EARLY, already in its transaction and
    blocking on LATE's row locks, applies its older timestamp afterwards.
    """
    barrier = threading.Barrier(2)
    late_upserted = threading.Event()

    def per_thread_manifests(_root):
        return [str(manifests[threading.current_thread().name])]

    # psycopg2's own execute_values, never whatever a previous iteration left
    # patched onto the module — gates must not nest across iterations.
    def gated_execute_values(cur, sql, rows, **kwargs):
        name = threading.current_thread().name
        barrier.wait(timeout=GATE_TIMEOUT)
        if name == EARLY:
            assert late_upserted.wait(timeout=GATE_TIMEOUT), "LATE never upserted"
        real_execute_values(cur, sql, rows, **kwargs)
        if name == LATE:
            late_upserted.set()

    monkeypatch.setattr(ingest, "datetime", _PerThreadClock(stamps))
    monkeypatch.setattr(ingest, "find_jsonl_files", per_thread_manifests)
    monkeypatch.setattr(ingest, "execute_values", gated_execute_values)

    failures = {}

    def target():
        try:
            ingest.main()
        except BaseException as exc:  # surfaced in the main thread below
            failures[threading.current_thread().name] = exc

    threads = [
        threading.Thread(target=target, name=n, daemon=True) for n in (LATE, EARLY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=GATE_TIMEOUT * 2)
        assert not t.is_alive(), f"{t.name} did not finish"
    assert not failures, f"a racing run raised: {failures}"


@pytest.mark.xfail(
    strict=True,
    reason="MyOpenFund/vault#3: the upsert overwrites last_seen_at "
    "unconditionally, so the run that commits last wins even when its run "
    "timestamp is older than the one already stored",
)
def test_concurrent_ingest_runs_do_not_lose_or_duplicate_rows(
    clean_db, tmp_path, monkeypatch
):
    """Overlapping runs must keep every document, once, freshly stamped.

    Whatever order the two runs commit in, the registry afterwards must hold
    each doc_id exactly once — including the one only the other run saw —
    none of them tombstoned, and every shared document stamped with the
    LATEST of the two run timestamps. A row rewound to an older stamp looks
    stale to the next run's sweep and gets soft-deleted while still very much
    present on the share.

    SWEEP_MAX_DELETE_FRACTION is left at its production default here: with
    partially overlapping manifests each run sees a quarter of the corpus as
    missing, so the torn-share guard blocks both sweeps and the only thing
    left to fail is the timestamp itself.
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
        _run_both_concurrently(monkeypatch, ingest, stamps, manifests)

        rows = fetch_all(
            clean_db,
            "SELECT doc_id, deleted_at, last_seen_at FROM documents "
            "ORDER BY doc_id",
        )
        where = f"iteration {iteration}"
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
