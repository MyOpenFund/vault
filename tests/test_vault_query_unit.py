"""Argument checks of `vault_query.search_documents`, without a database.

`sort_by`, `sort_dir` and the column projection are interpolated into the SQL
text, so their allowlists are the injection guard; `limit` and `offset` are
bounds a caller (an API query string, an MCP tool argument) controls. Every
refusal has to happen before a cursor is opened: the connection stub below
fails the test if any SQL would run.
"""

import datetime

import pytest

from vault_query import (
    InvalidQuery, build_where_clause, corpus_stats, get_document, open_discovery_errors, recent_runs,
    runs_status, search_documents, source_health,
)


class _NoConnection:
    def cursor(self, *a, **k):
        raise AssertionError("argument checks must run before any SQL")


@pytest.mark.parametrize("kwargs", [
    {"sort_by": "local_path; DROP TABLE documents"},
    {"sort_dir": "sideways"},
    {"limit": 0},
    {"offset": -1},
    {"limit": True},
    {"limit": "10"},
    {"limit": None},
    {"limit": 1.5},
    {"offset": True},
    {"offset": "0"},
    {"limit": 2**31},
    {"columns": ("doc_id", "password")},
    {"columns": ()},
])
def test_search_documents_refuses_bad_arguments_before_sql(kwargs):
    """Each bad argument raises InvalidQuery, never reaches the database.

    InvalidQuery is what the API maps to HTTP 400 and the MCP server to an
    `invalid_argument` error; anything else (a psycopg2 error, a 500) would
    mean the value travelled into the SQL first.
    """
    with pytest.raises(InvalidQuery):
        search_documents(_NoConnection(), {}, **kwargs)


@pytest.mark.parametrize("args, kwargs", [
    (("local_path",), {}),
    (("year; DROP TABLE documents",), {}),
    (("year",), {"limit": 0}),
    (("year",), {"limit": "5"}),
    (("year",), {"limit": True}),
])
def test_corpus_stats_refuses_bad_arguments_before_sql(args, kwargs):
    """An unlisted group_by or a limit below 1 raises InvalidQuery, no SQL runs.

    `group_by` is interpolated into GROUP BY, so GROUPABLE_FIELDS is the
    injection guard: a column outside it (even a real one such as
    `local_path`) must be refused before a cursor is opened.
    """
    with pytest.raises(InvalidQuery):
        corpus_stats(_NoConnection(), *args, **kwargs)


@pytest.mark.parametrize("fn", [runs_status, open_discovery_errors, recent_runs])
def test_health_reads_refuse_a_limit_below_one_before_sql(fn):
    """`limit` is caller-controlled: below 1 is InvalidQuery, no SQL runs."""
    with pytest.raises(InvalidQuery):
        fn(_NoConnection(), limit=0)


@pytest.mark.parametrize("fn, kwargs", [
    (runs_status, {"max_error_samples": 0}),
    (runs_status, {"max_text_chars": 0}),
    (open_discovery_errors, {"max_text_chars": 0}),
    (open_discovery_errors, {"max_text_chars": True}),
    (runs_status, {"max_error_samples": "5"}),
])
def test_health_reads_refuse_text_bounds_below_one_before_sql(fn, kwargs):
    """The text bounds are caller-controlled too: below 1 is InvalidQuery, no SQL runs."""
    with pytest.raises(InvalidQuery):
        fn(_NoConnection(), **kwargs)


@pytest.mark.parametrize("fn", [runs_status, open_discovery_errors, recent_runs])
@pytest.mark.parametrize("limit", ["5", True, None, 2**31])
def test_health_reads_refuse_a_non_int_limit_before_sql(fn, limit):
    """A limit that is not a plain int in range (a str, a bool, None, past
    int4) is InvalidQuery, not a psycopg2 error from the LIMIT parameter."""
    with pytest.raises(InvalidQuery):
        fn(_NoConnection(), limit=limit)


# The `since` grammar: ASCII YYYY-MM-DD[THH:MM[:SS[.f{1,6}]][Z|+HH:MM|-HH:MM]],
# a real calendar instant, offset magnitude at most 15:59. Every accepted string
# must also run on Postgres (tests/integration/test_vault_query.py keeps the
# same list and runs it).
ACCEPTED_SINCE = [
    "2026-10-01",
    "2026-10-01T00:00",
    "2026-10-01T00:00:00",
    "2026-10-01T00:00:00.1",
    "2026-10-01T00:00:00.123456",
    "2026-10-01T00:00Z",
    "2026-10-01T00:00+15:59",
    "2026-10-01T00:00-15:59",
    "2026-10-01T23:59:59+00:00",
    "2024-02-29T12:00",           # a real leap day
    "0001-01-01T00:00Z",
    "9999-12-31T23:59:59Z",
]

REFUSED_SINCE = [
    # Shapes Python's fromisoformat takes but Postgres rejects.
    "2026-W40-1T00:00",           # week date
    "2026-W40-1",
    "2026-10-01T00:00+16:00",     # past Postgres's +-15:59 displacement limit
    "2026-10-01T00:00-16:00",
    "2026-10-01T00:00+24:00",
    # Shapes outside the documented grammar (some of them Postgres takes).
    "2026-274",                   # ordinal date
    "20261001",                   # basic format
    "20261001T0000",
    "2026-10-01 00:00",           # space separator
    "2026-10-01t00:00",           # lowercase t
    "2026-10-01T00:00z",          # lowercase z
    "2026-10-01Z",                # offset without a time
    "2026-10-01+02:00",
    "2026-10-01T00",              # hour without minutes
    "2026-10-01T0:00",
    "2026-10-01T00:00+1:00",
    "2026-10-01T00:00+0100",
    "2026-10-01T00:00+01",
    "2026-10-01T00:00+01:00:30",  # offset seconds
    "2026-10-01T00:00:00.1234567",  # 7 fractional digits
    "2026-10-01T00:00:00.",
    "2026-10-01T00:00:00,5",      # comma decimal mark
    # Right shape, not a real instant.
    "2026-10-01T24:00",
    "2026-10-01T23:59:60",
    "2026-10-01T23:60",
    "2026-02-29",                 # 2026 is not a leap year
    "1900-02-29",                 # century, not a leap year
    "0000-01-01",
    "2026-13-01",
    "2026-00-10",
    "2026-10-32",
    "2026-10-01T25:00",
    "2026-10-01T12:60",
    "2026-10-01T12:00:61",
    # Offset minutes 60-99: Python normalises "+05:60" to +06:00, Postgres
    # rejects it (time zone displacement out of range).
    "2026-10-01T00:00+05:60",
    "2026-10-01T00:00+00:60",
    "2026-10-01T00:00+14:99",
    "2026-10-01T00:00-09:75",
    "2026-10-01T00:00+29:00",
    # Not ASCII digits (str.isdigit and the `\d` class would take them).
    "２０２６-１０-０１",  # full-width
    "٢٠٢٦-١٠-٠١",  # Arabic-Indic
    "2026-10-0١",
    # Whitespace around a valid value.
    "2026-10-01\n",
    " 2026-10-01",
    "2026-10-01 ",
    "2026-10-01T00:00Z\n",
    "2026-10-01\x00",
    # Not a timestamp at all.
    "",
    "yesterday",
    "now",
    "2026-10-01T00:00Z; DROP TABLE runs",
    # Not a str, date or datetime.
    True,
    False,
    5,
    5.0,
    b"2026-10-01",
    bytearray(b"2026-10-01"),
    object(),
    ["2026-10-01"],
    datetime.time(0, 0),
    datetime.timedelta(days=1),
]

_UTC_PLUS = lambda **kw: datetime.timezone(datetime.timedelta(**kw))  # noqa: E731

ACCEPTED_SINCE_OBJECTS = [
    datetime.date(2010, 1, 13),
    datetime.date(1, 1, 1),
    datetime.datetime(2010, 1, 13, 12),                                   # naive
    datetime.datetime(2010, 1, 13, 12, tzinfo=datetime.timezone.utc),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=15, minutes=59, seconds=59)),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=-15, minutes=-59, seconds=-59)),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=5, minutes=30, seconds=15)),
]

# Aware datetimes psycopg2 would render as an offset Postgres rejects
# (measured on Postgres 16: +16:00 is InvalidTimeZoneDisplacementValue, a
# sub-second offset is InvalidDatetimeFormat).
REFUSED_SINCE_OBJECTS = [
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=16)),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=-16)),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=23, minutes=59)),
    datetime.datetime(2010, 1, 13, tzinfo=_UTC_PLUS(hours=1, microseconds=5)),
]


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
@pytest.mark.parametrize("since", REFUSED_SINCE + REFUSED_SINCE_OBJECTS, ids=repr)
def test_health_reads_refuse_a_since_outside_the_grammar_before_sql(fn, since):
    """Anything outside the documented `since` grammar is InvalidQuery, before SQL.

    The refusals Python alone would let through (a week date, an offset past
    15:59) are the ones that used to reach Postgres and come back as a
    DataError, a 500 instead of a 400.
    """
    with pytest.raises(InvalidQuery, match=r"since must be an ISO-8601 timestamp"):
        fn(_NoConnection(), since=since)


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
@pytest.mark.parametrize("since", ACCEPTED_SINCE + ACCEPTED_SINCE_OBJECTS, ids=repr)
def test_health_reads_accept_a_since_inside_the_grammar(fn, since):
    """Inside the grammar the check passes and the query goes on to SQL."""
    with pytest.raises(AssertionError, match="before any SQL"):
        fn(_NoConnection(), since=since)


def test_since_refusal_names_the_grammar():
    """The message tells the caller what to send, not only that it was wrong."""
    with pytest.raises(InvalidQuery) as exc:
        runs_status(_NoConnection(), since="2026-W40-1")
    message = str(exc.value)
    assert "YYYY-MM-DD[THH:MM[:SS[.ffffff]][Z|+HH:MM|-HH:MM]]" in message
    assert "15:59" in message


def test_since_reaches_sql_unchanged():
    """Validation only: the caller's original string is the bound parameter."""
    executed = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append(params)

        def fetchall(self):
            return []

    class _Connection:
        def cursor(self, *a, **k):
            return _Cursor()

    since = "2026-10-01T00:00:00.5-15:59"
    recent_runs(_Connection(), since=since)
    runs_status(_Connection(), since=since)
    assert all(since in params for params in executed) and len(executed) == 2


def test_postgres_offset_limit_is_a_named_constant():
    """The +-15:59 bound is Postgres's, kept as one named module constant."""
    from vault_query import health

    assert health.POSTGRES_MAX_UTC_OFFSET == datetime.timedelta(hours=15, minutes=59, seconds=59)


@pytest.mark.parametrize("filters, message", [
    ({"year": "abc"}, "year must be an integer"),
    ({"year": True}, "year must be an integer"),
    ({"year": 2010.0}, "year must be an integer"),
    ({"year": -1}, "year must be an integer"),
    ({"date_from": "garbage"}, r"date_from must be an ISO date \(YYYY-MM-DD\)"),
    ({"date_to": "13/01/2010"}, r"date_to must be an ISO date \(YYYY-MM-DD\)"),
    ({"date_from": "2010-W02-3"}, r"date_from must be an ISO date \(YYYY-MM-DD\)"),  # week date
    ({"date_from": "2010-02-30"}, r"date_from must be an ISO date \(YYYY-MM-DD\)"),  # not a real day
    ({"date_from": 20100113}, r"date_from must be an ISO date \(YYYY-MM-DD\)"),
])
def test_filter_values_are_refused_before_sql(filters, message):
    """A filter value the column cannot hold is InvalidQuery (a 400), not a
    psycopg2 DataError from inside the statement (a 500)."""
    with pytest.raises(InvalidQuery, match=message):
        build_where_clause(filters)
    with pytest.raises(InvalidQuery, match=message):
        search_documents(_NoConnection(), filters)


@pytest.mark.parametrize("filters, clause, value", [
    ({"year": 2010}, "year = %s", 2010),
    ({"date_from": "2010-01-13"}, "date >= %s", "2010-01-13"),
    ({"date_to": datetime.date(2010, 1, 13)}, "date <= %s", datetime.date(2010, 1, 13)),
    ({"date_to": datetime.datetime(2010, 1, 13, 12)}, "date <= %s",
     datetime.datetime(2010, 1, 13, 12)),
])
def test_valid_filter_values_reach_sql_unchanged(filters, clause, value):
    """Validation only: the caller's original value is the bound parameter."""
    where_sql, params = build_where_clause(filters)
    assert clause in where_sql
    assert params == [value]


# --- q is a literal substring (ILIKE with every wildcard escaped) -------------

@pytest.mark.parametrize("q, param", [
    ("housing", "%housing%"),
    ("50%", "%50!%%"),
    ("a_b", "%a!_b%"),
    ("wow!", "%wow!!%"),
    ("!%_", "%!!!%!_%"),          # the escape character is escaped first
    ("%", "%!%%"),
    ("_", "%!_%"),
    ("!", "%!!%"),
    ("%%", "%!%!%%"),
    ("back\\slash", "%back\\slash%"),  # a backslash is an ordinary character now
    ("\\%", "%\\!%%"),
    ("caf\u00e9 \u00a7", "%caf\u00e9 \u00a7%"),  # non-ASCII is untouched
])
def test_q_is_escaped_into_a_literal_ilike_pattern(q, param):
    """`%`, `_` and the escape character itself are escaped; the rest is untouched."""
    where_sql, params = build_where_clause({"q": q})
    assert "title ILIKE %s ESCAPE '!'" in where_sql
    assert params == [param]


def test_like_escape_needs_no_backslash():
    """The escape character is not `\\`: the SQL literal then reads the same
    whatever standard_conforming_strings is."""
    from vault_query import filters

    assert filters.LIKE_ESCAPE == "!"


def test_empty_q_adds_no_clause():
    """An empty `q` is no filter, as before (not a match-everything ILIKE)."""
    where_sql, params = build_where_clause({"q": ""})
    assert "ILIKE" not in where_sql and params == []


# --- COUNT and page in one snapshot ------------------------------------------

class _RecordingCursor:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom in " + self.fail_on)

    def fetchone(self):
        return {"total": 0}

    def fetchall(self):
        return []


class _RecordingConnection:
    """Records the SQL `search_documents` sends; `status` is psycopg2's transaction status."""

    def __init__(self, autocommit, status=0, fail_on=None):
        self.autocommit, self.status, self.fail_on, self.log = autocommit, status, fail_on, []

    def cursor(self, *a, **k):
        return _RecordingCursor(self.log, self.fail_on)

    def get_transaction_status(self):
        return self.status

    def commit(self):
        raise AssertionError("search_documents must not call commit() on the caller's connection")

    def rollback(self):
        raise AssertionError("search_documents must not call rollback() on the caller's connection")


def _kinds(log):
    return [s.split()[0] + (" COUNT" if "COUNT(*)" in s else "") for s in log]


def test_search_runs_count_and_page_in_one_read_only_snapshot_on_autocommit():
    """Autocommit and idle: BEGIN REPEATABLE READ READ ONLY, COUNT, page, COMMIT."""
    conn = _RecordingConnection(autocommit=True)
    search_documents(conn, {"q": "x"})
    assert conn.log[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert _kinds(conn.log) == ["BEGIN", "SELECT COUNT", "SELECT", "COMMIT"]


@pytest.mark.parametrize("autocommit, status", [
    (False, 0),   # psycopg2 will open its own transaction: the caller owns it
    (False, 2),   # already in a transaction
    (True, 2),    # autocommit, but the caller sent BEGIN itself
    (True, 3),    # autocommit, caller's transaction already failed
])
def test_search_leaves_a_caller_transaction_alone(autocommit, status):
    """Outside the autocommit-and-idle case, no BEGIN, COMMIT or ROLLBACK is sent.

    Committing would end a transaction the caller opened (an explicit BEGIN on
    an autocommit connection included); the caller owns its consistency.
    """
    conn = _RecordingConnection(autocommit=autocommit, status=status)
    search_documents(conn, {})
    assert _kinds(conn.log) == ["SELECT COUNT", "SELECT"]


@pytest.mark.parametrize("fail_on", ["COUNT(*)", "LIMIT"])
def test_search_rolls_the_snapshot_back_when_a_statement_fails(fail_on):
    """An error inside the snapshot ends it with ROLLBACK and propagates unchanged."""
    conn = _RecordingConnection(autocommit=True, fail_on=fail_on)
    with pytest.raises(RuntimeError, match="boom"):
        search_documents(conn, {})
    assert conn.log[0].startswith("BEGIN") and conn.log[-1] == "ROLLBACK"
    assert "COMMIT" not in conn.log


def test_search_argument_errors_open_no_snapshot():
    """Argument checks still run before any SQL, the BEGIN included."""
    with pytest.raises(InvalidQuery):
        search_documents(_NoConnection(), {"q": "x"}, limit=0)


# --- the since grammar is self-contained --------------------------------------

@pytest.mark.parametrize("value", [
    "2026-10-01T24:00", "2026-10-01T23:60", "2026-10-01T23:59:60", "2026-10-01T30:00",
    "2026-10-01T00:00+05:60", "2026-10-01T00:00+20:00",
    "2026-10-01T00:00-09:75",
])
def test_since_regex_alone_refuses_out_of_range_fields(value):
    """Hour, minute, second and offset ranges are in the regex itself, not left
    to whatever a given Python's fromisoformat normalises or refuses."""
    from vault_query import health

    assert health._SINCE.fullmatch(value) is None


# --- NUL characters are InvalidQuery, not a driver ValueError -----------------

NUL = "a\x00b"


@pytest.mark.parametrize("call", [
    lambda c: search_documents(c, {"q": NUL}),
    lambda c: search_documents(c, {"source_code": NUL}),
    lambda c: search_documents(c, {"corpus": NUL}),
    lambda c: search_documents(c, {"doc_type": "\x00"}),
    lambda c: build_where_clause({"language": NUL}),
    lambda c: runs_status(c, corpus=NUL),
    lambda c: runs_status(c, source_code=NUL),
    lambda c: recent_runs(c, corpus=NUL),
    lambda c: open_discovery_errors(c, source_code=NUL),
    lambda c: source_health(c, corpus=NUL),
    lambda c: corpus_stats(c, "year", corpus=NUL),
    lambda c: get_document(c, NUL),
], ids=["q", "source_code", "corpus", "doc_type", "where_language", "runs_corpus",
        "runs_source", "recent_corpus", "errors_source", "health_corpus",
        "stats_corpus", "get_document"])
def test_a_nul_character_is_refused_before_sql(call):
    """psycopg2 cannot send a NUL in a string (ValueError, an API 500): the
    value is refused as InvalidQuery first, naming the argument."""
    with pytest.raises(InvalidQuery, match="NUL"):
        call(_NoConnection())


@pytest.mark.parametrize("value", ["\ud800", "a\udfffb", "\udc80"])
@pytest.mark.parametrize("call", [
    lambda c, v: search_documents(c, {"q": v}),
    lambda c, v: search_documents(c, {"source_code": v}),
    lambda c, v: runs_status(c, corpus=v),
    lambda c, v: get_document(c, v),
], ids=["q", "source_code", "runs_corpus", "get_document"])
def test_a_lone_surrogate_is_refused_before_sql(call, value):
    """A str that cannot be encoded as UTF-8 (a lone surrogate, which a JSON
    caller can send) makes psycopg2 raise UnicodeEncodeError: refused as
    InvalidQuery first."""
    with pytest.raises(InvalidQuery, match="UTF-8"):
        call(_NoConnection(), value)
