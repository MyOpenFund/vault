"""The `vault-contract` package (contract/): identity, install, and shipping.

Producers (company-corpus, central-bank-corpus) and data-orchestrator pin it by
commit and install it with pip, so what matters is what a *non-editable*
install delivers: the editable install from requirements-dev.txt reads the
source tree and would hide a module or a golden file left out of the wheel.
The vault ingestion service does not pip-install it: its image copies the
package next to ingest.py, which the last tests pin.
"""
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:     # Python 3.10: the contract's CI job runs these tests there
    tomllib = None

import vault_contract
import vault_contract.golden

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract"


def _project():
    """pyproject.toml's [project] table; on Python 3.10 (no tomllib) its
    `key = "value"` lines and `dependencies` list, which is all these tests read."""
    text = (CONTRACT / "pyproject.toml").read_text()
    if tomllib is not None:
        return tomllib.loads(text)["project"]
    table = text.split("[project]", 1)[1].split("\n[", 1)[0]
    project = dict(re.findall(r'^([\w-]+) = "([^"]*)"', table, re.M))
    deps = re.search(r"^dependencies = \[(.*?)\]", table, re.M | re.S)
    project["dependencies"] = re.findall(r'"([^"]*)"', deps.group(1)) if deps else []
    return project


def test_contract_name_and_version():
    assert vault_contract.CONTRACT_NAME == "documents"
    assert vault_contract.CONTRACT_VERSION == "1.0.0"


def test_package_version_is_the_contract_version():
    """One number: the package release IS the contract release (spec §3.3)."""
    project = _project()
    assert project["name"] == "vault-contract"
    assert project["version"] == vault_contract.CONTRACT_VERSION


def test_package_has_no_dependency_beyond_the_standard_library():
    """Every producer installs it; a dependency would leak into all of them."""
    project = _project()
    assert project.get("dependencies", []) == []
    # data-orchestrator (>=3.10) and company-corpus (>=3.11) import it at run
    # time; CI's `contract-py310` job runs these tests on 3.10.
    assert project["requires-python"] == ">=3.10"


def test_dev_requirements_install_the_contract_package():
    lines = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert "-e ./contract" in lines


def test_a_clean_venv_install_imports_the_package_and_its_golden_data(tmp_path):
    """`pip install ./contract` into a venv without this repo on its path.

    The probe runs from tmp_path, never from the repo, so the import can only
    be satisfied by the installed copy; it reads the golden rows through
    importlib.resources, which fails if package data was not declared.
    """
    # Install from a copy without build leftovers: setuptools builds in-tree
    # and reuses contract/build/, which keeps files deleted since.
    source = tmp_path / "contract"
    shutil.copytree(CONTRACT, source,
                    ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", str(source)],
        check=True, capture_output=True, text=True, timeout=300,
    )
    probe = (
        "import vault_contract as c\n"
        "from vault_contract import golden\n"
        "print(c.__file__)\n"
        "print(c.CONTRACT_NAME, c.CONTRACT_VERSION)\n"
        "print(' '.join(sorted(golden.accepted())))\n"
        "print(' '.join(sorted(golden.refused())))\n"
        "print(c.load_vocabulary(golden.vocabulary('company')).corpus,"
        " c.load_vocabulary(golden.vocabulary('central-bank')).corpus)\n"
    )
    # -I: no PYTHONPATH, no user site, no script directory: only the venv.
    out = subprocess.run([str(python), "-I", "-c", probe], cwd=tmp_path,
                         check=True, capture_output=True, text=True, timeout=60)
    file_line, name_line, accepted, refused, vocabularies = out.stdout.splitlines()
    assert Path(file_line).is_relative_to(env_dir), file_line
    assert name_line == "documents 1.0.0"
    assert accepted.split() == sorted(vault_contract.golden.accepted())
    assert len(accepted.split()) == 10
    assert refused.split() == sorted(vault_contract.golden.refused())
    assert vocabularies == "company central-bank"


def test_readme_documents_every_column_refusal_and_notice():
    """contract/README.md is the contract's user documentation (spec §3)."""
    readme = (CONTRACT / "README.md").read_text()
    missing = [n for n in (*vault_contract.COLUMNS, *vault_contract.REASONS, *vault_contract.NOTICES)
               if f"| `{n}` |" not in readme]
    assert missing == []
    assert "must put `corpus` in its point ids" in readme


def test_a_vendored_copy_works_from_pythonpath(tmp_path):
    """The ingestion image (and the NAS bind mount) use a plain copy of
    src/vault_contract, not an install: the copy alone, golden data included,
    must import and check a row."""
    vendor = tmp_path / "vendor"
    shutil.copytree(CONTRACT / "src" / "vault_contract", vendor / "vault_contract",
                    ignore=shutil.ignore_patterns("__pycache__"))
    probe = (
        "import vault_contract as c\n"
        "from vault_contract import golden\n"
        "case = golden.accepted()['sec_10k']\n"
        "voc = c.load_vocabulary(golden.vocabulary('company'))\n"
        "checked = c.check_row(case['row'], voc, expected_corpus='company')\n"
        "print(c.__file__)\n"
        "print(checked.accepted, len(checked.columns))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(vendor)
    out = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                         check=True, capture_output=True, text=True, timeout=60)
    file_line, result = out.stdout.splitlines()
    assert Path(file_line).is_relative_to(vendor), file_line
    assert result == "True 18"


# --- the ingestion image ships the package (no pip at run time) -------------

DOCKERFILE = (ROOT / "ingestion" / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()


def test_the_ingestion_image_copies_the_contract_package_next_to_ingest_py():
    """ingest.py runs as /app/ingest.py, so /app is on sys.path: a package
    copied to /app/vault_contract is importable without installing anything."""
    assert re.search(r"^COPY contract/src/vault_contract \./vault_contract/?$", DOCKERFILE, re.M)
    assert re.search(r"^WORKDIR /app$", DOCKERFILE, re.M)


def test_the_ingestion_image_builds_from_the_repo_root():
    """contract/ is outside ingestion/, so the build context must be the root
    (as for the api image); `build: ./ingestion` would fail at that COPY."""
    block = COMPOSE[COMPOSE.index("\n  ingestion:\n"):COMPOSE.index("\n  api:\n")]
    assert "build: ./ingestion" not in block
    assert re.search(r"^      context: \.$", block, re.M)
    assert re.search(r"^      dockerfile: ingestion/Dockerfile$", block, re.M)


def test_the_root_build_context_keeps_the_contract_package():
    """.dockerignore trims the root context; it must not drop contract/."""
    ignored = [l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines()
               if l.strip() and not l.startswith("#")]
    assert not any(p in ("contract", "contract/", "contract/src", "**/golden") for p in ignored)



def test_the_package_source_uses_nothing_newer_than_python_3_10():
    """requires-python >=3.10: a 3.11+ module or name would break the
    orchestrator at import. CI's 3.10 job is the real check; this one fails
    fast on the usual suspects."""
    newer = re.compile(r"\b(tomllib|typing\.Self|from typing import .*\bSelf\b|StrEnum|"
                       r"ExceptionGroup|except\*|itertools\.batched|\bbatched\(|datetime\.UTC|"
                       r"from datetime import .*\bUTC\b|^type \w+ =|def \w+\[)", re.M)
    for path in sorted((CONTRACT / "src" / "vault_contract").rglob("*.py")):
        hits = newer.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name}: {hits}"
