import pytest

from .conftest import fetch_all, run_ingest

pytestmark = pytest.mark.integration

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
