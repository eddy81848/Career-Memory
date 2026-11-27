"""B-004 Initial Backfill Fault Harness (T-005A, T-005B, T-005C, T-005D redacted evidence)."""

from career_memory_api.fault_harness.comparator import (
    ComparisonResult,
    ScenarioDiffEntry,
    compare_logical_snapshots,
    load_expected_snapshot,
)
from career_memory_api.fault_harness.fake_provider import (
    DeterministicFakeCredentialProvider,
    DeterministicFakeGitHubProvider,
    build_10page_canonical_provider,
    build_compact_multipage_provider,
)
from career_memory_api.fault_harness.fault_hooks import (
    B004LifecycleFaultHook,
    CanaryAuditResult,
    CapturingLogHandler,
    InjectedFaultCrash,
    InsidePageTxFaultInjector,
    audit_canaries_and_plan_c_compliance,
)
from career_memory_api.fault_harness.runner import (
    FIXED_NOW,
    B004PostgresScenarioRunner,
    EphemeralB004Pg18Cluster,
    PostgresDeletionBackedContractService,
    ScenarioRunResult,
    build_redacted_evidence_bundle,
)
from career_memory_api.fault_harness.scenario_schema import (
    DEFAULT_OBSERVED_AT_ISO,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    EXPECTED_SNAPSHOT_SCHEMA_VERSION,
    SCENARIO_SCHEMA_VERSION,
    CanarySetSpec,
    FaultInjectionPoint,
    ScenarioSpec,
    ScenarioValidationError,
    validate_expected_snapshot,
    validate_scenario_spec,
)
from career_memory_api.fault_harness.scenarios import (
    SCENARIO_SPECS,
    SCENARIO_SPECS_BY_ID,
    get_scenario_spec,
)
from career_memory_api.fault_harness.snapshot import (
    GateCheckObservations,
    InvariantViolation,
    LogicalDbSnapshot,
    canonical_json_sha256,
    extract_logical_db_snapshot,
    verify_b004_invariants,
)

__all__ = [
    "B004LifecycleFaultHook",
    "B004PostgresScenarioRunner",
    "CanaryAuditResult",
    "CanarySetSpec",
    "CapturingLogHandler",
    "ComparisonResult",
    "DEFAULT_OBSERVED_AT_ISO",
    "DeterministicFakeCredentialProvider",
    "DeterministicFakeGitHubProvider",
    "EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "EXPECTED_SNAPSHOT_SCHEMA_VERSION",
    "EphemeralB004Pg18Cluster",
    "FIXED_NOW",
    "FaultInjectionPoint",
    "GateCheckObservations",
    "InjectedFaultCrash",
    "InsidePageTxFaultInjector",
    "InvariantViolation",
    "LogicalDbSnapshot",
    "PostgresDeletionBackedContractService",
    "SCENARIO_SCHEMA_VERSION",
    "SCENARIO_SPECS",
    "SCENARIO_SPECS_BY_ID",
    "ScenarioDiffEntry",
    "ScenarioRunResult",
    "ScenarioSpec",
    "ScenarioValidationError",
    "audit_canaries_and_plan_c_compliance",
    "build_10page_canonical_provider",
    "build_compact_multipage_provider",
    "build_redacted_evidence_bundle",
    "canonical_json_sha256",
    "compare_logical_snapshots",
    "extract_logical_db_snapshot",
    "get_scenario_spec",
    "load_expected_snapshot",
    "validate_expected_snapshot",
    "validate_scenario_spec",
    "verify_b004_invariants",
]
