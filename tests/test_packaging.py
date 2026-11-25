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
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = [
    ("query", "myopenfund-vault-query", "vault-query"),
    ("contract", "myopenfund-vault-contract", "vault-contract"),
]
CREDIT = (
    "Created and maintained by [Marc Jeulin](https://github.com/jeulinmarc), "
    "part of [MyOpenFund](https://github.com/MyOpenFund)."
)
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _project(directory):
    return tomllib.loads((ROOT / directory / "pyproject.toml").read_text())["project"]


@pytest.mark.parametrize("directory, dist, old", PACKAGES)
def test_name_and_version(directory, dist, old):
    project = _project(directory)
    assert project["name"] == dist
    assert str(Version(project["version"])) == project["version"]  # static, valid PEP 440


@pytest.mark.parametrize("directory, dist, old", PACKAGES)
def test_installed_version_matches_pyproject(directory, dist, old):
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version(dist)
    except PackageNotFoundError:
        pytest.skip(f"{dist} is not installed")
    assert installed == _project(directory)["version"]


@pytest.mark.parametrize("directory, dist, old", PACKAGES)
def test_credit(directory, dist, old):
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


@pytest.mark.parametrize("directory, dist, old", PACKAGES)
def test_readme_credit_and_migration(directory, dist, old):
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
        "${{ (github.event_name == 'workflow_dispatch' && inputs.target == 'testpypi')"
        " && 'testpypi' || 'pypi' }}"
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


@pytest.mark.parametrize("directory, dist, old", PACKAGES)
def test_no_git_urls(directory, dist, old):
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
    checkout = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout[0]["with"]["fetch-depth"] == 0
    upload = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload and checks[0] < upload[0]


def test_dispatch_inputs():
    inputs = _workflow(RELEASE)["on"]["workflow_dispatch"]["inputs"]
    assert inputs["target"]["type"] == "choice"
    assert inputs["target"]["options"] == ["pypi", "testpypi"]
    assert inputs["target"]["default"] == "pypi"
    assert inputs["target"]["required"] is True
    assert inputs["package"]["options"] == ["query", "contract"]


def test_build_dispatch_runs_on_default_branch_only():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    guard = [s for s in steps if "refs/heads/master" in s.get("run", "")]
    assert len(guard) == 1
    assert guard[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert "exit 1" in guard[0]["run"]
    assert steps.index(guard[0]) == 0


def test_build_computes_tag_and_refuses_an_existing_pypi_tag():
    job = _workflow(RELEASE)["jobs"]["build"]
    step = [s for s in job["steps"] if s.get("id") == "tag"]
    assert len(step) == 1
    run = step[0]["run"]
    assert step[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert "importlib.metadata" in run
    assert '"$PKG_DIR-v$version"' in run
    assert 'git ls-remote --tags origin "refs/tags/$tag"' in run
    assert '"$INPUT_TARGET" = "pypi"' in run
    assert "already exists" in run and "exit 1" in run
    upload = [i for i, s in enumerate(job["steps"]) if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert job["steps"].index(step[0]) < upload[0]
    assert job["outputs"]["tag"] == "${{ steps.tag.outputs.tag || github.ref_name }}"
    assert job["outputs"]["sha"] == "${{ github.sha }}"


def _release_job():
    job = _workflow(RELEASE)["jobs"]["github-release"]
    assert job["if"] == (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && inputs.target == 'pypi')"
    )
    assert job["permissions"] == {"contents": "write"}
    assert set(job["needs"]) == {"build", "publish"}
    return job


def _assert_tag_created_and_pushed(run):
    # no personal address in a public workflow: the tagger email is the actor's noreply one
    assert 'user.name="Marc Jeulin"' in run
    assert 'user.email="${GITHUB_ACTOR_ID}+${GITHUB_ACTOR}@users.noreply.github.com"' in run
    assert 'tag -a "$TAG"' in run and '"$SHA"' in run
    assert 'git push origin "refs/tags/$TAG"' in run


def test_github_release_creates_and_pushes_the_tag_on_dispatch_not_testpypi():
    job = _release_job()
    checkout = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert checkout[0]["with"]["persist-credentials"] is True
    assert checkout[0]["with"]["ref"] == "${{ needs.build.outputs.sha }}"
    tag = [s for s in job["steps"] if "git push" in s.get("run", "")]
    assert len(tag) == 1
    assert tag[0]["if"] == "github.event_name == 'workflow_dispatch'"
    _assert_tag_created_and_pushed(tag[0]["run"])


def test_dispatch_tag_step_is_idempotent():
    run = next(s for s in _release_job()["steps"] if "git push" in s.get("run", ""))["run"]
    lookup = run.index('git ls-remote origin "refs/tags/$TAG" "refs/tags/$TAG^{}"')
    assert lookup < run.index("git push")
    assert '"$remote_sha" = "$SHA"' in run  # already there at the built commit: skip
    assert "already exists" in run and "exit 1" in run  # there, but elsewhere: fail
    assert '[ -z "$remote_sha" ]' in run  # absent: create


def test_github_release_is_created_or_completed_on_both_paths():
    job = _release_job()
    release = [s for s in job["steps"] if "gh release create" in s.get("run", "")]
    assert len(release) == 1
    assert "if" not in release[0]
    run = release[0]["run"]
    assert 'gh release view "$TAG"' in run
    assert 'gh release upload "$TAG" dist/* --repo "$GITHUB_REPOSITORY" --clobber' in run
    assert '--target "$SHA"' in run and '--title "$TAG"' in run
    assert "--verify-tag" in run
    assert "--generate-notes" in run




def test_testpypi_only_for_testpypi_target():
    steps = {s["name"]: s for s in _workflow(RELEASE)["jobs"]["publish"]["steps"] if "name" in s}
    assert "inputs.target == 'testpypi'" in steps["Publish to TestPyPI"]["if"]
    assert "inputs.target == 'testpypi'" in steps["Publish to PyPI"]["if"]
    assert steps["Publish to PyPI"]["if"].startswith("${{ !(")


@pytest.mark.parametrize("directory", ["query", "contract"])
def test_readme_latest_main_and_cross_repo(directory):
    text = (ROOT / directory / "README.md").read_text()
    assert "git+https://github.com/MyOpenFund/vault#subdirectory=" + directory in text
    assert "Working across repos" in text
    assert "](#" not in text


# ---- fail-closed approval gate (release hardening) -------------------------


def _gate_job():
    job = _workflow(RELEASE)["jobs"]["build"]
    gate = [i for i, s in enumerate(job["steps"]) if s.get("name") == "Check the approval gate"]
    assert len(gate) == 1
    return job, gate[0]


def test_release_checks_the_approval_gate_before_building():
    job, gate = _gate_job()
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    step = job["steps"][gate]
    run = step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "inputs.target == 'testpypi'" in step["env"]["ENV_NAME"]
    assert "'testpypi'" in step["env"]["ENV_NAME"] and "'pypi'" in step["env"]["ENV_NAME"]
    assert 'gh api "repos/$GITHUB_REPOSITORY/environments/$ENV_NAME"' in run
    assert "if ! " in run  # an API failure (non-200) is tested, not ignored
    assert "required_reviewers" in run and "exit 1" in run
    for i, other in enumerate(job["steps"]):
        uses = other.get("uses", "")
        building = uses.startswith(
            ("actions/checkout@", "actions/setup-python@", "actions/upload-artifact@")
        ) or "build" in other.get("run", "")
        if i != gate and (building or other.get("name", "").startswith("Build")):
            assert gate < i, other


def _run_gate(tmp_path, response, status):
    """Run the gate step against a stub `gh` that prints `response` and exits `status`."""
    import os
    import shutil
    import subprocess

    import pytest

    if shutil.which("jq") is None or shutil.which("bash") is None:
        pytest.skip("needs jq and bash")
    job, gate = _gate_job()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(f"#!/bin/sh\ncat <<'JSON'\n{response}\nJSON\nexit {status}\n")
    stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "MyOpenFund/example",
        "ENV_NAME": "pypi",
    }
    return subprocess.run(
        ["bash", "-e", "-c", job["steps"][gate]["run"]],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def test_gate_passes_with_a_required_reviewer(tmp_path):
    rules = (
        '{"protection_rules": [{"type": "wait_timer"}, {"type": "required_reviewers",'
        ' "reviewers": [{"type": "User", "reviewer": {"login": "release-approver"}}]}]}'
    )
    assert _run_gate(tmp_path, rules, 0).returncode == 0


def test_gate_fails_without_a_required_reviewer(tmp_path):
    for rules in (
        '{"protection_rules": []}',
        '{"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}',
        '{"protection_rules": [{"type": "branch_policy"}]}',
        "{}",
    ):
        result = _run_gate(tmp_path, rules, 0)
        assert result.returncode == 1, rules
        assert "required reviewer" in result.stdout + result.stderr


def test_gate_fails_closed_when_the_api_call_fails(tmp_path):
    result = _run_gate(tmp_path, "gh: Not Found (HTTP 404)", 1)
    assert result.returncode == 1
    assert "HTTP 404" in result.stdout + result.stderr


def test_how_to_release_names_the_version_source():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("**How to release**", 1)[1].split("\n\n", 3)[1]
    assert "`version` in `query/pyproject.toml` or `contract/pyproject.toml`" in section
    assert "and `version`" not in section
    assert "pip install -e" in section
