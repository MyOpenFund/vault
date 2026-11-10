"""`Document` exposes the OCR-policy facts (vault #6 bundled item, Spec W18).

has_text_layer / page_count are written on `documents` by the orchestrator's
probe pass; before this, pydantic silently dropped them from every response.
"""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import main
from models import Document

ROW = {"id": 1, "doc_id": "d1", "corpus": "central-bank", "title": "t"}


@contextmanager
def _no_db():
    yield object()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "get_conn", _no_db)
    return TestClient(main.app)  # no `with`: the startup gate does not run


@pytest.mark.parametrize("facts", [
    {"has_text_layer": True, "page_count": 12},
    {"has_text_layer": False, "page_count": 0},
    {"has_text_layer": None, "page_count": None},   # not probed yet
])
def test_get_document_returns_the_probe_facts(client, monkeypatch, facts):
    monkeypatch.setattr(main, "vq_get_document", lambda _c, _id: {**ROW, **facts})
    body = client.get("/documents/d1").json()
    assert {k: body[k] for k in facts} == facts


def test_list_documents_returns_the_probe_facts(client, monkeypatch):
    rows = [{**ROW, "has_text_layer": True, "page_count": 3},
            {**ROW, "id": 2, "doc_id": "d2"}]   # a row from before the columns existed
    monkeypatch.setattr(main, "search_documents", lambda *a, **k: (2, rows))
    items = client.get("/documents").json()["items"]
    assert [(i["has_text_layer"], i["page_count"]) for i in items] == [(True, 3), (None, None)]


def test_fields_are_optional_and_typed():
    assert Document(**ROW).has_text_layer is None
    assert Document(**ROW).page_count is None
    with pytest.raises(ValueError):
        Document(**ROW, page_count="many")
