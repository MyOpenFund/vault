"""Argument checks of `vault_query.search_documents`, without a database.

`sort_by`, `sort_dir` and the column projection are interpolated into the SQL
text, so their allowlists are the injection guard; `limit` and `offset` are
bounds a caller (an API query string, an MCP tool argument) controls. Every
refusal has to happen before a cursor is opened: the connection stub below
fails the test if any SQL would run.
"""

import pytest

from vault_query import InvalidQuery, search_documents


class _NoConnection:
    def cursor(self, *a, **k):
        raise AssertionError("argument checks must run before any SQL")


@pytest.mark.parametrize("kwargs", [
    {"sort_by": "local_path; DROP TABLE documents"},
    {"sort_dir": "sideways"},
    {"limit": 0},
    {"offset": -1},
    {"columns": ("doc_id", "password")},
])
def test_search_documents_refuses_bad_arguments_before_sql(kwargs):
    """Each bad argument raises InvalidQuery, never reaches the database.

    InvalidQuery is what the API maps to HTTP 400 and the MCP server to an
    `invalid_argument` error; anything else (a psycopg2 error, a 500) would
    mean the value travelled into the SQL first.
    """
    with pytest.raises(InvalidQuery):
        search_documents(_NoConnection(), {}, **kwargs)
