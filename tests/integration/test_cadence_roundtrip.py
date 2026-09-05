import psycopg2
import pytest

from .conftest import fetch_all, make_entry, write_cadence

pytestmark = pytest.mark.integration


def run_cadence(pg_url, data_dir, corpus="central-bank"):
    from datetime import datetime, timezone

    import ingest_cadence

    conn = psycopg2.connect(pg_url)
    try:
        return ingest_cadence.run(
            conn, str(data_dir), corpus, datetime.now(timezone.utc)
        )
    finally:
        conn.close()


@pytest.fixture()
def clean_cadence(pg_url):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cadence CASCADE")
    conn.close()
    return pg_url


def test_snapshot_fully_replaces_previous_rows(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry(), make_entry(bank_code="fr")])
    assert run_cadence(clean_cadence, tmp_path) == 2

    # Next snapshot drops fr and changes us's status.
    write_cadence(tmp_path, [make_entry(status="on-track")])
    assert run_cadence(clean_cadence, tmp_path) == 1

    rows = fetch_all(
        clean_cadence, "SELECT source_code, status FROM cadence"
    )
    assert rows == [("us", "on-track")]


def test_replace_is_scoped_to_the_service_corpus(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path, corpus="central-bank")

    other_dir = tmp_path / "company"
    write_cadence(other_dir, [make_entry(bank_code="edgar", doc_type="10-K")])
    run_cadence(clean_cadence, other_dir, corpus="company")

    # Re-running central-bank must not touch the company rows.
    write_cadence(tmp_path, [make_entry(status="soon")])
    run_cadence(clean_cadence, tmp_path, corpus="central-bank")

    rows = sorted(fetch_all(clean_cadence, "SELECT corpus, source_code FROM cadence"))
    assert rows == [("central-bank", "us"), ("company", "edgar")]


def test_missing_file_is_a_noop(clean_cadence, tmp_path):
    assert run_cadence(clean_cadence, tmp_path) == 0


def test_empty_snapshot_leaves_table_untouched(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path)

    write_cadence(tmp_path, [])  # torn/empty regeneration
    assert run_cadence(clean_cadence, tmp_path) == 0

    rows = fetch_all(clean_cadence, "SELECT source_code FROM cadence")
    assert rows == [("us",)]


def test_failed_replace_rolls_back_to_previous_snapshot(
    clean_cadence, tmp_path
):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path)

    # A row with a malformed date makes the INSERT raise AFTER the DELETE
    # already ran — the rollback must restore the previous snapshot.
    write_cadence(
        tmp_path,
        [make_entry(bank_code="fr", last="not-a-date")],
    )
    with pytest.raises(psycopg2.DataError):
        run_cadence(clean_cadence, tmp_path)

    rows = fetch_all(clean_cadence, "SELECT source_code FROM cadence")
    assert rows == [("us",)]


def test_singleton_series_lands_with_null_optional_columns(
    clean_cadence, tmp_path
):
    # A series with a single distinct date: the producer omits
    # interval_days/next_expected/days_until/status entirely rather than
    # emitting explicit JSON nulls. A line missing these optional fields is
    # legal and must land with NULLs in those columns.
    entry = make_entry()
    for key in ("interval_days", "next_expected", "days_until", "status"):
        del entry[key]
    write_cadence(tmp_path, [entry])

    assert run_cadence(clean_cadence, tmp_path) == 1

    rows = fetch_all(
        clean_cadence,
        "SELECT source_code, doc_type, interval_days, next_expected, "
        "days_until, status FROM cadence",
    )
    assert rows == [("us", "C1", None, None, None, None)]


def test_torn_snapshot_with_duplicate_series_rolls_back_previous_snapshot(
    clean_cadence, tmp_path
):
    # Torn append-instead-of-replace corruption: the same (bank_code,
    # doc_type) appears twice in one snapshot. The INSERT must violate the
    # (corpus, source_code, doc_type) primary key, the whole run must raise,
    # and the previous snapshot must survive untouched.
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path)

    write_cadence(
        tmp_path,
        [make_entry(status="on-track"), make_entry(status="overdue")],
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        run_cadence(clean_cadence, tmp_path)

    rows = fetch_all(clean_cadence, "SELECT source_code, status FROM cadence")
    assert rows == [("us", "overdue")]


def test_cadence_path_env_override(clean_cadence, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    write_cadence(elsewhere, [make_entry()])
    monkeypatch.setenv("CADENCE_PATH", str(elsewhere / "cadence.jsonl"))
    try:
        assert run_cadence(clean_cadence, tmp_path / "empty-dir") == 1
    finally:
        monkeypatch.delenv("CADENCE_PATH")
