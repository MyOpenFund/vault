"""Reference rows of the `documents` contract, shipped with the package.

Producers and consumers test against the same rows the vault tests against:

- accepted(): realistic rows every implementation must accept unchanged
  (`{"description", "row"}`), one file per case in this directory;
- refused(): rows that must be refused with exactly `expected_reasons`
  (`{"description", "expected_corpus", "row", "expected_reasons"}`), in refused/;
- vocabulary(corpus): the vocabulary excerpt the cases are checked against,
  `vocabulary_<corpus with - as _>.json`.

An accepted case's expected corpus is its row's `corpus`.
"""
import json
from importlib.resources import files

_HERE = files(__name__)


def _load_dir(directory) -> dict[str, dict]:
    return {
        entry.name.removesuffix(".json"): json.loads(entry.read_text(encoding="utf-8"))
        for entry in sorted(directory.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json") and not entry.name.startswith("vocabulary_")
    }


def accepted() -> dict[str, dict]:
    """Case name -> accepted case."""
    return _load_dir(_HERE)


def refused() -> dict[str, dict]:
    """Case name -> refused case."""
    return _load_dir(_HERE / "refused")


def vocabulary(corpus: str) -> dict:
    """The golden vocabulary document of `corpus` (`company`, `central-bank`)."""
    name = f"vocabulary_{corpus.replace('-', '_')}.json"
    return json.loads((_HERE / name).read_text(encoding="utf-8"))
