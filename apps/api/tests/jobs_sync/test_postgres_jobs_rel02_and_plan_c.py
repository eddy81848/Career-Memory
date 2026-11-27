"""Real PostgreSQL 18 tests for T-105A (REL-02), Outbox, Plan C checks, and FORCE RLS."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from career_memory_api.jobs.models import (
    BackoffPolicy,
    JobErrorClass,
    JobNotReplayableError,
    JobStatus,
    OutboxEventRecord,
    OutboxStatus,
    StaleJobLeaseError,
)
from career_memory_api.jobs.postgres_runtime import (
    PostgresJobRuntime,
    bind_worker_workspace_context,
)
from career_memory_api.sync_runs.postgres_service import PostgresSyncRunService
from tests.jobs_sync.pg_harness import (
    MIGRATION_0010,
    MIGRATION_0040,
    EphemeralPg18Cluster,
    add_repo_to_workspace,
    seed_workspace_with_repo,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralPg18Cluster]:
    cluster = EphemeralPg18Cluster(prefix="cm-m1-t105a-pg18-")
    try:
        cluster.start()
        cluster.apply_sql_file(MIGRATION_0010)
        cluster.apply_sql_file(MIGRATION_0040)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture()
def clean_db(pg18_cluster: EphemeralPg18Cluster) -> EphemeralPg18Cluster:
    pg18_cluster.truncate_all()
    return pg18_cluster


def _insert_standalone_job(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_id: str,
    logical_effect_key: str,
    next_run_at: datetime,
    max_attempts: int = 3,
    dedupe_key: str | None = None,
    job_type: str = "INITIAL_BACKFILL_SYNC",
) -> str:
    job_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.jobs (
                        id, workspace_id, job_type, dedupe_key,
                        input_hash, input_manifest, status,
                        attempt, max_attempts, next_run_at,
                        logical_effect_key, created_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s, %s,
                        %s, '{"ref": "ok"}'::jsonb, 'PENDING',
                        0, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        job_id,
                        workspace_id,
                        job_type,
                        dedupe_key,
                        "a" * 64,
                        max_attempts,
                        next_run_at,
                        logical_effect_key,
                        next_run_at,
                        next_run_at,
                    ),
                )
    return job_id


def test_rel02_poison_work_backoff_exhaustion_and_queue_non_starvation(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _acc, _repo = seed_workspace_with_repo(clean_db)
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    backoff = BackoffPolicy(base_seconds=10, multiplier=2, max_seconds=60)
    t0 = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)

    poison_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:poison-1",
        next_run_at=t0,
        max_attempts=3,
    )
    healthy_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:healthy-1",
        next_run_at=t0 + timedelta(microseconds=1),
        max_attempts=3,
    )

    # Attempt 1 on poison job -> RETRY_WAIT with +10s backoff
    c1 = runtime.claim_next_job(access.workspace_id, worker_id="w1", now=t0)
    assert c1 is not None and c1.job.id == poison_id and c1.job.attempt == 1
    failed_1 = runtime.fail_job(
        access.workspace_id,
        job_id=poison_id,
        lease_token=c1.lease_token,
        error_code="UPSTREAM_503",
        retryable=True,
        now=t0 + timedelta(seconds=1),
        backoff_policy=backoff,
    )
    assert failed_1.status == JobStatus.RETRY_WAIT
    assert failed_1.error_class == JobErrorClass.RETRYABLE
    assert failed_1.next_run_at == t0 + timedelta(seconds=11)

    # While poison is in RETRY_WAIT, healthy job runs immediately without starvation!
    ch = runtime.claim_next_job(
        access.workspace_id, worker_id="w1", now=t0 + timedelta(seconds=2)
    )
    assert ch is not None and ch.job.id == healthy_id
    done_h, applied_h = runtime.complete_job_with_effect(
        access.workspace_id,
        job_id=healthy_id,
        lease_token=ch.lease_token,
        now=t0 + timedelta(seconds=3),
    )
    assert done_h.status == JobStatus.SUCCEEDED and applied_h is True

    # Before next_run_at, poison is NOT claimable
    assert (
        runtime.claim_next_job(
            access.workspace_id, worker_id="w1", now=t0 + timedelta(seconds=10)
        )
        is None
    )

    # Attempt 2 -> RETRY_WAIT with +20s backoff
    c2 = runtime.claim_next_job(
        access.workspace_id, worker_id="w1", now=t0 + timedelta(seconds=11)
    )
    assert c2 is not None and c2.job.id == poison_id and c2.job.attempt == 2
    failed_2 = runtime.fail_job(
        access.workspace_id,
        job_id=poison_id,
        lease_token=c2.lease_token,
        error_code="UPSTREAM_503",
        retryable=True,
        now=t0 + timedelta(seconds=12),
        backoff_policy=backoff,
    )
    assert failed_2.status == JobStatus.RETRY_WAIT
    assert failed_2.next_run_at == t0 + timedelta(seconds=32)

    # Attempt 3 (budget exhausted) -> FAILED_TERMINAL (POISON_BUDGET_EXHAUSTED), permanently preserved!
    c3 = runtime.claim_next_job(
        access.workspace_id, worker_id="w1", now=t0 + timedelta(seconds=32)
    )
    assert c3 is not None and c3.job.id == poison_id and c3.job.attempt == 3
    terminal = runtime.fail_job(
        access.workspace_id,
        job_id=poison_id,
        lease_token=c3.lease_token,
        error_code="UPSTREAM_503",
        retryable=True,
        now=t0 + timedelta(seconds=33),
        backoff_policy=backoff,
    )
    assert terminal.status == JobStatus.FAILED_TERMINAL
    assert terminal.error_class == JobErrorClass.POISON_BUDGET_EXHAUSTED
    assert terminal.completed_at is not None

    # No longer claimed by queue, and row remains permanently in DB
    assert (
        runtime.claim_next_job(
            access.workspace_id, worker_id="w1", now=t0 + timedelta(hours=1)
        )
        is None
    )
    # Non-retryable error goes straight to FAILED_TERMINAL on attempt 1
    non_retry_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:non-retryable",
        next_run_at=t0 + timedelta(hours=1),
        max_attempts=5,
    )
    cn = runtime.claim_next_job(access.workspace_id, worker_id="w1", now=t0 + timedelta(hours=1))
    assert cn is not None and cn.job.id == non_retry_id
    term_nr = runtime.fail_job(
        access.workspace_id,
        job_id=non_retry_id,
        lease_token=cn.lease_token,
        error_code="SCHEMA_CORRUPTION",
        retryable=False,
        now=t0 + timedelta(hours=1, seconds=1),
    )
    assert term_nr.status == JobStatus.FAILED_TERMINAL
    assert term_nr.error_class == JobErrorClass.NON_RETRYABLE
    assert term_nr.attempt == 1


def test_rel02_lease_expiry_reclaim_blocks_zombie_worker_late_writes(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _acc, _repo = seed_workspace_with_repo(clean_db)
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    t0 = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)

    job_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:lease-fence",
        next_run_at=t0,
        max_attempts=3,
    )

    # Worker 1 claims with 30s lease
    c1 = runtime.claim_next_job(
        access.workspace_id,
        worker_id="zombie-worker-1",
        lease_duration=timedelta(seconds=30),
        now=t0,
    )
    assert c1 is not None and c1.job.id == job_id and c1.job.attempt == 1

    # Lease expires; Worker 2 reclaims with a fresh lease_token
    t_expired = t0 + timedelta(seconds=31)
    c2 = runtime.claim_next_job(
        access.workspace_id,
        worker_id="fresh-worker-2",
        lease_duration=timedelta(seconds=30),
        now=t_expired,
    )
    assert c2 is not None and c2.job.id == job_id
    assert c2.job.attempt == 2
    assert c2.lease_token != c1.lease_token

    # Zombie Worker 1 wakes up late and tries to write checkpoint or complete -> REJECTED!
    with pytest.raises(StaleJobLeaseError):
        runtime.heartbeat_and_checkpoint(
            access.workspace_id,
            job_id=job_id,
            lease_token=c1.lease_token,
            checkpoint={"page": 99},
            now=t_expired + timedelta(seconds=1),
        )
    with pytest.raises(StaleJobLeaseError):
        runtime.complete_job_with_effect(
            access.workspace_id,
            job_id=job_id,
            lease_token=c1.lease_token,
            now=t_expired + timedelta(seconds=2),
        )

    # Fresh Worker 2 saves checkpoint and commits side effect cleanly once
    cp = runtime.heartbeat_and_checkpoint(
        access.workspace_id,
        job_id=job_id,
        lease_token=c2.lease_token,
        checkpoint={"page": 4},
        now=t_expired + timedelta(seconds=3),
    )
    assert cp.checkpoint == {"page": 4}
    done, applied = runtime.complete_job_with_effect(
        access.workspace_id,
        job_id=job_id,
        lease_token=c2.lease_token,
        effect_metadata={"summary": "ok"},
        now=t_expired + timedelta(seconds=4),
    )
    assert done.status == JobStatus.SUCCEEDED
    assert applied is True


def test_rel02_reclaim_expired_leases_sweeper_moves_retryable_and_exhausted_jobs(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _acc, _repo = seed_workspace_with_repo(clean_db)
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    t0 = datetime(2026, 10, 5, 11, 30, tzinfo=UTC)

    retryable_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:sweep-retry",
        next_run_at=t0,
        max_attempts=2,
    )
    exhausted_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:sweep-exhausted",
        next_run_at=t0,
        max_attempts=1,
    )

    c1 = runtime.claim_next_job(
        access.workspace_id, worker_id="w1", lease_duration=timedelta(seconds=10), now=t0
    )
    c2 = runtime.claim_next_job(
        access.workspace_id, worker_id="w2", lease_duration=timedelta(seconds=10), now=t0
    )
    assert c1 is not None and c2 is not None

    swept = runtime.reclaim_expired_leases(
        access.workspace_id,
        now=t0 + timedelta(seconds=15),
        backoff_policy=BackoffPolicy(base_seconds=5, multiplier=2, max_seconds=60),
    )
    by_id = {j.id: j for j in swept}
    assert by_id[retryable_id].status == JobStatus.RETRY_WAIT
    assert by_id[retryable_id].error_code == "LEASE_EXPIRED"
    assert by_id[exhausted_id].status == JobStatus.FAILED_TERMINAL
    assert by_id[exhausted_id].error_class == JobErrorClass.POISON_BUDGET_EXHAUSTED
    assert by_id[exhausted_id].error_code == "LEASE_EXPIRED_BUDGET_EXHAUSTED"


def test_rel02_manual_replay_preserves_parent_lineage_and_single_logical_effect(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _acc, _repo = seed_workspace_with_repo(clean_db)
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    parent_id = _insert_standalone_job(
        clean_db,
        workspace_id=access.workspace_id,
        logical_effect_key="effect:replay-once",
        next_run_at=t0,
        max_attempts=1,
    )
    c1 = runtime.claim_next_job(access.workspace_id, worker_id="w1", now=t0)
    assert c1 is not None
    runtime.fail_job(
        access.workspace_id,
        job_id=parent_id,
        lease_token=c1.lease_token,
        error_code="TRANSIENT_FAILURE",
        retryable=True,
        now=t0 + timedelta(seconds=1),
    )

    # Replay parent -> creates child linked via execution_parent_id while parent stays FAILED_TERMINAL
    child = runtime.replay_terminal_job(
        access.workspace_id,
        parent_job_id=parent_id,
        operator_actor_id=access.user_id,
        max_attempts=2,
        now=t0 + timedelta(seconds=5),
    )
    assert child.id != parent_id
    assert child.execution_parent_id == parent_id
    assert child.logical_effect_key == "effect:replay-once"
    assert child.status == JobStatus.PENDING

    # Duplicate replay while child is PENDING is rejected by DB unique constraint!
    with pytest.raises(JobNotReplayableError):
        runtime.replay_terminal_job(
            access.workspace_id,
            parent_job_id=parent_id,
            operator_actor_id=access.user_id,
            now=t0 + timedelta(seconds=6),
        )

    # Execute child -> succeeds & records logical effect once
    c2 = runtime.claim_next_job(
        access.workspace_id, worker_id="w2", now=t0 + timedelta(seconds=10)
    )
    assert c2 is not None and c2.job.id == child.id
    done_child, applied = runtime.complete_job_with_effect(
        access.workspace_id,
        job_id=child.id,
        lease_token=c2.lease_token,
        now=t0 + timedelta(seconds=11),
    )
    assert done_child.status == JobStatus.SUCCEEDED
    assert applied is True

    # Replay after child SUCCEEDED is also blocked by uq_jobs_active_or_succeeded_logical_effect!
    with pytest.raises(JobNotReplayableError):
        runtime.replay_terminal_job(
            access.workspace_id,
            parent_job_id=parent_id,
            operator_actor_id=access.user_id,
            now=t0 + timedelta(seconds=12),
        )

    # Parent row is still untouched in FAILED_TERMINAL, and ledger has strictly 1 row
    with clean_db.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.jobs WHERE id = %s::uuid", (parent_id,))
            assert cur.fetchone() == ("FAILED_TERMINAL",)
            cur.execute(
                "SELECT count(*) FROM public.job_side_effect_ledger WHERE logical_effect_key = %s",
                ("effect:replay-once",),
            )
            assert cur.fetchone() == (1,)


def test_outbox_dedupe_single_import_ready_and_consumer_idempotency(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _acc, repo_id = seed_workspace_with_repo(clean_db)
    t0 = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
    service = PostgresSyncRunService(
        connection_factory=clean_db.api_connection,
        clock=lambda: t0,
    )
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)

    receipt = service.create_initial_import(
        access,
        idempotency_key="00000000-0000-4000-8000-000000000099",
        request_payload={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    claimed = runtime.claim_next_job(access.workspace_id, worker_id="w1", now=t0)
    assert claimed is not None
    runtime.complete_job_with_effect(
        access.workspace_id,
        job_id=claimed.job.id,
        lease_token=claimed.lease_token,
        sync_run_success=True,
        now=t0 + timedelta(seconds=2),
    )

    # Attempting to insert a second IMPORT_READY event for the same SyncRun is blocked by DB unique index!
    with pytest.raises(psycopg.errors.UniqueViolation):
        with clean_db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            workspace_id, event_type, aggregate_type, aggregate_id, dedupe_key, payload
                        )
                        VALUES (%s::uuid, 'IMPORT_READY', 'SYNC_RUN', %s::uuid, 'second-key', '{}'::jsonb)
                        """,
                        (access.workspace_id, receipt.target_id),
                    )

    # Dispatch outbox with a flaky publisher (fails once, succeeds on retry)
    attempts = {"count": 0}
    delivered: list[OutboxEventRecord] = []

    class FlakyPublisher:
        def publish(self, event: OutboxEventRecord) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient_bus_error")
            delivered.append(event)

    backoff = BackoffPolicy(base_seconds=5, multiplier=2, max_seconds=60)
    first_pass = runtime.dispatch_pending_outbox(
        access.workspace_id,
        publisher=FlakyPublisher(),
        now=t0 + timedelta(seconds=3),
        backoff_policy=backoff,
    )
    assert any(ev.status == OutboxStatus.PENDING and ev.attempt == 1 for ev in first_pass)

    second_pass = runtime.dispatch_pending_outbox(
        access.workspace_id,
        publisher=FlakyPublisher(),
        now=t0 + timedelta(seconds=10),
        backoff_policy=backoff,
    )
    assert all(ev.status == OutboxStatus.PUBLISHED for ev in second_pass)
    import_ready_event = next(ev for ev in delivered if ev.event_type == "IMPORT_READY")

    # Duplicate delivery to consumer executes side effect strictly ONCE
    consumer_side_effects: list[str] = []
    first_consume = runtime.consume_outbox_event_once(
        access.workspace_id,
        consumer_name="downstream-indexer",
        event=import_ready_event,
        on_effect=lambda ev: consumer_side_effects.append(ev.id),
        now=t0 + timedelta(seconds=11),
    )
    duplicate_consume = runtime.consume_outbox_event_once(
        access.workspace_id,
        consumer_name="downstream-indexer",
        event=import_ready_event,
        on_effect=lambda ev: consumer_side_effects.append(ev.id),
        now=t0 + timedelta(seconds=12),
    )
    assert first_consume is True
    assert duplicate_consume is False
    assert consumer_side_effects == [import_ready_event.id]


def test_plan_c_raw_and_secret_keys_blocked_by_db_check_and_rls_isolation(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access_a, _acc_a, repo_a = seed_workspace_with_repo(
        clean_db, workspace_name="WS-A", repo_name="acme/plan-c-a"
    )
    access_b, _acc_b, repo_b = seed_workspace_with_repo(
        clean_db, workspace_name="WS-B", repo_name="acme/plan-c-b"
    )

    # Plan C forbidden raw keys rejected at DB CHECK layer across sync_runs, jobs, outbox_events, audit_events
    with pytest.raises(psycopg.errors.CheckViolation):
        with clean_db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.sync_runs (workspace_id, source_resource_id, mode, status, plan_spec)
                        VALUES (%s::uuid, %s::uuid, 'INITIAL_BACKFILL', 'PENDING', '{"nested": [{"raw_body": "leak"}]}'::jsonb)
                        """,
                        (access_a.workspace_id, repo_a),
                    )

    with pytest.raises(psycopg.errors.CheckViolation):
        with clean_db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.jobs (workspace_id, job_type, input_hash, input_manifest, logical_effect_key)
                        VALUES (%s::uuid, 'SYNC', %s, '{"diff": "+secret"}'::jsonb, 'effect:bad-1')
                        """,
                        (access_a.workspace_id, "b" * 64),
                    )

    with pytest.raises(psycopg.errors.CheckViolation):
        with clean_db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (workspace_id, event_type, aggregate_type, aggregate_id, dedupe_key, payload)
                        VALUES (%s::uuid, 'TEST', 'SYNC_RUN', %s::uuid, 'bad-outbox', '{"patch": "@@ -1 +1 @@"}'::jsonb)
                        """,
                        (access_a.workspace_id, str(uuid.uuid4())),
                    )

    with pytest.raises(psycopg.errors.CheckViolation):
        with clean_db.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.audit_events (workspace_id, actor_type, action, target_type, target_id, redacted_metadata)
                        VALUES (%s::uuid, 'USER', 'TEST', 'SYNC_RUN', %s::uuid, '{"access_token": "gho_secret"}'::jsonb)
                        """,
                        (access_a.workspace_id, str(uuid.uuid4())),
                    )

    # FORCE RLS fail-closed + cross-tenant isolation + least-privilege checks
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    service = PostgresSyncRunService(
        connection_factory=clean_db.api_connection, clock=lambda: t0
    )
    rec_a = service.create_initial_import(
        access_a,
        idempotency_key="00000000-0000-4000-8000-000000000101",
        request_payload={"source_resource_id": repo_a, "mode": "INITIAL_BACKFILL"},
    )
    _rec_b = service.create_initial_import(
        access_b,
        idempotency_key="00000000-0000-4000-8000-000000000102",
        request_payload={"source_resource_id": repo_b, "mode": "INITIAL_BACKFILL"},
    )

    # Worker without workspace context sees 0 rows (fail-closed)
    with clean_db.worker_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.jobs")
                assert cur.fetchone() == (0,)
                cur.execute("SELECT count(*) FROM public.sync_runs")
                assert cur.fetchone() == (0,)

    # Worker scoped to WS-A sees only WS-A jobs and CANNOT read api_idempotency_records or DELETE jobs
    with clean_db.worker_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                bind_worker_workspace_context(cur, access_a.workspace_id)
                cur.execute("SELECT target_sync_run_id::text FROM public.jobs")
                assert cur.fetchall() == [(rec_a.target_id,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM public.api_idempotency_records")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, access_a.workspace_id)
                    cur.execute("DELETE FROM public.jobs")

    # API cannot UPDATE api_idempotency_records (expires_at non-extension enforced by privilege!)
    with clean_db.api_connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.api_idempotency_records SET expires_at = now() + interval '48 hours'"
                    )


def test_migration_0040_standalone_and_idempotent_bootstrap() -> None:
    """Prove 0040 applies cleanly both standalone (without 0010/0020) and repeatedly."""
    cluster = EphemeralPg18Cluster(prefix="cm-m1-t105-standalone-")
    try:
        cluster.start()
        cluster.apply_sql_file(MIGRATION_0040)
        cluster.apply_sql_file(MIGRATION_0040)
        cluster.provision_runtime_logins()
        access, acc_id, repo_id = seed_workspace_with_repo(cluster)
        _repo2 = add_repo_to_workspace(
            cluster,
            workspace_id=access.workspace_id,
            source_account_id=acc_id,
            repo_name="acme/standalone-repo-2",
        )
        service = PostgresSyncRunService(
            connection_factory=cluster.api_connection,
            worker_connection_factory=cluster.worker_connection,
            clock=lambda: datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
        )
        rec = service.create_initial_import(
            access,
            idempotency_key="00000000-0000-4000-8000-000000000201",
            request_payload={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
        )
        assert rec.target_id
    finally:
        cluster.stop()
