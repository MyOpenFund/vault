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

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


@pytest.fixture()
def fresh_db(clean_db):
    """clean_db, plus a dropped `cadence` table so each test starts empty."""
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cadence")
    conn.close()
    return clean_db


def test_ingest_main_rolls_back_and_reraises_on_mid_run_db_failure(
    fresh_db, tmp_path, monkeypatch
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
        run_ingest(monkeypatch, fresh_db, tmp_path)

    (count,) = fetch_all(fresh_db, "SELECT COUNT(*) FROM documents")[0]
    assert count == 0
