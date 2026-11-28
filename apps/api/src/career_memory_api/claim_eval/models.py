"""Typed models and authoritative constants for M4 Wave 4 T-407A Claim & Refetch Evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVAL_BUNDLE_SCHEMA_VERSION = "cm-t407a-claim-eval-bundle.v1"
EXPECTED_APPROVED_DATASET_VERSION = "0.4.0-synthetic-golden"
EXPECTED_CANDIDATE_DATASET_VERSION = "0.4.0-owner-review-candidate"
EXPECTED_APPROVED_BUNDLE_SHA256 = (
    "sha256:27b6d1eeace3e4ac5771f1abb1555a2af3c6cd9446a9f7f10005404572d7b059"
)
EXPECTED_CANDIDATE_MANIFEST_SHA256 = (
    "sha256:2e9ffde13dfa619cee9035fdf2f9746adfd6b925875a7a955790e210b4a0bcea"
)
IMMUTABLE_OPENAPI_BLOB_SHA = "07594d75425744d6149250a9f7ff95ac24659f80"

EXPECTED_SCHEMA_MIGRATIONS: tuple[str, ...] = (
    "0010_identity_workspace_source_auth.sql",
    "0020_source_provenance_metadata.sql",
    "0030_events_facts_claims_contributions.sql",
    "0040_sync_runs_jobs_outbox_audit.sql",
    "0050_m2_backfill_observations.sql",
    "0060_m3_event_versions_and_constraints.sql",
    "0070_m4_fact_versions_and_exports.sql",
)

EXPECTED_APPROVED_COUNTS: dict[str, int] = {
    "source_revisions": 20,
    "content_units": 30,
    "fact_candidates": 34,
    "fact_support_validations": 38,
    "canonical_facts": 31,
    "fact_supports": 35,
    "confirmed_events": 16,
    "scenarios": 16,
    "claims": 29,
    "claim_atoms": 50,
    "claim_atom_supports": 50,
    "refetch_cases": 27,
    "evaluation_families": 17,
}

SYNTHETIC_GATE_STATUS_PASSED = "PASSED"
SYNTHETIC_GATE_STATUS_FAILED = "FAILED"
LIVE_LLM_SEMANTIC_EVAL_BLOCKED = "NOT_RUN_BLOCKED_BY_OD002_OD014"

# Volatile keys excluded from --verify-bundle deterministic drift checks
VOLATILE_BUNDLE_KEYS: frozenset[str] = frozenset(
    {
        "volatile_runtime_metrics",
    }
)


@dataclass(frozen=True, slots=True)
class OracleDiffEntry:
    """Single read-only path-level mismatch between frozen 0.4.0 oracle and live PG18 result."""

    path: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class PlanCMultiSurfaceAuditResult:
    """Multi-surface Plan C (ADR-013 / REF-04) non-persistence audit result."""

    canaries_checked: int
    raw_texts_checked: int
    pg_tables_scanned: int
    pg_canary_hits: int
    pg_raw_text_hits: int
    content_units_plan_c_compliant: bool
    fact_support_plan_c_compliant: bool
    jsonb_forbidden_key_hits: int
    log_records_scanned: int
    log_canary_hits: int
    http_responses_scanned: int
    http_metadata_canary_hits: int
    export_artifacts_scanned: int
    export_canary_hits: int
    cache_entries_remaining: int
    negative_leak_detector_verified: bool
    leak_locations: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.pg_canary_hits == 0
            and self.pg_raw_text_hits == 0
            and self.content_units_plan_c_compliant
            and self.fact_support_plan_c_compliant
            and self.jsonb_forbidden_key_hits == 0
            and self.log_canary_hits == 0
            and self.http_metadata_canary_hits == 0
            and self.export_canary_hits == 0
            and self.cache_entries_remaining == 0
            and self.negative_leak_detector_verified
            and len(self.leak_locations) == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canaries_checked": self.canaries_checked,
            "raw_texts_checked": self.raw_texts_checked,
            "pg_tables_scanned": self.pg_tables_scanned,
            "pg_canary_hits": self.pg_canary_hits,
            "pg_raw_text_hits": self.pg_raw_text_hits,
            "content_units_plan_c_compliant": self.content_units_plan_c_compliant,
            "fact_support_plan_c_compliant": self.fact_support_plan_c_compliant,
            "jsonb_forbidden_key_hits": self.jsonb_forbidden_key_hits,
            "log_records_scanned": self.log_records_scanned,
            "log_canary_hits": self.log_canary_hits,
            "http_responses_scanned": self.http_responses_scanned,
            "http_metadata_canary_hits": self.http_metadata_canary_hits,
            "export_artifacts_scanned": self.export_artifacts_scanned,
            "export_canary_hits": self.export_canary_hits,
            "cache_entries_remaining": self.cache_entries_remaining,
            "negative_leak_detector_verified": self.negative_leak_detector_verified,
            "passed": self.passed,
            "leak_locations": list(self.leak_locations),
        }


@dataclass(frozen=True, slots=True)
class FactCandidateEvalResult:
    """Observed Stage 2A/2B outcome for a single fixture fact candidate."""

    fact_candidate_id: str
    scenario_id: str
    expected_fact_type: str
    expected_canonical_fact_id: str | None
    expected_active_supported_fact: bool
    observed_fact_status: str
    observed_active_supported_fact: bool
    distinct_validator_run_id: bool
    extracted_span_is_null: bool
    matched: bool
    diffs: tuple[OracleDiffEntry, ...] = ()

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "fact_candidate_id": self.fact_candidate_id,
            "scenario_id": self.scenario_id,
            "expected_fact_type": self.expected_fact_type,
            "expected_canonical_fact_id": self.expected_canonical_fact_id,
            "expected_active_supported_fact": self.expected_active_supported_fact,
            "observed_fact_status": self.observed_fact_status,
            "observed_active_supported_fact": self.observed_active_supported_fact,
            "distinct_validator_run_id": self.distinct_validator_run_id,
            "extracted_span_is_null": self.extracted_span_is_null,
            "matched": self.matched,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
        }


@dataclass(frozen=True, slots=True)
class AtomEvalResult:
    """Observed Stage 3B/3C outcome for a single fixture ClaimAtom."""

    claim_atom_id: str
    claim_id: str
    scenario_id: str
    expected_domain_atom_status: str
    observed_domain_atom_status: str
    od003_purged_after_cleanup: bool
    matched: bool
    diffs: tuple[OracleDiffEntry, ...] = ()

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "claim_atom_id": self.claim_atom_id,
            "claim_id": self.claim_id,
            "scenario_id": self.scenario_id,
            "expected_domain_atom_status": self.expected_domain_atom_status,
            "observed_domain_atom_status": self.observed_domain_atom_status,
            "od003_purged_after_cleanup": self.od003_purged_after_cleanup,
            "matched": self.matched,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
        }


@dataclass(frozen=True, slots=True)
class ClaimEvalResult:
    """Observed Stage 3A/3B/3C + Approval + Export gate outcome for a single fixture Claim."""

    claim_id: str
    scenario_id: str
    evaluation_partition: str
    claim_version: int
    expected_source_verified_computed: bool
    observed_computed_verification: str
    observed_source_verified_computed: bool
    observed_claim_status: str
    pairwise_distinct_stage_run_ids: bool
    provenance_drilldown_verified: bool
    approval_attempt_http_status: int
    approval_allowed: bool
    export_attempt_http_status: int
    export_allowed: bool
    od003_purged_verified: bool | None
    atom_results: tuple[AtomEvalResult, ...]
    matched: bool
    diffs: tuple[OracleDiffEntry, ...] = ()

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "scenario_id": self.scenario_id,
            "evaluation_partition": self.evaluation_partition,
            "claim_version": self.claim_version,
            "expected_source_verified_computed": self.expected_source_verified_computed,
            "observed_computed_verification": self.observed_computed_verification,
            "observed_source_verified_computed": self.observed_source_verified_computed,
            "observed_claim_status": self.observed_claim_status,
            "pairwise_distinct_stage_run_ids": self.pairwise_distinct_stage_run_ids,
            "provenance_drilldown_verified": self.provenance_drilldown_verified,
            "approval_attempt_http_status": self.approval_attempt_http_status,
            "approval_allowed": self.approval_allowed,
            "export_attempt_http_status": self.export_attempt_http_status,
            "export_allowed": self.export_allowed,
            "od003_purged_verified": self.od003_purged_verified,
            "atoms": [a.to_deterministic_dict() for a in self.atom_results],
            "matched": self.matched,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
        }


@dataclass(frozen=True, slots=True)
class ScenarioEvalResult:
    """End-to-end PG18 + FastAPI evaluation result for one scenario (SCN-01..SCN-16)."""

    scenario_id: str
    evaluation_family_id: str
    evaluation_partition: str
    notion_test_ids: tuple[str, ...]
    expected_candidate_count: int
    observed_candidate_count: int
    expected_fact_count: int
    observed_fact_count: int
    expected_atom_count: int
    observed_atom_count: int
    fact_results: tuple[FactCandidateEvalResult, ...]
    claim_results: tuple[ClaimEvalResult, ...]
    plan_c_audit: PlanCMultiSurfaceAuditResult
    assertions_checked: int
    diffs: tuple[OracleDiffEntry, ...] = ()
    execution_ms: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            len(self.diffs) == 0
            and all(f.matched for f in self.fact_results)
            and all(c.matched for c in self.claim_results)
            and self.plan_c_audit.passed
        )

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "evaluation_family_id": self.evaluation_family_id,
            "evaluation_partition": self.evaluation_partition,
            "notion_test_ids": list(self.notion_test_ids),
            "expected_candidate_count": self.expected_candidate_count,
            "observed_candidate_count": self.observed_candidate_count,
            "expected_fact_count": self.expected_fact_count,
            "observed_fact_count": self.observed_fact_count,
            "expected_atom_count": self.expected_atom_count,
            "observed_atom_count": self.observed_atom_count,
            "passed": self.passed,
            "assertions_checked": self.assertions_checked,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
            "fact_results": [f.to_deterministic_dict() for f in self.fact_results],
            "claim_results": [c.to_deterministic_dict() for c in self.claim_results],
            "plan_c_audit": self.plan_c_audit.to_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RefetchCaseEvalResult:
    """Real PG18 + FastAPI + RefetchResolverService result for one REF-01..08 case."""

    fixture_case_id: str
    official_ref_id: str
    evaluation_family_id: str
    evaluation_partition: str
    observed_resolution_status: str | None
    observed_fetch_attempted: bool
    observed_unverifiable_now: bool
    observed_approval_blocked: bool
    observed_source_deleted_confirmed: bool
    observed_historical_fact_falsified: bool
    observed_claim_auto_mutated: bool
    observed_canary_leaked_in_clean_snapshot: bool
    observed_canary_detected_in_negative_fixture: bool | None
    observed_extra_assertions: dict[str, Any]
    plan_c_audit: PlanCMultiSurfaceAuditResult
    assertions_checked: int
    diffs: tuple[OracleDiffEntry, ...] = ()
    execution_ms: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return len(self.diffs) == 0 and self.plan_c_audit.passed

    def to_deterministic_dict(self) -> dict[str, Any]:
        return {
            "fixture_case_id": self.fixture_case_id,
            "official_ref_id": self.official_ref_id,
            "evaluation_family_id": self.evaluation_family_id,
            "evaluation_partition": self.evaluation_partition,
            "observed_resolution_status": self.observed_resolution_status,
            "observed_fetch_attempted": self.observed_fetch_attempted,
            "observed_unverifiable_now": self.observed_unverifiable_now,
            "observed_approval_blocked": self.observed_approval_blocked,
            "observed_source_deleted_confirmed": self.observed_source_deleted_confirmed,
            "observed_historical_fact_falsified": self.observed_historical_fact_falsified,
            "observed_claim_auto_mutated": self.observed_claim_auto_mutated,
            "observed_canary_leaked_in_clean_snapshot": self.observed_canary_leaked_in_clean_snapshot,
            "observed_canary_detected_in_negative_fixture": self.observed_canary_detected_in_negative_fixture,
            "observed_extra_assertions": self.observed_extra_assertions,
            "plan_c_audit": self.plan_c_audit.to_dict(),
            "assertions_checked": self.assertions_checked,
            "passed": self.passed,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ClaimEvalSuiteResult:
    """Complete T-407A 0.4.0-synthetic-golden evaluation result (16 scenarios, 29 claims, 27 refetch)."""

    dataset_version: str
    approved_candidate_bundle_sha256: str
    candidate_manifest_sha256: str
    approval_manifest_sha256: str
    scenario_results: tuple[ScenarioEvalResult, ...]
    refetch_results: tuple[RefetchCaseEvalResult, ...]
    live_llm_hard_off_verified: bool
    external_live_llm_calls: int
    unsupported_source_verified_count: int
    unsupported_approvals_count: int
    unsupported_or_stale_exports_count: int
    rejected_candidates_promoted_count: int
    total_plan_c_leaks: int
    volatile_runtime_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed_scenarios(self) -> int:
        return sum(1 for s in self.scenario_results if s.passed)

    @property
    def total_scenarios(self) -> int:
        return len(self.scenario_results)

    @property
    def passed_claims(self) -> int:
        return sum(1 for s in self.scenario_results for c in s.claim_results if c.matched)

    @property
    def total_claims(self) -> int:
        return sum(len(s.claim_results) for s in self.scenario_results)

    @property
    def passed_atoms(self) -> int:
        return sum(
            1
            for s in self.scenario_results
            for c in s.claim_results
            for a in c.atom_results
            if a.matched
        )

    @property
    def total_atoms(self) -> int:
        return sum(
            len(c.atom_results) for s in self.scenario_results for c in s.claim_results
        )

    @property
    def passed_refetch_cases(self) -> int:
        return sum(1 for r in self.refetch_results if r.passed)

    @property
    def total_refetch_cases(self) -> int:
        return len(self.refetch_results)

    @property
    def all_passed(self) -> bool:
        return (
            self.total_scenarios == 16
            and self.passed_scenarios == 16
            and self.total_claims == 29
            and self.passed_claims == 29
            and self.total_atoms == 50
            and self.passed_atoms == 50
            and self.total_refetch_cases == 27
            and self.passed_refetch_cases == 27
            and self.live_llm_hard_off_verified
            and self.external_live_llm_calls == 0
            and self.unsupported_source_verified_count == 0
            and self.unsupported_approvals_count == 0
            and self.unsupported_or_stale_exports_count == 0
            and self.rejected_candidates_promoted_count == 0
            and self.total_plan_c_leaks == 0
        )


__all__ = [
    "EVAL_BUNDLE_SCHEMA_VERSION",
    "EXPECTED_APPROVED_BUNDLE_SHA256",
    "EXPECTED_APPROVED_COUNTS",
    "EXPECTED_APPROVED_DATASET_VERSION",
    "EXPECTED_CANDIDATE_DATASET_VERSION",
    "EXPECTED_CANDIDATE_MANIFEST_SHA256",
    "EXPECTED_SCHEMA_MIGRATIONS",
    "IMMUTABLE_OPENAPI_BLOB_SHA",
    "LIVE_LLM_SEMANTIC_EVAL_BLOCKED",
    "SYNTHETIC_GATE_STATUS_FAILED",
    "SYNTHETIC_GATE_STATUS_PASSED",
    "VOLATILE_BUNDLE_KEYS",
    "AtomEvalResult",
    "ClaimEvalResult",
    "ClaimEvalSuiteResult",
    "FactCandidateEvalResult",
    "OracleDiffEntry",
    "PlanCMultiSurfaceAuditResult",
    "RefetchCaseEvalResult",
    "ScenarioEvalResult",
]
