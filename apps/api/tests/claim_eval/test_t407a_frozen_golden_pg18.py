"""End-to-End Real PostgreSQL 18 + FastAPI Frozen 0.4.0-Synthetic-Golden Evaluation Test (T-407A)."""

from __future__ import annotations

import json

import pytest

from career_memory_api.claim_eval import (
    EXPECTED_APPROVED_BUNDLE_SHA256,
    EXPECTED_APPROVED_DATASET_VERSION,
    IMMUTABLE_OPENAPI_BLOB_SHA,
    LIVE_LLM_SEMANTIC_EVAL_BLOCKED,
    SYNTHETIC_GATE_STATUS_PASSED,
    build_redacted_claim_eval_bundle,
    default_repo_root,
    diff_deterministic_bundles,
    run_frozen_t407a_evaluation,
)


@pytest.mark.postgres
def test_t407a_frozen_040_golden_pg18_full_eval_and_zero_drift_bundle() -> None:
    repo_root = default_repo_root()
    dataset, suite = run_frozen_t407a_evaluation(
        repo_root=repo_root,
        run_subprocess_validators=True,
    )

    assert suite.dataset_version == EXPECTED_APPROVED_DATASET_VERSION
    assert suite.approved_candidate_bundle_sha256 == EXPECTED_APPROVED_BUNDLE_SHA256
    assert suite.total_scenarios == 16
    assert suite.passed_scenarios == 16, [
        (s.scenario_id, [d.to_dict() for d in s.diffs])
        for s in suite.scenario_results
        if not s.passed
    ]
    assert suite.total_claims == 29
    assert suite.passed_claims == 29
    assert suite.total_atoms == 50
    assert suite.passed_atoms == 50
    assert suite.total_refetch_cases == 27
    assert suite.passed_refetch_cases == 27, [
        (r.fixture_case_id, [d.to_dict() for d in r.diffs])
        for r in suite.refetch_results
        if not r.passed
    ]

    # Strict safety & governance invariants (ADR-013, ADR-018, ADR-020, OD-002, OD-014)
    assert suite.live_llm_hard_off_verified is True
    assert suite.external_live_llm_calls == 0
    assert suite.unsupported_source_verified_count == 0
    assert suite.unsupported_approvals_count == 0
    assert suite.unsupported_or_stale_exports_count == 0
    assert suite.rejected_candidates_promoted_count == 0
    assert suite.total_plan_c_leaks == 0
    assert suite.all_passed is True

    # Verify redacted bundle matches committed zero-drift artifact
    actual_bundle = build_redacted_claim_eval_bundle(
        suite,
        openapi_blob_sha=IMMUTABLE_OPENAPI_BLOB_SHA,
        canary_tokens=dataset.canary_tokens,
        raw_source_texts=list(dataset.raw_source_texts_by_rev.values()),
    )
    assert (
        actual_bundle["governance_gates"]["SYNTHETIC_CONTRACT_AND_SAFETY_GATE"]
        == SYNTHETIC_GATE_STATUS_PASSED
    )
    assert (
        actual_bundle["governance_gates"]["LIVE_LLM_SEMANTIC_EVAL"]
        == LIVE_LLM_SEMANTIC_EVAL_BLOCKED
    )
    assert actual_bundle["governance_gates"]["live_pilot_gate_eligible"] is False
    assert actual_bundle["governance_gates"]["population_accuracy_claim_permitted"] is False

    committed_path = repo_root / "contracts/claim-eval/t407a_eval_bundle.v1.json"
    assert committed_path.is_file(), f"Missing committed T-407A bundle: {committed_path}"
    committed_bundle = json.loads(committed_path.read_text(encoding="utf-8"))
    diffs = diff_deterministic_bundles(committed_bundle, actual_bundle)
    assert diffs == [], f"T-407A bundle drift detected: {diffs[:20]}"
