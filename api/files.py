"""Where a document's file is: its corpus root (CORPUS_ROOTS) plus its local_path.

Documents contract: `local_path` is a POSIX path relative to the corpus data
root (the directory holding `manifest/`), and each deployment says where that
root is mounted, one per corpus:

    CORPUS_ROOTS=central-bank=/cbdata,company=/companydata

`local_path` comes from a producer's manifest, so it is untrusted input
reaching a filesystem read: the resolved file (every symlink followed) must
stay under its corpus root, whatever the path says.
"""
import re
from pathlib import Path, PurePosixPath

ENV_NAME = "CORPUS_ROOTS"

# A corpus name is a contract code (vault_contract.vocabulary.CODE_PATTERN).
# Copied, not imported: the API image installs vault-query only.
# tests/test_corpus_roots.py fails if the two drift apart.
CORPUS_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")


def parse_corpus_roots(text):
    """{corpus: absolute root Path} from a CORPUS_ROOTS value; {} for an empty one.

    Strict, so a typo stops the API at startup instead of 404-ing every
    download: each comma-separated entry is `name=/absolute/path`, the name a
    contract code, the path absolute without a `..` component, each corpus
    once. Spaces around entries, names and paths are ignored. ValueError
    (message starting with "CORPUS_ROOTS") otherwise.
    """
    roots = {}
    if not text.strip():
        return roots
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            raise ValueError(f"{ENV_NAME}: empty entry in {text!r} (a stray comma?)")
        name, sep, path = (part.strip() for part in entry.partition("="))
        if not sep:
            raise ValueError(f"{ENV_NAME}: entry {entry!r} is not name=/absolute/path")
        if not CORPUS_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{ENV_NAME}: corpus name {name!r} must match "
                             f"{CORPUS_NAME_PATTERN.pattern}")
        root = PurePosixPath(path)
        if not path or not root.is_absolute():
            raise ValueError(f"{ENV_NAME}: root of {name!r} must be an absolute path, got {path!r}")
        if ".." in root.parts:
            raise ValueError(f"{ENV_NAME}: root of {name!r} must not contain '..', got {path!r}")
        if name in roots:
            raise ValueError(f"{ENV_NAME}: corpus {name!r} is given twice")
        roots[name] = Path(path)
    return roots


def resolve_under_root(root, local_path):
    """The resolved file `local_path` names under `root`, or None if it leaves it.

    None for an absolute path, a `..` component, a path naming the root itself
    (empty, `.`), and a path whose symlinks lead out of the root (no `..`
    needed: only resolving it on the filesystem shows it). The root is
    resolved too, so a root that is itself a symlink still contains its files.
    The file may not exist: the caller checks.
    """
    relative = PurePosixPath(local_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        return None
    return candidate
