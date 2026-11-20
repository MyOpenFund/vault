import json
from datetime import datetime, timezone

import pytest

from ingest import UPSERT_COLUMNS, RunReport, parse_line

RUN_TS = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
CORPUS = "central-bank"


def make_line(**overrides):
    obj = {
        "doc_id": "c184d44f298ff622",
        "corpus": "central-bank",
        "source_code": "us",
        "doc_type": "C1",
        "title": "Some speech",
        "date": "2010-01-13",
        "year": 2010,
        "language": "en",
        "provenance": "bis_index",
        "local_path": "raw/us/C1/2010/c184d44f298ff622.pdf",
    }
    obj.update(overrides)
    return json.dumps(obj)


def legacy_line(**overrides):
    """make_line in a pre-contract form: `overrides` set, None deletes a key."""
    obj = json.loads(make_line())
    for key, value in overrides.items():
        if value is None:
            obj.pop(key, None)
        else:
            obj[key] = value
    return json.dumps(obj)


def as_dict(row):
    """An upsert row by column name (UPSERT_COLUMNS order)."""
    return dict(zip(UPSERT_COLUMNS, row, strict=True))


def refusals_of(line):
    report = RunReport(CORPUS, RUN_TS)
    assert parse_line(line, "f.jsonl", 1, RUN_TS, CORPUS, report=report) is None
    return report.refusals


def test_valid_line_parses():
    row = parse_line(make_line(), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row is not None
    assert row[0] == "c184d44f298ff622"  # doc_id
    assert row[1] == "central-bank"  # corpus
    assert row[2] == "us"  # source_code
    assert as_dict(row)["local_path"] == "raw/us/C1/2010/c184d44f298ff622.pdf"


def test_manifest_corpus_field_matching_env_is_accepted():
    row = parse_line(
        make_line(corpus="central-bank"), "f.jsonl", 1, RUN_TS, CORPUS
    )
    assert row is not None
    assert row[1] == "central-bank"


@pytest.mark.parametrize("corpus", [None, ""])
def test_a_line_without_corpus_is_refused(corpus):
    # The 2026-11 transition is over: the service's CORPUS no longer fills an
    # absent or empty corpus.
    assert refusals_of(legacy_line(corpus=None) if corpus is None
                       else make_line(corpus=corpus)) == {"missing_corpus": 1}


def test_a_whitespace_corpus_is_refused():
    assert sum(refusals_of(make_line(corpus="  ")).values()) == 1


def test_bank_code_is_no_longer_read_as_source_code():
    assert refusals_of(legacy_line(source_code=None, bank_code="us")) == {
        "missing_source_code": 1}


def test_bank_code_beside_source_code_is_a_plain_extra():
    row = parse_line(make_line(bank_code="fr"), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[2] == "us"
    assert json.loads(row[-1]) == {"bank_code": "fr"}


def test_a_data_prefixed_local_path_is_refused():
    assert refusals_of(make_line(local_path="data/raw/us/C1/2010/c184d44f298ff622.pdf")) == {
        "bad_local_path": 1}


def test_contradicting_corpus_is_a_refusal():
    # Before the contract: a skipped line. Now a corpus_conflict refusal.
    report = RunReport(CORPUS, RUN_TS)
    row = parse_line(
        make_line(corpus="company"), "f.jsonl", 1, RUN_TS, CORPUS, report=report,
    )
    assert row is None
    assert report.refusals == {"corpus_conflict": 1}
    assert report.refused_rows == 1


def test_corpus_and_source_code_do_not_leak_into_extra():
    row = parse_line(
        make_line(corpus="central-bank", source_code="us"),
        "f.jsonl", 1, RUN_TS, CORPUS,
    )
    assert row[-1] is None  # no extra


DOCUMENT_URL = UPSERT_COLUMNS.index("document_url")


def test_document_url_fills_the_column():
    row = parse_line(make_line(document_url="https://x.invalid/a.pdf"),
                     "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[DOCUMENT_URL] == "https://x.invalid/a.pdf"
    assert row[-1] is None  # no extra


def test_pdf_url_is_a_plain_extra_and_never_fills_document_url():
    # pdf_url was the name of document_url until 2026-11-19; no longer read.
    row = parse_line(make_line(pdf_url="https://x.invalid/old.pdf"),
                     "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[DOCUMENT_URL] is None
    assert json.loads(row[-1]) == {"pdf_url": "https://x.invalid/old.pdf"}


def test_document_url_beside_pdf_url_keeps_its_own_value():
    row = parse_line(
        make_line(document_url="https://x.invalid/new.html", pdf_url="https://x.invalid/old.pdf"),
        "f.jsonl", 1, RUN_TS, CORPUS,
    )
    assert row[DOCUMENT_URL] == "https://x.invalid/new.html"
    assert json.loads(row[-1]) == {"pdf_url": "https://x.invalid/old.pdf"}


def test_an_empty_document_url_is_null():
    assert parse_line(make_line(document_url=""), "f.jsonl", 1, RUN_TS, CORPUS)[DOCUMENT_URL] is None


def test_no_url_at_all_leaves_document_url_null():
    row = parse_line(make_line(), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[DOCUMENT_URL] is None


def test_row_carries_run_timestamp_for_updated_and_last_seen():
    row = parse_line(make_line(), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[-3] == RUN_TS  # updated_at
    assert row[-2] == RUN_TS  # last_seen_at


def test_missing_doc_id_is_skipped():
    obj = json.loads(make_line())
    del obj["doc_id"]
    assert parse_line(json.dumps(obj), "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_invalid_json_is_not_written():
    assert parse_line("{not json", "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_blank_line_is_skipped():
    assert parse_line("   ", "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_unknown_fields_go_to_extra():
    row = parse_line(make_line(custom_field="hello"), "f.jsonl", 1, RUN_TS, CORPUS)
    extra = json.loads(row[-1])
    assert extra == {"custom_field": "hello"}


def test_empty_date_becomes_none():
    # year goes with the date: a year without a date is year_mismatch.
    row = parse_line(make_line(date="", year=None), "f.jsonl", 1, RUN_TS, CORPUS)
    assert as_dict(row)["date"] is None


# --- documents-scan exclusion of cadence files ---------------------------

def test_find_manifest_files_excludes_cadence_files(tmp_path):
    from ingest import find_manifest_files

    (tmp_path / "us.jsonl").write_text("{}\n")
    (tmp_path / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "cadence_state.jsonl").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "sub" / "fr.jsonl").write_text("{}\n")

    found = find_manifest_files(str(tmp_path), {})
    basenames = sorted(p.split("/")[-1] for p in found)
    assert basenames == ["fr.jsonl", "us.jsonl"]


def test_find_manifest_files_skips_every_producer_root_file(tmp_path):
    # U2 — the seven root-level .jsonl files the producer writes beside
    # manifest/ must never be read as document manifests: at the data/ root,
    # directly under manifest/ AND nested inside it (the glob is recursive).
    from ingest import EXCLUDED_BASENAMES, find_manifest_files
    expected = {
        "cadence.jsonl", "cadence_state.jsonl", "runs.jsonl",
        "discovery_errors.jsonl", "download_errors.jsonl",
        "download_quarantine.jsonl", "wp_dates_index.jsonl",
    }
    assert EXCLUDED_BASENAMES == frozenset(expected)
    (tmp_path / "manifest" / "nested").mkdir(parents=True)
    for name in expected:
        (tmp_path / name).write_text("{}\n")
        (tmp_path / "manifest" / name).write_text("{}\n")
        (tmp_path / "manifest" / "nested" / name).write_text("{}\n")
    (tmp_path / "manifest" / "us.jsonl").write_text("{}\n")
    found = [p.rsplit("/", 1)[1] for p in find_manifest_files(str(tmp_path), {})]
    assert found == ["us.jsonl"]


def test_find_manifest_files_keeps_legacy_monolithic_manifest(tmp_path):
    # U3 — data/manifest.jsonl is the legacy single-file manifest: in scope.
    from ingest import find_manifest_files
    (tmp_path / "manifest.jsonl").write_text("{}\n")
    assert [p.rsplit("/", 1)[1] for p in find_manifest_files(str(tmp_path), {})] == ["manifest.jsonl"]


def test_upsert_refreshes_extra_on_conflict():
    # vault #8/#2: `extra` used to be written at first insert only. The
    # manifest is the truth for extra exactly as for every other column.
    from ingest import UPSERT_SQL
    set_clause = UPSERT_SQL.split("DO UPDATE SET", 1)[1]
    assert "extra = EXCLUDED.extra" in set_clause


def test_upsert_never_rewinds_last_seen_at():
    # vault #3: a slow run committing an older run timestamp after a newer
    # one must not move the stamp backwards (the sweep trusts it).
    from ingest import UPSERT_SQL
    set_clause = UPSERT_SQL.split("DO UPDATE SET", 1)[1]
    assert "last_seen_at = GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at)" in set_clause
    assert "last_seen_at = EXCLUDED.last_seen_at" not in set_clause


def test_lock_keys_are_distinct_per_corpus_and_from_the_ddl_lock():
    # vault #3: two runs of the same corpus serialize on their own key; a
    # different corpus (and the global DDL key) must never collide with it.
    from ingest import DDL_LOCK_KEY, corpus_lock_key
    assert corpus_lock_key("central-bank") == "vault-ingest-central-bank"
    assert corpus_lock_key("central-bank") != corpus_lock_key("company")
    assert DDL_LOCK_KEY not in {corpus_lock_key("central-bank"), corpus_lock_key("company")}


# --- doc_id must be a non-empty string (vault #6 follow-up) -----------------
#
# dedupe_rows compares Python values while Postgres compares TEXT: 5 and "5"
# are distinct to Python but the same key once adapted, so they still
# crashed the upsert; 1 and true are equal in Python; [1] is unhashable. A
# doc_id that is not a non-empty string is therefore refused at parse time:
# before the contract a skipped line counted `invalid_doc_id`, now a contract
# refusal (bad_text, missing_doc_id) that skips the sweep and fails the run.


@pytest.mark.parametrize("bad, reason", [
    (5, "bad_text"), (0, "bad_text"), (1.5, "bad_text"), (True, "bad_text"),
    (False, "bad_text"), ([1], "bad_text"), ({"a": 1}, "bad_text"),
    ("", "missing_doc_id"), ("a\x00b", "bad_text"), ("  ", "bad_text"),
    (" padded ", "bad_text"),        # the contract refuses padding since 45d8b63
])
def test_non_string_or_empty_doc_id_is_refused_and_logged(bad, reason, caplog):
    import logging
    report = RunReport(CORPUS, RUN_TS)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        row = parse_line(make_line(doc_id=bad), "f.jsonl", 7, RUN_TS, CORPUS, report=report)
    assert row is None
    assert report.refusals == {reason: 1}
    (record,) = caplog.records
    assert record.getMessage().startswith(f"f.jsonl:7 {reason} doc_id=")


@pytest.mark.parametrize("missing", ["absent", None])
def test_missing_doc_id_is_refused_too(missing):
    obj = json.loads(make_line())
    if missing == "absent":
        del obj["doc_id"]
    else:
        obj["doc_id"] = None
    report = RunReport(CORPUS, RUN_TS)
    assert parse_line(json.dumps(obj), "f.jsonl", 1, RUN_TS, CORPUS, report=report) is None
    assert report.refusals == {"missing_doc_id": 1}


@pytest.mark.parametrize("ok", ["5", "ünïcode-ид", "a" * 500])
def test_any_non_empty_string_doc_id_is_kept_verbatim(ok):
    assert parse_line(make_line(doc_id=ok), "f.jsonl", 1, RUN_TS, CORPUS)[0] == ok


@pytest.mark.parametrize("line", ['"just a string"', "[1, 2]", "5", "null", "true"])
def test_valid_json_that_is_not_an_object_is_refused(line, caplog):
    import logging
    report = RunReport(CORPUS, RUN_TS)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        assert parse_line(line, "f.jsonl", 2, RUN_TS, CORPUS, report=report) is None
    assert caplog.records[0].getMessage().startswith("f.jsonl:2 not_json")
    assert report.refusals == {"not_json": 1}


def test_a_huge_invalid_doc_id_is_logged_truncated(caplog):
    import ingest
    huge = list(range(10_000))
    line = make_line(doc_id=huge)
    with caplog.at_level("WARNING", logger="ingest"):
        assert ingest.parse_line(line, "f.jsonl", 1, None, "central-bank") is None
    (message,) = [r.getMessage() for r in caplog.records]
    assert len(message) < ingest.REFUSAL_VALUE_CHARS + 100
    assert "…" in message
