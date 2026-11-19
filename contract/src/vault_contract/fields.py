"""The common fields of a `documents` row and `split_row` (contract 1.0.0).

Every manifest line is one JSON object. The keys named in COLUMNS have one
name and one meaning in every corpus and become typed columns in the vault;
every other key is corpus-specific and goes to `extra` unrefused.
"""
from collections.abc import Mapping

# The common fields, in the order of the vault's `documents` DDL.
COLUMNS: tuple[str, ...] = (
    "doc_id", "corpus", "source_code", "doc_type", "title", "date", "year",
    "date_precision", "date_kind", "language", "provenance", "document_url",
    "source_url", "mime_type", "sha256", "local_path", "entity_scheme", "entity_value",
)

# Mandatory on every row. `corpus` is listed because the contract requires it;
# during the transition the vault still accepts a row without it (the
# ingesting service's CORPUS applies), so validate_row does not refuse that.
REQUIRED: tuple[str, ...] = (
    "doc_id", "corpus", "source_code", "doc_type", "title", "language", "provenance",
)

DATE_PRECISIONS: tuple[str, ...] = ("day", "month", "year")
DATE_KINDS: tuple[str, ...] = ("publication", "meeting", "period")

# Old key -> contract column. Transition only (removed by the cleanup PR once
# every manifest is migrated): the old key fills the column when the contract
# key is absent or blank, and never lands in `extra`.
TRANSITION_ALIASES: dict[str, str] = {"bank_code": "source_code", "pdf_url": "document_url"}

# Transition only: central-bank manifests stored local_path relative to the
# repo (`data/raw/...`); the contract wants it relative to the data root.
TRANSITION_LOCAL_PATH_PREFIX = "data/"

# An optional column holding "" means "not known": it is read as absent.
# Required columns keep "" so that validation refuses it by name.
_BLANK_IS_ABSENT = frozenset(COLUMNS) - frozenset(REQUIRED)


def split_row(row: Mapping) -> tuple[dict, dict]:
    """(columns, extra) for one manifest row; never refuses, never mutates `row`.

    `columns` has every name of COLUMNS, in order (None when absent); `extra`
    holds every other key of the row except the transition aliases, values
    untouched. A `date_precision` on an undated row is dropped (pass the row
    to quality_notices to count it). See validate_row for what is accepted.
    """
    if not isinstance(row, Mapping):
        raise TypeError(f"a manifest row is a JSON object, not {type(row).__name__}")

    columns = {name: row.get(name) for name in COLUMNS}
    for old, new in TRANSITION_ALIASES.items():
        if columns[new] in (None, "") and row.get(old) not in (None, ""):
            columns[new] = row[old]
    for name in _BLANK_IS_ABSENT:
        if columns[name] == "":
            columns[name] = None

    # No date, no precision: a precision on an undated row describes nothing,
    # so it is dropped here (never refused) and quality_notices reports it.
    # date_kind is kept.
    if columns["date"] is None:
        columns["date_precision"] = None

    path = columns["local_path"]
    if isinstance(path, str) and path.startswith(TRANSITION_LOCAL_PATH_PREFIX):
        columns["local_path"] = path[len(TRANSITION_LOCAL_PATH_PREFIX):] or None

    extra = {k: v for k, v in row.items() if k not in columns and k not in TRANSITION_ALIASES}
    return columns, extra
