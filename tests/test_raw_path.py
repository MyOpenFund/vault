import os

os.environ.setdefault("RAW_DATA_DIR", "/data/raw")

from main import resolve_raw_relative


def test_standard_manifest_path():
    assert (
        str(resolve_raw_relative("data/raw/us/C1/2010/abc.pdf"))
        == "us/C1/2010/abc.pdf"
    )


def test_path_without_raw_component_is_rejected():
    assert resolve_raw_relative("data/other/us/abc.pdf") is None


def test_raw_as_substring_of_another_dir_is_not_matched():
    # "rawdata" contains "raw" as a substring but is NOT the raw/ directory:
    # naive split("raw/") would mis-resolve this.
    assert resolve_raw_relative("data/rawdata/raw/us/abc.pdf") == \
        resolve_raw_relative("data/x/raw/us/abc.pdf")


def test_traversal_components_are_rejected():
    assert resolve_raw_relative("data/raw/../secrets.txt") is None
    assert resolve_raw_relative("data/raw/us/../../../../etc/passwd") is None


def test_empty_remainder_is_rejected():
    assert resolve_raw_relative("data/raw/") is None
    assert resolve_raw_relative("data/raw") is None


def test_absolute_manifest_path_still_resolves():
    assert (
        str(resolve_raw_relative("/some/mount/data/raw/us/C1/abc.pdf"))
        == "us/C1/abc.pdf"
    )
