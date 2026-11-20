"""Reference rows of the `documents` contract, shipped with the package.

Producers and consumers test against the same rows the vault tests against,
two JSON Lines files in this directory, one case per line, `case` naming it:

- accepted.jsonl, accepted(): the representative rows every implementation
  must accept, `{"case", "description", "expected_corpus", "row",
  "expected_columns", "expected_notices"}`: `expected_columns` is the 18
  columns after split_row, `expected_notices` the notice reasons (one entry
  per notice, [] for a clean row); together they trigger every notice;
- refused.jsonl, refused(): exactly one row per refusal reason, `{"case",
  "description", "expected_corpus", "row", "expected_reasons"}`, plus
  `"transition": false` for a case judged with transition=False (how the
  vault ingests);
- vocabulary(corpus): the vocabulary excerpt the cases are checked against,
  `vocabulary_<corpus with - as _>.json`.

Every case is checked with the vocabulary of its `expected_corpus` and, unless
it says otherwise, with transition=True.
"""
import json
from importlib.resources import files

_HERE = files(__name__)


def _load(name: str) -> dict[str, dict]:
    """Case name -> case (the line without its `case` key), in file order."""
    cases = {}
    for line in (_HERE / name).read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            cases[case.pop("case")] = case
    return cases


def accepted() -> dict[str, dict]:
    """Case name -> accepted case."""
    return _load("accepted.jsonl")


def refused() -> dict[str, dict]:
    """Case name -> refused case."""
    return _load("refused.jsonl")


def vocabulary(corpus: str) -> dict:
    """The golden vocabulary document of `corpus` (`company`, `central-bank`)."""
    name = f"vocabulary_{corpus.replace('-', '_')}.json"
    return json.loads((_HERE / name).read_text(encoding="utf-8"))
