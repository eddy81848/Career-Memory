"""Unit tests for T-203B CommitPrAssociationClassifier (default branch, PR-first, 3-state)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from career_memory_api.backfill_engine import (
    AssociationMatchMechanism,
    BackfillFailureReasonCode,
    CommitPrAssociationClassifier,
    CommitPrLookupObservation,
    DefaultBranchCommitRecord,
    ItemOutcomeKind,
    PullRequestRecord,
    RepositoryHeadSnapshot,
    SkipReasonCode,
)
from career_memory_api.schema_contracts.enums import PrAssociationState


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:40]


@pytest.mark.parametrize("default_branch", ["main", "master", "trunk", "release/default"])
def test_non_master_default_branches_supported_without_assumption(default_branch: str) -> None:
    head = _sha(f"head-{default_branch}")
    direct_sha = _sha(f"direct-{default_branch}")

    start = RepositoryHeadSnapshot(
        default_branch=default_branch,
        head_sha=head,
        start_head_reachable=True,
        pr_enumeration_digest=_sha("prs-v1"),
    )
    end = RepositoryHeadSnapshot(
        default_branch=default_branch,
        head_sha=head,
        start_head_reachable=True,
        pr_enumeration_digest=_sha("prs-v1"),
    )

    classifier = CommitPrAssociationClassifier()
    result = classifier.classify_batch(
        start_snapshot=start,
        end_snapshot=end,
        pull_requests=(),
        default_branch_commits=(
            DefaultBranchCommitRecord(
                provider_object_id=f"commit:{direct_sha}",
                commit_sha=direct_sha,
                title="Direct hotfix commit",
                committed_at=datetime(2026, 6, 10, tzinfo=UTC),
            ),
        ),
        lookup_observations=(
            CommitPrLookupObservation(
                commit_sha=direct_sha,
                lookup_succeeded=True,
                associated_pr_ids=(),
            ),
        ),
    )

    assert result.plan_integrity.drift_detected is False
    assert result.can_finalize_success is True
    assert result.direct_commit_shas == (direct_sha,)
    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.default_branch == default_branch
    assert decision.state == PrAssociationState.NO_MATCH_OBSERVED
    assert decision.direct_commit_candidate is True
    assert decision.direct_commit_badge_metadata is not None
    assert decision.direct_commit_badge_metadata["user_review_required"] is True
    assert (
        decision.direct_commit_badge_metadata["association_note"]
        == "NO_MATCH_OBSERVED_REQUIRES_CONFIRMATION"
    )


def test_plan_drift_triggers_partial_no_retry_on_branch_head_force_push_or_pr_list_change() -> None:
    base = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("head-1"),
        start_head_reachable=True,
        pr_enumeration_digest=_sha("prs-1"),
    )
    classifier = CommitPrAssociationClassifier()

    # 1. Default branch renamed
    r1 = classifier.evaluate_plan_integrity(
        base,
        RepositoryHeadSnapshot(
            default_branch="trunk",
            head_sha=_sha("head-1"),
            start_head_reachable=True,
            pr_enumeration_digest=_sha("prs-1"),
        ),
    )
    assert r1.drift_detected is True
    assert r1.failure_reason_code == BackfillFailureReasonCode.PLAN_DRIFT
    assert r1.retry_allowed is False

    # 2. HEAD SHA moved
    r2 = classifier.evaluate_plan_integrity(
        base,
        RepositoryHeadSnapshot(
            default_branch="main",
            head_sha=_sha("head-2"),
            start_head_reachable=True,
            pr_enumeration_digest=_sha("prs-1"),
        ),
    )
    assert r2.drift_detected is True
    assert r2.failure_reason_code == BackfillFailureReasonCode.PLAN_DRIFT

    # 3. Start HEAD disappeared due to force-push
    r3 = classifier.evaluate_plan_integrity(
        base,
        RepositoryHeadSnapshot(
            default_branch="main",
            head_sha=_sha("head-1"),
            start_head_reachable=False,
            pr_enumeration_digest=_sha("prs-1"),
        ),
    )
    assert r3.drift_detected is True
    assert r3.failure_reason_code == BackfillFailureReasonCode.PLAN_DRIFT

    # 4. Secondary PR enumeration digest mismatch
    r4 = classifier.evaluate_plan_integrity(
        base,
        RepositoryHeadSnapshot(
            default_branch="main",
            head_sha=_sha("head-1"),
            start_head_reachable=True,
            pr_enumeration_digest=_sha("prs-2"),
        ),
    )
    assert r4.drift_detected is True
    assert r4.failure_reason_code == BackfillFailureReasonCode.PLAN_DRIFT


def test_pr_first_attribution_covers_merge_squash_rebase_and_out_of_window_prs() -> None:
    head = _sha("head-trunk")
    snap = RepositoryHeadSnapshot(
        default_branch="trunk",
        head_sha=head,
        start_head_reachable=True,
        pr_enumeration_digest=_sha("prs-ok"),
    )

    c_pr_commit = _sha("c-pr-commit")
    c_merge = _sha("c-merge-sha")
    c_squash = _sha("c-squash-sha")
    c_rebase = _sha("c-rebase-sha")
    c_out_of_window = _sha("c-old-pr-commit")
    c_direct_1 = _sha("c-direct-1")
    c_direct_2 = _sha("c-direct-2")

    prs = (
        PullRequestRecord(
            provider_object_id="pr:101",
            pr_number=101,
            title="Implement payment idempotency",
            merged=True,
            in_window=True,
            base_branch="trunk",
            head_sha=_sha("pr101-head"),
            merge_commit_sha=c_merge,
            commit_shas=(c_pr_commit,),
        ),
        PullRequestRecord(
            provider_object_id="pr:102",
            pr_number=102,
            title="Squashed cache optimization",
            merged=True,
            in_window=True,
            base_branch="trunk",
            squash_commit_sha=c_squash,
        ),
        PullRequestRecord(
            provider_object_id="pr:103",
            pr_number=103,
            title="Rebased query planner",
            merged=True,
            in_window=True,
            base_branch="trunk",
            rebase_commit_shas=(c_rebase,),
        ),
    )

    commits = tuple(
        DefaultBranchCommitRecord(
            provider_object_id=f"commit:{sha}",
            commit_sha=sha,
            title=f"Commit {sha[:8]}",
            committed_at=datetime(2026, 6, 15, tzinfo=UTC),
        )
        for sha in (c_pr_commit, c_merge, c_squash, c_rebase, c_out_of_window, c_direct_1, c_direct_2)
    )

    lookups = (
        CommitPrLookupObservation(commit_sha=c_pr_commit, lookup_succeeded=True, associated_pr_ids=("pr:101",)),
        CommitPrLookupObservation(commit_sha=c_merge, lookup_succeeded=True, associated_pr_ids=("pr:101",)),
        CommitPrLookupObservation(commit_sha=c_squash, lookup_succeeded=True, associated_pr_ids=("pr:102",)),
        CommitPrLookupObservation(commit_sha=c_rebase, lookup_succeeded=True, associated_pr_ids=("pr:103",)),
        # Out-of-window PR linked via provider commit-to-PR API!
        CommitPrLookupObservation(
            commit_sha=c_out_of_window,
            lookup_succeeded=True,
            associated_pr_ids=("pr:12",),
            associated_pr_in_window={"pr:12": False},
        ),
        CommitPrLookupObservation(commit_sha=c_direct_1, lookup_succeeded=True, associated_pr_ids=()),
        CommitPrLookupObservation(commit_sha=c_direct_2, lookup_succeeded=True, associated_pr_ids=()),
    )

    result = CommitPrAssociationClassifier().classify_batch(
        start_snapshot=snap,
        end_snapshot=snap,
        pull_requests=prs,
        default_branch_commits=commits,
        lookup_observations=lookups,
    )

    assert result.can_finalize_success is True
    assert result.unknown_commit_shas == ()
    assert set(result.eligible_pr_ids) == {"pr:101", "pr:102", "pr:103"}
    assert set(result.direct_commit_shas) == {c_direct_1, c_direct_2}
    assert result.out_of_window_excluded_shas == (c_out_of_window,)

    by_sha = {d.commit_sha: d for d in result.decisions}
    assert by_sha[c_pr_commit].state == PrAssociationState.MATCHED_PR
    assert by_sha[c_merge].match_mechanism == AssociationMatchMechanism.PR_MERGE_COMMIT_SHA
    assert by_sha[c_squash].match_mechanism == AssociationMatchMechanism.SQUASH_COMMIT_SHA
    assert by_sha[c_rebase].match_mechanism == AssociationMatchMechanism.REBASE_COMMIT_SHA

    # Out-of-window PR commit MUST NOT become DIRECT_COMMIT and MUST record explicit exclusion!
    out_decision = by_sha[c_out_of_window]
    assert out_decision.state == PrAssociationState.MATCHED_PR
    assert out_decision.matched_pr_in_window is False
    assert out_decision.direct_commit_candidate is False
    assert out_decision.exclusion_outcome is not None
    assert out_decision.exclusion_outcome.outcome_kind == ItemOutcomeKind.EXCLUDED
    assert out_decision.exclusion_outcome.reason_code == SkipReasonCode.OUT_OF_WINDOW_PR_LINKED.value


def test_unknown_pr_association_blocks_direct_commit_and_blocks_success() -> None:
    head = _sha("head-main")
    snap = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=head,
        start_head_reachable=True,
        pr_enumeration_digest=_sha("prs-ok"),
    )

    c_timeout = _sha("c-timeout")
    c_conflict = _sha("c-conflict")
    c_missing_field = _sha("c-missing-field")
    c_ambiguous = _sha("c-ambiguous")
    c_good_direct = _sha("c-good-direct")

    commits = tuple(
        DefaultBranchCommitRecord(
            provider_object_id=f"commit:{sha}",
            commit_sha=sha,
            title=f"Commit {sha[:8]}",
        )
        for sha in (c_timeout, c_conflict, c_missing_field, c_ambiguous, c_good_direct)
    )

    lookups = (
        CommitPrLookupObservation(
            commit_sha=c_timeout, lookup_succeeded=False, error_code="PROVIDER_TIMEOUT"
        ),
        CommitPrLookupObservation(
            commit_sha=c_conflict, lookup_succeeded=True, is_conflicting=True, error_code="CONFLICTING_PR_LINK"
        ),
        CommitPrLookupObservation(
            commit_sha=c_missing_field, lookup_succeeded=True, has_missing_fields=True
        ),
        CommitPrLookupObservation(
            commit_sha=c_ambiguous, lookup_succeeded=True, is_ambiguous=True
        ),
        CommitPrLookupObservation(
            commit_sha=c_good_direct, lookup_succeeded=True, associated_pr_ids=()
        ),
    )

    result = CommitPrAssociationClassifier().classify_batch(
        start_snapshot=snap,
        end_snapshot=snap,
        pull_requests=(),
        default_branch_commits=commits,
        lookup_observations=lookups,
    )

    assert result.can_finalize_success is False
    assert result.blocking_failure_reason == BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION
    assert set(result.unknown_commit_shas) == {c_timeout, c_conflict, c_missing_field, c_ambiguous}
    # Even though c_good_direct was NO_MATCH_OBSERVED, UNKNOWN commits never become DIRECT_COMMIT!
    for sha in (c_timeout, c_conflict, c_missing_field, c_ambiguous):
        assert sha not in result.direct_commit_shas
        decision = next(d for d in result.decisions if d.commit_sha == sha)
        assert decision.state == PrAssociationState.UNKNOWN
        assert decision.direct_commit_candidate is False
