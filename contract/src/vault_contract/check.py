"""Check manifest files against the `documents` contract, read-only.

    python -m vault_contract.check <manifest_dir> --corpus central-bank \
        [--vocabulary vocabulary.json] [--glob '*.jsonl'] [--no-transition] [--strict]

Every line goes through check_row exactly as the vault ingestion will, and
doc_ids are checked for uniqueness across all the files (identity is
(corpus, doc_id)): every occurrence after the first is refused as
duplicate_doc_id. Prints the counts of refusals and notices per reason with up
to 3 examples each.

Exit 0: no row refused. 1: at least one row refused, or under --strict at
least one notice or one line that is not a JSON object. 2: usage error, no
file matching the glob, a file that is not UTF-8, an unreadable or invalid
vocabulary, or a vocabulary of another corpus. Lines that are not a JSON
object are counted apart: the ingestion skips them before the contract applies.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .validate import check_row, is_text
from .vocabulary import load_vocabulary

DUPLICATE_DOC_ID = "duplicate_doc_id"
EXAMPLES = 3
VALUE_CHARS = 80


def _shown(value):
    text = repr(value)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS] + "…"


class _Tally:
    def __init__(self):
        self.counts = Counter()
        self.examples = defaultdict(list)

    def add(self, reason, example):
        self.counts[reason] += 1
        if len(self.examples[reason]) < EXAMPLES:
            self.examples[reason].append(example)

    def print(self, title):
        print(f"{title}: {sum(self.counts.values())}")
        for reason, n in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {reason}: {n}")
            for example in self.examples[reason]:
                print(f"    {example}")


def _load_vocabulary(path, corpus):
    """(vocabulary, error message)."""
    try:
        vocabulary = load_vocabulary(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError) as e:     # JSONDecodeError is a ValueError
        return None, f"vocabulary {path}: {e}"
    if vocabulary.corpus != corpus:
        return None, (f"vocabulary {path} is the vocabulary of corpus {vocabulary.corpus!r}, "
                      f"not of {corpus!r}")
    return vocabulary, None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m vault_contract.check",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("--corpus", required=True, help="the corpus the rows must belong to")
    parser.add_argument("--vocabulary", type=Path,
                        help="vocabulary.json; without it code membership is not checked")
    parser.add_argument("--glob", default="*.jsonl", help="manifest files under manifest_dir")
    parser.add_argument("--no-transition", dest="transition", action="store_false",
                        help="judge as after the cleanup: no old key mapped, no corpus implied")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 on any notice or line that is not a JSON object")
    args = parser.parse_args(argv)

    if not args.manifest_dir.is_dir():
        parser.error(f"not a directory: {args.manifest_dir}")
    vocabulary = None
    if args.vocabulary:
        vocabulary, error = _load_vocabulary(args.vocabulary, args.corpus)
        if error:
            print(error, file=sys.stderr)
            return 2
    files = sorted(p for p in args.manifest_dir.glob(args.glob) if p.is_file())
    if not files:
        print(f"no file matches {args.glob!r} under {args.manifest_dir}", file=sys.stderr)
        return 2

    rows = refused_rows = noticed_rows = not_objects = 0
    not_utf8 = []
    refusals, notices = _Tally(), _Tally()
    first_seen = {}

    for path in files:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            not_utf8.append(path)
            print(f"{path.name}: not UTF-8 ({e}), not checked", file=sys.stderr)
            continue
        for line_num, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            if not isinstance(row, dict):
                not_objects += 1
                continue
            rows += 1
            checked = check_row(row, vocabulary, expected_corpus=args.corpus,
                                transition=args.transition)
            here = f"{path.name}:{line_num}"
            where = f"{here} doc_id={_shown(checked.columns['doc_id'])}"
            refused = bool(checked.refusals)
            for r in checked.refusals:
                refusals.add(r.reason, f"{where} {r.field}={_shown(r.value)}")
            doc_id = checked.columns["doc_id"]
            if is_text(doc_id):
                if doc_id in first_seen:
                    refusals.add(DUPLICATE_DOC_ID, f"{where} first at {first_seen[doc_id]}")
                    refused = True
                else:
                    first_seen[doc_id] = here
            refused_rows += refused
            noticed_rows += bool(checked.notices)
            for n in checked.notices:
                notices.add(n.reason, f"{where} {n.field}={_shown(n.value)}")

    print(f"corpus: {args.corpus}")
    print(f"vocabulary: {args.vocabulary if vocabulary else 'none (membership not checked)'}")
    print(f"transition: {'on' if args.transition else 'off'}")
    print(f"files: {len(files)}" + (f" ({len(not_utf8)} not UTF-8, not checked)" if not_utf8 else ""))
    print(f"rows: {rows} (refused {refused_rows}, with notices {noticed_rows})")
    print(f"lines that are not a JSON object: {not_objects}")
    refusals.print("refusals")
    notices.print("notices")

    if not_utf8:
        return 2
    if refused_rows or (args.strict and (noticed_rows or not_objects)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
