"""Real local PostgreSQL 18 atomicity, 2-worker fencing, crash recovery, and promotion gate tests."""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

pytestmark = pytest.mark.postgres

from career_memory_api.backfill_engine import (
    AssociationMatchMechanism,
    BackfillFailureReasonCode,
    BackfillStreamName,
    CommitAssociationDecision,
    FinalizationDirectCommitCandidate,
    FinalizationPrCandidate,
    FinalizeOutcomeStatus,
    FinalizeRunRequest,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    NormalizedPageItem,
    PageCommitRequest,
    PageCommitStatus,
    PostgresBackfillPageCommitter,
    RepositoryHeadSnapshot,
    SkipReasonCode,
    apply_backfill_engine_migration,
)
from career_memory_api.schema_contracts.enums import PrAssociationState, SourceObjectType
from career_memory_api.sync_runs.models import SyncRunStatus
from tests.backfill_engine.pg_harness import (
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def pg_cluster() -> EphemeralBackfillPg18Cluster:
    cluster = EphemeralBackfillPg18Cluster()
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


def _make_pr_item(pr_num: int, content_tag: str = "v1") -> NormalizedPageItem:
    return NormalizedPageItem(
        object_type=SourceObjectType.PR,
        provider_object_id=f"pr:{pr_num}",
        content_hash=_sha(f"pr-{pr_num}-{content_tag}"),
        provider_updated_at=datetime(2026, 6, 1, 12, 0, pr_num % 60, tzinfo=UTC),
        normalized_metadata={
            "provider_number": pr_num,
            "state": "closed",
            "merged": True,
            "base_ref": "main",
        },
    )


def _make_commit_item(label: str, *, pr_state: PrAssociationState | None = None) -> NormalizedPageItem:
    sha = _sha(label)[:40]
    return NormalizedPageItem(
        object_type=SourceObjectType.COMMIT,
        provider_object_id=f"commit:{sha}",
        commit_sha=sha,
        content_hash=_sha(f"commit-rev-{label}"),
        provider_updated_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC),
        pr_association_state=pr_state,
        normalized_metadata={"base_ref": "main"},
    )


def test_migration_0050_idempotent_rls_and_plan_c_enforced(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # Re-applying 0050 must be strictly idempotent
    with pg_cluster.admin_connection(autocommit=True) as conn:
        applied = apply_backfill_engine_migration(conn)
        assert applied == "0050_m2_backfill_observations.sql"

    # Verify FORCE RLS on all 0050 tables
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relnamespace = 'public'::regnamespace
                  AND relname IN (
                      'sync_item_outcomes',
                      'sync_commit_pr_associations',
                      'sync_run_page_observations',
                      'sync_run_event_promotions'
                  )
                ORDER BY relname
                """
            )
            rows = cur.fetchall()
            assert len(rows) == 4
            for relname, rls, force_rls in rows:
                assert rls is True, f"{relname} missing ENABLE RLS"
                assert force_rls is True, f"{relname} missing FORCE RLS"

    # Cross-workspace isolation & Plan C forbidden key check under cm_worker_app
    ws_a = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/repo-a")
    ws_b = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="beta/repo-b")

    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
    res_a = committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=ws_a["workspace_id"],
            sync_run_id=ws_a["sync_run_id"],
            source_resource_id=ws_a["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("page-a-1"),
            items=(_make_pr_item(1),),
            item_outcomes=(
                ItemOutcomeRecord(
                    stream_name=BackfillStreamName.PRS,
                    object_type="FILE",
                    provider_object_id="pr:1:bin.dat",
                    outcome_kind=ItemOutcomeKind.SKIPPED,
                    reason_code=SkipReasonCode.BINARY_FILE.value,
                    affected_range="bin.dat",
                ),
            ),
            stream_completed=True,
        )
    )
    assert res_a.status == PageCommitStatus.COMMITTED

    # Worker scoped to workspace B sees 0 rows from workspace A
    with pg_cluster.worker_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_workspace_id', %s, true)",
                    (ws_b["workspace_id"],),
                )
                cur.execute("SELECT COUNT(*) FROM public.sync_item_outcomes")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(*) FROM public.sync_run_page_observations")
                assert cur.fetchone()[0] == 0

    # DB-level Plan C check rejects forbidden raw key on sync_item_outcomes
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_cluster.worker_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.current_workspace_id', %s, true)",
                        (ws_a["workspace_id"],),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.sync_item_outcomes (
                            workspace_id, sync_run_id, source_resource_id,
                            stream_name, object_type, provider_object_id,
                            outcome_kind, reason_code, outcome_metadata
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid,
                            'PRS', 'FILE', 'pr:1:leak.txt',
                            'SKIPPED', 'BINARY_FILE', '{"raw_body":"forbidden"}'::jsonb
                        )
                        """,
                        (ws_a["workspace_id"], ws_a["sync_run_id"], ws_a["source_resource_id"]),
                    )


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_source_objects",
        "after_revisions",
        "before_checkpoint_update",
        "after_checkpoint_update",
    ],
)
def test_mid_transaction_fault_rolls_back_revisions_and_checkpoint_atomically(
    pg_cluster: EphemeralBackfillPg18Cluster,
    fault_stage: str,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg_cluster)

    def _inject(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"Injected crash at {stage}")

    committer = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection,
        fault_injector=_inject,
    )

    with pytest.raises(RuntimeError, match=fault_stage):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed["workspace_id"],
                sync_run_id=seed["sync_run_id"],
                source_resource_id=seed["source_resource_id"],
                stream_name=BackfillStreamName.PRS,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor="cur-2",
                page_fingerprint=_sha("p1"),
                items=(_make_pr_item(1), _make_pr_item(2)),
            )
        )

    # Verify strict rollback: 0 source_objects, 0 revisions, 0 checkpoints, 0 page observations
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_objects")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.source_object_revisions")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.sync_checkpoints")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.sync_run_page_observations")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.work_events")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0


def test_crash_after_commit_and_replay_produces_zero_duplicates_and_logical_parity(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed_clean = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/clean-run")
    seed_crash = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/crash-run")

    def _run_clean(seed: dict[str, str]) -> dict[str, int]:
        committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
        r1 = committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed["workspace_id"],
                sync_run_id=seed["sync_run_id"],
                source_resource_id=seed["source_resource_id"],
                stream_name=BackfillStreamName.PRS,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor="cur-2",
                page_fingerprint=_sha("prs-p1"),
                items=(_make_pr_item(1), _make_pr_item(2)),
            )
        )
        assert r1.status == PageCommitStatus.COMMITTED
        r2 = committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed["workspace_id"],
                sync_run_id=seed["sync_run_id"],
                source_resource_id=seed["source_resource_id"],
                stream_name=BackfillStreamName.PRS,
                page_number=2,
                expected_checkpoint_version=r1.checkpoint_version,
                expected_cursor="cur-2",
                next_cursor=None,
                page_fingerprint=_sha("prs-p2"),
                items=(_make_pr_item(2), _make_pr_item(3)),  # pr:2 boundary duplicate
                stream_completed=True,
            )
        )
        assert r2.status == PageCommitStatus.COMMITTED

        with pg_cluster.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.source_objects WHERE workspace_id = %s::uuid",
                    (seed["workspace_id"],),
                )
                obj_cnt = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM public.source_object_revisions WHERE workspace_id = %s::uuid",
                    (seed["workspace_id"],),
                )
                rev_cnt = int(cur.fetchone()[0])
                return {"objects": obj_cnt, "revisions": rev_cnt}

    clean_counts = _run_clean(seed_clean)
    assert clean_counts == {"objects": 3, "revisions": 3}

    # Now simulate crash IMMEDIATELY AFTER page 1 commits, before returning to caller
    def _post_commit_crash(stage: str) -> None:
        if stage == "after_commit_before_return":
            raise RuntimeError("Worker SIGKILL right after DB commit")

    crashing_committer = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection,
        fault_injector=_post_commit_crash,
    )
    page1_req = PageCommitRequest(
        workspace_id=seed_crash["workspace_id"],
        sync_run_id=seed_crash["sync_run_id"],
        source_resource_id=seed_crash["source_resource_id"],
        stream_name=BackfillStreamName.PRS,
        page_number=1,
        expected_checkpoint_version=0,
        expected_cursor=None,
        next_cursor="cur-2",
        page_fingerprint=_sha("prs-p1"),
        items=(_make_pr_item(1), _make_pr_item(2)),
    )
    with pytest.raises(RuntimeError, match="SIGKILL"):
        crashing_committer.commit_page_batch(page1_req)

    # Recovery worker replays page 1 with stale version 0 -> discarded with 0 writes, returns latest checkpoint!
    recovery_committer = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection
    )
    replay_res = recovery_committer.commit_page_batch(page1_req)
    assert replay_res.status == PageCommitStatus.STALE_CHECKPOINT_VERSION
    assert replay_res.checkpoint_version == 1
    assert replay_res.last_committed_page == 1
    assert replay_res.current_cursor == "cur-2"
    assert replay_res.objects_upserted == 0
    assert replay_res.revisions_inserted == 0

    # Recovery worker continues from returned checkpoint version=1, cursor='cur-2'
    res2 = recovery_committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_crash["workspace_id"],
            sync_run_id=seed_crash["sync_run_id"],
            source_resource_id=seed_crash["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=2,
            expected_checkpoint_version=replay_res.checkpoint_version,
            expected_cursor=replay_res.current_cursor,
            next_cursor=None,
            page_fingerprint=_sha("prs-p2"),
            items=(_make_pr_item(2), _make_pr_item(3)),
            stream_completed=True,
        )
    )
    assert res2.status == PageCommitStatus.COMMITTED

    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.source_objects WHERE workspace_id = %s::uuid",
                (seed_crash["workspace_id"],),
            )
            crash_obj_cnt = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM public.source_object_revisions WHERE workspace_id = %s::uuid",
                (seed_crash["workspace_id"],),
            )
            crash_rev_cnt = int(cur.fetchone()[0])

    assert {"objects": crash_obj_cnt, "revisions": crash_rev_cnt} == clean_counts


def test_two_concurrent_workers_fenced_by_select_for_update_and_checkpoint_version(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg_cluster)

    # First commit page 1 cleanly so checkpoint_version = 1 exists
    base_committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
    p1 = base_committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor="cur-2",
            page_fingerprint=_sha("race-p1"),
            items=(_make_pr_item(1),),
        )
    )
    assert p1.checkpoint_version == 1

    worker_a_locked = threading.Event()
    release_worker_a = threading.Event()
    results: dict[str, object] = {}

    def _worker_a_hook(stage: str) -> None:
        if stage == "checkpoint_locked":
            worker_a_locked.set()
            assert release_worker_a.wait(timeout=5.0)

    committer_a = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection,
        after_lock_hook=_worker_a_hook,
    )
    committer_b = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection,
    )

    # Both workers read checkpoint_version=1 and try to commit page 2 with different payloads
    req_a = PageCommitRequest(
        workspace_id=seed["workspace_id"],
        sync_run_id=seed["sync_run_id"],
        source_resource_id=seed["source_resource_id"],
        stream_name=BackfillStreamName.PRS,
        page_number=2,
        expected_checkpoint_version=1,
        expected_cursor="cur-2",
        next_cursor="cur-3-winner",
        page_fingerprint=_sha("race-p2-winner"),
        items=(_make_pr_item(2, "winner"),),
    )
    req_b = PageCommitRequest(
        workspace_id=seed["workspace_id"],
        sync_run_id=seed["sync_run_id"],
        source_resource_id=seed["source_resource_id"],
        stream_name=BackfillStreamName.PRS,
        page_number=2,
        expected_checkpoint_version=1,
        expected_cursor="cur-2",
        next_cursor="cur-3-stale",
        page_fingerprint=_sha("race-p2-stale"),
        items=(_make_pr_item(999, "stale-must-not-be-written"),),
    )

    def _run_a() -> None:
        results["a"] = committer_a.commit_page_batch(req_a)

    def _run_b() -> None:
        assert worker_a_locked.wait(timeout=5.0)
        # Release A slightly after B starts attempting FOR UPDATE lock
        threading.Timer(0.15, release_worker_a.set).start()
        results["b"] = committer_b.commit_page_batch(req_b)

    t_a = threading.Thread(target=_run_a)
    t_b = threading.Thread(target=_run_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)

    res_a = results["a"]
    res_b = results["b"]
    assert res_a.status == PageCommitStatus.COMMITTED
    assert res_a.checkpoint_version == 2
    assert res_b.status == PageCommitStatus.STALE_CHECKPOINT_VERSION
    assert res_b.checkpoint_version == 2
    assert res_b.current_cursor == "cur-3-winner"
    assert res_b.objects_upserted == 0
    assert res_b.revisions_inserted == 0

    # Verify losing worker's pr:999 was NEVER written to DB!
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider_object_id
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                ORDER BY provider_object_id
                """,
                (seed["workspace_id"],),
            )
            ids = [row[0] for row in cur.fetchall()]
            assert ids == ["pr:1", "pr:2"]


def test_final_success_promotes_single_import_ready_and_pr_plus_direct_commit_events_idempotently(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg_cluster, default_branch="trunk")
    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)

    pr_item = _make_pr_item(42)
    pr_commit_item = _make_commit_item("pr-42-commit")
    direct_commit_1 = _make_commit_item("direct-1")
    direct_commit_2 = _make_commit_item("direct-2")
    out_window_commit = _make_commit_item("out-window-pr-commit")
    review_item = NormalizedPageItem(
        object_type=SourceObjectType.REVIEW,
        provider_object_id="review:42:1",
        content_hash=_sha("review-42-1"),
        provider_updated_at=datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC),
        normalized_metadata={"state": "APPROVED"},
    )

    # Commit all 4 required streams
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("s-prs"),
            items=(pr_item,),
            stream_completed=True,
        )
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.PR_REVIEWS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("s-reviews"),
            items=(review_item,),
            stream_completed=True,
        )
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("s-commits"),
            items=(pr_commit_item, direct_commit_1, direct_commit_2, out_window_commit),
            stream_completed=True,
        )
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("s-assoc"),
            items=(),
            commit_associations=(
                CommitAssociationDecision(
                    commit_provider_object_id=pr_commit_item.provider_object_id,
                    commit_sha=pr_commit_item.commit_sha or "",
                    default_branch="trunk",
                    state=PrAssociationState.MATCHED_PR,
                    matched_pr_provider_id="pr:42",
                    matched_pr_in_window=True,
                    match_mechanism=AssociationMatchMechanism.IN_WINDOW_PR_COMMIT,
                    lookup_completed=True,
                ),
                CommitAssociationDecision(
                    commit_provider_object_id=out_window_commit.provider_object_id,
                    commit_sha=out_window_commit.commit_sha or "",
                    default_branch="trunk",
                    state=PrAssociationState.MATCHED_PR,
                    matched_pr_provider_id="pr:5",
                    matched_pr_in_window=False,
                    match_mechanism=AssociationMatchMechanism.OUT_OF_WINDOW_PR_API,
                    lookup_completed=True,
                    exclusion_outcome=ItemOutcomeRecord(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        object_type="COMMIT",
                        provider_object_id=out_window_commit.provider_object_id,
                        outcome_kind=ItemOutcomeKind.EXCLUDED,
                        reason_code=SkipReasonCode.OUT_OF_WINDOW_PR_LINKED.value,
                    ),
                ),
                CommitAssociationDecision(
                    commit_provider_object_id=direct_commit_1.provider_object_id,
                    commit_sha=direct_commit_1.commit_sha or "",
                    default_branch="trunk",
                    state=PrAssociationState.NO_MATCH_OBSERVED,
                    direct_commit_candidate=True,
                    lookup_completed=True,
                ),
                CommitAssociationDecision(
                    commit_provider_object_id=direct_commit_2.provider_object_id,
                    commit_sha=direct_commit_2.commit_sha or "",
                    default_branch="trunk",
                    state=PrAssociationState.NO_MATCH_OBSERVED,
                    direct_commit_candidate=True,
                    lookup_completed=True,
                ),
            ),
            stream_completed=True,
        )
    )

    # Before finalize: ZERO IMPORT_READY outbox and ZERO work_events!
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0

    head_snap = RepositoryHeadSnapshot(
        default_branch="trunk",
        head_sha=_sha("trunk-head")[:40],
        start_head_reachable=True,
        pr_enumeration_digest=_sha("pr-enum-42"),
    )

    finalize_req = FinalizeRunRequest(
        workspace_id=seed["workspace_id"],
        sync_run_id=seed["sync_run_id"],
        source_resource_id=seed["source_resource_id"],
        start_snapshot=head_snap,
        end_snapshot=head_snap,
        pr_candidates=(
            FinalizationPrCandidate(
                provider_object_id="pr:42",
                title="PR #42: Build atomic backfill engine",
                member_commit_provider_ids=(pr_commit_item.provider_object_id,),
                member_review_provider_ids=(review_item.provider_object_id,),
                summary_metadata={"provider_number": 42, "default_branch": "trunk"},
            ),
        ),
        direct_commit_candidates=(
            FinalizationDirectCommitCandidate(
                provider_object_id=direct_commit_1.provider_object_id,
                commit_sha=direct_commit_1.commit_sha or "",
                default_branch="trunk",
                title="Direct hotfix 1",
            ),
            FinalizationDirectCommitCandidate(
                provider_object_id=direct_commit_2.provider_object_id,
                commit_sha=direct_commit_2.commit_sha or "",
                default_branch="trunk",
                title="Direct hotfix 2",
            ),
        ),
        processed_range="2026-01-01..2026-06-30",
    )

    fin1 = committer.finalize_import_run(finalize_req)
    assert fin1.outcome == FinalizeOutcomeStatus.SUCCESS
    assert fin1.sync_run_status == SyncRunStatus.SUCCESS
    assert fin1.import_ready is True
    assert fin1.import_ready_outbox_id is not None
    assert len(fin1.promoted_pr_event_ids) == 1
    assert len(fin1.promoted_direct_commit_event_ids) == 2

    # Duplicate completion signal -> strictly idempotent, 0 duplicate events or outbox rows!
    fin2 = committer.finalize_import_run(finalize_req)
    assert fin2.outcome == FinalizeOutcomeStatus.ALREADY_SUCCEEDED
    assert fin2.import_ready_outbox_id == fin1.import_ready_outbox_id
    assert fin2.promoted_pr_event_ids == fin1.promoted_pr_event_ids
    assert fin2.promoted_direct_commit_event_ids == fin1.promoted_direct_commit_event_ids

    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 1

            cur.execute(
                """
                SELECT anchor_mode, status, created_by, summary_metadata
                FROM public.work_events
                ORDER BY anchor_mode, title
                """
            )
            events = cur.fetchall()
            assert len(events) == 3  # 1 PR + 2 individual DIRECT_COMMIT (no clustering!)

            direct_events = [e for e in events if e[0] == "DIRECT_COMMIT"]
            assert len(direct_events) == 2
            for mode, status, created_by, meta in direct_events:
                assert status == "CANDIDATE"
                assert created_by == "RULE"
                assert meta["user_review_required"] is True
                assert meta["association_note"] == "NO_MATCH_OBSERVED_REQUIRES_CONFIRMATION"

            # Verify PR event MUST_LINK memberships include PR + PR commit + PR review
            cur.execute(
                """
                SELECT relation_class, relation_type
                FROM public.event_memberships
                WHERE event_id = %s::uuid
                ORDER BY relation_type
                """,
                (fin1.promoted_pr_event_ids[0],),
            )
            pr_memberships = cur.fetchall()
            assert pr_memberships == [
                ("MUST_LINK", "PRIMARY_ANCHOR"),
                ("MUST_LINK", "PR_COMMIT"),
                ("MUST_LINK", "PR_REVIEW"),
            ]


def test_unknown_association_or_plan_drift_blocks_promotion_and_leaves_zero_work_events(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # Scenario A: UNKNOWN commit PR association blocks SUCCESS
    seed_unk = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/unknown-assoc")
    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
    c_unk = _make_commit_item("unknown-commit")

    for stream in (BackfillStreamName.PRS, BackfillStreamName.PR_REVIEWS):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed_unk["workspace_id"],
                sync_run_id=seed_unk["sync_run_id"],
                source_resource_id=seed_unk["source_resource_id"],
                stream_name=stream,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor=None,
                page_fingerprint=_sha(f"unk-{stream}"),
                items=(),
                stream_completed=True,
            )
        )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_unk["workspace_id"],
            sync_run_id=seed_unk["sync_run_id"],
            source_resource_id=seed_unk["source_resource_id"],
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("unk-commits"),
            items=(c_unk,),
            stream_completed=True,
        )
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_unk["workspace_id"],
            sync_run_id=seed_unk["sync_run_id"],
            source_resource_id=seed_unk["source_resource_id"],
            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("unk-assoc"),
            items=(),
            commit_associations=(
                CommitAssociationDecision(
                    commit_provider_object_id=c_unk.provider_object_id,
                    commit_sha=c_unk.commit_sha or "",
                    default_branch="main",
                    state=PrAssociationState.UNKNOWN,
                    unknown_reason_code="PROVIDER_TIMEOUT",
                    lookup_completed=False,
                ),
            ),
            stream_completed=True,
        )
    )

    snap = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("h1")[:40],
        start_head_reachable=True,
        pr_enumeration_digest=_sha("e1"),
    )
    res_unk = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed_unk["workspace_id"],
            sync_run_id=seed_unk["sync_run_id"],
            source_resource_id=seed_unk["source_resource_id"],
            start_snapshot=snap,
            end_snapshot=snap,
        )
    )
    assert res_unk.outcome == FinalizeOutcomeStatus.BLOCKED_PARTIAL
    assert res_unk.sync_run_status == SyncRunStatus.PARTIAL
    assert res_unk.failure_reason_code == BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION.value
    assert res_unk.import_ready is False
    assert res_unk.import_ready_outbox_id is None

    # Scenario B: PLAN_DRIFT (HEAD moved between start and end) blocks SUCCESS
    seed_drift = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/plan-drift")
    for stream in (
        BackfillStreamName.PRS,
        BackfillStreamName.PR_REVIEWS,
        BackfillStreamName.DEFAULT_BRANCH_COMMITS,
        BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
    ):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed_drift["workspace_id"],
                sync_run_id=seed_drift["sync_run_id"],
                source_resource_id=seed_drift["source_resource_id"],
                stream_name=stream,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor=None,
                page_fingerprint=_sha(f"drift-{stream}"),
                items=(),
                stream_completed=True,
            )
        )
    res_drift = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed_drift["workspace_id"],
            sync_run_id=seed_drift["sync_run_id"],
            source_resource_id=seed_drift["source_resource_id"],
            start_snapshot=snap,
            end_snapshot=RepositoryHeadSnapshot(
                default_branch="main",
                head_sha=_sha("h2-changed")[:40],
                start_head_reachable=True,
                pr_enumeration_digest=_sha("e1"),
            ),
        )
    )
    assert res_drift.outcome == FinalizeOutcomeStatus.BLOCKED_PARTIAL
    assert res_drift.sync_run_status == SyncRunStatus.PARTIAL
    assert res_drift.failure_reason_code == BackfillFailureReasonCode.PLAN_DRIFT.value
    assert res_drift.source_drift is True
    assert res_drift.retry_allowed is False
    assert res_drift.import_ready is False

    # Verify ZERO work_events, ZERO facts, ZERO claims, ZERO IMPORT_READY across both runs
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for tbl in ("work_events", "facts", "claims"):
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0


def test_deletion_fence_blocks_page_commit_and_finalization(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/deleted-repo")

    # Insert an active deletion_jobs fence
    with pg_cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.deletion_jobs (
                        id, workspace_id, source_resource_id, status
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, 'PENDING')
                    """,
                    (str(uuid.uuid4()), seed["workspace_id"], seed["source_resource_id"]),
                )

    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
    page_res = committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("fenced-p1"),
            items=(_make_pr_item(1),),
            stream_completed=True,
        )
    )
    assert page_res.status == PageCommitStatus.BLOCKED_BY_DELETION_FENCE
    assert page_res.objects_upserted == 0
    assert page_res.revisions_inserted == 0

    snap = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("h-fence")[:40],
        start_head_reachable=True,
        pr_enumeration_digest=_sha("e-fence"),
    )
    fin_res = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            start_snapshot=snap,
            end_snapshot=snap,
        )
    )
    assert fin_res.outcome == FinalizeOutcomeStatus.BLOCKED_BY_DELETION_FENCE
    assert fin_res.sync_run_status == SyncRunStatus.CANCELED
    assert fin_res.import_ready is False

    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_objects")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.work_events")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0


def test_partial_with_limit_preserves_committed_pages_with_zero_promotion(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/limit-repo")
    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)

    # Page 1 commits cleanly (metadata/hashes preserved)
    r1 = committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor="cur-2",
            page_fingerprint=_sha("limit-p1"),
            items=(_make_pr_item(1), _make_pr_item(2)),
        )
    )
    assert r1.status == PageCommitStatus.COMMITTED

    # Page 2 hits max_objects -> record PARTIAL_WITH_LIMIT without advancing checkpoint
    status = committer.record_non_success_terminal_state(
        workspace_id=seed["workspace_id"],
        sync_run_id=seed["sync_run_id"],
        source_resource_id=seed["source_resource_id"],
        stream_name=BackfillStreamName.PRS,
        target_status=SyncRunStatus.PARTIAL_WITH_LIMIT,
        failure_reason_code=BackfillFailureReasonCode.LIMIT_EXCEEDED,
        processed_range="stream=PRS,committed_pages=0..1,unique_objects=2",
        remaining_range="stream=PRS,from_page=2,cursor=cur-2,reason=max_objects(2)",
        item_outcomes=(
            ItemOutcomeRecord(
                stream_name=BackfillStreamName.PRS,
                object_type="STREAM",
                provider_object_id="stream:PRS:page:2",
                outcome_kind=ItemOutcomeKind.EXCLUDED,
                reason_code=SkipReasonCode.LIMIT_EXCEEDED.value,
                affected_range="from_page=2",
            ),
        ),
    )
    assert status == SyncRunStatus.PARTIAL_WITH_LIMIT

    cp = committer.get_stream_checkpoint(
        workspace_id=seed["workspace_id"],
        sync_run_id=seed["sync_run_id"],
        stream_name=BackfillStreamName.PRS,
    )
    assert cp is not None
    assert cp.last_committed_page == 1  # checkpoint did NOT advance on rejected page 2!
    assert cp.stream_status == SyncRunStatus.PARTIAL_WITH_LIMIT

    # Attempting finalize on PARTIAL_WITH_LIMIT produces zero promotion
    snap = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("h-limit")[:40],
        start_head_reachable=True,
        pr_enumeration_digest=_sha("e-limit"),
    )
    fin = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed["workspace_id"],
            sync_run_id=seed["sync_run_id"],
            source_resource_id=seed["source_resource_id"],
            start_snapshot=snap,
            end_snapshot=snap,
        )
    )
    assert fin.outcome == FinalizeOutcomeStatus.BLOCKED_PARTIAL_WITH_LIMIT
    assert fin.import_ready is False

    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_objects")
            assert cur.fetchone()[0] == 2  # committed page 1 metadata preserved
            cur.execute("SELECT COUNT(*) FROM public.work_events")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0


def test_policy_visibility_change_and_pr_commit_direct_misclassification_and_finalize_rollback(
    pg_cluster: EphemeralBackfillPg18Cluster,
) -> None:
    # 1. Visibility change PUBLIC -> PRIVATE blocks finalization
    seed_vis = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/vis-change")
    committer = PostgresBackfillPageCommitter(worker_connection_factory=pg_cluster.worker_connection)
    for stream in (
        BackfillStreamName.PRS,
        BackfillStreamName.PR_REVIEWS,
        BackfillStreamName.DEFAULT_BRANCH_COMMITS,
        BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
    ):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed_vis["workspace_id"],
                sync_run_id=seed_vis["sync_run_id"],
                source_resource_id=seed_vis["source_resource_id"],
                stream_name=stream,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor=None,
                page_fingerprint=_sha(f"vis-{stream}"),
                items=(),
                stream_completed=True,
            )
        )
    vis_res = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed_vis["workspace_id"],
            sync_run_id=seed_vis["sync_run_id"],
            source_resource_id=seed_vis["source_resource_id"],
            start_snapshot=RepositoryHeadSnapshot(
                default_branch="main",
                head_sha=_sha("vh")[:40],
                pr_enumeration_digest=_sha("ve"),
                visibility="PUBLIC",
            ),
            end_snapshot=RepositoryHeadSnapshot(
                default_branch="main",
                head_sha=_sha("vh")[:40],
                pr_enumeration_digest=_sha("ve"),
                visibility="PRIVATE",
            ),
        )
    )
    assert vis_res.outcome == FinalizeOutcomeStatus.BLOCKED_BY_POLICY_FENCE
    assert vis_res.failure_reason_code == BackfillFailureReasonCode.POLICY_VISIBILITY_CHANGED.value
    assert vis_res.import_ready is False

    # 2. Defense-in-depth: passing a MATCHED_PR commit as a DIRECT_COMMIT candidate is rejected
    seed_dup = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/no-dup-direct")
    pr_c = _make_commit_item("already-in-pr")
    for stream in (BackfillStreamName.PRS, BackfillStreamName.PR_REVIEWS):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed_dup["workspace_id"],
                sync_run_id=seed_dup["sync_run_id"],
                source_resource_id=seed_dup["source_resource_id"],
                stream_name=stream,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor=None,
                page_fingerprint=_sha(f"dup-{stream}"),
                items=(),
                stream_completed=True,
            )
        )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_dup["workspace_id"],
            sync_run_id=seed_dup["sync_run_id"],
            source_resource_id=seed_dup["source_resource_id"],
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("dup-commits"),
            items=(pr_c,),
            stream_completed=True,
        )
    )
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_dup["workspace_id"],
            sync_run_id=seed_dup["sync_run_id"],
            source_resource_id=seed_dup["source_resource_id"],
            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("dup-assoc"),
            items=(),
            commit_associations=(
                CommitAssociationDecision(
                    commit_provider_object_id=pr_c.provider_object_id,
                    commit_sha=pr_c.commit_sha or "",
                    default_branch="main",
                    state=PrAssociationState.MATCHED_PR,
                    matched_pr_provider_id="pr:99",
                    matched_pr_in_window=True,
                    match_mechanism=AssociationMatchMechanism.PR_MERGE_COMMIT_SHA,
                    lookup_completed=True,
                ),
            ),
            stream_completed=True,
        )
    )
    snap_dup = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("dh")[:40],
        pr_enumeration_digest=_sha("de"),
    )
    dup_res = committer.finalize_import_run(
        FinalizeRunRequest(
            workspace_id=seed_dup["workspace_id"],
            sync_run_id=seed_dup["sync_run_id"],
            source_resource_id=seed_dup["source_resource_id"],
            start_snapshot=snap_dup,
            end_snapshot=snap_dup,
            direct_commit_candidates=(
                FinalizationDirectCommitCandidate(
                    provider_object_id=pr_c.provider_object_id,
                    commit_sha=pr_c.commit_sha or "",
                    default_branch="main",
                    title="Should never be promoted as DIRECT_COMMIT",
                ),
            ),
        )
    )
    assert dup_res.outcome == FinalizeOutcomeStatus.BLOCKED_PARTIAL
    assert dup_res.failure_reason_code == BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION.value

    # 3. Crash inside finalize before commit rolls back BOTH work_events and IMPORT_READY atomically
    seed_fin_crash = seed_workspace_repo_and_sync_run(pg_cluster, repo_name="acme/fin-crash")
    pr_item = _make_pr_item(77)
    committer.commit_page_batch(
        PageCommitRequest(
            workspace_id=seed_fin_crash["workspace_id"],
            sync_run_id=seed_fin_crash["sync_run_id"],
            source_resource_id=seed_fin_crash["source_resource_id"],
            stream_name=BackfillStreamName.PRS,
            page_number=1,
            expected_checkpoint_version=0,
            expected_cursor=None,
            next_cursor=None,
            page_fingerprint=_sha("fc-prs"),
            items=(pr_item,),
            stream_completed=True,
        )
    )
    for stream in (
        BackfillStreamName.PR_REVIEWS,
        BackfillStreamName.DEFAULT_BRANCH_COMMITS,
        BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
    ):
        committer.commit_page_batch(
            PageCommitRequest(
                workspace_id=seed_fin_crash["workspace_id"],
                sync_run_id=seed_fin_crash["sync_run_id"],
                source_resource_id=seed_fin_crash["source_resource_id"],
                stream_name=stream,
                page_number=1,
                expected_checkpoint_version=0,
                expected_cursor=None,
                next_cursor=None,
                page_fingerprint=_sha(f"fc-{stream}"),
                items=(),
                stream_completed=True,
            )
        )

    crashing_fin = PostgresBackfillPageCommitter(
        worker_connection_factory=pg_cluster.worker_connection,
        fault_injector=lambda s: (_ for _ in ()).throw(RuntimeError("crash before finalize commit"))
        if s == "before_finalize_commit"
        else None,
    )
    snap_fc = RepositoryHeadSnapshot(
        default_branch="main",
        head_sha=_sha("fch")[:40],
        pr_enumeration_digest=_sha("fce"),
    )
    fin_req = FinalizeRunRequest(
        workspace_id=seed_fin_crash["workspace_id"],
        sync_run_id=seed_fin_crash["sync_run_id"],
        source_resource_id=seed_fin_crash["source_resource_id"],
        start_snapshot=snap_fc,
        end_snapshot=snap_fc,
        pr_candidates=(
            FinalizationPrCandidate(provider_object_id="pr:77", title="PR #77"),
        ),
    )
    with pytest.raises(RuntimeError, match="crash before finalize commit"):
        crashing_fin.finalize_import_run(fin_req)

    # Full rollback verified!
    with pg_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.work_events")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.outbox_events WHERE event_type = 'IMPORT_READY'")
            assert cur.fetchone()[0] == 0

    # Clean retry succeeds!
    ok_res = committer.finalize_import_run(fin_req)
    assert ok_res.outcome == FinalizeOutcomeStatus.SUCCESS
    assert len(ok_res.promoted_pr_event_ids) == 1
    assert ok_res.import_ready_outbox_id is not None

