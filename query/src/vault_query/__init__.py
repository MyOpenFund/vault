"""Read-only queries over the MyOpenFund vault.

One implementation of the corpus reads, shared by the vault API and the MCP server.
Every function takes an open psycopg2 connection owned by the caller and never commits.
"""
from .documents import ALL_COLUMNS, SEARCH_COLUMNS, get_document, search_documents
from .filters import FILTERABLE_FIELDS, SORTABLE_FIELDS, InvalidQuery, build_where_clause
from .health import freshness, open_discovery_errors, runs_status, source_health
from .stats import GROUPABLE_FIELDS, corpus_stats

__all__ = [
    "ALL_COLUMNS", "FILTERABLE_FIELDS", "GROUPABLE_FIELDS", "SEARCH_COLUMNS", "SORTABLE_FIELDS",
    "InvalidQuery", "build_where_clause", "corpus_stats", "freshness", "get_document",
    "open_discovery_errors", "runs_status", "search_documents", "source_health",
]
