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
