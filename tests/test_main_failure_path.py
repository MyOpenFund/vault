"""What main() must raise when the run fails *and* the connection is gone.

The failure handler rolls back before logging and re-raising. On a connection
that psycopg2 has already marked broken — the server went away mid-run, which
is exactly when a run fails — that rollback raises `InterfaceError` itself.
Unguarded, it replaces the real cause: the operator gets "connection already
closed" and never sees the error that actually killed the run, and the
`log.exception` line never runs either.

No database here: a fake connection whose cursor raises on the first statement
(the DDL lock's `pg_try_advisory_lock`) and whose `rollback` then raises is
enough to pin the ordering.
"""

import pytest

import ingest


class _Boom(RuntimeError):
    """The original failure — the one the caller must actually see."""


class _DeadCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        raise _Boom("the statement that really failed")


class _DeadConn:
    """A connection whose rollback fails the way a dropped one does."""

    def __init__(self):
        self.autocommit = False
        self.closed = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return _DeadCursor(self)

    def rollback(self):
        self.rollbacks += 1
        raise ingest.psycopg2.InterfaceError("connection already closed")

    def commit(self):  # pragma: no cover — never reached in this scenario
        raise AssertionError("commit must not be reached")

    def close(self):
        self.closes += 1
        self.closed = 1


@pytest.fixture()
def dead_conn(monkeypatch):
    conn = _DeadConn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.setattr(ingest.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(ingest, "find_jsonl_files", lambda _root: [])
    return conn


def test_failing_rollback_does_not_mask_the_original_error(dead_conn):
    with pytest.raises(_Boom):
        ingest.main(corpus="central-bank", data_dir="/nowhere")
    assert dead_conn.rollbacks >= 1, "the failure handler never tried to roll back"


def test_the_connection_is_still_closed_when_rollback_fails(dead_conn):
    with pytest.raises(_Boom):
        ingest.main(corpus="central-bank", data_dir="/nowhere")
    assert dead_conn.closes == 1, "a failing rollback skipped the connection close"


def test_the_failure_is_logged_even_when_rollback_fails(dead_conn, caplog):
    with caplog.at_level("ERROR", logger="ingest"):
        with pytest.raises(_Boom):
            ingest.main(corpus="central-bank", data_dir="/nowhere")
    assert any("Run failed" in r.getMessage() for r in caplog.records), (
        "a failing rollback swallowed the log.exception describing the run"
    )
