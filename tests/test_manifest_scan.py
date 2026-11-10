"""Which .jsonl files the documents pass reads (Spec W5).

Two real layouts:
- repo compose: DATA_DIR is the corpus data/ ROOT -- manifest/ beside the
  legacy root manifest.jsonl and every producer file (cadence.jsonl,
  runs.jsonl, ...). Root mode: allowlist manifest/**/*.jsonl + manifest.jsonl.
- NAS: DATA_DIR IS the manifest directory itself (the producer files come
  from *_PATH variables on another mount). Manifest-dir mode: **/*.jsonl,
  i.e. exactly what the scan found before this change.
MANIFEST_GLOBS (comma-separated, relative to DATA_DIR) overrides both.
"""
import logging
import os

import pytest

from ingest import ManifestGlobsError, find_manifest_files

PRODUCER_ROOT_FILES = (
    "cadence.jsonl", "cadence_state.jsonl", "runs.jsonl",
    "discovery_errors.jsonl", "download_errors.jsonl",
    "download_quarantine.jsonl", "wp_dates_index.jsonl",
)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _rel(found, root):
    return [os.path.relpath(p, root) for p in found]


@pytest.fixture()
def repo_layout(tmp_path):
    """The corpus data/ root as the repo compose mounts it."""
    root = tmp_path / "data"
    _touch(root / "manifest" / "us.jsonl")
    _touch(root / "manifest" / "ecb" / "D1.jsonl")
    _touch(root / "manifest.jsonl")  # legacy monolithic manifest: in scope
    for name in PRODUCER_ROOT_FILES:
        _touch(root / name)
    _touch(root / "foo.jsonl")  # unknown future producer file
    _touch(root / "raw" / "us" / "stray.jsonl")  # nothing outside manifest/
    return root


@pytest.fixture()
def nas_layout(tmp_path):
    """DATA_DIR = the manifest directory itself (the NAS mount)."""
    root = tmp_path / "manifest-mount"
    _touch(root / "us.jsonl")
    _touch(root / "fr.jsonl")
    _touch(root / "sub" / "ecb.jsonl")
    return root


def test_root_layout_reads_only_the_allowlist(repo_layout, caplog):
    with caplog.at_level(logging.INFO, logger="ingest"):
        found = find_manifest_files(str(repo_layout), {})
    assert _rel(found, repo_layout) == [
        "manifest.jsonl", os.path.join("manifest", "ecb", "D1.jsonl"),
        os.path.join("manifest", "us.jsonl"),
    ]
    assert any("root layout" in r.getMessage() for r in caplog.records)


def test_root_layout_ignores_every_producer_file_and_unknown_root_files(repo_layout):
    names = {os.path.basename(p) for p in find_manifest_files(str(repo_layout), {})}
    assert names.isdisjoint(PRODUCER_ROOT_FILES)
    assert "foo.jsonl" not in names and "stray.jsonl" not in names


def test_root_layout_never_reads_producer_files_under_manifest(tmp_path):
    # Parity with the pre-W5 scan: a producer file that lands inside
    # manifest/ (at any depth) is still not a manifest.
    _touch(tmp_path / "manifest" / "us.jsonl")
    for name in PRODUCER_ROOT_FILES:
        _touch(tmp_path / "manifest" / name)
        _touch(tmp_path / "manifest" / "deep" / name)
    assert _rel(find_manifest_files(str(tmp_path), {}), tmp_path) == [
        os.path.join("manifest", "us.jsonl")]


def test_root_layout_warns_about_unknown_root_jsonl_files(repo_layout, caplog):
    # foo.jsonl sits at the data/ root: not read, but named in a WARNING so a
    # producer that writes a manifest to the wrong place is noticed. Known
    # producer files and the legacy manifest.jsonl are not mentioned.
    _touch(repo_layout / "bar.jsonl")
    with caplog.at_level(logging.WARNING, logger="ingest"):
        find_manifest_files(str(repo_layout), {})
    (msg,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "not read" in msg and "2 .jsonl file(s)" in msg
    assert msg.rsplit(": ", 1)[1] == "bar.jsonl, foo.jsonl"  # the named list, nothing else


def test_root_layout_without_unknown_root_files_is_quiet(tmp_path, caplog):
    _touch(tmp_path / "manifest" / "us.jsonl")
    _touch(tmp_path / "manifest.jsonl")
    for name in PRODUCER_ROOT_FILES:
        _touch(tmp_path / name)
    with caplog.at_level(logging.WARNING, logger="ingest"):
        find_manifest_files(str(tmp_path), {})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_root_layout_without_legacy_manifest(tmp_path):
    _touch(tmp_path / "manifest" / "us.jsonl")
    _touch(tmp_path / "cadence.jsonl")
    assert _rel(find_manifest_files(str(tmp_path), {}), tmp_path) == [
        os.path.join("manifest", "us.jsonl")]


def test_manifest_dir_layout_reads_everything_recursively(nas_layout, caplog):
    with caplog.at_level(logging.INFO, logger="ingest"):
        found = find_manifest_files(str(nas_layout), {})
    assert _rel(found, nas_layout) == [
        "fr.jsonl", os.path.join("sub", "ecb.jsonl"), "us.jsonl"]
    assert any("manifest-dir layout" in r.getMessage() for r in caplog.records)


def test_manifest_dir_layout_keeps_the_previous_scan_exactly(nas_layout):
    # Parity with the pre-W5 scan on the NAS: **/*.jsonl minus the producer
    # basenames it already skipped (a producer file that strays into the
    # manifest mount is still not read as a manifest).
    for name in PRODUCER_ROOT_FILES:
        _touch(nas_layout / name)
    _touch(nas_layout / "manifest.jsonl")
    found = {os.path.basename(p) for p in find_manifest_files(str(nas_layout), {})}
    assert found == {"us.jsonl", "fr.jsonl", "ecb.jsonl", "manifest.jsonl"}


def test_manifest_that_is_a_file_is_not_the_root_layout(tmp_path, caplog):
    # A regular FILE named "manifest" does not make DATA_DIR the data/ root.
    _touch(tmp_path / "manifest")
    _touch(tmp_path / "us.jsonl")
    with caplog.at_level(logging.WARNING, logger="ingest"):
        found = find_manifest_files(str(tmp_path), {})
    assert _rel(found, tmp_path) == ["us.jsonl"]
    assert any("not a directory" in r.getMessage() for r in caplog.records)


def test_symlinked_manifest_dir_is_the_root_layout(tmp_path):
    real = tmp_path / "elsewhere"
    _touch(real / "us.jsonl")
    _touch(real / "deep" / "fr.jsonl")
    root = tmp_path / "data"
    root.mkdir()
    (root / "manifest").symlink_to(real, target_is_directory=True)
    _touch(root / "cadence.jsonl")
    found = _rel(find_manifest_files(str(root), {}), root)
    assert found == [os.path.join("manifest", "deep", "fr.jsonl"),
                     os.path.join("manifest", "us.jsonl")]


def test_dangling_manifest_symlink_falls_back_to_manifest_dir_layout(tmp_path, caplog):
    (tmp_path / "manifest").symlink_to(tmp_path / "gone", target_is_directory=True)
    _touch(tmp_path / "us.jsonl")
    with caplog.at_level(logging.WARNING, logger="ingest"):
        found = find_manifest_files(str(tmp_path), {})
    assert _rel(found, tmp_path) == ["us.jsonl"]
    assert any("not a directory" in r.getMessage() for r in caplog.records)


def test_empty_match_is_logged(tmp_path, caplog):
    (tmp_path / "manifest").mkdir()
    _touch(tmp_path / "cadence.jsonl")
    with caplog.at_level(logging.INFO, logger="ingest"):
        assert find_manifest_files(str(tmp_path), {}) == []
    assert any("0 file(s)" in r.getMessage() for r in caplog.records)


def test_missing_data_dir_matches_nothing(tmp_path):
    assert find_manifest_files(str(tmp_path / "absent"), {}) == []


def test_a_directory_named_like_a_manifest_is_skipped(tmp_path, caplog):
    (tmp_path / "manifest" / "weird.jsonl").mkdir(parents=True)
    _touch(tmp_path / "manifest" / "us.jsonl")
    with caplog.at_level(logging.WARNING, logger="ingest"):
        found = find_manifest_files(str(tmp_path), {})
    assert _rel(found, tmp_path) == [os.path.join("manifest", "us.jsonl")]
    assert any("weird.jsonl" in r.getMessage() for r in caplog.records)


def test_hidden_files_stay_out_as_before(tmp_path):
    _touch(tmp_path / "manifest" / ".partial.jsonl")  # e.g. an rsync temp file
    _touch(tmp_path / "manifest" / "us.jsonl")
    assert _rel(find_manifest_files(str(tmp_path), {}), tmp_path) == [
        os.path.join("manifest", "us.jsonl")]


# --- MANIFEST_GLOBS override ---------------------------------------------

def test_manifest_globs_override_both_layouts(repo_layout, caplog):
    env = {"MANIFEST_GLOBS": "manifest/us.jsonl, foo.jsonl"}
    with caplog.at_level(logging.INFO, logger="ingest"):
        found = find_manifest_files(str(repo_layout), env)
    assert _rel(found, repo_layout) == ["foo.jsonl", os.path.join("manifest", "us.jsonl")]
    assert any("MANIFEST_GLOBS" in r.getMessage() for r in caplog.records)


def test_manifest_globs_are_taken_literally(repo_layout):
    # An explicit override is the operator's word: no producer-file filter.
    found = find_manifest_files(str(repo_layout), {"MANIFEST_GLOBS": "*.jsonl"})
    assert "cadence.jsonl" in {os.path.basename(p) for p in found}


def test_manifest_globs_recursive_and_overlapping_patterns_dedupe(nas_layout):
    env = {"MANIFEST_GLOBS": "**/*.jsonl,us.jsonl,sub/*.jsonl"}
    assert _rel(find_manifest_files(str(nas_layout), env), nas_layout) == [
        "fr.jsonl", os.path.join("sub", "ecb.jsonl"), "us.jsonl"]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_manifest_globs_means_detection(repo_layout, value):
    found = find_manifest_files(str(repo_layout), {"MANIFEST_GLOBS": value})
    assert len(found) == 3


@pytest.mark.parametrize("value", [
    "a.jsonl,,b.jsonl", "a.jsonl,", ",", "/etc/*.jsonl", "../other/*.jsonl",
    "manifest/../../x.jsonl",
])
def test_bad_manifest_globs_are_refused(repo_layout, value):
    with pytest.raises(ManifestGlobsError) as exc:
        find_manifest_files(str(repo_layout), {"MANIFEST_GLOBS": value})
    assert "MANIFEST_GLOBS" in str(exc.value)


def _pre_w5_scan(root):
    """The scan as it was before W5 (find_jsonl_files), kept verbatim as the
    parity oracle for the manifest-dir layout."""
    import glob
    from ingest import EXCLUDED_BASENAMES
    return sorted(
        p for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
        if os.path.basename(p) not in EXCLUDED_BASENAMES
    )


def test_manifest_dir_layout_parity_with_the_pre_w5_scan(nas_layout):
    # A busy NAS-like manifest mount: nested sources, the legacy file, stray
    # producer files at several depths, non-.jsonl noise, hidden files.
    for rel in ("us/C1.jsonl", "us/C1/2010.jsonl", "a/b/c/d.jsonl", "manifest.jsonl",
                "runs.jsonl", "deep/cadence.jsonl", "notes.txt", "x.jsonl.bak",
                ".hidden.jsonl", ".git/objects.jsonl", "UPPER.JSONL"):
        _touch(nas_layout / rel)
    # A file that cannot be read must stay IN the list: open() then fails
    # the run before the sweep, instead of its rows being soft-deleted.
    (nas_layout / "dangling.jsonl").symlink_to(nas_layout / "vanished.jsonl")
    assert find_manifest_files(str(nas_layout), {}) == _pre_w5_scan(str(nas_layout))
    assert str(nas_layout / "dangling.jsonl") in find_manifest_files(str(nas_layout), {})


@pytest.mark.parametrize("layout", ["nas_layout", "repo_layout"])
def test_a_dangling_symlink_stays_in_the_list(layout, request):
    root = request.getfixturevalue(layout)
    where = root if layout == "nas_layout" else root / "manifest"
    (where / "gone.jsonl").symlink_to(root / "nowhere.jsonl")
    assert str(where / "gone.jsonl") in find_manifest_files(str(root), {})


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file anyway")
def test_an_unreadable_file_stays_in_the_list(nas_layout):
    locked = _touch(nas_layout / "locked.jsonl")
    locked.chmod(0)
    try:
        found = find_manifest_files(str(nas_layout), {})
        assert str(locked) in found
        assert found == _pre_w5_scan(str(nas_layout))
    finally:
        locked.chmod(0o644)
