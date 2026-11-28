"""Real PostgreSQL 18 integration, concurrency (EVENT-09), and rollback tests for M3 Wave 3 Mutations.

Covers T-304, T-304A (SPLIT), T-304B (MERGE & DISSOLVE), Search/Export single logical Event
collapse (EV-009 ~ EV-012), multi-connection threading.Barrier concurrency contention (EVENT-09),
and before_commit_hook mid-transaction failure rollback + Idempotency-Key retry convergence.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.schema_contracts.enums import (
    EventConstraintOrigin,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.work_events import (
    ConfirmWorkEventRequest,
    EditWorkEventRequest,
    MergeMemberInput,
    MergeWorkEventsRequest,
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    RejectWorkEventRequest,
    SplitAnchorScopeInput,
    SplitPartitionInput,
    SplitWorkEventRequest,
    WorkEventError,
    append_event_version_snapshot,
    collapse_event_ids_for_search_and_export,
    create_work_events_router,
    query_searchable_top_level_work_events,
    record_pairwise_cannot_link,
    record_source_relation,
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
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m3-wave3-mut-pg18-")
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
                        'v1', %s, %s, '{}'::jsonb, 'gh-v1', 'v1'
                    )
                    """,
                    (
                        rev_id,
                        workspace_id,
                        obj_id,
                        _sha256(f"{workspace_id}:{provider_object_id}:{obj_id}"),
                        now,
                        now,
                    ),
                )
    return obj_id, rev_id


def _insert_content_unit(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    source_object_revision_id: str,
    unit_type: str = "DIFF_HUNK",
    ordinal: int = 0,
    file_path: str = "src/app.py",
) -> str:
    cu_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.content_units (
                        id, workspace_id, source_object_revision_id, unit_type,
                        anchor, content_hash, canonicalization_version, ordinal
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s::jsonb, %s, 'v1', %s
                    )
                    """,
                    (
                        cu_id,
                        workspace_id,
                        source_object_revision_id,
                        unit_type,
                        json.dumps({"parser_version": "v1", "file_path": file_path}),
                        _sha256(f"cu:{cu_id}:{file_path}:{ordinal}"),
                        ordinal,
                    ),
                )
    return cu_id


def _insert_work_event_with_v1(
    cluster: EphemeralBackfillPg18Cluster,
    access: WorkspaceAccessContext,
    *,
    anchor_mode: WorkEventAnchorMode,
    title: str,
    primary_anchor_source_object_id: str,
    status: WorkEventStatus = WorkEventStatus.CANDIDATE,
    summary_metadata: dict[str, Any] | None = None,
    occurred_start_at: datetime | None = None,
    occurred_end_at: datetime | None = None,
    extra_must_link_ids: list[tuple[str, str]] | None = None,
    context_only_ids: list[tuple[str, str]] | None = None,
) -> str:
    ev_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    t_start = occurred_start_at or now - timedelta(hours=2)
    t_end = occurred_end_at or now - timedelta(hours=1)
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        anchor_scope, version, title, summary_metadata, status, created_by,
                        occurred_start_at, occurred_end_at, created_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        NULL, 1, %s, %s::jsonb, %s, 'RULE',
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        ev_id,
                        access.workspace_id,
                        primary_anchor_source_object_id,
                        anchor_mode.value,
                        title,
                        json.dumps(summary_metadata or {}, sort_keys=True),
                        status.value,
                        t_start,
                        t_end,
                        now,
                        now,
                    ),
                )
    with cluster.api_connection() as conn:
        with workspace_scoped_transaction(conn, access) as cur:
            record_source_relation(
                cur,
                workspace_id=access.workspace_id,
                event_id=ev_id,
                source_object_id=primary_anchor_source_object_id,
                relation_class=EventMembershipRelationClass.MUST_LINK,
                relation_type="PRIMARY_ANCHOR",
                origin=EventMembershipOrigin.SOURCE,
            )
            for src_id, rel_type in extra_must_link_ids or []:
                record_source_relation(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_id,
                    source_object_id=src_id,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                    relation_type=rel_type,
                    origin=EventMembershipOrigin.SOURCE,
                )
            for src_id, rel_type in context_only_ids or []:
                record_source_relation(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_id,
                    source_object_id=src_id,
                    relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                    relation_type=rel_type,
                    origin=EventMembershipOrigin.SOURCE,
                )
            append_event_version_snapshot(
                cur,
                workspace_id=access.workspace_id,
                event_id=ev_id,
                version=1,
            )
    return ev_id


def _insert_fact_and_claim(
    cluster: EphemeralBackfillPg18Cluster,
    *,
    workspace_id: str,
    event_id: str,
    fact_status: str = "SUPPORTED",
    claim_status: str = "APPROVED",
) -> tuple[str, str]:
    fact_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.facts (
                        id, workspace_id, event_id, fact_type, predicate,
                        object_text, status, extractor_version
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTION', 'implemented', 'idempotency lock', %s, 'v1')
                    """,
                    (fact_id, workspace_id, event_id, fact_status),
                )
                cur.execute(
                    """
                    INSERT INTO public.claims (
                        id, workspace_id, event_id, version, claim_text, status
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, 1, 'Implemented idempotency lock', %s)
                    """,
                    (claim_id, workspace_id, event_id, claim_status),
                )
    return fact_id, claim_id


def _make_client(
    cluster: EphemeralBackfillPg18Cluster,
    active_access: list[WorkspaceAccessContext],
    *,
    before_commit_hook: Any = None,
) -> TestClient:
    read_service = PostgresWorkEventsReadService(api_connection_factory=cluster.api_connection)
    mutation_service = PostgresWorkEventMutationService(
        api_connection_factory=cluster.api_connection,
        before_commit_hook=before_commit_hook,
    )
    app = create_app(
        work_events_router=create_work_events_router(
            service=read_service,
            mutation_service=mutation_service,
            resolve_access=lambda _req: active_access[0],
        ),
        install_work_events_handlers=True,
    )
    return TestClient(app)


# =============================================================================
# 1. CONFIRM (CAND-EV-022), EDIT (CAND-EV-023), REJECT (EV-011), Idempotency & Policy
# =============================================================================
def test_confirm_edit_reject_lifecycle_idempotency_policy_and_provenance_preservation(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/workflow-audit")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/other-tenant")
    access_a = _access_for(seed_a)
    access_b = _access_for(seed_b)
    ws_a, res_a = seed_a["workspace_id"], seed_a["source_resource_id"]
    ws_b, res_b = seed_b["workspace_id"], seed_b["source_resource_id"]

    pr_a1, _ = _insert_source_object(
        pg18_cluster, workspace_id=ws_a, source_resource_id=res_a, object_type=SourceObjectType.PR, provider_object_id="PR#201"
    )
    pr_a2, rev_a2 = _insert_source_object(
        pg18_cluster, workspace_id=ws_a, source_resource_id=res_a, object_type=SourceObjectType.PR, provider_object_id="PR#202"
    )
    _insert_content_unit(pg18_cluster, workspace_id=ws_a, source_object_revision_id=rev_a2, ordinal=0)
    pr_b1, _ = _insert_source_object(
        pg18_cluster, workspace_id=ws_b, source_resource_id=res_b, object_type=SourceObjectType.PR, provider_object_id="PR#999"
    )

    ev_1 = _insert_work_event_with_v1(
        pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="PR #201 Original", primary_anchor_source_object_id=pr_a1
    )
    ev_2 = _insert_work_event_with_v1(
        pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="PR #202 Sibling", primary_anchor_source_object_id=pr_a2
    )
    ev_b = _insert_work_event_with_v1(
        pg18_cluster, access_b, anchor_mode=WorkEventAnchorMode.PR, title="Tenant B Event", primary_anchor_source_object_id=pr_b1
    )
    fact_1, claim_1 = _insert_fact_and_claim(pg18_cluster, workspace_id=ws_a, event_id=ev_1)

    active_access = [access_a]
    with _make_client(pg18_cluster, active_access) as client:
        # 1. CONFIRM (CAND-EV-022): CANDIDATE v1 -> CONFIRMED v2
        r_confirm = client.post(
            f"/v1/work-events/{ev_1}/actions/confirm",
            headers={"Idempotency-Key": "cmd-confirm-022", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Verified event accuracy", "idempotency_key": "cmd-confirm-022"},
        )
        assert r_confirm.status_code == 200
        assert r_confirm.headers["ETag"] == '"v2"'
        assert r_confirm.headers["Cache-Control"] == "private, no-store"
        c_json = r_confirm.json()
        assert c_json["status"] == "CONFIRMED"
        assert c_json["version"] == 2
        assert [v["version"] for v in c_json["version_history"]] == [1, 2]
        assert len(c_json["review_actions"]) == 1
        assert c_json["review_actions"][0]["action"] == "CONFIRM"

        # 2. Exact Idempotency replay -> returns v2 without creating v3 or extra review_action
        r_replay = client.post(
            f"/v1/work-events/{ev_1}/actions/confirm",
            headers={"Idempotency-Key": "cmd-confirm-022", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Verified event accuracy", "idempotency_key": "cmd-confirm-022"},
        )
        assert r_replay.status_code == 200
        assert r_replay.json()["version"] == 2
        assert len(r_replay.json()["review_actions"]) == 1

        # 3. Conflicting Idempotency-Key reuse (different target or params) -> 409 IDEMPOTENCY_CONFLICT
        r_idem_conflict = client.post(
            f"/v1/work-events/{ev_2}/actions/confirm",
            headers={"Idempotency-Key": "cmd-confirm-022"},
            json={"expected_version": 1, "reason": "Wrong target"},
        )
        assert r_idem_conflict.status_code == 409
        assert r_idem_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        # 4. Stale expected_version -> 409 EVENT_VERSION_CONFLICT with required details
        r_stale = client.post(
            f"/v1/work-events/{ev_1}/actions/confirm",
            headers={"Idempotency-Key": "cmd-confirm-stale", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Stale tab"},
        )
        assert r_stale.status_code == 409
        err_stale = r_stale.json()["error"]
        assert err_stale["code"] == "EVENT_VERSION_CONFLICT"
        assert err_stale["details"]["event_id"] == ev_1
        assert err_stale["details"]["expected_version"] == 1
        assert err_stale["details"]["current_version"] == 2
        assert err_stale["details"]["current_status"] == "CONFIRMED"
        assert err_stale["details"]["etag"] == '"v2"'
        assert "EDIT" in err_stale["details"]["allowed_actions"]

        # 5. EDIT (CAND-EV-023): v2 CONFIRMED -> v3 CANDIDATE created_by='USER', marks fact/claim STALE, sibling untouched
        r_edit = client.patch(
            f"/v1/work-events/{ev_1}",
            headers={"Idempotency-Key": "cmd-edit-023", "If-Match": '"v2"'},
            json={
                "expected_version": 2,
                "title": "PR #201 User Edited Title",
                "problem": "Audit log gaps",
                "action": "Added immutable append-only log",
                "outcome": "100% traceability",
                "reason": "Clarify scope",
            },
        )
        assert r_edit.status_code == 200
        assert r_edit.headers["ETag"] == '"v3"'
        e_json = r_edit.json()
        assert e_json["status"] == "CANDIDATE"
        assert e_json["version"] == 3
        assert e_json["created_by"] == "USER"
        assert e_json["title"] == "PR #201 User Edited Title"
        assert [v["version"] for v in e_json["version_history"]] == [1, 2, 3]
        assert e_json["version_history"][0]["title"] == "PR #201 Original"
        assert e_json["version_history"][2]["title"] == "PR #201 User Edited Title"
        assert e_json["downstream_impact"]["stale_fact_count"] == 1
        assert e_json["downstream_impact"]["stale_claim_count"] == 1

        # Verify sibling untouched & outbox event recorded
        d_sib = client.get(f"/v1/work-events/{ev_2}")
        assert d_sib.json()["version"] == 1
        assert d_sib.json()["title"] == "PR #202 Sibling"

        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.facts WHERE id = %s::uuid", (fact_1,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute("SELECT status FROM public.claims WHERE id = %s::uuid", (claim_1,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute(
                    "SELECT COUNT(*) FROM public.outbox_events WHERE workspace_id = %s::uuid AND event_type = 'WORK_EVENT_EDITED'",
                    (ws_a,),
                )
                assert cur.fetchone()[0] == 1

        # 6. REJECT (EV-011): excludes event from active list/search while preserving 100% of source provenance
        r_rej = client.post(
            f"/v1/work-events/{ev_2}/actions/reject",
            headers={"Idempotency-Key": "cmd-reject-011", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Chore commit, exclude from career log"},
        )
        assert r_rej.status_code == 200
        assert r_rej.headers["ETag"] == '"v2"'
        assert r_rej.json()["status"] == "REJECTED"
        assert r_rej.json()["allowed_actions"] == []

        # Excluded from active list & search collapse
        list_res = client.get("/v1/work-events").json()
        assert [item["event_id"] for item in list_res["items"]] == [ev_1]
        with pg18_cluster.api_connection() as conn:
            with workspace_scoped_transaction(conn, access_a) as cur:
                assert collapse_event_ids_for_search_and_export(
                    cur, workspace_id=ws_a, candidate_event_ids=[ev_1, ev_2]
                ) == [ev_1]
                assert query_searchable_top_level_work_events(
                    cur, workspace_id=ws_a, query_text="Sibling"
                ) == []

        # Source objects, revisions, and content_units 100% preserved!
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.source_objects WHERE id = %s::uuid", (pr_a2,))
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT COUNT(*) FROM public.source_object_revisions WHERE id = %s::uuid", (rev_a2,))
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT COUNT(*) FROM public.content_units WHERE source_object_revision_id = %s::uuid", (rev_a2,))
                assert cur.fetchone()[0] == 1

        # 7. Cross-workspace concealment -> 404 RESOURCE_NOT_FOUND with no details
        r_cross = client.post(
            f"/v1/work-events/{ev_b}/actions/confirm",
            headers={"Idempotency-Key": "cmd-cross-404"},
            json={"expected_version": 1},
        )
        assert r_cross.status_code == 404
        assert r_cross.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "details" not in r_cross.json()["error"]

        # 8. Real-time PRIVATE visibility block (403 SOURCE_POLICY_BLOCKED) & deletion fence (409 DELETION_PENDING)
        with pg18_cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.source_resources SET visibility = 'PRIVATE', status = 'POLICY_REVIEW_REQUIRED' WHERE id = %s::uuid",
                        (res_a,),
                    )
        r_priv = client.post(
            f"/v1/work-events/{ev_1}/actions/confirm",
            headers={"Idempotency-Key": "cmd-policy-blocked"},
            json={"expected_version": 3},
        )
        assert r_priv.status_code == 403
        assert r_priv.json()["error"]["code"] == "SOURCE_POLICY_BLOCKED"

        with pg18_cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.deletion_jobs (workspace_id, source_resource_id, status) VALUES (%s::uuid, %s::uuid, 'PENDING')",
                        (ws_a, res_a),
                    )
        r_del = client.post(
            f"/v1/work-events/{ev_1}/actions/confirm",
            headers={"Idempotency-Key": "cmd-del-fence"},
            json={"expected_version": 3},
        )
        assert r_del.status_code == 409
        assert r_del.json()["error"]["code"] == "DELETION_PENDING"
        # Zero side effects: ev_1 still v3 CANDIDATE
        assert client.get(f"/v1/work-events/{ev_1}").json()["version"] == 3


# =============================================================================
# 2. SPLIT (T-304A, EV-008): PR_SEGMENT, DIRECT_COMMIT scope split, guards & CANNOT_LINK
# =============================================================================
def test_split_pr_and_direct_commit_scope_guards_and_cannot_link(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/reporting-core")
    access = _access_for(seed)
    ws_id, res_id = seed["workspace_id"], seed["source_resource_id"]

    pr_obj, pr_rev = _insert_source_object(
        pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#308"
    )
    shared_commit, commit_rev = _insert_source_object(
        pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.COMMIT, provider_object_id="c0ffee123456"
    )
    issue_obj, _ = _insert_source_object(
        pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.ISSUE, provider_object_id="ISSUE#88"
    )
    cu_a = _insert_content_unit(pg18_cluster, workspace_id=ws_id, source_object_revision_id=commit_rev, ordinal=0, file_path="src/ledger/a.py")
    cu_b = _insert_content_unit(pg18_cluster, workspace_id=ws_id, source_object_revision_id=commit_rev, ordinal=1, file_path="src/worker/b.py")

    dc_obj, _ = _insert_source_object(
        pg18_cluster,
        workspace_id=ws_id,
        source_resource_id=res_id,
        object_type=SourceObjectType.COMMIT,
        provider_object_id="beefdead7777",
        pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
    )
    unrelated_obj, _ = _insert_source_object(
        pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.COMMIT, provider_object_id="999999999999"
    )

    ev_pr = _insert_work_event_with_v1(
        pg18_cluster,
        access,
        anchor_mode=WorkEventAnchorMode.PR,
        title="PR #308 Mixed Purpose",
        primary_anchor_source_object_id=pr_obj,
        extra_must_link_ids=[(shared_commit, "PR_COMMIT")],
        context_only_ids=[(issue_obj, "ISSUE_REFERENCE")],
    )
    ev_dc = _insert_work_event_with_v1(
        pg18_cluster,
        access,
        anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
        title="Direct commit mixed hotfix",
        primary_anchor_source_object_id=dc_obj,
        summary_metadata={
            "default_branch": "main",
            "abbreviated_sha": "beefdead7777",
            "default_branch_reachable": True,
            "pr_association_state": "NO_MATCH_OBSERVED",
            "requires_user_confirmation": True,
        },
    )

    with _make_client(pg18_cluster, [access]) as client:
        # Reject empty anchor_scope (422 INVALID_SPLIT_SCOPE)
        r_empty = client.post(
            f"/v1/work-events/{ev_pr}/actions/split",
            headers={"Idempotency-Key": "split-empty", "If-Match": '"v1"'},
            json={
                "expected_version": 1,
                "partitions": [
                    {"title": "Part A", "source_object_ids": [shared_commit]},
                    {"title": "Part B", "source_object_ids": [shared_commit]},
                ],
            },
        )
        assert r_empty.status_code == 422
        assert r_empty.json()["error"]["code"] == "INVALID_SPLIT_SCOPE"

        # Reject overlapping file_paths / hunk_ranges (422 SPLIT_SCOPE_OVERLAP)
        r_overlap = client.post(
            f"/v1/work-events/{ev_pr}/actions/split",
            headers={"Idempotency-Key": "split-overlap", "If-Match": '"v1"'},
            json={
                "expected_version": 1,
                "partitions": [
                    {"title": "Part A", "anchor_scope": {"file_paths": ["src/ledger/a.py"], "hunk_ranges": ["a.py:1-10"]}},
                    {"title": "Part B", "anchor_scope": {"file_paths": ["src/ledger/a.py"], "hunk_ranges": ["b.py:1-10"]}},
                ],
            },
        )
        assert r_overlap.status_code == 422
        assert r_overlap.json()["error"]["code"] == "SPLIT_SCOPE_OVERLAP"

        # Reject source_object_id outside parent scope (422 INVALID_SPLIT_SCOPE)
        r_foreign = client.post(
            f"/v1/work-events/{ev_pr}/actions/split",
            headers={"Idempotency-Key": "split-foreign", "If-Match": '"v1"'},
            json={
                "expected_version": 1,
                "partitions": [
                    {"title": "Part A", "source_object_ids": [unrelated_obj], "anchor_scope": {"file_paths": ["src/a.py"]}},
                    {"title": "Part B", "anchor_scope": {"file_paths": ["src/b.py"]}},
                ],
            },
        )
        assert r_foreign.status_code == 422
        assert r_foreign.json()["error"]["code"] == "INVALID_SPLIT_SCOPE"

        # Valid PR split sharing same commit with mutually disjoint anchor_scope (EV-008)
        split_body = {
            "expected_version": 1,
            "reason": "Separate ledger logic from worker refactor",
            "partitions": [
                {
                    "title": "PR #308 Segment A: Ledger",
                    "source_object_ids": [shared_commit],
                    "anchor_scope": {
                        "file_paths": ["src/ledger/a.py"],
                        "hunk_ranges": ["src/ledger/a.py:10-40"],
                        "content_unit_ids": [cu_a],
                    },
                },
                {
                    "title": "PR #308 Segment B: Worker",
                    "source_object_ids": [shared_commit],
                    "anchor_scope": {
                        "file_paths": ["src/worker/b.py"],
                        "diff_hunks": ["src/worker/b.py:1-30"],
                        "content_unit_ids": [cu_b],
                    },
                },
            ],
        }
        r_split = client.post(
            f"/v1/work-events/{ev_pr}/actions/split",
            headers={"Idempotency-Key": "split-pr-008", "If-Match": '"v1"'},
            json=split_body,
        )
        assert r_split.status_code == 200
        assert r_split.headers["ETag"] == '"v2"'
        sp_json = r_split.json()
        assert sp_json["superseded_parent"]["status"] == "SUPERSEDED"
        assert sp_json["superseded_parent"]["version"] == 2
        assert len(sp_json["created_segments"]) == 2
        seg_a, seg_b = sp_json["created_segments"]
        assert seg_a["anchor_mode"] == "PR_SEGMENT"
        assert seg_b["anchor_mode"] == "PR_SEGMENT"
        assert seg_a["status"] == "CANDIDATE"
        assert seg_a["created_by"] == "USER"
        assert "SPLIT" not in seg_a["allowed_actions"]
        assert "MERGE" in seg_a["allowed_actions"]
        # Inherited CONTEXT_ONLY stays in context_relations (never in evidence_memberships!)
        assert len(seg_a["context_relations"]) == 1
        assert seg_a["context_relations"][0]["source_object_id"] == issue_obj
        assert all(m["source_object_id"] != issue_obj for m in seg_a["evidence_memberships"])
        # Pairwise CANNOT_LINK created between siblings
        assert len(sp_json["pairwise_constraints_created"]) == 1
        assert sp_json["pairwise_constraints_created"][0]["origin"] == "SPLIT"

        # Idempotent replay returns identical segments without creating duplicates
        r_split_replay = client.post(
            f"/v1/work-events/{ev_pr}/actions/split",
            headers={"Idempotency-Key": "split-pr-008", "If-Match": '"v1"'},
            json=split_body,
        )
        assert r_split_replay.status_code == 200
        assert [s["event_id"] for s in r_split_replay.json()["created_segments"]] == [
            seg_a["event_id"],
            seg_b["event_id"],
        ]

        # Merging split siblings blocked with 409 CANNOT_LINK_CONFLICT
        r_merge_sibs = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "merge-split-siblings"},
            json={
                "members": [
                    {"event_id": seg_a["event_id"], "expected_version": 1},
                    {"event_id": seg_b["event_id"], "expected_version": 1},
                ],
                "title": "Forbidden Re-Merge",
            },
        )
        assert r_merge_sibs.status_code == 409
        assert r_merge_sibs.json()["error"]["code"] == "CANNOT_LINK_CONFLICT"

        # Valid unsplit DIRECT_COMMIT split -> retains anchor_mode=DIRECT_COMMIT with non-empty anchor_scope
        d_dc_before = client.get(f"/v1/work-events/{ev_dc}").json()
        assert "SPLIT" in d_dc_before["allowed_actions"]

        r_dc_split = client.post(
            f"/v1/work-events/{ev_dc}/actions/split",
            headers={"Idempotency-Key": "split-dc-1", "If-Match": '"v1"'},
            json={
                "expected_version": 1,
                "partitions": [
                    {"title": "DC Scope 1", "anchor_scope": {"file_paths": ["src/hotfix/a.py"]}},
                    {"title": "DC Scope 2", "anchor_scope": {"file_paths": ["src/hotfix/b.py"]}},
                ],
            },
        )
        assert r_dc_split.status_code == 200
        dc_seg_1, dc_seg_2 = r_dc_split.json()["created_segments"]
        assert dc_seg_1["anchor_mode"] == "DIRECT_COMMIT"
        assert dc_seg_2["anchor_mode"] == "DIRECT_COMMIT"
        assert dc_seg_1["direct_commit_badge"]["abbreviated_sha"] == "beefdead7777"
        assert "SPLIT" not in dc_seg_1["allowed_actions"]
        assert "MERGE" in dc_seg_1["allowed_actions"]

        # Re-splitting already scoped DIRECT_COMMIT rejected with 409 SPLIT_NOT_ALLOWED
        r_dc_resplit = client.post(
            f"/v1/work-events/{dc_seg_1['event_id']}/actions/split",
            headers={"Idempotency-Key": "resplit-dc-1", "If-Match": '"v1"'},
            json={
                "expected_version": 1,
                "partitions": [
                    {"title": "Sub 1", "anchor_scope": {"file_paths": ["src/hotfix/a1.py"]}},
                    {"title": "Sub 2", "anchor_scope": {"file_paths": ["src/hotfix/a2.py"]}},
                ],
            },
        )
        assert r_dc_resplit.status_code == 409
        assert r_dc_resplit.json()["error"]["code"] == "SPLIT_NOT_ALLOWED"


# =============================================================================
# 3. MERGE & DISSOLVE (T-304B, EV-009..016, CAND-EV-021, EVENT-11, EVENT-12)
# =============================================================================
def test_merge_guards_collapse_non_inheritance_and_dissolve_status_reevaluation(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed_a1 = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/catalog-api")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/other-ws-repo")
    access_a = _access_for(seed_a1)
    access_b = _access_for(seed_b)
    ws_a, res_a1 = seed_a1["workspace_id"], seed_a1["source_resource_id"]
    ws_b, res_b = seed_b["workspace_id"], seed_b["source_resource_id"]

    # Second repository in same workspace A (for EV-016 cross-repository check)
    res_a2 = str(uuid.uuid4())
    with pg18_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_resources (
                        id, workspace_id, source_account_id, provider_resource_id,
                        resource_type, name, canonical_url, visibility,
                        processing_mode, raw_storage_mode, status, policy_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'gh-repo-second',
                        'GITHUB_REPOSITORY', 'acme-syn-lab/billing-api',
                        'https://github.com/acme-syn-lab/billing-api', 'PUBLIC',
                        'PUBLIC_CLOUD', 'NONE', 'ACTIVE', 'v1'
                    )
                    """,
                    (res_a2, ws_a, seed_a1["source_account_id"]),
                )

    pr_m1, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.PR, provider_object_id="PR#401")
    dc_m2, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.COMMIT, provider_object_id="dc402aaaaaaa", pr_association_state=PrAssociationState.NO_MATCH_OBSERVED)
    pr_m3, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.PR, provider_object_id="PR#403")
    pr_rej, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.PR, provider_object_id="PR#404")
    pr_other_repo, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a2, object_type=SourceObjectType.PR, provider_object_id="PR#501")
    issue_so, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.ISSUE, provider_object_id="ISSUE#405")
    unknown_commit, _ = _insert_source_object(pg18_cluster, workspace_id=ws_a, source_resource_id=res_a1, object_type=SourceObjectType.COMMIT, provider_object_id="unk999999999", pr_association_state=PrAssociationState.UNKNOWN)
    pr_ws_b, _ = _insert_source_object(pg18_cluster, workspace_id=ws_b, source_resource_id=res_b, object_type=SourceObjectType.PR, provider_object_id="PR#801")

    ev_m1 = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="Catalog Alpha Query", primary_anchor_source_object_id=pr_m1, status=WorkEventStatus.CONFIRMED)
    ev_m2 = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT, title="Catalog Beta Hotfix", primary_anchor_source_object_id=dc_m2, status=WorkEventStatus.CANDIDATE)
    ev_m3 = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="Catalog Gamma Indexing", primary_anchor_source_object_id=pr_m3, status=WorkEventStatus.CONFIRMED)
    ev_rej = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="Rejected Event", primary_anchor_source_object_id=pr_rej, status=WorkEventStatus.REJECTED)
    ev_other_repo = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="Other Repo Event", primary_anchor_source_object_id=pr_other_repo, status=WorkEventStatus.CANDIDATE)
    ev_ws_b = _insert_work_event_with_v1(pg18_cluster, access_b, anchor_mode=WorkEventAnchorMode.PR, title="Other Workspace Event", primary_anchor_source_object_id=pr_ws_b, status=WorkEventStatus.CANDIDATE)

    # Attach Fact & Claim to member ev_m1 (must become STALE and NEVER be inherited by composite!)
    m1_fact, m1_claim = _insert_fact_and_claim(pg18_cluster, workspace_id=ws_a, event_id=ev_m1)

    with _make_client(pg18_cluster, [access_a]) as client:
        # Error matrix checks (0 side effects)
        # Duplicate member -> 409 DUPLICATE_COMPOSITE_MEMBER
        r_dup = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-dup"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": ev_m1, "expected_version": 1}], "title": "Dup"},
        )
        assert r_dup.status_code == 409
        assert r_dup.json()["error"]["code"] == "DUPLICATE_COMPOSITE_MEMBER"

        # EV-013: ISSUE source_object -> 422 NON_EVENT_MEMBER_NOT_ALLOWED
        r_issue = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-issue"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": issue_so, "expected_version": 1}], "title": "With Issue"},
        )
        assert r_issue.status_code == 422
        assert r_issue.json()["error"]["code"] == "NON_EVENT_MEMBER_NOT_ALLOWED"

        # EV-013: unpromoted UNKNOWN commit -> 409 PR_ASSOCIATION_INCOMPLETE
        r_unk = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-unk"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": unknown_commit, "expected_version": 1}], "title": "With Unknown Commit"},
        )
        assert r_unk.status_code == 409
        assert r_unk.json()["error"]["code"] == "PR_ASSOCIATION_INCOMPLETE"

        # EV-016: cross-workspace member -> HTTP 404 concealment; domain service returns CROSS_WORKSPACE_MEMBER
        r_cross_ws = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-cross-ws"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": ev_ws_b, "expected_version": 1}], "title": "Cross WS"},
        )
        assert r_cross_ws.status_code == 404
        assert r_cross_ws.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "details" not in r_cross_ws.json()["error"]

        mut_svc = PostgresWorkEventMutationService(api_connection_factory=pg18_cluster.api_connection)
        with pytest.raises(WorkEventError) as exc_cross_ws:
            mut_svc.merge_work_events(
                access_a,
                request=MergeWorkEventsRequest(
                    members=[
                        MergeMemberInput(event_id=ev_m1, expected_version=1),
                        MergeMemberInput(event_id=ev_ws_b, expected_version=1),
                    ],
                    title="Cross WS Domain",
                    idempotency_key="m-cross-ws-domain",
                ),
                conceal_missing_as_not_found=False,
            )
        assert exc_cross_ws.value.code == "CROSS_WORKSPACE_MEMBER"

        # EV-016: cross-repository member -> 409 CROSS_REPOSITORY_MEMBER
        r_cross_repo = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-cross-repo"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": ev_other_repo, "expected_version": 1}], "title": "Cross Repo"},
        )
        assert r_cross_repo.status_code == 409
        assert r_cross_repo.json()["error"]["code"] == "CROSS_REPOSITORY_MEMBER"

        # EV-014: REJECTED member -> 409 MEMBER_NOT_ACTIVE
        r_rej_mem = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-rej-mem"},
            json={"members": [{"event_id": ev_m1, "expected_version": 1}, {"event_id": ev_rej, "expected_version": 1}], "title": "With Rejected"},
        )
        assert r_rej_mem.status_code == 409
        assert r_rej_mem.json()["error"]["code"] == "MEMBER_NOT_ACTIVE"

        # Valid 3-member MERGE (EV-009 / EV-010) using composite_title alias
        merge_payload = {
            "members": [
                {"event_id": ev_m1, "expected_version": 1},
                {"event_id": ev_m2, "expected_version": 1},
                {"event_id": ev_m3, "expected_version": 1},
            ],
            "composite_title": "Catalog Search Overhaul Composite",
            "reason": "Unified multi-PR search rollout",
        }
        r_merge = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "cmd-merge-009"},
            json=merge_payload,
        )
        assert r_merge.status_code == 200
        assert r_merge.headers["ETag"] == '"v1"'
        m_json = r_merge.json()
        comp_id = m_json["composite_event"]["event_id"]
        assert m_json["composite_event"]["anchor_mode"] == "USER_COMPOSITE"
        assert m_json["composite_event"]["primary_anchor_source_object_id"] is None
        assert m_json["composite_event"]["evidence_memberships"] == []  # ZERO MUST_LINK rows on composite!
        assert m_json["composite_event"]["downstream_impact"]["fact_count"] == 0  # NO inheritance!
        assert m_json["composite_event"]["downstream_impact"]["claim_count"] == 0  # NO inheritance!
        assert m_json["superseded_member_ids"] == [ev_m1, ev_m2, ev_m3]
        assert m_json["invalidated_downstream_count"] == 2
        assert m_json["affected_downstream_impact"] == {"fact_count": 1, "claim_count": 1, "export_count": 0}
        assert m_json["regeneration_job_status"] == "PENDING"

        # Verify member facts/claims became STALE and outbox event was written atomically
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.facts WHERE id = %s::uuid", (m1_fact,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute("SELECT status FROM public.claims WHERE id = %s::uuid", (m1_claim,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute(
                    "SELECT COUNT(*) FROM public.outbox_events WHERE workspace_id = %s::uuid AND event_type = 'WORK_EVENT_COMPOSITE_MERGED' AND aggregate_id = %s::uuid",
                    (ws_a, comp_id),
                )
                assert cur.fetchone()[0] == 1

        # Idempotent MERGE replay returns exact same composite without duplicating
        r_merge_replay = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "cmd-merge-009"},
            json=merge_payload,
        )
        assert r_merge_replay.status_code == 200
        assert r_merge_replay.json()["composite_event"]["event_id"] == comp_id

        # EV-014: Nested composite -> 409 NESTED_COMPOSITE_NOT_ALLOWED; already composed -> 409 MEMBER_ALREADY_COMPOSED
        ev_extra = _insert_work_event_with_v1(pg18_cluster, access_a, anchor_mode=WorkEventAnchorMode.PR, title="Extra Event", primary_anchor_source_object_id=pr_m1)
        r_nested = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-nested"},
            json={"members": [{"event_id": comp_id, "expected_version": 1}, {"event_id": ev_extra, "expected_version": 1}], "title": "Nested"},
        )
        assert r_nested.status_code == 409
        assert r_nested.json()["error"]["code"] == "NESTED_COMPOSITE_NOT_ALLOWED"

        r_composed = client.post(
            "/v1/work-events/actions/merge",
            headers={"Idempotency-Key": "m-composed"},
            json={"members": [{"event_id": ev_m1, "expected_version": 2}, {"event_id": ev_extra, "expected_version": 1}], "title": "Already Composed"},
        )
        assert r_composed.status_code == 409
        assert r_composed.json()["error"]["code"] == "MEMBER_ALREADY_COMPOSED"

        # REJECT on USER_COMPOSITE advises dissolve (409)
        r_rej_comp = client.post(
            f"/v1/work-events/{comp_id}/actions/reject",
            headers={"Idempotency-Key": "rej-comp", "If-Match": '"v1"'},
            json={"expected_version": 1},
        )
        assert r_rej_comp.status_code == 409
        assert r_rej_comp.json()["error"]["code"] == "USER_COMPOSITE_REJECT_REQUIRES_DISSOLVE"

        # EV-009 / EV-010: Single logical Event collapse across search & list (0 duplicates!)
        with pg18_cluster.api_connection() as conn:
            with workspace_scoped_transaction(conn, access_a) as cur:
                assert collapse_event_ids_for_search_and_export(
                    cur,
                    workspace_id=ws_a,
                    candidate_event_ids=[ev_m1, ev_m2, comp_id, ev_m3, ev_rej],
                ) == [comp_id]
                # Searching "Catalog" hits all 3 members + composite -> collapsed strictly to [comp_id]!
                assert query_searchable_top_level_work_events(
                    cur, workspace_id=ws_a, query_text="Catalog", source_resource_id=res_a1
                ) == [comp_id]

        # Prepare DISSOLVE (EV-012 / EVENT-12):
        # - Attach composite-owned Fact & Claim (must become STALE on dissolve)
        # - Mark ev_m3's source_object as ORPHANED so ev_m3 MUST restore as STALE (not CONFIRMED!)
        comp_fact, comp_claim = _insert_fact_and_claim(pg18_cluster, workspace_id=ws_a, event_id=comp_id)
        with pg18_cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.source_objects SET lifecycle_state = 'ORPHANED' WHERE id = %s::uuid",
                        (pr_m3,),
                    )

        r_dissolve = client.post(
            f"/v1/work-events/{comp_id}/actions/dissolve",
            headers={"Idempotency-Key": "cmd-dissolve-012", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Split back into independent PRs"},
        )
        assert r_dissolve.status_code == 200
        assert r_dissolve.headers["ETag"] == '"v2"'
        d_json = r_dissolve.json()
        assert d_json["dissolved_composite"]["status"] == "SUPERSEDED"
        assert d_json["superseded_composite"]["event_id"] == comp_id
        assert d_json["invalidated_downstream_count"] == 2
        assert d_json["stale_downstream_impact"] == {
            "stale_fact_count": 1,
            "stale_claim_count": 1,
            "stale_export_count": 0,
        }
        assert d_json["cleanup_job_status"] == "PENDING"

        restored_by_id = {m["event_id"]: m for m in d_json["restored_members"]}
        # M1 was CONFIRMED + valid source -> CONFIRMED
        assert restored_by_id[ev_m1]["status"] == "CONFIRMED"
        assert restored_by_id[ev_m1]["version"] == 3
        # M2 was CANDIDATE + valid source -> CANDIDATE
        assert restored_by_id[ev_m2]["status"] == "CANDIDATE"
        assert restored_by_id[ev_m2]["version"] == 3
        # M3 was CONFIRMED before merge, but source_object is ORPHANED -> STALE!
        assert restored_by_id[ev_m3]["status"] == "STALE"
        assert restored_by_id[ev_m3]["version"] == 3

        # Verify composite fact/claim STALE & outbox cleanup recorded
        with pg18_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.facts WHERE id = %s::uuid", (comp_fact,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute("SELECT status FROM public.claims WHERE id = %s::uuid", (comp_claim,))
                assert cur.fetchone()[0] == "STALE"
                cur.execute(
                    "SELECT COUNT(*) FROM public.outbox_events WHERE workspace_id = %s::uuid AND event_type = 'WORK_EVENT_COMPOSITE_DISSOLVED' AND aggregate_id = %s::uuid",
                    (ws_a, comp_id),
                )
                assert cur.fetchone()[0] == 1

        # EV-012: Search collapse excludes dissolved SUPERSEDED composite and returns restored members
        with pg18_cluster.api_connection() as conn:
            with workspace_scoped_transaction(conn, access_a) as cur:
                collapsed_after = collapse_event_ids_for_search_and_export(
                    cur, workspace_id=ws_a, candidate_event_ids=[comp_id, ev_m1, ev_m2, ev_m3]
                )
                assert comp_id not in collapsed_after
                assert collapsed_after == [ev_m1, ev_m2, ev_m3]
                search_after = query_searchable_top_level_work_events(
                    cur, workspace_id=ws_a, query_text="Catalog", source_resource_id=res_a1
                )
                assert comp_id not in search_after
                assert set(search_after) == {ev_m1, ev_m2, ev_m3}

        # Idempotent DISSOLVE replay returns identical response without bumping versions
        r_dissolve_replay = client.post(
            f"/v1/work-events/{comp_id}/actions/dissolve",
            headers={"Idempotency-Key": "cmd-dissolve-012", "If-Match": '"v1"'},
            json={"expected_version": 1, "reason": "Split back into independent PRs"},
        )
        assert r_dissolve_replay.status_code == 200
        assert r_dissolve_replay.json()["dissolved_composite"]["version"] == 2


# =============================================================================
# 4. EVENT-09 Real PG18 Multi-Connection threading.Barrier Concurrency Contention
# =============================================================================
def test_event_09_concurrent_split_edit_reject_and_cannot_link_merge_races(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/concurrency-lab")
    access = _access_for(seed)
    ws_id, res_id = seed["workspace_id"], seed["source_resource_id"]

    pr_split, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#901")
    pr_edit_rej, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#902")
    pr_m_a, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#903")
    pr_m_b, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#904")

    ev_split = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Race Split PR", primary_anchor_source_object_id=pr_split)
    ev_er = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Race Edit/Reject PR", primary_anchor_source_object_id=pr_edit_rej)
    ev_a = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Race Merge A", primary_anchor_source_object_id=pr_m_a)
    ev_b = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Race Merge B", primary_anchor_source_object_id=pr_m_b)

    cluster_factory = pg18_cluster.api_connection
    svc = PostgresWorkEventMutationService(api_connection_factory=cluster_factory)

    # --- Race 1: Concurrent SPLIT vs SPLIT on same PR v1 ---
    barrier_split = threading.Barrier(2)
    split_outcomes: list[tuple[str, Any]] = []

    def _run_split(worker_tag: str) -> None:
        barrier_split.wait(timeout=10)
        try:
            res = svc.split_work_event(
                access,
                event_id=ev_split,
                request=SplitWorkEventRequest(
                    expected_version=1,
                    idempotency_key=f"race-split-{worker_tag}",
                    partitions=[
                        SplitPartitionInput(title=f"{worker_tag}-A", anchor_scope=SplitAnchorScopeInput(file_paths=["src/a.py"])),
                        SplitPartitionInput(title=f"{worker_tag}-B", anchor_scope=SplitAnchorScopeInput(file_paths=["src/b.py"])),
                    ],
                ),
            )
            split_outcomes.append(("ok", res))
        except WorkEventError as exc:
            split_outcomes.append(("err", exc))

    t1 = threading.Thread(target=_run_split, args=("w1",))
    t2 = threading.Thread(target=_run_split, args=("w2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(split_outcomes) == 2
    ok_splits = [val for kind, val in split_outcomes if kind == "ok"]
    err_splits = [val for kind, val in split_outcomes if kind == "err"]
    assert len(ok_splits) == 1
    assert len(err_splits) == 1
    assert err_splits[0].status_code == 409
    assert err_splits[0].code == "EVENT_VERSION_CONFLICT"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events WHERE workspace_id = %s::uuid AND anchor_mode = 'PR_SEGMENT'", (ws_id,))
            assert cur.fetchone()[0] == 2  # Exactly 2 segments from winner, 0 from loser

    # --- Race 2: Concurrent EDIT vs REJECT on same Event v1 ---
    barrier_er = threading.Barrier(2)
    er_outcomes: list[tuple[str, Any]] = []

    def _run_edit() -> None:
        barrier_er.wait(timeout=10)
        try:
            res = svc.edit_work_event(
                access,
                event_id=ev_er,
                request=EditWorkEventRequest(expected_version=1, title="Concurrent Edit Title", idempotency_key="race-edit-1"),
            )
            er_outcomes.append(("ok", res))
        except WorkEventError as exc:
            er_outcomes.append(("err", exc))

    def _run_reject() -> None:
        barrier_er.wait(timeout=10)
        try:
            res = svc.reject_work_event(
                access,
                event_id=ev_er,
                request=RejectWorkEventRequest(expected_version=1, reason="Concurrent Reject", idempotency_key="race-reject-1"),
            )
            er_outcomes.append(("ok", res))
        except WorkEventError as exc:
            er_outcomes.append(("err", exc))

    te = threading.Thread(target=_run_edit)
    tr = threading.Thread(target=_run_reject)
    te.start()
    tr.start()
    te.join(timeout=15)
    tr.join(timeout=15)

    assert len(er_outcomes) == 2
    assert sum(1 for kind, _ in er_outcomes if kind == "ok") == 1
    er_errs = [val for kind, val in er_outcomes if kind == "err"]
    assert len(er_errs) == 1
    assert er_errs[0].status_code == 409
    assert er_errs[0].code == "EVENT_VERSION_CONFLICT"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.review_actions WHERE workspace_id = %s::uuid AND target_id = %s::uuid", (ws_id, ev_er))
            assert cur.fetchone()[0] == 1

    # --- Race 3: Delayed MERGE racing with a newly committed CANNOT_LINK ---
    # Tx1 locks (ev_a, ev_b) in UUID ASC order, inserts CANNOT_LINK, signals Tx2 to enter MERGE
    # (blocking on PG FOR UPDATE queue), then Tx1 commits once Tx2 is waiting on the lock.
    lock_acquired = threading.Event()
    merge_started = threading.Event()
    merge_outcomes: list[tuple[str, Any]] = []

    def _run_cannot_link_writer() -> None:
        with cluster_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                cur.execute(
                    """
                    SELECT id FROM public.work_events
                    WHERE workspace_id = %s::uuid AND id = ANY(%s::uuid[])
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (ws_id, sorted([ev_a, ev_b])),
                )
                record_pairwise_cannot_link(
                    cur,
                    workspace_id=ws_id,
                    event_a_id=ev_a,
                    event_b_id=ev_b,
                    origin=EventConstraintOrigin.USER,
                    rationale_code="USER_EXPLICIT_SEPARATE",
                )
                lock_acquired.set()
                assert merge_started.wait(timeout=10)
                # Poll pg_locks deterministically until Tx2 is queued waiting on row lock (no sleep!)
                with pg18_cluster.admin_connection(autocommit=True) as probe_conn:
                    with probe_conn.cursor() as probe_cur:
                        while True:
                            probe_cur.execute(
                                "SELECT COUNT(*) FROM pg_locks WHERE granted = false"
                            )
                            if int(probe_cur.fetchone()[0]) >= 1:
                                break

    def _run_delayed_merge() -> None:
        assert lock_acquired.wait(timeout=10)
        merge_started.set()
        try:
            res = svc.merge_work_events(
                access,
                request=MergeWorkEventsRequest(
                    members=[
                        MergeMemberInput(event_id=ev_a, expected_version=1),
                        MergeMemberInput(event_id=ev_b, expected_version=1),
                    ],
                    title="Delayed Merge Blocked by Fresh CANNOT_LINK",
                    idempotency_key="race-merge-cannot-link",
                ),
            )
            merge_outcomes.append(("ok", res))
        except WorkEventError as exc:
            merge_outcomes.append(("err", exc))

    t_cl = threading.Thread(target=_run_cannot_link_writer)
    t_mg = threading.Thread(target=_run_delayed_merge)
    t_cl.start()
    t_mg.start()
    t_cl.join(timeout=15)
    t_mg.join(timeout=15)

    assert len(merge_outcomes) == 1
    assert merge_outcomes[0][0] == "err"
    assert merge_outcomes[0][1].status_code == 409
    assert merge_outcomes[0][1].code == "CANNOT_LINK_CONFLICT"
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events WHERE workspace_id = %s::uuid AND anchor_mode = 'USER_COMPOSITE'", (ws_id,))
            assert cur.fetchone()[0] == 0


# =============================================================================
# 5. Mid-Transaction Failure Injection (before_commit_hook) & Retry Convergence
# =============================================================================
def test_before_commit_hook_crash_rolls_back_all_tables_and_idempotency_retry_converges(
    pg18_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme-syn-lab/rollback-lab")
    access = _access_for(seed)
    ws_id, res_id = seed["workspace_id"], seed["source_resource_id"]

    pr_1, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#701")
    pr_2, _ = _insert_source_object(pg18_cluster, workspace_id=ws_id, source_resource_id=res_id, object_type=SourceObjectType.PR, provider_object_id="PR#702")

    ev_1 = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Rollback Merge 1", primary_anchor_source_object_id=pr_1)
    ev_2 = _insert_work_event_with_v1(pg18_cluster, access, anchor_mode=WorkEventAnchorMode.PR, title="Rollback Merge 2", primary_anchor_source_object_id=pr_2)
    _insert_fact_and_claim(pg18_cluster, workspace_id=ws_id, event_id=ev_1)

    should_crash = [True]

    def _inject_crash(action_name: str, _cur: Any) -> None:
        if should_crash[0]:
            raise RuntimeError(f"Injected crash before commit for {action_name}")

    svc = PostgresWorkEventMutationService(
        api_connection_factory=pg18_cluster.api_connection,
        before_commit_hook=_inject_crash,
    )

    merge_req = MergeWorkEventsRequest(
        members=[
            MergeMemberInput(event_id=ev_1, expected_version=1),
            MergeMemberInput(event_id=ev_2, expected_version=1),
        ],
        title="Atomic Rollback Composite",
        reason="Test crash rollback",
        idempotency_key="idem-crash-retry-merge",
    )

    with pytest.raises(RuntimeError, match="Injected crash before commit"):
        svc.merge_work_events(access, request=merge_req)

    # Verify 100% rollback across ALL tables
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events WHERE workspace_id = %s::uuid AND anchor_mode = 'USER_COMPOSITE'", (ws_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT status, version FROM public.work_events WHERE id = %s::uuid", (ev_1,))
            assert cur.fetchone() == ("CANDIDATE", 1)
            cur.execute("SELECT COUNT(*) FROM public.review_actions WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.event_composite_memberships WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.audit_events WHERE workspace_id = %s::uuid", (ws_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT status FROM public.facts WHERE event_id = %s::uuid", (ev_1,))
            assert cur.fetchone()[0] == "SUPPORTED"

    # Retry with the EXACT SAME Idempotency-Key now converges cleanly
    should_crash[0] = False
    merged = svc.merge_work_events(access, request=merge_req)
    assert merged.composite_event.anchor_mode is WorkEventAnchorMode.USER_COMPOSITE
    assert merged.composite_event.version == 1
    assert merged.superseded_member_ids == [ev_1, ev_2]

    # Second retry with same key replays idempotently
    replayed = svc.merge_work_events(access, request=merge_req)
    assert replayed.composite_event.event_id == merged.composite_event.event_id
    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events WHERE workspace_id = %s::uuid AND anchor_mode = 'USER_COMPOSITE'", (ws_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE workspace_id = %s::uuid AND event_type = 'WORK_EVENT_COMPOSITE_MERGED'", (ws_id,))
            assert cur.fetchone()[0] == 1
