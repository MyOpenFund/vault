"""The MyOpenFund `documents` contract.

What a producer (a corpus) writes on each manifest line, what its vocabulary
file declares, and how a row is checked. The vault ingestion enforces it;
producers validate their own rows and vocabulary against it in CI;
data-orchestrator splits rows with it. Standard library only.

See contract/README.md for the field table and the versioning rule.
"""

from .fields import (
    COLUMNS, DATE_KINDS, DATE_PRECISIONS, REQUIRED, TRANSITION_ALIASES, split_row,
)
from .validate import NOTICES, REASONS, Notice, Refusal, quality_notices, validate_row
from .vocabulary import Vocabulary, is_supported_version, load_vocabulary, validate_vocabulary

CONTRACT_NAME = "documents"
CONTRACT_VERSION = "1.0.0"

__all__ = [
    "COLUMNS", "CONTRACT_NAME", "CONTRACT_VERSION", "DATE_KINDS", "DATE_PRECISIONS", "NOTICES",
    "Notice", "REASONS", "REQUIRED", "Refusal", "TRANSITION_ALIASES", "Vocabulary",
    "is_supported_version", "load_vocabulary", "quality_notices", "split_row", "validate_row",
    "validate_vocabulary",
]
