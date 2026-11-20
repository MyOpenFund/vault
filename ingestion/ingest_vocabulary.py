"""The corpus vocabulary file (vocabulary.json) -> the `vocabulary` table.

The producer writes vocabulary.json at its data root (documents contract,
contract/README.md "Vocabulary file"): the codes its manifest rows may use
for source_code, doc_type, provenance and entity_scheme. The documents pass
checks every row's codes against it, and this module mirrors it into the
`vocabulary` table as a full replace for the service's corpus (the
cadence.jsonl model, ingest_cadence.py), in one transaction.

- Absent file: transition (spec §4.3). Code membership is not checked, the
  run says so, and the table is left untouched (a file that disappears is not
  a vocabulary that became empty).
- Present but unreadable, invalid (vault_contract.validate_vocabulary) or of
  another corpus: the run fails before connecting (VocabularyError, exit 1):
  checking rows against a broken vocabulary would refuse or accept them on
  the strength of a producer bug, and nothing is written to documents.

The file is <DATA_DIR>/vocabulary.json, or VOCABULARY_PATH when set (the
manifest-directory layout, where DATA_DIR is the manifest directory and the
data root is elsewhere, as for CADENCE_PATH).
"""

import json
import logging
import os

from psycopg2.extras import execute_values
from vault_contract import load_vocabulary

log = logging.getLogger("ingest")

FILE_NAME = "vocabulary.json"
ENV_PATH = "VOCABULARY_PATH"

# Table kinds, named after the column each code is for.
KIND_SOURCE_CODE = "source_code"
KIND_DOC_TYPE = "doc_type"
KIND_PROVENANCE = "provenance"
KIND_ENTITY_SCHEME = "entity_scheme"

INSERT_VOCABULARY_SQL = """
INSERT INTO vocabulary (corpus, kind, code, label, family) VALUES %s
"""


class VocabularyError(ValueError):
    """vocabulary.json exists but cannot be used; the run fails before connecting."""


def vocabulary_path(data_dir, env):
    return env.get(ENV_PATH) or os.path.join(data_dir, FILE_NAME)


def load(path, corpus):
    """The Vocabulary at `path`, or None when there is no file there.

    VocabularyError when the file exists but cannot be read, is not valid
    JSON, fails validate_vocabulary, or belongs to another corpus.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        vocabulary = load_vocabulary(doc)
    except (OSError, ValueError, RecursionError) as exc:   # JSONDecodeError, UnicodeDecodeError are ValueErrors
        raise VocabularyError(f"vocabulary {path}: {exc}") from None
    if vocabulary.corpus != corpus:
        raise VocabularyError(
            f"vocabulary {path} is the vocabulary of corpus {vocabulary.corpus!r}, "
            f"this service ingests {corpus!r}")
    return vocabulary


def table_rows(vocabulary):
    """(corpus, kind, code, label, family) for every code, in file order per kind."""
    corpus = vocabulary.corpus
    rows = [(corpus, KIND_SOURCE_CODE, code, label, None)
            for code, label in vocabulary.source_codes.items()]
    rows += [(corpus, KIND_DOC_TYPE, code, label, family)
             for code, (label, family) in vocabulary.doc_types.items()]
    rows += [(corpus, KIND_PROVENANCE, code, None, None)
             for code in sorted(vocabulary.provenances)]
    rows += [(corpus, KIND_ENTITY_SCHEME, code, None, None)
             for code in sorted(vocabulary.entity_schemes)]
    return rows


def replace(conn, vocabulary):
    """Replace the vocabulary rows of the vocabulary's corpus, in one transaction."""
    rows = table_rows(vocabulary)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vocabulary WHERE corpus = %s", (vocabulary.corpus,))
            execute_values(cur, INSERT_VOCABULARY_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info(f"vocabulary: replaced with {len(rows)} code(s) "
             f"(contract {vocabulary.contract_version})")
    return len(rows)
