"""T-203B CommitPrAssociationClassifier: default branch, PR-first attribution & 3-state completeness."""

from __future__ import annotations

from collections.abc import Sequence

from career_memory_api.backfill_engine.models import (
    DIRECT_COMMIT_ASSOCIATION_NOTE,
    AssociationBatchResult,
    AssociationMatchMechanism,
    BackfillFailureReasonCode,
    BackfillStreamName,
    CommitAssociationDecision,
    CommitPrLookupObservation,
    DefaultBranchCommitRecord,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    PlanIntegrityEvaluation,
    PullRequestRecord,
    RepositoryHeadSnapshot,
    SkipReasonCode,
)
from career_memory_api.schema_contracts.enums import PrAssociationState


def _norm_sha(sha: str | None) -> str | None:
    if sha is None:
        return None
    cleaned = sha.strip().lower()
    return cleaned if cleaned else None


class CommitPrAssociationClassifier:
    """Implements T-203B default-branch & PR association completeness rules (SYNC-09/14/16/17/20/21/22).

    Invariants:
    1. Never assumes the default branch is ``master``; uses the repository snapshot's
       authoritative ``default_branch`` (e.g. ``main``, ``master``, ``trunk``).
    2. Detects start-to-end plan drift (default branch renamed/changed, HEAD moved,
       start HEAD unreachable due to force-push, secondary PR enumeration digest changed)
       and blocks finalization with ``PLAN_DRIFT`` (``retry_allowed=False``).
    3. Cross-checks commits against PR commit lists, ``head_sha``, ``merge_commit_sha``,
       ``squash_commit_sha``, ``rebase_commit_shas``, and provider commit-to-PR lookups
       (including out-of-window PRs, which receive ``OUT_OF_WINDOW_PR_LINKED`` exclusion
       and are NEVER double-counted as ``DIRECT_COMMIT``).
    4. Assigns ``NO_MATCH_OBSERVED`` ONLY when the commit is reachable at start and end
       and commit-to-PR lookup completed cleanly with zero errors/missing fields/conflicts
       and zero matched PRs.
    5. Any ``UNKNOWN`` commit blocks ``DIRECT_COMMIT`` candidate generation for that commit
       AND blocks whole-run ``SUCCESS`` (``INCOMPLETE_PR_ASSOCIATION``).
    """

    @staticmethod
    def evaluate_plan_integrity(
        start_snapshot: RepositoryHeadSnapshot,
        end_snapshot: RepositoryHeadSnapshot,
    ) -> PlanIntegrityEvaluation:
        """Verify repository default branch, HEAD SHA, reachability, and PR enumeration stability."""
        reasons: list[str] = []
        if start_snapshot.default_branch != end_snapshot.default_branch:
            reasons.append(
                f"DEFAULT_BRANCH_CHANGED:{start_snapshot.default_branch}->{end_snapshot.default_branch}"
            )
        if _norm_sha(start_snapshot.head_sha) != _norm_sha(end_snapshot.head_sha):
            reasons.append("DEFAULT_BRANCH_HEAD_MOVED")
        if not end_snapshot.start_head_reachable:
            reasons.append("START_HEAD_UNREACHABLE_FORCE_PUSH")
        if start_snapshot.pr_enumeration_digest != end_snapshot.pr_enumeration_digest:
            reasons.append("SECONDARY_PR_ENUMERATION_MISMATCH")

        if reasons:
            return PlanIntegrityEvaluation(
                drift_detected=True,
                drift_reasons=tuple(reasons),
                failure_reason_code=BackfillFailureReasonCode.PLAN_DRIFT,
                retry_allowed=False,
            )
        return PlanIntegrityEvaluation(
            drift_detected=False,
            drift_reasons=(),
            failure_reason_code=None,
            retry_allowed=False,
        )

    def classify_batch(
        self,
        *,
        start_snapshot: RepositoryHeadSnapshot,
        end_snapshot: RepositoryHeadSnapshot,
        pull_requests: Sequence[PullRequestRecord],
        default_branch_commits: Sequence[DefaultBranchCommitRecord],
        lookup_observations: Sequence[CommitPrLookupObservation],
    ) -> AssociationBatchResult:
        """Classify every default-branch commit into MATCHED_PR / NO_MATCH_OBSERVED / UNKNOWN."""
        plan_integrity = self.evaluate_plan_integrity(start_snapshot, end_snapshot)
        default_branch = start_snapshot.default_branch

        # Index all PR-associated SHAs -> list of (PullRequestRecord, AssociationMatchMechanism)
        sha_to_prs: dict[str, list[tuple[PullRequestRecord, AssociationMatchMechanism]]] = {}

        def _register(sha: str | None, pr: PullRequestRecord, mech: AssociationMatchMechanism) -> None:
            normalized = _norm_sha(sha)
            if normalized is None:
                return
            entries = sha_to_prs.setdefault(normalized, [])
            if all(existing_pr.provider_object_id != pr.provider_object_id for existing_pr, _ in entries):
                entries.append((pr, mech))

        eligible_pr_ids: list[str] = []
        for pr in pull_requests:
            if pr.merged and pr.in_window:
                eligible_pr_ids.append(pr.provider_object_id)
            commit_mech = (
                AssociationMatchMechanism.IN_WINDOW_PR_COMMIT
                if pr.in_window
                else AssociationMatchMechanism.OUT_OF_WINDOW_PR_API
            )
            for c_sha in pr.commit_shas:
                _register(c_sha, pr, commit_mech)
            _register(pr.head_sha, pr, AssociationMatchMechanism.PR_HEAD_SHA)
            _register(pr.merge_commit_sha, pr, AssociationMatchMechanism.PR_MERGE_COMMIT_SHA)
            _register(pr.squash_commit_sha, pr, AssociationMatchMechanism.SQUASH_COMMIT_SHA)
            for r_sha in pr.rebase_commit_shas:
                _register(r_sha, pr, AssociationMatchMechanism.REBASE_COMMIT_SHA)

        lookup_by_sha: dict[str, CommitPrLookupObservation] = {}
        for obs in lookup_observations:
            norm = _norm_sha(obs.commit_sha)
            if norm is not None:
                lookup_by_sha[norm] = obs

        decisions: list[CommitAssociationDecision] = []
        direct_commit_shas: list[str] = []
        out_of_window_excluded_shas: list[str] = []
        unknown_commit_shas: list[str] = []
        item_outcomes: list[ItemOutcomeRecord] = []

        for commit in default_branch_commits:
            sha = _norm_sha(commit.commit_sha) or commit.commit_sha
            obs = lookup_by_sha.get(sha)

            # 1. Explicit conflict or ambiguity in provider association lookup -> always UNKNOWN
            if obs is not None and (obs.is_conflicting or obs.is_ambiguous):
                reason = obs.error_code or (
                    "ASSOCIATION_CONFLICT" if obs.is_conflicting else "AMBIGUOUS_MERGE_LINK"
                )
                decision = CommitAssociationDecision(
                    commit_provider_object_id=commit.provider_object_id,
                    commit_sha=sha,
                    default_branch=default_branch,
                    state=PrAssociationState.UNKNOWN,
                    unknown_reason_code=reason,
                    direct_commit_candidate=False,
                    reachable_at_start=commit.reachable_at_start,
                    reachable_at_end=commit.reachable_at_end,
                    lookup_completed=False,
                )
                decisions.append(decision)
                unknown_commit_shas.append(sha)
                item_outcomes.append(
                    ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.ASSOCIATION_UNKNOWN,
                        reason_code=reason,
                        outcome_metadata={"commit_sha": sha, "default_branch": default_branch},
                    )
                )
                continue

            # 2. Check collected PR SHA indices (commit list, head, merge, squash, rebase)
            indexed_matches = sha_to_prs.get(sha, [])
            if len(indexed_matches) > 1:
                reason = "AMBIGUOUS_MERGE_LINK"
                decision = CommitAssociationDecision(
                    commit_provider_object_id=commit.provider_object_id,
                    commit_sha=sha,
                    default_branch=default_branch,
                    state=PrAssociationState.UNKNOWN,
                    unknown_reason_code=reason,
                    direct_commit_candidate=False,
                    reachable_at_start=commit.reachable_at_start,
                    reachable_at_end=commit.reachable_at_end,
                    lookup_completed=bool(obs and obs.lookup_succeeded),
                )
                decisions.append(decision)
                unknown_commit_shas.append(sha)
                item_outcomes.append(
                    ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.ASSOCIATION_UNKNOWN,
                        reason_code=reason,
                        outcome_metadata={
                            "commit_sha": sha,
                            "candidate_pr_ids": sorted(
                                pr.provider_object_id for pr, _ in indexed_matches
                            ),
                        },
                    )
                )
                continue

            if len(indexed_matches) == 1:
                matched_pr, mechanism = indexed_matches[0]
                exclusion: ItemOutcomeRecord | None = None
                if not matched_pr.in_window:
                    out_of_window_excluded_shas.append(sha)
                    exclusion = ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.EXCLUDED,
                        reason_code=SkipReasonCode.OUT_OF_WINDOW_PR_LINKED.value,
                        affected_range=f"pr={matched_pr.provider_object_id}",
                        outcome_metadata={
                            "commit_sha": sha,
                            "matched_pr_provider_id": matched_pr.provider_object_id,
                            "default_branch": default_branch,
                        },
                    )
                    item_outcomes.append(exclusion)
                decisions.append(
                    CommitAssociationDecision(
                        commit_provider_object_id=commit.provider_object_id,
                        commit_sha=sha,
                        default_branch=default_branch,
                        state=PrAssociationState.MATCHED_PR,
                        matched_pr_provider_id=matched_pr.provider_object_id,
                        matched_pr_in_window=matched_pr.in_window,
                        match_mechanism=mechanism,
                        direct_commit_candidate=False,
                        exclusion_outcome=exclusion,
                        reachable_at_start=commit.reachable_at_start,
                        reachable_at_end=commit.reachable_at_end,
                        lookup_completed=True,
                    )
                )
                continue

            # 3. Not in collected PR SHA indices -> rely on provider commit-to-PR lookup
            if obs is None or not obs.lookup_succeeded or obs.has_missing_fields:
                if obs is None:
                    reason = "MISSING_ASSOCIATION_LOOKUP"
                elif not obs.lookup_succeeded:
                    reason = obs.error_code or "ASSOCIATION_LOOKUP_FAILED"
                else:
                    reason = obs.error_code or "ASSOCIATION_MISSING_FIELDS"
                decisions.append(
                    CommitAssociationDecision(
                        commit_provider_object_id=commit.provider_object_id,
                        commit_sha=sha,
                        default_branch=default_branch,
                        state=PrAssociationState.UNKNOWN,
                        unknown_reason_code=reason,
                        direct_commit_candidate=False,
                        reachable_at_start=commit.reachable_at_start,
                        reachable_at_end=commit.reachable_at_end,
                        lookup_completed=False,
                    )
                )
                unknown_commit_shas.append(sha)
                item_outcomes.append(
                    ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.ASSOCIATION_UNKNOWN,
                        reason_code=reason,
                        outcome_metadata={"commit_sha": sha, "default_branch": default_branch},
                    )
                )
                continue

            # Provider API returned one or more associated PRs (in-window OR out-of-window)
            if obs.associated_pr_ids:
                matched_pr_id = obs.associated_pr_ids[0]
                in_window = bool(obs.associated_pr_in_window.get(matched_pr_id, False))
                mechanism = (
                    AssociationMatchMechanism.PROVIDER_COMMIT_PR_API
                    if in_window
                    else AssociationMatchMechanism.OUT_OF_WINDOW_PR_API
                )
                exclusion = None
                if not in_window:
                    out_of_window_excluded_shas.append(sha)
                    exclusion = ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.EXCLUDED,
                        reason_code=SkipReasonCode.OUT_OF_WINDOW_PR_LINKED.value,
                        affected_range=f"pr={matched_pr_id}",
                        outcome_metadata={
                            "commit_sha": sha,
                            "matched_pr_provider_id": matched_pr_id,
                            "default_branch": default_branch,
                        },
                    )
                    item_outcomes.append(exclusion)
                decisions.append(
                    CommitAssociationDecision(
                        commit_provider_object_id=commit.provider_object_id,
                        commit_sha=sha,
                        default_branch=default_branch,
                        state=PrAssociationState.MATCHED_PR,
                        matched_pr_provider_id=matched_pr_id,
                        matched_pr_in_window=in_window,
                        match_mechanism=mechanism,
                        direct_commit_candidate=False,
                        exclusion_outcome=exclusion,
                        reachable_at_start=commit.reachable_at_start,
                        reachable_at_end=commit.reachable_at_end,
                        lookup_completed=True,
                    )
                )
                continue

            # 4. Lookup succeeded cleanly with empty PR list -> verify reachability on default branch
            if not (commit.reachable_at_start and commit.reachable_at_end):
                reason = "COMMIT_NOT_REACHABLE_ON_DEFAULT_BRANCH"
                decisions.append(
                    CommitAssociationDecision(
                        commit_provider_object_id=commit.provider_object_id,
                        commit_sha=sha,
                        default_branch=default_branch,
                        state=PrAssociationState.UNKNOWN,
                        unknown_reason_code=reason,
                        direct_commit_candidate=False,
                        reachable_at_start=commit.reachable_at_start,
                        reachable_at_end=commit.reachable_at_end,
                        lookup_completed=True,
                    )
                )
                unknown_commit_shas.append(sha)
                item_outcomes.append(
                    ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.ASSOCIATION_UNKNOWN,
                        reason_code=reason,
                        outcome_metadata={"commit_sha": sha, "default_branch": default_branch},
                    )
                )
                continue

            # Qualified NO_MATCH_OBSERVED -> 1 commit = 1 DIRECT_COMMIT candidate with review badge
            badge_metadata = {
                "user_review_required": True,
                "association_note": DIRECT_COMMIT_ASSOCIATION_NOTE,
                "commit_sha": sha,
                "default_branch": default_branch,
            }
            direct_commit_shas.append(sha)
            decisions.append(
                CommitAssociationDecision(
                    commit_provider_object_id=commit.provider_object_id,
                    commit_sha=sha,
                    default_branch=default_branch,
                    state=PrAssociationState.NO_MATCH_OBSERVED,
                    direct_commit_candidate=True,
                    direct_commit_badge_metadata=badge_metadata,
                    reachable_at_start=True,
                    reachable_at_end=True,
                    lookup_completed=True,
                )
            )

        if plan_integrity.drift_detected:
            can_finalize = False
            blocking_reason: BackfillFailureReasonCode | None = BackfillFailureReasonCode.PLAN_DRIFT
        elif unknown_commit_shas:
            can_finalize = False
            blocking_reason = BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION
        else:
            can_finalize = True
            blocking_reason = None

        return AssociationBatchResult(
            plan_integrity=plan_integrity,
            decisions=tuple(decisions),
            eligible_pr_ids=tuple(eligible_pr_ids),
            direct_commit_shas=tuple(direct_commit_shas),
            out_of_window_excluded_shas=tuple(out_of_window_excluded_shas),
            unknown_commit_shas=tuple(unknown_commit_shas),
            item_outcomes=tuple(item_outcomes),
            can_finalize_success=can_finalize,
            blocking_failure_reason=blocking_reason,
        )
