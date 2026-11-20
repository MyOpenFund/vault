"""The common fields of a `documents` row and `split_row` (contract 1.1.0).

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

# Absent (None or "") -> the row is refused (missing_<field>). `corpus` is
# listed because the contract requires it; with transition=True its absence
# is only a notice (corpus_missing) and the caller's expected corpus applies.
REFUSED_WHEN_ABSENT: tuple[str, ...] = (
    "doc_id", "corpus", "source_code", "doc_type", "language", "provenance",
)

# Absent -> the row is written with a notice (empty_title).
EXPECTED: tuple[str, ...] = ("title",)

DATE_PRECISIONS: tuple[str, ...] = ("day", "month", "year")
DATE_KINDS: tuple[str, ...] = ("publication", "meeting", "period")

# Old key -> contract column. Transition only: with split_row(transition=True)
# (the default, kept for the whole 1.x line) the old key fills the column when
# the contract key is absent or blank and never lands in `extra`; with
# transition=False (how the vault ingests since 2026-11-20) it stays in
# `extra`, where extra_refusals refuses it (legacy_key, since 1.1.0).
TRANSITION_ALIASES: dict[str, str] = {"bank_code": "source_code", "pdf_url": "document_url"}

# Transition only: central-bank manifests stored local_path relative to the
# repo (`data/raw/...`); the contract wants it relative to the data root.
TRANSITION_LOCAL_PATH_PREFIX = "data/"

# Facts that only exist on an undated row's date: dropped with the date.
DATE_DEPENDENT: tuple[str, ...] = ("date_precision", "date_kind")

# A blank string ("" or whitespace) in these columns means "not known": it is
# read as absent. Columns refused when absent keep it, so that validation
# refuses it by name.
_BLANK_IS_ABSENT = frozenset(COLUMNS) - frozenset(REFUSED_WHEN_ABSENT)


def is_blank(value: object) -> bool:
    """None, or a string of whitespace only."""
    return value is None or (isinstance(value, str) and not value.strip())


def alias_used(row: Mapping, alias: str) -> bool:
    """Would split_row (transition=True) take this column from its old key?"""
    return is_blank(row.get(TRANSITION_ALIASES[alias])) and not is_blank(row.get(alias))


def split_row(row: Mapping, *, transition: bool = True) -> tuple[dict, dict]:
    """(columns, extra) for one manifest row; never refuses, never mutates `row`.

    `columns` has every name of COLUMNS, in order (None when absent); `extra`
    holds every other key of the row, values untouched. Blank optional values
    are None; an undated row loses its date_precision and date_kind (pass the
    row to quality_notices to count each drop). With `transition` (default
    True, kept for the whole 1.x line; the vault passes False) the old keys of
    TRANSITION_ALIASES fill their columns and leave `extra`, and a leading
    `data/` is stripped from local_path. See validate_row for what is accepted.
    """
    if not isinstance(row, Mapping):
        raise TypeError(f"a manifest row is a JSON object, not {type(row).__name__}")

    columns = {name: row.get(name) for name in COLUMNS}
    if transition:
        for old, new in TRANSITION_ALIASES.items():
            if alias_used(row, old):
                columns[new] = row[old]
    for name in _BLANK_IS_ABSENT:
        if is_blank(columns[name]):
            columns[name] = None

    if columns["date"] is None:
        for name in DATE_DEPENDENT:
            columns[name] = None

    # A bare "data/" is kept as is (nothing would be left): validation refuses it.
    path = columns["local_path"]
    if transition and isinstance(path, str) and path.startswith(TRANSITION_LOCAL_PATH_PREFIX) \
            and len(path) > len(TRANSITION_LOCAL_PATH_PREFIX):
        columns["local_path"] = path[len(TRANSITION_LOCAL_PATH_PREFIX):]

    aliases = TRANSITION_ALIASES if transition else {}
    extra = {k: v for k, v in row.items() if k not in columns and k not in aliases}
    return columns, extra
