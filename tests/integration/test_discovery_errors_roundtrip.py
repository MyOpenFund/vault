import json
import shutil
from pathlib import Path

import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration
FIX = Path(__file__).parent.parent / "fixtures" / "discovery_errors_real.jsonl"
ECB_URL = "https://www.ecb.europa.eu/press/pr/html/index.en.html?page=3"


def _counts(pg_url):
    return fetch_all(pg_url, "SELECT url, occurrences, first_seen_at, last_seen_at FROM discovery_errors ORDER BY url")


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
    assert third[ECB_URL][1] >= dict((u, l) for u, _, _, l in second)[ECB_URL]  # last_seen_at monotone


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


def test_trail_beside_manifests_adds_no_documents(clean_db, tmp_path, monkeypatch):  # I16
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("d1")])
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")
    (tmp_path / "download_errors.jsonl").write_text('{"url": "https://z", "error": "x"}\n')
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT count(*) FROM documents")[0][0] == 1
    assert fetch_all(clean_db, "SELECT count(*) FROM discovery_errors")[0][0] == 3
