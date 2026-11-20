"""CI pre-pulls postgres:16, with retries, before the integration suite (Spec W19)."""
from pathlib import Path

WORKFLOW = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml").read_text()


def test_postgres_is_pulled_before_the_integration_tests():
    pull = WORKFLOW.index("docker pull postgres:16")
    integration = WORKFLOW.index("-m integration")
    assert pull < integration


def test_the_pull_is_retried_and_bounded():
    step = WORKFLOW[WORKFLOW.index("- name: Pre-pull postgres:16"):WORKFLOW.index("- name: Integration tests")]
    assert 'PULL_ATTEMPTS: "3"' in step
    assert 'timeout "$PULL_TIMEOUT_SECONDS"' in step
    assert step.rstrip().splitlines()[-1].strip() == "exit 1"  # all attempts failed -> job fails


def test_the_contract_is_tested_alone_on_python_3_10():
    """vault-contract's requires-python is >=3.10: CI proves it there, with the
    package installed alone (no requirements-dev.txt, no editable install)."""
    job = WORKFLOW[WORKFLOW.index("\n  contract-py310:\n"):]      # the last job
    assert 'python-version: "3.10"' in job
    assert "pip install pytest==8.3.3 pytest-timeout==2.3.1 ./contract" in job
    assert "requirements-dev.txt" not in job
    contract_tests = sorted(p.name for p in (Path(__file__).parent).glob("test_contract_*.py"))
    for name in contract_tests:
        assert f"tests/{name}" in job, name
    assert "python .github/scripts/check_golden.py" in job


def test_the_contract_tests_import_nothing_else_from_the_repo():
    """The 3.10 job installs the contract package only: its tests may import
    the standard library, pytest and vault_contract, nothing of api/, cli/,
    ingestion/ or query/."""
    import re
    allowed = {"vault_contract", "pytest", "json", "re", "os", "sys", "io", "copy", "shutil",
               "subprocess", "tomllib", "venv", "pathlib", "dataclasses", "datetime", "math",
               "unicodedata", "collections", "itertools"}
    for path in sorted(Path(__file__).parent.glob("test_contract_*.py")):
        modules = re.findall(r"^\s*(?:from|import) (\w+)", path.read_text(), re.M)
        assert set(modules) <= allowed, (path.name, set(modules) - allowed)


def test_the_golden_check_script_passes(tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "check_golden.py"
    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "mismatches: 0" in done.stdout
