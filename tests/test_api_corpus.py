"""The API under the documents contract (plan V7), without a database.

Identity is (corpus, doc_id): `/documents/{doc_id}` answers one document with
`?corpus=`, the list of every corpus's document of that id without it.
A `doc_type` filter needs `corpus` (vault-query's DocTypeNeedsCorpus -> 400,
its message passed through). Files are served from `CORPUS_ROOTS`, one root
per corpus, read at startup.
"""
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import db
import files
import main

CB = {"id": 1, "doc_id": "d1", "corpus": "central-bank", "title": "cb",
      "local_path": "raw/us/a.pdf"}
CO = {"id": 2, "doc_id": "d1", "corpus": "company", "title": "co",
      "local_path": "raw/0000320193/A1/2024/d1.pdf", "entity_scheme": "cik",
      "entity_value": "0000320193", "date_precision": "day", "date_kind": "publication"}


@contextmanager
def _no_db():
    yield object()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "get_conn", _no_db)
    return TestClient(main.app)  # no `with`: the startup gate does not run


@pytest.fixture
def lookups(monkeypatch):
    """Patch vault-query's get_document with a two-corpus table; record calls."""
    calls = []

    def fake(_conn, doc_id, corpus=None):
        calls.append((doc_id, corpus))
        return [r for r in (CB, CO) if r["doc_id"] == doc_id and corpus in (None, r["corpus"])]

    monkeypatch.setattr(main, "vq_get_document", fake)
    return calls


# --- /documents/{doc_id} -----------------------------------------------------------

def test_without_corpus_the_document_route_returns_every_corpus(client, lookups):
    r = client.get("/documents/d1")
    assert r.status_code == 200
    assert [(d["corpus"], d["title"]) for d in r.json()] == [
        ("central-bank", "cb"), ("company", "co")]
    assert lookups == [("d1", None)]


def test_with_corpus_the_document_route_returns_one_document(client, lookups):
    r = client.get("/documents/d1", params={"corpus": "company"})
    assert r.status_code == 200
    body = r.json()
    assert body["corpus"] == "company"
    assert (body["entity_scheme"], body["entity_value"]) == ("cik", "0000320193")
    assert (body["date_precision"], body["date_kind"]) == ("day", "publication")
    assert lookups == [("d1", "company")]


@pytest.mark.parametrize("params", [{}, {"corpus": "company"}])
def test_an_unknown_document_is_a_404(client, lookups, params):
    r = client.get("/documents/zz", params=params)
    assert r.status_code == 404
    assert "zz" in r.json()["detail"]


def test_a_known_doc_id_in_another_corpus_is_a_404(client, lookups):
    assert client.get("/documents/d1", params={"corpus": "third"}).status_code == 404


def test_a_nul_in_the_corpus_is_a_400(client, lookups):
    for path in ("/documents/d1", "/documents/d1/file"):
        r = client.get(path, params={"corpus": "co\x00"})
        assert r.status_code == 400 and "NUL" in r.json()["detail"]
    assert lookups == []


# --- doc_type needs corpus -----------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert "FROM vocabulary" in sql, "only the code listing may run"

    def fetchall(self):
        return self.rows


class _CodesConnection:
    autocommit = True

    def cursor(self, *a, **k):
        return _Cursor([
            {"corpus": "central-bank", "code": "A1", "label": "Rate decision", "declared": True},
            {"corpus": "company", "code": "A1", "label": "Annual report (10-K)", "declared": True},
        ])


@pytest.fixture
def codes_client(monkeypatch):
    @contextmanager
    def conn():
        yield _CodesConnection()

    monkeypatch.setattr(main, "get_conn", conn)
    return TestClient(main.app)


def test_a_doc_type_filter_without_corpus_is_a_400_with_the_codes(codes_client):
    r = codes_client.get("/documents", params={"doc_type": "A1"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail.startswith("doc_type needs corpus")
    assert "central-bank: A1 = Rate decision" in detail
    assert "company: A1 = Annual report (10-K)" in detail


def test_the_list_route_returns_the_contract_columns(client, monkeypatch):
    monkeypatch.setattr(main, "search_documents", lambda *a, **k: (1, [CO]))
    [item] = client.get("/documents", params={"corpus": "company", "doc_type": "A1"}).json()["items"]
    assert (item["entity_scheme"], item["entity_value"]) == ("cik", "0000320193")
    assert (item["date_precision"], item["date_kind"]) == ("day", "publication")


def test_stats_summary_counts_doc_types_per_corpus(client, monkeypatch):
    calls = []

    def fake_stats(_conn, group_by, *, corpus=None, limit=200):
        calls.append((group_by, corpus))
        if group_by == "corpus":
            return {"total": 5, "group_by": "corpus",
                    "groups": [{"key": "central-bank", "count": 3}, {"key": "company", "count": 2}]}
        if group_by == "doc_type":
            groups = {"central-bank": [{"key": "A1", "count": 3}],
                      "company": [{"key": "A1", "count": 2}]}[corpus]
            return {"total": 0, "group_by": group_by, "groups": groups}
        return {"total": 5, "group_by": group_by, "groups": []}

    monkeypatch.setattr(main, "corpus_stats", fake_stats)
    body = client.get("/stats/summary").json()
    assert body["by_doc_type"] == [
        {"corpus": "central-bank", "key": "A1", "count": 3},
        {"corpus": "company", "key": "A1", "count": 2},
    ]
    assert ("doc_type", None) not in calls


# --- /documents/{doc_id}/file ------------------------------------------------------

@pytest.fixture
def roots(tmp_path, monkeypatch):
    cb = tmp_path / "cbdata"
    (cb / "raw" / "us").mkdir(parents=True)
    (cb / "raw" / "us" / "a.pdf").write_bytes(b"%PDF cb")
    co = tmp_path / "companydata"
    (co / "raw" / "0000320193" / "A1" / "2024").mkdir(parents=True)
    (co / "raw" / "0000320193" / "A1" / "2024" / "d1.pdf").write_bytes(b"%PDF co")
    monkeypatch.setattr(main, "CORPUS_ROOTS", {"central-bank": cb, "company": co})
    return tmp_path


def test_the_file_is_read_under_its_corpus_root(client, lookups, roots):
    r = client.get("/documents/d1/file", params={"corpus": "company"})
    assert r.status_code == 200 and r.content == b"%PDF co"
    assert "d1.pdf" in r.headers["content-disposition"]
    r = client.get("/documents/d1/file", params={"corpus": "central-bank"})
    assert r.status_code == 200 and r.content == b"%PDF cb"


def test_a_doc_id_in_several_corpora_needs_corpus_for_its_file(client, lookups, roots):
    r = client.get("/documents/d1/file")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "central-bank" in detail and "company" in detail and "corpus" in detail


def test_a_doc_id_in_one_corpus_needs_no_corpus_for_its_file(client, monkeypatch, roots):
    monkeypatch.setattr(main, "vq_get_document", lambda _c, _id, corpus=None: [CB])
    r = client.get("/documents/d1/file")
    assert r.status_code == 200 and r.content == b"%PDF cb"


def test_an_unknown_document_of_a_corpus_is_a_404_naming_it(client, monkeypatch):
    monkeypatch.setattr(main, "vq_get_document", lambda _c, _id, corpus=None: [])
    r = client.get("/documents/d9/file", params={"corpus": "company"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Document d9 not found in corpus company"
    assert client.get("/documents/d9/file").json()["detail"] == "Document d9 not found"


def test_a_corpus_without_a_root_is_a_clear_404(client, lookups, monkeypatch, roots):
    monkeypatch.setattr(main, "CORPUS_ROOTS", {"central-bank": roots / "cbdata"})
    r = client.get("/documents/d1/file", params={"corpus": "company"})
    assert r.status_code == 404
    assert r.json()["detail"] == "No file root configured for corpus 'company' (CORPUS_ROOTS)"


@pytest.mark.parametrize("local_path", ["../companydata/raw/0000320193/A1/2024/d1.pdf",
                                        "/etc/passwd", "raw/../../outside.txt"])
def test_a_path_leaving_the_root_is_a_404(client, monkeypatch, roots, local_path):
    (roots / "outside.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(main, "vq_get_document",
                        lambda _c, _id, corpus=None: [{**CB, "local_path": local_path}])
    r = client.get("/documents/d1/file")
    assert r.status_code == 404
    assert r.json()["detail"] == "Invalid file path"


@pytest.mark.parametrize("local_path", ["raw/" + "a" * 300 + ".pdf", "raw/" + "a/" * 2100 + "x"])
def test_an_over_long_path_is_a_404_not_a_500(client, monkeypatch, roots, local_path):
    monkeypatch.setattr(main, "vq_get_document",
                        lambda _c, _id, corpus=None: [{**CB, "local_path": local_path}])
    r = client.get("/documents/d1/file")
    assert r.status_code == 404


def test_an_os_error_on_the_file_check_is_a_404(client, monkeypatch, roots):
    def boom(self):
        raise OSError(36, "File name too long")
    monkeypatch.setattr(files.Path, "is_file", boom)
    monkeypatch.setattr(main, "vq_get_document", lambda _c, _id, corpus=None: [CB])
    r = client.get("/documents/d1/file")
    assert r.status_code == 404
    assert r.json()["detail"] == f"File not found on disk: {CB['local_path']}"


def test_a_missing_file_is_a_404_naming_the_path(client, monkeypatch, roots):
    monkeypatch.setattr(main, "vq_get_document",
                        lambda _c, _id, corpus=None: [{**CB, "local_path": "data/raw/us/a.pdf"}])
    r = client.get("/documents/d1/file")
    assert r.status_code == 404
    assert r.json()["detail"] == "File not found on disk: data/raw/us/a.pdf"


def test_no_local_path_is_a_404(client, monkeypatch, roots):
    monkeypatch.setattr(main, "vq_get_document",
                        lambda _c, _id, corpus=None: [{**CB, "local_path": None}])
    r = client.get("/documents/d1/file")
    assert r.status_code == 404
    assert r.json()["detail"] == "No file path recorded for this document"


def test_raw_data_dir_is_gone():
    assert not hasattr(main, "RAW_DATA_DIR")
    assert not hasattr(main, "resolve_raw_relative")


# --- CORPUS_ROOTS at startup ---------------------------------------------------------

def test_startup_reads_corpus_roots(monkeypatch):
    monkeypatch.setattr(db.psycopg2, "connect", lambda *a, **k: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(main, "CORPUS_ROOTS", {})
    monkeypatch.setenv("CORPUS_ROOTS", "central-bank=/cbdata,company=/companydata")
    with TestClient(main.app):
        assert set(main.CORPUS_ROOTS) == {"central-bank", "company"}


def test_startup_warns_about_a_root_that_is_not_a_directory(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(db.psycopg2, "connect", lambda *a, **k: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(main, "CORPUS_ROOTS", {})
    (tmp_path / "cb").mkdir()
    monkeypatch.setenv("CORPUS_ROOTS", f"central-bank={tmp_path / 'cb'},company={tmp_path / 'nope'}")
    with caplog.at_level(logging.WARNING), TestClient(main.app):
        pass
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("company" in w and "not an existing directory" in w for w in warnings), warnings
    assert not any("central-bank" in w and "not an existing directory" in w for w in warnings)


def test_startup_refuses_a_malformed_corpus_roots_before_connecting(monkeypatch, caplog):
    def no_connect(*a, **k):
        raise AssertionError("CORPUS_ROOTS is checked before the database")

    monkeypatch.setattr(db.psycopg2, "connect", no_connect)
    monkeypatch.setenv("CORPUS_ROOTS", "central-bank=/cbdata,central-bank=/other")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="CORPUS_ROOTS"):
        with TestClient(main.app):
            pass
    assert "twice" in caplog.text


def test_the_api_image_copies_every_api_module():
    """The image copies the API's modules one by one: a new module (files.py)
    left out would only fail at container start (ModuleNotFoundError)."""
    from pathlib import Path

    api = Path(main.__file__).resolve().parent
    dockerfile = (api / "Dockerfile").read_text()
    copy = next(line for line in dockerfile.splitlines() if line.startswith("COPY api/main.py"))
    modules = {p.name for p in api.glob("*.py")}
    assert {w.removeprefix("api/") for w in copy.split()[1:-1]} == modules
