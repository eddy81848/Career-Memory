"""Unit & Governance Tests for M4 Wave 4 T-407A Claim & Refetch Evaluation.

Verifies without needing PostgreSQL:
- Single-entrypoint anti-misload guard (rejects APPROVAL_MANIFEST.json, CANDIDATE_MANIFEST.json,
  and any non-0.4.0 manifest path).
- Frozen 0.4.0-synthetic-golden SHA-256 integrity across all 10 artifacts + composite bundle SHA.
- Exact approved counts (16 scenarios, 29 claims, 50 atoms, 34 candidates, 38 validations,
  31 canonical facts, 35 supports, 27 refetch cases, 17 families).
- Strict DEV/TEST partition isolation (0 family, scenario, claim, refetch, or revision overlap).
- TEST_FIXTURE_ONLY_NOT_DOMAIN_API label isolation (zero leakage into production API enums).
- Live external LLM hard-OFF default verification (OD-002 / OD-014).
- Deterministic bundle comparator & Plan C canary self-audit rejection on deliberate leaks.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from career_memory_api.claim_eval import (
    EXPECTED_APPROVED_BUNDLE_SHA256,
    EXPECTED_APPROVED_COUNTS,
    EXPECTED_APPROVED_DATASET_VERSION,
    EXPECTED_CANDIDATE_MANIFEST_SHA256,
    GoldenDatasetMisloadError,
    assert_golden_approval_manifest_only,
    default_repo_root,
    diff_deterministic_bundles,
    golden_approval_manifest_path,
    load_frozen_golden_dataset,
    scan_object_for_canaries,
    verify_live_llm_hard_off,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimStatus,
    ClaimVerification,
    FactStatus,
    FactType,
)


def test_single_entrypoint_rejects_archived_and_candidate_manifests() -> None:
    root = default_repo_root()
    valid_path = golden_approval_manifest_path(root)
    assert_golden_approval_manifest_only(valid_path, repo_root=root)

    for forbidden in (
        root / "evaluation/claim-draft/APPROVAL_MANIFEST.json",
        root / "evaluation/claim-draft/CANDIDATE_MANIFEST.json",
        root / "evaluation/claim-draft/expected/oracle_draft.json",
        Path("/tmp/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json"),
    ):
        with pytest.raises(GoldenDatasetMisloadError):
            assert_golden_approval_manifest_only(forbidden, repo_root=root)


def test_frozen_040_dataset_integrity_counts_and_partition_isolation() -> None:
    ds = load_frozen_golden_dataset(run_subprocess_validators=True)
    assert ds.dataset_version == EXPECTED_APPROVED_DATASET_VERSION
    assert ds.approved_candidate_bundle_sha256 == EXPECTED_APPROVED_BUNDLE_SHA256
    assert ds.candidate_manifest_sha256 == EXPECTED_CANDIDATE_MANIFEST_SHA256
    assert len(ds.source_revisions) == EXPECTED_APPROVED_COUNTS["source_revisions"]
    assert len(ds.content_units) == EXPECTED_APPROVED_COUNTS["content_units"]
    assert len(ds.stage2_doc["fact_candidates"]) == EXPECTED_APPROVED_COUNTS["fact_candidates"]
    assert len(ds.stage2_doc["canonical_facts"]) == EXPECTED_APPROVED_COUNTS["canonical_facts"]
    assert len(ds.stage3_doc["claims"]) == EXPECTED_APPROVED_COUNTS["claims"]
    assert len(ds.stage3_doc["claim_atoms"]) == EXPECTED_APPROVED_COUNTS["claim_atoms"]
    assert len(ds.stage4_doc["refetch_checks"]) == EXPECTED_APPROVED_COUNTS["refetch_cases"]

    dev = ds.partitions["SYNTHETIC_REGRESSION_DEV"]
    test = ds.partitions["SYNTHETIC_REGRESSION_TEST"]
    for key in ("family_ids", "scenario_ids", "claim_ids", "refetch_fixture_case_ids"):
        assert set(dev[key]).isdisjoint(set(test[key])), f"Leakage across DEV/TEST on {key}"

    assert len(ds.canary_tokens) >= 1
    assert any("CM_SYNTHETIC_CANARY" in tok for tok in ds.canary_tokens)


def test_fixture_only_taxonomy_never_leaks_into_product_enums() -> None:
    ds = load_frozen_golden_dataset(run_subprocess_validators=False)
    fixture_only_labels: set[str] = set()
    for ef in ds.expected_oracle["expected_facts"]:
        fixture_only_labels.add(str(ef["expected_promotion_decision"]))
    for ec in ds.expected_oracle["expected_claims"]:
        cls = ec.get("fixture_test_classification", {})
        if isinstance(cls, dict):
            for v in cls.values():
                if isinstance(v, str) and v != "TEST_FIXTURE_ONLY_NOT_DOMAIN_API":
                    fixture_only_labels.add(v)

    product_enum_values = (
        {e.value for e in FactStatus}
        | {e.value for e in FactType}
        | {e.value for e in ClaimStatus}
        | {e.value for e in ClaimVerification}
        | {e.value for e in ClaimAtomStatus}
    )
    assert "TEST_FIXTURE_ONLY_NOT_DOMAIN_API" not in product_enum_values
    assert "PARTIALLY_GROUNDED" not in product_enum_values
    assert "DELETED_REVIEW_REQUIRED" not in product_enum_values
    assert "CONFLICTING_REVIEW_REQUIRED" not in product_enum_values
    assert "IDENTITY_UNRESOLVED_REVIEW_REQUIRED" not in product_enum_values
    assert len(fixture_only_labels) > 0


def test_live_llm_hard_off_default_verified() -> None:
    assert verify_live_llm_hard_off() is True


def test_canary_scanner_and_bundle_diff_comparator() -> None:
    canaries = ("CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST",)
    assert scan_object_for_canaries({"safe": "metadata-only"}, canaries) == []
    hits = scan_object_for_canaries(
        {"nested": ["prefix", "CM_SYNTHETIC_CANARY_RAW_TEXT_7F9A2C_DO_NOT_PERSIST"]},
        canaries,
    )
    assert hits == list(canaries)

    base = {
        "schema_version": "cm-t407a-claim-eval-bundle.v1",
        "safety_invariants": {"claims_passed": 29, "total_plan_c_leaks": 0},
        "volatile_runtime_metrics": {"total_duration_ms": 1234.5},
    }
    same_with_different_timing = copy.deepcopy(base)
    same_with_different_timing["volatile_runtime_metrics"]["total_duration_ms"] = 9999.9
    assert diff_deterministic_bundles(base, same_with_different_timing) == []

    drifted = copy.deepcopy(base)
    drifted["safety_invariants"]["total_plan_c_leaks"] = 1
    diffs = diff_deterministic_bundles(base, drifted)
    assert len(diffs) == 1
    assert "total_plan_c_leaks" in diffs[0]
