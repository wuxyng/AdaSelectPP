"""Public API for the offline Evaluation Substrate v0 core.

The package is deliberately independent of AdaSelectPP's online candidate,
selector, and database-connector paths. Database collection is possible only
through a validated :class:`EvaluationRunContext`; the HypoPG session backend
and response service remain private implementation details.
"""

from .cost_store import MissingOptimizerResponseError
from .epoch_fingerprint import (
    EpochFingerprintError,
    collect_epoch_fingerprint,
    compute_epoch_hash,
    validate_epoch_hash,
)
from .evidence import EvidenceStoreError, ExactResponseKey
from .manifest import (
    ManifestError,
    build_manifest,
    initialize_run,
    validate_manifest,
    write_manifest_atomic,
)
from .reveal import (
    OptimizerCallBudgetExceeded,
    RevealResult,
    reveal,
)
from .run_context import (
    ArtifactDriftError,
    DeterminismAuthorization,
    DeterminismAuthorizationError,
    EvaluationRunContext,
    RunContextError,
    WriterLockError,
    inspect_writer_lock,
)
from .schema import (
    CandidateSnapshot,
    CandidateSnapshotError,
    CandidateTier,
    ConfigurationSpec,
    IndexDefinition,
    QuerySpec,
    load_tier1_candidate_snapshot,
    load_tier2_candidate_snapshot,
)

__all__ = [
    "CandidateSnapshot",
    "CandidateSnapshotError",
    "CandidateTier",
    "ConfigurationSpec",
    "ArtifactDriftError",
    "DeterminismAuthorization",
    "DeterminismAuthorizationError",
    "EpochFingerprintError",
    "EvidenceStoreError",
    "EvaluationRunContext",
    "IndexDefinition",
    "ExactResponseKey",
    "ManifestError",
    "MissingOptimizerResponseError",
    "OptimizerCallBudgetExceeded",
    "QuerySpec",
    "RevealResult",
    "RunContextError",
    "WriterLockError",
    "build_manifest",
    "collect_epoch_fingerprint",
    "compute_epoch_hash",
    "initialize_run",
    "inspect_writer_lock",
    "load_tier1_candidate_snapshot",
    "load_tier2_candidate_snapshot",
    "reveal",
    "validate_epoch_hash",
    "validate_manifest",
    "write_manifest_atomic",
]
