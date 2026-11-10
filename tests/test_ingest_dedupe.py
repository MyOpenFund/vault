"""vault #6: per-file dedupe of manifest rows by doc_id (Spec W16)."""
import logging

from ingest import DUPLICATE_SAMPLE_SIZE, dedupe_rows


def _row(doc_id, tag):
    # Only position 0 (doc_id) matters to dedupe_rows; `tag` tells copies apart.
    return (doc_id, tag)


def test_no_duplicates_is_identity_and_silent(caplog):
    rows = [_row("a", 1), _row("b", 1), _row("c", 1)]
    with caplog.at_level(logging.WARNING, logger="ingest"):
        assert dedupe_rows(rows, "f.jsonl") == rows
    assert not caplog.records


def test_empty_input():
    assert dedupe_rows([], "f.jsonl") == []


def test_last_copy_wins_in_order_of_last_occurrence():
    rows = [_row("a", 1), _row("b", 1), _row("a", 2), _row("c", 1), _row("b", 2)]
    assert dedupe_rows(rows, "f.jsonl") == [
        _row("a", 2), _row("c", 1), _row("b", 2)]


def test_three_copies_keep_the_third():
    rows = [_row("a", 1), _row("a", 2), _row("a", 3)]
    assert dedupe_rows(rows, "f.jsonl") == [_row("a", 3)]


def test_warning_counts_dropped_lines_and_names_the_ids(caplog):
    rows = [_row("a", 1), _row("a", 2), _row("a", 3), _row("b", 1), _row("b", 2)]
    with caplog.at_level(logging.WARNING, logger="ingest"):
        dedupe_rows(rows, "path/us.jsonl")
    (record,) = caplog.records
    msg = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "path/us.jsonl" in msg
    assert "3 duplicate line(s)" in msg and "2 doc_id(s)" in msg
    assert "'a'" in msg and "'b'" in msg


def test_sample_is_capped(caplog):
    assert DUPLICATE_SAMPLE_SIZE == 5
    rows = [_row(f"id{i}", t) for i in range(8) for t in (1, 2)]
    with caplog.at_level(logging.WARNING, logger="ingest"):
        kept = dedupe_rows(rows, "f.jsonl")
    assert kept == [_row(f"id{i}", 2) for i in range(8)]
    msg = caplog.records[0].getMessage()
    assert "8 doc_id(s)" in msg
    assert sum(f"'id{i}'" in msg for i in range(8)) == 5
    assert "'id5'" not in msg  # the first five repeated ids, in first-seen order


def test_sample_size_is_a_parameter(caplog):
    rows = [_row("a", 1), _row("a", 2), _row("b", 1), _row("b", 2)]
    with caplog.at_level(logging.WARNING, logger="ingest"):
        dedupe_rows(rows, "f.jsonl", sample_size=1)
    msg = caplog.records[0].getMessage()
    assert "'a'" in msg and "'b'" not in msg


def test_ids_differing_only_in_case_or_whitespace_are_distinct():
    # doc_id is TEXT UNIQUE: Postgres compares bytes, so must we.
    rows = [_row("A", 1), _row("a", 1), _row("a ", 1)]
    assert dedupe_rows(rows, "f.jsonl") == rows
