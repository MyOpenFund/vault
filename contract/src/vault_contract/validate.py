"""Two levels of checks on a manifest row (contract 1.0.0).

- Refusals (validate_row): structure and identity. A refused row is not
  written, and the vault skips its soft-delete sweep for that run.
- Notices (quality_notices): the row is written, the notice is counted (an
  empty title, a date whose precision is not stated, a transition key read).

check_row runs split_row and both levels on one row: the entry point for a
consumer. The lower-level functions stay public for callers that need one
level only.
"""
import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .fields import (
    DATE_DEPENDENT, DATE_KINDS, DATE_PRECISIONS, REFUSED_WHEN_ABSENT, TRANSITION_ALIASES,
    TRANSITION_LOCAL_PATH_PREFIX, alias_used, is_blank, split_row,
)
from .vocabulary import Vocabulary

# Every reason validate_row can give, in check order. Run reports count
# refused rows per reason: these strings are part of the contract.
REASONS: tuple[str, ...] = (
    "missing_doc_id", "missing_corpus", "missing_source_code", "missing_doc_type",
    "missing_language", "missing_provenance", "bad_text", "corpus_conflict",
    "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
    "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind", "bad_local_path",
    "bad_sha256", "entity_half", "unknown_entity_scheme",
)

# Every reason quality_notices can give, in check order. Counted by run
# reports too; the row is written all the same.
NOTICES: tuple[str, ...] = (
    "corpus_missing", "legacy_alias", "legacy_data_prefix", "empty_title",
    "precision_not_stated", "date_not_period_start", "precision_without_date",
    "kind_without_date", "mime_type_missing",
)

# Columns holding text: when set, each must be a non-blank string without NUL
# (Postgres TEXT cannot store NUL; a number would lose its spelling, e.g. the
# leading zeros of a CIK). `date` has its own check (bad_date).
TEXT_COLUMNS: tuple[str, ...] = (
    "doc_id", "corpus", "source_code", "doc_type", "title", "date_precision", "date_kind",
    "language", "provenance", "document_url", "source_url", "mime_type", "local_path",
    "entity_scheme", "entity_value",
)

_LANGUAGE = re.compile(r"[a-z]{2}")                     # ISO 639-1
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")        # file:, https:, C: (a drive)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Refusal:
    reason: str      # one of REASONS
    field: str       # the column at fault
    value: object    # its value, as split_row gave it


@dataclass(frozen=True)
class Notice:
    reason: str      # one of NOTICES
    field: str       # the column (or, for legacy_alias, the old key) concerned
    value: object    # its value in the row as written by the producer


@dataclass(frozen=True)
class CheckedRow:
    columns: dict
    extra: dict
    refusals: tuple[Refusal, ...]
    notices: tuple[Notice, ...]

    @property
    def accepted(self) -> bool:
        return not self.refusals


def is_text(value: object) -> bool:
    """A non-blank string without NUL."""
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _parse_day(value: object) -> datetime.date | None:
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def is_bad_local_path(path: str) -> bool:
    """Not a plain POSIX path relative to the corpus data root."""
    if path != path.strip() or _CONTROL.search(path) or "\\" in path or _SCHEME.match(path):
        return True
    segments = path.split("/")       # "" segments: leading "/", "//", trailing "/"
    return any(s in ("", ".", "..") for s in segments) or segments[0].startswith("~")


def validate_row(columns: Mapping, vocabulary: Vocabulary | None, *,
                 expected_corpus: str, transition: bool = True) -> list[Refusal]:
    """Every refusal of one split row ([] = accepted).

    `vocabulary` None (the corpus has no vocabulary file yet, transition)
    skips the membership checks (source_code, doc_type, provenance,
    entity_scheme); every presence and shape check is kept. A vocabulary of
    another corpus is a caller error: ValueError. With `transition`, a row
    without `corpus` is accepted (quality_notices says corpus_missing).
    A field refused for presence or type is not judged further.
    """
    if vocabulary is not None and vocabulary.corpus != expected_corpus:
        raise ValueError(f"vocabulary of corpus {vocabulary.corpus!r} cannot check "
                         f"rows of corpus {expected_corpus!r}")
    refusals: list[Refusal] = []
    failed: set[str] = set()

    def refuse(reason: str, field: str) -> None:
        refusals.append(Refusal(reason, field, columns.get(field)))
        failed.add(field)

    def ok(field: str) -> bool:
        return field not in failed and columns.get(field) is not None

    for name in REFUSED_WHEN_ABSENT:
        if columns.get(name) in (None, ""):
            if name == "corpus" and transition:
                failed.add(name)                # absent: tolerated, noticed
            else:
                refuse(f"missing_{name}", name)
    for name in TEXT_COLUMNS:
        if ok(name) and not is_text(columns[name]):
            refuse("bad_text", name)

    if ok("corpus") and columns["corpus"] != expected_corpus:
        refuse("corpus_conflict", "corpus")

    if vocabulary is not None:
        for name, codes in (("source_code", vocabulary.source_codes),
                            ("doc_type", vocabulary.doc_types),
                            ("provenance", vocabulary.provenances)):
            if ok(name) and columns[name] not in codes:
                refuse(f"unknown_{name}", name)

    if ok("language") and not _LANGUAGE.fullmatch(columns["language"]):
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

    if ok("date_precision") and columns["date_precision"] not in DATE_PRECISIONS:
        refuse("bad_date_precision", "date_precision")
    if ok("date_kind") and columns["date_kind"] not in DATE_KINDS:
        refuse("bad_date_kind", "date_kind")

    if ok("local_path") and is_bad_local_path(columns["local_path"]):
        refuse("bad_local_path", "local_path")

    sha = columns.get("sha256")
    if sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
        refuse("bad_sha256", "sha256")

    scheme, value = columns.get("entity_scheme"), columns.get("entity_value")
    if (scheme is None) != (value is None):
        refuse("entity_half", "entity_scheme" if value is None else "entity_value")
    elif vocabulary is not None and ok("entity_scheme") \
            and columns["entity_scheme"] not in vocabulary.entity_schemes:
        refuse("unknown_entity_scheme", "entity_scheme")

    return refusals


def _not_period_start(day: datetime.date, precision: object) -> bool:
    return (precision == "month" and day.day != 1) or \
        (precision == "year" and (day.month, day.day) != (1, 1))


def quality_notices(columns: Mapping, *, row: Mapping, transition: bool = True) -> list[Notice]:
    """Every quality notice of one row ([] = nothing to note); never a refusal.

    `row` is the manifest row `columns` was split from, with the same
    `transition`: what split_row mapped or dropped (an alias, the `data/`
    prefix, the precision and kind of an undated row) only shows there. It is
    a required keyword so that no caller loses those notices silently.
    """
    notices: list[Notice] = []

    if transition:
        if columns.get("corpus") in (None, ""):
            notices.append(Notice("corpus_missing", "corpus", row.get("corpus")))
        for alias in TRANSITION_ALIASES:
            if alias_used(row, alias):
                notices.append(Notice("legacy_alias", alias, row[alias]))
        path = row.get("local_path")
        if isinstance(path, str) and path.startswith(TRANSITION_LOCAL_PATH_PREFIX):
            notices.append(Notice("legacy_data_prefix", "local_path", path))

    if columns.get("title") is None:
        notices.append(Notice("empty_title", "title", row.get("title")))

    day = _parse_day(columns.get("date"))
    precision = columns.get("date_precision")
    if columns.get("date") is not None and precision is None:
        notices.append(Notice("precision_not_stated", "date_precision", row.get("date_precision")))
    if day is not None and _not_period_start(day, precision):
        notices.append(Notice("date_not_period_start", "date", columns["date"]))

    if columns.get("date") is None:
        for name, reason in zip(DATE_DEPENDENT, ("precision_without_date", "kind_without_date")):
            if not is_blank(row.get(name)):
                notices.append(Notice(reason, name, row[name]))

    if columns.get("local_path") is not None and columns.get("mime_type") is None:
        notices.append(Notice("mime_type_missing", "mime_type", row.get("mime_type")))

    return notices


def check_row(row: Mapping, vocabulary: Vocabulary | None, *, expected_corpus: str,
              transition: bool = True) -> CheckedRow:
    """split_row, validate_row and quality_notices on one manifest row."""
    columns, extra = split_row(row, transition=transition)
    return CheckedRow(
        columns=columns,
        extra=extra,
        refusals=tuple(validate_row(columns, vocabulary, expected_corpus=expected_corpus,
                                    transition=transition)),
        notices=tuple(quality_notices(columns, row=row, transition=transition)),
    )
