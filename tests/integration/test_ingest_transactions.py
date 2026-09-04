"""Transactional semantics of a full ingest.main() run.

The service is a cron job pointed at a network share: a torn manifest, a
schema drift or a hostile line can make any run die halfway through. What
the operator must be able to rely on is exactly what these tests pin down —
an aborted run leaves the registry in a state that is still true, and it
never half-applies the deletion sweep or the cadence replace on the strength
of a partial read of the share.
"""

import psycopg2
import pytest

from .conftest import (
    fetch_all,
    make_doc,
    make_entry,
    run_ingest,
    write_cadence,
    write_manifest,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def fresh_db(clean_db):
    """clean_db, plus a dropped `cadence` table so each test starts empty."""
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cadence CASCADE")
    conn.close()
    return clean_db


def test_ingest_main_rolls_back_and_reraises_on_mid_run_db_failure(
    clean_db, tmp_path, monkeypatch
):
    """A DB failure mid-batch must abort the run and leave nothing behind.

    execute_values pages the upsert 500 rows at a time, so with 501 manifest
    lines the first page is already written inside the run's transaction when
    the last line — valid JSON, but an invalid DATE at the DB layer — makes
    the second page raise. The half-written batch must not be visible to
    readers: the run has to roll back and re-raise rather than commit the
    part of the share it happened to get through.
    """
    docs = [make_doc(f"d{i:04d}") for i in range(500)]
    docs.append(make_doc("d0500", date="not-a-date"))
    write_manifest(tmp_path, "us.jsonl", docs)

    with pytest.raises(psycopg2.DataError):
        run_ingest(monkeypatch, clean_db, tmp_path)

    (count,) = fetch_all(clean_db, "SELECT COUNT(*) FROM documents")[0]
    assert count == 0


def test_ingest_main_partial_multi_file_run_leaves_earlier_files_committed(
    fresh_db, tmp_path, monkeypatch
):
    """A failing manifest keeps earlier files, and cancels the whole post-pass.

    Manifests are committed one file at a time on purpose: a bad file must not
    cost the operator the good ones. But everything that depends on having
    seen the WHOLE share — the soft-delete sweep and the cadence full replace
    — must not run on a partial read, or a single unreadable manifest would
    tombstone live documents and overwrite the cadence snapshot with a torn
    one. Here file 1 lands, file 2 dies at the DB layer, and neither the
    sweep nor the cadence replace is allowed to happen.
    """
    # Run 1 — one manifest and one cadence snapshot, both committed.
    write_manifest(tmp_path, "a_first.jsonl", [make_doc("stale1")])
    write_cadence(tmp_path, [make_entry(status="overdue")])
    run_ingest(monkeypatch, fresh_db, tmp_path)

    # Run 2 — stale1 has vanished from the manifests (so the sweep, whose
    # fraction guard run_ingest disables, would tombstone it), a second
    # manifest carries a DB-invalid date, and a newer cadence snapshot waits.
    write_manifest(tmp_path, "a_first.jsonl", [make_doc("kept1")])
    write_manifest(
        tmp_path, "b_second.jsonl", [make_doc("lost1", date="not-a-date")]
    )
    write_cadence(tmp_path, [make_entry(status="on-track")])

    with pytest.raises(psycopg2.DataError):
        run_ingest(monkeypatch, fresh_db, tmp_path)

    rows = dict(fetch_all(fresh_db, "SELECT doc_id, deleted_at FROM documents"))
    assert set(rows) == {"stale1", "kept1"}  # file 2's row never landed
    assert rows["kept1"] is None  # file 1's commit survived the abort
    assert rows["stale1"] is None  # deleted_at still NULL: the sweep never ran

    cadence = fetch_all(fresh_db, "SELECT source_code, status FROM cadence")
    assert cadence == [("us", "overdue")]  # the cadence replace never ran
