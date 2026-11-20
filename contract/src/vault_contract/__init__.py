"""The MyOpenFund `documents` contract.

What a producer (a corpus) writes on each manifest line, what its vocabulary
file declares, and how a row is checked. The vault ingestion enforces it;
producers validate their own rows and vocabulary against it in CI;
data-orchestrator splits rows with it. Standard library only.

See contract/README.md for the field table and the versioning rule.
"""

from .fields import (
    COLUMNS, DATE_KINDS, DATE_PRECISIONS, EXPECTED, REFUSED_WHEN_ABSENT, TRANSITION_ALIASES,
    split_row,
)
from .validate import (
    MAX_CODE_CHARS, MAX_ID_BYTES, MAX_PATH_BYTES, MAX_PATH_SEGMENT_BYTES, MAX_TITLE_CHARS,
    MAX_URL_CHARS, NOTICES, REASONS, CheckedRow, Notice, Refusal, check_row, extra_refusals, quality_notices,
    validate_row,
)
from .vocabulary import Vocabulary, is_supported_version, load_vocabulary, validate_vocabulary

CONTRACT_NAME = "documents"
CONTRACT_VERSION = "1.0.0"

__all__ = [
    "COLUMNS", "CONTRACT_NAME", "MAX_CODE_CHARS", "MAX_ID_BYTES", "MAX_PATH_BYTES",
    "MAX_PATH_SEGMENT_BYTES", "MAX_TITLE_CHARS", "MAX_URL_CHARS", "CONTRACT_VERSION", "DATE_KINDS", "DATE_PRECISIONS", "EXPECTED",
    "NOTICES", "REASONS", "REFUSED_WHEN_ABSENT", "TRANSITION_ALIASES", "CheckedRow", "Notice",
    "Refusal", "Vocabulary", "check_row", "extra_refusals", "is_supported_version", "load_vocabulary",
    "quality_notices", "split_row", "validate_row", "validate_vocabulary",
]
