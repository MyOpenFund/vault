"""The read API exercised end to end, over a real Postgres.

The API is the only public surface of the vault: everything downstream —
dashboards, the RAG orchestrator, humans — reads the corpus through these
routes. Its filters, its pagination and its sort allowlist are all glued to
hand-built SQL, and its download route hands out files from disk, so the
behaviours worth pinning are the ones that only show up once a request has
travelled all the way to the database and back.

The app is pointed at the throwaway Postgres by patching `db.DATABASE_URL`
(read inside `get_conn` on every call) rather than by mocking the driver.
"""

import pytest

from .conftest import make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration

# doc_id -> (source_code, date, year); dated apart so sorting is unambiguous.
SEED = [
    make_doc("us1", source="us", date="2010-01-13", year=2010),
    make_doc("us2", source="us", date="2011-02-01", year=2011),
    make_doc("us3", source="us", date="2012-03-01", year=2012),
    make_doc("fr1", source="fr", date="2010-01-13", year=2010),
]
TOMBSTONED = make_doc("de1", source="de", date="2013-01-01", year=2013)


@pytest.fixture()
def client():
    """TestClient over the real app object (needs httpx, a dev dependency)."""
    from fastapi.testclient import TestClient

    import main as api_main

    with TestClient(api_main.app) as test_client:
        yield test_client


@pytest.fixture()
def api_db(clean_db, tmp_path, monkeypatch):
    """Seed the corpus through the real ingester and wire the app to it.

    Two runs: the second drops de1 from the manifests so the soft-delete
    sweep tombstones it, giving every read route a deleted row to ignore.
    """
    import db

    manifests = tmp_path / "manifests"
    write_manifest(manifests, "corpus.jsonl", SEED + [TOMBSTONED])
    run_ingest(monkeypatch, clean_db, manifests)
    write_manifest(manifests, "corpus.jsonl", SEED)
    run_ingest(monkeypatch, clean_db, manifests)

    monkeypatch.setattr(db, "DATABASE_URL", clean_db)
    return clean_db


@pytest.fixture()
def download_db(clean_db, tmp_path, monkeypatch):
    """Corpus of engineered local_paths, plus a real RAW_DATA_DIR on disk."""
    import db
    import main as api_main

    raw_dir = tmp_path / "raw"
    (raw_dir / "us" / "C1" / "2010").mkdir(parents=True)
    (raw_dir / "us" / "C1" / "2010" / "good.pdf").write_bytes(b"%PDF-1.4 fixture\n")

    manifests = tmp_path / "manifests"
    write_manifest(
        manifests,
        "corpus.jsonl",
        [
            make_doc("good", local_path="data/raw/us/C1/2010/good.pdf"),
            make_doc("traversal", local_path="data/raw/../../etc/passwd"),
            make_doc("absolute", local_path="/etc/passwd"),
        ],
    )
    run_ingest(monkeypatch, clean_db, manifests)

    monkeypatch.setattr(db, "DATABASE_URL", clean_db)
    monkeypatch.setattr(api_main, "RAW_DATA_DIR", raw_dir.resolve())
    return clean_db


def test_list_documents_applies_filters_and_pagination(api_db, client):
    """Filter, sort and window apply together; total ignores the window.

    Callers page through the corpus by trusting `total` to describe the
    filtered set, not the page: an offset leaking into the count, or a filter
    applied to only one of the two queries, silently truncates every consumer
    that pages to the end.
    """
    response = client.get(
        "/documents",
        params={
            "source_code": "us",
            "sort_by": "date",
            "sort_dir": "asc",
            "limit": 2,
            "offset": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3  # us1, us2, us3 — not the two returned
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["doc_id"] for item in body["items"]] == ["us2", "us3"]


def test_list_documents_rejects_a_sort_field_outside_the_allowlist(api_db, client):
    """An unlisted sort_by must be refused with 400, never reach the SQL.

    `sort_by` is interpolated straight into the ORDER BY — the allowlist is
    the only thing standing between a query string and SQL injection, so it
    has to reject before the database is touched.
    """
    response = client.get("/documents", params={"sort_by": "local_path; DROP TABLE"})

    assert response.status_code == 400
    assert "sort_by" in response.json()["detail"]


def test_get_document_returns_the_stored_row(api_db, client):
    """A known doc_id returns that document's own stored metadata."""
    response = client.get("/documents/us2")

    assert response.status_code == 200
    body = response.json()
    assert body["doc_id"] == "us2"
    assert body["source_code"] == "us"
    assert body["year"] == 2011


def test_get_document_returns_404_for_an_unknown_doc_id(api_db, client):
    """An absent doc_id is a clean 404, not a 500 on an empty result set."""
    response = client.get("/documents/no-such-doc")

    assert response.status_code == 404
    assert "no-such-doc" in response.json()["detail"]


def test_stats_summary_counts_only_live_documents(api_db, client):
    """Dashboard figures must exclude soft-deleted rows.

    Soft-deleted documents stay in the table forever; counting them would
    make the dashboard claim a corpus that no longer exists on the share.
    """
    response = client.get("/stats/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["total_documents"] == 4  # de1 is tombstoned
    assert dict(
        (item["key"], item["count"]) for item in body["by_source_code"]
    ) == {"us": 3, "fr": 1}


@pytest.mark.parametrize("doc_id", ["traversal", "absolute"])
def test_download_document_file_rejects_path_traversal_through_the_endpoint(
    download_db, client, doc_id
):
    """A local_path that escapes RAW_DATA_DIR must be refused, not served.

    local_path comes from a manifest written by an upstream corpus builder,
    so it is untrusted input reaching a filesystem read. Both a relative
    escape ("data/raw/../../etc/passwd") and an absolute path outside the
    tree must stop at the guard with 400 — a 404 would mean the request was
    resolved against the filesystem and merely missed.
    """
    response = client.get(f"/documents/{doc_id}/file")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid file path"


def test_download_document_file_serves_a_file_under_the_raw_data_dir(
    download_db, client
):
    """The guard must not break the happy path: a legitimate file downloads."""
    response = client.get("/documents/good/file")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 fixture\n"
    assert "good.pdf" in response.headers["content-disposition"]
