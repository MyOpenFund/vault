"""The documents pass under the documents contract (chantier 4, plan V5).

parse_line wiring (check_row + the vault's own refusals), the run report
(counters, refusal rows, log cap, summary), the vocabulary file, and main()'s
sweep decision and exit code -- the last ones against a fake connection. The
same behaviour against a real Postgres lives in
tests/integration/test_documents_contract.py.
"""
import json
import logging
from collections import Counter
from datetime import datetime, timezone

import pytest
from vault_contract import COLUMNS, golden

import ingest
import ingest_vocabulary
from ingest import (
    REFUSAL_LOG_LIMIT, REFUSAL_VALUE_CHARS, UPSERT_COLUMNS, UPSERT_SQL, VAULT_REASONS,
    RunReport, parse_line, shown_value,
)

RUN_TS = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
CB = "central-bank"


def as_dict(row):
    return dict(zip(UPSERT_COLUMNS, row, strict=True))


def cb_row(**overrides):
    row = {
        "doc_id": "d1", "corpus": CB, "source_code": "ecb", "doc_type": "A1",
        "title": "Monetary policy decisions", "date": "2022-07-21", "year": 2022,
        "date_precision": "day", "date_kind": "meeting", "language": "en",
        "provenance": "bank_site",
    }
    row.update(overrides)
    return row


def parse(row, corpus=CB, vocabulary=None, report=None, line_num=1):
    line = row if isinstance(row, str) else json.dumps(row)
    return parse_line(line, "manifest/ecb.jsonl", line_num, RUN_TS, corpus, vocabulary, report)


def cb_vocabulary():
    return ingest_vocabulary.load_vocabulary(golden.vocabulary("central-bank"))


# --- upsert shape -------------------------------------------------------------


def test_upsert_columns_are_the_contract_columns_then_the_stamps():
    assert UPSERT_COLUMNS == COLUMNS + ("updated_at", "last_seen_at", "extra")
    insert_list = UPSERT_SQL.split("(", 1)[1].split(")", 1)[0]
    assert [c.strip() for c in insert_list.split(",")] == list(UPSERT_COLUMNS)


def test_upsert_conflicts_on_corpus_and_doc_id():
    assert "ON CONFLICT (corpus, doc_id) DO UPDATE SET" in UPSERT_SQL
    set_clause = UPSERT_SQL.split("DO UPDATE SET", 1)[1]
    for name in COLUMNS[2:] + ("extra", "updated_at"):
        assert f"{name} = EXCLUDED.{name}" in set_clause
    assert "doc_id = EXCLUDED" not in set_clause and "corpus = EXCLUDED" not in set_clause


# --- golden rows ----------------------------------------------------------------


@pytest.mark.parametrize("name, case", sorted(golden.accepted().items()))
def test_every_golden_accepted_row_is_upserted_with_its_columns(name, case):
    report = RunReport(case["expected_corpus"], RUN_TS)
    row = parse(case["row"], corpus=case["expected_corpus"], report=report,
                vocabulary=ingest_vocabulary.load_vocabulary(
                    golden.vocabulary(case["expected_corpus"])))
    assert row is not None, report.refusals
    got = as_dict(row)
    expected = dict(case["expected_columns"])
    if expected["corpus"] is None:          # transition: the service's CORPUS applies
        expected["corpus"] = case["expected_corpus"]
    assert {k: got[k] for k in COLUMNS} == expected
    assert report.notices == Counter(case.get("expected_notices", []))
    assert report.refused_rows == 0
    extra = json.loads(got["extra"]) if got["extra"] else {}
    assert not set(extra) & set(COLUMNS)


@pytest.mark.parametrize("name, case", sorted(
    (n, c) for n, c in golden.refused().items() if c.get("transition", True)))
def test_every_golden_refused_row_is_refused_with_its_reasons(name, case):
    report = RunReport(case["expected_corpus"], RUN_TS)
    row = parse(case["row"], corpus=case["expected_corpus"], report=report,
                vocabulary=ingest_vocabulary.load_vocabulary(
                    golden.vocabulary(case["expected_corpus"])))
    assert row is None
    assert report.refusals == Counter(case["expected_reasons"])
    assert report.refused_rows == 1
    assert report.notices == Counter()   # a refused row is not written: no notice


def test_company_golden_row_under_central_bank_is_a_corpus_conflict():
    report = RunReport(CB, RUN_TS)
    assert parse(golden.accepted()["sec_10k"]["row"], corpus=CB, report=report) is None
    assert report.refusals == {"corpus_conflict": 1}


def test_company_golden_row_fills_all_eighteen_columns():
    row = as_dict(parse(golden.accepted()["sec_10k"]["row"], corpus="company"))
    assert all(row[name] is not None for name in COLUMNS)
    extra = json.loads(row["extra"])
    assert extra["sec_form"] == "10-K" and "entity_scheme" not in extra


def test_central_bank_date_facts_leave_extra_for_their_columns():
    # Before the contract, date_precision/date_kind of a cb row landed in extra.
    legacy = golden.accepted()["cb_legacy_form"]["row"]
    row = as_dict(parse(legacy))
    assert row["date_precision"] == "month"
    extra = json.loads(row["extra"])
    assert "date_precision" not in extra and "date_kind" not in extra
    assert "bank_code" not in extra and "pdf_url" not in extra
    assert extra["date_source"] == "repec"
    assert row["source_code"] == "jp" and row["local_path"].startswith("raw/")


# --- vocabulary membership -----------------------------------------------------


def test_unknown_doc_type_is_refused_only_with_a_vocabulary():
    bad = cb_row(doc_type="ZZ")
    report = RunReport(CB, RUN_TS)
    assert parse(bad, vocabulary=cb_vocabulary(), report=report) is None
    assert report.refusals == {"unknown_doc_type": 1}
    assert parse(bad, vocabulary=None) is not None       # transition: codes not checked


# --- the vault's own refusals ----------------------------------------------------


def test_vault_reasons_are_not_contract_reasons():
    from vault_contract import REASONS
    assert VAULT_REASONS == ("bad_extra",)
    assert not set(VAULT_REASONS) & set(REASONS)


@pytest.mark.parametrize("extra", [
    {"note": "a\u0000b"},                       # JSONB refuses \u0000
    {"deep": [{"x": ["ok", "nul\u0000"]}]},      # at any depth
    {"k\u0000": 1},                             # in a key
    {"surrogate": "\ud800"},                    # a lone surrogate
    {"n": float("nan")},
    {"n": [float("inf")]},
])
def test_an_extra_jsonb_cannot_hold_is_a_bad_extra_refusal(extra):
    report = RunReport(CB, RUN_TS)
    line = json.dumps(cb_row(**extra))           # NaN/Infinity written as JSON allows
    assert parse(line, report=report) is None
    assert report.refusals == {"bad_extra": 1}
    (record,) = report.records
    assert record[5] == "bad_extra" and record[6] == next(iter(extra))


def test_a_harmless_escaped_backslash_u0000_text_is_not_refused():
    # "\\u0000" in a JSON string is the six characters \u0000, not a NUL.
    row = parse(json.dumps(cb_row(note="\\u0000")))
    assert json.loads(as_dict(row)["extra"]) == {"note": "\\u0000"}


def test_a_lone_surrogate_in_a_text_column_is_bad_text():
    report = RunReport(CB, RUN_TS)
    assert parse(json.dumps(cb_row(title="a\ud800")), report=report) is None
    assert report.refusals == {"bad_text": 1}
    assert report.records[0][6] == "title"


def test_one_row_with_two_defects_counts_both_reasons_and_one_row():
    report = RunReport(CB, RUN_TS)
    parse(cb_row(language="english", note="\u0000"), report=report)
    assert report.refusals == {"bad_language": 1, "bad_extra": 1}
    assert report.refused_rows == 1
    assert len(report.records) == 2


# --- unparsed lines are not refusals ------------------------------------------------


@pytest.mark.parametrize("line", ["{not json", '"a string"', "[1]", '{"doc_id": ' + "9" * 5000 + "}"])
def test_a_line_that_is_not_a_json_object_is_unparsed_not_refused(line):
    report = RunReport(CB, RUN_TS)
    assert parse(line, report=report) is None
    assert report.unparsed == 1 and report.refused_rows == 0 and report.rows_read == 0


def test_blank_lines_count_nowhere():
    report = RunReport(CB, RUN_TS)
    assert parse("   ", report=report) is None
    assert (report.rows_read, report.unparsed, report.refused_rows) == (0, 0, 0)


# --- refusal rows and logs -----------------------------------------------------------


def test_refusal_row_holds_corpus_run_file_line_doc_id_reason_field_value(tmp_path):
    report = RunReport(CB, RUN_TS, data_dir=str(tmp_path))
    parse_line(json.dumps(cb_row(doc_id="x1", date_precision="Day")),
               str(tmp_path / "manifest" / "ecb.jsonl"), 42, RUN_TS, CB, None, report)
    assert report.records == [
        (CB, RUN_TS, "manifest/ecb.jsonl", 42, "x1", "bad_date_precision",
         "date_precision", "'Day'")]


def test_refusal_value_is_the_repr_truncated():
    huge = "x" * 1000
    assert shown_value(huge) == repr(huge)[:REFUSAL_VALUE_CHARS] + "…"
    assert len(shown_value(huge)) == REFUSAL_VALUE_CHARS + 1
    assert shown_value("Day") == "'Day'"
    assert shown_value(None) == "None"
    assert shown_value("a\x00b") == "'a\\x00b'"            # storable as TEXT


def test_a_doc_id_that_cannot_be_stored_is_null_in_the_refusal_row():
    for bad in (5, ["a"], "a\x00b", "\ud800"):
        report = RunReport(CB, RUN_TS)
        parse(json.dumps(cb_row(doc_id=bad)), report=report)
        assert report.records[0][4] is None
        assert report.records[0][6] == "doc_id"


def test_a_file_outside_data_dir_is_stored_as_given():
    report = RunReport(CB, RUN_TS, data_dir="/data")
    assert report.source_name("/elsewhere/m.jsonl") == "/elsewhere/m.jsonl"
    assert report.source_name("/data/manifest/m.jsonl") == "manifest/m.jsonl"


def test_each_refusal_is_logged_file_line_reason_field_value(caplog):
    report = RunReport(CB, RUN_TS)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        parse(cb_row(doc_type="ZZ"), vocabulary=cb_vocabulary(), report=report, line_num=3)
    assert [r.getMessage() for r in caplog.records] == [
        "manifest/ecb.jsonl:3 unknown_doc_type doc_type='ZZ'"]


def test_refusal_logs_are_capped_but_every_refusal_is_kept(caplog):
    report = RunReport(CB, RUN_TS)
    n = REFUSAL_LOG_LIMIT + 7
    with caplog.at_level(logging.WARNING, logger="ingest"):
        for i in range(n):
            parse(cb_row(doc_id=f"d{i}", language="EN"), report=report, line_num=i + 1)
    assert len(caplog.records) == REFUSAL_LOG_LIMIT
    assert len(report.records) == n and report.unlogged == 7
    assert any("7 more not logged" in line for line in report.summary_lines("x"))


def test_notices_are_counted_for_written_rows():
    report = RunReport(CB, RUN_TS)
    parse(cb_row(date_precision=None), report=report)
    parse(cb_row(doc_id="d2", title=""), report=report)
    assert report.notices == {"precision_not_stated": 1, "empty_title": 1}


def test_summary_lines_show_rows_refusals_notices_and_the_sweep():
    report = RunReport(CB, RUN_TS)
    report.rows_read, report.upserted, report.duplicates, report.unparsed = 10, 8, 1, 2
    report.refused_rows = 1
    report.refusals.update({"unknown_doc_type": 1})
    report.notices.update({"corpus_missing": 9, "empty_title": 1})
    lines = report.summary_lines("skipped (1 refused rows)")
    assert lines == [
        "documents: 10 rows read, 8 upserted, 1 refused (unknown_doc_type=1), "
        "1 duplicate line(s) dropped, 2 unparsed line(s)",
        "notices: 10 (corpus_missing=9, empty_title=1)",
        "sweep: skipped (1 refused rows)",
    ]


# --- vocabulary.json -----------------------------------------------------------------


def write_vocabulary(directory, doc):
    path = directory / "vocabulary.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_vocabulary_absent_is_none(tmp_path):
    assert ingest_vocabulary.load(str(tmp_path / "vocabulary.json"), CB) is None


def test_vocabulary_path_defaults_to_data_dir_and_can_be_overridden(tmp_path):
    assert ingest_vocabulary.vocabulary_path("/data", {}) == "/data/vocabulary.json"
    assert ingest_vocabulary.vocabulary_path("/data", {"VOCABULARY_PATH": ""}) == "/data/vocabulary.json"
    assert ingest_vocabulary.vocabulary_path("/data", {"VOCABULARY_PATH": "/v/x.json"}) == "/v/x.json"


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"contract": "documents"}),                      # fails validate_vocabulary
    json.dumps(dict(golden.vocabulary("central-bank"), contract_version="2.0.0")),
])
def test_an_invalid_vocabulary_is_a_vocabulary_error(tmp_path, content):
    path = tmp_path / "vocabulary.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ingest_vocabulary.VocabularyError) as exc:
        ingest_vocabulary.load(str(path), CB)
    assert str(path) in str(exc.value)


def test_a_vocabulary_of_another_corpus_is_a_vocabulary_error(tmp_path):
    path = write_vocabulary(tmp_path, golden.vocabulary("company"))
    with pytest.raises(ingest_vocabulary.VocabularyError, match="'company'"):
        ingest_vocabulary.load(str(path), CB)


def test_vocabulary_table_rows():
    doc = {"contract": "documents", "contract_version": "1.0.0", "corpus": "company",
           "source_codes": [{"code": "sec", "label": "SEC EDGAR"}],
           "doc_types": [{"code": "A1", "label": "10-K", "family": "A"},
                         {"code": "Z9", "label": "Other"}],
           "provenances": ["edgar_index", "edgar_fts"],
           "entity_schemes": ["cik"]}
    vocabulary = ingest_vocabulary.load_vocabulary(doc)
    assert ingest_vocabulary.table_rows(vocabulary) == [
        ("company", "source_code", "sec", "SEC EDGAR", None),
        ("company", "doc_type", "A1", "10-K", "A"),
        ("company", "doc_type", "Z9", "Other", None),
        ("company", "provenance", "edgar_fts", None, None),
        ("company", "provenance", "edgar_index", None, None),
        ("company", "entity_scheme", "cik", None, None),
    ]


# --- main(): vocabulary gate, sweep decision, exit code (fake connection) -------------


class _Connected(Exception):
    pass


def test_an_invalid_vocabulary_fails_the_run_before_connecting(monkeypatch, tmp_path, caplog):
    def connect(*_a, **_k):
        raise _Connected()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.delenv("VOCABULARY_PATH", raising=False)
    monkeypatch.setattr(ingest.psycopg2, "connect", connect)
    write_vocabulary(tmp_path, golden.vocabulary("company"))
    with caplog.at_level(logging.ERROR, logger="ingest"), pytest.raises(SystemExit) as exc:
        ingest.main(corpus=CB, data_dir=str(tmp_path))
    assert exc.value.code == 1
    assert any("vocabulary" in r.getMessage() for r in caplog.records)


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)
        if sql is ingest.SWEEP_SQL:
            self.rowcount = self.conn.sweep_candidates

    def fetchone(self):
        return (self.conn.sweep_candidates, self.conn.live)


class _FakeConn:
    def __init__(self, sweep_candidates=1, live=100):
        self.autocommit = False
        self.closed = 0
        self.statements = []
        self.sweep_candidates = sweep_candidates
        self.live = live

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = 1


@pytest.fixture()
def fake_run(monkeypatch, tmp_path):
    """main() over tmp_path with every database step faked; returns a recorder."""
    conn = _FakeConn()
    seen = {"upserted": [], "refusals": None, "vocabulary": None}
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "1.0")
    monkeypatch.delenv("VOCABULARY_PATH", raising=False)
    monkeypatch.delenv("MANIFEST_GLOBS", raising=False)
    monkeypatch.setattr(ingest.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(ingest, "apply_schema", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "acquire_lock", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "execute_values",
                        lambda cur, sql, rows, page_size: seen["upserted"].extend(rows))
    monkeypatch.setattr(ingest, "replace_refusals",
                        lambda c, report: seen.__setitem__("refusals", list(report.records)))
    monkeypatch.setattr(ingest_vocabulary, "replace",
                        lambda c, vocabulary: seen.__setitem__("vocabulary", vocabulary))
    for mod in (ingest.ingest_cadence, ingest.ingest_runs, ingest.ingest_discovery_errors):
        monkeypatch.setattr(mod, "run", lambda *a, **k: 0)

    def run(rows):
        manifest = tmp_path / "manifest"
        manifest.mkdir(exist_ok=True)
        (manifest / "ecb.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        ingest.main(corpus=CB, data_dir=str(tmp_path))

    seen["run"] = run
    seen["conn"] = conn
    seen["dir"] = tmp_path
    return seen


def test_a_clean_run_sweeps_and_returns(fake_run, caplog):
    with caplog.at_level(logging.INFO, logger="ingest"):
        fake_run["run"]([cb_row(doc_id=f"d{i}") for i in range(3)])     # no SystemExit
    assert len(fake_run["upserted"]) == 3
    assert fake_run["refusals"] == []
    assert ingest.SWEEP_SQL in fake_run["conn"].statements
    assert "sweep: done (1 soft-deleted)" in caplog.text
    assert "vocabulary: missing (membership not checked)" in caplog.text


def test_one_refused_row_skips_the_sweep_and_exits_1_after_every_pass(
    fake_run, monkeypatch, caplog
):
    passes = []
    for mod in (ingest.ingest_cadence, ingest.ingest_runs, ingest.ingest_discovery_errors):
        monkeypatch.setattr(mod, "run", lambda *a, _m=mod.__name__, **k: passes.append(_m) or 0)
    write_vocabulary(fake_run["dir"], golden.vocabulary("central-bank"))
    rows = [cb_row(doc_id=f"d{i}") for i in range(9)] + [cb_row(doc_id="bad", doc_type="ZZ")]
    with caplog.at_level(logging.INFO, logger="ingest"), pytest.raises(SystemExit) as exc:
        fake_run["run"](rows)
    assert exc.value.code == 1
    assert len(fake_run["upserted"]) == 9
    assert [r[5] for r in fake_run["refusals"]] == ["unknown_doc_type"]
    assert fake_run["vocabulary"].corpus == CB
    assert ingest.SWEEP_SQL not in fake_run["conn"].statements
    assert ingest.SWEEP_COUNT_SQL not in fake_run["conn"].statements
    assert passes == ["ingest_cadence", "ingest_runs", "ingest_discovery_errors"]
    assert "sweep skipped: 1 refused rows" in caplog.text
    assert "1 refused (unknown_doc_type=1)" in caplog.text
    assert fake_run["conn"].closed


def test_every_row_refused_soft_deletes_nothing(fake_run):
    write_vocabulary(fake_run["dir"], golden.vocabulary("central-bank"))
    with pytest.raises(SystemExit):
        fake_run["run"]([cb_row(doc_id=f"d{i}", doc_type="ZZ") for i in range(5)])
    assert fake_run["upserted"] == []
    assert len(fake_run["refusals"]) == 5
    assert ingest.SWEEP_SQL not in fake_run["conn"].statements
