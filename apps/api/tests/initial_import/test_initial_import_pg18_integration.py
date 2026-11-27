"""Real PostgreSQL 18 (0010~0050) integration tests for M2-T206 Initial Backfill Orchestrator.

Verifies:
1. Multi-page clean run -> SUCCESS + 1 IMPORT_READY + PR & 1:1 DIRECT_COMMIT work_events +
   derived content_units (text_content=NULL, content_ref=NULL) + zero raw text canary leakage.
2. T-206 mid-page transient failures (503 / 429) -> PARTIAL (retry_allowed=True) ->
   POST /v1/sync-runs/{id}/retry (expected_run_version) resume -> 100% identical logical PG18
   snapshot as clean run + 0 duplicate page fetches + 0 duplicate rows + identical FrozenImportPlan.
3. Post-SUCCESS 409 rejections on both POST /v1/sync-runs/{id}/retry and new POST /v1/sync-runs.
4. Fail-closed non-promotion (work_events=0, IMPORT_READY=0, content_units=0, retry_allowed=False)
   on PLAN_DRIFT, PARTIAL_WITH_LIMIT, UNKNOWN PR association, CURSOR_CYCLE_DETECTED,
   PUBLIC->PRIVATE visibility flip, 401 AUTH_REVOKED, and deletion fence.
5. Production BackfillLifecycleHookPort boundary ordering & post-commit crash recovery.
6. Post-IMPORT_READY DerivedUnitIngestionProcessor boundary + BLOCKED_SOURCE (hash mismatch)
   & BLOCKED_POLICY (visibility flip) projection preserving historical sync_runs.status='SUCCESS'.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.oauth import InMemorySecretStore
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.backfill_engine.models import BackfillStreamName
from career_memory_api.github_connect import (
    GitHubConnectError,
    PolicyGuardedOperation,
    PostgresGitHubConnectService,
    create_github_connect_router,
)
from career_memory_api.github_provider import (
    GitHubResolverContentFetcher,
    HttpGitHubImportClient,
    InMemoryWireTransport,
    SafeGitHubHttpTransport,
)
from career_memory_api.initial_import import (
    DerivedUnitIngestionProcessor,
    FinalizeHookContext,
    InitialBackfillJobWorker,
    InitialBackfillOrchestrator,
    PageHookContext,
)
from career_memory_api.jobs import JobStatus, PostgresJobRuntime
from career_memory_api.resolver import (
    PostgresProvenanceMetadataStore,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionStatus,
)
from career_memory_api.sync_runs import (
    ProcessingStatus,
    PostgresSyncRunService,
    SyncRunStatus,
    create_sync_runs_router,
    install_sync_runs_error_handlers,
)
from tests.backfill_engine.pg_harness import EphemeralBackfillPg18Cluster
from tests.initial_import.fake_github_repo import (
    CANARY_COMMENT_BODY,
    CANARY_COMMIT_MSG,
    CANARY_PR_BODY,
    FixtureGitHubCredentialProvider,
    OfflineGitHubRepoFixture,
)

_FIXED_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m2-t206-pg18-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(autouse=True)
def _clean_db(pg_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg_cluster.truncate_all()


def _seed_connected_workspace_and_repo(
    cluster: EphemeralBackfillPg18Cluster,
    fixture: OfflineGitHubRepoFixture,
    secret_store: InMemorySecretStore,
) -> tuple[WorkspaceAccessContext, PostgresGitHubConnectService, str]:
    """Seed user/workspace and connect GitHub account + public repo via real M2-T201 service."""
    user_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                    VALUES (%s::uuid, 'https://auth.local/v1', %s, %s, 'M2 Orchestrator User')
                    """,
                    (user_id, f"sub-{user_id}", f"{user_id}@local.test"),
                )
                cur.execute(
                    """
                    INSERT INTO public.workspaces (id, owner_user_id, name)
                    VALUES (%s::uuid, %s::uuid, 'M2 T206 Workspace')
                    """,
                    (workspace_id, user_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                    VALUES (%s::uuid, %s::uuid, 'OWNER')
                    """,
                    (workspace_id, user_id),
                )

    access = WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer="https://auth.local/v1", subject=f"sub-{user_id}"),
        ),
        membership=WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role="OWNER",
        ),
    )

    connect_service = PostgresGitHubConnectService(
        connection_factory=cluster.api_connection,
        credential_provider=FixtureGitHubCredentialProvider(fixture),
        secret_store=secret_store,
        allowed_redirect_uris=("https://app.career-memory.local/oauth/callback",),
        clock=lambda: _FIXED_NOW,
    )
    start_res = connect_service.start_oauth(
        access,
        return_path="/settings/sources",
        redirect_uri="https://app.career-memory.local/oauth/callback",
    )
    cb_res = connect_service.complete_oauth_callback(
        access,
        idempotency_key=str(uuid.uuid4()),
        state=start_res.state,
        code="gh-auth-code-001",
        redirect_uri="https://app.career-memory.local/oauth/callback",
        repository_full_name=fixture.full_name,
    )
    assert cb_res.source_resource is not None
    return access, connect_service, cb_res.source_resource.id


def _build_stack(
    cluster: EphemeralBackfillPg18Cluster,
    fixture: OfflineGitHubRepoFixture,
    *,
    lifecycle_hook: Any = None,
) -> dict[str, Any]:
    secret_store = InMemorySecretStore()
    access, connect_service, resource_id = _seed_connected_workspace_and_repo(
        cluster, fixture, secret_store
    )
    wire = InMemoryWireTransport(fixture.handle_wire_request)
    transport = SafeGitHubHttpTransport(
        wire_transport=wire,
        secret_store=secret_store,
        clock=lambda: _FIXED_NOW,
    )
    github_client = HttpGitHubImportClient(transport=transport, clock=lambda: _FIXED_NOW)
    content_fetcher = GitHubResolverContentFetcher(transport=transport, require_credentials=True)
    sync_runs_service = PostgresSyncRunService(
        connection_factory=cluster.api_connection,
        worker_connection_factory=cluster.worker_connection,
        clock=lambda: _FIXED_NOW,
    )
    job_runtime = PostgresJobRuntime(connection_factory=cluster.worker_connection)
    orchestrator = InitialBackfillOrchestrator(
        worker_connection_factory=cluster.worker_connection,
        github_client=github_client,
        github_connect_service=connect_service,
        lifecycle_hook=lifecycle_hook,
        clock=lambda: _FIXED_NOW,
    )
    derived_processor = DerivedUnitIngestionProcessor(
        worker_connection_factory=cluster.worker_connection,
        content_fetcher=content_fetcher,
        clock=lambda: _FIXED_NOW,
    )
    worker = InitialBackfillJobWorker(
        job_runtime=job_runtime,
        orchestrator=orchestrator,
        derived_unit_processor=derived_processor,
        clock=lambda: _FIXED_NOW,
    )
    app = create_app(
        sync_runs_router=create_sync_runs_router(
            service=sync_runs_service, resolve_access=lambda _req: access
        ),
        github_connect_router=create_github_connect_router(
            service=connect_service, resolve_access=lambda _req: access
        ),
        install_github_connect_handlers=True,
    )
    install_sync_runs_error_handlers(app)
    return {
        "access": access,
        "secret_store": secret_store,
        "connect_service": connect_service,
        "resource_id": resource_id,
        "transport": transport,
        "github_client": github_client,
        "content_fetcher": content_fetcher,
        "sync_runs_service": sync_runs_service,
        "job_runtime": job_runtime,
        "orchestrator": orchestrator,
        "derived_processor": derived_processor,
        "worker": worker,
        "app": app,
    }


def _extract_logical_db_snapshot(
    cluster: EphemeralBackfillPg18Cluster, workspace_id: str, sync_run_id: str
) -> dict[str, Any]:
    """Capture a canonical logical snapshot of all M2 backfill + derived tables (ignoring random UUIDs/timestamps)."""
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, processing_status, import_ready, retry_allowed,
                       has_safe_checkpoint, source_drift, failure_reason_code,
                       processed_range, remaining_range,
                       plan_spec - 'plan_id' - 'source_resource_id'
                FROM public.sync_runs
                WHERE workspace_id = %s::uuid AND id = %s::uuid
                """,
                (workspace_id, sync_run_id),
            )
            run_row = cur.fetchone()
            assert run_row is not None

            cur.execute(
                """
                SELECT object_type, provider_object_id, lifecycle_state, pr_association_state
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                ORDER BY object_type, provider_object_id
                """,
                (workspace_id,),
            )
            objects = cur.fetchall()

            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, rev.content_hash,
                       rev.hash_policy_version, rev.is_tombstone, rev.normalized_metadata
                FROM public.source_object_revisions rev
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE rev.workspace_id = %s::uuid
                ORDER BY so.object_type, so.provider_object_id, rev.content_hash
                """,
                (workspace_id,),
            )
            revisions = cur.fetchall()

            cur.execute(
                """
                SELECT stream_name, stream_status, last_committed_page, unresolved_failures
                FROM public.sync_checkpoints
                WHERE workspace_id = %s::uuid AND sync_run_id = %s::uuid
                ORDER BY stream_name
                """,
                (workspace_id, sync_run_id),
            )
            checkpoints = cur.fetchall()

            cur.execute(
                """
                SELECT stream_name, page_number, request_cursor, next_cursor,
                       page_fingerprint, item_count, unique_object_count
                FROM public.sync_run_page_observations
                WHERE workspace_id = %s::uuid AND sync_run_id = %s::uuid
                ORDER BY stream_name, page_number
                """,
                (workspace_id, sync_run_id),
            )
            page_obs = cur.fetchall()

            cur.execute(
                """
                SELECT commit_sha, default_branch_name, pr_association_state,
                       matched_pr_provider_id, matched_pr_in_window, match_mechanism,
                       unknown_reason_code, reachable_at_start, reachable_at_end,
                       lookup_completed, observation_metadata
                FROM public.sync_commit_pr_associations
                WHERE workspace_id = %s::uuid AND sync_run_id = %s::uuid
                ORDER BY commit_sha
                """,
                (workspace_id, sync_run_id),
            )
            associations = cur.fetchall()

            cur.execute(
                """
                SELECT stream_name, object_type, provider_object_id,
                       outcome_kind, reason_code, affected_range
                FROM public.sync_item_outcomes
                WHERE workspace_id = %s::uuid AND sync_run_id = %s::uuid
                ORDER BY stream_name, object_type, provider_object_id, outcome_kind, reason_code
                """,
                (workspace_id, sync_run_id),
            )
            outcomes = cur.fetchall()

            cur.execute(
                """
                SELECT we.anchor_mode, so.object_type, so.provider_object_id,
                       we.title, we.status, we.created_by, we.summary_metadata
                FROM public.work_events we
                JOIN public.source_objects so ON so.id = we.primary_anchor_source_object_id
                WHERE we.workspace_id = %s::uuid
                ORDER BY we.anchor_mode, so.provider_object_id
                """,
                (workspace_id,),
            )
            work_events = cur.fetchall()

            cur.execute(
                """
                SELECT primary_so.provider_object_id, member_so.object_type,
                       member_so.provider_object_id, em.relation_class,
                       em.relation_type, em.origin, em.rationale_code
                FROM public.event_memberships em
                JOIN public.work_events we ON we.id = em.event_id
                JOIN public.source_objects primary_so ON primary_so.id = we.primary_anchor_source_object_id
                JOIN public.source_objects member_so ON member_so.id = em.source_object_id
                WHERE em.workspace_id = %s::uuid
                ORDER BY 1, 2, 3, 5
                """,
                (workspace_id,),
            )
            memberships = cur.fetchall()

            cur.execute(
                """
                SELECT event_type, aggregate_type, status
                FROM public.outbox_events
                WHERE workspace_id = %s::uuid AND event_type = 'IMPORT_READY'
                """,
                (workspace_id,),
            )
            import_ready_outbox = cur.fetchall()

            cur.execute(
                """
                SELECT so.object_type, so.provider_object_id, cu.unit_type,
                       cu.ordinal, cu.content_hash, cu.retention_class,
                       cu.text_content, cu.content_ref, cu.policy_state
                FROM public.content_units cu
                JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                JOIN public.source_objects so ON so.id = rev.source_object_id
                WHERE cu.workspace_id = %s::uuid
                ORDER BY so.object_type, so.provider_object_id, cu.ordinal
                """,
                (workspace_id,),
            )
            content_units = cur.fetchall()

    return {
        "sync_run": run_row,
        "source_objects": objects,
        "source_object_revisions": revisions,
        "sync_checkpoints": checkpoints,
        "sync_run_page_observations": page_obs,
        "sync_commit_pr_associations": associations,
        "sync_item_outcomes": outcomes,
        "work_events": work_events,
        "event_memberships": memberships,
        "import_ready_outbox": import_ready_outbox,
        "content_units": content_units,
    }


def _assert_zero_canary_raw_text_in_db(cluster: EphemeralBackfillPg18Cluster) -> None:
    """Verify Plan C invariant: zero canary raw body strings exist anywhere in persisted tables."""
    canaries = (CANARY_PR_BODY, CANARY_COMMIT_MSG, CANARY_COMMENT_BODY)
    tables_to_scan = (
        "source_objects",
        "source_object_revisions",
        "content_units",
        "work_events",
        "sync_runs",
        "sync_checkpoints",
        "sync_item_outcomes",
        "sync_commit_pr_associations",
        "sync_run_page_observations",
        "outbox_events",
        "audit_events",
        "jobs",
    )
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for tbl in tables_to_scan:
                cur.execute(f"SELECT row_to_json(t)::text FROM public.{tbl} t")
                for (row_text,) in cur.fetchall():
                    for canary in canaries:
                        assert canary not in row_text, (
                            f"Plan C raw-text canary {canary!r} leaked into public.{tbl}!"
                        )


# =============================================================================
# 1. Clean Run vs T-206 Interrupted Checkpoint Resume Equivalence + Post-SUCCESS 409
# =============================================================================
@pytest.mark.postgres
def test_clean_run_vs_interrupted_retry_resume_converge_to_identical_pg18_snapshot(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # --- A. Clean single-pass multi-page run ---
    clean_fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    clean_stack = _build_stack(pg_cluster, clean_fixture)
    clean_access: WorkspaceAccessContext = clean_stack["access"]

    with TestClient(clean_stack["app"]) as client:
        # Confirm github_connect routes are mounted on create_app cleanly
        accts_resp = client.get("/v1/source-accounts")
        assert accts_resp.status_code == 200
        assert len(accts_resp.json()["items"]) == 1

        create_resp = client.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "source_resource_id": clean_stack["resource_id"],
                "mode": "INITIAL_BACKFILL",
                "backfill_from": "2026-01-01",
                "limits": {"max_objects": 500},
            },
        )
        assert create_resp.status_code == 202
        clean_run_id = create_resp.json()["target_id"]

    step_clean = clean_stack["worker"].claim_and_execute_next(
        clean_access.workspace_id,
        worker_id="worker-clean-1",
        access=clean_access,
        run_derived_units=True,
        now=_FIXED_NOW,
    )
    assert step_clean is not None
    assert step_clean.job_final_status is JobStatus.SUCCEEDED
    assert step_clean.effect_applied is True
    assert step_clean.execution.status is SyncRunStatus.SUCCESS
    assert step_clean.execution.import_ready is True
    assert step_clean.execution.import_ready_outbox_id is not None
    assert len(step_clean.execution.promoted_pr_event_ids) == 3
    assert len(step_clean.execution.promoted_direct_commit_event_ids) == 1
    assert step_clean.derived_units is not None
    assert step_clean.derived_units.executed is True
    assert step_clean.derived_units.processing_status is ProcessingStatus.COMPLETE
    assert step_clean.derived_units.content_units_inserted >= 5

    # Verify Plan C zero raw-text persistence
    _assert_zero_canary_raw_text_in_db(pg_cluster)

    # Verify live RefetchResolverService resolves persisted PR unit back to MATCHED!
    with pg_cluster.api_connection() as api_conn:
        with pg_cluster.admin_connection() as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cu.id::text
                    FROM public.content_units cu
                    JOIN public.source_object_revisions rev ON rev.id = cu.source_object_revision_id
                    JOIN public.source_objects so ON so.id = rev.source_object_id
                    WHERE cu.workspace_id = %s::uuid
                      AND so.object_type = 'PR'
                      AND so.provider_object_id = '10'
                    ORDER BY cu.ordinal ASC
                    LIMIT 1
                    """,
                    (clean_access.workspace_id,),
                )
                pr10_unit_id = str(cur.fetchone()[0])

        resolver = RefetchResolverService(
            metadata_store=PostgresProvenanceMetadataStore(api_conn),
            fetcher=clean_stack["content_fetcher"],
        )
        with resolver.open_operation_session(clean_access, operation_id="op-verify-clean") as sess:
            resolved = resolver.resolve_unit(
                sess,
                RefetchRequestInput(
                    access=clean_access,
                    content_unit_id=pr10_unit_id,
                    operation_id="op-verify-clean",
                ),
            )
            assert resolved.metadata.status is ResolutionStatus.MATCHED
            assert CANARY_PR_BODY in resolved.ephemeral_unit.text

    # Verify post-SUCCESS 409 rejections on retry and new create
    with TestClient(clean_stack["app"]) as client:
        status_resp = client.get(f"/v1/sync-runs/{clean_run_id}")
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["status"] == "SUCCESS"
        assert status_body["import_ready"] is True
        assert status_body["processing_status"] == "COMPLETE"
        assert status_body["retry"]["allowed"] is False

        retry_after_success = client.post(
            f"/v1/sync-runs/{clean_run_id}/retry",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_run_version": status_body["run_version"]},
        )
        assert retry_after_success.status_code == 409

        recreate_after_success = client.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "source_resource_id": clean_stack["resource_id"],
                "mode": "INITIAL_BACKFILL",
            },
        )
        assert recreate_after_success.status_code == 409

    # Idempotent re-invocation of execute_sync_run on already-SUCCESS run does 0 fetches
    req_count_before_noop = len(clean_fixture.request_log)
    noop_exec = clean_stack["orchestrator"].execute_sync_run(
        workspace_id=clean_access.workspace_id,
        sync_run_id=clean_run_id,
        access=clean_access,
        now=_FIXED_NOW,
    )
    assert noop_exec.status is SyncRunStatus.SUCCESS
    assert len(clean_fixture.request_log) == req_count_before_noop

    clean_snapshot = _extract_logical_db_snapshot(
        pg_cluster, clean_access.workspace_id, clean_run_id
    )

    # --- B. Interrupted mid-page transient faults (503 on PRS page 2, 429 on COMMITS page 2) + Retry Resume ---
    pg_cluster.truncate_all()
    resumed_fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    resumed_fixture.transient_faults[f"/repositories/{resumed_fixture.repo_id}/pulls?page=2"] = [503]
    resumed_fixture.transient_faults[f"/repositories/{resumed_fixture.repo_id}/commits?page=2"] = [429]

    resumed_stack = _build_stack(pg_cluster, resumed_fixture)
    resumed_access: WorkspaceAccessContext = resumed_stack["access"]

    with TestClient(resumed_stack["app"]) as client:
        create_resp = client.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "source_resource_id": resumed_stack["resource_id"],
                "mode": "INITIAL_BACKFILL",
                "backfill_from": "2026-01-01",
                "limits": {"max_objects": 500},
            },
        )
        assert create_resp.status_code == 202
        resumed_run_id = create_resp.json()["target_id"]

    # Attempt 1: Commits PRS page 1, then fails with 503 on PRS page 2 -> PARTIAL (retry_allowed=True)
    step1 = resumed_stack["worker"].claim_and_execute_next(
        resumed_access.workspace_id,
        worker_id="worker-resume-1",
        access=resumed_access,
        run_derived_units=True,
        now=_FIXED_NOW,
    )
    assert step1 is not None
    assert step1.execution.status is SyncRunStatus.PARTIAL
    assert step1.execution.failure_reason_code == "TRANSIENT_PROVIDER_ERROR"
    assert step1.execution.retry_allowed is True
    assert step1.execution.has_safe_checkpoint is True
    assert step1.execution.import_ready is False
    assert step1.derived_units is None  # NEVER run derived units on PARTIAL!
    first_plan_fp = step1.execution.frozen_plan_fingerprint
    assert first_plan_fp is not None

    # Confirm 0 work_events, 0 IMPORT_READY outbox, 0 content_units while PARTIAL
    partial_snap = _extract_logical_db_snapshot(
        pg_cluster, resumed_access.workspace_id, resumed_run_id
    )
    assert partial_snap["work_events"] == []
    assert partial_snap["import_ready_outbox"] == []
    assert partial_snap["content_units"] == []

    # Trigger retry #1 via POST /v1/sync-runs/{id}/retry with exact expected_run_version
    with TestClient(resumed_stack["app"]) as client:
        st1 = client.get(f"/v1/sync-runs/{resumed_run_id}").json()
        assert st1["status"] == "PARTIAL"
        assert st1["retry"] == {"allowed": True, "from_safe_checkpoint": True}

        # Wrong run_version -> 409 RESOURCE_VERSION_CONFLICT
        bad_retry = client.post(
            f"/v1/sync-runs/{resumed_run_id}/retry",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_run_version": st1["run_version"] + 99},
        )
        assert bad_retry.status_code == 409
        assert bad_retry.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

        retry1 = client.post(
            f"/v1/sync-runs/{resumed_run_id}/retry",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_run_version": st1["run_version"]},
        )
        assert retry1.status_code == 202

    # Record request counts before resume #1 so we prove PRS page 1 is NOT re-requested
    prs_p1_key = f"/repositories/{resumed_fixture.repo_id}/pulls?page=1"
    prs_p1_before_resume1 = resumed_fixture.request_counts[prs_p1_key]
    assert prs_p1_before_resume1 == 1

    # Attempt 2: Resumes PRS page 2 -> completes PR_REVIEWS -> commits COMMITS page 1 -> 429 on COMMITS page 2
    step2 = resumed_stack["worker"].claim_and_execute_next(
        resumed_access.workspace_id,
        worker_id="worker-resume-2",
        access=resumed_access,
        run_derived_units=True,
        now=_FIXED_NOW,
    )
    assert step2 is not None
    assert step2.execution.resumed_from_checkpoint is True
    assert step2.execution.frozen_plan_fingerprint == first_plan_fp  # Frozen plan untouched!
    assert step2.execution.status is SyncRunStatus.PARTIAL
    assert step2.execution.retry_allowed is True
    # PRS page 1 was NEVER re-fetched during resume #1!
    assert resumed_fixture.request_counts[prs_p1_key] == 1

    commits_p1_key = f"/repositories/{resumed_fixture.repo_id}/commits?page=1"
    assert resumed_fixture.request_counts[commits_p1_key] == 1

    # Trigger retry #2
    with TestClient(resumed_stack["app"]) as client:
        st2 = client.get(f"/v1/sync-runs/{resumed_run_id}").json()
        retry2 = client.post(
            f"/v1/sync-runs/{resumed_run_id}/retry",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_run_version": st2["run_version"]},
        )
        assert retry2.status_code == 202

    # Attempt 3: Resumes COMMITS page 2 -> completes COMMIT_PR_ASSOCIATIONS & CONTEXT_ISSUES -> SUCCESS!
    step3 = resumed_stack["worker"].claim_and_execute_next(
        resumed_access.workspace_id,
        worker_id="worker-resume-3",
        access=resumed_access,
        run_derived_units=True,
        now=_FIXED_NOW,
    )
    assert step3 is not None
    assert step3.execution.resumed_from_checkpoint is True
    assert step3.execution.frozen_plan_fingerprint == first_plan_fp
    assert step3.execution.status is SyncRunStatus.SUCCESS
    assert step3.execution.import_ready is True
    assert step3.derived_units is not None
    assert step3.derived_units.processing_status is ProcessingStatus.COMPLETE

    # COMMITS page 1 was NEVER re-fetched during resume #2!
    assert resumed_fixture.request_counts[commits_p1_key] == 1

    resumed_snapshot = _extract_logical_db_snapshot(
        pg_cluster, resumed_access.workspace_id, resumed_run_id
    )

    # T-206 core convergence assertion: clean single-pass run == interrupted-then-resumed run!
    assert resumed_snapshot == clean_snapshot
    _assert_zero_canary_raw_text_in_db(pg_cluster)


# =============================================================================
# 2. Fail-Closed Non-Promotion Scenarios (PLAN_DRIFT, LIMIT, UNKNOWN, CYCLE)
# =============================================================================
@pytest.mark.postgres
def test_plan_drift_blocks_success_and_retry_with_zero_promotions(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()

    class _DriftBeforeFinalizeHook:
        def before_page_commit(self, ctx: PageHookContext) -> None:
            return None

        def after_page_commit(self, ctx: PageHookContext) -> None:
            # Move HEAD SHA after default-branch commits complete to trigger PLAN_DRIFT at completion gate
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                fixture.head_sha = "9" * 40

        def before_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

        def after_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

    stack = _build_stack(pg_cluster, fixture, lifecycle_hook=_DriftBeforeFinalizeHook())
    access: WorkspaceAccessContext = stack["access"]
    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-drift", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.status is SyncRunStatus.PARTIAL
    assert step.execution.source_drift is True
    assert step.execution.failure_reason_code == "PLAN_DRIFT"
    assert step.execution.retry_allowed is False
    assert step.execution.import_ready is False
    assert step.derived_units is None

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []
    assert snap["content_units"] == []

    # Derived unit processor MUST refuse to run on PLAN_DRIFT
    derived = stack["derived_processor"].process_import_ready_run(
        workspace_id=access.workspace_id, sync_run_id=receipt.target_id, now=_FIXED_NOW
    )
    assert derived.executed is False


@pytest.mark.postgres
def test_partial_with_limit_blocks_promotion_and_retry(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    stack = _build_stack(pg_cluster, fixture)
    access: WorkspaceAccessContext = stack["access"]

    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={
            "source_resource_id": stack["resource_id"],
            "mode": "INITIAL_BACKFILL",
            "limits": {"max_objects": 2},
        },
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-limit", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.status is SyncRunStatus.PARTIAL_WITH_LIMIT
    assert step.execution.failure_reason_code == "LIMIT_EXCEEDED"
    assert step.execution.retry_allowed is False
    assert step.execution.remaining_range
    assert step.derived_units is None

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []
    assert snap["content_units"] == []


@pytest.mark.postgres
def test_unknown_pr_association_blocks_success_and_direct_commit_promotion(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    # Non-retryable 404 on /commits/{sha}/pulls for the direct commit -> UNKNOWN
    fixture.commit_assoc_status_override["e" * 40] = 404
    stack = _build_stack(pg_cluster, fixture)
    access: WorkspaceAccessContext = stack["access"]

    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-unknown", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.status is SyncRunStatus.PARTIAL
    assert step.execution.failure_reason_code == "INCOMPLETE_PR_ASSOCIATION"
    assert step.execution.retry_allowed is False
    assert step.execution.import_ready is False
    assert step.derived_units is None

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []
    assert any(a[2] == "UNKNOWN" for a in snap["sync_commit_pr_associations"])


@pytest.mark.postgres
def test_cursor_cycle_detected_halts_without_promotion(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    fixture.force_cursor_cycle_on_pr_page = 2
    stack = _build_stack(pg_cluster, fixture)
    access: WorkspaceAccessContext = stack["access"]

    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-cycle", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.status is SyncRunStatus.PARTIAL
    assert step.execution.failure_reason_code == "CURSOR_CYCLE_DETECTED"
    assert step.execution.retry_allowed is False

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []


# =============================================================================
# 3. Visibility Flip (PUBLIC -> PRIVATE), 401 Revocation, and Deletion Fence
# =============================================================================
@pytest.mark.postgres
def test_mid_run_private_visibility_flip_updates_policy_and_blocks_all_operations(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()

    class _FlipPrivateMidRunHook:
        def before_page_commit(self, ctx: PageHookContext) -> None:
            return None

        def after_page_commit(self, ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.DEFAULT_BRANCH_COMMITS and ctx.page_number == 2:
                fixture.private = True
                fixture.visibility = "private"

        def before_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

        def after_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

    stack = _build_stack(pg_cluster, fixture, lifecycle_hook=_FlipPrivateMidRunHook())
    access: WorkspaceAccessContext = stack["access"]
    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-priv", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.import_ready is False
    assert step.execution.retry_allowed is False
    assert step.execution.status in (SyncRunStatus.FAILED, SyncRunStatus.CANCELED)

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []
    # Zero tombstones created on visibility flip!
    assert all(rev[4] is False for rev in snap["source_object_revisions"])

    # Confirm github_connect blocks ingestion, refetch, AI transmission, and export!
    for op in PolicyGuardedOperation:
        with pytest.raises(GitHubConnectError):
            stack["connect_service"].assert_resource_processing_allowed(
                access, source_resource_id=stack["resource_id"], operation=op
            )


@pytest.mark.postgres
def test_mid_run_401_revocation_marks_account_revoked_with_zero_promotions(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()

    class _RevokeTokenHook:
        def before_page_commit(self, ctx: PageHookContext) -> None:
            return None

        def after_page_commit(self, ctx: PageHookContext) -> None:
            if ctx.stream_name is BackfillStreamName.PRS and ctx.page_number == 1:
                fixture.auth_revoked = True

        def before_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

        def after_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

    stack = _build_stack(pg_cluster, fixture, lifecycle_hook=_RevokeTokenHook())
    access: WorkspaceAccessContext = stack["access"]
    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-401", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.import_ready is False
    assert step.execution.retry_allowed is False

    accounts = stack["connect_service"].list_source_accounts(access)
    assert accounts[0].status == "REVOKED"
    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []


@pytest.mark.postgres
def test_deletion_fence_before_finalize_cancels_run_with_zero_promotions(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()

    class _InsertDeletionFenceHook:
        def before_page_commit(self, ctx: PageHookContext) -> None:
            return None

        def after_page_commit(self, ctx: PageHookContext) -> None:
            return None

        def before_finalize(self, ctx: FinalizeHookContext) -> None:
            with pg_cluster.admin_connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO public.deletion_jobs (
                                id, workspace_id, source_resource_id, status
                            ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'PENDING')
                            """,
                            (str(uuid.uuid4()), ctx.workspace_id, ctx.source_resource_id),
                        )

        def after_finalize(self, ctx: FinalizeHookContext) -> None:
            return None

    stack = _build_stack(pg_cluster, fixture, lifecycle_hook=_InsertDeletionFenceHook())
    access: WorkspaceAccessContext = stack["access"]
    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step = stack["worker"].claim_and_execute_next(
        access.workspace_id, worker_id="w-del", access=access, now=_FIXED_NOW
    )
    assert step is not None
    assert step.execution.status is SyncRunStatus.CANCELED
    assert step.execution.import_ready is False
    assert step.execution.retry_allowed is False

    snap = _extract_logical_db_snapshot(pg_cluster, access.workspace_id, receipt.target_id)
    assert snap["work_events"] == []
    assert snap["import_ready_outbox"] == []


# =============================================================================
# 4. BackfillLifecycleHookPort Ordering & Post-Commit Crash Recovery (B-004 Readiness)
# =============================================================================
@pytest.mark.postgres
def test_lifecycle_hook_crash_after_page_commit_recovers_cleanly_without_duplicates(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    fixture = OfflineGitHubRepoFixture.build_standard_multipage()
    events: list[str] = []
    crashed_once = [False]

    class _CrashAfterCommitHook:
        def before_page_commit(self, ctx: PageHookContext) -> None:
            events.append(f"before_page:{ctx.stream_name.value}:{ctx.page_number}")

        def after_page_commit(self, ctx: PageHookContext) -> None:
            events.append(f"after_page:{ctx.stream_name.value}:{ctx.page_number}")
            if (
                not crashed_once[0]
                and ctx.stream_name is BackfillStreamName.PRS
                and ctx.page_number == 1
            ):
                crashed_once[0] = True
                raise RuntimeError("SIMULATED_WORKER_CRASH_AFTER_PAGE_COMMIT")

        def before_finalize(self, ctx: FinalizeHookContext) -> None:
            events.append("before_finalize")

        def after_finalize(self, ctx: FinalizeHookContext) -> None:
            events.append("after_finalize")

    stack = _build_stack(pg_cluster, fixture, lifecycle_hook=_CrashAfterCommitHook())
    access: WorkspaceAccessContext = stack["access"]
    receipt = stack["sync_runs_service"].create_initial_import(
        access,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack["resource_id"], "mode": "INITIAL_BACKFILL"},
    )

    with pytest.raises(RuntimeError, match="SIMULATED_WORKER_CRASH_AFTER_PAGE_COMMIT"):
        stack["orchestrator"].execute_sync_run(
            workspace_id=access.workspace_id,
            sync_run_id=receipt.target_id,
            access=access,
            attempt=1,
            now=_FIXED_NOW,
        )

    # Re-entering orchestrator on the RUNNING run resumes strictly at PRS page 2!
    prs_p1_key = f"/repositories/{fixture.repo_id}/pulls?page=1"
    assert fixture.request_counts[prs_p1_key] == 1

    res = stack["orchestrator"].execute_sync_run(
        workspace_id=access.workspace_id,
        sync_run_id=receipt.target_id,
        access=access,
        attempt=2,
        now=_FIXED_NOW,
    )
    assert res.status is SyncRunStatus.SUCCESS
    assert res.resumed_from_checkpoint is True
    assert len(res.promoted_pr_event_ids) == 3
    assert len(res.promoted_direct_commit_event_ids) == 1
    assert "before_finalize" in events
    assert "after_finalize" in events
    assert events.index("before_finalize") < events.index("after_finalize")
    # PRS page 1 was only requested once during attempt 1 (+ 1 during 2nd-pass completion gate re-enumeration)
    assert fixture.request_counts[prs_p1_key] == 2


# =============================================================================
# 5. DerivedUnitIngestionProcessor BLOCKED_SOURCE & BLOCKED_POLICY Projection
# =============================================================================
@pytest.mark.postgres
def test_derived_units_projects_blocked_source_and_blocked_policy_preserving_success_status(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # --- Case A: Upstream PR body mutates after IMPORT_READY -> BLOCKED_SOURCE, status stays SUCCESS ---
    fixture_a = OfflineGitHubRepoFixture.build_standard_multipage()
    stack_a = _build_stack(pg_cluster, fixture_a)
    access_a: WorkspaceAccessContext = stack_a["access"]
    receipt_a = stack_a["sync_runs_service"].create_initial_import(
        access_a,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack_a["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step_a = stack_a["worker"].claim_and_execute_next(
        access_a.workspace_id,
        worker_id="w-src-a",
        access=access_a,
        run_derived_units=False,
        now=_FIXED_NOW,
    )
    assert step_a is not None and step_a.execution.status is SyncRunStatus.SUCCESS

    # Mutate PR #10 body upstream BEFORE derived unit refetch runs!
    fixture_a.pr_body_override_by_number[10] = "MUTATED_UPSTREAM_BODY_CAUSING_HASH_MISMATCH"
    derived_a = stack_a["derived_processor"].process_import_ready_run(
        workspace_id=access_a.workspace_id,
        sync_run_id=receipt_a.target_id,
        now=_FIXED_NOW,
    )
    assert derived_a.executed is True
    assert derived_a.sync_run_status is SyncRunStatus.SUCCESS
    assert derived_a.processing_status is ProcessingStatus.BLOCKED_SOURCE
    assert derived_a.reason_code == "SOURCE_REVISION_MISMATCH"

    # Verify DB sync_runs kept status='SUCCESS' and import_ready=True while projecting processing_status='BLOCKED_SOURCE'
    st_a = stack_a["sync_runs_service"].get_status(access_a, run_id=receipt_a.target_id)
    assert st_a.status is SyncRunStatus.SUCCESS
    assert st_a.import_ready is True
    assert st_a.processing_status is ProcessingStatus.BLOCKED_SOURCE

    # --- Case B: Upstream repo turns PRIVATE after IMPORT_READY -> BLOCKED_POLICY, status stays SUCCESS ---
    pg_cluster.truncate_all()
    fixture_b = OfflineGitHubRepoFixture.build_standard_multipage()
    stack_b = _build_stack(pg_cluster, fixture_b)
    access_b: WorkspaceAccessContext = stack_b["access"]
    receipt_b = stack_b["sync_runs_service"].create_initial_import(
        access_b,
        idempotency_key=str(uuid.uuid4()),
        request_payload={"source_resource_id": stack_b["resource_id"], "mode": "INITIAL_BACKFILL"},
    )
    step_b = stack_b["worker"].claim_and_execute_next(
        access_b.workspace_id,
        worker_id="w-pol-b",
        access=access_b,
        run_derived_units=False,
        now=_FIXED_NOW,
    )
    assert step_b is not None and step_b.execution.status is SyncRunStatus.SUCCESS

    # Flip repository to PRIVATE after import succeeded
    fixture_b.private = True
    fixture_b.visibility = "private"
    derived_b = stack_b["derived_processor"].process_import_ready_run(
        workspace_id=access_b.workspace_id,
        sync_run_id=receipt_b.target_id,
        now=_FIXED_NOW,
    )
    assert derived_b.executed is True
    assert derived_b.sync_run_status is SyncRunStatus.SUCCESS
    assert derived_b.processing_status is ProcessingStatus.BLOCKED_POLICY

    st_b = stack_b["sync_runs_service"].get_status(access_b, run_id=receipt_b.target_id)
    assert st_b.status is SyncRunStatus.SUCCESS
    assert st_b.import_ready is True
    assert st_b.processing_status is ProcessingStatus.BLOCKED_POLICY
