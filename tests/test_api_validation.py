"""`/documents` maps a bad filter value to a 400, without a database.

`year` is typed `int` by FastAPI (a 422 before the handler); `date_from` /
`date_to` are plain strings, so the vault-query check is what turns a
non-date into InvalidQuery -> 400 instead of a Postgres DataError -> 500. The
connection stub fails the test if any SQL would run.
"""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import main


class _NoConnection:
    def cursor(self, *a, **k):
        raise AssertionError("filter values must be checked before any SQL")


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def no_db():
        yield _NoConnection()

    monkeypatch.setattr(main, "get_conn", no_db)
    return TestClient(main.app)  # no `with`: the startup gate does not run


@pytest.mark.parametrize("param", ["date_from", "date_to"])
@pytest.mark.parametrize("value", ["garbage", "13/01/2010"])
def test_a_non_iso_date_filter_is_a_400(client, param, value):
    r = client.get("/documents", params={param: value})
    assert r.status_code == 400
    assert r.json()["detail"] == f"{param} must be an ISO date (YYYY-MM-DD)"


def test_a_non_integer_year_is_refused_by_fastapi(client):
    assert client.get("/documents", params={"year": "abc"}).status_code == 422
