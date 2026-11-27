"""Real PostgreSQL 18 integration tests for M3 Event Candidate Builder (T-301 + T-301A).

Verifies all frozen candidate-generation regression scenarios against real PG18
migrations 0010..0050 with NOBYPASSRLS runtime roles:
- EV-001 (PR centered basic, EVENT-01/02/07)
- DELTA-EV-002 (DIRECT_COMMIT confirmation metadata + 1:1 non-clustering, T-301A/EVENT-10/SYNC-20)
- EV-003 (UNKNOWN association hold & missing observation rejection, T-301A)
- EV-004 (non-default branch commit -> 0 DIRECT_COMMIT candidates)
- EV-005 (Issue/Ticket strictly CONTEXT_ONLY, 0 standalone Issue events, EVENT-04)
- EV-006 (Shared ticket across 2 PRs -> 2 separate PR events, 0 auto-merge/USER_COMPOSITE)
- EV-007 (Mixed-purpose PR -> 1 PR event + MIXED_PURPOSE_SIGNALS + 0 auto-splits)
- CAND-EV-019 (Provider default_branch='trunk', decoy 'main', squash/rebase/out-of-window PR dedup,
  zero timestamp/message heuristic linking)
- CAND-EV-020 (Provider default_branch='master', decoy 'main', UNKNOWN -> 0 promoted events)
- EVENT-03 (PR reviews, inline review comments, PR comments -> MUST_LINK origin='SOURCE')
- EVENT-05 (Active CANNOT_LINK precedence preserved on re-run)
- All 7 deterministic review flags (including PARTIAL_SOURCE_SKIPPED, AMBIGUOUS_AUTHOR_IDENTITY,
  GENERIC_TITLE_OR_SUMMARY, MULTIPLE_LINKED_ISSUES, REVIEW_REQUIRED_MULTI_PARENT)
- Idempotency & enrichment over M2 PostgresBackfillPageCommitter.finalize_import_run
- User review protection (CONFIRMED, REJECTED, SUPERSEDED, version > 1 untouched)
- Policy & deletion fence gates (start AND pre-commit rollback)
- Plan C (REF-04) canary non-leak & cross-workspace RLS isolation
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from career_memory_api.backfill_engine.models import (
    BackfillStreamName,
    CommitAssociationDecision,
    FinalizeOutcomeStatus,
    FinalizeRunRequest,
    FinalizationDirectCommitCandidate,
    FinalizationPrCandidate,
    NormalizedPageItem,
    PageCommitRequest,
    RepositoryHeadSnapshot,
)
from career_memory_api.backfill_engine.postgres_page_committer import (
    PostgresBackfillPageCommitter,
)
from career_memory_api.event_candidates import (
    DIRECT_COMMIT_BADGE_CODE,
    CandidateBuildErrorCode,
    CandidateBuildRequest,
    CandidateBuildStatus,
    EventRelationType,
    PostgresEventCandidateBuilder,
    ReviewFlagCode,
)
from career_memory_api.schema_contracts.enums import (
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import find_forbidden_raw_keys
from tests.backfill_engine.pg_harness import (
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m3-t301-pg18-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _sha256(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _mark_sync_run_import_ready(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    sync_run_id: str,
    source_resource_id: str,
    plan_id: str,
    default_branch: str,
    status: str = "SUCCESS",
    import_ready: bool = True,
    emit_outbox: bool = True,
    import_window_start: str | None = None,
    import_window_end: str | None = None,
) -> None:
    now = datetime.now(UTC)
    plan_spec: dict[str, Any] = {
        "default_branch": default_branch,
        "mode": "INITIAL_BACKFILL",
    }
    if import_window_start is not None:
        plan_spec["backfill_from"] = import_window_start
    if import_window_end is not None:
        plan_spec["cutoff_at"] = import_window_end

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.sync_runs
                    SET status = %s,
                        import_ready = %s,
                        plan_spec = %s::jsonb,
                        observed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (
                        status,
                        import_ready,
                        json.dumps(plan_spec, sort_keys=True),
                        now,
                        now,
                        workspace_id,
                        sync_run_id,
                    ),
                )
                if emit_outbox:
                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'IMPORT_READY', 'SYNC_RUN',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
                        """,
                        (
                            str(uuid.uuid4()),
                            workspace_id,
                            sync_run_id,
                            f"import_ready:{sync_run_id}",
                            json.dumps(
                                {
                                    "sync_run_id": sync_run_id,
                                    "source_resource_id": source_resource_id,
                                    "plan_id": plan_id,
                                    "default_branch": default_branch,
                                },
                                sort_keys=True,
                            ),
                            now,
                            now,
                        ),
                    )


def _insert_source_object_with_revision(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
    object_type: SourceObjectType,
    provider_object_id: str,
    metadata: dict[str, Any],
    pr_association_state: PrAssociationState | None = None,
    provider_updated_at: datetime | None = None,
) -> tuple[str, str]:
    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, pr_association_state,
                        first_seen_at, last_seen_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'ACTIVE', %s,
                        %s, %s
                    )
                    """,
                    (
                        obj_id,
                        workspace_id,
                        source_resource_id,
                        object_type.value,
                        provider_object_id,
                        pr_association_state.value if pr_association_state else None,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        provider_updated_at, is_tombstone, normalized_metadata,
                        connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'v1', %s,
                        %s, false, %s::jsonb,
                        'github-m2-v1', 'v1'
                    )
                    """,
                    (
                        rev_id,
                        workspace_id,
                        obj_id,
                        f"rev-{provider_object_id}",
                        _sha256(f"{object_type.value}:{provider_object_id}:{obj_id}"),
                        now,
                        provider_updated_at or now,
                        json.dumps(metadata, sort_keys=True),
                    ),
                )
    return obj_id, rev_id


def _insert_commit_association(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    sync_run_id: str,
    source_resource_id: str,
    commit_source_object_id: str,
    commit_sha: str,
    default_branch_name: str,
    state: PrAssociationState,
    matched_pr_provider_id: str | None = None,
    matched_pr_in_window: bool | None = None,
    match_mechanism: str | None = None,
    unknown_reason_code: str | None = None,
    reachable_at_start: bool = True,
    reachable_at_end: bool = True,
    lookup_completed: bool = True,
    observation_metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC)
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.sync_commit_pr_associations (
                        id, workspace_id, sync_run_id, source_resource_id,
                        commit_source_object_id, commit_sha, default_branch_name,
                        pr_association_state, matched_pr_provider_id,
                        matched_pr_in_window, match_mechanism, unknown_reason_code,
                        reachable_at_start, reachable_at_end, lookup_completed,
                        observation_metadata, observed_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        %s::uuid, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s, %s
                    )
                    """,
                    (
                        str(uuid.uuid4()),
                        workspace_id,
                        sync_run_id,
                        source_resource_id,
                        commit_source_object_id,
                        commit_sha.lower(),
                        default_branch_name,
                        state.value,
                        matched_pr_provider_id,
                        matched_pr_in_window,
                        match_mechanism,
                        unknown_reason_code,
                        reachable_at_start,
                        reachable_at_end,
                        lookup_completed,
                        json.dumps(observation_metadata or {}, sort_keys=True),
                        now,
                        now,
                    ),
                )


def _fetch_all_events(
    cluster: EphemeralBackfillPg18Cluster, workspace_id: str
) -> list[dict[str, Any]]:
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id::text, primary_anchor_source_object_id::text, anchor_mode,
                    version, title, summary_metadata, status, created_by,
                    occurred_start_at, occurred_end_at
                FROM public.work_events
                WHERE workspace_id = %s::uuid
                ORDER BY anchor_mode ASC, title ASC, id ASC
                """,
                (workspace_id,),
            )
            return [
                {
                    "id": str(r[0]),
                    "primary_anchor_source_object_id": str(r[1]) if r[1] else None,
                    "anchor_mode": str(r[2]),
                    "version": int(r[3]),
                    "title": str(r[4]),
                    "summary_metadata": dict(r[5] or {}),
                    "status": str(r[6]),
                    "created_by": str(r[7]),
                    "occurred_start_at": r[8],
                    "occurred_end_at": r[9],
                }
                for r in cur.fetchall()
            ]


def _fetch_event_memberships(
    cluster: EphemeralBackfillPg18Cluster, workspace_id: str, event_id: str
) -> list[dict[str, Any]]:
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    em.source_object_id::text,
                    so.object_type,
                    so.provider_object_id,
                    em.relation_class,
                    em.relation_type,
                    em.origin,
                    em.rationale_code,
                    em.status
                FROM public.event_memberships em
                JOIN public.source_objects so
                  ON so.workspace_id = em.workspace_id
                 AND so.id = em.source_object_id
                WHERE em.workspace_id = %s::uuid
                  AND em.event_id = %s::uuid
                ORDER BY em.relation_class ASC, em.relation_type ASC, so.provider_object_id ASC
                """,
                (workspace_id, event_id),
            )
            return [
                {
                    "source_object_id": str(r[0]),
                    "object_type": str(r[1]),
                    "provider_object_id": str(r[2]),
                    "relation_class": str(r[3]),
                    "relation_type": str(r[4]),
                    "origin": str(r[5]),
                    "rationale_code": str(r[6]) if r[6] else None,
                    "status": str(r[7]),
                }
                for r in cur.fetchall()
            ]


# =============================================================================
# 1. Frozen Case EV-001 (EVENT-01, EVENT-02, EVENT-03, EVENT-07)
# =============================================================================
def test_ev001_pr_centered_basic_with_commits_reviews_and_comments(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/ledger-service", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )

    pr_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="101",
        metadata={
            "provider_number": 101,
            "title": "Add idempotent payment retry guard",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
            "head_ref": "feat/payment-retry-guard",
            "head_sha": "101b000000000000000000000000000000000002",
            "created_at": "2026-10-20T11:00:00Z",
            "merged_at": "2026-10-20T14:30:00Z",
            "author_login": "syn-dev-a",
            "commit_ids": [
                "101a000000000000000000000000000000000001",
                "101b000000000000000000000000000000000002",
            ],
        },
    )
    c1_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="101a000000000000000000000000000000000001",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={
            "head_sha": "101a000000000000000000000000000000000001",
            "on_default_branch": False,
            "branch_name": "feat/payment-retry-guard",
            "associated_pr_number": 101,
            "committed_at": "2026-10-20T12:00:00Z",
            "changed_paths": ["src/settlement/retry_guard.py"],
        },
    )
    c2_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="101b000000000000000000000000000000000002",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={
            "head_sha": "101b000000000000000000000000000000000002",
            "on_default_branch": False,
            "branch_name": "feat/payment-retry-guard",
            "associated_pr_number": 101,
            "committed_at": "2026-10-20T13:15:00Z",
            "changed_paths": ["tests/test_retry_guard.py"],
        },
    )
    # EVENT-03: PR review, inline review comment, PR conversation comment, plus non-PR issue comment
    rev1_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.REVIEW,
        provider_object_id="rev-9001",
        metadata={"provider_number": 101, "state": "APPROVED", "author_login": "syn-reviewer"},
    )
    rev_comment_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.REVIEW,
        provider_object_id="rev-cmt-9002",
        metadata={"provider_number": 101, "author_login": "syn-reviewer"},
    )
    pr_cmt_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMENT,
        provider_object_id="cmt-9003",
        metadata={"provider_number": 101, "author_login": "syn-dev-b"},
    )
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMENT,
        provider_object_id="issue-cmt-9999",
        metadata={
            "provider_number": 101,
            "target_object_type": "ISSUE",
            "author_login": "syn-pm",
        },
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates(CandidateBuildRequest(workspace_id=ws_id, sync_run_id=run_id))

    assert res.status is CandidateBuildStatus.SUCCEEDED
    assert res.default_branch == "main"
    assert len(res.pr_event_ids) == 1
    assert len(res.direct_commit_event_ids) == 0
    assert res.created_event_count == 1

    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 1
    ev = events[0]
    assert ev["anchor_mode"] == WorkEventAnchorMode.PR.value
    assert ev["status"] == WorkEventStatus.CANDIDATE.value
    assert ev["version"] == 1
    assert ev["primary_anchor_source_object_id"] == pr_id
    assert ev["title"] == "Add idempotent payment retry guard"
    assert ev["summary_metadata"]["review_flags"] == []

    memberships = _fetch_event_memberships(pg18_cluster, ws_id, ev["id"])
    by_type = {(m["relation_type"], m["source_object_id"]): m for m in memberships}
    assert (EventRelationType.PRIMARY_ANCHOR.value, pr_id) in by_type
    assert (EventRelationType.PR_COMMIT.value, c1_id) in by_type
    assert (EventRelationType.PR_COMMIT.value, c2_id) in by_type
    assert (EventRelationType.PR_REVIEW.value, rev1_id) in by_type
    assert (EventRelationType.PR_REVIEW.value, rev_comment_id) in by_type
    assert (EventRelationType.PR_COMMENT.value, pr_cmt_id) in by_type
    # Issue-targeted comment must NOT attach as MUST_LINK
    assert all(m["provider_object_id"] != "issue-cmt-9999" for m in memberships)
    assert all(
        m["relation_class"] == EventMembershipRelationClass.MUST_LINK.value
        and m["origin"] == EventMembershipOrigin.SOURCE.value
        for m in memberships
    )
    # EVENT-07: zero PR_SEGMENT or USER_COMPOSITE
    assert all(
        e["anchor_mode"] not in (WorkEventAnchorMode.PR_SEGMENT.value, WorkEventAnchorMode.USER_COMPOSITE.value)
        for e in events
    )


# =============================================================================
# 2. Frozen Case DELTA-EV-002 (T-301A, EVENT-10, SYNC-20, 1:1 non-clustering)
# =============================================================================
def test_delta_ev002_direct_commit_confirmation_metadata_and_zero_clustering(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/edge-gateway", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )

    sha1 = "2010000000000000000000000000000000000001"
    sha2 = "2010000000000000000000000000000000000002"
    c1_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=sha1,
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": sha1,
            "title": "Hotfix: raise circuit breaker timeout from 2s to 5s on edge router",
            "author_login": "syn-dev-b",
            "committed_at": "2026-10-21T03:45:00Z",
            "branch_name": "main",
            "on_default_branch": True,
        },
    )
    c2_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=sha2,
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": sha2,
            "title": "Hotfix: increase edge router healthcheck grace window",
            "author_login": "syn-dev-b",
            "committed_at": "2026-10-21T03:46:00Z",
            "branch_name": "main",
            "on_default_branch": True,
        },
    )
    for cid, sha in ((c1_id, sha1), (c2_id, sha2)):
        _insert_commit_association(
            pg18_cluster,
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            commit_source_object_id=cid,
            commit_sha=sha,
            default_branch_name="main",
            state=PrAssociationState.NO_MATCH_OBSERVED,
            reachable_at_start=True,
            reachable_at_end=True,
            lookup_completed=True,
        )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    # 1:1 per commit, ZERO automatic clustering even for consecutive same-author commits!
    assert len(res.direct_commit_event_ids) == 2
    assert len(res.pr_event_ids) == 0

    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 2
    ev1 = next(e for e in events if e["primary_anchor_source_object_id"] == c1_id)
    meta = ev1["summary_metadata"]
    assert ev1["anchor_mode"] == WorkEventAnchorMode.DIRECT_COMMIT.value
    assert ev1["status"] == WorkEventStatus.CANDIDATE.value
    assert meta["default_branch"] == "main"
    assert meta["abbreviated_sha"] == sha1[:12]
    assert 7 <= len(meta["abbreviated_sha"]) <= 12
    assert meta["committed_at"] == "2026-10-21T03:45:00Z"
    assert meta["default_branch_reachable"] is True
    assert meta["pr_association_state"] == PrAssociationState.NO_MATCH_OBSERVED.value
    assert meta["no_match_observed_means_definitely_no_pr"] is False
    assert meta["requires_user_confirmation"] is True
    assert meta["badge_code"] == DIRECT_COMMIT_BADGE_CODE
    assert meta["review_flags"] == [
        ReviewFlagCode.DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED.value
    ]
    assert find_forbidden_raw_keys(meta) == []


# =============================================================================
# 3. Frozen Case EV-003 (UNKNOWN association hold & missing observation gate)
# =============================================================================
def test_ev003_partial_run_and_unknown_or_missing_association_promote_zero_events(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # 3a. PARTIAL run -> SYNC_RUN_NOT_IMPORT_READY, 0 events
    seed_partial = seed_workspace_repo_and_sync_run(
        pg18_cluster,
        repo_name="acme-syn-lab/billing-engine",
        default_branch="main",
        run_status="PENDING",
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.sync_runs
                    SET status = 'PARTIAL',
                        import_ready = false,
                        failure_reason_code = 'INCOMPLETE_PR_ASSOCIATION'
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (seed_partial["workspace_id"], seed_partial["sync_run_id"]),
                )
    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res_partial = builder.build_candidates_for_sync_run(
        workspace_id=seed_partial["workspace_id"], sync_run_id=seed_partial["sync_run_id"]
    )
    assert res_partial.status is CandidateBuildStatus.REJECTED
    assert res_partial.error_code is CandidateBuildErrorCode.SYNC_RUN_NOT_IMPORT_READY
    assert _fetch_all_events(pg18_cluster, seed_partial["workspace_id"]) == []

    # 3b. Even if sync_run claims SUCCESS + IMPORT_READY, UNKNOWN association -> PR_ASSOCIATION_INCOMPLETE, 0 events
    seed_unk = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/billing-engine-unk", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed_unk["workspace_id"],
        seed_unk["source_resource_id"],
        seed_unk["sync_run_id"],
        seed_unk["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )
    sha = "3010000000000000000000000000000000000001"
    c_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=sha,
        pr_association_state=PrAssociationState.UNKNOWN,
        metadata={"head_sha": sha, "branch_name": "main", "on_default_branch": True},
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_id,
        commit_sha=sha,
        default_branch_name="main",
        state=PrAssociationState.UNKNOWN,
        unknown_reason_code="ASSOCIATION_LOOKUP_FAILED",
        lookup_completed=False,
    )
    res_unk = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)
    assert res_unk.status is CandidateBuildStatus.REJECTED
    assert res_unk.error_code is CandidateBuildErrorCode.PR_ASSOCIATION_INCOMPLETE
    assert _fetch_all_events(pg18_cluster, ws_id) == []

    # 3c. Default-branch commit missing sync_commit_pr_associations ledger entry -> PR_ASSOCIATION_INCOMPLETE
    seed_missing = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/billing-engine-missing", default_branch="main"
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=seed_missing["workspace_id"],
        sync_run_id=seed_missing["sync_run_id"],
        source_resource_id=seed_missing["source_resource_id"],
        plan_id=seed_missing["plan_id"],
        default_branch="main",
    )
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=seed_missing["workspace_id"],
        source_resource_id=seed_missing["source_resource_id"],
        object_type=SourceObjectType.COMMIT,
        provider_object_id="3020000000000000000000000000000000000002",
        pr_association_state=None,
        metadata={"branch_name": "main", "on_default_branch": True},
    )
    res_missing = builder.build_candidates_for_sync_run(
        workspace_id=seed_missing["workspace_id"], sync_run_id=seed_missing["sync_run_id"]
    )
    assert res_missing.status is CandidateBuildStatus.REJECTED
    assert res_missing.error_code is CandidateBuildErrorCode.PR_ASSOCIATION_INCOMPLETE
    assert _fetch_all_events(pg18_cluster, seed_missing["workspace_id"]) == []


# =============================================================================
# 4. Frozen Case EV-004 (Non-default branch commit -> 0 DIRECT_COMMIT)
# =============================================================================
def test_ev004_non_default_branch_commit_yields_zero_direct_commit_candidates(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/search-indexer", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )
    sha = "4010000000000000000000000000000000000001"
    c_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=sha,
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": sha,
            "title": "WIP: prototype trigram shard balancer",
            "branch_name": "exp/trigram-prototype",
            "on_default_branch": False,
        },
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_id,
        commit_sha=sha,
        default_branch_name="exp/trigram-prototype",
        state=PrAssociationState.NO_MATCH_OBSERVED,
        reachable_at_start=True,
        reachable_at_end=True,
        lookup_completed=True,
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    assert res.direct_commit_event_ids == ()
    assert res.pr_event_ids == ()
    assert res.excluded_non_default_commit_count == 1
    assert _fetch_all_events(pg18_cluster, ws_id) == []


# =============================================================================
# 5. Frozen Cases EV-005 & EV-006 (CONTEXT_ONLY Issue/Ticket, Zero Auto-Merge)
# =============================================================================
def test_ev005_and_ev006_issue_context_only_and_shared_ticket_no_auto_merge(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/catalog-api", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )

    issue_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="202",
        metadata={
            "provider_number": 202,
            "external_key": "SYN-202",
            "title": "Support SKU deprecation across catalog model and API",
            "labels": ["ticket:syn-202"],
        },
    )
    # PR 601 explicitly references issue #202 (origin='SOURCE')
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="601",
        metadata={
            "provider_number": 601,
            "title": "Add SKU deprecation flags to catalog schema (SYN-202)",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
            "head_ref": "feat/syn-202-schema",
            "author_login": "syn-dev-a",
            "merged_at": "2026-10-25T10:00:00Z",
            "referenced_issue_numbers": [202],
        },
    )
    # PR 602 only mentions SYN-202 in title/head_ref (rule context cue -> origin='RULE', CONTEXT_ONLY)
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="602",
        metadata={
            "provider_number": 602,
            "title": "Expose SKU deprecation filter on catalog endpoint (SYN-202)",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
            "head_ref": "feat/syn-202-api",
            "author_login": "syn-dev-a",
            "merged_at": "2026-10-25T14:00:00Z",
            "labels": ["ticket:syn-202"],
        },
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    # Strictly 2 separate PR CANDIDATE events, 0 USER_COMPOSITE, 0 standalone Issue events
    assert len(res.pr_event_ids) == 2
    assert len(res.direct_commit_event_ids) == 0

    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 2
    assert all(e["anchor_mode"] == WorkEventAnchorMode.PR.value for e in events)

    ev_601 = next(e for e in events if e["summary_metadata"]["pr_number"] == 601)
    ev_602 = next(e for e in events if e["summary_metadata"]["pr_number"] == 602)

    m_601 = _fetch_event_memberships(pg18_cluster, ws_id, ev_601["id"])
    m_602 = _fetch_event_memberships(pg18_cluster, ws_id, ev_602["id"])

    iss_link_601 = [m for m in m_601 if m["source_object_id"] == issue_id]
    iss_link_602 = [m for m in m_602 if m["source_object_id"] == issue_id]
    assert len(iss_link_601) == 1
    assert iss_link_601[0]["relation_class"] == EventMembershipRelationClass.CONTEXT_ONLY.value
    assert iss_link_601[0]["relation_type"] == EventRelationType.ISSUE_REFERENCE.value
    assert iss_link_601[0]["origin"] == EventMembershipOrigin.SOURCE.value

    assert len(iss_link_602) == 1
    assert iss_link_602[0]["relation_class"] == EventMembershipRelationClass.CONTEXT_ONLY.value
    assert iss_link_602[0]["relation_type"] == EventRelationType.ISSUE_REFERENCE.value
    assert iss_link_602[0]["origin"] == EventMembershipOrigin.RULE.value


# =============================================================================
# 6. Frozen Case EV-007 (Mixed-Purpose PR -> Flagged, 0 Auto-Splits)
# =============================================================================
def test_ev007_mixed_purpose_pr_emits_single_pr_with_flag_and_zero_auto_splits(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/reporting-core", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )

    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="701",
        metadata={
            "provider_number": 701,
            "title": "Refactor auth header parser and add monthly CSV export",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
            "head_ref": "feat/auth-and-csv-export",
            "merged_at": "2026-10-26T17:15:00Z",
        },
    )
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="701a000000000000000000000000000000000001",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={
            "head_sha": "701a000000000000000000000000000000000001",
            "on_default_branch": False,
            "associated_pr_number": 701,
            "changed_paths": ["src/middleware/auth_header.py"],
        },
    )
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="701b000000000000000000000000000000000002",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={
            "head_sha": "701b000000000000000000000000000000000002",
            "on_default_branch": False,
            "associated_pr_number": 701,
            "changed_paths": ["src/export/csv_stream.py"],
        },
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 1
    assert events[0]["anchor_mode"] == WorkEventAnchorMode.PR.value
    assert ReviewFlagCode.MIXED_PURPOSE_SIGNALS.value in events[0]["summary_metadata"]["review_flags"]


# =============================================================================
# 7. Frozen Case CAND-EV-019 (default_branch='trunk', decoy 'main', squash/rebase/OOW dedup, no heuristic link)
# =============================================================================
def test_cand_ev019_trunk_default_branch_dedup_and_no_heuristic_linking(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/payments-trunk", default_branch="trunk"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="trunk",
        import_window_start="2026-10-01T00:00:00Z",
        import_window_end="2026-11-01T00:00:00Z",
    )

    # Squash PR #1901 (in-window, trunk)
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="1901",
        metadata={
            "provider_number": 1901,
            "title": "Add settlement idempotency guard for retry queue",
            "state": "MERGED",
            "merged": True,
            "base_ref": "trunk",
            "merge_strategy": "SQUASH",
            "squash_commit_sha": "1903000000000000000000000000000000000003",
            "merged_at": "2026-10-18T14:00:00Z",
            "author_login": "syn-dev-a",
        },
    )
    # Rebase PR #1902 (in-window, trunk)
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="1902",
        metadata={
            "provider_number": 1902,
            "title": "Add ledger currency precision rounding tests",
            "state": "MERGED",
            "merged": True,
            "base_ref": "trunk",
            "merge_strategy": "REBASE",
            "rebase_commit_shas": ["1904000000000000000000000000000000000004"],
            "merged_at": "2026-10-20T11:00:00Z",
            "author_login": "syn-dev-b",
        },
    )

    # Non-default commit on branch literally named 'main' (must NOT become DIRECT_COMMIT!)
    c_main_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1901000000000000000000000000000000000001",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": "1901000000000000000000000000000000000001",
            "title": "Legacy maintenance patch on non-default branch named main",
            "branch_name": "main",
            "on_default_branch": False,
        },
    )
    # Non-default commit on 'feat/draft-exp'
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1902000000000000000000000000000000000002",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": "1902000000000000000000000000000000000002",
            "title": "WIP: scratch retry benchmark",
            "branch_name": "feat/draft-exp",
            "on_default_branch": False,
        },
    )
    # Squash trunk commit -> MATCHED_PR 1901
    c_squash_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1903000000000000000000000000000000000003",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={"head_sha": "1903000000000000000000000000000000000003", "branch_name": "trunk", "on_default_branch": True},
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_squash_id,
        commit_sha="1903000000000000000000000000000000000003",
        default_branch_name="trunk",
        state=PrAssociationState.MATCHED_PR,
        matched_pr_provider_id="1901",
        matched_pr_in_window=True,
        match_mechanism="SQUASH_COMMIT_SHA",
    )
    # Rebase trunk commit -> MATCHED_PR 1902
    c_rebase_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1904000000000000000000000000000000000004",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={"head_sha": "1904000000000000000000000000000000000004", "branch_name": "trunk", "on_default_branch": True},
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_rebase_id,
        commit_sha="1904000000000000000000000000000000000004",
        default_branch_name="trunk",
        state=PrAssociationState.MATCHED_PR,
        matched_pr_provider_id="1902",
        matched_pr_in_window=True,
        match_mechanism="REBASE_COMMIT_SHA",
    )
    # Out-of-window PR #1903 linked trunk commit -> 0 PR event AND 0 DIRECT_COMMIT event
    c_oow_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1905000000000000000000000000000000000005",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={"head_sha": "1905000000000000000000000000000000000005", "branch_name": "trunk", "on_default_branch": True},
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_oow_id,
        commit_sha="1905000000000000000000000000000000000005",
        default_branch_name="trunk",
        state=PrAssociationState.MATCHED_PR,
        matched_pr_provider_id="1903",
        matched_pr_in_window=False,
        match_mechanism="OUT_OF_WINDOW_PR_API",
    )
    # Heuristic decoy commit (same author, +2m timestamp, near-identical title to PR #1901, NO_MATCH_OBSERVED)
    c_direct_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="1906000000000000000000000000000000000006",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": "1906000000000000000000000000000000000006",
            "title": "Add settlement idempotency guard for retry queue config",
            "author_login": "syn-dev-a",
            "committed_at": "2026-10-18T14:02:00Z",
            "branch_name": "trunk",
            "on_default_branch": True,
        },
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_direct_id,
        commit_sha="1906000000000000000000000000000000000006",
        default_branch_name="trunk",
        state=PrAssociationState.NO_MATCH_OBSERVED,
        reachable_at_start=True,
        reachable_at_end=True,
        lookup_completed=True,
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    assert res.default_branch == "trunk"
    assert len(res.pr_event_ids) == 2
    assert len(res.direct_commit_event_ids) == 1
    assert res.excluded_non_default_commit_count == 2
    assert res.excluded_out_of_window_pr_commit_count == 1

    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 3
    direct_ev = next(e for e in events if e["anchor_mode"] == WorkEventAnchorMode.DIRECT_COMMIT.value)
    assert direct_ev["primary_anchor_source_object_id"] == c_direct_id
    assert direct_ev["summary_metadata"]["default_branch"] == "trunk"
    assert direct_ev["summary_metadata"]["requires_user_confirmation"] is True
    # Ensure decoy commit was NEVER heuristically attached to PR #1901
    pr_1901 = next(e for e in events if e["summary_metadata"].get("pr_number") == 1901)
    pr_1901_members = _fetch_event_memberships(pg18_cluster, ws_id, pr_1901["id"])
    assert all(m["source_object_id"] != c_direct_id for m in pr_1901_members)
    assert all(m["source_object_id"] != c_main_id for m in pr_1901_members)


# =============================================================================
# 8. Frozen Case CAND-EV-020 (default_branch='master', UNKNOWN -> 0 promoted even for sibling PR)
# =============================================================================
def test_cand_ev020_master_default_branch_unknown_blocks_all_events_including_sibling_pr(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/legacy-master-svc", default_branch="master"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="master",
    )
    # Sibling merged PR targeting 'master'
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="2001",
        metadata={
            "provider_number": 2001,
            "title": "Tighten legacy adapter connection pool limit",
            "state": "MERGED",
            "merged": True,
            "base_ref": "master",
            "merged_at": "2026-10-19T12:00:00Z",
        },
    )
    # UNKNOWN commit on 'master'
    c_unk_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="2001000000000000000000000000000000000001",
        pr_association_state=PrAssociationState.UNKNOWN,
        metadata={"branch_name": "master", "on_default_branch": True},
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_unk_id,
        commit_sha="2001000000000000000000000000000000000001",
        default_branch_name="master",
        state=PrAssociationState.UNKNOWN,
        unknown_reason_code="ASSOCIATION_LOOKUP_FAILED",
        lookup_completed=False,
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.REJECTED
    assert res.error_code is CandidateBuildErrorCode.PR_ASSOCIATION_INCOMPLETE
    assert _fetch_all_events(pg18_cluster, ws_id) == []


# =============================================================================
# 9. EVENT-05 (Active CANNOT_LINK precedence) + All 7 Deterministic Review Flags
# =============================================================================
def test_event05_cannot_link_precedence_and_all_seven_review_flags_in_pg18(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme-syn-lab/flags-and-constraints", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )

    # Seed AMBIGUOUS / CONFLICTED identity mapping for author "ambig-dev"
    identity_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.identities (id, workspace_id, display_name, identity_type, status)
                    VALUES (%s::uuid, %s::uuid, 'Ambiguous Dev', 'PERSON', 'AMBIGUOUS')
                    """,
                    (identity_id, ws_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.external_identity_mappings (
                        id, workspace_id, identity_id, provider, external_user_id,
                        username, verification_method, status
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'GITHUB', 'gh-ambig-42',
                        'ambig-dev', 'PROVIDER_ASSOCIATION', 'CONFLICTED'
                    )
                    """,
                    (str(uuid.uuid4()), ws_id, identity_id),
                )

    # PR with generic title "wip", 2 linked issues, skipped oversized file, ambiguous author, mixed dirs
    pr_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="801",
        metadata={
            "provider_number": 801,
            "title": "wip",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
            "author_external_id": "gh-ambig-42",
            "author_login": "ambig-dev",
            "referenced_issue_numbers": [11, 12],
            "top_level_dirs": ["auth", "billing", "infra"],
        },
    )
    commit_blocked_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="801a000000000000000000000000000000000001",
        pr_association_state=PrAssociationState.MATCHED_PR,
        metadata={"associated_pr_number": 801, "on_default_branch": False},
    )
    iss1_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="11",
        metadata={"provider_number": 11},
    )
    iss2_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="12",
        metadata={"provider_number": 12},
    )

    # Multi-parent DIRECT_COMMIT -> REVIEW_REQUIRED_MULTI_PARENT + DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED
    dc_sha = "8020000000000000000000000000000000000002"
    dc_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=dc_sha,
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": dc_sha,
            "title": "Merge hotfix branch into main directly",
            "branch_name": "main",
            "on_default_branch": True,
            "parent_count": 2,
            "parent_shas": [
                "80200000000000000000000000000000000000aa",
                "80200000000000000000000000000000000000bb",
            ],
        },
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=dc_id,
        commit_sha=dc_sha,
        default_branch_name="main",
        state=PrAssociationState.NO_MATCH_OBSERVED,
        reachable_at_start=True,
        reachable_at_end=True,
        lookup_completed=True,
    )

    # Seed SKIPPED item outcome (OVERSIZED_FILE) on PR #801
    # AND pre-seed existing PR work_event + active CANNOT_LINK on commit_blocked_id and iss2_id (EVENT-05)
    pre_event_id = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.sync_item_outcomes (
                        id, workspace_id, sync_run_id, source_resource_id, source_object_id,
                        stream_name, object_type, provider_object_id, outcome_kind,
                        reason_code, outcome_metadata
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        'PRS', 'FILE', 'large_bundle.bin', 'SKIPPED',
                        'OVERSIZED_FILE', '{"pr_number": 801}'::jsonb
                    )
                    """,
                    (str(uuid.uuid4()), ws_id, run_id, res_id, pr_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        version, title, summary_metadata, status, created_by
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'PR',
                        1, 'PR #801', '{}'::jsonb, 'CANDIDATE', 'RULE'
                    )
                    """,
                    (pre_event_id, ws_id, pr_id),
                )
                for blocked_obj, rel_type in (
                    (commit_blocked_id, "USER_EXCLUDED_COMMIT"),
                    (iss2_id, "USER_EXCLUDED_ISSUE"),
                ):
                    cur.execute(
                        """
                        INSERT INTO public.event_memberships (
                            id, workspace_id, event_id, source_object_id,
                            relation_class, relation_type, origin, rationale_code, status
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            'CANNOT_LINK', %s, 'USER', 'USER_EXCLUDED', 'ACTIVE'
                        )
                        """,
                        (str(uuid.uuid4()), ws_id, pre_event_id, blocked_obj, rel_type),
                    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)

    assert res.status is CandidateBuildStatus.SUCCEEDED
    assert res.preserved_cannot_link_count == 2

    events = _fetch_all_events(pg18_cluster, ws_id)
    pr_ev = next(e for e in events if e["id"] == pre_event_id)
    dc_ev = next(e for e in events if e["anchor_mode"] == WorkEventAnchorMode.DIRECT_COMMIT.value)

    assert pr_ev["summary_metadata"]["review_flags"] == sorted(
        [
            ReviewFlagCode.AMBIGUOUS_AUTHOR_IDENTITY.value,
            ReviewFlagCode.GENERIC_TITLE_OR_SUMMARY.value,
            ReviewFlagCode.MIXED_PURPOSE_SIGNALS.value,
            ReviewFlagCode.MULTIPLE_LINKED_ISSUES.value,
            ReviewFlagCode.PARTIAL_SOURCE_SKIPPED.value,
        ]
    )
    assert dc_ev["summary_metadata"]["review_flags"] == sorted(
        [
            ReviewFlagCode.DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED.value,
            ReviewFlagCode.REVIEW_REQUIRED_MULTI_PARENT.value,
        ]
    )

    # EVENT-05: CANNOT_LINK preserved, NEVER overwritten or bypassed with MUST_LINK/CONTEXT_ONLY
    pr_memberships = _fetch_event_memberships(pg18_cluster, ws_id, pre_event_id)
    commit_rows = [m for m in pr_memberships if m["source_object_id"] == commit_blocked_id]
    iss2_rows = [m for m in pr_memberships if m["source_object_id"] == iss2_id]
    iss1_rows = [m for m in pr_memberships if m["source_object_id"] == iss1_id]

    assert len(commit_rows) == 1
    assert commit_rows[0]["relation_class"] == EventMembershipRelationClass.CANNOT_LINK.value
    assert commit_rows[0]["origin"] == EventMembershipOrigin.USER.value

    assert len(iss2_rows) == 1
    assert iss2_rows[0]["relation_class"] == EventMembershipRelationClass.CANNOT_LINK.value
    assert iss2_rows[0]["origin"] == EventMembershipOrigin.USER.value

    assert len(iss1_rows) == 1
    assert iss1_rows[0]["relation_class"] == EventMembershipRelationClass.CONTEXT_ONLY.value


# =============================================================================
# 10. Idempotency + Enrichment over M2 finalize_import_run + User Review Protection
# =============================================================================
def test_idempotent_enrichment_over_m2_finalize_and_user_review_protection(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/idempotent-enrich", default_branch="main"
    )
    ws_id, res_id, run_id = seed["workspace_id"], seed["source_resource_id"], seed["sync_run_id"]
    committer = PostgresBackfillPageCommitter(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    dc_sha = "9010000000000000000000000000000000000001"
    pr_commit_sha = "9020000000000000000000000000000000000002"

    # Commit required streams through real M2 PostgresBackfillPageCommitter
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha256("prs-p1"),
            items=(
                NormalizedPageItem(
                    object_type=SourceObjectType.PR,
                    provider_object_id="50",
                    content_hash=_sha256("pr-50"),
                    normalized_metadata={
                        "provider_number": 50,
                        "state": "closed",
                        "merged": True,
                        "base_ref": "main",
                        "head_sha": pr_commit_sha,
                        "created_at": "2026-10-20T10:00:00Z",
                        "merged_at": "2026-10-20T12:00:00Z",
                    },
                ),
            ),
            stream_completed=True,
            cursor_metadata_extra={"referenced_issues_by_pr": {"50": [77]}},
        ),
        now=now,
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            stream_name=BackfillStreamName.PR_REVIEWS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha256("reviews-p1"),
            items=(
                NormalizedPageItem(
                    object_type=SourceObjectType.REVIEW,
                    provider_object_id="rev-50-1",
                    content_hash=_sha256("rev-50-1"),
                    normalized_metadata={"provider_number": 50, "state": "APPROVED"},
                ),
                NormalizedPageItem(
                    object_type=SourceObjectType.COMMENT,
                    provider_object_id="cmt-50-1",
                    content_hash=_sha256("cmt-50-1"),
                    normalized_metadata={"provider_number": 50},
                ),
            ),
            stream_completed=True,
        ),
        now=now,
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha256("commits-p1"),
            items=(
                NormalizedPageItem(
                    object_type=SourceObjectType.COMMIT,
                    provider_object_id=pr_commit_sha,
                    content_hash=_sha256("c-pr"),
                    normalized_metadata={"head_sha": pr_commit_sha},
                ),
                NormalizedPageItem(
                    object_type=SourceObjectType.COMMIT,
                    provider_object_id=dc_sha,
                    content_hash=_sha256("c-dc"),
                    normalized_metadata={"head_sha": dc_sha, "updated_at": "2026-10-21T08:00:00Z"},
                ),
            ),
            stream_completed=True,
        ),
        now=now,
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha256("assoc-p1"),
            commit_associations=(
                CommitAssociationDecision(
                    commit_provider_object_id=pr_commit_sha,
                    commit_sha=pr_commit_sha,
                    default_branch="main",
                    state=PrAssociationState.MATCHED_PR,
                    matched_pr_provider_id="50",
                    matched_pr_in_window=True,
                    lookup_completed=True,
                ),
                CommitAssociationDecision(
                    commit_provider_object_id=dc_sha,
                    commit_sha=dc_sha,
                    default_branch="main",
                    state=PrAssociationState.NO_MATCH_OBSERVED,
                    direct_commit_candidate=True,
                    lookup_completed=True,
                ),
            ),
            stream_completed=True,
        ),
        now=now,
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            stream_name=BackfillStreamName.CONTEXT_ISSUES,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha256("issues-p1"),
            items=(
                NormalizedPageItem(
                    object_type=SourceObjectType.ISSUE,
                    provider_object_id="77",
                    content_hash=_sha256("issue-77"),
                    normalized_metadata={"provider_number": 77},
                ),
            ),
            stream_completed=True,
        ),
        now=now,
    )

    snap = RepositoryHeadSnapshot(default_branch="main", head_sha=dc_sha)
    fin = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=ws_id,
            sync_run_id=run_id,
            source_resource_id=res_id,
            start_snapshot=snap,
            end_snapshot=snap,
            pr_candidates=(
                FinalizationPrCandidate(
                    provider_object_id="50",
                    title="PR #50",
                    member_commit_provider_ids=(pr_commit_sha,),
                    member_review_provider_ids=("rev-50-1",),
                    summary_metadata={"pr_number": 50},
                ),
            ),
            direct_commit_candidates=(
                FinalizationDirectCommitCandidate(
                    provider_object_id=dc_sha,
                    commit_sha=dc_sha,
                    default_branch="main",
                    title=f"Direct commit {dc_sha[:12]} on main",
                ),
            ),
        ),
        now=now,
    )
    assert fin.outcome is FinalizeOutcomeStatus.SUCCESS

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    # 1st builder pass: enriches M2 events in-place without duplicates
    r1 = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)
    assert r1.status is CandidateBuildStatus.SUCCEEDED
    assert r1.created_event_count == 0
    assert r1.enriched_event_count == 2
    assert r1.pr_event_ids == fin.promoted_pr_event_ids
    assert r1.direct_commit_event_ids == fin.promoted_direct_commit_event_ids
    # Added PR_COMMENT + ISSUE_REFERENCE and upgraded RULE -> SOURCE on existing memberships
    assert r1.memberships_inserted_count == 2
    assert r1.memberships_upgraded_count >= 4

    # 2nd builder pass: strict idempotent no-op!
    r2 = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)
    assert r2.status is CandidateBuildStatus.SUCCEEDED
    assert r2.created_event_count == 0
    assert r2.enriched_event_count == 0
    assert r2.memberships_inserted_count == 0
    assert r2.memberships_upgraded_count == 0
    assert r2.pr_event_ids == r1.pr_event_ids
    assert r2.direct_commit_event_ids == r1.direct_commit_event_ids

    # User review protection: mark PR event CONFIRMED & edited title, DIRECT_COMMIT as REJECTED
    pr_ev_id = fin.promoted_pr_event_ids[0]
    dc_ev_id = fin.promoted_direct_commit_event_ids[0]
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'CONFIRMED', version = 2, title = 'User Curated PR Title'
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (ws_id, pr_ev_id),
                )
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'REJECTED', title = 'Rejected Direct Commit'
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (ws_id, dc_ev_id),
                )

    r3 = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)
    assert r3.status is CandidateBuildStatus.SUCCEEDED
    assert r3.preserved_user_event_count == 2
    assert r3.created_event_count == 0
    assert r3.enriched_event_count == 0

    after_events = {e["id"]: e for e in _fetch_all_events(pg18_cluster, ws_id)}
    assert after_events[pr_ev_id]["status"] == "CONFIRMED"
    assert after_events[pr_ev_id]["version"] == 2
    assert after_events[pr_ev_id]["title"] == "User Curated PR Title"
    assert after_events[dc_ev_id]["status"] == "REJECTED"
    assert after_events[dc_ev_id]["title"] == "Rejected Direct Commit"


# =============================================================================
# 11. Policy Fence, Start & Pre-Commit Deletion Fence Rollback, Canary & RLS
# =============================================================================
def test_policy_and_deletion_fence_start_and_pre_commit_rollback_and_rls(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # 11a. Non-PUBLIC visibility -> BLOCKED with 0 writes
    seed_priv = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/private-repo", default_branch="main", visibility="PRIVATE"
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=seed_priv["workspace_id"],
        sync_run_id=seed_priv["sync_run_id"],
        source_resource_id=seed_priv["source_resource_id"],
        plan_id=seed_priv["plan_id"],
        default_branch="main",
    )
    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res_priv = builder.build_candidates_for_sync_run(
        workspace_id=seed_priv["workspace_id"], sync_run_id=seed_priv["sync_run_id"]
    )
    assert res_priv.status is CandidateBuildStatus.BLOCKED
    assert res_priv.error_code is CandidateBuildErrorCode.POLICY_BLOCKED
    assert _fetch_all_events(pg18_cluster, seed_priv["workspace_id"]) == []

    # 11b. Pre-commit deletion fence activation rolls back savepoint -> 0 persisted events
    seed_fence = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/fence-race-repo", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed_fence["workspace_id"],
        seed_fence["source_resource_id"],
        seed_fence["sync_run_id"],
        seed_fence["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )
    _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="99",
        metadata={
            "provider_number": 99,
            "title": "Add billing reconciliation metric",
            "state": "MERGED",
            "merged": True,
            "base_ref": "main",
        },
    )

    def _activate_deletion_fence_pre_commit(
        cur: psycopg.Cursor, hook_ws: str, hook_res: str
    ) -> None:
        with pg18_cluster.admin_connection() as admin_conn:
            with admin_conn.transaction():
                with admin_conn.cursor() as admin_cur:
                    admin_cur.execute(
                        """
                        INSERT INTO public.deletion_jobs (
                            id, workspace_id, source_resource_id, status
                        )
                        VALUES (%s::uuid, %s::uuid, %s::uuid, 'RUNNING')
                        """,
                        (str(uuid.uuid4()), hook_ws, hook_res),
                    )

    fenced_builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection,
        before_commit_hook=_activate_deletion_fence_pre_commit,
    )
    res_fenced = fenced_builder.build_candidates_for_sync_run(
        workspace_id=ws_id, sync_run_id=run_id
    )
    assert res_fenced.status is CandidateBuildStatus.BLOCKED
    assert res_fenced.error_code is CandidateBuildErrorCode.DELETION_FENCE_ACTIVE
    assert _fetch_all_events(pg18_cluster, ws_id) == []

    # 11b-2. REVOKED source_account -> BLOCKED / SOURCE_ACCOUNT_REVOKED with 0 writes
    seed_revoked = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/revoked-acct-repo", default_branch="main"
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=seed_revoked["workspace_id"],
        sync_run_id=seed_revoked["sync_run_id"],
        source_resource_id=seed_revoked["source_resource_id"],
        plan_id=seed_revoked["plan_id"],
        default_branch="main",
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.source_accounts
                    SET status = 'REVOKED', revoked_at = now()
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (seed_revoked["workspace_id"], seed_revoked["source_account_id"]),
                )
    res_revoked = builder.build_candidates_for_sync_run(
        workspace_id=seed_revoked["workspace_id"], sync_run_id=seed_revoked["sync_run_id"]
    )
    assert res_revoked.status is CandidateBuildStatus.BLOCKED
    assert res_revoked.error_code is CandidateBuildErrorCode.SOURCE_ACCOUNT_REVOKED
    assert _fetch_all_events(pg18_cluster, seed_revoked["workspace_id"]) == []

    # 11c. Cross-workspace RLS isolation: workspace B cannot access workspace A's sync_run
    seed_b = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/other-ws-repo", default_branch="main"
    )
    with pytest.raises(ValueError, match="not found in workspace"):
        builder.build_candidates_for_sync_run(
            workspace_id=seed_b["workspace_id"], sync_run_id=run_id
        )


def test_plan_c_ref04_zero_canary_leak_in_persisted_work_events(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(
        pg18_cluster, repo_name="acme/canary-audit-repo", default_branch="main"
    )
    ws_id, res_id, run_id, plan_id = (
        seed["workspace_id"],
        seed["source_resource_id"],
        seed["sync_run_id"],
        seed["plan_id"],
    )
    _mark_sync_run_import_ready(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        plan_id=plan_id,
        default_branch="main",
    )
    canary = "CM_CANARY_RAW_SECRET_BODY_9F8E7D6C"
    sha = "a1b2c3d4e5f60000000000000000000000000001"
    c_id, _ = _insert_source_object_with_revision(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id=sha,
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={
            "head_sha": sha,
            "subject": f"Harden cache lock retry timeout\n\n{canary}\nfull body lines",
            "branch_name": "main",
            "on_default_branch": True,
        },
    )
    _insert_commit_association(
        pg18_cluster,
        workspace_id=ws_id,
        sync_run_id=run_id,
        source_resource_id=res_id,
        commit_source_object_id=c_id,
        commit_sha=sha,
        default_branch_name="main",
        state=PrAssociationState.NO_MATCH_OBSERVED,
        reachable_at_start=True,
        reachable_at_end=True,
        lookup_completed=True,
    )

    builder = PostgresEventCandidateBuilder(
        worker_connection_factory=pg18_cluster.worker_connection
    )
    res = builder.build_candidates_for_sync_run(workspace_id=ws_id, sync_run_id=run_id)
    assert res.status is CandidateBuildStatus.SUCCEEDED

    events = _fetch_all_events(pg18_cluster, ws_id)
    assert len(events) == 1
    serialized = json.dumps(events, default=str)
    assert canary not in serialized
    assert events[0]["title"] == "Harden cache lock retry timeout"
    assert find_forbidden_raw_keys(events[0]["summary_metadata"]) == []
