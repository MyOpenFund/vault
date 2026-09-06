import logging

import psycopg2
import pytest

from .conftest import run_ingest

pytestmark = pytest.mark.integration


def test_train_succeeds_and_creates_nothing_when_the_variables_are_unset(
    clean_db, tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("VAULT_ORCHESTRATOR_PASSWORD", raising=False)
    monkeypatch.delenv("VAULT_READONLY_PASSWORD", raising=False)
    with caplog.at_level(logging.WARNING):
        run_ingest(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'vault\\_%'")
        assert cur.fetchone() == (0,)
        # the train itself still ran to completion: its tail objects are there
        cur.execute("SELECT count(*) FROM source_health")
        assert cur.fetchone() is not None
    conn.close()
    assert "VAULT_ORCHESTRATOR_PASSWORD not set" in caplog.text
    assert "VAULT_READONLY_PASSWORD not set" in caplog.text
