import pytest

from .conftest import fetch_all, run_ingest

pytestmark = pytest.mark.integration

EXPECTED_INDEXES = {
    "idx_documents_live_agg",
    "idx_documents_corpus_doc_type",
    "idx_documents_extra_gin",
    "idx_runs_corpus_finished",
}


def _names(pg_url, sql):
    return {r[0] for r in fetch_all(pg_url, sql)}


def test_new_indexes_exist_after_one_train(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)  # empty data dir: DDL only
    run_ingest(monkeypatch, clean_db, tmp_path)  # run twice: prove idempotency
    names = _names(clean_db, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    assert EXPECTED_INDEXES <= names
