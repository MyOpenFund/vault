"""Publication metadata of the two vault packages (query/, contract/).

Both are published on PyPI under the `myopenfund-` prefix; the import names
(`vault_query`, `vault_contract`) never change. Python 3.12+ (tomllib): the
py3.10 CI job does not run this file.
"""
import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = [
    ("query", "myopenfund-vault-query", "0.1.0", "vault-query"),
    ("contract", "myopenfund-vault-contract", "1.1.0", "vault-contract"),
]
CREDIT = (
    "Created and maintained by [Marc Jeulin](https://github.com/jeulinmarc), "
    "part of [MyOpenFund](https://github.com/MyOpenFund)."
)
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


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
    _assert_credit_under_title(text, dist)
    assert f"pip install {dist}" in text
    assert f"pip uninstall -y {old}" in text


def _assert_credit_under_title(text, title):
    lines = text.splitlines()
    assert lines[0] == f"# {title}"
    assert [line for line in lines[1:] if line.strip()][0] == CREDIT


def test_root_readme_credit():
    _assert_credit_under_title((ROOT / "README.md").read_text(), "vault")


@pytest.mark.parametrize(
    "directory, versions",
    [("query", ["3.12", "3.13"]), ("contract", ["3.10", "3.11", "3.12", "3.13"])],
)
def test_python_classifiers(directory, versions):
    classifiers = _project(directory)["classifiers"]
    assert "Programming Language :: Python :: 3" in classifiers
    for version in versions:
        assert f"Programming Language :: Python :: {version}" in classifiers


def _workflow(path):
    # PyYAML reads the bare key `on` as the boolean True.
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_release_workflow_publishes_through_environment():
    workflow = _workflow(RELEASE)
    assert workflow["permissions"] == {}
    assert workflow["on"]["push"]["tags"] == ["query-v*", "contract-v*"]
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == (
        "${{ github.event_name == 'workflow_dispatch' && 'testpypi' || 'pypi' }}"
    )
    assert publish["permissions"] == {"id-token": "write"}
    with_id_token = [
        name for name, job in workflow["jobs"].items() if "id-token" in job.get("permissions", {})
    ]
    assert with_id_token == ["publish"]
    assert publish["needs"] in ("build", ["build"])


def test_actions_pinned_by_sha():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text())
    texts = [RELEASE.read_text(), yaml.safe_dump(ci["jobs"]["package"])]
    for text in texts:
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.M)
        assert uses
        for ref in uses:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_tag_check_present():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    checks = [
        i for i, s in enumerate(steps) if "tag_version" in s.get("run", "") and "exit 1" in s["run"]
    ]
    assert len(checks) == 1
    assert steps[checks[0]]["if"] == "github.event_name == 'push'"
    upload = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload and checks[0] < upload[0]


def test_dependabot_covers_github_actions_only():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert [(u["package-ecosystem"], u["schedule"]["interval"]) for u in config["updates"]] == [
        ("github-actions", "weekly")
    ]


def test_ci_has_package_job():
    ci = _workflow(ROOT / ".github" / "workflows" / "tests.yml")
    assert ci["jobs"]["package"]["strategy"]["matrix"]["package"] == ["query", "contract"]
    assert {"tests", "contract-py310"} <= set(ci["jobs"])


@pytest.mark.parametrize("directory, dist, version, old", PACKAGES)
def test_no_git_urls(directory, dist, version, old):
    assert not [d for d in _project(directory)["dependencies"] if "git+" in d]


def test_testpypi_publish_skips_existing_but_pypi_does_not():
    steps = {s["name"]: s for s in _workflow(RELEASE)["jobs"]["publish"]["steps"] if "name" in s}
    assert steps["Publish to TestPyPI"]["with"]["skip-existing"] is True
    assert "skip-existing" not in steps["Publish to PyPI"].get("with", {})


def test_concurrency_group_is_per_package():
    group = _workflow(RELEASE)["concurrency"]["group"]
    assert group == "release-${{ github.ref }}-${{ inputs.package || 'tag' }}"


def test_build_checks_tag_is_on_default_branch():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    checks = [i for i, s in enumerate(steps) if "merge-base --is-ancestor" in s.get("run", "")]
    assert len(checks) == 1
    assert steps[checks[0]]["if"] == "github.event_name == 'push'"
    assert steps[0]["with"]["fetch-depth"] == 0
    upload = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload and checks[0] < upload[0]


def test_github_release_is_push_only_and_verifies_tag():
    job = _workflow(RELEASE)["jobs"]["github-release"]
    assert job["if"] == "github.event_name == 'push'"
    assert any("--verify-tag" in s.get("run", "") for s in job["steps"])


@pytest.mark.parametrize("directory", ["query", "contract"])
def test_readme_latest_main_and_cross_repo(directory):
    text = (ROOT / directory / "README.md").read_text()
    assert "git+https://github.com/MyOpenFund/vault#subdirectory=" + directory in text
    assert "Working across repos" in text
    assert "](#" not in text
