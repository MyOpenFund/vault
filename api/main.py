import os
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from db import get_conn, build_where_clause, SORTABLE_FIELDS
from models import Document, DocumentList, StatsSummary, CountItem

RAW_DATA_DIR = Path(os.environ.get("RAW_DATA_DIR", "/data/raw")).resolve()

app = FastAPI(
    title="MyOpenFund vault API",
    description="API for browsing the document corpus (metadata + download).",
    version="0.1.0",
)


def resolve_raw_relative(local_path: str) -> Optional[PurePosixPath]:
    """Resolve the path relative to raw/ from a manifest local_path.

    Takes the components after the first exact "raw" path component (not a
    substring: ".../rawdata/..." does not match). Relies on the corpus
    builder's convention that local_path looks like "data/raw/<...>".
    Returns None when there is no "raw" component, nothing after it, or a
    ".." traversal component in the remainder.
    """
    parts = PurePosixPath(local_path).parts
    if "raw" not in parts:
        return None
    remainder = parts[parts.index("raw") + 1:]
    if not remainder or ".." in remainder:
        return None
    return PurePosixPath(*remainder)


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
    q: Optional[str] = Query(None, description="Free-text search on the title"),
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
    where_sql, params = build_where_clause(filters, include_deleted=include_deleted)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM documents{where_sql}", params)
            total = cur.fetchone()["total"]

            query = (
                f"SELECT * FROM documents{where_sql} "
                f"ORDER BY {sort_by} {sort_dir} NULLS LAST "
                f"LIMIT %s OFFSET %s"
            )
            cur.execute(query, params + [limit, offset])
            rows = cur.fetchall()

    return DocumentList(total=total, limit=limit, offset=offset, items=rows)


@app.get("/documents/{doc_id}", response_model=Document)
def get_document(doc_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(404, f"Document {doc_id} not found")
    return row


@app.get("/documents/{doc_id}/file")
def download_document_file(doc_id: str):
    """Download the actual file (PDF...) matching the document."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT local_path, title FROM documents WHERE doc_id = %s", (doc_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(404, f"Document {doc_id} not found")
    if not row["local_path"]:
        raise HTTPException(404, "No file path recorded for this document")

    raw_relative = resolve_raw_relative(row["local_path"])
    if raw_relative is None:
        raise HTTPException(400, "Invalid file path")
    file_path = (RAW_DATA_DIR / raw_relative).resolve()

    # Security: prevent any escape from the allowed directory (path traversal)
    if RAW_DATA_DIR not in file_path.parents and file_path != RAW_DATA_DIR:
        raise HTTPException(400, "Invalid file path")

    if not file_path.is_file():
        raise HTTPException(404, f"File not found on disk: {raw_relative}")

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.get("/stats/summary", response_model=StatsSummary)
def stats_summary():
    """Aggregate figures across the whole corpus, for dashboards/quick overview."""

    def top_counts(cur, field, limit=20):
        cur.execute(
            f"SELECT {field} AS key, COUNT(*) AS count FROM documents "
            f"WHERE deleted_at IS NULL "
            f"GROUP BY {field} ORDER BY count DESC LIMIT %s",
            (limit,),
        )
        return [CountItem(**r) for r in cur.fetchall()]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM documents WHERE deleted_at IS NULL")
            total = cur.fetchone()["total"]

            by_corpus = top_counts(cur, "corpus")
            by_source_code = top_counts(cur, "source_code")
            by_doc_type = top_counts(cur, "doc_type")
            by_language = top_counts(cur, "language")
            by_year = top_counts(cur, "year", limit=200)
            by_provenance = top_counts(cur, "provenance")

    return StatsSummary(
        total_documents=total,
        by_corpus=by_corpus,
        by_source_code=by_source_code,
        by_doc_type=by_doc_type,
        by_language=by_language,
        by_year=by_year,
        by_provenance=by_provenance,
    )
