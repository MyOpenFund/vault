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
import subprocess
import tomllib
import venv
from pathlib import Path

import vault_contract
import vault_contract.golden

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract"


def test_contract_name_and_version():
    assert vault_contract.CONTRACT_NAME == "documents"
    assert vault_contract.CONTRACT_VERSION == "1.0.0"


def test_package_version_is_the_contract_version():
    """One number: the package release IS the contract release (spec §3.3)."""
    project = tomllib.loads((CONTRACT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "vault-contract"
    assert project["version"] == vault_contract.CONTRACT_VERSION


def test_package_has_no_dependency_beyond_the_standard_library():
    """Every producer installs it; a dependency would leak into all of them."""
    project = tomllib.loads((CONTRACT / "pyproject.toml").read_text())["project"]
    assert project.get("dependencies", []) == []
    assert project["requires-python"] == ">=3.12"


def test_dev_requirements_install_the_contract_package():
    lines = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert "-e ./contract" in lines


def test_a_clean_venv_install_imports_the_package_and_its_golden_data(tmp_path):
    """`pip install ./contract` into a venv without this repo on its path.

    The probe runs from tmp_path, never from the repo, so the import can only
    be satisfied by the installed copy; it reads the golden rows through
    importlib.resources, which fails if package data was not declared.
    """
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", str(CONTRACT)],
        check=True, capture_output=True, text=True, timeout=300,
    )
    probe = (
        "import vault_contract as c\n"
        "from vault_contract import golden\n"
        "print(c.__file__)\n"
        "print(c.CONTRACT_NAME, c.CONTRACT_VERSION)\n"
        "print(' '.join(sorted(golden.accepted())))\n"
    )
    out = subprocess.run([str(python), "-c", probe], cwd=tmp_path,
                         check=True, capture_output=True, text=True, timeout=60)
    file_line, name_line, accepted = out.stdout.splitlines()
    assert Path(file_line).is_relative_to(env_dir), file_line
    assert name_line == "documents 1.0.0"
    assert accepted.split() == sorted(vault_contract.golden.accepted())
    assert len(accepted.split()) == 5


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

