"""`vault_contract.vocabulary` and `vault_contract.validate` (contract 1.0.0, spec §3).

Two levels. A refused row is never written and makes the vault skip its
soft-delete sweep (spec §4.3), so refusals are kept for structure and identity
and must be exact in both directions: every golden row a producer writes today
is accepted, and every malformed value names the one reason a run report will
count. Quality notices (an empty title, a date whose precision is not stated,
a transition key read) are counted but the row is written: on the real
central-bank manifests (39,663 rows) refusing them would have blocked 25,121
rows and every sweep.
The golden files ship with the package, so producers' CI checks the same cases.
"""
import dataclasses

import pytest

from vault_contract import golden
from vault_contract.fields import split_row
from vault_contract.validate import (
    MAX_CODE_CHARS, MAX_ID_BYTES, MAX_PATH_BYTES, MAX_PATH_SEGMENT_BYTES, MAX_TITLE_CHARS,
    MAX_URL_CHARS, NOTICES, REASONS, CheckedRow, Notice, Refusal, check_row, extra_refusals, quality_notices,
    validate_row,
)
from vault_contract.vocabulary import (
    Vocabulary, is_supported_version, load_vocabulary, validate_vocabulary,
)

VOCABULARIES = {c: load_vocabulary(golden.vocabulary(c)) for c in ("company", "central-bank")}


def _reasons(row, *, expected_corpus, vocabulary="from corpus", transition=True):
    if vocabulary == "from corpus":
        vocabulary = VOCABULARIES[expected_corpus]
    checked = check_row(row, vocabulary, expected_corpus=expected_corpus, transition=transition)
    return sorted(r.reason for r in checked.refusals)


def _notices(row, *, transition=True):
    columns, _ = split_row(row, transition=transition)
    return sorted(n.reason for n in quality_notices(columns, row=row, transition=transition))


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
    (_broken(source_codes=[]), "source_codes"),
    (_broken(doc_types=[]), "doc_types"),
    (_broken(provenances=[]), "provenances"),
    (_broken(source_codes=[{"code": "sec", "label": "SEC\x00EDGAR"}]), "label"),
    (_broken(doc_types=[{"code": "A1", "label": "Annual\x00report"}]), "label"),
    # Control characters (Unicode Cc): a label is shown in error messages and UIs.
    (_broken(doc_types=[{"code": "A1", "label": "Annual\nreport"}]), "control"),
    (_broken(doc_types=[{"code": "A1", "label": "Annual\treport"}]), "control"),
    (_broken(source_codes=[{"code": "sec", "label": "SEC\x1bEDGAR"}]), "control"),
    (_broken(source_codes=[{"code": "sec", "label": "SEC\x85EDGAR"}]), "control"),
    (_broken(source_codes=[{"code": "sec", "label": "SEC\ud800"}]), "label"),
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


def test_a_label_may_hold_any_printable_text():
    doc = _broken(doc_types=[{"code": "A1", "label": "Rapport annuel — 10‑K (é, 中文)"}])
    assert validate_vocabulary(doc) == []


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


# --- golden ------------------------------------------------------------------

# The clean reference rows; the other accepted rows each carry notices.
REFERENCE_ROWS = ("cb_rate_decision", "sec_10k")


def test_golden_has_the_representative_rows_only():
    assert sorted(golden.accepted()) == sorted([
        "sec_10k", "sec_form4", "cb_rate_decision", "cb_legacy_form", "cb_undated",
        "cb_rendered_html"])


def test_golden_has_exactly_one_refused_case_per_reason():
    reasons = [r for case in golden.refused().values() for r in case["expected_reasons"]]
    assert sorted(reasons) == sorted(REASONS)
    assert all(len(case["expected_reasons"]) == 1 for case in golden.refused().values())


@pytest.mark.parametrize("name", sorted(golden.accepted()))
def test_every_golden_row_is_accepted_with_exactly_its_columns_and_notices(name):
    case = golden.accepted()[name]
    assert set(case) == {
        "description", "expected_corpus", "row", "expected_columns", "expected_notices"}
    corpus = case["expected_corpus"]
    checked = check_row(case["row"], VOCABULARIES[corpus], expected_corpus=corpus)
    assert checked.refusals == ()
    assert checked.columns == case["expected_columns"]
    assert sorted(n.reason for n in checked.notices) == sorted(case.get("expected_notices", []))


@pytest.mark.parametrize("name", sorted(golden.accepted()))
def test_every_golden_row_is_accepted_without_a_vocabulary(name):
    case = golden.accepted()[name]
    assert _reasons(case["row"], expected_corpus=case["expected_corpus"], vocabulary=None) == []


def test_every_notice_reason_has_an_accepted_golden_row():
    covered = {n for case in golden.accepted().values() for n in case.get("expected_notices", [])}
    assert covered == set(NOTICES)


def test_the_reference_rows_carry_no_notice():
    for name in REFERENCE_ROWS:
        assert golden.accepted()[name]["expected_notices"] == []


@pytest.mark.parametrize("name", sorted(golden.refused()))
def test_every_refused_golden_row_gets_exactly_its_reasons(name):
    case = golden.refused()[name]
    assert set(case) - {"transition"} == {
        "description", "expected_corpus", "row", "expected_reasons"}
    assert _reasons(case["row"], expected_corpus=case["expected_corpus"],
                    transition=case.get("transition", True)) == sorted(case["expected_reasons"])


def test_every_refusal_reason_has_a_refused_golden_row():
    covered = {r for case in golden.refused().values() for r in case["expected_reasons"]}
    assert covered == set(REASONS)


def test_reasons_are_the_decided_list():
    assert REASONS == (
        "missing_doc_id", "missing_corpus", "missing_source_code", "missing_doc_type",
        "missing_language", "missing_provenance", "bad_text", "too_long", "corpus_conflict",
        "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
        "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind", "bad_local_path",
        "bad_sha256", "entity_half", "unknown_entity_scheme", "bad_extra",
    )


def test_notices_are_the_decided_list():
    assert NOTICES == (
        "corpus_missing", "legacy_alias", "legacy_data_prefix", "empty_title",
        "precision_not_stated", "date_not_period_start", "precision_without_date",
        "kind_without_date", "mime_type_missing",
    )


# --- check_row ---------------------------------------------------------------

def test_check_row_is_split_validate_and_notice_in_one_call():
    row = _golden("sec_10k")
    checked = check_row(row, VOCABULARIES["company"], expected_corpus="company")
    assert isinstance(checked, CheckedRow)
    assert (checked.columns, checked.extra) == split_row(row)
    assert checked.accepted and checked.refusals == () and checked.notices == ()


def test_check_row_reports_refusals_and_notices_together():
    row = _golden("cb_rate_decision")
    row.update(title="", date_kind="Meeting")
    checked = check_row(row, VOCABULARIES["central-bank"], expected_corpus="central-bank")
    assert not checked.accepted
    assert [r.reason for r in checked.refusals] == ["bad_date_kind"]
    assert [n.reason for n in checked.notices] == ["empty_title"]


def test_a_vocabulary_of_another_corpus_is_a_caller_error():
    columns, _ = split_row(_golden("sec_10k"))
    with pytest.raises(ValueError, match="central-bank"):
        validate_row(columns, VOCABULARIES["central-bank"], expected_corpus="company")
    with pytest.raises(ValueError):
        check_row(_golden("sec_10k"), VOCABULARIES["company"], expected_corpus="central-bank")


# --- refusals: presence and text ---------------------------------------------

@pytest.mark.parametrize("field", ["source_code", "doc_type", "provenance", "language", "doc_id"])
@pytest.mark.parametrize("value", [None, ""])
def test_an_absent_code_is_refused_even_without_a_vocabulary(field, value):
    row = _golden("cb_rendered_html")
    row[field] = value
    assert _reasons(row, expected_corpus="central-bank", vocabulary=None) == [f"missing_{field}"]


@pytest.mark.parametrize("field", ["source_code", "doc_type", "provenance", "doc_id"])
@pytest.mark.parametrize("value", ["  ", 5, ["ecb"], {"a": 1}, True, "ec\x00b"])
def test_a_code_that_is_not_text_is_bad_text_even_without_a_vocabulary(field, value):
    row = _golden("cb_rendered_html")
    row[field] = value
    assert _reasons(row, expected_corpus="central-bank", vocabulary=None) == ["bad_text"]
    assert _reasons(row, expected_corpus="central-bank") == ["bad_text"]   # not also unknown_


@pytest.mark.parametrize("field", [
    "title", "document_url", "source_url", "mime_type", "local_path", "entity_value",
    "date_precision", "date_kind", "language",
])
@pytest.mark.parametrize("value", [5, ["x"], {"x": 1}, "a\x00b"])
def test_every_set_text_column_must_be_text(field, value):
    row = _golden("sec_10k")
    row[field] = value
    columns, _ = split_row(row)
    refusals = validate_row(columns, VOCABULARIES["company"], expected_corpus="company")
    assert refusals == [Refusal("bad_text", field, value)]


def test_a_numeric_cik_is_refused():
    """320193 is not how the scheme spells 0000320193."""
    row = _golden("sec_10k")
    row["entity_value"] = 320193
    assert _reasons(row, expected_corpus="company") == ["bad_text"]


def test_a_non_text_entity_scheme_is_refused_without_a_vocabulary():
    row = _golden("sec_10k")
    row["entity_scheme"] = 7
    assert _reasons(row, expected_corpus="company", vocabulary=None) == ["bad_text"]


def test_a_refusal_names_the_field_and_the_offending_value():
    row = _golden("cb_rate_decision")
    row["date_precision"] = "Day"
    columns, _ = split_row(row)
    assert validate_row(columns, VOCABULARIES["central-bank"], expected_corpus="central-bank") == [
        Refusal(reason="bad_date_precision", field="date_precision", value="Day")]


# --- refusals: values ----------------------------------------------------------

@pytest.mark.parametrize("base, change, reason", [
    ("cb_rate_decision", {"date_precision": "Day"}, "bad_date_precision"),
    ("sec_10k", {"doc_type": "10-K"}, "unknown_doc_type"),
    ("cb_rendered_html", {"language": "english"}, "bad_language"),
    ("sec_10k", {"local_path": "/Users/x/raw/a.pdf"}, "bad_local_path"),
    ("sec_10k", {"local_path": "raw/../../etc"}, "bad_local_path"),
    ("sec_10k", {"year": 2023}, "year_mismatch"),
    ("sec_form4", {"entity_value": None}, "entity_half"),
    ("sec_form4", {"entity_scheme": None}, "entity_half"),
])
def test_adversarial_values_are_refused_for_one_named_reason(base, change, reason):
    row = _golden(base)
    row.update(change)
    assert _reasons(row, expected_corpus=row["corpus"]) == [reason]


@pytest.mark.parametrize("path", [
    "/raw/a.pdf", "raw/../a.pdf", "..", "../raw/a.pdf", "raw/..", "raw\\a.pdf",
    "C:/raw/a.pdf", "c:raw/a.pdf", "//server/share/a.pdf", "raw//a.pdf", "./a.pdf",
    "raw/./a.pdf", "raw/", "raw/a.pdf ", " raw/a.pdf", "raw/a\tb.pdf", "raw/a\nb.pdf",
    "raw/a\x7f.pdf", "~/raw/a.pdf", "~marc/raw/a.pdf", "file:///raw/a.pdf",
    "https://example.org/a.pdf", "s3:bucket/a.pdf",
])
def test_local_path_must_be_a_plain_path_under_the_data_root(path):
    row = _golden("sec_10k")
    row["local_path"] = path
    assert _reasons(row, expected_corpus="company") == ["bad_local_path"]


@pytest.mark.parametrize("path", ["raw/a.pdf", "raw/..a.pdf", "raw/a..pdf", "a.pdf",
                                  "raw/a~b.pdf", "raw/a:b.pdf", "raw/é.pdf"])
def test_plain_relative_local_paths_are_accepted(path):
    row = _golden("sec_10k")
    row["local_path"] = path
    assert _reasons(row, expected_corpus="company") == []


@pytest.mark.parametrize("sha", ["a" * 63, "a" * 65, "g" * 64, 12345])
def test_sha256_must_be_64_lowercase_hex(sha):
    row = _golden("sec_10k")
    row["sha256"] = sha
    assert _reasons(row, expected_corpus="company") == ["bad_sha256"]


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


def test_a_row_without_corpus_is_accepted_and_noticed_during_the_transition():
    row = _golden("cb_rendered_html")
    del row["corpus"]
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["corpus_missing"]


@pytest.mark.parametrize("corpus", [None, ""])
def test_a_row_without_corpus_is_refused_after_the_transition(corpus):
    row = _golden("cb_rendered_html")
    row["corpus"] = corpus
    assert _reasons(row, expected_corpus="central-bank", transition=False) == ["missing_corpus"]
    assert _notices(row, transition=False) == []


@pytest.mark.parametrize("corpus", ["Central-Bank", "company"])
def test_any_other_corpus_conflicts(corpus):
    row = _golden("cb_rendered_html")
    row["corpus"] = corpus
    assert _reasons(row, expected_corpus="central-bank") == ["corpus_conflict"]


def test_without_a_vocabulary_membership_is_not_checked_but_shapes_are():
    """Transition (spec §4.3): a corpus with no vocabulary.json yet."""
    row = _golden("sec_10k")
    row.update(source_code="SEC", doc_type="10-K", provenance="edgar", entity_scheme="isin",
               date_precision="Day")
    assert _reasons(row, expected_corpus="company", vocabulary=None) == ["bad_date_precision"]


def test_a_doc_type_is_judged_by_its_own_corpus_vocabulary():
    """Spec Q11: a company code the central-bank vocabulary lacks is unknown there."""
    row = _golden("cb_rendered_html")
    row["doc_type"] = "B1"
    assert _reasons(row, expected_corpus="central-bank") == ["unknown_doc_type"]


def test_several_defects_are_all_reported():
    row = _golden("sec_10k")
    row.update(title=42, language="EN", sha256="x", date_kind="filing")
    assert _reasons(row, expected_corpus="company") == sorted(
        ["bad_text", "bad_language", "bad_sha256", "bad_date_kind"])


def test_validate_row_does_not_modify_the_columns():
    columns, _ = split_row(_golden("sec_form4"))
    before = dict(columns)
    validate_row(columns, VOCABULARIES["company"], expected_corpus="company")
    assert columns == before


# --- notices: the row is written, the notice counted --------------------------

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
    assert _notices(row) == ["empty_title"]


@pytest.mark.parametrize("precision", [None, ""])
def test_a_dated_row_without_precision_is_a_notice(precision):
    """central-bank doctrine since #37: null means "not stated" (25,004 real rows)."""
    row = _golden("cb_rate_decision")
    row["date_precision"] = precision
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["precision_not_stated"]


@pytest.mark.parametrize("date, precision", [
    ("2022-07-21", "month"), ("2022-07-01", "year"), ("2022-02-01", "year"),
])
def test_a_month_or_year_date_off_its_period_start_is_a_notice(date, precision):
    """26 real RePEc month rows carry a day: kept, noticed."""
    row = _golden("cb_rate_decision")
    row.update(date=date, year=2022, date_precision=precision)
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["date_not_period_start"]


@pytest.mark.parametrize("date, precision", [
    ("2022-07-21", "day"), ("2022-07-01", "month"), ("2022-01-01", "year"),
])
def test_a_date_on_its_period_start_gets_no_notice(date, precision):
    row = _golden("cb_rate_decision")
    row.update(date=date, year=2022, date_precision=precision)
    assert _notices(row) == []


@pytest.mark.parametrize("date", [None, ""])
def test_precision_and_kind_on_an_undated_row_are_dropped_and_noticed(date):
    row = _golden("cb_undated")
    row.update(date=date, date_precision="month", date_kind="publication")
    columns, extra = split_row(row)
    assert (columns["date_precision"], columns["date_kind"]) == (None, None)
    assert "date_precision" not in extra and "date_kind" not in extra
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["kind_without_date", "precision_without_date"]


def test_bad_values_on_an_undated_row_are_dropped_not_refused():
    row = _golden("cb_undated")
    row.update(date_precision="Day", date_kind="Meeting")
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == ["kind_without_date", "precision_without_date"]


def test_a_file_without_mime_type_is_a_notice():
    row = _golden("sec_10k")
    row["mime_type"] = ""
    assert _reasons(row, expected_corpus="company") == []
    assert _notices(row) == ["mime_type_missing"]


def test_a_mime_type_without_local_path_is_accepted_silently():
    """Not a spec rule; real rows can know the type before a download."""
    row = _golden("cb_rate_decision")
    row["mime_type"] = "application/pdf"
    assert _reasons(row, expected_corpus="central-bank") == []
    assert _notices(row) == []


def test_transition_mappings_are_noticed_with_the_old_key_and_value():
    row = _golden("cb_rendered_html")
    row["bank_code"] = row.pop("source_code")
    row["pdf_url"] = row.pop("document_url")
    row["local_path"] = "data/" + row["local_path"]
    columns, _ = split_row(row)
    notices = quality_notices(columns, row=row)
    assert notices == [
        Notice("legacy_alias", "bank_code", "ecb"),
        Notice("legacy_alias", "pdf_url", row["pdf_url"]),
        Notice("legacy_data_prefix", "local_path", row["local_path"]),
    ]
    assert _reasons(row, expected_corpus="central-bank") == []


def test_an_alias_shadowed_by_its_contract_key_is_not_noticed():
    row = _golden("cb_rendered_html")
    row["bank_code"] = "us"
    assert _notices(row) == []


def test_after_the_transition_old_keys_are_not_mapped_and_a_data_path_is_refused():
    """transition=False: bank_code is an extra, so source_code is missing; a
    `data/` path is no longer stripped, and storing data/raw/... would break
    every reader, so it is refused, still with its legacy_data_prefix notice."""
    row = _golden("cb_rendered_html")
    row["bank_code"] = row.pop("source_code")
    row["local_path"] = "data/" + row["local_path"]
    assert _reasons(row, expected_corpus="central-bank", transition=False) == [
        "bad_local_path", "missing_source_code"]
    assert _notices(row, transition=False) == ["legacy_data_prefix"]


def test_a_notice_names_the_field_and_the_value_as_written():
    row = _golden("cb_undated")
    row["date_precision"] = "year"
    del row["date_kind"]
    columns, _ = split_row(row)
    assert quality_notices(columns, row=row) == [
        Notice(reason="precision_without_date", field="date_precision", value="year")]
    row = _golden("cb_rate_decision")
    row["title"] = " "
    columns, _ = split_row(row)
    assert quality_notices(columns, row=row) == [
        Notice(reason="empty_title", field="title", value=" ")]


def test_quality_notices_does_not_modify_its_arguments():
    row = _golden("cb_undated")
    row["date_precision"] = "year"
    columns, _ = split_row(row)
    before_row, before_columns = dict(row), dict(columns)
    quality_notices(columns, row=row)
    assert (row, columns) == (before_row, before_columns)



# --- second fix wave ------------------------------------------------------------

@pytest.mark.parametrize("doc_id", [" cf0e49e6e176a851", "cf0e49e6e176a851 ", "cf0e\n"])
def test_a_doc_id_with_surrounding_whitespace_is_bad_text(doc_id):
    row = _golden("sec_10k")
    row["doc_id"] = doc_id
    assert _reasons(row, expected_corpus="company") == ["bad_text"]


@pytest.mark.parametrize("field", [
    "doc_id", "source_code", "doc_type", "title", "date_precision", "date_kind", "language",
    "provenance", "document_url", "source_url", "mime_type", "local_path", "entity_scheme",
    "entity_value",
])
def test_a_lone_surrogate_in_any_text_column_is_bad_text(field):
    """json.loads accepts a lone surrogate escape; Postgres (UTF-8) cannot store it."""
    row = _golden("sec_10k")
    row[field] = "a\ud800b"
    columns, _ = split_row(row)
    assert validate_row(columns, VOCABULARIES["company"], expected_corpus="company") == [
        Refusal("bad_text", field, "a\ud800b")]


def test_a_lone_surrogate_in_corpus_is_bad_text():
    row = _golden("sec_10k")
    row["corpus"] = "comp\udfffany"
    assert _reasons(row, expected_corpus="company") == ["bad_text"]


@pytest.mark.parametrize("path", [
    "raw/ a.pdf", "raw /a.pdf", "raw/a.pdf ", "raw/a\u0085.pdf", "raw/a .pdf",
    "raw/a .pdf", "raw/a​.pdf", "raw/a‏.pdf", "raw/﻿a.pdf", "raw/a\x9f.pdf",
])
def test_local_path_segments_refuse_edge_whitespace_and_invisible_characters(path):
    row = _golden("sec_10k")
    row["local_path"] = path
    assert _reasons(row, expected_corpus="company") == ["bad_local_path"]


def test_an_inner_space_in_a_local_path_segment_is_accepted():
    row = _golden("sec_10k")
    row["local_path"] = "raw/annual report 2024.pdf"
    assert _reasons(row, expected_corpus="company") == []


def test_a_data_prefix_with_nothing_after_is_refused_not_silently_dropped():
    row = _golden("sec_10k")
    row["local_path"] = "data/"
    assert _reasons(row, expected_corpus="company") == ["bad_local_path"]
    assert "legacy_data_prefix" in _notices(row)


@pytest.mark.parametrize("missing, present", [("entity_value", "entity_scheme"),
                                              ("entity_scheme", "entity_value")])
def test_entity_half_names_the_missing_field(missing, present):
    row = _golden("sec_form4")
    row[missing] = None
    columns, _ = split_row(row)
    assert validate_row(columns, VOCABULARIES["company"], expected_corpus="company") == [
        Refusal("entity_half", missing, None)]


# --- bad_extra: what a JSON database cannot store in `extra` -------------------------

@pytest.mark.parametrize("extra", [
    {"note": "a\u0000b"},                       # NUL in a value
    {"deep": [{"x": ["ok", "nul\u0000"]}]},      # at any depth
    {"k\u0000": 1},                             # NUL in a key
    {"outer": {"in\u0000ner": 1}},              # NUL in a nested key
    {"surrogate": "\ud800"},                    # a lone surrogate
    {"\udfff": "x"},                            # ... in a key
    {"n": float("nan")},
    {"n": [float("inf")]},
    {"n": {"m": float("-inf")}},
])
def test_an_extra_a_database_cannot_hold_is_bad_extra(extra):
    row = dict(_golden("sec_10k"), **extra)
    checked = check_row(row, VOCABULARIES["company"], expected_corpus="company")
    key = next(iter(extra))
    (refusal,) = checked.refusals
    assert (refusal.reason, refusal.field) == ("bad_extra", key)
    assert refusal.value is extra[key]                # NaN != NaN: identity


@pytest.mark.parametrize("extra", [
    {"note": "\\u0000"},           # the six characters \u0000, not a NUL
    {"n": 1e308, "m": -0.0, "big": 10 ** 30},
    {"deep": [[[{"a": None, "b": True}]]]},
    {"emoji": "\U0001F600", "nbsp": "a\u00a0b"},
])
def test_storable_extras_are_accepted(extra):
    row = dict(_golden("sec_10k"), **extra)
    assert check_row(row, VOCABULARIES["company"], expected_corpus="company").accepted


def test_one_refusal_per_bad_top_level_key_in_row_order():
    assert [r.field for r in extra_refusals({"a": "\u0000", "ok": 1, "b": float("nan")})] == [
        "a", "b"]


def test_extra_refusals_is_iterative_not_recursive():
    deep = "x\u0000"
    for _ in range(100_000):
        deep = [deep]
    assert [r.reason for r in extra_refusals({"deep": deep})] == ["bad_extra"]


# --- too_long: length caps -------------------------------------------------------------

def test_the_caps_are_the_decided_ones():
    assert (MAX_ID_BYTES, MAX_CODE_CHARS, MAX_PATH_BYTES, MAX_PATH_SEGMENT_BYTES,
            MAX_TITLE_CHARS, MAX_URL_CHARS) == (512, 32, 4096, 255, 2000, 4096)


def _path(total_bytes):
    """A plain relative path of exactly `total_bytes` bytes, segments of 200."""
    segments, left = [], total_bytes
    while left > 200:
        segments.append("a" * 199)          # + "/"
        left -= 200
    segments.append("b" * left)
    return "/".join(segments)


CODE_FIELDS = ("source_code", "doc_type", "provenance", "corpus", "entity_scheme", "language",
               "date_kind", "date_precision")


@pytest.mark.parametrize("field, at_cap, over_cap", [
    ("doc_id", "d" * 512, "d" * 513),
    ("doc_id", "é" * 256, "é" * 256 + "d"),            # bytes, not characters
    ("entity_value", "0" * 512, "0" * 513),
    ("title", "t" * 2000, "t" * 2001),
    ("document_url", "https://x.org/" + "u" * 4082, "https://x.org/" + "u" * 4083),
    ("source_url", "https://x.org/" + "u" * 4082, "https://x.org/" + "u" * 4083),
    ("local_path", _path(4096), _path(4097)),
    ("local_path", "raw/" + "s" * 255, "raw/" + "s" * 256),
    ("local_path", "raw/" + "é" * 127 + "s", "raw/" + "é" * 128),   # 256-byte segment
] + [(f, None, "x" * 33) for f in CODE_FIELDS])
def test_a_value_past_its_cap_is_too_long(field, at_cap, over_cap):
    base = _golden("sec_form4")
    for value, refused in ((at_cap, False), (over_cap, True)):
        if value is None:
            continue
        row = dict(base, **{field: value})
        if field == "entity_value":
            row["entity_scheme"] = "cik"
        # A too-long corpus is too_long only: not judged further (no corpus_conflict).
        checked = check_row(row, None, expected_corpus=base["corpus"])
        reasons = [(r.reason, r.field) for r in checked.refusals]
        if refused:
            assert reasons == [("too_long", field)], reasons
        else:
            assert ("too_long", field) not in reasons, reasons


def test_a_too_long_field_is_not_judged_further():
    row = dict(_golden("sec_10k"), doc_type="X" * 40, language="e" * 40)
    reasons = [(r.reason, r.field) for r in check_row(
        row, VOCABULARIES["company"], expected_corpus="company").refusals]
    assert reasons == [("too_long", "doc_type"), ("too_long", "language")]


# --- the package's public names; the expected corpus must be a code ---------------------

def test_code_pattern_and_is_bad_local_path_are_exported():
    import vault_contract
    from vault_contract.validate import is_bad_local_path
    from vault_contract.vocabulary import CODE_PATTERN

    assert vault_contract.CODE_PATTERN is CODE_PATTERN
    assert vault_contract.is_bad_local_path is is_bad_local_path
    assert {"CODE_PATTERN", "is_bad_local_path"} <= set(vault_contract.__all__)


@pytest.mark.parametrize("corpus", ["central bank", "", "c" * 33, "cb/x", "cb\n"])
def test_an_expected_corpus_that_is_not_a_code_is_a_caller_error(corpus):
    with pytest.raises(ValueError, match="expected_corpus"):
        check_row(_golden("sec_10k"), None, expected_corpus=corpus)


@pytest.mark.parametrize("corpus", ["company ", "comp any", "company\u200b", "Company/x"])
def test_a_row_corpus_that_is_not_a_code_never_matches(corpus):
    reasons = _reasons(dict(_golden("sec_10k"), corpus=corpus), expected_corpus="company")
    assert reasons in (["corpus_conflict"], ["bad_text"]), reasons


# --- adversarial variants (golden refused files before the JSONL form) -----------------
# Each: an accepted golden row, the keys changed (None = removed), the corpus it is
# checked as, and its exact reasons.


@pytest.mark.parametrize("base, change, corpus, reasons", [
    ("cb_rate_decision", {"date": "2022-02-30"}, "central-bank", ["bad_date"]),
    ("sec_10k", {"revenue_ratio": float("nan")}, "company", ["bad_extra"]),
    ("sec_10k", {"note\ud800": "x"}, "company", ["bad_extra"]),
    ("cb_undated", {"local_path": "./raw/ca/E1/0/57d62f059376ad8e.pdf"}, "central-bank",
     ["bad_local_path"]),
    ("sec_10k", {"local_path": "raw/0000320193//A1/2024/cf0e49e6e176a851.pdf"}, "company",
     ["bad_local_path"]),
    ("cb_rendered_html", {"local_path": "file:///srv/cb/raw/ecb/C1/2023/e1e6a63950c50d3f.pdf"},
     "central-bank", ["bad_local_path"]),
    ("sec_form4", {"local_path": "~/company-corpus-data/raw/a.txt"}, "company",
     ["bad_local_path"]),
    ("cb_rendered_html", {"local_path": "raw/../../etc"}, "central-bank", ["bad_local_path"]),
    ("sec_10k", {"local_path": "/Users/x/raw/a.pdf"}, "company", ["bad_local_path"]),
    ("cb_rendered_html", {"local_path": "raw/ecb/C1/2023/e1e6a63950c50d3f.pdf "},
     "central-bank", ["bad_local_path"]),
    ("cb_rendered_html",
     {"sha256": "sha256:fdd3a53b21b5fd7b7fe7ef6f4209327854b0383ba3cd93a9f720c7fee8456983"},
     "central-bank", ["bad_sha256"]),
    ("cb_rate_decision", {"doc_id": "   "}, "central-bank", ["bad_text"]),
    ("sec_10k", {"doc_id": "cf0e49e6\u0000e176a851"}, "company", ["bad_text"]),
    ("cb_rate_decision", {"doc_id": 5}, "central-bank", ["bad_text"]),
    ("sec_form4", {"document_url": {"href": "https://www.sec.gov/Archives/edgar/data/320193/"
                                            "000032019324000081/wk-form4_1728081005.xml"}},
     "company", ["bad_text"]),
    ("cb_rendered_html", {"title": "Disinflation\u0000 policy"}, "central-bank", ["bad_text"]),
    ("cb_rendered_html", {"title": 42}, "central-bank", ["bad_text"]),
    # A company row fed to the central-bank ingestion: its codes are unknown there too.
    ("sec_10k", {}, "central-bank",
     ["corpus_conflict", "unknown_entity_scheme", "unknown_provenance", "unknown_source_code"]),
    ("cb_rate_decision", {"local_path": "raw/ecb/A1/2022/" + "n" * 252 + ".pdf",
                          "mime_type": "application/pdf"}, "central-bank", ["too_long"]),
    ("cb_rendered_html", {"doc_type": "G9"}, "central-bank", ["unknown_doc_type"]),
    ("cb_undated", {"year": 2021}, "central-bank", ["year_mismatch"]),
])
def test_adversarial_variants_are_refused_for_exactly_their_reasons(base, change, corpus, reasons):
    row = _golden(base)
    row.update(change)
    assert _reasons(row, expected_corpus=corpus) == sorted(reasons)
