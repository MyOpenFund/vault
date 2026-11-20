import json
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from .conftest import fetch_all, make_entry, run_ingest, write_cadence

pytestmark = pytest.mark.integration
NOW = datetime.now(timezone.utc)


def _exec(pg_url, sql, params=None):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def _run_row(run_id, days_ago, outcome, sources, corpus="central-bank"):
    finished = NOW - timedelta(days=days_ago)
    return (run_id, "central-bank-corpus", "discover", finished - timedelta(minutes=5), finished,
            outcome, {"ok": 0, "degraded": 3, "failed": 1}[outcome], json.dumps(sources), corpus)


def _plant_runs(pg_url, rows):
    for r in rows:
        _exec(pg_url,
              "INSERT INTO runs (run_id, tool, command, started_at, finished_at, outcome, exit_code, sources, corpus) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", r)


def _src(code, seen, new, failed=0, fetch=0, truncated=False):
    return {"source_code": code, "docs_seen": seen, "docs_new": new, "docs_failed": failed,
            "fetch_errors": fetch, "truncated": truncated, "error_samples": []}


def _health(pg_url, where="TRUE"):
    cols = ("source_code, doc_type, source_runs_7d, source_degraded_runs_7d, source_truncated_runs_7d, "
            "source_zero_yield_runs_7d, source_docs_new_7d, source_docs_seen_7d, source_fetch_errors_7d, "
            "source_runs_90d, source_docs_new_median_per_run_90d, last_run_outcome, last_run_truncated, days_late")
    return fetch_all(pg_url, f"SELECT {cols} FROM source_health WHERE {where} ORDER BY 1, 2")


def _setup(monkeypatch, pg_url, tmp_path, entries):
    write_cadence(tmp_path, entries)
    run_ingest(monkeypatch, pg_url, tmp_path)
    # clean_db drops documents/rag_ingestions but not runs, and the postgres
    # container is module-scoped: without this, runs planted with fixed ids by
    # an earlier test in this module survive (and collide on the primary key).
    # cadence needs no such help -- ingest_cadence replaces the corpus's rows.
    _exec(pg_url, "DELETE FROM runs")


def test_source_health_observed_columns(clean_db, tmp_path, monkeypatch):
    # I9 — a known degradation pattern for ecb; fed healthy but sharing runs.
    _setup(monkeypatch, clean_db, tmp_path,
           [make_entry(bank_code="ecb", doc_type="A1", days_until=-3, status="overdue"),
            make_entry(bank_code="fed", doc_type="C1", days_until=5, status="ok")])
    rows = []
    for i in range(7):  # 7 runs in the last 7 days, days_ago 0..6
        ecb = _src("ecb", 0 if i in (1, 2) else 10, [3, 0, 0, 5, 3, 1, 3][i],
                   fetch=1 if i == 1 else 0, truncated=(i in (1, 2)))
        fed = _src("fed", 4, 1)
        rows.append(_run_row(f"r{i}", i, "degraded" if i in (1, 2) else "ok", [ecb, fed]))
    rows.append(_run_row("old", 30, "ok", [_src("ecb", 10, 9), _src("fed", 4, 1)]))   # in 90d only
    rows.append(_run_row("ancient", 200, "ok", [_src("ecb", 10, 99), _src("fed", 4, 1)]))  # outside both
    _plant_runs(clean_db, rows)

    ecb, fed = _health(clean_db)
    assert ecb[:2] == ("ecb", "A1")
    assert ecb[2:10] == (7, 2, 2, 2, 15, 50, 1, 8)
    assert ecb[10] == 3.0          # median of [3,0,0,5,3,1,3,9] over 90d
    assert ecb[11] == "ok" and ecb[12] is False and ecb[13] == 3
    assert fed[2:5] == (7, 2, 0)   # run-grain outcome leaks: 2 degraded runs, 0 truncated of its own
    assert fed[13] == 0


def test_runs_without_corpus_leave_observed_columns_zero(clean_db, tmp_path, monkeypatch):
    # I10 — pinned on purpose: with runs.corpus NULL the observed half is
    # silently empty. If this test fails one day, corpus stamping broke.
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    _plant_runs(clean_db, [_run_row("r0", 0, "ok", [_src("ecb", 10, 3)], corpus=None)])
    (row,) = _health(clean_db)
    assert row[2:10] == (0, 0, 0, 0, 0, 0, 0, 0) and row[10] is None and row[11] is None


def test_source_columns_repeat_across_doc_types(clean_db, tmp_path, monkeypatch):
    # I11 — runs.sources has no doc_type: source_* values are SOURCE grain.
    _setup(monkeypatch, clean_db, tmp_path,
           [make_entry(bank_code="ecb", doc_type="A1"), make_entry(bank_code="ecb", doc_type="D3")])
    _plant_runs(clean_db, [_run_row("r0", 0, "ok", [_src("ecb", 10, 3)])])
    a1, d3 = _health(clean_db)
    assert a1[2:11] == d3[2:11] == (1, 0, 0, 0, 3, 10, 0, 1, 3.0)


def test_sources_without_cadence_is_loud(clean_db, tmp_path, monkeypatch):
    # I12 — a source that runs but has no cadence row is invisible to
    # source_health; the companion view names it.
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    _plant_runs(clean_db, [_run_row("r0", 0, "ok", [_src("ecb", 10, 3), _src("gb", 2, 1)])])
    assert fetch_all(clean_db, "SELECT count(*) FROM source_health WHERE source_code = 'gb'")[0][0] == 0
    rows = fetch_all(clean_db, "SELECT corpus, source_code FROM sources_without_cadence")
    assert rows == [("central-bank", "gb")]


def test_last_run_tie_is_broken_by_run_id(clean_db, tmp_path, monkeypatch):
    # Documented contract #4: ties on finished_at are broken by run_id, so the
    # last_run_* columns are deterministic rather than plan-dependent.
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    finished = NOW - timedelta(hours=1)
    for run_id in ("r-a", "r-b"):
        _exec(clean_db,
              "INSERT INTO runs (run_id, tool, started_at, finished_at, outcome, exit_code, sources, corpus) "
              "VALUES (%s, 'central-bank-corpus', %s, %s, 'ok', 0, %s, 'central-bank')",
              (run_id, finished, finished, json.dumps([_src("ecb", 10, 3)])))
    rows = fetch_all(clean_db, "SELECT last_run_id FROM source_health")
    assert rows == [("r-b",)]


def test_days_late_is_null_when_days_until_is_unknown(clean_db, tmp_path, monkeypatch):
    # A cadence entry with no next_expected has no days_until, so "how late is
    # it" is unknown -- not 0. Reporting 0 would make an unmeasurable series
    # indistinguishable from an on-time one.
    _setup(monkeypatch, clean_db, tmp_path,
           [make_entry(bank_code="ecb", doc_type="A1", next_expected=None, days_until=None,
                       status="unknown"),
            make_entry(bank_code="fed", doc_type="C1", days_until=5, status="ok")])
    rows = fetch_all(clean_db, "SELECT source_code, days_late FROM source_health ORDER BY 1")
    assert rows == [("ecb", None), ("fed", 0)]


def test_source_health_counts_open_discovery_errors(clean_db, tmp_path, monkeypatch):
    _setup(monkeypatch, clean_db, tmp_path,
           [make_entry(bank_code="ecb", doc_type="A1"), make_entry(bank_code="fed", doc_type="C1")])
    _exec(clean_db,
          "INSERT INTO discovery_errors (fingerprint, corpus, source_code, context, url, error, "
          "first_seen_at, last_seen_at, occurrences) VALUES "
          "('f1', 'central-bank', 'ecb', 'listing', 'https://x/a', 'ReadTimeout: x', now(), now(), 4), "
          "('f2', 'central-bank', 'ecb', 'listing', 'https://x/b', 'HTTPError: 503', now(), now(), 1), "
          "('f3', 'central-bank', 'fed', 'listing', 'https://y', 'x', now(), now(), 1)")
    _exec(clean_db, "UPDATE discovery_errors SET resolved_at = now() WHERE fingerprint = 'f3'")
    rows = fetch_all(clean_db, "SELECT source_code, source_open_discovery_errors, source_open_discovery_attempts "
                               "FROM source_health ORDER BY 1")
    assert rows == [("ecb", 2, 5), ("fed", 0, 0)]


# --- bad_content (central-bank-corpus, 2026-11-19): a sub-count of docs_failed --------------------
# NULL means "not reported" (a producer, or a run, that does not check content), never 0.

def _bad(code, bad, failed=0):
    src = _src(code, 10, 1, failed=failed)
    if bad is not ...:
        src["bad_content"] = bad
    return src


def test_runs_sources_bad_content_is_cast_defensively(clean_db, tmp_path, monkeypatch):
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    _plant_runs(clean_db, [_run_row("r0", 0, "ok", [_bad("ecb", 2, failed=3), _bad("fed", ...),
                                                   _bad("fr", "garbage"), _bad("gb", None),
                                                   _bad("it", 0), _bad("jp", 10000000000),
                                                   _bad("nl", 999999999), _bad("se", 1.5)])])
    rows = fetch_all(clean_db, "SELECT source_code, bad_content FROM runs_sources ORDER BY 1")
    # Past the int range: NULL, never an "integer out of range" for every reader of the view.
    assert rows == [("ecb", 2), ("fed", None), ("fr", None), ("gb", None), ("it", 0),
                    ("jp", None), ("nl", 999999999), ("se", None)]
    assert fetch_all(clean_db, "SELECT count(*) FROM source_health")[0][0] == 1


def test_source_health_bad_content_sums_the_window_and_keeps_null_apart(clean_db, tmp_path, monkeypatch):
    _setup(monkeypatch, clean_db, tmp_path,
           [make_entry(bank_code=b, doc_type="A1") for b in ("ecb", "fed", "fr", "gb", "it")])
    _plant_runs(clean_db, [
        # ecb: 2 + 1 in the window (one run there did not report), 50 outside it.
        _run_row("r-old", 30, "ok", [_bad("ecb", 50), _bad("fr", 4)]),
        _run_row("r2", 2, "ok", [_bad("ecb", 2, failed=2), _bad("fed", ...), _bad("gb", 0)]),
        _run_row("r1", 1, "ok", [_bad("ecb", ...), _bad("fed", ...), _bad("gb", 0)]),
        _run_row("r0", 0, "ok", [_bad("ecb", 1, failed=1), _bad("fed", ...), _bad("it", 5)]),
    ])
    rows = fetch_all(clean_db, "SELECT source_code, source_bad_content_7d, last_run_bad_content "
                               "FROM source_health ORDER BY 1")
    assert rows == [
        ("ecb", 3, 1),        # summed over the 7-day window, last run's own value
        ("fed", None, None),  # runs, but never reported: unknown, not 0
        ("fr", None, 4),      # reported only outside the window; its last run (30 d ago) said 4
        ("gb", 0, 0),         # reported 0, which is not the same as NULL (not reported)
        ("it", 5, 5),
    ]


def test_source_health_last_run_bad_content_follows_last_run(clean_db, tmp_path, monkeypatch):
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    _plant_runs(clean_db, [_run_row("r1", 1, "ok", [_bad("ecb", 7)]),
                           _run_row("r0", 0, "ok", [_bad("ecb", 0)])])
    rows = fetch_all(clean_db, "SELECT last_run_id, last_run_bad_content, source_bad_content_7d "
                               "FROM source_health")
    assert rows == [("r0", 0, 7)]


def test_bad_content_columns_come_last(clean_db, tmp_path, monkeypatch):
    # Additive: existing positional readers of the views keep their columns in place.
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    cols = [r[0] for r in fetch_all(clean_db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'source_health' "
            "ORDER BY ordinal_position")]
    assert cols[-2:] == ["source_bad_content_7d", "last_run_bad_content"]
    assert cols[:-2][-1] == "source_last_discovery_error_at"
    cols = [r[0] for r in fetch_all(clean_db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs_sources' "
            "ORDER BY ordinal_position")]
    assert cols[-2:] == ["error_samples", "bad_content"]


def test_out_of_range_counters_are_null_not_an_error(clean_db, tmp_path, monkeypatch):
    # One malformed report must not make runs_sources (and every view and reader on it) raise
    # "integer out of range": every counter is bounded to 9 digits, like bad_content. A
    # negative counter is malformed too and reads as NULL: a negative bad_content would
    # otherwise offset a positive one and hide a bank under Q6's `HAVING sum > 0`.
    _setup(monkeypatch, clean_db, tmp_path, [make_entry(bank_code="ecb", doc_type="A1")])
    ten_digits = 2147483647                 # int max, yet past the 9-digit bound
    _plant_runs(clean_db, [_run_row("r0", 0, "ok", [
        {"source_code": "ecb", "docs_seen": ten_digits, "docs_new": ten_digits,
         "docs_failed": ten_digits, "fetch_errors": ten_digits, "bad_content": ten_digits},
        {"source_code": "fed", "docs_seen": 999999999, "docs_new": 999999999,
         "docs_failed": 999999999, "fetch_errors": 999999999, "bad_content": 999999999},
        {"source_code": "neg", "docs_seen": -5, "docs_new": -5, "docs_failed": -5,
         "fetch_errors": -5, "bad_content": -5}])])
    rows = fetch_all(clean_db, "SELECT source_code, docs_seen, docs_new, docs_failed, fetch_errors, "
                               "bad_content FROM runs_sources ORDER BY 1")
    assert rows == [("ecb", None, None, None, None, None),
                    ("fed", 999999999, 999999999, 999999999, 999999999, 999999999),
                    ("neg", None, None, None, None, None)]
    assert fetch_all(clean_db, "SELECT source_runs_7d FROM source_health")[0][0] == 1
