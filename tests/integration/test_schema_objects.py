import json
import shutil
from pathlib import Path

import pytest

from .conftest import (
    fetch_all, make_doc, make_entry, run_ingest, write_cadence, write_manifest,
)

pytestmark = pytest.mark.integration

FIX = Path(__file__).parent.parent / "fixtures" / "discovery_errors_real.jsonl"

EXPECTED_INDEXES = {
    "idx_documents_live_agg",
    "idx_documents_corpus_doc_type",
    "idx_documents_extra_gin",
    "idx_runs_corpus_finished",
    "idx_discovery_errors_corpus_source",
    "idx_discovery_errors_open",
    "idx_discovery_errors_last_run",
}

EXPECTED_VIEWS = {
    "runs_sources", "rag_backlog", "rag_backlog_any",
    "sources_without_cadence", "source_health",
}


def _names(pg_url, sql):
    return {r[0] for r in fetch_all(pg_url, sql)}


def test_new_indexes_exist_after_one_train(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)  # empty data dir: DDL only
    run_ingest(monkeypatch, clean_db, tmp_path)  # run twice: prove idempotency
    names = _names(clean_db, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    assert EXPECTED_INDEXES <= names


def test_views_exist_and_train_is_idempotent(clean_db, tmp_path, monkeypatch):
    for _ in range(3):  # I6: three consecutive trains
        run_ingest(monkeypatch, clean_db, tmp_path)
    names = _names(clean_db, "SELECT table_name FROM information_schema.views WHERE table_schema = current_schema()")
    assert EXPECTED_VIEWS <= names


def test_discovery_errors_table_exists(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)  # empty data dir: DDL only
    names = _names(clean_db, "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    assert "discovery_errors" in names


def _snapshot(pg_url):
    """Everything a re-run must leave untouched. last_seen_at is deliberately
    excluded from the discovery_errors projection: with no producer `ts` it is
    ingestion time and legitimately advances on every run."""
    return {
        "documents": fetch_all(pg_url, "SELECT doc_id, extra FROM documents ORDER BY doc_id"),
        "errors": fetch_all(
            pg_url,
            "SELECT fingerprint, corpus, source_code, context, url, error_class, "
            "error, occurrences, first_seen_at FROM discovery_errors ORDER BY fingerprint",
        ),
        "cadence": fetch_all(pg_url, "SELECT count(*) FROM cadence")[0][0],
        "runs": fetch_all(pg_url, "SELECT count(*) FROM runs")[0][0],
    }


def test_three_whole_ingester_runs_are_idempotent(clean_db, tmp_path, monkeypatch):  # I5
    """The whole train — documents + cadence + runs + discovery errors — over
    one unchanged data dir three times: run 2 and run 3 must be byte-identical
    to run 1 on everything that is not ingestion time."""
    write_manifest(
        tmp_path / "manifest", "us.jsonl",
        [make_doc("d1", unknown_field="keep-me"), make_doc("d2", source="ecb")],
    )
    write_cadence(tmp_path, [make_entry(), make_entry(bank_code="ecb", doc_type="C2")])
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"run_id": "r-idem", "tool": "central-bank-corpus",
                    "outcome": "ok", "sources": []}) + "\n",
        encoding="utf-8",
    )
    shutil.copy(FIX, tmp_path / "discovery_errors.jsonl")

    run_ingest(monkeypatch, clean_db, tmp_path)
    first = _snapshot(clean_db)
    assert len(first["documents"]) == 2
    assert len(first["errors"]) == 3
    assert first["cadence"] == 2 and first["runs"] == 1

    for _ in range(2):  # runs 2 and 3
        run_ingest(monkeypatch, clean_db, tmp_path)
        assert _snapshot(clean_db) == first
