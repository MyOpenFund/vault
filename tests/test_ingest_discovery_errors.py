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
    assert len(fed[7]) == ide.MAX_ERROR_CHARS
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


def test_shrink_guard(monkeypatch):  # U11 — pure decision function
    assert ide.should_replace(file_rows=1, held_rows=100, min_retain_fraction=0.5) is False
    assert ide.should_replace(file_rows=60, held_rows=100, min_retain_fraction=0.5) is True
    assert ide.should_replace(file_rows=1, held_rows=100, min_retain_fraction=0.0) is True
    assert ide.should_replace(file_rows=5, held_rows=0, min_retain_fraction=0.5) is True
