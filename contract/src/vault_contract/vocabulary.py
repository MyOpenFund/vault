"""The corpus vocabulary file (`vocabulary.json`, contract 1.0.0, spec §3.2).

Each producer writes one at its data root, generated from its own code: the
codes its rows may use for `source_code`, `doc_type`, `provenance` and
`entity_scheme`. A `doc_type` means something only within its corpus, so it
is always judged against that corpus's vocabulary.
"""
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CODE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_SURROGATE = re.compile("[\ud800-\udfff]")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _own_version() -> tuple[int, int]:
    from . import CONTRACT_VERSION
    major, minor, _ = CONTRACT_VERSION.split(".")
    return int(major), int(minor)


def is_supported_version(version: object) -> bool:
    """True for a MAJOR.MINOR.PATCH this package reads: its own MAJOR, a MINOR
    at or below its own (spec §3.3). A newer MINOR may carry fields or values
    this package does not know, so it is refused rather than half-read."""
    if not isinstance(version, str):
        return False
    m = _VERSION.fullmatch(version)
    if not m:
        return False
    own_major, own_minor = _own_version()
    return int(m[1]) == own_major and int(m[2]) <= own_minor


@dataclass(frozen=True)
class Vocabulary:
    """A validated vocabulary; build it with load_vocabulary."""
    corpus: str
    contract_version: str
    source_codes: Mapping[str, str]                    # code -> label
    doc_types: Mapping[str, tuple[str, str | None]]    # code -> (label, family)
    provenances: frozenset[str]
    entity_schemes: frozenset[str]


def _is_code(value: object) -> bool:
    return isinstance(value, str) and CODE_PATTERN.fullmatch(value) is not None


def _check_labelled(doc: Mapping, key: str, errors: list[str], *, with_family: bool) -> None:
    entries = doc.get(key)
    if not isinstance(entries, list) or not entries:
        errors.append(f"{key}: must be a non-empty list of objects")
        return
    seen = set()
    for i, entry in enumerate(entries):
        where = f"{key}[{i}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{where}: must be an object with code and label")
            continue
        code = entry.get("code")
        if not _is_code(code):
            errors.append(f"{where}: code {code!r} must match {CODE_PATTERN.pattern}")
        elif code in seen:
            errors.append(f"{where}: duplicate code {code!r}")
        else:
            seen.add(code)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip() or _SURROGATE.search(label):
            errors.append(f"{where}: label must be a non-empty string")
        elif any(unicodedata.category(c) == "Cc" for c in label):
            # NUL included: a TEXT column cannot store it, and a newline or an
            # escape sequence breaks the messages and UIs that show labels.
            errors.append(f"{where}: label {label!r} must not contain a control character")
        if with_family and entry.get("family") is not None and not _is_code(entry["family"]):
            errors.append(f"{where}: family {entry['family']!r} must match {CODE_PATTERN.pattern}")


def _check_codes(doc: Mapping, key: str, errors: list[str], *, may_be_empty: bool) -> None:
    codes = doc.get(key)
    if not isinstance(codes, list) or not (codes or may_be_empty):
        errors.append(f"{key}: must be a {'' if may_be_empty else 'non-empty '}list of codes")
        return
    seen = set()
    for i, code in enumerate(codes):
        if not _is_code(code):
            errors.append(f"{key}[{i}]: code {code!r} must match {CODE_PATTERN.pattern}")
        elif code in seen:
            errors.append(f"{key}[{i}]: duplicate code {code!r}")
        else:
            seen.add(code)


def validate_vocabulary(doc: object) -> list[str]:
    """Every defect of a vocabulary document, as readable messages ([] = valid).

    Unknown top-level keys are allowed (a later MINOR may add some).
    """
    if not isinstance(doc, Mapping):
        return [f"vocabulary: must be a JSON object, not {type(doc).__name__}"]
    from . import CONTRACT_NAME, CONTRACT_VERSION
    errors = []
    if doc.get("contract") != CONTRACT_NAME:
        errors.append(f"contract: must be {CONTRACT_NAME!r}, got {doc.get('contract')!r}")
    if not is_supported_version(doc.get("contract_version")):
        errors.append(f"contract_version: {doc.get('contract_version')!r} is not readable by "
                      f"vault-contract {CONTRACT_VERSION} (same MAJOR, MINOR at most its own)")
    if not _is_code(doc.get("corpus")):
        errors.append(f"corpus: {doc.get('corpus')!r} must match {CODE_PATTERN.pattern}")
    _check_labelled(doc, "source_codes", errors, with_family=False)
    _check_labelled(doc, "doc_types", errors, with_family=True)
    _check_codes(doc, "provenances", errors, may_be_empty=False)
    _check_codes(doc, "entity_schemes", errors, may_be_empty=True)   # no single company
    return errors


def load_vocabulary(doc: Mapping) -> Vocabulary:
    """The Vocabulary of a vocabulary document; ValueError listing every defect
    when validate_vocabulary finds any."""
    errors = validate_vocabulary(doc)
    if errors:
        raise ValueError("invalid vocabulary: " + "; ".join(errors))
    return Vocabulary(
        corpus=doc["corpus"],
        contract_version=doc["contract_version"],
        source_codes=MappingProxyType({e["code"]: e["label"] for e in doc["source_codes"]}),
        doc_types=MappingProxyType(
            {e["code"]: (e["label"], e.get("family")) for e in doc["doc_types"]}),
        provenances=frozenset(doc["provenances"]),
        entity_schemes=frozenset(doc["entity_schemes"]),
    )
