import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ingest_discovery_errors as ide

RUN_TS = datetime(2026, 9, 4, 3, 0, 0, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "discovery_errors_real.jsonl"


@pytest.mark.parametrize("raw, expected", [
    ("ReadTimeout: pool timed out", ("ReadTimeout", "ReadTimeout: pool timed out")),
    ("ValueError: a: b: c", ("ValueError", "ValueError: a: b: c")),
    ("requests.exceptions.ConnectionError: boom", ("requests.exceptions.ConnectionError", "requests.exceptions.ConnectionError: boom")),
    ("weird message with no colon", (None, "weird message with no colon")),
    ("", (None, "")),
    (None, (None, "")),
])
def test_split_error_never_guesses(raw, expected):  # U6
    assert ide.split_error(raw) == expected


def test_fingerprint_is_stable_and_sensitive():  # U7
    base = ("central-bank", "ecb", "listing", "https://x/a", "ReadTimeout")
    assert ide.fingerprint(*base) == ide.fingerprint(*base)
    assert len(ide.fingerprint(*base)) == 64
    for i in range(5):
        changed = list(base); changed[i] = "other"
        assert ide.fingerprint(*changed) != ide.fingerprint(*base)
    # same class, different message -> same fingerprint (message is not a field)
    assert ide.fingerprint(*base) == ide.fingerprint("central-bank", "ecb", "listing", "https://x/a", "ReadTimeout")


def test_aggregate_counts_occurrences_from_the_whole_file():  # U8
    rows = ide.load_error_rows(FIX, RUN_TS, "central-bank")
    by_url = {r[5]: r for r in rows}
    ecb = by_url["https://www.ecb.europa.eu/press/pr/html/index.en.html?page=3"]
    assert ecb[14] == 3                       # occurrences assigned, not incremented
    assert ecb[6] == "ReadTimeout"
    assert ecb[7].endswith("(read timeout=60)")  # latest message wins
    assert ecb[11] <= ecb[12]                 # first_seen_at <= last_seen_at
    assert ecb[13] is True                    # seen_at_is_ingestion_time
    assert ecb[2] == "ecb" and ecb[1] == "central-bank"
    assert len(rows) == 3
    jp = by_url["https://www.boj.or.jp/en/research/wps_rev/index.htm"]
    assert jp[6] is None                      # unparseable class -> None, never a guess


def test_torn_lines_are_skipped_and_good_lines_aggregate(tmp_path):  # U9
    p = tmp_path / "discovery_errors.jsonl"
    long = "X" * 50_000
    p.write_bytes(
        b'{"bank": "ecb", "context": "c", "url": "https://x", "error": "A: ok"}\n'
        b'{"bank": "ecb", "context": "c", "url": "https://x", "err\n'          # truncated JSON
        b'null\n'                                                              # bare null
        b'[1, 2]\n'                                                            # array
        b'{"bank": "ecb", "context": "c", "error": "A: no url"}\n'             # no url
        b'\n'                                                                  # empty
        + json.dumps({"bank": "fed", "context": "c", "url": "https://y", "error": "B: " + long}).encode() + b"\n"
        + b'\xff\xfe{"bank": "x"}\n'                                           # invalid UTF-8
    )
    counters = {}
    rows = ide.load_error_rows(p, RUN_TS, "central-bank", counters)
    assert {r[5] for r in rows} == {"https://x", "https://y"}
    fed = [r for r in rows if r[5] == "https://y"][0]
    assert len(fed[7]) == ide.DEFAULT_MAX_ERROR_CHARS
    assert counters.get("invalid_lines", 0) == 5


def test_producer_ts_flips_the_ingestion_time_flag():  # U10
    with_ts = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": "A: b",
                    "ts": "2026-09-01T02:00:00+00:00", "run_id": "r1", "http_status": 503}),
        "f", 1, RUN_TS, "central-bank")
    assert with_ts["seen_at_is_ingestion_time"] is False
    assert with_ts["event_ts"].isoformat() == "2026-09-01T02:00:00+00:00"
    assert with_ts["run_id"] == "r1" and with_ts["http_status"] == 503
    without = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": "A: b"}), "f", 1, RUN_TS, "central-bank")
    assert without["seen_at_is_ingestion_time"] is True and without["event_ts"] == RUN_TS


def test_corpus_contradiction_is_rejected_and_counted():
    counters = {}
    rec = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": "A: b", "corpus": "company"}),
        "f", 1, RUN_TS, "central-bank", counters)
    assert rec is None and counters["corpus_conflict"] == 1


# --- Type-corrupt (JSON-valid, shape-invalid) lines -------------------------
#
# The trail file is append-only: a line whose `bank`/`source_code`/`context`/
# `doc_type`/`error_class` is a dict or a list would reach execute_values as an
# unadaptable Python object, blow up the batch, make run() re-raise and thereby
# fail the service EVERY night forever. Reject it here instead.


@pytest.mark.parametrize("field, value", [
    ("bank", {"code": "ecb"}),
    ("source_code", ["ecb"]),
    ("context", ["listing", "index"]),
    ("doc_type", {"t": "A1"}),
    ("error_class", ["ReadTimeout"]),
])
def test_non_string_identity_field_rejects_the_line(field, value):
    counters = {}
    obj = {"bank": "ecb", "context": "c", "url": "https://x", "error": "A: b"}
    obj[field] = value
    rec = ide.parse_error_line(json.dumps(obj), "f", 1, RUN_TS, "central-bank", counters)
    assert rec is None
    assert counters["invalid_lines"] == 1


def test_null_identity_fields_are_still_accepted():
    rec = ide.parse_error_line(
        json.dumps({"bank": None, "context": None, "doc_type": None,
                    "url": "https://x", "error": "A: b"}),
        "f", 1, RUN_TS, "central-bank")
    assert rec is not None and rec["source_code"] is None and rec["context"] is None


@pytest.mark.parametrize("bad_ts", ["yesterday", 12345, "", {"at": "now"}])
def test_malformed_ts_is_counted_and_falls_back_to_ingestion_time(bad_ts):
    counters = {}
    rec = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x",
                    "error": "A: b", "ts": bad_ts}),
        "f", 1, RUN_TS, "central-bank", counters)
    assert rec is not None                        # the line is kept…
    assert rec["event_ts"] == RUN_TS              # …with the honest fallback
    assert rec["seen_at_is_ingestion_time"] is True
    assert counters["bad_ts"] == 1


def test_absent_ts_is_not_counted_as_malformed():
    counters = {}
    ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": "A: b"}),
        "f", 1, RUN_TS, "central-bank", counters)
    assert "bad_ts" not in counters


@pytest.mark.parametrize("value", [
    {"type": "ReadTimeout", "detail": "pool"},
    ["ReadTimeout", "pool"],
    503,
])
def test_non_string_error_is_serialised_not_dropped(value):
    rec = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": value}),
        "f", 1, RUN_TS, "central-bank")
    assert rec["error"] == json.dumps(value, ensure_ascii=False)
    assert rec["error_class"] is None             # never guessed from a non-string


def test_non_string_error_keeps_an_explicit_error_class():
    rec = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x",
                    "error": {"detail": "pool"}, "error_class": "ReadTimeout"}),
        "f", 1, RUN_TS, "central-bank")
    assert rec["error_class"] == "ReadTimeout"


# --- The "latest record" ordering rule (cutover) ----------------------------


def _line(**over):
    obj = {"bank": "ecb", "context": "listing", "url": "https://x", "error": "A: b"}
    obj.update(over)
    return json.dumps(obj) + "\n"


def test_ts_carrying_line_beats_a_legacy_ts_less_one(tmp_path):
    # Cutover: a ts-less line's event_ts is ingestion time (now), which would
    # otherwise always beat a real producer timestamp from the past.
    p = tmp_path / "discovery_errors.jsonl"
    p.write_text(_line(error="A: legacy") + _line(error="A: with-ts", ts="2026-09-01T02:00:00+00:00"))
    (row,) = ide.load_error_rows(p, RUN_TS, "central-bank")
    assert row[7] == "A: with-ts"
    assert row[13] is False                       # seen_at_is_ingestion_time
    assert row[14] == 2                           # both lines still counted


def test_ts_less_lines_are_broken_by_file_order(tmp_path):
    p = tmp_path / "discovery_errors.jsonl"
    p.write_text(_line(error="A: first") + _line(error="A: second"))
    (row,) = ide.load_error_rows(p, RUN_TS, "central-bank")
    assert row[7] == "A: second"
    assert row[13] is True


# --- Tunables are resolved at call time, never at import time ---------------


def test_max_error_chars_is_read_from_the_environment_at_call_time(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ERROR_MAX_CHARS", "10")
    rec = ide.parse_error_line(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://x", "error": "A: " + "y" * 500}),
        "f", 1, RUN_TS, "central-bank")
    assert len(rec["error"]) == 10


def test_max_error_chars_has_a_floor_of_one(monkeypatch):
    # 0 (or a negative) would blank EVERY message — a truncation tunable must
    # never be able to destroy the column it truncates.
    monkeypatch.setenv("DISCOVERY_ERROR_MAX_CHARS", "0")
    assert ide._max_error_chars() == 1
    monkeypatch.setenv("DISCOVERY_ERROR_MAX_CHARS", "-5")
    assert ide._max_error_chars() == 1


def test_max_error_chars_rejects_a_non_numeric_value_by_name(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ERROR_MAX_CHARS", "lots")
    with pytest.raises(ValueError, match=r"DISCOVERY_ERROR_MAX_CHARS must be an integer, got 'lots'"):
        ide._max_error_chars()


def test_min_retain_fraction_is_read_from_the_environment_at_call_time(monkeypatch):
    assert ide._min_retain_fraction() == ide.DEFAULT_MIN_RETAIN_FRACTION
    monkeypatch.setenv("DISCOVERY_ERRORS_MIN_RETAIN_FRACTION", "0.0")
    assert ide._min_retain_fraction() == 0.0


def test_shrink_guard():  # U11 — pure decision function
    assert ide.should_replace(file_rows=1, held_rows=100, min_retain_fraction=0.5) is False
    assert ide.should_replace(file_rows=60, held_rows=100, min_retain_fraction=0.5) is True
    assert ide.should_replace(file_rows=1, held_rows=100, min_retain_fraction=0.0) is True
    assert ide.should_replace(file_rows=5, held_rows=0, min_retain_fraction=0.5) is True
