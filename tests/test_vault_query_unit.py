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
    InvalidQuery, build_where_clause, corpus_stats, open_discovery_errors, recent_runs, runs_status, search_documents,
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


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
@pytest.mark.parametrize("since", ["yesterday", 5, "", object()])
def test_health_reads_refuse_a_non_iso_since_before_sql(fn, since):
    """`since` is None, a datetime or an ISO-8601 string; anything else is refused."""
    with pytest.raises(InvalidQuery, match="since must be an ISO-8601 timestamp"):
        fn(_NoConnection(), since=since)


@pytest.mark.parametrize("fn", [runs_status, recent_runs])
def test_health_reads_accept_a_date_since(fn):
    """A `datetime.date` passes the check (it reaches SQL as-is, read as midnight)."""
    with pytest.raises(AssertionError, match="before any SQL"):
        fn(_NoConnection(), since=datetime.date(2010, 1, 13))


@pytest.mark.parametrize("filters, message", [
    ({"year": "abc"}, "year must be an integer"),
    ({"year": True}, "year must be an integer"),
    ({"year": 2010.0}, "year must be an integer"),
    ({"year": -1}, "year must be an integer"),
    ({"date_from": "garbage"}, r"date_from must be an ISO date \(YYYY-MM-DD\)"),
    ({"date_to": "13/01/2010"}, r"date_to must be an ISO date \(YYYY-MM-DD\)"),
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
