"""`CORPUS_ROOTS` and the file path a document's `local_path` resolves to (plan V7).

Documents contract: `local_path` is relative to the corpus data root, and the
deployment says where each corpus's root is mounted
(`CORPUS_ROOTS=central-bank=/cbdata,company=/companydata`). It replaces the
split on the `raw` path component (`RAW_DATA_DIR`). The path comes from a
manifest, so it is untrusted input reaching a filesystem read: whatever it
says, the file served must be under its corpus root once every symlink is
resolved.
"""
from pathlib import Path

import pytest

import files


# --- parse_corpus_roots ----------------------------------------------------------

def test_parses_one_root_per_corpus():
    assert files.parse_corpus_roots("central-bank=/cbdata,company=/companydata") == {
        "central-bank": Path("/cbdata"), "company": Path("/companydata")}


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_means_no_root(text):
    assert files.parse_corpus_roots(text) == {}


def test_spaces_around_entries_are_ignored():
    assert files.parse_corpus_roots(" central-bank = /cbdata , company=/co ") == {
        "central-bank": Path("/cbdata"), "company": Path("/co")}


@pytest.mark.parametrize("text, match", [
    ("central-bank", "name=/absolute/path"),
    ("=/cbdata", "corpus name"),
    ("central bank=/cbdata", "corpus name"),
    ("central-bank/x=/cbdata", "corpus name"),
    ("c" * 33 + "=/cbdata", "corpus name"),
    ("central-bank=cbdata", "absolute"),
    ("central-bank=", "absolute"),
    ("central-bank=./cbdata", "absolute"),
    ("central-bank=/cb/../etc", r"\.\."),
    ("central-bank=/cbdata,", "empty entry"),
    ("central-bank=/cbdata,,company=/co", "empty entry"),
    ("central-bank=/cbdata,central-bank=/other", "twice"),
])
def test_a_malformed_value_is_refused(text, match):
    with pytest.raises(ValueError, match=match) as exc:
        files.parse_corpus_roots(text)
    assert str(exc.value).startswith("CORPUS_ROOTS")


def test_the_corpus_name_pattern_is_the_contracts():
    """A corpus name is a contract code (vault_contract.CODE_PATTERN). The API
    image does not ship vault-contract, so the pattern is copied: drift guard."""
    from vault_contract.vocabulary import CODE_PATTERN

    assert files.CORPUS_NAME_PATTERN.pattern == CODE_PATTERN.pattern


# --- resolve_under_root ----------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    """A corpus root with one file, an inner symlink and an escaping one."""
    root = tmp_path / "cbdata"
    (root / "raw" / "us" / "C1" / "2010").mkdir(parents=True)
    (root / "raw" / "us" / "C1" / "2010" / "a.pdf").write_bytes(b"%PDF")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")
    (root / "raw" / "escape").symlink_to(outside, target_is_directory=True)
    (root / "raw" / "inner").symlink_to(root / "raw" / "us", target_is_directory=True)
    return root


def test_a_relative_path_resolves_under_the_root(root):
    assert files.resolve_under_root(root, "raw/us/C1/2010/a.pdf") == \
        (root / "raw/us/C1/2010/a.pdf").resolve()


def test_a_symlink_staying_under_the_root_is_followed(root):
    assert files.resolve_under_root(root, "raw/inner/C1/2010/a.pdf") == \
        (root / "raw/us/C1/2010/a.pdf").resolve()


@pytest.mark.parametrize("local_path", [
    "../outside/secret.txt",
    "raw/../../outside/secret.txt",
    "raw/us/../../../outside/secret.txt",
    "/etc/passwd",
    "raw/escape/secret.txt",        # no ".." at all: only resolving reveals it
    "",
    ".",
    "raw/..",
])
def test_a_path_leaving_the_root_or_naming_it_is_refused(root, local_path):
    assert files.resolve_under_root(root, local_path) is None


def test_a_root_that_is_itself_a_symlink_still_contains(tmp_path, root):
    """The NAS mounts may be symlinks: the root is resolved too, so a path
    under it is not refused for being under the link's target."""
    link = tmp_path / "cblink"
    link.symlink_to(root, target_is_directory=True)
    assert files.resolve_under_root(link, "raw/us/C1/2010/a.pdf") == \
        (root / "raw/us/C1/2010/a.pdf").resolve()


def test_a_legacy_data_prefixed_path_is_not_rewritten(root):
    """Rows ingested before the documents contract hold `data/raw/...`; the
    first post-deploy ingestion rewrites every live row, so no compatibility
    code: the path is looked up as stored (and simply not found)."""
    resolved = files.resolve_under_root(root, "data/raw/us/C1/2010/a.pdf")
    assert resolved == (root / "data/raw/us/C1/2010/a.pdf").resolve()
    assert not resolved.exists()
