"""The API's startup gate (issue #5).

Since the API connects as `vault_readonly` — a role the DDL train creates, so
one that does not exist on a brand-new cluster until the ingestion service has
run once — the interesting question is what the container does in that window.
The answer must be *loud*: one ERROR naming the role and the exact command that
fixes it, then a non-zero exit, so `docker compose ps` shows a restarting
container and `logs` says why. The failure mode this replaces is worse than a
crash: a process that starts happily, answers `/health` with "ok", and returns
500 on every route that touches the corpus.

And, as everywhere else in this chantier, the password never reaches a log line
or an exception — here the driver's own error text is redacted before it is
logged, because a malformed DSN makes psycopg2 echo the connection string back.
"""
import logging

import psycopg2
import pytest

import db
import main

URL = "postgresql://vault_readonly:s3cr3t@postgres:5432/documents"
MISSING_ROLE = ('connection to server at "postgres", port 5432 failed: '
                'FATAL:  role "vault_readonly" does not exist')


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_a_reachable_database_is_verified_and_the_probe_connection_closed(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "DATABASE_URL", URL)
    monkeypatch.setattr(db.psycopg2, "connect", lambda *a, **k: conn)
    db.verify_connection()
    assert conn.closed, "the startup probe leaked its connection"


def test_a_missing_role_is_reported_loudly_with_the_command_that_fixes_it(
    monkeypatch, caplog
):
    monkeypatch.setattr(db, "DATABASE_URL", URL)

    def boom(*a, **k):
        raise psycopg2.OperationalError(MISSING_ROLE)

    monkeypatch.setattr(db.psycopg2, "connect", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc:
            db.verify_connection()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "vault_readonly" in logged
    assert "docker compose run --rm ingestion" in logged
    assert 'role "vault_readonly" does not exist' in logged  # the driver's own words
    assert caplog.records and all(r.levelno >= logging.ERROR for r in caplog.records)
    # loud, but not chatty enough to leak: no password anywhere
    assert "s3cr3t" not in logged
    assert "s3cr3t" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_the_drivers_echo_of_the_connection_string_is_redacted(monkeypatch, caplog):
    # psycopg2 quotes the DSN back at you for a malformed connection string --
    # the one error whose text really does carry the password.
    monkeypatch.setattr(db, "DATABASE_URL", URL)

    def boom(*a, **k):
        raise psycopg2.ProgrammingError(f"invalid dsn: {URL}")

    monkeypatch.setattr(db.psycopg2, "connect", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            db.verify_connection()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "s3cr3t" not in logged
    assert "invalid dsn" in logged


def test_the_app_refuses_to_start_when_the_database_is_unreachable(monkeypatch):
    # Uvicorn turns a lifespan-startup exception into a non-zero exit, which is
    # what `restart: unless-stopped` then retries -- visibly.
    from fastapi.testclient import TestClient

    def boom():
        raise RuntimeError("cannot reach the vault database")

    monkeypatch.setattr(main, "verify_connection", boom)
    with pytest.raises(RuntimeError):
        with TestClient(main.app):
            pass


def test_the_app_serves_once_the_database_answers(monkeypatch):
    from fastapi.testclient import TestClient

    calls = []
    monkeypatch.setattr(main, "verify_connection", lambda: calls.append(1))
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    assert calls == [1], "the startup gate did not run"
