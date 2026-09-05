import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration
FIX = Path(__file__).parent.parent / "fixtures" / "discovery_errors_real.jsonl"
ECB_URL = "https://www.ecb.europa.eu/press/pr/html/index.en.html?page=3"


def _counts(pg_url):
    return fetch_all(pg_url, "SELECT url, occurrences, first_seen_at, last_seen_at FROM discovery_errors ORDER BY url")


def _stamps(pg_url):
    return {u: (f, l, flag) for u, f, l, flag in fetch_all(
        pg_url,
        "SELECT url, first_seen_at, last_seen_at, seen_at_is_ingestion_time "
        "FROM discovery_errors ORDER BY url")}


def test_roundtrip_idempotent_then_growing(clean_db, tmp_path, monkeypatch):  # I14
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    run_ingest(monkeypatch, clean_db, tmp_path)
    first = _counts(clean_db)
    assert [(u, o) for u, o, *_ in first if u == ECB_URL] == [(ECB_URL, 3)]
    run_ingest(monkeypatch, clean_db, tmp_path)          # unchanged file: no-op
    second = _counts(clean_db)
    assert [(u, o) for u, o, *_ in second] == [(u, o) for u, o, *_ in first]
    assert [f for _, _, f, _ in second] == [f for _, _, f, _ in first]   # first_seen_at unchanged
    with open(tmp_path / "discovery_errors.jsonl", "a") as fh:            # two more retries
        for _ in range(2):
            fh.write(json.dumps({"bank": "ecb", "context": "listing", "url": ECB_URL, "error": "ReadTimeout: again"}) + "\n")
    run_ingest(monkeypatch, clean_db, tmp_path)
    third = {u: (o, l) for u, o, _, l in _counts(clean_db)}
    assert third[ECB_URL][0] == 5
    assert third[ECB_URL][1] > dict((u, l) for u, _, _, l in second)[ECB_URL]   # last_seen_at advances


def test_ts_cutover_backwards_never_labels_ingestion_time_as_producer_time(
        clean_db, tmp_path, monkeypatch):
    """The producer starts stamping `ts` and rewrites the lines already on
    disk with a stamp EARLIER than the ingestion time we had stored for them
    (its clock is behind, or the events really are older than the night we
    first read them). The stored stamp and its flag must move together: a
    producer stamp outranks an ingestion-time fallback whatever their values,
    so last_seen_at moves BACKWARDS once — a correction, not a regression —
    and must never end up as the old fallback labelled as producer time."""
    trail = tmp_path / "discovery_errors.jsonl"
    lines = [{"bank": "ecb", "context": "listing", "url": ECB_URL, "error": "ReadTimeout: pool"},
             {"bank": "fed", "context": "listing", "url": "https://fed/x", "error": "HTTPError: 503"}]
    legacy = "".join(json.dumps(o) + "\n" for o in lines)

    trail.write_text(legacy)                                   # 1. no `ts`: fallback stamps
    run_ingest(monkeypatch, clean_db, tmp_path)
    first = _stamps(clean_db)
    t1 = first[ECB_URL][1]
    assert all(flag is True for _, _, flag in first.values())

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)              # 2. same lines, earlier `ts`
    assert ts < t1
    trail.write_text("".join(
        json.dumps({**o, "ts": "2026-01-01T00:00:00+00:00"}) + "\n" for o in lines))
    run_ingest(monkeypatch, clean_db, tmp_path)
    stamped = _stamps(clean_db)
    assert len(stamped) == len(first)                           # no shrink, same fingerprints
    for url, (first_seen, last_seen, flag) in stamped.items():
        assert (last_seen, flag) == (ts, False), url            # never (t1, False)
        assert first_seen == ts, url

    trail.write_text(legacy)                                    # 3. legacy again: fallback loses
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _stamps(clean_db) == stamped


def test_torn_file_never_empties_the_table(clean_db, tmp_path, monkeypatch):  # I15
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert len(_counts(clean_db)) == 3
    (tmp_path / "discovery_errors.jsonl").write_bytes(b'{"bank": "ecb", "url": "https://x", "err\n\xff\xfe\n')
    run_ingest(monkeypatch, clean_db, tmp_path)          # zero valid rows -> untouched, exit 0
    assert len(_counts(clean_db)) == 3
    (tmp_path / "discovery_errors.jsonl").write_text(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://only-one", "error": "A: b"}) + "\n")
    run_ingest(monkeypatch, clean_db, tmp_path)          # 1 row vs 3 held -> shrink guard
    assert len(_counts(clean_db)) == 3

    # A type-corrupt line (dict `bank`) beside good ones: it would reach
    # execute_values as an unadaptable object and make the whole run raise.
    # The run must still complete and the good rows must land.
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    with open(tmp_path / "discovery_errors.jsonl", "a") as fh:
        fh.write(json.dumps({"bank": {"code": "ecb"}, "context": "c",
                             "url": "https://poison", "error": "A: b"}) + "\n")
        fh.write(json.dumps({"bank": "fed", "context": "c", "url": "https://good",
                             "error": {"detail": "structured"}, "ts": "yesterday"}) + "\n")
    run_ingest(monkeypatch, clean_db, tmp_path)
    urls = [u for u, *_ in _counts(clean_db)]
    assert "https://poison" not in urls
    assert "https://good" in urls

    # The shrink guard is an operator-overridable tunable, and the override
    # only works because run() reads the environment at call time.
    (tmp_path / "discovery_errors.jsonl").write_text(
        json.dumps({"bank": "ecb", "context": "c", "url": "https://after-rotation", "error": "A: b"}) + "\n")
    monkeypatch.setenv("DISCOVERY_ERRORS_MIN_RETAIN_FRACTION", "0.0")
    run_ingest(monkeypatch, clean_db, tmp_path)
    rotated = {u: o for u, o, *_ in _counts(clean_db)}
    assert "https://after-rotation" in rotated              # the shrunken file went through
    assert ECB_URL in rotated                               # the upsert never deletes


def test_discovery_errors_path_override_is_honoured(clean_db, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(FIX, elsewhere / "trail.jsonl")
    data_dir = tmp_path / "data"
    data_dir.mkdir()                                       # default location: empty
    monkeypatch.setenv("DISCOVERY_ERRORS_PATH", str(elsewhere / "trail.jsonl"))
    run_ingest(monkeypatch, clean_db, data_dir)
    assert len(_counts(clean_db)) == 3
    assert not (data_dir / "discovery_errors.jsonl").exists()


def test_standalone_module_ddl_creates_the_indexes(clean_db, tmp_path):
    # ingest_discovery_errors.run() must leave the same schema the ingest.py
    # train does, indexes included — the two DDL copies are kept identical.
    import ingest_discovery_errors as ide

    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    conn = psycopg2.connect(clean_db)
    try:
        ide.run(conn, str(tmp_path), "central-bank", datetime.now(timezone.utc))
    finally:
        conn.close()
    names = {r[0] for r in fetch_all(
        clean_db, "SELECT indexname FROM pg_indexes WHERE tablename = 'discovery_errors'")}
    assert {"idx_discovery_errors_corpus_source", "idx_discovery_errors_open",
            "idx_discovery_errors_last_run"} <= names


def test_trail_beside_manifests_adds_no_documents(clean_db, tmp_path, monkeypatch):  # I16
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("d1")])
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    (tmp_path / "download_errors.jsonl").write_text('{"url": "https://z", "error": "x"}\n')
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT count(*) FROM documents")[0][0] == 1
    assert fetch_all(clean_db, "SELECT count(*) FROM discovery_errors")[0][0] == 3
