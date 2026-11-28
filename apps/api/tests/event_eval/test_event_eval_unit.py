"""Unit tests for M3 T-306 / T-306A loader anti-misload guards, read-only comparator, and R-002 telemetry."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from career_memory_api.event_eval import (
    EXPECTED_APPROVED_21_BUNDLE_SHA256,
    EXPECTED_APPROVED_DATASET_VERSION,
    SUPERSEDED_EV002_REL_PATH,
    ApprovedReleaseMisloadError,
    CommandExecutionRecord,
    CurationTelemetryCollector,
    ObservedCaseSnapshot,
    ObservedHeldItem,
    ObservedOutOfWindowDedup,
    ObservedSearchCheck,
    ObservedWorkEvent,
    assert_approved_manifest_path_only,
    compare_case_against_frozen_oracle,
    default_repo_root,
    diff_deterministic_bundles,
    load_approved_21_cases,
    record_curation_telemetry_sample,
    validate_loaded_case_composition,
)


# =============================================================================
# 1. Anti-Misload Guards (T-306A-HANDOFF.md §1)
# =============================================================================
def test_loader_loads_exact_21_approved_cases_with_verified_sha256() -> None:
    manifest, cases = load_approved_21_cases()
    assert manifest["dataset_version"] == EXPECTED_APPROVED_DATASET_VERSION
    assert (
        manifest["integrity_hashes"]["approved_21_bundle_sha256"]
        == EXPECTED_APPROVED_21_BUNDLE_SHA256
    )
    assert len(cases) == 21
    case_ids = {c.case_id for c in cases}
    assert "EV-002" in case_ids
    assert "EV-017" not in case_ids
    assert "EV-018" not in case_ids
    assert not any(cid.startswith("NEG-") for cid in case_ids)

    ev002 = next(c for c in cases if c.case_id == "EV-002")
    assert ev002.source_record_id == "DELTA-EV-002"
    assert ev002.origin == "REPLACED_FROM_M0_GATE_DELTA"
    assert not ev002.rel_path.endswith(SUPERSEDED_EV002_REL_PATH)

    ev021 = next(c for c in cases if c.case_id == "EV-021")
    assert ev021.confirmed_api_route_handoff == "POST /work-events/actions/merge"


def test_anti_misload_rejects_baseline_1_0_0_manifest_alone() -> None:
    root = default_repo_root()
    baseline_manifest = root / "evaluation" / "event-draft" / "manifest.json"
    assert baseline_manifest.is_file()
    with pytest.raises(ApprovedReleaseMisloadError, match="1.0.0-mvp-approved"):
        assert_approved_manifest_path_only(baseline_manifest, repo_root=root)
    with pytest.raises(ApprovedReleaseMisloadError, match="1.0.0-mvp-approved"):
        load_approved_21_cases(repo_root=root, manifest_path=baseline_manifest)


def test_anti_misload_rejects_naive_union_duplicating_ev002_and_leaking_post_mvp() -> None:
    _, valid_cases = load_approved_21_cases()
    valid_dicts = [
        {
            "case_id": c.case_id,
            "source_record_id": c.source_record_id,
            "path": c.rel_path,
        }
        for c in valid_cases
    ]
    # 1. Duplicate EV-002 (baseline + DELTA-EV-002)
    dup_union = list(valid_dicts) + [
        {
            "case_id": "EV-002",
            "source_record_id": "EV-002",
            "path": SUPERSEDED_EV002_REL_PATH,
        }
    ]
    with pytest.raises(ApprovedReleaseMisloadError, match="Duplicate case_id"):
        validate_loaded_case_composition(dup_union)

    # 2. Superseded baseline EV-002 replacing DELTA-EV-002
    superseded_swap = [
        (
            {
                "case_id": "EV-002",
                "source_record_id": "EV-002",
                "path": SUPERSEDED_EV002_REL_PATH,
            }
            if c["case_id"] == "EV-002"
            else c
        )
        for c in valid_dicts
    ]
    with pytest.raises(ApprovedReleaseMisloadError, match="Superseded baseline EV-002"):
        validate_loaded_case_composition(superseded_swap)

    # 3. Post-MVP EV-017 / EV-018 leakage
    with pytest.raises(ApprovedReleaseMisloadError, match="Forbidden excluded case"):
        validate_loaded_case_composition(
            valid_dicts + [{"case_id": "EV-017", "source_record_id": "EV-017", "path": "cases/EV-017.json"}]
        )

    # 4. NEG-001 leakage
    with pytest.raises(ApprovedReleaseMisloadError, match="Forbidden excluded case"):
        validate_loaded_case_composition(
            valid_dicts + [{"case_id": "NEG-001", "source_record_id": "NEG-001", "path": "negative-fixtures/NEG-001.json"}]
        )


# =============================================================================
# 2. Read-Only Oracle Comparator Negative Mutation Detection
# =============================================================================
def _sample_observed_event(**overrides: object) -> ObservedWorkEvent:
    base = {
        "event_uuid": "00000000-0000-4000-8000-000000000001",
        "logical_event_id": "ev_exp_001_1",
        "workspace_id": "ws_syn_001",
        "repo_id": "repo_syn_001",
        "anchor_mode": "PR",
        "lifecycle_status": "CANDIDATE",
        "is_active_top_level": True,
        "version": 1,
        "title": "Add idempotent payment retry guard",
        "summary": None,
        "primary_pr_id": "pr_syn_101",
        "source_commit_ids": ("cmt_syn_101a", "cmt_syn_101b"),
        "context_ticket_ids": (),
        "cannot_link_source_ids": (),
        "member_event_ids": (),
        "parent_event_id": None,
        "composite_membership_status": None,
        "composite_direct_must_link_count": 3,
        "member_snapshots_preserved": False,
        "prior_version_preserved": True,
        "version_numbers": (1,),
        "searchable_as_top_level": True,
        "counted_in_metrics": True,
        "review_flags": (),
        "requires_user_confirmation": False,
        "no_match_observed_means_definitely_no_pr": None,
        "anchor_scope": None,
        "dependent_fact_ids": (),
        "dependent_claim_ids": (),
        "merge_origin": "NONE",
        "api_detail_verified": True,
    }
    base.update(overrides)
    return ObservedWorkEvent(**base)  # type: ignore[arg-type]


def _sample_snapshot(
    case_id: str,
    *,
    events: tuple[ObservedWorkEvent, ...],
    search_checks: tuple[ObservedSearchCheck, ...] = (),
    held_items: tuple[ObservedHeldItem, ...] = (),
    oow_dedups: tuple[ObservedOutOfWindowDedup, ...] = (),
    total_countable: int = 1,
    total_composites: int = 0,
    total_direct: int = 0,
    total_rejected: int = 0,
    top_level_dups: int = 0,
) -> ObservedCaseSnapshot:
    return ObservedCaseSnapshot(
        case_id=case_id,
        events=events,
        review_actions=(),
        downstream_artifacts=(),
        held_items=held_items,
        out_of_window_dedups=oow_dedups,
        search_checks=search_checks,
        per_workspace_api_counts={"ws_syn_001": total_countable},
        total_countable_events=total_countable,
        total_user_composite_events=total_composites,
        total_direct_commit_events=total_direct,
        total_held_items=len(held_items),
        total_rejected_events=total_rejected,
        promoted_events_from_partial_runs=0,
        source_provenance_counts_before={"source_objects": 3},
        source_provenance_counts_after={"source_objects": 3},
        dissolve_outbox_event_count=0,
        merge_outbox_event_count=0,
        top_level_duplicate_count=top_level_dups,
        logical_state_sha256="deadbeef",
    )


def test_oracle_comparator_detects_negative_mutations() -> None:
    _, cases = load_approved_21_cases()
    ev001_data = next(c.data for c in cases if c.case_id == "EV-001")
    ev015_data = next(c.data for c in cases if c.case_id == "EV-015")

    # 1. Context-only leak into MUST_LINK source_commit_ids + wrong lifecycle_status
    bad_ev = _sample_observed_event(
        lifecycle_status="CONFIRMED",
        source_commit_ids=("cmt_syn_101a", "cmt_syn_101b", "tkt_leaked"),
        context_ticket_ids=("tkt_leaked",),
    )
    bad_snap = _sample_snapshot(
        "EV-001",
        events=(bad_ev,),
        search_checks=(
            ObservedSearchCheck(
                query="payment retry guard",
                workspace_id="ws_syn_001",
                matched_event_uuids=(bad_ev.event_uuid,),
                result_count=1,
                duplicate_count=0,
            ),
        ),
        top_level_dups=1,
    )
    cmp_bad = compare_case_against_frozen_oracle(
        case_id="EV-001",
        case_data=ev001_data,
        observed=bad_snap,
        command_records=(),
    )
    assert not cmp_bad.matched
    diff_paths = {d.path for d in cmp_bad.diffs}
    assert "events[ev_exp_001_1].lifecycle_status" in diff_paths
    assert "events[ev_exp_001_1].must_link_context_disjoint" in diff_paths
    assert "aggregation_check.top_level_duplicate_count" in diff_paths

    # 2. Rejected command that illegally mutated DB state OR returned wrong spec_error_code
    ev015_ev = _sample_observed_event(
        logical_event_id="ev_init_015_1",
        workspace_id="ws_syn_015",
        repo_id="repo_syn_015",
        version=2,
        primary_pr_id="pr_syn_1501",
        source_commit_ids=("cmt_syn_1501a",),
        title="Add ledger reconciliation drift check",
    )
    ev015_snap = _sample_snapshot(
        "EV-015",
        events=(ev015_ev,),
        search_checks=(
            ObservedSearchCheck(
                query="reconciliation drift",
                workspace_id="ws_syn_015",
                matched_event_uuids=(ev015_ev.event_uuid,),
                result_count=1,
                duplicate_count=0,
            ),
        ),
    )
    mutated_reject = CommandExecutionRecord(
        command_id="cmd_syn_015_stale_exclude",
        command_action="exclude",
        http_method="POST",
        http_path="/v1/work-events/x/actions/reject",
        http_status=409,
        outcome="REJECTED",
        error_code="WRONG_ERROR_CODE",
        internal_policy_code=None,
        state_mutated=True,
        pre_state_sha256="aaa",
        post_state_sha256="bbb",
        resulting_event_ids=(),
        latency_ms=1.0,
    )
    cmp_rej = compare_case_against_frozen_oracle(
        case_id="EV-015",
        case_data=ev015_data,
        observed=ev015_snap,
        command_records=(mutated_reject,),
    )
    assert not cmp_rej.matched
    rej_paths = {d.path for d in cmp_rej.diffs}
    assert "command_results[cmd_syn_015_stale_exclude].state_mutated" in rej_paths
    assert "command_results[cmd_syn_015_stale_exclude].zero_mutation_state_hash" in rej_paths
    assert "command_results[cmd_syn_015_stale_exclude].spec_error_code" in rej_paths


# =============================================================================
# 3. R-002 Curation Usability Telemetry Partition Separation
# =============================================================================
def test_telemetry_strictly_separates_synthetic_regression_and_closed_pilot_partitions() -> None:
    collector = CurationTelemetryCollector()
    record_curation_telemetry_sample(
        collector,
        partition="SYNTHETIC_CONTRACT_REGRESSION",
        case_or_session_id="EV-001",
        anchor_mode="PR",
        outcome="CANDIDATE_GENERATED",
        review_duration_ms=12.5,
    )
    record_curation_telemetry_sample(
        collector,
        partition="SYNTHETIC_CONTRACT_REGRESSION",
        case_or_session_id="EV-002",
        anchor_mode="DIRECT_COMMIT",
        outcome="CANDIDATE_GENERATED",
        review_duration_ms=14.0,
        review_flags_present=("DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED",),
    )
    record_curation_telemetry_sample(
        collector,
        partition="SYNTHETIC_CONTRACT_REGRESSION",
        case_or_session_id="EV-009",
        anchor_mode="USER_COMPOSITE",
        action="MERGE",
        outcome="ACCEPTED",
        review_duration_ms=25.0,
        post_merge_top_level_duplicates=0,
    )

    det, vol = collector.summarize()
    assert det["governance_policy"]["auto_clustering_enabled"] is False
    assert det["governance_policy"]["strict_partition_separation"] is True

    # Closed pilot partition MUST remain completely honest pre-pilot (0 / null)!
    pilot = det["partitions"]["CLOSED_PILOT_USER_TELEMETRY"]
    assert pilot["status"] == "NOT_YET_COLLECTED_PRE_PILOT"
    assert pilot["pilot_session_count"] == 0
    assert pilot["events_reviewed_count"] == 0
    assert pilot["candidate_anchor_counts"]["PR"] == 0
    assert vol["CLOSED_PILOT_USER_TELEMETRY"]["review_time_p50_ms"] is None
    assert vol["CLOSED_PILOT_USER_TELEMETRY"]["review_time_p95_ms"] is None

    # Synthetic partition reflects synthetic observations only
    syn = det["partitions"]["SYNTHETIC_CONTRACT_REGRESSION"]
    assert syn["classification"] == "CONTRACT_REGRESSION_ONLY_NOT_ARCHITECTURE_GO_NO_GO"
    assert syn["candidate_anchor_counts"]["PR"] == 1
    assert syn["candidate_anchor_counts"]["DIRECT_COMMIT"] == 1
    assert syn["candidate_anchor_counts"]["direct_commit_to_pr_ratio"] == 1.0
    assert syn["user_composite_merge_stats"]["accepted_merges"] == 1
    assert syn["post_merge_top_level_search_export_duplicate_count"] == 0
    assert vol["SYNTHETIC_CONTRACT_REGRESSION"]["execution_or_review_p50_ms"] == 14.0

    # Adding a real closed-pilot sample updates ONLY CLOSED_PILOT_USER_TELEMETRY
    record_curation_telemetry_sample(
        collector,
        partition="CLOSED_PILOT_USER_TELEMETRY",
        case_or_session_id="pilot-session-001",
        event_id="ev-1",
        anchor_mode="PR",
        action="CONFIRM",
        outcome="ACCEPTED",
        review_duration_ms=8200.0,
    )
    det2, vol2 = collector.summarize()
    assert det2["partitions"]["CLOSED_PILOT_USER_TELEMETRY"]["status"] == "COLLECTED"
    assert det2["partitions"]["CLOSED_PILOT_USER_TELEMETRY"]["pilot_session_count"] == 1
    assert det2["partitions"]["CLOSED_PILOT_USER_TELEMETRY"]["action_counts"]["CONFIRM"] == 1
    assert vol2["CLOSED_PILOT_USER_TELEMETRY"]["execution_or_review_p50_ms"] == 8200.0
    # Synthetic partition unchanged
    assert det2["partitions"]["SYNTHETIC_CONTRACT_REGRESSION"]["action_counts"]["CONFIRM"] == 0


# =============================================================================
# 4. Deterministic Bundle Diffing (Volatile Key Separation)
# =============================================================================
def test_bundle_diff_ignores_volatile_timings_and_catches_deterministic_drift() -> None:
    base = {
        "schema_version": "cm-t306a-event-eval-bundle.v1",
        "summary": {"passed_cases": 21, "failed_cases": 0},
        "volatile_runtime_metrics": {"total_suite_execution_ms": 1234.5},
    }
    different_timing = copy.deepcopy(base)
    different_timing["volatile_runtime_metrics"]["total_suite_execution_ms"] = 9876.5
    assert diff_deterministic_bundles(base, different_timing) == []

    drifted = copy.deepcopy(base)
    drifted["summary"]["passed_cases"] = 20
    diffs = diff_deterministic_bundles(base, drifted)
    assert len(diffs) == 1
    assert diffs[0]["path"] == "$.summary.passed_cases"


def test_committed_bundle_contains_no_local_absolute_paths() -> None:
    bundle_path = (
        default_repo_root() / "contracts" / "event-eval" / "t306a_eval_bundle.v1.json"
    )
    assert bundle_path.is_file()
    raw = bundle_path.read_text(encoding="utf-8")
    assert "/Users/" not in raw
    assert "/home/" not in raw
