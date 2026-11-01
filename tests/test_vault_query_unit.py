"""Argument checks of `vault_query.search_documents`, without a database.

`sort_by`, `sort_dir` and the column projection are interpolated into the SQL
text, so their allowlists are the injection guard; `limit` and `offset` are
bounds a caller (an API query string, an MCP tool argument) controls. Every
refusal has to happen before a cursor is opened: the connection stub below
fails the test if any SQL would run.
"""

import pytest

from vault_query import (
    InvalidQuery, corpus_stats, open_discovery_errors, recent_runs, runs_status, search_documents,
)


class _NoConnection:
    def cursor(self, *a, **k):
        raise AssertionError("argument checks must run before any SQL")


@pytest.mark.parametrize("kwargs", [
    {"sort_by": "local_path; DROP TABLE documents"},
    {"sort_dir": "sideways"},
    {"limit": 0},
    {"offset": -1},
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
])
def test_health_reads_refuse_text_bounds_below_one_before_sql(fn, kwargs):
    """The text bounds are caller-controlled too: below 1 is InvalidQuery, no SQL runs."""
    with pytest.raises(InvalidQuery):
        fn(_NoConnection(), **kwargs)
