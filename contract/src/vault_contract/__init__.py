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

CONTRACT_NAME = "documents"
CONTRACT_VERSION = "1.0.0"

__all__ = [
    "COLUMNS", "CONTRACT_NAME", "CONTRACT_VERSION", "DATE_KINDS", "DATE_PRECISIONS",
    "REQUIRED", "TRANSITION_ALIASES", "split_row",
]
