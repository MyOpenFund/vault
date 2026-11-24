"""Smoke test of an installed wheel: import the package, and for the contract
read one golden file from the installed copy (package data must ship in the wheel).

Usage (with the venv's python): smoke_wheel.py <import_name>
"""
import importlib
import sys
from importlib import resources


def main(import_name):
    module = importlib.import_module(import_name)
    print(f"imported {import_name} from {module.__file__}")
    if import_name == "vault_contract":
        golden = resources.files("vault_contract.golden").joinpath("accepted.jsonl")
        text = golden.read_text(encoding="utf-8")
        assert text.strip(), "golden/accepted.jsonl is empty in the installed wheel"
        print(f"read {golden.name}: {len(text.splitlines())} rows")


if __name__ == "__main__":
    main(sys.argv[1])
