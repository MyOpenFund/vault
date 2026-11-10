"""schema_gate: the pure parts of the DDL-train gate (vault #4).

Settings parsing, the fingerprint, the relation list the gate checks, and the
lock-timeout retry loop -- the last one against a fake connection, so every
branch (success, retry, give up, unrelated failure, blocker listing failing)
is pinned without a database. The real-Postgres behaviour lives in
tests/integration/test_schema_gate.py.
"""

import hashlib
import logging
import math

import psycopg2.errors
import pytest

import schema_gate
from schema_gate import SettingsError, load_settings


# ---------------------------------------------------------------------------
# load_settings


def test_defaults_when_nothing_is_set():
    s = load_settings({})
    assert s.lock_timeout == "5s"
    assert s.lock_timeout_ms == 5000
    assert s.retry_delays == (10.0, 30.0)
    assert s.attempts == 3
    assert s.force is False


def test_empty_values_mean_default():
    # compose forwards ${VAR:-}: an operator who set nothing produces "", and
    # that must not silently mean "no retry" or "no timeout".
    s = load_settings({
        "VAULT_DDL_LOCK_TIMEOUT": "",
        "VAULT_DDL_RETRY_DELAYS": "",
        "VAULT_DDL_FORCE": "",
    })
    assert s == load_settings({})


@pytest.mark.parametrize("value, normalized, ms", [
    ("5s", "5s", 5000),
    ("250ms", "250ms", 250),
    ("2min", "2min", 120000),
    ("750", "750ms", 750),        # Postgres: no unit = milliseconds
    (" 5s ", "5s", 5000),         # surrounding blanks from a .env file
    ("05s", "5s", 5000),
])
def test_lock_timeout_accepted(value, normalized, ms):
    s = load_settings({"VAULT_DDL_LOCK_TIMEOUT": value})
    assert s.lock_timeout == normalized
    assert s.lock_timeout_ms == ms


@pytest.mark.parametrize("value", [
    "0", "0s", "0ms", "-1", "-5s", "5 s", "5S", "5sec", "5h", "1.5s", "s",
    "five", "5s;", "5s; DROP TABLE documents", "'5s'", "nan", "inf",
    "٥s",            # Arabic-Indic five: str.isdigit() would accept it
    "2147484min",         # above Postgres's INT_MAX milliseconds
])
def test_lock_timeout_refused(value):
    with pytest.raises(SettingsError) as exc:
        load_settings({"VAULT_DDL_LOCK_TIMEOUT": value})
    assert "VAULT_DDL_LOCK_TIMEOUT" in str(exc.value)
    assert repr(value) in str(exc.value)


def test_lock_timeout_upper_bound_is_postgres_int_max():
    s = load_settings({"VAULT_DDL_LOCK_TIMEOUT": str(2**31 - 1)})
    assert s.lock_timeout_ms == 2**31 - 1
    with pytest.raises(SettingsError):
        load_settings({"VAULT_DDL_LOCK_TIMEOUT": str(2**31)})


@pytest.mark.parametrize("value, delays", [
    ("10,30", (10.0, 30.0)),
    ("0", (0.0,)),
    ("0.05,0.1,0.2", (0.05, 0.1, 0.2)),
    (" 10 , 30 ", (10.0, 30.0)),
    ("none", ()),
    ("NONE", ()),
])
def test_retry_delays_accepted(value, delays):
    s = load_settings({"VAULT_DDL_RETRY_DELAYS": value})
    assert s.retry_delays == delays
    assert s.attempts == len(delays) + 1


@pytest.mark.parametrize("value", [
    "10,,30", "10,", ",10", ",", "-1", "10,-1", "abc", "10,abc", "nan",
    "NaN", "inf", "-inf", "1e400", "10;30", "10 30", "0x10", "١٠",
    "10_0",  # float() accepts underscores; an operator never means that
])
def test_retry_delays_refused(value):
    with pytest.raises(SettingsError) as exc:
        load_settings({"VAULT_DDL_RETRY_DELAYS": value})
    assert "VAULT_DDL_RETRY_DELAYS" in str(exc.value)
    assert repr(value) in str(exc.value)


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("Yes", True), (" yes ", True),
    ("0", False), ("false", False), ("No", False), ("", False),
])
def test_force_accepted(value, expected):
    assert load_settings({"VAULT_DDL_FORCE": value}).force is expected


@pytest.mark.parametrize("value", ["2", "on", "y", "maybe", "1 1", "-1", "truee"])
def test_force_refused(value):
    with pytest.raises(SettingsError) as exc:
        load_settings({"VAULT_DDL_FORCE": value})
    assert "VAULT_DDL_FORCE" in str(exc.value)
    assert repr(value) in str(exc.value)


def test_every_retry_delay_is_finite_and_non_negative():
    s = load_settings({"VAULT_DDL_RETRY_DELAYS": "0,1.5,300"})
    assert all(math.isfinite(d) and d >= 0 for d in s.retry_delays)


# ---------------------------------------------------------------------------
# fingerprint


def test_fingerprint_is_sha256_hex_of_the_text():
    text = "CREATE TABLE IF NOT EXISTS t (x int);"
    assert schema_gate.fingerprint(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_fingerprint_changes_with_any_edit():
    assert schema_gate.fingerprint("a") != schema_gate.fingerprint("a ")


# ---------------------------------------------------------------------------
# expected_relations


def test_expected_relations_in_order_of_appearance_without_duplicates():
    sql_text = """
    DROP VIEW IF EXISTS v1;
    CREATE TABLE IF NOT EXISTS alpha (id int);
    create table if not exists Beta (id int);
    CREATE INDEX IF NOT EXISTS idx_alpha ON alpha(id);
    CREATE TABLE IF NOT EXISTS alpha (id int);
    CREATE VIEW v1 AS SELECT 1;
    CREATE
        VIEW   v2 AS SELECT 2;
    """
    assert schema_gate.expected_relations(sql_text) == ("alpha", "beta", "v1", "v2")


def test_expected_relations_ignores_comments():
    # The real train has exactly this trap: a comment explaining that another
    # module "also issues this same\n-- CREATE TABLE IF NOT EXISTS so the
    # module stays usable" would otherwise add a relation called `so`.
    sql_text = """
    -- ingest_cadence.run() also issues this same
    -- CREATE TABLE IF NOT EXISTS so the module stays usable standalone
    -- (CREATE OR REPLACE VIEW cannot drop a column); CREATE VIEW nope AS ...
    CREATE TABLE IF NOT EXISTS real_one (x int); -- CREATE VIEW trailing AS
    """
    assert schema_gate.expected_relations(sql_text) == ("real_one",)


def test_expected_relations_refuses_a_train_it_cannot_parse():
    with pytest.raises(ValueError):
        schema_gate.expected_relations("SELECT 1;")


# ---------------------------------------------------------------------------
# run_with_lock_retries


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.conn.log.append(("execute", statement, params))
        self._last = statement
        if self.conn.blocker_query_fails and statement is schema_gate.BLOCKERS_SQL:
            raise psycopg2.errors.InsufficientPrivilege("no")

    def fetchall(self):
        assert self._last is schema_gate.BLOCKERS_SQL
        return list(self.conn.blockers)


class FakeConn:
    def __init__(self, blockers=(), blocker_query_fails=False):
        self.log = []
        self.blockers = blockers
        self.blocker_query_fails = blocker_query_fails

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))


def lock_timeout_error():
    return psycopg2.errors.LockNotAvailable("canceling statement due to lock timeout")


class Apply:
    """apply(cur) that fails with a lock timeout for its first `fail` calls."""

    def __init__(self, fail=0, error=None):
        self.calls = 0
        self.fail = fail
        self.error = error

    def __call__(self, cur):
        self.calls += 1
        cur.execute("TRAIN")
        if self.error is not None:
            raise self.error
        if self.calls <= self.fail:
            raise lock_timeout_error()


SETTINGS = load_settings({"VAULT_DDL_LOCK_TIMEOUT": "250ms", "VAULT_DDL_RETRY_DELAYS": "10,30"})
RELATIONS = ("documents", "runs")
BLOCKER = (4242, "metabase_user", "Metabase v0.50", "idle in transaction", 93.5,
           "documents, runs_sources")


def statements(conn):
    return [e[1] if e[0] == "execute" else e[0] for e in conn.log]


def test_success_first_attempt_sets_the_timeout_then_commits():
    conn, apply, slept = FakeConn(), Apply(), []
    schema_gate.run_with_lock_retries(conn, apply, SETTINGS, RELATIONS, sleep=slept.append)
    assert apply.calls == 1
    assert slept == []
    assert conn.log[0] == ("execute", schema_gate.SET_LOCK_TIMEOUT_SQL, ("250ms",))
    assert statements(conn)[1:] == ["TRAIN", "commit"]


def test_retries_with_the_configured_delays_then_succeeds():
    conn, apply, slept = FakeConn(blockers=[BLOCKER]), Apply(fail=2), []
    schema_gate.run_with_lock_retries(conn, apply, SETTINGS, RELATIONS, sleep=slept.append)
    assert apply.calls == 3
    assert slept == [10.0, 30.0]
    # every attempt re-arms the timeout: SET LOCAL died with the rolled-back transaction
    assert statements(conn).count(schema_gate.SET_LOCK_TIMEOUT_SQL) == 3
    assert statements(conn)[-1] == "commit"
    assert statements(conn).count("commit") == 1


def test_gives_up_after_the_last_attempt_with_blockers_logged(caplog):
    conn, apply, slept = FakeConn(blockers=[BLOCKER]), Apply(fail=99), []
    with caplog.at_level(logging.WARNING, logger="ingest"):
        with pytest.raises(schema_gate.TrainLockTimeout) as exc:
            schema_gate.run_with_lock_retries(
                conn, apply, SETTINGS, RELATIONS, sleep=slept.append)
    assert apply.calls == 3
    assert slept == [10.0, 30.0]          # no sleep after the last attempt
    assert "commit" not in statements(conn)
    assert "3 attempt" in str(exc.value)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3, "one WARNING per failed attempt"
    for message in warnings:
        for fragment in ("4242", "metabase_user", "Metabase v0.50", "idle in transaction",
                         "93.5", "documents, runs_sources"):
            assert fragment in message
    # the blocker listing is asked about the train's own relations
    blocker_calls = [e for e in conn.log if e[0] == "execute" and e[1] is schema_gate.BLOCKERS_SQL]
    assert blocker_calls and all(e[2] == (list(RELATIONS),) for e in blocker_calls)


def test_single_attempt_when_retries_are_disabled():
    settings = load_settings({"VAULT_DDL_RETRY_DELAYS": "none"})
    conn, apply, slept = FakeConn(), Apply(fail=99), []
    with pytest.raises(schema_gate.TrainLockTimeout):
        schema_gate.run_with_lock_retries(conn, apply, settings, RELATIONS, sleep=slept.append)
    assert apply.calls == 1
    assert slept == []


def test_an_unrelated_failure_is_not_retried():
    boom = psycopg2.errors.UndefinedTable("relation x does not exist")
    conn, apply, slept = FakeConn(), Apply(error=boom), []
    with pytest.raises(psycopg2.errors.UndefinedTable):
        schema_gate.run_with_lock_retries(conn, apply, SETTINGS, RELATIONS, sleep=slept.append)
    assert apply.calls == 1
    assert slept == []
    assert "commit" not in statements(conn)


def test_no_blocker_found_is_said_out_loud(caplog):
    conn, apply = FakeConn(blockers=[]), Apply(fail=1)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        schema_gate.run_with_lock_retries(conn, apply, SETTINGS, RELATIONS, sleep=lambda _s: None)
    assert any("no session" in r.getMessage() for r in caplog.records)


def test_a_failing_blocker_listing_never_masks_the_retry(caplog):
    conn, apply = FakeConn(blocker_query_fails=True), Apply(fail=1)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        schema_gate.run_with_lock_retries(conn, apply, SETTINGS, RELATIONS, sleep=lambda _s: None)
    assert apply.calls == 2
    assert statements(conn)[-1] == "commit"
    assert any("could not list" in r.getMessage() for r in caplog.records)


def test_the_blocker_query_never_reads_query_text():
    # pg_stat_activity.query can carry literals (a password in an ALTER ROLE,
    # client data in a WHERE): it must never reach the log.
    import re
    assert not re.search(r"\bquery\b", schema_gate.BLOCKERS_SQL, re.IGNORECASE)
    for column in ("pid", "usename", "application_name", "state", "xact_start"):
        assert column in schema_gate.BLOCKERS_SQL
