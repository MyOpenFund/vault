"""Two levels of checks on a split manifest row (contract 1.0.0).

- validate_row -> refusals: structure and identity. A refused row is not
  written (and the vault skips its soft-delete sweep for that run).
- quality_notices -> notices: the row is written, the notice is counted
  (an empty title, a date whose precision is not stated).

Both take the columns of split_row (blank optional values already None,
transition aliases, the `data/` prefix and an undated row's precision already
handled) and return their findings in a fixed check order. Extras are never
judged.
"""
import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .fields import DATE_KINDS, DATE_PRECISIONS
from .vocabulary import Vocabulary

# Every reason validate_row can give, in check order. A run report counts
# refused rows per reason, so these strings are part of the contract.
REASONS: tuple[str, ...] = (
    "missing_doc_id", "invalid_doc_id", "corpus_conflict", "bad_title",
    "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
    "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind",
    "absolute_local_path", "bad_sha256", "entity_half", "unknown_entity_scheme",
)

# Every reason quality_notices can give, in check order. Also counted by the
# run report; the row is written all the same.
NOTICES: tuple[str, ...] = ("empty_title", "precision_not_stated", "precision_without_date")

_LANGUAGE = re.compile(r"[a-z]{2}")                     # ISO 639-1
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DRIVE = re.compile(r"[A-Za-z]:")


@dataclass(frozen=True)
class Refusal:
    reason: str      # one of REASONS
    field: str       # the column at fault
    value: object    # its value, as split_row gave it


@dataclass(frozen=True)
class Notice:
    reason: str      # one of NOTICES
    field: str       # the column concerned
    value: object    # its value (for precision_without_date: the dropped one)


def _parse_day(value: object) -> datetime.date | None:
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _escapes_root(path: object) -> bool:
    """Not a POSIX path relative to the corpus data root: absolute, a drive
    letter, a backslash, or a `..` segment (which could climb out of it)."""
    if not isinstance(path, str):
        return True
    return (path.startswith("/") or "\\" in path or _DRIVE.match(path) is not None
            or ".." in path.split("/"))


def _known(value: object, codes) -> bool:
    return isinstance(value, str) and value in codes


def validate_row(columns: Mapping, vocabulary: Vocabulary | None, *,
                 expected_corpus: str) -> list[Refusal]:
    """Every refusal of one row ([] = accepted).

    `vocabulary` None (the corpus has no vocabulary file yet, transition) skips
    the code checks (source_code, doc_type, provenance, entity_scheme) and
    keeps every shape check. A row without `corpus` is accepted (transition:
    the ingesting service's corpus applies); one naming another corpus is not.
    """
    refusals: list[Refusal] = []

    def refuse(reason: str, field: str) -> None:
        refusals.append(Refusal(reason, field, columns.get(field)))

    doc_id = columns.get("doc_id")
    if doc_id is None or doc_id == "":
        refuse("missing_doc_id", "doc_id")
    elif not isinstance(doc_id, str) or "\x00" in doc_id:
        refuse("invalid_doc_id", "doc_id")

    corpus = columns.get("corpus")
    if corpus is not None and corpus != expected_corpus:
        refuse("corpus_conflict", "corpus")

    title = columns.get("title")
    if title is not None and not isinstance(title, str):
        refuse("bad_title", "title")

    if vocabulary is not None:
        if not _known(columns.get("source_code"), vocabulary.source_codes):
            refuse("unknown_source_code", "source_code")
        if not _known(columns.get("doc_type"), vocabulary.doc_types):
            refuse("unknown_doc_type", "doc_type")
        if not _known(columns.get("provenance"), vocabulary.provenances):
            refuse("unknown_provenance", "provenance")

    language = columns.get("language")
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        refuse("bad_language", "language")

    raw_date = columns.get("date")
    day = _parse_day(raw_date)
    if raw_date is not None and day is None:
        refuse("bad_date", "date")
    year = columns.get("year")
    if day is not None:
        if type(year) is not int or year != day.year:
            refuse("year_mismatch", "year")
    elif raw_date is None and year is not None:
        refuse("year_mismatch", "year")

    precision = columns.get("date_precision")
    if precision is not None and precision not in DATE_PRECISIONS:
        refuse("bad_date_precision", "date_precision")

    kind = columns.get("date_kind")
    if kind is not None and kind not in DATE_KINDS:
        refuse("bad_date_kind", "date_kind")

    local_path = columns.get("local_path")
    if local_path is not None and _escapes_root(local_path):
        refuse("absolute_local_path", "local_path")

    sha = columns.get("sha256")
    if sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
        refuse("bad_sha256", "sha256")

    scheme, value = columns.get("entity_scheme"), columns.get("entity_value")
    if (scheme is None) != (value is None):
        refuse("entity_half", "entity_scheme" if value is None else "entity_value")
    elif scheme is not None and vocabulary is not None \
            and not _known(scheme, vocabulary.entity_schemes):
        refuse("unknown_entity_scheme", "entity_scheme")

    return refusals


def quality_notices(columns: Mapping, *, row: Mapping) -> list[Notice]:
    """Every quality notice of one row ([] = nothing to note); never a refusal.

    `row` is the original manifest row `columns` was split from: split_row
    drops the precision of an undated row, so only the row still shows it.
    It is a required keyword on purpose, so that no caller misses that notice
    silently.
    """
    notices: list[Notice] = []

    title = columns.get("title")
    if title is None or (isinstance(title, str) and not title.strip()):
        notices.append(Notice("empty_title", "title", title))

    if columns.get("date") is not None and columns.get("date_precision") is None:
        notices.append(Notice("precision_not_stated", "date_precision", None))

    dropped = row.get("date_precision")
    if columns.get("date") is None and dropped not in (None, ""):
        notices.append(Notice("precision_without_date", "date_precision", dropped))

    return notices
