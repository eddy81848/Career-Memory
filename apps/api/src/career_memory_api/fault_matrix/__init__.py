"""M5 Wave 3 Cross-Subsystem Fault Injection & Race Matrix (`T-504` / `T-504A`)."""

from career_memory_api.fault_matrix.models import (
    T504A_EVIDENCE_SCHEMA_VERSION,
    CrossSubsystemFaultPoint,
    FaultMatrixScenarioId,
    FaultMatrixScenarioResult,
    PlanCAuditSummary,
    T504AEvidenceBundle,
    compute_canonical_sha256,
    normalize_t504a_bundle_for_drift_check,
)
from career_memory_api.fault_matrix.runner import (
    DETERMINISTIC_MATRIX_NOW,
    EphemeralFaultMatrixPg18Cluster,
    run_t504a_fault_matrix,
)

__all__ = [
    "DETERMINISTIC_MATRIX_NOW",
    "T504A_EVIDENCE_SCHEMA_VERSION",
    "CrossSubsystemFaultPoint",
    "EphemeralFaultMatrixPg18Cluster",
    "FaultMatrixScenarioId",
    "FaultMatrixScenarioResult",
    "PlanCAuditSummary",
    "T504AEvidenceBundle",
    "compute_canonical_sha256",
    "normalize_t504a_bundle_for_drift_check",
    "run_t504a_fault_matrix",
]
