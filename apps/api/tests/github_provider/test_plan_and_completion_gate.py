"""Tests for FrozenImportPlan validation and CompletionGateEvaluator determinism (M2-T202)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career_memory_api.github_provider import (
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_MAX_OBJECTS,
    CompletionGateEvaluator,
    FrozenImportPlan,
    GitHubRepositorySnapshot,
    GitHubStreamKind,
    ImportPlanLimits,
    ImportPlanValidationError,
    MergedPullRequestCue,
    RepositoryPolicyViolationError,
    build_frozen_import_plan,
)
from career_memory_api.schema_contracts.enums import ResourceVisibility
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.sync_runs.models import SyncRunMode, SyncRunStatus

_CUTOFF = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
_START_SHA = "a" * 40
_MOVED_SHA = "b" * 40


def _make_repo_snapshot(
    *,
    provider_resource_id: str = "987654321",
    default_branch: str = "trunk",
    head_sha: str = _START_SHA,
    visibility: ResourceVisibility = ResourceVisibility.PUBLIC,
    observed_at: datetime = _CUTOFF,
) -> GitHubRepositorySnapshot:
    return GitHubRepositorySnapshot(
        provider_resource_id=provider_resource_id,
        owner="acme",
        name="career-core",
        full_name="acme/career-core",
        canonical_url="https://github.com/acme/career-core",
        visibility=visibility,
        default_branch=default_branch,
        default_branch_head_sha=head_sha,
        observed_at=observed_at,
    )


def _make_pr_cue(
    pr_number: int,
    *,
    head_sha: str | None = None,
    merged_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> MergedPullRequestCue:
    merged = merged_at or (_CUTOFF - timedelta(days=pr_number))
    updated = updated_at or (merged + timedelta(minutes=5))
    return MergedPullRequestCue(
        pr_number=pr_number,
        head_sha=head_sha or f"{pr_number:040x}",
        merged_at=merged,
        updated_at=updated,
        merge_commit_sha=f"{(pr_number + 1000):040x}",
    )


@pytest.mark.parametrize(
    "branch_name",
    ["main", "master", "trunk", "develop", "release/2026-q4"],
)
def test_frozen_import_plan_preserves_provider_default_branch_without_hardcoding(
    branch_name: str,
) -> None:
    snap = _make_repo_snapshot(default_branch=branch_name)
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=snap,
        cutoff_at=_CUTOFF,
    )
    assert isinstance(plan, FrozenImportPlan)
    assert plan.mode is SyncRunMode.INITIAL_BACKFILL
    assert plan.default_branch == branch_name
    assert plan.start_head_sha == _START_SHA
    assert plan.visibility is ResourceVisibility.PUBLIC
    assert plan.cutoff_at == _CUTOFF
    assert plan.backfill_from == _CUTOFF - timedelta(days=DEFAULT_BACKFILL_DAYS)
    assert plan.limits.max_objects == DEFAULT_MAX_OBJECTS
    assert GitHubStreamKind.REFERENCED_ISSUES in plan.optional_streams
    assert GitHubStreamKind.REFERENCED_ISSUES not in plan.required_streams

    spec = plan.to_plan_spec_dict()
    assert_no_forbidden_raw_keys(spec, field_name="plan_spec")
    assert spec["default_branch"] == branch_name
    assert spec["start_head_sha"] == _START_SHA
    assert len(plan.plan_fingerprint()) == 64


def test_frozen_import_plan_accepts_iso_date_and_rejects_invalid_window_or_limits() -> None:
    snap = _make_repo_snapshot()

    # Valid explicit YYYY-MM-DD and limits within 1..5000
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=snap,
        cutoff_at=_CUTOFF,
        backfill_from="2026-01-01",
        limits={"max_objects": 250},
    )
    assert plan.backfill_from == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert plan.limits.max_objects == 250

    # backfill_from > cutoff_at must be rejected
    with pytest.raises(ImportPlanValidationError):
        build_frozen_import_plan(
            source_resource_id="11111111-1111-4111-8111-111111111111",
            repo_snapshot=snap,
            cutoff_at=_CUTOFF,
            backfill_from="2026-12-01",
        )

    # max_objects > 5000, <= 0, or bool must be rejected
    for bad_max in (0, -5, 5001, True):
        with pytest.raises(ImportPlanValidationError):
            build_frozen_import_plan(
                source_resource_id="11111111-1111-4111-8111-111111111111",
                repo_snapshot=snap,
                cutoff_at=_CUTOFF,
                limits={"max_objects": bad_max},  # type: ignore[dict-item]
            )

    with pytest.raises(ImportPlanValidationError):
        ImportPlanLimits(max_objects=5001)


@pytest.mark.parametrize("visibility", [ResourceVisibility.PRIVATE, ResourceVisibility.INTERNAL])
def test_frozen_import_plan_rejects_non_public_repository(
    visibility: ResourceVisibility,
) -> None:
    snap = _make_repo_snapshot(visibility=visibility)
    with pytest.raises(RepositoryPolicyViolationError):
        build_frozen_import_plan(
            source_resource_id="11111111-1111-4111-8111-111111111111",
            repo_snapshot=snap,
            cutoff_at=_CUTOFF,
        )


def test_completion_gate_passes_when_branch_head_and_pr_enumeration_match() -> None:
    start_snap = _make_repo_snapshot(default_branch="develop", head_sha=_START_SHA)
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=start_snap,
        cutoff_at=_CUTOFF,
    )
    end_snap = _make_repo_snapshot(
        default_branch="develop",
        head_sha=_START_SHA,
        observed_at=_CUTOFF + timedelta(minutes=3),
    )
    cues_first = (_make_pr_cue(10), _make_pr_cue(11))
    cues_second = (_make_pr_cue(11), _make_pr_cue(10))  # Order-independent set comparison

    evaluator = CompletionGateEvaluator()
    result = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=end_snap,
        first_pass_merged_prs=cues_first,
        second_pass_merged_prs=cues_second,
    )
    assert result.passed is True
    assert result.status is SyncRunStatus.SUCCESS
    assert result.import_ready is True
    assert result.source_drift is False
    assert result.retry_allowed is False
    assert result.failure_reason_code is None
    assert result.drift_reasons == ()

    obs = result.to_sync_run_observation()
    assert obs.status is SyncRunStatus.SUCCESS
    assert obs.import_ready is True
    assert obs.source_drift is False
    assert obs.retry_allowed is False
    assert obs.failure_reason is None


def test_completion_gate_detects_head_drift_and_branch_drift_as_plan_drift() -> None:
    start_snap = _make_repo_snapshot(default_branch="trunk", head_sha=_START_SHA)
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=start_snap,
        cutoff_at=_CUTOFF,
    )
    cues = (_make_pr_cue(10),)
    evaluator = CompletionGateEvaluator()

    # 1) HEAD SHA moved
    res_head = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=_make_repo_snapshot(default_branch="trunk", head_sha=_MOVED_SHA),
        first_pass_merged_prs=cues,
        second_pass_merged_prs=cues,
    )
    assert res_head.passed is False
    assert res_head.status is SyncRunStatus.PARTIAL
    assert res_head.failure_reason_code == "PLAN_DRIFT"
    assert res_head.source_drift is True
    assert res_head.retry_allowed is False
    assert res_head.import_ready is False
    assert "DEFAULT_BRANCH_HEAD_CHANGED" in res_head.drift_reasons

    # 2) Default branch name changed
    res_branch = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=_make_repo_snapshot(default_branch="main", head_sha=_START_SHA),
        first_pass_merged_prs=cues,
        second_pass_merged_prs=cues,
    )
    assert res_branch.status is SyncRunStatus.PARTIAL
    assert res_branch.failure_reason_code == "PLAN_DRIFT"
    assert res_branch.source_drift is True
    assert res_branch.retry_allowed is False
    assert "DEFAULT_BRANCH_CHANGED" in res_branch.drift_reasons


def test_completion_gate_detects_second_pass_pr_enumeration_drift() -> None:
    start_snap = _make_repo_snapshot(default_branch="main", head_sha=_START_SHA)
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=start_snap,
        cutoff_at=_CUTOFF,
    )
    end_snap = _make_repo_snapshot(default_branch="main", head_sha=_START_SHA)
    evaluator = CompletionGateEvaluator()

    # Case A: New PR appeared on 2nd pass
    pr10 = _make_pr_cue(10)
    pr11 = _make_pr_cue(11)
    res_added = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=end_snap,
        first_pass_merged_prs=(pr10,),
        second_pass_merged_prs=(pr10, pr11),
    )
    assert res_added.status is SyncRunStatus.PARTIAL
    assert res_added.failure_reason_code == "PLAN_DRIFT"
    assert res_added.source_drift is True
    assert res_added.retry_allowed is False
    assert "MERGED_PR_ENUMERATION_DRIFT" in res_added.drift_reasons
    assert res_added.drift_details["added_pr_numbers"] == [11]

    # Case B: Existing PR updated_at changed between 1st and 2nd pass
    pr10_edited = _make_pr_cue(
        10,
        head_sha=pr10.head_sha,
        merged_at=pr10.merged_at,
        updated_at=pr10.updated_at + timedelta(seconds=30),
    )
    res_edited = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=end_snap,
        first_pass_merged_prs=(pr10,),
        second_pass_merged_prs=(pr10_edited,),
    )
    assert res_edited.status is SyncRunStatus.PARTIAL
    assert res_edited.failure_reason_code == "PLAN_DRIFT"
    assert res_edited.source_drift is True
    assert res_edited.retry_allowed is False
    assert res_edited.drift_details["modified_pr_numbers"] == [10]

    # Out-of-window PRs outside [backfill_from, cutoff_at] are ignored cleanly
    out_of_window = _make_pr_cue(99, merged_at=_CUTOFF + timedelta(hours=1))
    res_ok = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=end_snap,
        first_pass_merged_prs=(pr10,),
        second_pass_merged_prs=(pr10, out_of_window),
    )
    assert res_ok.passed is True
    assert res_ok.status is SyncRunStatus.SUCCESS


def test_completion_gate_handles_visibility_change_limits_and_transient_errors() -> None:
    start_snap = _make_repo_snapshot(default_branch="main", head_sha=_START_SHA)
    plan = build_frozen_import_plan(
        source_resource_id="11111111-1111-4111-8111-111111111111",
        repo_snapshot=start_snap,
        cutoff_at=_CUTOFF,
    )
    cues = (_make_pr_cue(10),)
    evaluator = CompletionGateEvaluator()

    # 1) Visibility changed to PRIVATE
    res_priv = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=_make_repo_snapshot(
            default_branch="main",
            head_sha=_START_SHA,
            visibility=ResourceVisibility.PRIVATE,
        ),
        first_pass_merged_prs=cues,
        second_pass_merged_prs=cues,
    )
    assert res_priv.status is SyncRunStatus.FAILED
    assert res_priv.failure_reason_code == "REPOSITORY_VISIBILITY_CHANGED"
    assert res_priv.import_ready is False
    assert res_priv.retry_allowed is False

    # 2) Limit exceeded -> PARTIAL_WITH_LIMIT with non-empty remaining_range
    res_lim = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=start_snap,
        first_pass_merged_prs=cues,
        second_pass_merged_prs=cues,
        limit_exceeded=True,
        processed_range="objects:0..5000",
        remaining_range="objects:5000..",
    )
    assert res_lim.status is SyncRunStatus.PARTIAL_WITH_LIMIT
    assert res_lim.failure_reason_code == "IMPORT_LIMIT_EXCEEDED"
    assert res_lim.remaining_range == "objects:5000.."
    assert res_lim.retry_allowed is False

    # 3) Transient provider failure with safe checkpoint -> PARTIAL retry_allowed=True
    res_transient = evaluator.verify_completion_snapshot(
        plan=plan,
        end_repo_snapshot=start_snap,
        first_pass_merged_prs=cues,
        second_pass_merged_prs=cues,
        transient_error_count=1,
        has_safe_checkpoint=True,
    )
    assert res_transient.status is SyncRunStatus.PARTIAL
    assert res_transient.failure_reason_code == "TRANSIENT_PROVIDER_ERROR"
    assert res_transient.source_drift is False
    assert res_transient.retry_allowed is True
    obs = res_transient.to_sync_run_observation()
    assert obs.retry_allowed is True
    assert obs.has_safe_checkpoint is True
