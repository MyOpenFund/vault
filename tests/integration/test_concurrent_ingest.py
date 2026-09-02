"""Two ingest.main() runs racing each other against the same database.

Nothing stops a second run from starting while the first is still going —
a slow nightly cron overlapping the next one, or an operator kicking off a
manual re-ingest. Both runs then upsert the same doc_ids over the same rows
with their own run timestamp, and `last_seen_at` is what the soft-delete
sweep trusts to decide which documents have vanished from the share. This
test pins what an overlapping pair must never do to the registry.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from psycopg2.extras import execute_values as real_execute_values

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

LATE = "ingest-late"  # the run holding the NEWER run timestamp
EARLY = "ingest-early"  # the run holding the OLDER one
DOC_IDS = ("d1", "d2", "d3")
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


def _run_both_concurrently(monkeypatch, ingest, stamps):
    """Run two ingest.main() in parallel, EARLY committing its batch last.

    The gate wraps ingest's own execute_values so the interleaving is the
    damaging one rather than a coin flip: both runs first meet at a barrier
    (each has finished its DDL and is inside its own open transaction), then
    LATE upserts and commits while EARLY — already in its transaction and
    blocking on LATE's row locks — applies its older timestamp afterwards.
    """
    barrier = threading.Barrier(2)
    late_upserted = threading.Event()

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
    monkeypatch.setattr(ingest, "execute_values", gated_execute_values)

    failures = {}

    def target():
        try:
            ingest.main()
        except BaseException as exc:  # surfaced in the main thread below
            failures[threading.current_thread().name] = exc

    threads = [threading.Thread(target=target, name=n) for n in (LATE, EARLY)]
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
    """Overlapping runs must leave every document live and freshly stamped.

    Two runs see the same doc_ids and stamp them with different run
    timestamps. Whatever order they commit in, the registry afterwards must
    hold each doc_id exactly once, none of them tombstoned by either run's
    sweep, and `last_seen_at` at the LATEST of the two timestamps: a row
    rewound to an older stamp looks stale to the next run's sweep and gets
    soft-deleted while still very much present on the share.
    """
    import ingest

    write_manifest(tmp_path, "us.jsonl", [make_doc(d) for d in DOC_IDS])
    # Seed the schema and the rows first, so the race is a pure update race
    # and not a fight between two copies of the DDL train.
    run_ingest(monkeypatch, clean_db, tmp_path)

    t0 = datetime.now(timezone.utc)
    for iteration in range(3):
        stamps = {
            EARLY: t0 + timedelta(seconds=2 * iteration + 1),
            LATE: t0 + timedelta(seconds=2 * iteration + 2),
        }
        _run_both_concurrently(monkeypatch, ingest, stamps)

        rows = fetch_all(
            clean_db,
            "SELECT doc_id, deleted_at, last_seen_at FROM documents "
            "ORDER BY doc_id",
        )
        assert [r[0] for r in rows] == sorted(DOC_IDS), (
            f"iteration {iteration}: a doc_id was lost or duplicated"
        )
        assert all(r[1] is None for r in rows), (
            f"iteration {iteration}: a racing sweep tombstoned a live document"
        )
        assert {r[2] for r in rows} == {stamps[LATE]}, (
            f"iteration {iteration}: last_seen_at is not the later run's stamp"
        )
