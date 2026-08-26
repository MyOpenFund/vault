import datetime as dt
from typing import Optional, Any

from pydantic import BaseModel, ConfigDict


class Document(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    doc_id: str
    corpus: Optional[str] = None
    source_code: Optional[str] = None
    doc_type: Optional[str] = None
    title: Optional[str] = None
    pdf_url: Optional[str] = None
    source_url: Optional[str] = None
    date: Optional[dt.date] = None
    year: Optional[int] = None
    language: Optional[str] = None
    provenance: Optional[str] = None
    mime_type: Optional[str] = None
    sha256: Optional[str] = None
    local_path: Optional[str] = None
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
    last_seen_at: Optional[dt.datetime] = None
    deleted_at: Optional[dt.datetime] = None
    extra: Optional[dict[str, Any]] = None


class DocumentList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Document]


class CountItem(BaseModel):
    key: Optional[Any]
    count: int


class StatsSummary(BaseModel):
    total_documents: int
    by_corpus: list[CountItem]
    by_source_code: list[CountItem]
    by_doc_type: list[CountItem]
    by_language: list[CountItem]
    by_year: list[CountItem]
    by_provenance: list[CountItem]
