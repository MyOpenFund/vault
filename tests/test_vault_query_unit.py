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


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
@pytest.mark.parametrize("until", REFUSED_SINCE + REFUSED_SINCE_OBJECTS, ids=repr)
def test_health_reads_refuse_an_until_outside_the_grammar_before_sql(fn, until):
    with pytest.raises(InvalidQuery, match=r"until must be an ISO-8601 timestamp"):
        fn(_NoConnection(), until=until)


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
@pytest.mark.parametrize("until", ACCEPTED_SINCE + ACCEPTED_SINCE_OBJECTS, ids=repr)
def test_health_reads_accept_an_until_inside_the_grammar(fn, until):
    with pytest.raises(AssertionError, match="before any SQL"):
        fn(_NoConnection(), until=until)


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


def test_search_breaks_sort_ties_by_doc_id_then_corpus():
    """Identity is (corpus, doc_id): a doc_id alone no longer orders two rows
    of different corpora that share the sort value, so `corpus` follows it in
    the tie-break (otherwise a pager may repeat one row and skip the other)."""
    conn = _RecordingConnection(autocommit=True)
    search_documents(conn, {}, sort_by="date", sort_dir="desc")
    [page] = [s for s in conn.log if s.startswith("SELECT") and "COUNT(*)" not in s]
    assert "ORDER BY date desc NULLS LAST, doc_id, corpus LIMIT" in page


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


def test_run_columns_end_with_bad_content():
    """0.4.0 is additive: `bad_content` is appended, the earlier columns keep their order."""
    from vault_query.health import RUN_COLUMNS
    assert RUN_COLUMNS[-1] == "bad_content"
    assert RUN_COLUMNS[:-1] == (
        "run_id", "tool", "command", "corpus", "source_code", "started_at", "finished_at",
        "outcome", "exit_code", "docs_seen", "docs_new", "docs_failed", "fetch_errors",
        "truncated", "error_samples",
    )



# --- 0.5.0: identity (corpus, doc_id) and corpus-scoped doc_type --------------

class _ScriptedCursor:
    """Records (sql, params) and answers every fetch with the scripted rows."""

    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _ScriptedConnection:
    def __init__(self, rows=()):
        self.rows, self.log = list(rows), []
        self.autocommit = True

    def cursor(self, *a, **k):
        return _ScriptedCursor(self.log, self.rows)

    def get_transaction_status(self):
        return 0


# What the doc_type message query returns: (corpus, code, label, declared).
CODES = [
    {"corpus": "central-bank", "code": "A1", "label": "Monetary policy decision", "declared": True},
    {"corpus": "central-bank", "code": "E3", "label": "Annual report", "declared": True},
    {"corpus": "company", "code": "A1", "label": "Annual report (10-K)", "declared": True},
    {"corpus": "third", "code": "X9", "label": None, "declared": False},
]


def test_get_document_returns_every_row_of_the_doc_id_as_a_list():
    rows = [{"doc_id": "d1", "corpus": "central-bank"}, {"doc_id": "d1", "corpus": "company"}]
    conn = _ScriptedConnection(rows)
    assert get_document(conn, "d1") == rows
    (sql, params), = conn.log
    assert "doc_id = %s" in sql and "corpus = %s" not in sql
    assert "ORDER BY corpus" in sql
    assert params == ("d1",)


def test_get_document_with_corpus_filters_on_both_keys():
    conn = _ScriptedConnection([{"doc_id": "d1", "corpus": "company"}])
    assert get_document(conn, "d1", corpus="company") == [{"doc_id": "d1", "corpus": "company"}]
    (sql, params), = conn.log
    assert "doc_id = %s" in sql and "corpus = %s" in sql
    assert params == ("d1", "company")


def test_get_document_unknown_is_an_empty_list():
    assert get_document(_ScriptedConnection([]), "nope") == []


@pytest.mark.parametrize("corpus, match", [(NUL, "NUL"), ("\ud800", "UTF-8")])
def test_get_document_checks_the_corpus_before_sql(corpus, match):
    with pytest.raises(InvalidQuery, match=match):
        get_document(_NoConnection(), "d1", corpus=corpus)


def test_doc_type_needs_corpus_is_an_invalid_query():
    from vault_query import DocTypeNeedsCorpus

    assert issubclass(DocTypeNeedsCorpus, InvalidQuery)
    assert issubclass(DocTypeNeedsCorpus, ValueError)


@pytest.mark.parametrize("filters", [{"doc_type": "A1"}, {"doc_type": "A1", "source_code": "ecb"},
                                     {"doc_type": ""}])
def test_build_where_clause_refuses_doc_type_without_corpus(filters):
    from vault_query import DocTypeNeedsCorpus

    with pytest.raises(DocTypeNeedsCorpus, match=r"^doc_type needs corpus"):
        build_where_clause(filters)


def test_build_where_clause_takes_doc_type_with_corpus():
    where_sql, params = build_where_clause({"doc_type": "A1", "corpus": "company"})
    assert "doc_type = %s" in where_sql and "corpus = %s" in where_sql
    assert sorted(params) == ["A1", "company"]


def test_a_bad_value_wins_over_the_missing_corpus():
    """Values are checked first: a NUL doc_type or a bad date is still InvalidQuery
    for that value, before the corpus rule (which needs SQL to word its message)."""
    with pytest.raises(InvalidQuery, match="NUL"):
        build_where_clause({"doc_type": "\x00"})
    with pytest.raises(InvalidQuery, match="date_from"):
        search_documents(_NoConnection(), {"doc_type": "A1", "date_from": "garbage"})


def test_search_documents_doc_type_without_corpus_lists_the_codes_per_corpus():
    from vault_query import DocTypeNeedsCorpus

    conn = _ScriptedConnection(CODES)
    with pytest.raises(DocTypeNeedsCorpus) as exc:
        search_documents(conn, {"doc_type": "A1"})
    message = str(exc.value)
    assert message.startswith("doc_type needs corpus")
    assert "central-bank: A1 = Monetary policy decision, E3 = Annual report" in message
    assert "company: A1 = Annual report (10-K)" in message
    # A corpus with no vocabulary yet: the codes in use, said as such, no label.
    assert "third (no vocabulary loaded, codes in use): X9" in message
    # One read only, the code listing: no snapshot, no COUNT, no page.
    assert len(conn.log) == 1
    sql, params = conn.log[0]
    assert "FROM vocabulary" in sql and "FROM documents" in sql
    assert "LIMIT %s" in sql


def test_the_code_list_is_cut_past_max_message_codes():
    from vault_query import DocTypeNeedsCorpus
    from vault_query.vocabulary import MAX_MESSAGE_CODES

    rows = [{"corpus": "big", "code": f"C{i:04d}", "label": None, "declared": True}
            for i in range(MAX_MESSAGE_CODES + 1)]      # the SQL fetches one more than kept
    conn = _ScriptedConnection(rows)
    with pytest.raises(DocTypeNeedsCorpus) as exc:
        search_documents(conn, {"doc_type": "A1"})
    message = str(exc.value)
    assert message.endswith(" (list cut: see vault_query.list_vocabulary)")
    assert f"C{MAX_MESSAGE_CODES - 1:04d}" in message and f"C{MAX_MESSAGE_CODES:04d}" not in message
    assert conn.log[0][1] == (MAX_MESSAGE_CODES + 1,)


def test_each_label_is_cut_to_max_label_chars():
    from vault_query import DocTypeNeedsCorpus
    from vault_query.vocabulary import MAX_LABEL_CHARS, MAX_MESSAGE_CODES

    rows = [{"corpus": "big", "code": f"C{i:04d}", "label": "é" * 10_000, "declared": True}
            for i in range(MAX_MESSAGE_CODES + 1)]
    with pytest.raises(DocTypeNeedsCorpus) as exc:
        search_documents(_ScriptedConnection(rows), {"doc_type": "A1"})
    message = str(exc.value)
    assert f"C0000 = {'é' * MAX_LABEL_CHARS}…," in message
    # Bounded whatever the labels: every code with a cut label, plus the head.
    per_code = len(f"C0000 = {'é' * MAX_LABEL_CHARS}…, ".encode("utf-8"))
    assert len(message.encode("utf-8")) < MAX_MESSAGE_CODES * per_code + 1000


def test_the_codes_in_use_are_read_only_for_corpora_without_a_vocabulary():
    from vault_query.vocabulary import _CODES_SQL

    in_use = _CODES_SQL.split("UNION ALL", 1)[1]
    assert "NOT EXISTS (SELECT 1 FROM vocabulary v WHERE v.corpus = d.corpus)" in " ".join(
        in_use.split())


def test_the_message_says_so_when_no_corpus_has_codes():
    from vault_query import DocTypeNeedsCorpus

    with pytest.raises(DocTypeNeedsCorpus, match="no corpus declares or uses a doc_type yet"):
        search_documents(_ScriptedConnection([]), {"doc_type": "A1"})


def test_corpus_stats_by_doc_type_needs_corpus():
    from vault_query import DocTypeNeedsCorpus

    conn = _ScriptedConnection(CODES)
    with pytest.raises(DocTypeNeedsCorpus) as exc:
        corpus_stats(conn, "doc_type")
    assert str(exc.value).startswith("doc_type needs corpus")
    assert "company: A1 = Annual report (10-K)" in str(exc.value)


def test_corpus_stats_by_doc_type_with_corpus_runs():
    conn = _ScriptedConnection([])
    assert corpus_stats(conn, "doc_type", corpus="company")["total"] == 0
    (sql, params), = conn.log
    assert "GROUP BY doc_type" in sql and params == ["company", 200]


def test_corpus_stats_checks_arguments_before_the_doc_type_rule():
    with pytest.raises(InvalidQuery, match="limit"):
        corpus_stats(_NoConnection(), "doc_type", limit=0)


def test_list_vocabulary_reads_the_table_ordered_and_bounded():
    from vault_query import list_vocabulary

    rows = [{"corpus": "company", "kind": "doc_type", "code": "A1",
             "label": "Annual report (10-K)", "family": "A"}]
    conn = _ScriptedConnection(rows)
    assert list_vocabulary(conn) == rows
    (sql, params), = conn.log
    assert "FROM vocabulary" in sql and "ORDER BY corpus, kind, code" in sql
    assert params == [1000]


def test_list_vocabulary_filters_by_corpus_and_kind():
    from vault_query import list_vocabulary

    conn = _ScriptedConnection([])
    list_vocabulary(conn, corpus="company", kind="doc_type", limit=10)
    (sql, params), = conn.log
    assert "corpus = %s" in sql and "kind = %s" in sql
    assert params == ["company", "doc_type", 10]


@pytest.mark.parametrize("kwargs, match", [
    ({"kind": "password"}, "kind must be one of"),
    ({"corpus": NUL}, "NUL"),
    ({"limit": 0}, "limit"),
    ({"limit": True}, "limit"),
])
def test_list_vocabulary_refuses_bad_arguments_before_sql(kwargs, match):
    from vault_query import list_vocabulary

    with pytest.raises(InvalidQuery, match=match):
        list_vocabulary(_NoConnection(), **kwargs)


def test_package_version_is_0_1_0():
    from importlib.metadata import version

    assert version("myopenfund-vault-query") == "0.1.0"
