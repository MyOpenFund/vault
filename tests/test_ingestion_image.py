"""The ingestion image must ship every module ingest.py imports.

The Dockerfile lists its sources one by one: a new module that is imported but
not copied passes every test (they run from the source tree) and only fails in
the container, at import time, on the NAS.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "ingestion" / "Dockerfile").read_text()


def _copied():
    files = set()
    for line in DOCKERFILE.splitlines():
        if line.startswith("COPY "):
            files.update(line.split()[1:-1])
    return files


def test_every_ingestion_module_is_copied_into_the_image():
    modules = {p.name for p in (ROOT / "ingestion").glob("*.py")}
    assert modules <= _copied(), f"missing from the image: {sorted(modules - _copied())}"


def test_every_local_import_of_ingest_is_a_shipped_module():
    source = (ROOT / "ingestion" / "ingest.py").read_text()
    local = {p.stem for p in (ROOT / "ingestion").glob("*.py")}
    imported = set(re.findall(r"^(?:import|from) (\w+)", source, re.M)) & local
    assert {f"{m}.py" for m in imported} <= _copied()
