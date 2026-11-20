"""`python -m vault_contract.check`: the read-only manifest checker.

Producers run it on their data root before the vault does, and an operator
runs it on a NAS copy before an ingestion; its exit code is what a script or
a CI step reads, so every path to 0, 1 and 2 is pinned here.
"""
import json
import subprocess
import sys

import pytest

from vault_contract import golden
from vault_contract.check import main


def _write(directory, name, rows):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _row(name):
    return dict(golden.accepted()[name]["row"])


def _vocabulary_file(tmp_path, corpus):
    path = tmp_path / f"vocabulary-{corpus}.json"
    path.write_text(json.dumps(golden.vocabulary(corpus)), encoding="utf-8")
    return path


def _run(capsys, *argv):
    code = main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_clean_manifests_exit_0(tmp_path, capsys):
    _write(tmp_path / "manifest", "ecb.jsonl", [_row("cb_rate_decision"), _row("cb_rendered_html")])
    code, out, _ = _run(capsys, tmp_path / "manifest", "--corpus", "central-bank",
                        "--vocabulary", _vocabulary_file(tmp_path, "central-bank"))
    assert code == 0
    assert "rows: 2 (refused 0, with notices 0)" in out
    assert "refusals: 0" in out


def test_a_refused_row_exits_1_with_its_reason_and_an_example(tmp_path, capsys):
    bad = _row("cb_rate_decision")
    bad["date_precision"] = "Day"
    _write(tmp_path, "ecb.jsonl", [_row("cb_rendered_html"), bad])
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "  bad_date_precision: 1" in out
    assert "ecb.jsonl:2 doc_id='ceb04c0aa89ed888' date_precision='Day'" in out


def test_notices_alone_exit_0_and_1_under_strict(tmp_path, capsys):
    _write(tmp_path, "ca.jsonl", [_row("cb_legacy_form")])
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 0
    assert "  empty_title: 1" in out
    assert _run(capsys, tmp_path, "--corpus", "central-bank", "--strict")[0] == 1


def test_duplicates_are_reported_as_the_vault_resolves_them_the_last_valid_line_wins(
        tmp_path, capsys):
    """The vault keeps the last valid line of a doc_id (dedupe_rows, then
    upserts in file order): every earlier occurrence is refused, pointing at
    the kept one."""
    row = _row("cb_rendered_html")
    _write(tmp_path, "a.jsonl", [row])
    _write(tmp_path, "b.jsonl", [row, row])
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "rows: 3 (refused 2, with notices 0)" in out
    assert "  duplicate_doc_id: 2" in out
    assert "a.jsonl:1 doc_id='e1e6a63950c50d3f' superseded by b.jsonl:2" in out
    assert "b.jsonl:1 doc_id='e1e6a63950c50d3f' superseded by b.jsonl:2" in out


def test_a_refused_line_is_not_a_duplicate_of_a_valid_one(tmp_path, capsys):
    row = _row("cb_rendered_html")
    bad = dict(row, date_precision="Day")
    _write(tmp_path, "a.jsonl", [row, bad])
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "rows: 2 (refused 1, with notices 0)" in out
    assert "duplicate_doc_id" not in out


def test_without_transition_a_legacy_row_is_refused(tmp_path, capsys):
    _write(tmp_path, "jp.jsonl", [_row("cb_legacy_form")])
    assert _run(capsys, tmp_path, "--corpus", "central-bank")[0] == 0
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank", "--no-transition")
    assert code == 1
    assert "missing_corpus" in out and "missing_source_code" in out
    assert "  legacy_key: 2" in out                     # bank_code and pdf_url


def test_without_transition_a_lone_pdf_url_is_refused_with_one_hint_per_key(tmp_path, capsys):
    """The row the vault would otherwise write with document_url NULL."""
    from vault_contract import legacy_key_message
    rows = []
    for doc_id in ("a1", "a2"):
        row = _row("cb_rate_decision")
        row["doc_id"] = doc_id
        row["pdf_url"] = row.pop("document_url")
        rows.append(row)
    _write(tmp_path, "ecb.jsonl", rows)
    code, out, err = _run(capsys, tmp_path, "--corpus", "central-bank", "--no-transition")
    assert code == 1
    assert "  legacy_key: 2" in out
    assert "ecb.jsonl:1 doc_id='a1' pdf_url='https://" in out
    assert err.splitlines() == [f"note: {legacy_key_message('pdf_url')}"]


@pytest.mark.parametrize("change, named", [
    (lambda r: None, "corpus_missing, legacy_alias, legacy_data_prefix"),
    (lambda r: r.update(corpus="central-bank", source_code=r.pop("bank_code"),
                        document_url=r.pop("pdf_url")), "legacy_data_prefix"),
    (lambda r: r.update(local_path=r["local_path"].removeprefix("data/")),
     "corpus_missing, legacy_alias"),
])
def test_with_transition_a_legacy_form_prints_one_stderr_hint(tmp_path, capsys, change, named):
    """The default (transition=True) accepts the pre-contract forms the vault
    refuses: one stderr line says so; stdout and the exit code are unchanged."""
    row = _row("cb_legacy_form")
    change(row)
    _write(tmp_path, "jp.jsonl", [row])
    code, _, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 0
    hints = [line for line in err.splitlines() if "--no-transition" in line]
    assert hints == [(f"note: {named} noticed: the vault ingests with transition=False and "
                      "refuses these rows; run with --no-transition to judge as it does")]


def test_the_transition_hint_is_silent_without_legacy_forms_or_with_no_transition(
        tmp_path, capsys):
    _write(tmp_path / "clean", "ecb.jsonl", [_row("cb_rate_decision")])
    code, _, err = _run(capsys, tmp_path / "clean", "--corpus", "central-bank")
    assert code == 0 and err == ""
    _write(tmp_path / "legacy", "jp.jsonl", [_row("cb_legacy_form")])
    code, out, err = _run(capsys, tmp_path / "legacy", "--corpus", "central-bank",
                          "--no-transition")
    assert code == 1
    assert "  legacy_data_prefix: 1" in out      # noticed in both modes
    assert "noticed" not in err and "beside" not in err   # only the legacy_key notes
    assert len(err.splitlines()) == 2


def test_with_transition_an_old_key_beside_its_contract_key_prints_a_stderr_hint(
        tmp_path, capsys):
    """transition=True drops an old key shadowed by its contract key without a
    notice; the vault refuses the row (legacy_key): one stderr line says so."""
    row = _row("cb_rate_decision")
    row["pdf_url"] = "https://x.invalid/stale.pdf"
    _write(tmp_path, "ecb.jsonl", [row])
    code, out, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 0 and "notices: 0" in out
    assert err.splitlines() == [(
        "note: 1 row(s) carry an old key (pdf_url) that transition=True drops without a "
        "notice (beside its contract key, or blank): the vault ingests with transition=False "
        "and refuses these rows (legacy_key); run with --no-transition to judge as it does")]


def test_the_vocabulary_checks_codes(tmp_path, capsys):
    row = _row("sec_10k")
    row["doc_type"] = "10-K"
    _write(tmp_path / "m", "x.jsonl", [row])
    assert _run(capsys, tmp_path / "m", "--corpus", "company")[0] == 0
    code, out, _ = _run(capsys, tmp_path / "m", "--corpus", "company",
                        "--vocabulary", _vocabulary_file(tmp_path, "company"))
    assert code == 1
    assert "unknown_doc_type: 1" in out


def test_glob_selects_the_files(tmp_path, capsys):
    _write(tmp_path / "sub", "a.jsonl", [_row("cb_rendered_html")])
    _write(tmp_path, "other.json", [{"not": "a manifest"}])
    assert _run(capsys, tmp_path, "--corpus", "central-bank")[0] == 2          # no *.jsonl here
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank", "--glob", "**/*.jsonl")
    assert code == 0 and "files: 1" in out


def test_lines_that_are_not_objects_are_refused_as_not_json(tmp_path, capsys):
    """The vault ingestion refuses such a line (not_json: counted, stored,
    sweep skipped, exit 1), so the checker fails on it without --strict."""
    path = _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    with path.open("a", encoding="utf-8") as f:
        f.write("not json\n[1, 2]\n\n")
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "rows: 3 (refused 2, with notices 0)" in out
    assert "  not_json: 2" in out
    assert "a.jsonl:2 line='not json'" in out
    assert "a.jsonl:3 line='[1, 2]'" in out


def test_a_missing_directory_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "nope"), "--corpus", "central-bank"])
    assert exc.value.code == 2


def test_no_matching_file_exits_2(tmp_path, capsys):
    code, _, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 2
    assert "no file matches" in err


def test_a_file_that_is_not_utf8_exits_2_and_is_named(tmp_path, capsys):
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    (tmp_path / "b.jsonl").write_bytes(b'{"title": "caf\xe9"}\n')
    code, out, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 2
    assert "b.jsonl: not UTF-8" in err
    assert "rows: 1 " in out                       # the other files are still checked


@pytest.mark.parametrize("content", ["{not json", json.dumps({"contract": "documents"})])
def test_an_invalid_vocabulary_exits_2(tmp_path, capsys, content):
    _write(tmp_path / "m", "a.jsonl", [_row("cb_rendered_html")])
    vocabulary = tmp_path / "vocabulary.json"
    vocabulary.write_text(content, encoding="utf-8")
    code, _, err = _run(capsys, tmp_path / "m", "--corpus", "central-bank",
                        "--vocabulary", vocabulary)
    assert code == 2
    assert "vocabulary" in err


def test_an_unreadable_vocabulary_exits_2(tmp_path, capsys):
    _write(tmp_path / "m", "a.jsonl", [_row("cb_rendered_html")])
    code, _, _ = _run(capsys, tmp_path / "m", "--corpus", "central-bank",
                      "--vocabulary", tmp_path / "absent.json")
    assert code == 2


def test_a_vocabulary_of_another_corpus_exits_2(tmp_path, capsys):
    _write(tmp_path / "m", "a.jsonl", [_row("cb_rendered_html")])
    code, _, err = _run(capsys, tmp_path / "m", "--corpus", "central-bank",
                        "--vocabulary", _vocabulary_file(tmp_path, "company"))
    assert code == 2
    assert "company" in err


def test_runs_as_a_module(tmp_path):
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    out = subprocess.run([sys.executable, "-m", "vault_contract.check", str(tmp_path),
                          "--corpus", "central-bank"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "rows: 1 (refused 0, with notices 0)" in out.stdout



# --- second fix wave: lines, encodings, errors --------------------------------

def test_lines_are_split_as_the_ingestion_splits_them(tmp_path, capsys):
    """Only \n, \r\n and \r end a line (a text-mode file object), never the
    Unicode separators str.splitlines also splits on."""
    rows = [dict(_row("cb_rendered_html"), doc_id=f"id{i}", title=t)
            for i, t in enumerate(["a b", "c\u0085d", "e f", "g\x1ch"])]
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    (tmp_path / "a.jsonl").write_bytes(
        (lines[0] + "\r\n" + lines[1] + "\r" + lines[2] + "\n" + lines[3] + "\n").encode("utf-8"))
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 0, out
    assert "rows: 4 (refused 0, with notices 0)" in out
    assert "not_json" not in out


def test_a_utf8_bom_exits_2(tmp_path, capsys):
    """The ingestion reads with encoding="utf-8": a BOM glues to line 1, which
    json.loads then rejects, so that row would be refused as not_json."""
    (tmp_path / "a.jsonl").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(_row("cb_rendered_html")).encode() + b"\n")
    code, _, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 2
    assert "a.jsonl: starts with a UTF-8 BOM" in err


@pytest.mark.parametrize("pattern", ["", "/abs/*.jsonl"])
def test_a_bad_glob_exits_2(tmp_path, capsys, pattern):
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    code, _, err = _run(capsys, tmp_path, "--corpus", "central-bank", "--glob", pattern)
    assert code == 2
    assert "glob" in err


def test_a_file_that_cannot_be_read_exits_2(tmp_path, capsys, monkeypatch):
    from pathlib import Path
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    _write(tmp_path, "b.jsonl", [_row("cb_rate_decision")])
    real = Path.read_bytes

    def flaky(self):
        if self.name == "b.jsonl":
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    code, out, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 2
    assert "b.jsonl: cannot be read" in err
    assert "rows: 1 " in out


def test_deep_nesting_and_huge_ints_are_not_json(tmp_path, capsys):
    path = _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    with path.open("a", encoding="utf-8") as f:
        f.write("[" * 200_000 + "\n")
        f.write('{"doc_id": ' + "9" * 5000 + "}\n")
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "  not_json: 2" in out
    # The example shows the line's start, not a 200,000-character repr.
    example = next(line for line in out.splitlines() if "a.jsonl:2 line=" in line)
    assert len(example) < 120



def test_no_rows_at_all_exits_2(tmp_path, capsys):
    (tmp_path / "a.jsonl").write_text("\n[1]\n  \nnot json\n", encoding="utf-8")
    code, _, err = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 2
    assert "no rows" in err


def test_an_empty_corpus_is_a_usage_error(tmp_path, capsys):
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), "--corpus", ""])
    assert exc.value.code == 2


def test_bad_extra_is_reported_and_an_unprintable_key_shown_as_its_repr(tmp_path, capsys):
    rows = [dict(_row("cb_rendered_html"), doc_id="a", note=float("nan")),
            dict(_row("cb_rendered_html"), doc_id="b", **{"k\udfff": 1})]
    (tmp_path / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows),
                                      encoding="utf-8")
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank")
    assert code == 1
    assert "  bad_extra: 2" in out
    assert "a.jsonl:1 doc_id='a' note=nan" in out
    assert "a.jsonl:2 doc_id='b' 'k\\udfff'=1" in out


def test_files_are_read_in_the_ingestions_order_plain_string_sort(tmp_path, capsys):
    """The ingestion sorts the file paths as strings: "a-b/x" before "a/x"
    ('-' < '/'), where Path ordering (by parts) puts "a/x" first. The last
    valid line of a doc_id wins, so the order decides which one is kept."""
    _write(tmp_path / "a", "x.jsonl", [_row("cb_rendered_html")])
    _write(tmp_path / "a-b", "x.jsonl", [_row("cb_rendered_html")])
    code, out, _ = _run(capsys, tmp_path, "--corpus", "central-bank", "--glob", "**/*.jsonl")
    assert code == 1
    assert "  duplicate_doc_id: 1" in out
    (example,) = [l for l in out.splitlines() if "superseded by" in l]
    assert example.strip().startswith("a-b/x.jsonl:1") and example.endswith("superseded by a/x.jsonl:1")


@pytest.mark.parametrize("corpus", ["central bank", "c" * 33, "cb;x"])
def test_a_corpus_that_is_not_a_code_is_a_usage_error(tmp_path, capsys, corpus):
    _write(tmp_path, "a.jsonl", [_row("cb_rendered_html")])
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), "--corpus", corpus])
    assert exc.value.code == 2
    assert "--corpus" in capsys.readouterr().err


def test_producer_files_and_hidden_files_are_skipped_as_the_ingestion_skips_them(
        tmp_path, capsys):
    """`--glob '**/*.jsonl'` over a manifest directory also matches the
    producer's audit files and hidden files; the ingestion never reads them
    as manifests (EXCLUDED_BASENAMES; glob skips dot files), nor does the
    checker."""
    from vault_contract.check import EXCLUDED_BASENAMES
    _write(tmp_path / "m", "ecb.jsonl", [_row("cb_rate_decision")])
    for name in EXCLUDED_BASENAMES:
        (tmp_path / "m" / "sub").mkdir(parents=True, exist_ok=True)
        (tmp_path / "m" / name).write_text('{"not": "a row"}\n', encoding="utf-8")
        (tmp_path / "m" / "sub" / name).write_text('{"not": "a row"}\n', encoding="utf-8")
    (tmp_path / "m" / ".partial.jsonl").write_text("{torn\n", encoding="utf-8")
    (tmp_path / "m" / ".git" / "x.jsonl").parent.mkdir()
    (tmp_path / "m" / ".git" / "x.jsonl").write_text("{torn\n", encoding="utf-8")
    code, out, _ = _run(capsys, tmp_path / "m", "--corpus", "central-bank",
                        "--glob", "**/*.jsonl")
    assert code == 0, out
    assert "files: 1" in out and "rows: 1 (refused 0, with notices 0)" in out


def test_the_excluded_basenames_are_the_producer_files():
    from vault_contract.check import EXCLUDED_BASENAMES
    assert EXCLUDED_BASENAMES == frozenset({
        "cadence.jsonl", "cadence_state.jsonl", "runs.jsonl", "discovery_errors.jsonl",
        "download_errors.jsonl", "download_quarantine.jsonl", "wp_dates_index.jsonl"})
