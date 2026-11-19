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
    NOTICES, REASONS, CheckedRow, Notice, Refusal, check_row, quality_notices, validate_row,
)
from vault_contract.vocabulary import (
    Vocabulary, is_supported_version, load_vocabulary, validate_vocabulary,
)

VOCABULARIES = {c: load_vocabulary(golden.vocabulary(c)) for c in ("company", "central-bank")}


def _reasons(row, *, expected_corpus, vocabulary="from corpus", transition=True):
    if vocabulary == "from corpus":
        vocabulary = VOCABULARIES[expected_corpus]
    columns, _ = split_row(row, transition=transition)
    return sorted(r.reason for r in validate_row(columns, vocabulary, transition=transition,
                                                 expected_corpus=expected_corpus))


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


# --- golden ------------------------------------------------------------------

REFERENCE_ROWS = ("cb_rate_decision", "cb_rendered_html", "cb_undated", "sec_10k", "sec_form4")


def test_golden_has_the_reference_rows_and_the_notice_rows():
    assert sorted(golden.accepted()) == sorted([
        *REFERENCE_ROWS, "cb_empty_title", "cb_legacy_form", "cb_precision_not_stated",
        "cb_precision_without_date", "sec_text_without_mime"])


@pytest.mark.parametrize("name", sorted(golden.accepted()))
def test_every_golden_row_is_accepted_with_exactly_its_columns_and_notices(name):
    case = golden.accepted()[name]
    assert set(case) - {"expected_notices"} == {
        "description", "expected_corpus", "row", "expected_columns"}
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
        assert "expected_notices" not in golden.accepted()[name]


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
        "missing_language", "missing_provenance", "bad_text", "corpus_conflict",
        "unknown_source_code", "unknown_doc_type", "unknown_provenance", "bad_language",
        "bad_date", "year_mismatch", "bad_date_precision", "bad_date_kind", "bad_local_path",
        "bad_sha256", "entity_half", "unknown_entity_scheme",
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


def test_after_the_transition_old_keys_are_neither_mapped_nor_noticed():
    """transition=False: bank_code is an extra, so source_code is missing."""
    row = _golden("cb_rendered_html")
    row["bank_code"] = row.pop("source_code")
    row["local_path"] = "data/" + row["local_path"]
    assert _reasons(row, expected_corpus="central-bank", transition=False) == [
        "missing_source_code"]
    assert _notices(row, transition=False) == []


def test_a_notice_names_the_field_and_the_value_as_written():
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


def test_quality_notices_does_not_modify_its_arguments():
    row = _golden("cb_undated")
    row["date_precision"] = "year"
    columns, _ = split_row(row)
    before_row, before_columns = dict(row), dict(columns)
    quality_notices(columns, row=row)
    assert (row, columns) == (before_row, before_columns)
