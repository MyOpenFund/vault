import json
from datetime import datetime, timezone

from ingest import parse_line

RUN_TS = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
CORPUS = "central-bank"


def make_line(**overrides):
    obj = {
        "doc_id": "c184d44f298ff622",
        "bank_code": "us",
        "doc_type": "C1",
        "title": "Some speech",
        "date": "2010-01-13",
        "year": 2010,
        "language": "en",
        "provenance": "bis_index",
        "local_path": "data/raw/us/C1/2010/c184d44f298ff622.pdf",
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_valid_line_parses():
    row = parse_line(make_line(), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row is not None
    assert row[0] == "c184d44f298ff622"  # doc_id
    assert row[1] == "central-bank"  # corpus (from default)
    assert row[2] == "us"  # source_code (mapped from bank_code)


def test_source_code_field_takes_precedence_over_bank_code():
    row = parse_line(
        make_line(source_code="ecb"), "f.jsonl", 1, RUN_TS, CORPUS
    )
    assert row[2] == "ecb"


def test_manifest_corpus_field_matching_env_is_accepted():
    row = parse_line(
        make_line(corpus="central-bank"), "f.jsonl", 1, RUN_TS, CORPUS
    )
    assert row is not None
    assert row[1] == "central-bank"


def test_contradicting_corpus_rejects_line_and_counts():
    counters = {"corpus_conflict": 0}
    row = parse_line(
        make_line(corpus="company"), "f.jsonl", 1, RUN_TS, CORPUS,
        counters=counters,
    )
    assert row is None
    assert counters["corpus_conflict"] == 1


def test_corpus_and_source_code_do_not_leak_into_extra():
    row = parse_line(
        make_line(corpus="central-bank", source_code="us"),
        "f.jsonl", 1, RUN_TS, CORPUS,
    )
    assert row[-1] is None  # no extra


def test_row_carries_run_timestamp_for_updated_and_last_seen():
    row = parse_line(make_line(), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[-3] == RUN_TS  # updated_at
    assert row[-2] == RUN_TS  # last_seen_at


def test_missing_doc_id_is_skipped():
    obj = json.loads(make_line())
    del obj["doc_id"]
    assert parse_line(json.dumps(obj), "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_invalid_json_is_skipped():
    assert parse_line("{not json", "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_blank_line_is_skipped():
    assert parse_line("   ", "f.jsonl", 1, RUN_TS, CORPUS) is None


def test_unknown_fields_go_to_extra():
    row = parse_line(make_line(custom_field="hello"), "f.jsonl", 1, RUN_TS, CORPUS)
    extra = json.loads(row[-1])
    assert extra == {"custom_field": "hello"}


def test_empty_date_becomes_none():
    row = parse_line(make_line(date=""), "f.jsonl", 1, RUN_TS, CORPUS)
    assert row[7] is None  # date position (shifted by corpus/source_code)
