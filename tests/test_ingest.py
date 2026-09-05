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


# --- documents-scan exclusion of cadence files ---------------------------

def test_find_jsonl_files_excludes_cadence_files(tmp_path):
    from ingest import find_jsonl_files

    (tmp_path / "us.jsonl").write_text("{}\n")
    (tmp_path / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "cadence_state.jsonl").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "sub" / "fr.jsonl").write_text("{}\n")

    found = find_jsonl_files(str(tmp_path))
    basenames = sorted(p.split("/")[-1] for p in found)
    assert basenames == ["fr.jsonl", "us.jsonl"]


def test_find_jsonl_files_skips_every_producer_root_file(tmp_path):
    # U2 — the seven root-level .jsonl files the producer writes beside
    # manifest/ must never be read as document manifests, at the root AND
    # nested (the glob is recursive).
    from ingest import EXCLUDED_BASENAMES, find_jsonl_files
    expected = {
        "cadence.jsonl", "cadence_state.jsonl", "runs.jsonl",
        "discovery_errors.jsonl", "download_errors.jsonl",
        "download_quarantine.jsonl", "wp_dates_index.jsonl",
    }
    assert EXCLUDED_BASENAMES == frozenset(expected)
    (tmp_path / "nested").mkdir()
    for name in expected:
        (tmp_path / name).write_text("{}\n")
        (tmp_path / "nested" / name).write_text("{}\n")
    (tmp_path / "manifest").mkdir()
    (tmp_path / "manifest" / "us.jsonl").write_text("{}\n")
    found = [p.rsplit("/", 1)[1] for p in find_jsonl_files(str(tmp_path))]
    assert found == ["us.jsonl"]


def test_find_jsonl_files_keeps_legacy_monolithic_manifest(tmp_path):
    # U3 — data/manifest.jsonl is the legacy single-file manifest: in scope.
    from ingest import find_jsonl_files
    (tmp_path / "manifest.jsonl").write_text("{}\n")
    assert [p.rsplit("/", 1)[1] for p in find_jsonl_files(str(tmp_path))] == ["manifest.jsonl"]


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
