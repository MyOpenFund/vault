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


def test_a_negative_year_is_a_400(client):
    """FastAPI accepts -1 as an int; only the vault-query check refuses it."""
    r = client.get("/documents", params={"year": -1})
    assert r.status_code == 400
    assert "year" in r.json()["detail"]


def test_a_valid_date_passes_the_check_and_reaches_sql(client):
    """The positive path: a real YYYY-MM-DD goes past validation to the (stub) database."""
    with pytest.raises(AssertionError, match="before any SQL"):
        client.get("/documents", params={"date_from": "2010-01-13"})


@pytest.mark.parametrize("params", [
    {"q": "a\x00b"},
    {"source_code": "\x00"},
    {"corpus": "central-bank\x00"},
])
def test_a_nul_character_in_a_filter_is_a_400(client, params):
    """psycopg2 cannot send a NUL: refused as a 400, never a driver ValueError (500)."""
    r = client.get("/documents", params=params)
    assert r.status_code == 400
    assert "NUL" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/documents/a%00b", "/documents/a%00b/file"])
def test_a_nul_character_in_a_doc_id_is_a_400(client, path):
    r = client.get(path)
    assert r.status_code == 400
    assert "NUL" in r.json()["detail"]
