"""Check manifest files against the `documents` contract, read-only.

    python -m vault_contract.check <manifest_dir> --corpus central-bank \
        [--vocabulary vocabulary.json] [--glob '*.jsonl'] [--no-transition] [--strict]

Files are read as the vault ingestion reads them: UTF-8, lines ended by \n,
\r\n or \r only, each line stripped, blank lines skipped. A line that is not a
JSON object (unparsable, deep nesting and oversized integers included, or
valid JSON of another type) is refused as not_json, as the ingestion refuses
it; every other line goes through check_row exactly as the ingestion will. doc_ids are then resolved as the vault resolves
them: the last valid line of a doc_id wins (dedupe_rows within a file, upserts
in file order across files), so every earlier valid occurrence is refused as
duplicate_doc_id, pointing at the kept one. Prints the counts of refusals and
notices per reason with up to 3 examples each.

Exit 0: no row refused. 1: at least one row refused (not_json included), or
under --strict at least one notice. 2: usage error (an empty --corpus, a bad
--glob), no file matching the glob, a file that cannot be read, is not UTF-8
or starts with a UTF-8 BOM (the ingestion would refuse its first line as
not_json), no line that is a JSON object at all, an unreadable or invalid
vocabulary, or a vocabulary of another corpus.
"""
import argparse
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .validate import check_row, is_text
from .vocabulary import load_vocabulary

DUPLICATE_DOC_ID = "duplicate_doc_id"
# The vault ingestion's reason for a line that is not a JSON object (like
# duplicate_doc_id, not a contract reason: such a line is not a row at all).
NOT_JSON = "not_json"
EXAMPLES = 3
VALUE_CHARS = 80


def _shown(value):
    text = repr(value)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS] + "…"


def _field(name):
    """A refusal's field as printed: an extra key (bad_extra) can hold a NUL
    or a lone surrogate, which cannot be printed: its repr then."""
    if isinstance(name, str) and is_text(name):
        return name if len(name) <= VALUE_CHARS else name[:VALUE_CHARS] + "…"
    return _shown(name)


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
                        help="exit 1 on any notice too")
    args = parser.parse_args(argv)

    if not args.corpus:
        parser.error("--corpus must not be empty")
    if not args.manifest_dir.is_dir():
        parser.error(f"not a directory: {args.manifest_dir}")
    vocabulary = None
    if args.vocabulary:
        vocabulary, error = _load_vocabulary(args.vocabulary, args.corpus)
        if error:
            print(error, file=sys.stderr)
            return 2
    try:
        files = sorted(p for p in args.manifest_dir.glob(args.glob) if p.is_file())
    except (ValueError, NotImplementedError) as e:
        print(f"bad --glob {args.glob!r}: {e}", file=sys.stderr)
        return 2
    if not files:
        print(f"no file matches {args.glob!r} under {args.manifest_dir}", file=sys.stderr)
        return 2

    rows = refused_rows = noticed_rows = objects = 0
    unreadable = []
    refusals, notices = _Tally(), _Tally()
    valid = {}          # doc_id -> [location of each valid line], in vault order

    for path in files:
        try:
            data = path.read_bytes()
        except OSError as e:
            unreadable.append(path)
            print(f"{path.name}: cannot be read ({e}), not checked", file=sys.stderr)
            continue
        if data.startswith(b"\xef\xbb\xbf"):
            unreadable.append(path)
            print(f"{path.name}: starts with a UTF-8 BOM; the ingestion would skip its "
                  "first line, not checked", file=sys.stderr)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            unreadable.append(path)
            print(f"{path.name}: not UTF-8 ({e}), not checked", file=sys.stderr)
            continue
        # A text-mode file object, as open(..., encoding="utf-8") in the
        # ingestion: universal newlines, no split on U+2028, U+0085, ...
        for line_num, line in enumerate(io.StringIO(text, newline=None), 1):
            line = line.strip()
            if not line:
                continue
            rows += 1
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):   # JSONDecodeError; an int past 4300 digits
                row = None
            if not isinstance(row, dict):
                refused_rows += 1
                # Sliced before repr: a torn line can be megabytes long.
                refusals.add(NOT_JSON, f"{path.name}:{line_num} line={_shown(line[:VALUE_CHARS])}")
                continue
            objects += 1
            checked = check_row(row, vocabulary, expected_corpus=args.corpus,
                                transition=args.transition)
            where = f"{path.name}:{line_num} doc_id={_shown(checked.columns['doc_id'])}"
            if checked.refusals:
                refused_rows += 1
            elif is_text(checked.columns["doc_id"]):
                valid.setdefault(checked.columns["doc_id"], []).append(
                    (f"{path.name}:{line_num}", where))
            for r in checked.refusals:
                refusals.add(r.reason, f"{where} {_field(r.field)}={_shown(r.value)}")
            noticed_rows += bool(checked.notices)
            for n in checked.notices:
                notices.add(n.reason, f"{where} {n.field}={_shown(n.value)}")

    for occurrences in valid.values():
        kept = occurrences[-1][0]
        for _, where in occurrences[:-1]:
            refusals.add(DUPLICATE_DOC_ID, f"{where} superseded by {kept}")
            refused_rows += 1

    print(f"corpus: {args.corpus}")
    print(f"vocabulary: {args.vocabulary if vocabulary else 'none (membership not checked)'}")
    print(f"transition: {'on' if args.transition else 'off'}")
    print(f"files: {len(files)}" + (f" ({len(unreadable)} not checked)" if unreadable else ""))
    print(f"rows: {rows} (refused {refused_rows}, with notices {noticed_rows})")
    refusals.print("refusals")
    notices.print("notices")

    if unreadable:
        return 2
    if not objects:
        print("no rows: no line of the matching files is a JSON object", file=sys.stderr)
        return 2
    if refused_rows or (args.strict and noticed_rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
