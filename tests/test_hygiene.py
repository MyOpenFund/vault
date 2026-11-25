"""The hygiene workflow and its commit-attribution check."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
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
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "check_attribution.sh" in runs
    assert "${{" not in runs


def _git(repo, *args):
    env = {
        "GIT_AUTHOR_NAME": "Marc Jeulin",
        "GIT_AUTHOR_EMAIL": "jeulinmarc@gmail.com",
        "GIT_COMMITTER_NAME": "Marc Jeulin",
        "GIT_COMMITTER_EMAIL": "jeulinmarc@gmail.com",
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
    }
    result = subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return result.stdout


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")
def test_check_attribution_script(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a").write_text("1")
    _git(tmp_path, "add", "a")
    _git(tmp_path, "commit", "-q", "-m", "clean commit")
    clean = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "a").write_text("2")
    trailer = "Co-Authored-By: Claude <noreply@anthropic.com>"
    _git(tmp_path, "commit", "-q", "-am", f"dirty commit\n\n{trailer}")
    dirty = _git(tmp_path, "rev-parse", "HEAD").strip()

    def run(rev_range):
        cmd = ["bash", str(SCRIPT), rev_range]
        return subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, check=False)

    ok = run(f"{clean}^!")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "checked 1 commit" in ok.stdout
    bad = run(f"{clean}..{dirty}")
    assert bad.returncode == 1
    assert dirty[:7] in bad.stdout
    assert "Co-Authored-By" in bad.stdout
