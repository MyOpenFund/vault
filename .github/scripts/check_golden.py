"""Run `python -m vault_contract.check` over the contract's golden rows.

Every accepted case must exit 0 and every refused case 1, each in a manifest
directory of its own (two accepted cases may share a doc_id on purpose), with
the vocabulary of its corpus. Judged with the transition on, as the checker
runs by default; a case written for after the cleanup adds --no-transition.
Used by CI's contract-py310 job on the installed package; exits 1 listing
every case whose exit code differs.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from vault_contract import golden


def main():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for expected_exit, cases in ((0, golden.accepted()), (1, golden.refused())):
            for name, case in cases.items():
                corpus = case["expected_corpus"]
                manifest = work / name
                manifest.mkdir()
                (manifest / f"{name}.jsonl").write_text(
                    json.dumps(case["row"], ensure_ascii=False) + "\n", encoding="utf-8")
                vocabulary = work / f"vocabulary-{name}.json"
                vocabulary.write_text(json.dumps(golden.vocabulary(corpus)), encoding="utf-8")
                argv = [sys.executable, "-m", "vault_contract.check", str(manifest),
                        "--corpus", corpus, "--vocabulary", str(vocabulary)]
                if case.get("transition") is False:
                    argv.append("--no-transition")
                done = subprocess.run(argv, capture_output=True, text=True)
                if done.returncode != expected_exit:
                    failures.append(f"{name}: exit {done.returncode}, expected {expected_exit}\n"
                                    f"{done.stdout}{done.stderr}")
        checked = len(golden.accepted()) + len(golden.refused())
    for failure in failures:
        print(failure)
    print(f"golden cases checked: {checked}, mismatches: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
