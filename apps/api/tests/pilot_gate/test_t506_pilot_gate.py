"""Pytest verification suite for T-506 Closed Pilot & 3-Tier Go/No-Go Release Gate."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from career_memory_api.conformance.ai_schemas import render_canonical_json
from career_memory_api.conformance.checks import (
    M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1,
    default_repo_root,
)
from career_memory_api.pilot_gate import (
    PilotGatePolicyViolationError,
    ReleaseGateDecision,
    ReleaseGateStatusCode,
    ReleaseGateTier,
    assert_strict_three_tier_honesty,
    evaluate_t506_pilot_gate,
    normalize_t506_gate_for_drift_check,
)


def test_t506_release_gate_verifies_all_9_bundles_and_zero_drift() -> None:
    """Verify all 9 M0-M5, Track 2 & Track 3 evidence bundles, 3-tier gate separation, and 0 drift against committed JSON."""
    repo_root = default_repo_root()
    bundle = evaluate_t506_pilot_gate(repo_root=repo_root)

    assert bundle.openapi_pin_verified is True
    assert bundle.openapi_import_v1_blob_sha == M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1
    assert bundle.external_network_calls == 0
    assert bundle.spend_krw == 0

    assert bundle.summary["bundles_total"] == 9
    assert bundle.summary["bundles_verified"] == 9
    assert bundle.summary["invariants_passed"] == bundle.summary["invariants_total"]
    assert all(b.verified for b in bundle.evidence_bundles)
    assert all(i.passed for i in bundle.invariants)

    gates = {g.tier: g for g in bundle.release_gates}
    assert gates[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE].decision is ReleaseGateDecision.GO
    assert gates[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE].status_code is ReleaseGateStatusCode.PASSED

    assert gates[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE].decision is ReleaseGateDecision.GO
    assert (
        gates[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE].status_code
        is ReleaseGateStatusCode.PASSED
    )

    assert gates[ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE].decision is ReleaseGateDecision.NO_GO
    assert (
        gates[ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE].status_code
        is ReleaseGateStatusCode.HELD_PENDING_OD002_OD014
    )

    assert_strict_three_tier_honesty(bundle)

    committed_path = repo_root / "contracts" / "pilot-gate" / "t506_release_gate.v1.json"
    assert committed_path.is_file(), "Committed t506_release_gate.v1.json must exist."
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    assert render_canonical_json(
        normalize_t506_gate_for_drift_check(committed)
    ) == render_canonical_json(
        normalize_t506_gate_for_drift_check(bundle.model_dump(mode="json"))
    )


def test_t506_fail_closed_on_missing_or_drifted_evidence_bundle(tmp_path: Path) -> None:
    """Missing local or hosted evidence bundle MUST fail closed on the corresponding gate tier."""
    repo_root = default_repo_root()
    fake_root = tmp_path / "repo"
    shutil.copytree(repo_root / "contracts", fake_root / "contracts")
    # Remove one required local evidence bundle (T-504A) -> both LOCAL and HOSTED gates fail closed
    (fake_root / "contracts" / "fault-matrix" / "t504a_evidence_bundle.v1.json").unlink()

    bundle = evaluate_t506_pilot_gate(
        repo_root=fake_root, require_live_fe_be_tests_present=False
    )
    gates = {g.tier: g for g in bundle.release_gates}
    assert gates[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE].decision is ReleaseGateDecision.NO_GO
    assert gates[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE].status_code is ReleaseGateStatusCode.FAILED
    assert gates[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE].decision is ReleaseGateDecision.NO_GO

    # Restore local bundle, remove hosted staging bundle -> LOCAL stays GO, HOSTED fails closed
    fake_root_hosted = tmp_path / "repo_no_hosted"
    shutil.copytree(repo_root / "contracts", fake_root_hosted / "contracts")
    (
        fake_root_hosted / "contracts" / "hosted-staging" / "t007f_t106b_staging_evidence.v1.json"
    ).unlink()
    bundle_no_hosted = evaluate_t506_pilot_gate(
        repo_root=fake_root_hosted, require_live_fe_be_tests_present=False
    )
    gates_nh = {g.tier: g for g in bundle_no_hosted.release_gates}
    assert gates_nh[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE].decision is ReleaseGateDecision.GO
    assert gates_nh[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE].decision is ReleaseGateDecision.NO_GO
    assert (
        gates_nh[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE].status_code
        is ReleaseGateStatusCode.HELD_PENDING_OD013_OD015
    )


def test_t506_rejects_false_promotion_of_held_hosted_or_ai_gates(tmp_path: Path) -> None:
    """assert_strict_three_tier_honesty MUST reject unverified hosted GO or any synthetic live AI GO."""
    repo_root = default_repo_root()
    fake_root = tmp_path / "repo_unverified_hosted"
    shutil.copytree(repo_root / "contracts", fake_root / "contracts")
    (fake_root / "contracts" / "hosted-staging" / "t007f_t106b_staging_evidence.v1.json").unlink()
    unverified_hosted_bundle = evaluate_t506_pilot_gate(
        repo_root=fake_root, require_live_fe_be_tests_present=False
    )

    tampered_hosted = unverified_hosted_bundle.model_copy(
        update={
            "release_gates": [
                (
                    g.model_copy(update={"decision": ReleaseGateDecision.GO})
                    if g.tier is ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE
                    else g
                )
                for g in unverified_hosted_bundle.release_gates
            ]
        }
    )
    with pytest.raises(PilotGatePolicyViolationError):
        assert_strict_three_tier_honesty(tampered_hosted)

    bundle = evaluate_t506_pilot_gate(repo_root=repo_root)
    tampered_ai = bundle.model_copy(
        update={
            "release_gates": [
                (
                    g.model_copy(update={"decision": ReleaseGateDecision.GO})
                    if g.tier is ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE
                    else g
                )
                for g in bundle.release_gates
            ]
        }
    )
    with pytest.raises(PilotGatePolicyViolationError):
        assert_strict_three_tier_honesty(tampered_ai)
