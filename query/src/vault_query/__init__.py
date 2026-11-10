"""Read-only queries over the MyOpenFund vault.

One implementation of the corpus reads, shared by the vault API and the MCP server.
Every function takes an open psycopg2 connection owned by the caller and never commits the
caller's transaction (search_documents opens and ends its own read-only snapshot, only on an
idle autocommit connection).
Callers should pass an autocommit connection (or roll back after each call): a read on a
non-autocommit connection leaves it "idle in transaction", holding ACCESS SHARE locks that
block the vault's DDL train.
"""
from .documents import ALL_COLUMNS, SEARCH_COLUMNS, get_document, search_documents
from .filters import FILTERABLE_FIELDS, SORTABLE_FIELDS, InvalidQuery, build_where_clause
from .health import freshness, open_discovery_errors, recent_runs, runs_status, source_health
from .stats import GROUPABLE_FIELDS, corpus_stats

__all__ = [
    "ALL_COLUMNS", "FILTERABLE_FIELDS", "GROUPABLE_FIELDS", "SEARCH_COLUMNS", "SORTABLE_FIELDS",
    "InvalidQuery", "build_where_clause", "corpus_stats", "freshness", "get_document",
    "open_discovery_errors", "recent_runs", "runs_status", "search_documents", "source_health",
]
