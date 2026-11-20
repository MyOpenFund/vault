"""Where a document's file is: its corpus root (CORPUS_ROOTS) plus its local_path.

Documents contract: `local_path` is a POSIX path relative to the corpus data
root (the directory holding `manifest/`), and each deployment says where that
root is mounted, one per corpus:

    CORPUS_ROOTS=central-bank=/cbdata,company=/companydata

`local_path` comes from a producer's manifest, so it is untrusted input
reaching a filesystem read: the resolved file (every symlink followed) must
stay under its corpus root, whatever the path says.
"""
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

ENV_NAME = "CORPUS_ROOTS"

# Linux limits (NAME_MAX, PATH_MAX), in bytes: a longer path cannot name a
# file, and the OS answers it with an error (ENAMETOOLONG) rather than "no".
NAME_MAX_BYTES = 255
PATH_MAX_BYTES = 4096

# What a root may not contain: whitespace and control characters (Unicode
# Cc, NEL included) are a copy-paste or quoting slip, and `;` a shell one.
_BAD_ROOT_CHAR = re.compile(r"[\s\x00-\x1f\x7f-\x9f;]")

# A corpus name is a contract code (vault_contract.CODE_PATTERN).
# Copied, not imported: the API image installs vault-query only.
# tests/test_corpus_roots.py fails if the two drift apart.
CORPUS_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")


def parse_corpus_roots(text):
    """{corpus: absolute root Path} from a CORPUS_ROOTS value; {} for an empty one.

    Strict, so a typo stops the API at startup instead of 404-ing every
    download: each comma-separated entry is `name=/absolute/path`, the name a
    contract code, the path absolute without a `..` component, whitespace,
    control characters or `;`, and not the filesystem root `/` (that would
    serve the whole filesystem), each corpus once. Spaces around entries,
    names and paths are ignored. ValueError (message starting with
    "CORPUS_ROOTS") otherwise. Whether each root exists is not checked here
    (see missing_roots).
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
        bad = _BAD_ROOT_CHAR.search(path)
        if bad:
            char = bad.group()
            kind = ("';'" if char == ";" else
                    "a control character" if unicodedata.category(char) == "Cc" else
                    "whitespace")
            raise ValueError(f"{ENV_NAME}: root of {name!r} must not contain {kind}, "
                             f"got {path!r}")
        if ".." in root.parts:
            raise ValueError(f"{ENV_NAME}: root of {name!r} must not contain '..', got {path!r}")
        if len(root.parts) == 1:          # the anchor alone: "/", "//"
            raise ValueError(f"{ENV_NAME}: root of {name!r} must not be the filesystem root, "
                             f"got {path!r}")
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
    The file may not exist: the caller checks (is_file).

    None too for a path no file can have (a component past NAME_MAX_BYTES,
    the whole past PATH_MAX_BYTES) and for one the OS refuses to resolve (a
    NUL, a symlink loop, ENAMETOOLONG): to the caller, "not found", never a
    500.
    """
    relative = PurePosixPath(local_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        base = Path(root).resolve()
        joined = base / relative
        if (len(os.fsencode(joined)) > PATH_MAX_BYTES
                or any(len(os.fsencode(part)) > NAME_MAX_BYTES for part in relative.parts)):
            return None
        candidate = joined.resolve()
    except (OSError, RuntimeError, ValueError):   # UnicodeEncodeError is a ValueError
        return None
    if candidate == base or not candidate.is_relative_to(base):
        return None
    return candidate


def is_file(path):
    """Whether `path` is a regular file; False when the OS cannot answer."""
    try:
        return path.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def missing_roots(roots):
    """{corpus: root} for the roots that are not an existing directory."""
    missing = {}
    for name, root in roots.items():
        try:
            ok = root.is_dir()
        except (OSError, ValueError):
            ok = False
        if not ok:
            missing[name] = root
    return missing
