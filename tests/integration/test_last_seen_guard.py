from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from psycopg2.extras import execute_values

from .conftest import fetch_all, run_ingest

pytestmark = pytest.mark.integration


def _upsert(pg_url, run_ts):
    import ingest
    row = ingest.parse_line(
        '{"doc_id": "x", "bank_code": "us", "doc_type": "C1", "title": "t"}',
        "m.jsonl", 1, run_ts, "central-bank")
    conn = psycopg2.connect(pg_url)
    with conn.cursor() as cur:
        execute_values(cur, ingest.UPSERT_SQL, [row])
    conn.commit()
    conn.close()


def test_last_seen_at_never_moves_backwards(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)  # DDL only
    t1 = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    t2, t3 = t1 + timedelta(hours=1), t1 + timedelta(hours=2)
    _upsert(clean_db, t2)
    _upsert(clean_db, t1)   # older stamp arrives late
    assert fetch_all(clean_db, "SELECT last_seen_at FROM documents WHERE doc_id = 'x'")[0][0] == t2
    _upsert(clean_db, t3)   # newer stamp still advances
    assert fetch_all(clean_db, "SELECT last_seen_at FROM documents WHERE doc_id = 'x'")[0][0] == t3
