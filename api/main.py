import os
from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from vault_query import InvalidQuery, corpus_stats, get_document as vq_get_document, search_documents
from vault_query.filters import check_text

from db import get_conn, log, verify_connection, SORTABLE_FIELDS
from files import (
    ENV_NAME as CORPUS_ROOTS_ENV, is_file, missing_roots, parse_corpus_roots, resolve_under_root,
)
from models import CorpusCountItem, Document, DocumentList, StatsSummary, CountItem

# {corpus: root directory} for file downloads, read from CORPUS_ROOTS at
# startup (lifespan). A corpus absent here has no downloadable files.
CORPUS_ROOTS = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Refuse to serve a corpus this process cannot read.

    CORPUS_ROOTS is read first: a malformed value (files.parse_corpus_roots)
    stops the process before it touches the database, with the reason logged.
    Unset or empty: the API serves metadata, and every file download is a 404
    naming the variable.

    The API connects as `vault_readonly`, a role the ingestion service's DDL
    train creates -- so on a brand-new cluster it does not exist yet and every
    connection fails. Crashing here is the point: uvicorn exits non-zero, the
    container restarts, and its logs say what to run. Serving 500s behind a
    green /health would hide exactly the same problem.
    """
    global CORPUS_ROOTS
    try:
        CORPUS_ROOTS = parse_corpus_roots(os.environ.get(CORPUS_ROOTS_ENV, ""))
    except ValueError as exc:
        reason = str(exc)
    else:
        reason = None
    if reason is not None:
        log.error("vault API: invalid %s: %s", CORPUS_ROOTS_ENV, reason)
        raise RuntimeError(f"invalid {CORPUS_ROOTS_ENV}: {reason}")
    if not CORPUS_ROOTS:
        log.warning("vault API: %s is empty, file downloads are disabled", CORPUS_ROOTS_ENV)
    # Not fatal: a mount can come up after the API, and metadata is still
    # served. But until it does, every download of that corpus is a 404.
    for name, root in missing_roots(CORPUS_ROOTS).items():
        log.warning("vault API: %s root of %r, %s, is not an existing directory: its "
                    "file downloads are 404 until it is mounted", CORPUS_ROOTS_ENV, name, root)
    verify_connection()
    yield


app = FastAPI(
    title="MyOpenFund vault API",
    description="API for browsing the document corpus (metadata + download).",
    version="0.1.0",
    lifespan=lifespan,
)


def _check_text(name, value):
    try:
        check_text(name, value)
    except InvalidQuery as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/documents", response_model=DocumentList)
def list_documents(
    corpus: Optional[str] = None,
    source_code: Optional[str] = None,
    doc_type: Optional[str] = None,
    language: Optional[str] = None,
    provenance: Optional[str] = None,
    year: Optional[int] = None,
    mime_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = Query(
        None,
        description="Literal, case-insensitive substring of the title (% and _ are not wildcards)",
    ),
    sort_by: str = "date",
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_deleted: bool = Query(
        False, description="Include soft-deleted documents (gone from all manifests)"
    ),
):
    """List documents with filters, sorting and pagination."""
    if sort_by not in SORTABLE_FIELDS:
        raise HTTPException(400, f"sort_by must be one of {sorted(SORTABLE_FIELDS)}")

    filters = {
        "corpus": corpus,
        "source_code": source_code,
        "doc_type": doc_type,
        "language": language,
        "provenance": provenance,
        "year": year,
        "mime_type": mime_type,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
    }
    # columns=None: the response model needs every column (`id` included), not
    # the narrower SEARCH_COLUMNS projection meant for result lists.
    try:
        with get_conn() as conn:
            total, rows = search_documents(
                conn, filters, sort_by=sort_by, sort_dir=sort_dir, limit=limit,
                offset=offset, include_deleted=include_deleted,
            )
    except InvalidQuery as exc:
        raise HTTPException(400, str(exc)) from None

    return DocumentList(total=total, limit=limit, offset=offset, items=rows)


CORPUS_QUERY = Query(
    None, description="The document's corpus: a doc_id is unique within its corpus only")


@app.get("/documents/{doc_id}", response_model=Union[Document, list[Document]])
def get_document(doc_id: str, corpus: Optional[str] = CORPUS_QUERY):
    """One document with `corpus`; without it, the list of every corpus's
    document with this doc_id (identity is (corpus, doc_id)). 404 when none."""
    _check_text("doc_id", doc_id)
    _check_text("corpus", corpus)
    with get_conn() as conn:
        rows = vq_get_document(conn, doc_id, corpus)

    if not rows:
        where = f" in corpus {corpus}" if corpus is not None else ""
        raise HTTPException(404, f"Document {doc_id} not found{where}")
    return rows[0] if corpus is not None else rows


@app.get("/documents/{doc_id}/file")
def download_document_file(doc_id: str, corpus: Optional[str] = CORPUS_QUERY):
    """Download the document's file: its corpus root (CORPUS_ROOTS) / local_path.

    `corpus` is required when the doc_id exists in more than one corpus (400).
    """
    _check_text("doc_id", doc_id)
    _check_text("corpus", corpus)
    with get_conn() as conn:
        rows = vq_get_document(conn, doc_id, corpus)

    if not rows:
        raise HTTPException(404, f"Document {doc_id} not found")
    if len(rows) > 1:
        corpora = ", ".join(r["corpus"] for r in rows)
        raise HTTPException(
            400, f"Document {doc_id} exists in several corpora ({corpora}): pass ?corpus=")
    row = rows[0]
    if not row["local_path"]:
        raise HTTPException(404, "No file path recorded for this document")

    root = CORPUS_ROOTS.get(row["corpus"])
    if root is None:
        raise HTTPException(
            404, f"No file root configured for corpus {row['corpus']!r} ({CORPUS_ROOTS_ENV})")
    # A path leaving the root (`..`, absolute, a symlink out) is a 404 like a
    # missing file: nothing outside a corpus root is ever looked at.
    file_path = resolve_under_root(root, row["local_path"])
    if file_path is None:
        raise HTTPException(404, "Invalid file path")
    if not is_file(file_path):
        raise HTTPException(404, f"File not found on disk: {row['local_path']}")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.get("/stats/summary", response_model=StatsSummary)
def stats_summary():
    """Aggregate figures across the whole corpus, for dashboards/quick overview."""

    def counts(stats):
        return [CountItem(**g) for g in stats["groups"]]

    def top_counts(conn, field, limit=20):
        return counts(corpus_stats(conn, field, limit=limit))

    with get_conn() as conn:
        # Every corpus_stats result carries the live-document total (a window
        # total computed before the LIMIT), so the by-corpus call supplies it.
        corpus = corpus_stats(conn, "corpus", limit=20)
        total = corpus["total"]

        by_corpus = counts(corpus)
        by_source_code = top_counts(conn, "source_code")
        # A doc_type code means something only within its corpus: counted
        # per corpus (top 20 each), never summed across corpora.
        by_doc_type = [
            CorpusCountItem(corpus=c.key, **g)
            for c in by_corpus
            for g in corpus_stats(conn, "doc_type", corpus=c.key, limit=20)["groups"]
        ]
        by_language = top_counts(conn, "language")
        by_year = top_counts(conn, "year", limit=200)
        by_provenance = top_counts(conn, "provenance")

    return StatsSummary(
        total_documents=total,
        by_corpus=by_corpus,
        by_source_code=by_source_code,
        by_doc_type=by_doc_type,
        by_language=by_language,
        by_year=by_year,
        by_provenance=by_provenance,
    )
