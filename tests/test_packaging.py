"""Publication metadata of the two vault packages (query/, contract/).

Both are published on PyPI under the `myopenfund-` prefix; the import names
(`vault_query`, `vault_contract`) never change. Python 3.12+ (tomllib): the
py3.10 CI job does not run this file.
"""
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = [
    ("query", "myopenfund-vault-query", "0.1.0", "vault-query"),
    ("contract", "myopenfund-vault-contract", "1.1.0", "vault-contract"),
]
CREDIT = "Created and maintained by [Marc Jeulin](https://github.com/jeulinmarc)"


def _project(directory):
    return tomllib.loads((ROOT / directory / "pyproject.toml").read_text())["project"]


@pytest.mark.parametrize("directory, dist, version, old", PACKAGES)
def test_name_and_version(directory, dist, version, old):
    project = _project(directory)
    assert project["name"] == dist
    assert project["version"] == version


@pytest.mark.parametrize("directory, dist, version, old", PACKAGES)
def test_credit(directory, dist, version, old):
    project = _project(directory)
    assert {"name": "Marc Jeulin"} in project["authors"]
    assert {"name": "Marc Jeulin"} in project["maintainers"]
    assert all("email" not in person for person in project["authors"] + project["maintainers"])
    assert "myopenfund" in project["keywords"]
    assert project["urls"]["Organization"] == "https://github.com/MyOpenFund"
    assert project["readme"] == "README.md"


def test_license_copyright():
    for path in ("LICENSE", "query/LICENSE", "contract/LICENSE"):
        assert "Copyright (c) 2026 Marc Jeulin" in (ROOT / path).read_text(), path


@pytest.mark.parametrize("directory, dist, version, old", PACKAGES)
def test_readme_credit_and_migration(directory, dist, version, old):
    text = (ROOT / directory / "README.md").read_text()
    assert CREDIT in text
    assert f"pip install {dist}" in text
    assert f"pip uninstall -y {old}" in text


def test_root_readme_credit():
    assert CREDIT in (ROOT / "README.md").read_text()


@pytest.mark.parametrize("directory, dist, version, old", PACKAGES)
def test_no_git_urls(directory, dist, version, old):
    assert not [d for d in _project(directory)["dependencies"] if "git+" in d]
