import json
from datetime import datetime, timezone

from ingest_runs import parse_run_line, load_run_rows


def make_line(**overrides):
    obj = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "tool": "central-bank-corpus",
        "command": "discover",
        "started_at": "2026-08-31T02:00:00+00:00",
        "finished_at": "2026-08-31T02:10:00+00:00",
        "outcome": "ok",
        "exit_code": 0,
        "totals": {"docs_seen": 100, "docs_new": 3, "docs_failed": 0},
        "sources": [{"source_code": "us", "docs_seen": 100, "docs_new": 3,
                     "docs_failed": 0, "fetch_errors": 0, "truncated": False,
                     "error_samples": []}],
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_valid_line_parses():
    row = parse_run_line(make_line(), "runs.jsonl", 1, "central-bank")
    assert row[0] == "11111111-1111-1111-1111-111111111111"
    assert row[1] == "central-bank-corpus"
    assert row[5] == "ok" and row[6] == 0
    assert json.loads(row[7]) == {"docs_seen": 100, "docs_new": 3, "docs_failed": 0}
    assert json.loads(row[8])[0]["source_code"] == "us"
    assert row[10] is None  # no extra


def test_unknown_fields_go_to_extra():
    row = parse_run_line(make_line(host="nas"), "runs.jsonl", 1, "central-bank")
    assert json.loads(row[10]) == {"host": "nas"}


def test_missing_run_id_skips():
    line = make_line()
    obj = json.loads(line); del obj["run_id"]
    assert parse_run_line(json.dumps(obj), "runs.jsonl", 1, "central-bank") is None


def test_corrupt_and_blank_lines_skip():
    assert parse_run_line("{nope", "runs.jsonl", 2, "central-bank") is None
    assert parse_run_line("   ", "runs.jsonl", 3, "central-bank") is None


def test_load_run_rows_skips_bad_keeps_good(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text("{corrupt\n" + make_line() + "\n\n" + make_line(run_id="2" * 36) + "\n")
    rows = load_run_rows(str(p), "central-bank")
    assert [r[0] for r in rows] == ["1" * 8 + "-1111-1111-1111-" + "1" * 12, "2" * 36]


def test_runs_jsonl_excluded_from_documents_scan(tmp_path):
    from ingest import find_jsonl_files

    (tmp_path / "us.jsonl").write_text("{}\n")
    (tmp_path / "runs.jsonl").write_text("{}\n")
    found = find_jsonl_files(str(tmp_path))
    assert [f.split("/")[-1] for f in found] == ["us.jsonl"]


# --- Type-corrupt (JSON-valid, shape-invalid) lines -------------------------
#
# A JSON-decodable but type-corrupt line (e.g. exit_code as a string) must
# never reach execute_values: the batch insert would raise, roll back the
# *whole* batch (good rows included), and since runs.jsonl is append-only,
# the same bad line would re-poison every future ingestion cycle forever.


def test_type_corrupt_exit_code_skips():
    assert (
        parse_run_line(make_line(exit_code="three"), "runs.jsonl", 1, "central-bank")
        is None
    )


def test_bool_exit_code_skips():
    # bool is a subclass of int in Python; must not sneak past the int check.
    assert (
        parse_run_line(make_line(exit_code=True), "runs.jsonl", 1, "central-bank")
        is None
    )


def test_garbage_timestamp_skips():
    assert (
        parse_run_line(
            make_line(started_at="never o'clock"), "runs.jsonl", 1, "central-bank"
        )
        is None
    )


def test_sources_as_string_skips():
    assert (
        parse_run_line(make_line(sources="oops"), "runs.jsonl", 1, "central-bank")
        is None
    )


def test_type_corrupt_exit_code_line_skipped_good_line_kept(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        make_line(exit_code="three") + "\n" + make_line(run_id="2" * 36) + "\n"
    )
    rows = load_run_rows(str(p), "central-bank")
    assert [r[0] for r in rows] == ["2" * 36]


def test_garbage_timestamp_line_skipped_good_line_kept(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        make_line(started_at="never o'clock") + "\n"
        + make_line(run_id="3" * 36) + "\n"
    )
    rows = load_run_rows(str(p), "central-bank")
    assert [r[0] for r in rows] == ["3" * 36]


def test_sources_as_string_line_skipped_good_line_kept(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        make_line(sources="oops") + "\n" + make_line(run_id="4" * 36) + "\n"
    )
    rows = load_run_rows(str(p), "central-bank")
    assert [r[0] for r in rows] == ["4" * 36]


def test_corpus_absent_uses_service_default():
    row = parse_run_line(make_line(), "runs.jsonl", 1, "central-bank")
    assert row[9] == "central-bank"


def test_corpus_matching_is_accepted_and_not_in_extra():
    row = parse_run_line(
        make_line(corpus="central-bank"), "runs.jsonl", 1, "central-bank"
    )
    assert row[9] == "central-bank"
    assert row[10] is None  # U5: corpus is a column now, never extra


def test_corpus_contradiction_rejects_line_and_counts():
    counters = {"corpus_conflict": 0}
    row = parse_run_line(
        make_line(corpus="company"), "runs.jsonl", 1, "central-bank", counters
    )
    assert row is None
    assert counters["corpus_conflict"] == 1
