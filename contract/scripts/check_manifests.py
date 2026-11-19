"""Check manifest files against the `documents` contract, read-only.

    python contract/scripts/check_manifests.py <manifest_dir> --corpus central-bank \
        [--vocabulary vocabulary.json] [--glob '*.jsonl']

Every line goes through split_row, validate_row and quality_notices, exactly
as the vault ingestion will. Prints the counts of refusals and notices per
reason with up to 3 examples each. Exit 0 when no row is refused, 1 when at
least one is, 2 on a usage error or an invalid vocabulary file.

Standard library only. It imports vault_contract from this checkout
(contract/src) first, so it checks against the contract next to it, installed
or not. Lines that are not a JSON object are counted apart: the ingestion
skips them before the contract applies.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vault_contract import (  # noqa: E402
    load_vocabulary, quality_notices, split_row, validate_row,
)

EXAMPLES = 3
VALUE_CHARS = 80


def _shown(value):
    text = repr(value)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS] + "…"


def _print_section(title, counts, examples):
    print(f"{title}: {sum(counts.values())}")
    for reason, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {reason}: {n}")
        for example in examples[reason]:
            print(f"    {example}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("--corpus", required=True, help="the corpus the rows must belong to")
    parser.add_argument("--vocabulary", type=Path,
                        help="vocabulary.json; without it the code checks are skipped")
    parser.add_argument("--glob", default="*.jsonl", help="manifest files under manifest_dir")
    args = parser.parse_args(argv)

    if not args.manifest_dir.is_dir():
        parser.error(f"not a directory: {args.manifest_dir}")
    vocabulary = None
    if args.vocabulary:
        try:
            vocabulary = load_vocabulary(json.loads(args.vocabulary.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"vocabulary: {e}", file=sys.stderr)
            return 2

    files = sorted(p for p in args.manifest_dir.glob(args.glob) if p.is_file())
    rows = refused_rows = noticed_rows = unreadable = 0
    refusals, notices = Counter(), Counter()
    refusal_examples, notice_examples = defaultdict(list), defaultdict(list)

    for path in files:
        with path.open(encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    row = None
                if not isinstance(row, dict):
                    unreadable += 1
                    continue
                rows += 1
                columns, _ = split_row(row)
                where = f"{path.name}:{line_num} doc_id={_shown(columns['doc_id'])}"
                found = validate_row(columns, vocabulary, expected_corpus=args.corpus)
                refused_rows += bool(found)
                for r in found:
                    refusals[r.reason] += 1
                    if len(refusal_examples[r.reason]) < EXAMPLES:
                        refusal_examples[r.reason].append(f"{where} {r.field}={_shown(r.value)}")
                noted = quality_notices(columns, row=row)
                noticed_rows += bool(noted)
                for n in noted:
                    notices[n.reason] += 1
                    if len(notice_examples[n.reason]) < EXAMPLES:
                        notice_examples[n.reason].append(f"{where} {n.field}={_shown(n.value)}")

    print(f"corpus: {args.corpus}")
    print(f"vocabulary: {args.vocabulary if vocabulary else 'none (code checks skipped)'}")
    print(f"files: {len(files)}")
    print(f"rows: {rows} (refused {refused_rows}, with notices {noticed_rows})")
    print(f"unreadable lines (not a JSON object): {unreadable}")
    _print_section("refusals", refusals, refusal_examples)
    _print_section("notices", notices, notice_examples)
    return 1 if refused_rows else 0


if __name__ == "__main__":
    sys.exit(main())
