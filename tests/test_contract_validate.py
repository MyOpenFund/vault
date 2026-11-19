"""`vault_contract.vocabulary` and `vault_contract.validate` (contract 1.0.0, spec §3).

Two levels. A refused row is never written and makes the vault skip its
soft-delete sweep (spec §4.3), so refusals are kept for structure and identity
and must be exact in both directions: every golden row a producer writes today
is accepted, and every malformed value names the one reason a run report will
count. Quality notices (an empty title, a date whose precision is not stated)
are counted but the row is written: on the real central-bank manifests
(39,663 rows) refusing them would have blocked 25,121 rows and every sweep.
The golden files ship with the package, so producers' CI checks the same cases.
"""
import dataclasses

import pytest

from vault_contract import golden
from vault_contract.fields import split_row
from vault_contract.validate import (
    NOTICES, REASONS, Notice, Refusal, quality_notices, validate_row,
)
from vault_contract.vocabulary import (
    Vocabulary, is_supported_version, load_vocabulary, validate_vocabulary,
)

VOCABULARIES = {c: load_vocabulary(golden.vocabulary(c)) for c in ("company", "central-bank")}


def _reasons(row, *, expected_corpus, vocabulary="from corpus"):
    if vocabulary == "from corpus":
        vocabulary = VOCABULARIES[expected_corpus]
    columns, _ = split_row(row)
    return sorted(r.reason for r in validate_row(columns, vocabulary,
                                                 expected_corpus=expected_corpus))


def _notices(row):
    columns, _ = split_row(row)
    return sorted(n.reason for n in quality_notices(columns, row=row))


def _golden(name):
    return dict(golden.accepted()[name]["row"])


# --- vocabulary ------------------------------------------------------------

@pytest.mark.parametrize("corpus", ["company", "central-bank"])
def test_golden_vocabularies_are_valid(corpus):
    doc = golden.vocabulary(corpus)
    assert validate_vocabulary(doc) == []
    assert doc["corpus"] == corpus


def test_load_vocabulary_builds_lookup_tables():
    voc = VOCABULARIES["company"]
    assert isinstance(voc, Vocabulary)
    assert voc.corpus == "company"
    assert voc.contract_version == "1.0.0"
    assert voc.source_codes["sec"] == "SEC EDGAR"
    assert voc.doc_types["A1"] == ("Annual report (10-K)", "A")
    assert "edgar_index" in voc.provenances
    assert voc.entity_schemes == frozenset({"cik", "lei"})
    assert VOCABULARIES["central-bank"].entity_schemes == frozenset()


def test_vocabulary_is_immutable():
    voc = VOCABULARIES["company"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        voc.corpus = "other"
    with pytest.raises(TypeError):
        voc.source_codes["xx"] = "injected"


def test_a_doc_type_family_is_optional():
    """Families are a producer's grouping; a third-party corpus may have none."""
    doc = golden.vocabulary("company")
    doc["doc_types"] = [{"code": "A1", "label": "Annual report"}]
    assert validate_vocabulary(doc) == []
    assert load_vocabulary(doc).doc_types["A1"] == ("Annual report", None)


def _broken(**changes):
    doc = golden.vocabulary("company")
    for key, value in changes.items():
        if value is ...:
            del doc[key]
        else:
            doc[key] = value
    return doc


@pytest.mark.parametrize("doc, fragment", [
    (_broken(contract="dataset"), "contract"),
    (_broken(contract_version="2.0.0"), "contract_version"),
    (_broken(contract_version="1.1.0"), "contract_version"),   # newer MINOR than this package
    (_broken(contract_version="1.0"), "contract_version"),
    (_broken(contract_version=1), "contract_version"),
    (_broken(corpus=""), "corpus"),
    (_broken(corpus="central bank"), "corpus"),
    (_broken(source_codes=...), "source_codes"),
    (_broken(source_codes=[{"code": "sec", "label": "SEC"}, {"code": "sec", "label": "again"}]),
     "duplicate"),
    (_broken(source_codes=[{"code": "s e c", "label": "SEC"}]), "code"),
    (_broken(source_codes=[{"code": "", "label": "SEC"}]), "code"),
    (_broken(source_codes=[{"code": "x" * 33, "label": "too long"}]), "code"),
    (_broken(source_codes=[{"code": "sec"}]), "label"),
    (_broken(source_codes=["sec"]), "source_codes"),
    (_broken(doc_types=[{"code": "A1", "label": "x", "family": ""}]), "family"),
    (_broken(provenances=["edgar_index", "edgar_index"]), "duplicate"),
    (_broken(provenances="edgar_index"), "provenances"),
    (_broken(entity_schemes=[None]), "entity_schemes"),
    (_broken(entity_schemes=...), "entity_schemes"),
])
def test_validate_vocabulary_reports_each_defect(doc, fragment):
    errors = validate_vocabulary(doc)
    assert errors, doc
    assert any(fragment in e for e in errors), errors
    with pytest.raises(ValueError):
        load_vocabulary(doc)


@pytest.mark.parametrize("not_a_doc", [None, [], "documents"])
def test_validate_vocabulary_refuses_something_that_is_not_an_object(not_a_doc):
    assert validate_vocabulary(not_a_doc) != []


def test_unknown_top_level_keys_are_allowed():
    """A later MINOR may add a key; an older reader must not refuse it."""
    doc = golden.vocabulary("company")
    doc["generated_at"] = "2026-11-19T00:00:00Z"
    assert validate_vocabulary(doc) == []


@pytest.mark.parametrize("version, ok", [
    ("1.0.0", True), ("1.0.7", True), ("1.1.0", False), ("2.0.0", False),
    ("0.9.0", False), ("1.0", False), ("v1.0.0", False), (None, False),
])
def test_supported_versions_are_this_major_up_to_this_minor(version, ok):
    """Spec §3.3: the vault accepts its MAJOR and any MINOR at or below its own."""
    assert is_supported_version(version) is ok


# --- validate_row: golden ---------------------------------------------------

def test_golden_has_the_reference_rows_and_the_notice_rows():
    assert sorted(golden.accepted()) == [
        "cb_empty_title", "cb_precision_not_stated", "cb_precision_without_date",
        "cb_rate_decision", "cb_rendered_html", "cb_undated", "sec_10k", "sec_form4"]


@pytest.mark.parametrize("name", sorted(golden.accepted()))
def test_every_golden_row_is_accepted_with_exactly_its_notices(name):
    case = golden.accepted()[name]
    assert set(case) <= {"description", "row", "expected_notices"}
    row = case["row"]
    assert _reasons(row, expected_corpus=row["corpus"]) == []
    assert _notices(row) == sorted(case.get("expected_notices", []))


def test_every_notice_reason_has_an_accepted_golden_row():
    covered = {n for case in golden.accepted().values() for n in case.get("expected_notices", [])}
    assert covered == set(NOTICES)


def test_the_five_reference_rows_carry_no_notice():
    for name in ("cb_rate_decision", "cb_rendered_html", "cb_undated", "sec_10k", "sec_form4"):
        assert "expected_notices" not in golden.accepted()[name]


@pytest.mark.parametrize("name", sorted(golden.accepted()))
def test_every_golden_row_is_accepted_without_a_vocabulary(name):
    row = golden.accepted()[name]["row"]
    assert _reasons(row, expected_corpus=row["corpus"], vocabulary=None) == []


@pytest.mark.parametrize("name", sorted(golden.refused()))
def test_every_refused_golden_row_gets_exactly_its_reasons(name):
    case = golden.refused()[name]
    assert set(case) == {"description", "expected_corpus", "row", "expected_reasons"}
    assert _reasons(case["row"], expected_corpus=case["expected_corpus"]) == \
        sorted(case["expected_reasons"])


def test_every_refusal_reason_has_a_refused_golden_row():
    covered = {r for case in golden.refused().values() for r in case["expected_reasons"]}
    assert covered == set(REASONS)


def test_reasons_are_the_planned_list():
    assert REASONS == (
        "missing_doc_id", "invalid_doc_id", "corpus_conflict", "bad_title",
        "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
        "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind",
        "absolute_local_path", "bad_sha256", "entity_half", "unknown_entity_scheme",
    )


def test_notices_are_the_decided_list():
    assert NOTICES == ("empty_title", "precision_not_stated", "precision_without_date")


# --- validate_row: adversarial ----------------------------------------------

@pytest.mark.parametrize("base, change, reason", [
    ("cb_rate_decision", {"date_precision": "Day"}, "bad_date_precision"),
    ("sec_10k", {"doc_type": "10-K"}, "unknown_doc_type"),
    ("cb_rendered_html", {"language": "english"}, "bad_language"),
    ("sec_10k", {"local_path": "/Users/x/raw/a.pdf"}, "absolute_local_path"),
    ("sec_10k", {"local_path": "raw/../../etc"}, "absolute_local_path"),
    ("sec_10k", {"year": 2023}, "year_mismatch"),
    ("sec_form4", {"entity_value": None}, "entity_half"),
    ("sec_form4", {"entity_scheme": None}, "entity_half"),
])
def test_adversarial_values_are_refused_for_one_named_reason(base, change, reason):
    row = _golden(base)
    row.update(change)
    assert _reasons(row, expected_corpus=row["corpus"]) == [reason]


def test_a_refusal_names_the_field_and_the_offending_value():
    row = _golden("cb_rate_decision")
    row["date_precision"] = "Day"
    columns, _ = split_row(row)
    assert validate_row(columns, VOCABULARIES["central-bank"], expected_corpus="central-bank") == [
        Refusal(reason="bad_date_precision", field="date_precision", value="Day")]


@pytest.mark.parametrize("path", [
    "/raw/a.pdf", "raw/../a.pdf", "..", "../raw/a.pdf", "raw/..", "raw\\a.pdf",
    "C:/raw/a.pdf", "c:raw/a.pdf", "//server/share/a.pdf", 5, ["raw/a.pdf"],
])
def test_local_path_must_stay_under_the_corpus_data_root(path):
    row = _golden("sec_10k")
    row["local_path"] = path
    assert _reasons(row, expected_corpus="company") == ["absolute_local_path"]


@pytest.mark.parametrize("path", ["raw/a.pdf", "raw/..a.pdf", "raw/a..pdf", "raw/./a.pdf", "a.pdf"])
def test_relative_local_paths_are_accepted(path):
    row = _golden("sec_10k")
    row["local_path"] = path
    assert _reasons(row, expected_corpus="company") == []


def test_a_legacy_data_prefixed_path_is_accepted_after_split():
    """Transition: split_row strips `data/`, so the row passes."""
    row = _golden("cb_rendered_html")
    row["local_path"] = "data/" + row["local_path"]
    assert _reasons(row, expected_corpus="central-bank") == []


@pytest.mark.parametrize("sha", ["a" * 63, "a" * 65, "g" * 64, 12345, ""])
def test_sha256_must_be_64_lowercase_hex(sha):
    row = _golden("sec_10k")
    row["sha256"] = sha
    # "" is read as absent by split_row: no hash is not a bad hash.
    assert _reasons(row, expected_corpus="company") == ([] if sha == "" else ["bad_sha256"])


@pytest.mark.parametrize("year", [None, "2024", 2024.0, True])
def test_year_must_be_the_integer_year_of_the_date(year):
    row = _golden("sec_10k")
    row["year"] = year
    assert _reasons(row, expected_corpus="company") == ["year_mismatch"]


@pytest.mark.parametrize("date", [20241101, "2024-11", "2024-11-01T00:00:00", " 2024-11-01"])
def test_date_must_be_an_iso_day(date):
    row = _golden("sec_10k")
    row["date"] = date
    assert _reasons(row, expected_corpus="company") == ["bad_date"]


@pytest.mark.parametrize("precision", ["day", "month", "year"])
def test_every_precision_is_accepted_on_a_first_day_date(precision):
    row = _golden("cb_rate_decision")
    row.update(date="2022-07-01", date_precision=precision)
    assert _reasons(row, expected_corpus="central-bank") == []


@pytest.mark.parametrize("doc_id", ["", None])
def test_an_empty_doc_id_is_missing(doc_id):
    row = _golden("sec_10k")
    row["doc_id"] = doc_id
    assert _reasons(row, expected_corpus="company") == ["missing_doc_id"]


@pytest.mark.parametrize("doc_id", [5, True, ["a"], {"a": 1}, "a\x00b"])
def test_a_non_string_doc_id_is_invalid(doc_id):
    row = _golden("sec_10k")
    row["doc_id"] = doc_id
    assert _reasons(row, expected_corpus="company") == ["invalid_doc_id"]


def test_a_row_without_corpus_is_accepted_during_the_transition():
    """Spec §4.3: the ingesting service's CORPUS applies (counted as
    corpus_missing by the ingestion, not refused here)."""
    row = _golden("cb_rendered_html")
    del row["corpus"]
    assert _reasons(row, expected_corpus="central-bank") == []


@pytest.mark.parametrize("corpus", ["", "Central-Bank", "company"])
def test_any_other_corpus_conflicts(corpus):
    row = _golden("cb_rendered_html")
    row["corpus"] = corpus
    assert _reasons(row, expected_corpus="central-bank") == ["corpus_conflict"]


@pytest.mark.parametrize("field, value", [
    ("source_code", ["ecb"]), ("doc_type", {"A1": 1}), ("provenance", None), ("source_code", None),
])
def test_unhashable_or_missing_codes_are_unknown_not_a_crash(field, value):
    row = _golden("cb_rendered_html")
    row[field] = value
    assert _reasons(row, expected_corpus="central-bank") == [f"unknown_{field}"]


def test_without_a_vocabulary_codes_are_not_checked_but_shapes_are():
    """Transition (spec §4.3): a corpus with no vocabulary.json yet."""
    row = _golden("sec_10k")
    row.update(source_code="SEC", doc_type="10-K", provenance="edgar", entity_scheme="isin",
               date_precision="Day")
    assert _reasons(row, expected_corpus="company", vocabulary=None) == ["bad_date_precision"]


def test_a_doc_type_is_judged_by_its_own_corpus_vocabulary():
    """Spec Q11: A1 exists in both corpora with different meanings; E3 likewise.
    A company code the central-bank vocabulary lacks is unknown there."""
    row = _golden("cb_rendered_html")
    row["doc_type"] = "B1"           # company: 8-K; central-bank excerpt: absent
    assert _reasons(row, expected_corpus="central-bank") == ["unknown_doc_type"]


def test_several_defects_are_all_reported():
    row = _golden("sec_10k")
    row.update(title=42, language="EN", sha256="x", date_kind="filing")
    assert _reasons(row, expected_corpus="company") == sorted(
        ["bad_title", "bad_language", "bad_sha256", "bad_date_kind"])


def test_validate_row_does_not_modify_the_columns():
    columns, _ = split_row(_golden("sec_form4"))
    before = dict(columns)
    validate_row(columns, VOCABULARIES["company"], expected_corpus="company")
    assert columns == before


# --- quality notices: the row is written, the notice counted ------------------

@pytest.mark.parametrize("title", [None, "", "   "])
def test_an_empty_title_is_a_notice_not_a_refusal(title):
    """110 real central-bank rows have title "" (sitemap discoveries)."""
    row = _golden("cb_rendered_html")
    row["title"] = title
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["empty_title"]


def test_an_absent_title_is_a_notice_too():
    row = _golden("cb_rendered_html")
    del row["title"]
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["empty_title"]


@pytest.mark.parametrize("title", [42, ["a"], {"t": "x"}, True])
def test_a_title_that_is_not_a_string_is_refused_without_a_notice(title):
    row = _golden("cb_rendered_html")
    row["title"] = title
    assert _reasons(row, expected_corpus="central-bank") == ["bad_title"]
    assert _notices(row) == []


@pytest.mark.parametrize("precision", [None, ""])
def test_a_dated_row_without_precision_is_a_notice(precision):
    """central-bank doctrine since #37: a producer that says nothing does not
    claim a day; null means "not stated" (25,004 real rows)."""
    row = _golden("cb_rate_decision")
    row["date_precision"] = precision
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["precision_not_stated"]


def test_an_absent_precision_on_a_dated_row_is_a_notice():
    row = _golden("cb_rate_decision")
    del row["date_precision"]
    assert _notices(row) == ["precision_not_stated"]


@pytest.mark.parametrize("date", [None, ""])
def test_a_precision_on_an_undated_row_is_dropped_and_noticed(date):
    row = _golden("cb_undated")
    row.update(date=date, date_precision="month")
    columns, extra = split_row(row)
    assert columns["date_precision"] is None
    assert "date_precision" not in extra
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["precision_without_date"]


def test_a_bad_precision_value_on_an_undated_row_is_dropped_not_refused():
    """Dropped before any check: an undated row has no precision to judge."""
    row = _golden("cb_undated")
    row["date_precision"] = "Day"
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["precision_without_date"]


def test_a_date_kind_on_an_undated_row_is_kept():
    row = _golden("cb_undated")
    row["date_kind"] = "publication"
    columns, _ = split_row(row)
    assert columns["date_kind"] == "publication"
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == []


def test_a_mime_type_without_local_path_is_accepted():
    """Not a spec rule; real rows can know the type before a download."""
    row = _golden("cb_rate_decision")
    row["mime_type"] = "application/pdf"
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == []


def test_a_notice_names_the_field_and_the_value():
    row = _golden("cb_undated")
    row["date_precision"] = "year"
    columns, _ = split_row(row)
    assert quality_notices(columns, row=row) == [
        Notice(reason="precision_without_date", field="date_precision", value="year")]
    row = _golden("cb_rate_decision")
    row["title"] = " "
    columns, _ = split_row(row)
    assert quality_notices(columns, row=row) == [
        Notice(reason="empty_title", field="title", value=" ")]


def test_several_notices_on_one_row():
    row = _golden("cb_rate_decision")
    row.update(title="", date_precision=None)
    assert _notices(row) == ["empty_title", "precision_not_stated"]


def test_quality_notices_does_not_modify_its_arguments():
    row = _golden("cb_undated")
    row["date_precision"] = "year"
    columns, _ = split_row(row)
    before_row, before_columns = dict(row), dict(columns)
    quality_notices(columns, row=row)
    assert (row, columns) == (before_row, before_columns)
