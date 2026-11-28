"""Real PostgreSQL 18 integration tests for M3 Wave 2 Backend (0060 · T-302 · T-305 · T-303).

Tests run against an ephemeral local PostgreSQL 18 cluster with migrations 0010..0060
and NOBYPASSRLS runtime roles (cm_api_app & cm_worker_app). Guarantees full teardown.

Coverage:
1. Migration 0060: fresh, staged upgrade (0010..0050 -> seed -> 0060), re-apply idempotency,
   FORCE RLS, cross-workspace Composite FK 23503 blocking, Plan C forbidden raw-key JSONB
   blocking, canonical pair CHECK & partial unique index, and work_event_versions immutability
   trigger (UPDATE/DELETE blocked; DELETE allowed only under app.allow_revision_purge = 'on').
2. T-302 Relation Semantics: MUST_LINK evidence strict separation (CONTEXT_ONLY & CANNOT_LINK
   never mixed), USER_COMPOSITE MUST_LINK prohibition, EVENT-04/EVENT-05 enforcement,
   pairwise event_constraints canonicalization, and check_merge_cannot_link_conflicts
   (both CAND-EV-021 / SPLIT pairwise conflicts and cross-member MUST_LINK vs CANNOT_LINK).
3. T-305 Immutable History: PostgresEventCandidateBuilder auto-writing v1 snapshots,
   multi-version append preserving immutable v1..vN + membership_snapshot, and strict
   separation between user REJECT (review_actions) vs source STALE (audit_events + version).
4. T-303 Read API (FastAPI TestClient + real PG18 + cm_api_app FORCE RLS):
   - GET /v1/work-events: HMAC cursor pagination, filters (status, source_resource_id,
     from/to, role), OD-008 project_id rejection, EV-010 active USER_COMPOSITE single-count
     collapse (SUPERSEDED members & REJECTED excluded), 404 concealment.
   - GET /v1/work-events/{id}: ETag header, DIRECT_COMMIT badge, USER_COMPOSITE immutable
     member version snapshots, SUPERSEDED & REJECTED historical detail reads, cross-workspace
     404 concealment, live PRIVATE visibility & deletion_jobs fence projection, and Plan C
     canary non-leak.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from career_memory_api.app import create_app
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.event_candidates import (
    CandidateBuildRequest,
    CandidateBuildStatus,
    PostgresEventCandidateBuilder,
)
from career_memory_api.jobs.postgres_runtime import bind_worker_workspace_context
from career_memory_api.schema_contracts.enums import (
    ContributionRole,
    EventConstraintOrigin,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    ReviewActionType,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.work_events import (
    PostgresWorkEventsReadService,
    RelationSemanticsError,
    WorkEventPolicyState,
    append_event_version_snapshot,
    build_membership_snapshot,
    check_merge_cannot_link_conflicts,
    create_work_events_router,
    list_active_context_relations,
    list_active_evidence_memberships,
    list_cannot_link_summary,
    list_event_version_history,
    list_work_event_review_actions,
    mark_work_event_stale_from_source,
    record_pairwise_cannot_link,
    record_source_relation,
    record_work_event_review_action,
)
from tests.backfill_engine.pg_harness import (
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_0060 = REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql"
MIGRATIONS_0010_TO_0060 = (*MIGRATIONS_0010_TO_0050, MIGRATION_0060)


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m3-wave2-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0060:
            cluster.apply_sql_file(mig)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def clean_tables(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


def _sha256(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _access_for(seed: dict[str, str]) -> WorkspaceAccessContext:
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=seed["user_id"],
            auth_subject=AuthSubject(
                issuer="https://auth.local/v1",
                subject=f"sub-{seed['user_id']}",
            ),
        ),
        membership=WorkspaceMembership(
            workspace_id=seed["workspace_id"],
            user_id=seed["user_id"],
            role="OWNER",
        ),
    )


def _insert_source_object(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
    object_type: SourceObjectType,
    provider_object_id: str,
    metadata: dict[str, Any] | None = None,
    pr_association_state: PrAssociationState | None = None,
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
                        provider_object_id, lifecycle_state, pr_association_state
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, 'ACTIVE', %s)
                    """,
                    (
                        obj_id,
                        workspace_id,
                        source_resource_id,
                        object_type.value,
                        provider_object_id,
                        pr_association_state.value if pr_association_state is not None else None,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, content_hash,
                        hash_policy_version, observed_at, provider_updated_at,
                        normalized_metadata, connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        'v1', %s, %s,
                        %s::jsonb, 'gh-v1', 'v1'
                    )
                    """,
                    (
                        rev_id,
                        workspace_id,
                        obj_id,
                        _sha256(f"{workspace_id}:{provider_object_id}"),
                        now,
                        now,
                        json.dumps(metadata or {}, sort_keys=True),
                    ),
                )
    return obj_id, rev_id


def _insert_work_event(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    anchor_mode: WorkEventAnchorMode,
    title: str,
    primary_anchor_source_object_id: str | None = None,
    anchor_scope: dict[str, Any] | None = None,
    version: int = 1,
    status: WorkEventStatus = WorkEventStatus.CANDIDATE,
    created_by: str = "RULE",
    summary_metadata: dict[str, Any] | None = None,
    occurred_start_at: datetime | None = None,
    occurred_end_at: datetime | None = None,
    problem: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
) -> str:
    ev_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        anchor_scope, version, title, problem, action, outcome,
                        summary_metadata, status, created_by,
                        occurred_start_at, occurred_end_at, created_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s::jsonb, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s,
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        ev_id,
                        workspace_id,
                        primary_anchor_source_object_id,
                        anchor_mode.value,
                        json.dumps(anchor_scope, sort_keys=True) if anchor_scope is not None else None,
                        version,
                        title,
                        problem,
                        action,
                        outcome,
                        json.dumps(summary_metadata or {}, sort_keys=True),
                        status.value,
                        created_by,
                        occurred_start_at,
                        occurred_end_at,
                        now,
                        now,
                    ),
                )
    return ev_id


# =============================================================================
# 1. Migration 0060 Schema, RLS, Composite FK, Plan C & Immutability Tests
# =============================================================================
def test_migration_0060_idempotency_rls_composite_fks_plan_c_and_immutability(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # Re-applying 0060 on populated cluster is 100% idempotent
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/repo-a")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/repo-b")
    ws_a, ws_b = seed_a["workspace_id"], seed_b["workspace_id"]
    access_a, access_b = _access_for(seed_a), _access_for(seed_b)

    pr_a1, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=seed_a["source_resource_id"],
        object_type=SourceObjectType.PR,
        provider_object_id="pr-101",
    )
    pr_a2, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=seed_a["source_resource_id"],
        object_type=SourceObjectType.PR,
        provider_object_id="pr-102",
    )
    pr_b1, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_b,
        source_resource_id=seed_b["source_resource_id"],
        object_type=SourceObjectType.PR,
        provider_object_id="pr-201",
    )

    ev_a1 = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Event A1",
        primary_anchor_source_object_id=pr_a1,
    )
    ev_a2 = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Event A2",
        primary_anchor_source_object_id=pr_a2,
    )
    ev_b1 = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_b,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Event B1",
        primary_anchor_source_object_id=pr_b1,
    )

    # Seed valid rows under cm_api_app FORCE RLS
    with pg18_cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access_a) as cur:
            snap = append_event_version_snapshot(cur, workspace_id=ws_a, event_id=ev_a1)
            constraint = record_pairwise_cannot_link(
                cur,
                workspace_id=ws_a,
                event_a_id=ev_a1,
                event_b_id=ev_a2,
                origin=EventConstraintOrigin.USER,
                rationale_code="SEPARATE_INITIATIVES",
            )

    # Re-apply 0060 -> existing rows preserved untouched
    pg18_cluster.apply_sql_file(MIGRATION_0060)

    # Fail-closed RLS: without GUC -> 0 rows; with workspace B -> 0 rows of A
    with pg18_cluster.api_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.event_constraints")
                assert cur.fetchone() == (0,)
                cur.execute("SELECT COUNT(*) FROM public.work_event_versions")
                assert cur.fetchone() == (0,)

        with workspace_scoped_transaction(conn, access_b) as cur:
            cur.execute("SELECT COUNT(*) FROM public.event_constraints")
            assert cur.fetchone() == (0,)
            cur.execute("SELECT COUNT(*) FROM public.work_event_versions")
            assert cur.fetchone() == (0,)

        # Least-privilege GRANT check: cm_api_app has zero UPDATE/DELETE on work_event_versions
        with workspace_scoped_transaction(conn, access_a) as cur:
            with pytest.raises(pg_errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE public.work_event_versions SET title = 'Hacked' WHERE id = %s::uuid",
                    (snap.version_id,),
                )
        with workspace_scoped_transaction(conn, access_a) as cur:
            with pytest.raises(pg_errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM public.work_event_versions WHERE id = %s::uuid",
                    (snap.version_id,),
                )

    # Admin-level checks for Composite FK (23503), Plan C JSONB, canonical CHECK, and trigger
    with pg18_cluster.admin_connection() as conn:
        # 1. Cross-workspace Composite FK blocked with SQLSTATE 23503
        left_x, right_x = (ev_a1, ev_b1) if ev_a1 < ev_b1 else (ev_b1, ev_a1)
        with pytest.raises(pg_errors.ForeignKeyViolation) as fk_exc_1:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.event_constraints (
                            workspace_id, left_event_id, right_event_id, origin
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'USER')
                        """,
                        (ws_a, left_x, right_x),
                    )
        assert fk_exc_1.value.sqlstate == "23503"

        with pytest.raises(pg_errors.ForeignKeyViolation) as fk_exc_2:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.work_event_versions (
                            workspace_id, event_id, version, anchor_mode,
                            primary_anchor_source_object_id, title, status, created_by
                        ) VALUES (%s::uuid, %s::uuid, 1, 'PR', %s::uuid, 'Cross WS', 'CANDIDATE', 'RULE')
                        """,
                        (ws_a, ev_b1, pr_a1),
                    )
        assert fk_exc_2.value.sqlstate == "23503"

        # 2. Canonical pair CHECK blocks self-pair and reversed pair
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.event_constraints (
                            workspace_id, left_event_id, right_event_id, origin
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'USER')
                        """,
                        (ws_a, ev_a1, ev_a1),
                    )

        hi, lo = (ev_a1, ev_a2) if ev_a1 > ev_a2 else (ev_a2, ev_a1)
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.event_constraints (
                            workspace_id, left_event_id, right_event_id, origin
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'USER')
                        """,
                        (ws_a, hi, lo),
                    )

        # 3. Plan C forbidden raw-text JSONB key checks on both tables
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.event_constraints
                        SET constraint_metadata = '{"nested": {"raw_body": "CANARY_SECRET"}}'::jsonb
                        WHERE id = %s::uuid
                        """,
                        (constraint.constraint_id,),
                    )

        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.work_event_versions (
                            workspace_id, event_id, version, anchor_mode,
                            primary_anchor_source_object_id, title, status, created_by,
                            membership_snapshot
                        ) VALUES (
                            %s::uuid, %s::uuid, 99, 'PR',
                            %s::uuid, 'Bad Snapshot', 'CANDIDATE', 'RULE',
                            '{"quote": "CANARY_RAW_QUOTE"}'::jsonb
                        )
                        """,
                        (ws_a, ev_a1, pr_a1),
                    )

        # 4. Immutability trigger blocks UPDATE and normal DELETE even for superuser!
        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.work_event_versions SET title = 'Mutated' WHERE id = %s::uuid",
                        (snap.version_id,),
                    )

        with pytest.raises(pg_errors.CheckViolation):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM public.work_event_versions WHERE id = %s::uuid",
                        (snap.version_id,),
                    )

        # 5. Approved OD-003 purge context (app.allow_revision_purge = 'on') permits DELETE cascade
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.allow_revision_purge = 'on'")
                cur.execute(
                    "DELETE FROM public.work_events WHERE workspace_id = %s::uuid AND id = %s::uuid",
                    (ws_a, ev_a1),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM public.work_event_versions WHERE id = %s::uuid",
                    (snap.version_id,),
                )
                assert cur.fetchone() == (0,)


# =============================================================================
# 2. T-302 Relation Semantics & Merge Conflict Detection Tests
# =============================================================================
def test_t302_relation_semantics_and_merge_cannot_link_conflicts(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster)
    ws_id = seed["workspace_id"]
    res_id = seed["source_resource_id"]
    access = _access_for(seed)

    pr1, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-1",
    )
    commit1, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="aaaa1111bbbb2222",
    )
    commit2, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="cccc3333dddd4444",
    )
    issue1, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="issue-42",
    )

    ev_a = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_id,
        anchor_mode=WorkEventAnchorMode.PR,
        title="PR Event A",
        primary_anchor_source_object_id=pr1,
    )
    ev_b = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_id,
        anchor_mode=WorkEventAnchorMode.PR,
        title="PR Event B",
        primary_anchor_source_object_id=pr1,
    )
    ev_c = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_id,
        anchor_mode=WorkEventAnchorMode.PR,
        title="PR Event C",
        primary_anchor_source_object_id=pr1,
    )
    ev_comp = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_id,
        anchor_mode=WorkEventAnchorMode.USER_COMPOSITE,
        title="Composite Event",
        primary_anchor_source_object_id=None,
        created_by="USER",
    )

    with pg18_cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access) as cur:
            # Record MUST_LINK, CONTEXT_ONLY, and CANNOT_LINK on ev_a
            record_source_relation(
                cur,
                workspace_id=ws_id,
                event_id=ev_a,
                source_object_id=pr1,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PRIMARY_ANCHOR",
                origin=EventMembershipOrigin.SOURCE,
                rationale_code="PR_ANCHOR",
            )
            record_source_relation(
                cur,
                workspace_id=ws_id,
                event_id=ev_a,
                source_object_id=commit1,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PR_COMMIT",
                origin=EventMembershipOrigin.SOURCE,
            )
            record_source_relation(
                cur,
                workspace_id=ws_id,
                event_id=ev_a,
                source_object_id=issue1,
                relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                relation_type="ISSUE_REFERENCE",
                origin=EventMembershipOrigin.SOURCE,
            )
            record_source_relation(
                cur,
                workspace_id=ws_id,
                event_id=ev_a,
                source_object_id=commit2,
                relation_class=EventMembershipRelationClass.CANNOT_LINK,
                relation_type="USER_EXCLUDED_COMMIT",
                origin=EventMembershipOrigin.USER,
                rationale_code="UNRELATED_COMMIT",
            )

            # ER-002: evidence list contains strictly MUST_LINK + ACTIVE (never CONTEXT_ONLY or CANNOT_LINK)
            evidence = list_active_evidence_memberships(cur, workspace_id=ws_id, event_id=ev_a)
            assert {e.source_object_id for e in evidence} == {pr1, commit1}

            context = list_active_context_relations(cur, workspace_id=ws_id, event_id=ev_a)
            assert [c.source_object_id for c in context] == [issue1]

            cannot_summary = list_cannot_link_summary(cur, workspace_id=ws_id, event_id=ev_a)
            assert cannot_summary.source_object_ids == [commit2]
            assert cannot_summary.peer_event_ids == []

            # T-302: USER_COMPOSITE MUST_LINK source relation strictly forbidden
            with pytest.raises(RelationSemanticsError) as exc_comp:
                record_source_relation(
                    cur,
                    workspace_id=ws_id,
                    event_id=ev_comp,
                    source_object_id=pr1,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                    relation_type="PRIMARY_ANCHOR",
                    origin=EventMembershipOrigin.USER,
                )
            assert exc_comp.value.code == "USER_COMPOSITE_MUST_LINK_FORBIDDEN"

            # EVENT-04: Issue / ticket / file / branch / time / similarity forbidden as MUST_LINK
            for bad_rel_type in ("ISSUE_REFERENCE", "FILE_OVERLAP", "TIME_PROXIMITY", "BRANCH_HEURISTIC"):
                with pytest.raises(RelationSemanticsError) as exc_e04:
                    record_source_relation(
                        cur,
                        workspace_id=ws_id,
                        event_id=ev_a,
                        source_object_id=commit1,
                        relation_class=EventMembershipRelationClass.MUST_LINK,
                        relation_type=bad_rel_type,
                        origin=EventMembershipOrigin.RULE,
                    )
                assert exc_e04.value.code == "CONTEXT_ONLY_RELATION_FORBIDDEN_AS_MUST_LINK"

            with pytest.raises(RelationSemanticsError) as exc_issue_obj:
                record_source_relation(
                    cur,
                    workspace_id=ws_id,
                    event_id=ev_a,
                    source_object_id=issue1,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                    relation_type="PR_ANCHOR",
                    origin=EventMembershipOrigin.SOURCE,
                )
            assert exc_issue_obj.value.code == "CONTEXT_ONLY_RELATION_FORBIDDEN_AS_MUST_LINK"

            # EVENT-05: Active CANNOT_LINK blocks automatic SOURCE/RULE MUST_LINK attachment
            for auto_origin in (EventMembershipOrigin.SOURCE, EventMembershipOrigin.RULE):
                with pytest.raises(RelationSemanticsError) as exc_e05:
                    record_source_relation(
                        cur,
                        workspace_id=ws_id,
                        event_id=ev_a,
                        source_object_id=commit2,
                        relation_class=EventMembershipRelationClass.MUST_LINK,
                        relation_type="PR_COMMIT",
                        origin=auto_origin,
                    )
                assert exc_e05.value.code == "CANNOT_LINK_BLOCKS_AUTO_ATTACHMENT"

            # Pairwise CANNOT_LINK (OD-007 / CAND-EV-021 / SPLIT sibling) is order-invariant & idempotent
            c1 = record_pairwise_cannot_link(
                cur,
                workspace_id=ws_id,
                event_a_id=ev_b,
                event_b_id=ev_a,
                origin=EventConstraintOrigin.SPLIT,
                rationale_code="SPLIT_SIBLING_PAIR",
            )
            c2 = record_pairwise_cannot_link(
                cur,
                workspace_id=ws_id,
                event_a_id=ev_a,
                event_b_id=ev_b,
                origin=EventConstraintOrigin.SPLIT,
                rationale_code="SPLIT_SIBLING_PAIR",
            )
            assert c1.constraint_id == c2.constraint_id
            assert c1.left_event_id < c1.right_event_id

            # Attach commit2 as MUST_LINK on ev_c while ev_a has commit2 as CANNOT_LINK
            record_source_relation(
                cur,
                workspace_id=ws_id,
                event_id=ev_c,
                source_object_id=commit2,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PR_COMMIT",
                origin=EventMembershipOrigin.SOURCE,
            )

            # check_merge_cannot_link_conflicts catches BOTH pairwise (ev_a, ev_b) and source-level (ev_a, ev_c)
            conflicts = check_merge_cannot_link_conflicts(
                cur,
                workspace_id=ws_id,
                member_event_ids=[ev_a, ev_b, ev_c],
            )
            kinds = {c.conflict_kind for c in conflicts}
            assert kinds == {"PAIRWISE_EVENT_CANNOT_LINK", "SOURCE_OBJECT_CANNOT_LINK"}
            pairwise_c = [c for c in conflicts if c.conflict_kind == "PAIRWISE_EVENT_CANNOT_LINK"]
            assert len(pairwise_c) == 1
            assert pairwise_c[0].constraint_id == c1.constraint_id
            source_c = [c for c in conflicts if c.conflict_kind == "SOURCE_OBJECT_CANNOT_LINK"]
            assert len(source_c) == 1
            assert source_c[0].source_object_id == commit2

            # Clean pair with no conflicts (ev_b, ev_c) returns empty
            assert (
                check_merge_cannot_link_conflicts(
                    cur, workspace_id=ws_id, member_event_ids=[ev_b, ev_c]
                )
                == []
            )


# =============================================================================
# 3. T-305 Candidate Builder v1 Auto-Snapshot & Immutable Version/Audit History
# =============================================================================
def test_t305_candidate_builder_records_v1_and_separates_reject_vs_stale(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, default_branch="main")
    ws_id = seed["workspace_id"]
    res_id = seed["source_resource_id"]
    run_id = seed["sync_run_id"]
    plan_id = seed["plan_id"]
    access = _access_for(seed)
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)

    # Mark sync run SUCCESS + import_ready + IMPORT_READY outbox
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.sync_runs
                    SET status = 'SUCCESS',
                        import_ready = true,
                        plan_spec = %s::jsonb,
                        observed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (
                        json.dumps({"default_branch": "main", "mode": "INITIAL_BACKFILL"}),
                        now,
                        now,
                        ws_id,
                        run_id,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.outbox_events (
                        id, workspace_id, event_type, aggregate_type,
                        aggregate_id, dedupe_key, payload, status,
                        next_attempt_at, created_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, 'IMPORT_READY', 'SYNC_RUN',
                        %s::uuid, %s, %s::jsonb, 'PENDING', %s, %s
                    )
                    """,
                    (
                        str(uuid.uuid4()),
                        ws_id,
                        run_id,
                        f"import_ready:{run_id}",
                        json.dumps(
                            {
                                "sync_run_id": run_id,
                                "source_resource_id": res_id,
                                "plan_id": plan_id,
                                "default_branch": "main",
                            }
                        ),
                        now,
                        now,
                    ),
                )

    pr_id, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-50",
        metadata={
            "provider_number": 50,
            "title": "Add PostgreSQL RLS guard",
            "merged": True,
            "merged_at": "2026-11-19T10:00:00Z",
            "base_ref": "main",
            "referenced_issue_numbers": ["77"],
        },
    )
    issue_id, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="77",
        metadata={"provider_number": 77},
    )
    dc_id, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="abcdef1234567890",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
        metadata={"title": "Hotfix direct commit on main", "committed_at": "2026-11-19T14:00:00Z"},
    )
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.sync_commit_pr_associations (
                        id, workspace_id, sync_run_id, source_resource_id,
                        commit_source_object_id, commit_sha, default_branch_name,
                        pr_association_state, reachable_at_start, reachable_at_end,
                        lookup_completed
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        %s::uuid, 'abcdef1234567890', 'main',
                        'NO_MATCH_OBSERVED', true, true, true
                    )
                    """,
                    (str(uuid.uuid4()), ws_id, run_id, res_id, dc_id),
                )

    builder = PostgresEventCandidateBuilder(worker_connection_factory=pg18_cluster.worker_connection)
    res1 = builder.build_candidates(CandidateBuildRequest(workspace_id=ws_id, sync_run_id=run_id))
    assert res1.status is CandidateBuildStatus.SUCCEEDED
    assert len(res1.pr_event_ids) == 1
    assert len(res1.direct_commit_event_ids) == 1
    pr_event_id = res1.pr_event_ids[0]
    dc_event_id = res1.direct_commit_event_ids[0]

    # Re-run builder -> idempotent, v1 snapshot untouched
    res2 = builder.build_candidates(CandidateBuildRequest(workspace_id=ws_id, sync_run_id=run_id))
    assert res2.status is CandidateBuildStatus.SUCCEEDED

    with pg18_cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access) as cur:
            pr_versions = list_event_version_history(cur, workspace_id=ws_id, event_id=pr_event_id)
            assert len(pr_versions) == 1
            v1 = pr_versions[0]
            assert v1.version == 1
            assert v1.title == "Add PostgreSQL RLS guard"
            assert v1.status is WorkEventStatus.CANDIDATE
            assert v1.membership_snapshot["must_link_source_object_ids"] == [pr_id]
            assert v1.membership_snapshot["context_only_source_object_ids"] == [issue_id]

            dc_versions = list_event_version_history(cur, workspace_id=ws_id, event_id=dc_event_id)
            assert len(dc_versions) == 1
            assert dc_versions[0].anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT
            assert dc_versions[0].membership_snapshot["must_link_source_object_ids"] == [dc_id]

            # Multi-version edit + user REJECT on dc_event_id
            ra_edit = record_work_event_review_action(
                cur,
                workspace_id=ws_id,
                target_event_id=pr_event_id,
                action=ReviewActionType.EDIT,
                actor_user_id=seed["user_id"],
                from_version=1,
                to_version=2,
                idempotency_key=str(uuid.uuid4()),
                reason="Clarified title",
                origin="USER",
            )
            cur.execute(
                """
                UPDATE public.work_events
                SET version = 2, title = 'Implemented PostgreSQL FORCE RLS Guard', status = 'CONFIRMED'
                WHERE workspace_id = %s::uuid AND id = %s::uuid
                """,
                (ws_id, pr_event_id),
            )
            append_event_version_snapshot(
                cur,
                workspace_id=ws_id,
                event_id=pr_event_id,
                version=2,
                created_by_review_action_id=ra_edit.review_action_id,
            )

            # System source invalidation -> STALE (v3) without writing any REJECT review_action!
            stale_snap = mark_work_event_stale_from_source(
                cur,
                workspace_id=ws_id,
                event_id=pr_event_id,
                reason_code="SOURCE_REVISION_DRIFT",
                source_object_id=pr_id,
            )
            assert stale_snap.version == 3
            assert stale_snap.status is WorkEventStatus.STALE
            assert stale_snap.created_by_review_action_id is None

            # Verify immutable v1 -> v2 -> v3 preserved in order
            all_pr_versions = list_event_version_history(cur, workspace_id=ws_id, event_id=pr_event_id)
            assert [v.version for v in all_pr_versions] == [1, 2, 3]
            assert all_pr_versions[0].title == "Add PostgreSQL RLS guard"
            assert all_pr_versions[1].title == "Implemented PostgreSQL FORCE RLS Guard"
            assert all_pr_versions[2].status is WorkEventStatus.STALE

            # Verify review_actions has ONLY user EDIT (zero REJECT from STALE!)
            pr_actions = list_work_event_review_actions(cur, workspace_id=ws_id, event_id=pr_event_id)
            assert [a.action for a in pr_actions] == [ReviewActionType.EDIT]

            # Verify audit_events captured WORK_EVENT_MARKED_STALE
            cur.execute(
                """
                SELECT action, actor_type, redacted_metadata
                FROM public.audit_events
                WHERE workspace_id = %s::uuid AND target_id = %s::uuid
                """,
                (ws_id, pr_event_id),
            )
            audit_row = cur.fetchone()
            assert audit_row is not None
            assert audit_row[0] == "WORK_EVENT_MARKED_STALE"
            assert audit_row[1] == "SYSTEM"
            assert audit_row[2]["reason_code"] == "SOURCE_REVISION_DRIFT"

            # User REJECT on dc_event_id IS recorded in review_actions (distinct from STALE)
            ra_rej = record_work_event_review_action(
                cur,
                workspace_id=ws_id,
                target_event_id=dc_event_id,
                action=ReviewActionType.REJECT,
                actor_user_id=seed["user_id"],
                from_version=1,
                to_version=2,
                idempotency_key=str(uuid.uuid4()),
                reason="Noise hotfix excluded by user",
                origin="USER",
            )
            dc_actions = list_work_event_review_actions(cur, workspace_id=ws_id, event_id=dc_event_id)
            assert len(dc_actions) == 1
            assert dc_actions[0].review_action_id == ra_rej.review_action_id
            assert dc_actions[0].action is ReviewActionType.REJECT
            assert dc_actions[0].origin == "USER"


# =============================================================================
# 4. T-303 Read API End-to-End Integration Tests (TestClient + Real PG18)
# =============================================================================
def test_t303_work_events_read_api_list_detail_composite_policy_and_rls(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/core-api")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="other/secret-repo")
    ws_a, ws_b = seed_a["workspace_id"], seed_b["workspace_id"]
    res_a, res_b = seed_a["source_resource_id"], seed_b["source_resource_id"]
    access_a = _access_for(seed_a)
    access_b = _access_for(seed_b)

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)
    t3 = t0 + timedelta(days=3)
    t4 = t0 + timedelta(days=4)

    # Seed source objects in Workspace A
    pr1_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-1",
    )
    pr2_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-2",
    )
    pr3_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-3",
    )
    issue_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.ISSUE,
        provider_object_id="issue-99",
    )
    excl_commit_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="9999888877776666",
    )
    dc_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_a,
        source_resource_id=res_a,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="deadbeef12345678",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
    )
    pr_b_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_b,
        source_resource_id=res_b,
        object_type=SourceObjectType.PR,
        provider_object_id="pr-b-1",
    )

    # 1. Standalone CONFIRMED PR event (active top-level #1)
    ev_standalone = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Standalone Confirmed PR",
        primary_anchor_source_object_id=pr1_obj,
        version=2,
        status=WorkEventStatus.CONFIRMED,
        summary_metadata={"review_flags": ["MULTIPLE_LINKED_ISSUES"]},
        occurred_start_at=t1,
        occurred_end_at=t1,
    )
    # 2. DIRECT_COMMIT CANDIDATE event (active top-level #2)
    ev_dc = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
        title="Direct commit deadbeef1234 on trunk",
        primary_anchor_source_object_id=dc_obj,
        version=1,
        status=WorkEventStatus.CANDIDATE,
        summary_metadata={
            "default_branch": "trunk",
            "abbreviated_sha": "deadbeef1234",
            "committed_at": t2.isoformat().replace("+00:00", "Z"),
            "default_branch_reachable": True,
            "pr_association_state": "NO_MATCH_OBSERVED",
            "requires_user_confirmation": True,
            "review_flags": ["DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"],
        },
        occurred_start_at=t2,
        occurred_end_at=t2,
    )
    # 3. Two member PR events superseded by USER_COMPOSITE (EV-010: must NOT double count!)
    ev_m1 = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Member PR 1 (v1 active before merge)",
        primary_anchor_source_object_id=pr2_obj,
        version=1,
        status=WorkEventStatus.CONFIRMED,
        occurred_start_at=t0,
        occurred_end_at=t0,
    )
    ev_m2 = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Member PR 2 (v1 active before merge)",
        primary_anchor_source_object_id=pr3_obj,
        version=1,
        status=WorkEventStatus.CONFIRMED,
        occurred_start_at=t3,
        occurred_end_at=t3,
    )
    # 4. REJECTED event (excluded from default active list, readable by detail & status=REJECTED)
    ev_rejected = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_a,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Rejected Noise PR",
        primary_anchor_source_object_id=pr1_obj,
        version=2,
        status=WorkEventStatus.REJECTED,
        occurred_start_at=t0,
        occurred_end_at=t0,
    )
    # 5. Workspace B event (must never appear in Workspace A; 404 on direct detail lookup)
    ev_ws_b = _insert_work_event(
        pg18_cluster,
        workspace_id=ws_b,
        anchor_mode=WorkEventAnchorMode.PR,
        title="Workspace B Secret Event",
        primary_anchor_source_object_id=pr_b_obj,
        occurred_start_at=t4,
        occurred_end_at=t4,
    )

    # Wire memberships, v1 snapshots, USER_COMPOSITE merge, contributions, facts, claims, and pairwise constraint
    with pg18_cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access_a) as cur:
            # Memberships on ev_standalone: 1 MUST_LINK + 1 CONTEXT_ONLY + 1 CANNOT_LINK
            record_source_relation(
                cur,
                workspace_id=ws_a,
                event_id=ev_standalone,
                source_object_id=pr1_obj,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PRIMARY_ANCHOR",
                origin=EventMembershipOrigin.SOURCE,
            )
            record_source_relation(
                cur,
                workspace_id=ws_a,
                event_id=ev_standalone,
                source_object_id=issue_obj,
                relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                relation_type="ISSUE_REFERENCE",
                origin=EventMembershipOrigin.SOURCE,
            )
            record_source_relation(
                cur,
                workspace_id=ws_a,
                event_id=ev_standalone,
                source_object_id=excl_commit_obj,
                relation_class=EventMembershipRelationClass.CANNOT_LINK,
                relation_type="USER_EXCLUDED",
                origin=EventMembershipOrigin.USER,
            )
            record_pairwise_cannot_link(
                cur,
                workspace_id=ws_a,
                event_a_id=ev_standalone,
                event_b_id=ev_dc,
                origin=EventConstraintOrigin.USER,
                rationale_code="DISTINCT_WORK",
            )
            ra_conf = record_work_event_review_action(
                cur,
                workspace_id=ws_a,
                target_event_id=ev_standalone,
                action=ReviewActionType.CONFIRM,
                actor_user_id=seed_a["user_id"],
                from_version=1,
                to_version=2,
                idempotency_key=str(uuid.uuid4()),
                reason="Confirmed standalone PR",
                origin="USER",
            )
            append_event_version_snapshot(
                cur,
                workspace_id=ws_a,
                event_id=ev_standalone,
                version=1,
            )
            append_event_version_snapshot(
                cur,
                workspace_id=ws_a,
                event_id=ev_standalone,
                version=2,
                created_by_review_action_id=ra_conf.review_action_id,
            )

            # Memberships + v1 snapshots on member events BEFORE superseding them
            record_source_relation(
                cur,
                workspace_id=ws_a,
                event_id=ev_m1,
                source_object_id=pr2_obj,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PRIMARY_ANCHOR",
                origin=EventMembershipOrigin.SOURCE,
            )
            record_source_relation(
                cur,
                workspace_id=ws_a,
                event_id=ev_m2,
                source_object_id=pr3_obj,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PRIMARY_ANCHOR",
                origin=EventMembershipOrigin.SOURCE,
            )
            append_event_version_snapshot(cur, workspace_id=ws_a, event_id=ev_m1, version=1)
            append_event_version_snapshot(cur, workspace_id=ws_a, event_id=ev_m2, version=1)

            # Create USER_COMPOSITE (active top-level #3) & supersede members (v2)
            ev_composite = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                    version, title, summary_metadata, status, created_by,
                    occurred_start_at, occurred_end_at
                )
                VALUES (
                    %s::uuid, %s::uuid, NULL, 'USER_COMPOSITE',
                    1, 'Multi-PR Auth Migration',
                    %s::jsonb, 'CONFIRMED', 'USER',
                    %s, %s
                )
                """,
                (
                    ev_composite,
                    ws_a,
                    json.dumps(
                        {
                            "merge_reason": "Two PRs completed one migration",
                            "regeneration_status": "STALE_PENDING",
                        }
                    ),
                    t0,
                    t4,
                ),
            )
            ra_merge = record_work_event_review_action(
                cur,
                workspace_id=ws_a,
                target_event_id=ev_composite,
                action=ReviewActionType.MERGE,
                actor_user_id=seed_a["user_id"],
                from_version=None,
                to_version=1,
                idempotency_key=str(uuid.uuid4()),
                reason="Combined two sequential auth PRs",
                origin="USER",
            )
            for idx, m_id in enumerate((ev_m1, ev_m2)):
                cur.execute(
                    """
                    INSERT INTO public.event_composite_memberships (
                        workspace_id, composite_event_id, composite_event_version,
                        member_event_id, member_event_version, ordinal, status,
                        added_by_review_action_id
                    )
                    VALUES (%s::uuid, %s::uuid, 1, %s::uuid, 1, %s, 'ACTIVE', %s::uuid)
                    """,
                    (ws_a, ev_composite, m_id, idx, ra_merge.review_action_id),
                )
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'SUPERSEDED', version = 2, title = title || ' [superseded]'
                    WHERE workspace_id = %s::uuid AND id = %s::uuid
                    """,
                    (ws_a, m_id),
                )
                append_event_version_snapshot(
                    cur,
                    workspace_id=ws_a,
                    event_id=m_id,
                    version=2,
                    created_by_review_action_id=ra_merge.review_action_id,
                )
            append_event_version_snapshot(
                cur,
                workspace_id=ws_a,
                event_id=ev_composite,
                version=1,
                created_by_review_action_id=ra_merge.review_action_id,
            )

            # Seed contribution (IMPLEMENTED) + downstream fact & stale claim on ev_standalone
            ident_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.identities (id, workspace_id, display_name, identity_type, canonical_user_id, status)
                VALUES (%s::uuid, %s::uuid, 'Dev One', 'PERSON', %s::uuid, 'ACTIVE')
                """,
                (ident_id, ws_a, seed_a["user_id"]),
            )
            cur.execute(
                """
                INSERT INTO public.contributions (workspace_id, event_id, identity_id, role, origin, status)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'IMPLEMENTED', 'RULE', 'SOURCE_SUPPORTED')
                """,
                (ws_a, ev_standalone, ident_id),
            )
            cur.execute(
                """
                INSERT INTO public.facts (
                    workspace_id, event_id, fact_type, predicate, object_text,
                    status, extractor_version
                )
                VALUES (%s::uuid, %s::uuid, 'ACTION', 'implemented', 'RLS guard', 'SUPPORTED', 'v1')
                """,
                (ws_a, ev_standalone),
            )
            cur.execute(
                """
                INSERT INTO public.claims (workspace_id, event_id, claim_text, status)
                VALUES (%s::uuid, %s::uuid, 'Implemented RLS guard across tables.', 'STALE')
                """,
                (ws_a, ev_standalone),
            )

    # Mount real PostgresWorkEventsReadService into FastAPI app
    read_service = PostgresWorkEventsReadService(
        api_connection_factory=pg18_cluster.api_connection
    )
    active_access = [access_a]
    app = create_app(
        work_events_router=create_work_events_router(
            service=read_service,
            resolve_access=lambda _req: active_access[0],
        ),
        install_work_events_handlers=True,
    )

    with TestClient(app) as client:
        # 1. Default list: strictly 3 active top-level events (composite=1, standalone=1, dc=1);
        #    EV-010: SUPERSEDED members (ev_m1, ev_m2) and REJECTED (ev_rejected) excluded!
        resp1 = client.get("/v1/work-events", params={"limit": 2}, headers={"X-Request-ID": "r-list-1"})
        assert resp1.status_code == 200
        assert resp1.headers["Cache-Control"] == "private, no-store"
        assert resp1.headers["X-Request-ID"] == "r-list-1"
        body1 = resp1.json()
        assert body1["counts"] == {
            "active_top_level_total": 3,
            "by_status": {"CANDIDATE": 1, "CONFIRMED": 2, "STALE": 0},
        }
        assert len(body1["items"]) == 2
        assert body1["page"]["has_more"] is True
        cursor_p2 = body1["page"]["next_cursor"]
        assert isinstance(cursor_p2, str) and "." in cursor_p2

        # Page 2 via opaque cursor -> remaining 1 item, zero duplicates
        resp2 = client.get("/v1/work-events", params={"limit": 2, "cursor": cursor_p2})
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert len(body2["items"]) == 1
        assert body2["page"]["has_more"] is False
        assert body2["page"]["next_cursor"] is None
        seen_ids = [i["event_id"] for i in body1["items"]] + [i["event_id"] for i in body2["items"]]
        assert set(seen_ids) == {ev_composite, ev_dc, ev_standalone}
        assert ev_m1 not in seen_ids
        assert ev_m2 not in seen_ids
        assert ev_rejected not in seen_ids
        assert ev_ws_b not in seen_ids

        # Tampered filter with old cursor -> 400 INVALID_WORK_EVENTS_CURSOR
        bad_cursor = client.get(
            "/v1/work-events",
            params={"limit": 2, "status": "CONFIRMED", "cursor": cursor_p2},
        )
        assert bad_cursor.status_code == 400
        assert bad_cursor.json()["error"]["code"] == "INVALID_WORK_EVENTS_CURSOR"

        # Filters: role=IMPLEMENTED, date range, source_resource_id, status=SUPERSEDED, status=REJECTED
        by_role = client.get("/v1/work-events", params={"role": ContributionRole.IMPLEMENTED.value})
        assert by_role.status_code == 200
        assert [i["event_id"] for i in by_role.json()["items"]] == [ev_standalone]

        by_date = client.get("/v1/work-events", params={"from": "2026-11-12", "to": "2026-11-12"})
        assert by_date.status_code == 200
        # ev_dc (Nov 12) and ev_composite (Nov 10..Nov 14 overlapping Nov 12) match
        assert set(i["event_id"] for i in by_date.json()["items"]) == {ev_dc, ev_composite}

        by_res = client.get("/v1/work-events", params={"source_resource_id": res_a})
        assert by_res.status_code == 200
        assert by_res.json()["counts"]["active_top_level_total"] == 3

        by_superseded = client.get("/v1/work-events", params={"status": "SUPERSEDED"})
        assert by_superseded.status_code == 200
        assert set(i["event_id"] for i in by_superseded.json()["items"]) == {ev_m1, ev_m2}

        by_rejected = client.get("/v1/work-events", params={"status": "REJECTED"})
        assert by_rejected.status_code == 200
        assert [i["event_id"] for i in by_rejected.json()["items"]] == [ev_rejected]

        # Cross-workspace source_resource_id -> 404 RESOURCE_NOT_FOUND (no details)
        cross_res = client.get("/v1/work-events", params={"source_resource_id": res_b})
        assert cross_res.status_code == 404
        assert cross_res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "details" not in cross_res.json()["error"]

        # OD-008 project_id rejection
        bad_proj = client.get("/v1/work-events", params={"project_id": "proj-forbidden"})
        assert bad_proj.status_code == 422
        assert bad_proj.json()["error"]["code"] == "PROHIBITED_PROJECT_ID_SCOPE"

        # 2. Detail: standalone PR event (ETag, separated relations, constraints, history, downstream)
        d_pr = client.get(f"/v1/work-events/{ev_standalone}")
        assert d_pr.status_code == 200
        assert d_pr.headers["ETag"] == '"v2"'
        assert d_pr.headers["Cache-Control"] == "private, no-store"
        pr_json = d_pr.json()
        assert pr_json["etag"] == '"v2"'
        assert pr_json["evidence_count"] == 1
        assert pr_json["context_count"] == 1
        assert [e["source_object_id"] for e in pr_json["evidence_memberships"]] == [pr1_obj]
        assert [c["source_object_id"] for c in pr_json["context_relations"]] == [issue_obj]
        assert pr_json["cannot_link_constraints"]["source_object_ids"] == [excl_commit_obj]
        assert pr_json["cannot_link_constraints"]["peer_event_ids"] == [ev_dc]
        assert len(pr_json["version_history"]) == 2
        assert [v["version"] for v in pr_json["version_history"]] == [1, 2]
        assert len(pr_json["review_actions"]) == 1
        assert pr_json["review_actions"][0]["action"] == "CONFIRM"
        assert pr_json["downstream_impact"] == {
            "fact_count": 1,
            "claim_count": 1,
            "export_count": 0,
            "stale_fact_count": 0,
            "stale_claim_count": 1,
            "processing_status": "STALE_REGENERATION_REQUIRED",
        }
        assert pr_json["policy_state"] == WorkEventPolicyState.ALLOWED.value
        assert pr_json["allowed_actions"] == ["EDIT", "SPLIT", "MERGE", "REJECT"]

        # 3. Detail: DIRECT_COMMIT badge metadata
        d_dc = client.get(f"/v1/work-events/{ev_dc}")
        assert d_dc.status_code == 200
        assert d_dc.headers["ETag"] == '"v1"'
        dc_json = d_dc.json()
        assert dc_json["direct_commit_badge"] == {
            "default_branch": "trunk",
            "abbreviated_sha": "deadbeef1234",
            "committed_at": t2.isoformat().replace("+00:00", "Z"),
            "default_branch_reachable": True,
            "pr_association_state": "NO_MATCH_OBSERVED",
            "requires_user_confirmation": True,
        }
        assert dc_json["allowed_actions"] == ["CONFIRM", "EDIT", "MERGE", "REJECT"]

        # 4. Detail: USER_COMPOSITE immutable member snapshots from work_event_versions!
        d_comp = client.get(f"/v1/work-events/{ev_composite}")
        assert d_comp.status_code == 200
        comp_json = d_comp.json()
        assert comp_json["composite_summary"]["member_count"] == 2
        assert comp_json["composite_summary"]["member_event_ids"] == [ev_m1, ev_m2]
        assert comp_json["allowed_actions"] == ["EDIT", "DISSOLVE", "REJECT"]
        comp_members = comp_json["composite_details"]["members"]
        assert len(comp_members) == 2
        # Verify immutable v1 titles are loaded (NOT the later v2 '[superseded]' live titles!)
        assert comp_members[0]["title"] == "Member PR 1 (v1 active before merge)"
        assert comp_members[0]["snapshot_available"] is True
        assert comp_members[0]["status_at_version"] == "CONFIRMED"
        assert comp_members[0]["current_status"] == "SUPERSEDED"
        assert comp_members[0]["evidence_count"] == 1
        assert comp_members[1]["title"] == "Member PR 2 (v1 active before merge)"

        # 5. Detail: SUPERSEDED member & REJECTED historical detail reads (EV-010/011/012)
        d_sup = client.get(f"/v1/work-events/{ev_m1}")
        assert d_sup.status_code == 200
        sup_json = d_sup.json()
        assert sup_json["status"] == "SUPERSEDED"
        assert sup_json["is_active_top_level"] is False
        assert sup_json["allowed_actions"] == []
        assert sup_json["composite_details"]["superseded_by_composite_id"] == ev_composite

        d_rej = client.get(f"/v1/work-events/{ev_rejected}")
        assert d_rej.status_code == 200
        assert d_rej.json()["status"] == "REJECTED"
        assert d_rej.json()["allowed_actions"] == []

        # 6. Cross-workspace event_id -> 404 RESOURCE_NOT_FOUND (no details)
        d_cross = client.get(f"/v1/work-events/{ev_ws_b}")
        assert d_cross.status_code == 404
        assert d_cross.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "details" not in d_cross.json()["error"]

        # 7. Real-time PRIVATE visibility transition -> POLICY_BLOCKED & allowed_actions=[]
        with pg18_cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.source_resources
                        SET visibility = 'PRIVATE', status = 'POLICY_REVIEW_REQUIRED'
                        WHERE workspace_id = %s::uuid AND id = %s::uuid
                        """,
                        (ws_a, res_a),
                    )
        d_priv = client.get(f"/v1/work-events/{ev_standalone}")
        assert d_priv.status_code == 200
        priv_json = d_priv.json()
        assert priv_json["policy_state"] == WorkEventPolicyState.POLICY_BLOCKED.value
        assert priv_json["allowed_actions"] == []
        assert priv_json["downstream_impact"]["processing_status"] == "POLICY_BLOCKED"

        # 8. Real-time active deletion_jobs fence -> DELETION_PENDING & allowed_actions=[]
        with pg18_cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.deletion_jobs (
                            workspace_id, source_resource_id, status
                        ) VALUES (%s::uuid, %s::uuid, 'PENDING')
                        """,
                        (ws_a, res_a),
                    )
        d_del = client.get(f"/v1/work-events/{ev_standalone}")
        assert d_del.status_code == 200
        del_json = d_del.json()
        assert del_json["policy_state"] == WorkEventPolicyState.DELETION_PENDING.value
        assert del_json["allowed_actions"] == []
        assert del_json["downstream_impact"]["processing_status"] == "DELETION_PENDING"

        # Plan C check: serialized payload contains zero forbidden raw text keys
        raw_json_str = json.dumps(del_json)
        assert "raw_body" not in raw_json_str
        assert "text_content" not in raw_json_str

        # Switching to Workspace B cannot see Workspace A cursor
        active_access[0] = access_b
        ws_b_cursor = client.get("/v1/work-events", params={"cursor": cursor_p2})
        assert ws_b_cursor.status_code == 400
        assert ws_b_cursor.json()["error"]["code"] == "INVALID_WORK_EVENTS_CURSOR"
