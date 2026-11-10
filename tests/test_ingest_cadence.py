import json
from datetime import datetime, timezone

from ingest_cadence import parse_cadence_line

RUN_TS = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
CORPUS = "central-bank"


def make_line(**overrides):
    obj = {
        "bank_code": "us",
        "doc_type": "C1",
        "last": "2026-08-01",
        "interval_days": 14,
        "next_expected": "2026-08-15",
        "days_until": -12,
        "status": "overdue",
        "expected_per_year": 26,
        "n_3y": 78,
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_valid_line_parses_and_maps_bank_code_to_source_code():
    row = parse_cadence_line(make_line(), "cadence.jsonl", 1, RUN_TS, CORPUS)
    assert row == (
        "central-bank", "us", "C1", "2026-08-01", 14, "2026-08-15",
        -12, "overdue", 26, 78, RUN_TS, None,
    )


def test_source_code_field_takes_precedence_over_bank_code():
    row = parse_cadence_line(
        make_line(source_code="ecb"), "cadence.jsonl", 1, RUN_TS, CORPUS
    )
    assert row[1] == "ecb"


def test_singleton_series_parses_with_null_optional_fields():
    # A series with a single distinct date has no interval to compute: the
    # producer omits interval_days/next_expected/days_until/status entirely
    # rather than emitting them as explicit JSON nulls. A line missing these
    # optional fields must still parse, with None in those tuple positions.
    obj = {
        "bank_code": "us",
        "doc_type": "C1",
        "last": "2026-08-01",
        "expected_per_year": 26,
        "n_3y": 1,
    }
    row = parse_cadence_line(
        json.dumps(obj), "cadence.jsonl", 1, RUN_TS, CORPUS
    )
    assert row == (
        "central-bank", "us", "C1", "2026-08-01",
        None, None, None, None,  # interval_days, next_expected, days_until, status
        26, 1, RUN_TS, None,
    )


def test_unknown_fields_go_to_extra():
    row = parse_cadence_line(
        make_line(muted=True), "cadence.jsonl", 1, RUN_TS, CORPUS
    )
    assert json.loads(row[11]) == {"muted": True}


def test_missing_key_field_skips_line():
    line = make_line()
    obj = json.loads(line)
    del obj["doc_type"]
    assert parse_cadence_line(
        json.dumps(obj), "cadence.jsonl", 1, RUN_TS, CORPUS
    ) is None


def test_corrupt_json_line_skips():
    assert parse_cadence_line(
        "{not json", "cadence.jsonl", 3, RUN_TS, CORPUS
    ) is None


def test_blank_line_skips():
    assert parse_cadence_line("   ", "cadence.jsonl", 4, RUN_TS, CORPUS) is None


# --- file loading --------------------------------------------------------

def test_load_cadence_rows_reads_all_valid_lines(tmp_path):
    from ingest_cadence import load_cadence_rows

    path = tmp_path / "cadence.jsonl"
    path.write_text(make_line() + "\n" + make_line(bank_code="fr") + "\n")
    rows = load_cadence_rows(str(path), RUN_TS, CORPUS)
    assert [r[1] for r in rows] == ["us", "fr"]


def test_load_cadence_rows_skips_bad_lines_keeps_good(tmp_path):
    from ingest_cadence import load_cadence_rows

    path = tmp_path / "cadence.jsonl"
    path.write_text("{corrupt\n" + make_line() + "\n\n")
    rows = load_cadence_rows(str(path), RUN_TS, CORPUS)
    assert len(rows) == 1


# --- corpus contradiction (vault #6 bundled item, Spec W18) ----------------

import logging  # noqa: E402

from ingest_cadence import load_cadence_rows  # noqa: E402


def test_matching_corpus_field_is_accepted_and_not_copied_to_extra():
    row = parse_cadence_line(make_line(corpus=CORPUS), "cadence.jsonl", 1, RUN_TS, CORPUS)
    assert row[0] == CORPUS
    assert row[-1] is None


def test_contradicting_corpus_skips_the_line_with_a_warning(caplog):
    counters = {}
    with caplog.at_level(logging.WARNING, logger="ingest_cadence"):
        row = parse_cadence_line(make_line(corpus="company"), "cadence.jsonl", 3,
                                 RUN_TS, CORPUS, counters)
    assert row is None
    assert counters == {"corpus_conflict": 1}
    msg = caplog.records[0].getMessage()
    assert "cadence.jsonl:3" in msg and "'company'" in msg and "'central-bank'" in msg


import pytest  # noqa: E402


@pytest.mark.parametrize("value", ["", "Central-Bank", "central-bank ", 0, False, ["central-bank"]])
def test_near_miss_corpus_values_are_contradictions(value):
    # Exact comparison, as for documents: no case folding, no trimming.
    assert parse_cadence_line(make_line(corpus=value), "c", 1, RUN_TS, CORPUS) is None


def test_explicit_null_corpus_means_absent():
    # JSON null is the same as no field (resolve_corpus), as for documents.
    assert parse_cadence_line(make_line(corpus=None), "c", 1, RUN_TS, CORPUS)[0] == CORPUS


def test_load_cadence_rows_summarises_rejections(tmp_path, caplog):
    p = tmp_path / "cadence.jsonl"
    p.write_text(
        make_line(corpus="company") + "\n" + make_line(bank_code="fr") + "\n"
        + make_line(corpus="company", bank_code="de") + "\n"
    )
    with caplog.at_level(logging.WARNING, logger="ingest_cadence"):
        rows = load_cadence_rows(str(p), RUN_TS, CORPUS)
    assert [r[1] for r in rows] == ["fr"]
    assert any("Rejected 2 cadence line(s)" in r.getMessage() for r in caplog.records)
