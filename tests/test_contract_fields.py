"""`vault_contract.fields`: the column table and `split_row` (contract 1.0.0, spec §3.1).

`split_row` is the one place a manifest line becomes (columns, extra): the vault
ingestion and data-orchestrator both call it, so the vault row and the RAG
payload cannot drift apart again (they did, through two copies of the known
field list). It never refuses anything; `validate_row` judges the columns.
"""
import copy

import pytest

from vault_contract import golden
from vault_contract.fields import (
    COLUMNS, DATE_KINDS, DATE_PRECISIONS, EXPECTED, REFUSED_WHEN_ABSENT, TRANSITION_ALIASES,
    split_row,
)

APPLE_10K_EXTRA_KEYS = {
    "cik", "family", "sec_form", "accession", "period_of_report", "company",
    "company_current", "ticker", "entity_id", "primary_path", "text_path", "pdf_path",
    "submission_path", "submission_sha256", "submission_url", "alt_urls", "frequency",
}


def _row(name):
    return golden.accepted()[name]["row"]


def test_columns_are_the_eighteen_common_fields_in_ddl_order():
    assert COLUMNS == (
        "doc_id", "corpus", "source_code", "doc_type", "title", "date", "year",
        "date_precision", "date_kind", "language", "provenance", "document_url",
        "source_url", "mime_type", "sha256", "local_path", "entity_scheme", "entity_value",
    )


def test_mandatory_fields_split_into_refused_and_expected():
    """Absent -> refusal for identity and codes; absent title -> notice only
    (110 real central-bank rows have none)."""
    assert REFUSED_WHEN_ABSENT == ("doc_id", "corpus", "source_code", "doc_type",
                                   "language", "provenance")
    assert EXPECTED == ("title",)
    assert set(REFUSED_WHEN_ABSENT) | set(EXPECTED) <= set(COLUMNS)
    assert not set(REFUSED_WHEN_ABSENT) & set(EXPECTED)


def test_closed_value_sets_and_transition_aliases():
    assert DATE_PRECISIONS == ("day", "month", "year")
    assert DATE_KINDS == ("publication", "meeting", "period")
    assert TRANSITION_ALIASES == {"bank_code": "source_code", "pdf_url": "document_url"}


def test_apple_10k_splits_into_eighteen_columns_and_its_sec_extras():
    columns, extra = split_row(_row("sec_10k"))
    assert tuple(columns) == COLUMNS
    assert columns["doc_id"] == "cf0e49e6e176a851"
    assert columns["entity_scheme"] == "cik" and columns["entity_value"] == "0000320193"
    assert columns["local_path"] == "raw/0000320193/A1/2024/cf0e49e6e176a851.pdf"
    assert set(extra) == APPLE_10K_EXTRA_KEYS
    assert extra["accession"] == "0000320193-24-000123"
    assert extra["alt_urls"] == []


def test_a_legacy_row_without_transition_keeps_its_old_keys_as_extras():
    """transition=False is the switch the cleanup PR flips: no alias mapping,
    no prefix stripping."""
    legacy = {"doc_id": "x", "bank_code": "ecb", "pdf_url": "https://a/b.pdf",
              "local_path": "data/raw/ecb/a.pdf"}
    columns, extra = split_row(legacy, transition=False)
    assert columns["source_code"] is None
    assert columns["document_url"] is None
    assert columns["local_path"] == "data/raw/ecb/a.pdf"
    assert extra == {"bank_code": "ecb", "pdf_url": "https://a/b.pdf"}


def test_form4_filer_stays_an_extra_never_the_entity():
    columns, extra = split_row(_row("sec_form4"))
    assert (columns["entity_scheme"], columns["entity_value"]) == ("cik", "0000320193")
    assert (extra["filer_scheme"], extra["filer_value"]) == ("cik", "0001900001")


def test_legacy_central_bank_row_maps_bank_code_and_pdf_url():
    """A manifest line written before the contract (central-bank-corpus before
    its migrate-contract): the aliases fill their columns and leave no trace
    in extra, where they would duplicate the column."""
    legacy = {
        "doc_id": "0123456789abcdef", "bank_code": "ecb", "doc_type": "A1",
        "title": "Monetary policy decisions", "pdf_url": "https://example.org/a.pdf",
        "language": "en", "provenance": "bank_site", "local_path": "data/raw/ecb/A1/2022/a.pdf",
    }
    columns, extra = split_row(legacy)
    assert columns["source_code"] == "ecb"
    assert columns["document_url"] == "https://example.org/a.pdf"
    assert columns["local_path"] == "raw/ecb/A1/2022/a.pdf"
    assert columns["corpus"] is None
    assert extra == {}


def test_contract_name_wins_over_its_alias_and_the_alias_is_dropped():
    columns, extra = split_row({"source_code": "ecb", "bank_code": "fed",
                                "document_url": "https://a/new", "pdf_url": "https://a/old"})
    assert columns["source_code"] == "ecb"
    assert columns["document_url"] == "https://a/new"
    assert "bank_code" not in extra and "pdf_url" not in extra


@pytest.mark.parametrize("blank", ["", None])
def test_alias_fills_a_blank_contract_value(blank):
    """Same rule as ingest.py before the contract: `document_url or pdf_url`."""
    columns, _ = split_row({"document_url": blank, "pdf_url": "https://a/old",
                            "source_code": blank, "bank_code": "ecb"})
    assert columns["document_url"] == "https://a/old"
    assert columns["source_code"] == "ecb"


@pytest.mark.parametrize("path, expected", [
    ("data/raw/x.pdf", "raw/x.pdf"),
    ("raw/x.pdf", "raw/x.pdf"),
    ("raw/data/x.pdf", "raw/data/x.pdf"),     # only a leading data/ component
    ("database/x.pdf", "database/x.pdf"),     # a prefix of a name is not a component
    ("data/data/x.pdf", "data/x.pdf"),        # stripped once, not repeatedly
    ("data/", "data/"),                       # nothing left: kept for validation to refuse
])
def test_a_leading_data_component_of_local_path_is_dropped(path, expected):
    columns, _ = split_row({"local_path": path})
    assert columns["local_path"] == expected


def test_unknown_key_goes_to_extra_untouched():
    nested = {"a": [1, 2], "b": None}
    columns, extra = split_row({"doc_id": "x", "foo": nested, "repec_handle": ""})
    assert extra == {"foo": nested, "repec_handle": ""}
    assert "foo" not in columns


@pytest.mark.parametrize("date", [None, "", "  "])
def test_precision_and_kind_are_dropped_from_an_undated_row(date):
    """No date, no precision and no kind (quality_notices reports each drop)."""
    columns, extra = split_row({"date": date, "date_precision": "month", "date_kind": "period"})
    assert columns["date_precision"] is None
    assert columns["date_kind"] is None
    assert extra == {}


def test_precision_and_kind_are_kept_on_a_dated_row():
    columns, _ = split_row({"date": "2024-11-01", "date_precision": "day",
                            "date_kind": "publication"})
    assert (columns["date_precision"], columns["date_kind"]) == ("day", "publication")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
@pytest.mark.parametrize("field", [
    "title", "date", "year", "date_precision", "date_kind", "document_url", "source_url",
    "mime_type", "sha256", "local_path", "entity_scheme", "entity_value",
])
def test_a_blank_optional_or_expected_column_is_absent(field, blank):
    """A producer writes "" for "not known" (central-bank mime_type before a
    download, title of a sitemap discovery); the contract reads a blank string
    as absent, so it never reaches a column and never fails a shape check."""
    columns, extra = split_row({"date": "2024-11-01", field: blank})
    assert columns[field] is None
    assert extra == {}


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("field", REFUSED_WHEN_ABSENT)
def test_a_blank_refused_when_absent_column_is_kept_for_validation_to_judge(field, blank):
    columns, _ = split_row({field: blank})
    assert columns[field] == blank


def test_missing_columns_are_none():
    columns, extra = split_row({})
    assert columns == dict.fromkeys(COLUMNS)
    assert extra == {}


def test_split_row_does_not_modify_its_input():
    row = _row("cb_rendered_html")
    before = copy.deepcopy(row)
    split_row(row)
    assert row == before


@pytest.mark.parametrize("not_a_row", [None, [], "doc_id", 5])
def test_split_row_refuses_something_that_is_not_a_mapping(not_a_row):
    with pytest.raises(TypeError):
        split_row(not_a_row)
