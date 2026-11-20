"""Two levels of checks on a manifest row (contract 1.0.0).

- Refusals (validate_row on the columns, extra_refusals on `extra`):
  structure, identity, and what a database cannot store. A refused row is not
  written, and the vault skips its soft-delete sweep for that run.
- Notices (quality_notices): the row is written, the notice is counted (an
  empty title, a date whose precision is not stated, a transition key read).

check_row runs split_row and both levels on one row: the entry point for a
consumer. The lower-level functions stay public for callers that need one
level only.
"""
import datetime
import re
import unicodedata
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
    "missing_language", "missing_provenance", "bad_text", "too_long", "corpus_conflict",
    "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
    "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind", "bad_local_path",
    "bad_sha256", "entity_half", "unknown_entity_scheme", "bad_extra",
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

# Length caps (too_long). Generous for real data, small enough that one row
# cannot bloat an index entry (a btree entry holds ~2,700 bytes), a log line,
# or a filesystem call. Identifiers and paths count UTF-8 bytes (what an index
# and a filesystem see), free text and URLs characters.
MAX_ID_BYTES = 512             # doc_id, entity_value
MAX_CODE_CHARS = 32            # codes: as vocabulary.CODE_PATTERN allows ({1,32})
MAX_PATH_BYTES = 4096          # local_path, whole (Linux PATH_MAX)
MAX_PATH_SEGMENT_BYTES = 255   # local_path, each segment (Linux NAME_MAX)
MAX_TITLE_CHARS = 2000
MAX_URL_CHARS = 4096           # document_url, source_url

_CODE_COLUMNS: tuple[str, ...] = (
    "corpus", "source_code", "doc_type", "date_precision", "date_kind", "language",
    "provenance", "entity_scheme",
)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def is_too_long(name: str, value: str) -> bool:
    """Whether text `value` of column `name` is past its cap (see MAX_*)."""
    if name in ("doc_id", "entity_value"):
        return _utf8_len(value) > MAX_ID_BYTES
    if name in _CODE_COLUMNS:
        return len(value) > MAX_CODE_CHARS
    if name == "local_path":
        return (_utf8_len(value) > MAX_PATH_BYTES
                or any(_utf8_len(s) > MAX_PATH_SEGMENT_BYTES for s in value.split("/")))
    if name == "title":
        return len(value) > MAX_TITLE_CHARS
    if name in ("document_url", "source_url"):
        return len(value) > MAX_URL_CHARS
    return False


_LANGUAGE = re.compile(r"[a-z]{2}")                     # ISO 639-1
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")        # file:, https:, C: (a drive)
_SURROGATE = re.compile("[\ud800-\udfff]")
# Control (C0, C1 incl. U+0085), format (U+200B..U+200F, U+FEFF, ...) and
# line/paragraph separators (U+2028/2029): invisible or line-breaking in a path.
_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})


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
    """A non-blank string without NUL and without a lone surrogate (json.loads
    accepts an escaped one; a UTF-8 database cannot store it)."""
    return (isinstance(value, str) and bool(value.strip()) and "\x00" not in value
            and not _SURROGATE.search(value))


def _parse_day(value: object) -> datetime.date | None:
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def is_bad_local_path(path: str, *, transition: bool = True) -> bool:
    """Not a plain POSIX path relative to the corpus data root. Without the
    transition, a leading `data/` (no longer stripped) is refused too."""
    if "\\" in path or _SCHEME.match(path) \
            or any(unicodedata.category(c) in _INVISIBLE for c in path):
        return True
    if not transition and path.startswith(TRANSITION_LOCAL_PATH_PREFIX):
        return True
    segments = path.split("/")       # "" segments: leading "/", "//", trailing "/"
    return (any(s in ("", ".", "..") or s != s.strip() for s in segments)
            or segments[0].startswith("~"))


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
    if ok("doc_id") and columns["doc_id"] != columns["doc_id"].strip():
        refuse("bad_text", "doc_id")             # an id is compared byte for byte
    for name in TEXT_COLUMNS:
        if ok(name) and is_too_long(name, columns[name]):
            refuse("too_long", name)

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

    if ok("local_path") and is_bad_local_path(columns["local_path"], transition=transition):
        refuse("bad_local_path", "local_path")

    sha = columns.get("sha256")
    if sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
        refuse("bad_sha256", "sha256")

    scheme, value = columns.get("entity_scheme"), columns.get("entity_value")
    if (scheme is None) != (value is None):
        refuse("entity_half", "entity_value" if value is None else "entity_scheme")
    elif vocabulary is not None and ok("entity_scheme") \
            and columns["entity_scheme"] not in vocabulary.entity_schemes:
        refuse("unknown_entity_scheme", "entity_scheme")

    return refusals


def _storable_text(text: str) -> bool:
    return "\x00" not in text and not _SURROGATE.search(text)


def _storable(value: object) -> bool:
    """Whether a JSON database (Postgres JSONB, UTF-8) can hold `value`, a
    json.loads result: no NUL and no lone surrogate in any string or key, no
    NaN or infinity, at any depth. Iterative: no recursion limit to hit."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if not _storable_text(item):
                return False
        elif isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                return False
        elif isinstance(item, Mapping):
            for key, inner in item.items():
                if not (isinstance(key, str) and _storable_text(key)):
                    return False
                stack.append(inner)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return True


def extra_refusals(extra: Mapping) -> list[Refusal]:
    """One bad_extra refusal per top-level `extra` key whose key or value a
    JSON database cannot store (see _storable), in row order ([] = fine).

    `field` is the top-level key as written (it may itself be the unstorable
    part: a consumer storing it shows its repr), `value` its value.
    """
    return [Refusal("bad_extra", key, value) for key, value in extra.items()
            if not (isinstance(key, str) and _storable_text(key) and _storable(value))]


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
        notices.append(Notice("legacy_data_prefix", "local_path", path))   # both modes

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
    """split_row, validate_row, extra_refusals and quality_notices on one
    manifest row: every refusal of the row, columns first, then `extra`."""
    columns, extra = split_row(row, transition=transition)
    return CheckedRow(
        columns=columns,
        extra=extra,
        refusals=tuple(validate_row(columns, vocabulary, expected_corpus=expected_corpus,
                                    transition=transition) + extra_refusals(extra)),
        notices=tuple(quality_notices(columns, row=row, transition=transition)),
    )
