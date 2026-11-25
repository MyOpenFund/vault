"""The hygiene workflow and its commit-attribution check."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "hygiene.yml"
SCRIPT = ROOT / ".github" / "scripts" / "check_attribution.sh"


def _workflow():
    # PyYAML reads the bare key `on` as the boolean True.
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_hygiene_workflow_checks_commit_attribution():
    data = _workflow()
    assert data["name"] == "hygiene"
    assert "pull_request" in data["on"]
    assert data["on"]["push"]["branches"] == ["master"]
    assert data["permissions"] == {"contents": "read"}
    job = data["jobs"]["commit-attribution"]
    checkout = job["steps"][0]
    uses, _, ref = checkout["uses"].partition("@")
    assert uses == "actions/checkout"
    assert len(ref.split()[0]) == 40
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    release_pins = {
        step["uses"] for j in release["jobs"].values() for step in j["steps"] if "checkout" in step.get("uses", "")
    }
    assert checkout["uses"] in release_pins
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "check_attribution.sh" in runs
    assert "${{" not in runs


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Marc Jeulin",
    "GIT_AUTHOR_EMAIL": "jeulinmarc@gmail.com",
    "GIT_COMMITTER_NAME": "Marc Jeulin",
    "GIT_COMMITTER_EMAIL": "jeulinmarc@gmail.com",
}
needs_tools = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")


def _git(repo, *args, **env):
    full = {**GIT_ENV, **env, "PATH": os.environ["PATH"], "HOME": str(repo)}
    result = subprocess.run(["git", *args], cwd=repo, env=full, check=True, capture_output=True, text=True)
    return result.stdout


def _commit(repo, message, **env):
    (repo / "a").write_text(message)
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", message, **env)
    return _git(repo, "rev-parse", "HEAD").strip()


def _check(repo, rev_range):
    cmd = ["bash", str(SCRIPT), rev_range]
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "base")
    return tmp_path


@needs_tools
def test_clean_commit_passes_and_trailer_fails(repo):
    clean = _commit(repo, "clean commit")
    ok = _check(repo, f"{clean}^!")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "checked 1 commit" in ok.stdout
    dirty = _commit(repo, "dirty commit\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    bad = _check(repo, f"{clean}..{dirty}")
    assert bad.returncode == 1
    assert dirty[:7] in bad.stdout
    assert "Co-Authored-By" in bad.stdout


@needs_tools
def test_bad_range_fails_closed(repo):
    result = _check(repo, "no-such-ref..HEAD")
    assert result.returncode != 0
    assert "checked" not in result.stdout


MARC = "Co-authored-by: Marc Jeulin <jeulinmarc@gmail.com>"


@needs_tools
@pytest.mark.parametrize(
    "trailers",
    [
        [MARC],
        ["Co-authored-by: Marc Jeulin <12345+jeulinmarc@users.noreply.github.com>"],
        ["co-authored-by:  Marc Jeulin  <jeulinmarc@users.noreply.github.com>  "],
        [MARC, "Co-authored-by: Marc Jeulin <12345+jeulinmarc@users.noreply.github.com>"],
    ],
)
def test_marc_as_sole_coauthor_passes(repo, trailers):
    sha = _commit(repo, "squash\n\n" + "\n".join(trailers))
    result = _check(repo, f"{sha}^!")
    assert result.returncode == 0, result.stdout


@needs_tools
@pytest.mark.parametrize(
    "trailers",
    [
        ["Co-authored-by: Claude <noreply@anthropic.com>"],
        ["Co-authored-by: Someone Else <else@example.com>"],
        [MARC, "Co-authored-by: Someone Else <else@example.com>"],
        ["Co-authored-by: Someone Else <else@example.com>", MARC],
        ["Co-authored-by: Claude <jeulinmarc@gmail.com>"],
        ["Co-authored-by: Claude <c@x.io>, Marc Jeulin <jeulinmarc@gmail.com>"],
        ["Co-authored-by: Bob <notjeulinmarc@users.noreply.github.com>"],
        ["Co-authored-by: Marc Jeulin <whatever@example.com>"],
        ["Co-authored-by: M <jeulinmarc@gmail.com>"],
        ["Co-authored-by: Marc Jeulin <jeulinmarc@gmail.com> generated with a tool"],
    ],
)
def test_other_coauthor_fails(repo, trailers):
    sha = _commit(repo, "squash\n\n" + "\n".join(trailers))
    result = _check(repo, f"{sha}^!")
    assert result.returncode == 1, result.stdout
    assert sha[:7] in result.stdout
    assert "o-authored-by" in result.stdout


@needs_tools
@pytest.mark.parametrize(
    "env",
    [
        {"GIT_AUTHOR_NAME": "Claude"},
        {"GIT_COMMITTER_EMAIL": "noreply@anthropic.com"},
        {"GIT_AUTHOR_EMAIL": "x@anthropic.com"},
    ],
)
def test_agent_identity_fails(repo, env):
    sha = _commit(repo, "plain message", **env)
    result = _check(repo, f"{sha}^!")
    assert result.returncode == 1
    assert "identity" in result.stdout


@needs_tools
def test_false_positives_pass(repo):
    regenerated = _commit(repo, "regenerated with new data")
    assert _check(repo, f"{regenerated}^!").returncode == 0
    merge = _commit(
        repo,
        "Merge pull request #3 from MyOpenFund/claude/feat",
        GIT_COMMITTER_NAME="GitHub",
        GIT_COMMITTER_EMAIL="noreply@github.com",
    )
    result = _check(repo, f"{merge}^!")
    assert result.returncode == 0, result.stdout
