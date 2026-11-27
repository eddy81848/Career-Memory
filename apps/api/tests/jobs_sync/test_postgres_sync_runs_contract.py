"""Real PostgreSQL 18 integration, concurrency, restart, and contract tests for M1-T105."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.authz import (
    AccessVerificationUnavailableError,
    SessionTerminatedError,
    UnauthenticatedError,
    WorkspaceAccessContext,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.jobs.models import WakeSignal
from career_memory_api.jobs.postgres_runtime import PostgresJobRuntime
from career_memory_api.sync_runs.models import (
    ProcessingStatus,
    StreamProgress,
    SyncRunObservation,
    SyncRunStatus,
)
from career_memory_api.sync_runs.postgres_service import PostgresSyncRunService
from career_memory_api.sync_runs.router import create_sync_runs_app
from tests.jobs_sync.pg_harness import (
    MIGRATION_0010,
    MIGRATION_0040,
    EphemeralPg18Cluster,
    add_repo_to_workspace,
    revoke_membership,
    seed_workspace_with_repo,
)

pytestmark = pytest.mark.postgres

KEY_1 = "00000000-0000-4000-8000-000000000001"
KEY_2 = "00000000-0000-4000-8000-000000000002"
KEY_3 = "00000000-0000-4000-8000-000000000003"
KEY_4 = "00000000-0000-4000-8000-000000000004"


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class RecordingWakeNotifier:
    def __init__(
        self,
        *,
        on_notify: Callable[[WakeSignal], None] | None = None,
    ) -> None:
        self.signals: list[WakeSignal] = []
        self._on_notify = on_notify

    def notify_job_available(self, signal: WakeSignal) -> None:
        self.signals.append(signal)
        if self._on_notify is not None:
            self._on_notify(signal)


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralPg18Cluster]:
    cluster = EphemeralPg18Cluster()
    try:
        cluster.start()
        cluster.apply_sql_file(MIGRATION_0010)
        cluster.apply_sql_file(MIGRATION_0040)
        # Re-apply 0040 immediately to prove idempotency
        cluster.apply_sql_file(MIGRATION_0040)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture()
def clean_db(pg18_cluster: EphemeralPg18Cluster) -> EphemeralPg18Cluster:
    pg18_cluster.truncate_all()
    return pg18_cluster


def _table_counts(cluster: EphemeralPg18Cluster) -> dict[str, int]:
    tables = (
        "sync_runs",
        "sync_checkpoints",
        "jobs",
        "outbox_events",
        "api_idempotency_records",
        "audit_events",
    )
    counts: dict[str, int] = {}
    with cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT count(*) FROM public.{table}")
                row = cur.fetchone()
                assert row is not None
                counts[table] = int(row[0])
    return counts


def _make_service(
    cluster: EphemeralPg18Cluster,
    *,
    clock: MutableClock,
    wake_notifier: RecordingWakeNotifier | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> PostgresSyncRunService:
    return PostgresSyncRunService(
        connection_factory=cluster.api_connection,
        worker_connection_factory=cluster.worker_connection,
        clock=clock,
        wake_notifier=wake_notifier,
        fault_injector=fault_injector,
    )


def _transient_partial_observation(observed_at: datetime) -> SyncRunObservation:
    return SyncRunObservation(
        status=SyncRunStatus.PARTIAL,
        failure_reason="TRANSIENT_PROVIDER_ERROR",
        streams=(
            StreamProgress(
                type="PR",
                status="PARTIAL",
                last_committed_page=7,
                unresolved_failures=1,
            ),
        ),
        import_ready=False,
        processing_status=ProcessingStatus.NOT_STARTED,
        observed_at=observed_at,
        source_drift=False,
        processed_range="2025-09-23 through 2026-08-31",
        remaining_range="2026-09-01 through 2026-10-05",
        retry_allowed=True,
        has_safe_checkpoint=True,
    )


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_sync_run",
        "after_job",
        "after_outbox",
        "after_idempotency",
        "after_audit",
    ],
)
def test_single_transaction_atomicity_rolls_back_all_tables_and_skips_wake(
    clean_db: EphemeralPg18Cluster,
    fault_stage: str,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    notifier = RecordingWakeNotifier()

    def inject_fault(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"Injected failure at {stage}")

    service = _make_service(
        clean_db,
        clock=clock,
        wake_notifier=notifier,
        fault_injector=inject_fault,
    )
    app = create_sync_runs_app(service=service, resolve_access=lambda _req: access)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-atomic"},
        json={
            "source_resource_id": repo_id,
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-09-23",
            "limits": {"max_objects": 5000},
        },
    )
    assert resp.status_code == 500
    assert notifier.signals == []
    assert _table_counts(clean_db) == {
        "sync_runs": 0,
        "sync_checkpoints": 0,
        "jobs": 0,
        "outbox_events": 0,
        "api_idempotency_records": 0,
        "audit_events": 0,
    }


def test_post_commit_wake_sees_committed_state_and_wake_failure_is_recoverable(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    observed_from_separate_conn: list[str] = []

    def assert_committed_then_fail_wake(signal: WakeSignal) -> None:
        # Must already be visible from a completely separate PostgreSQL connection!
        with clean_db.admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM public.jobs WHERE id = %s::uuid",
                    (signal.job_id,),
                )
                row = cur.fetchone()
                assert row is not None and row[0] == "PENDING"
                observed_from_separate_conn.append(signal.job_id)
        raise ConnectionError("Simulated Cloud Run wake outage")

    notifier = RecordingWakeNotifier(on_notify=assert_committed_then_fail_wake)
    service = _make_service(clean_db, clock=clock, wake_notifier=notifier)
    app = create_sync_runs_app(service=service, resolve_access=lambda _req: access)
    client = TestClient(app)

    resp = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-wake-fail"},
        json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert len(observed_from_separate_conn) == 1
    assert observed_from_separate_conn[0] == body["job_id"]

    # Even though wake threw ConnectionError, PENDING job + outbox survive and recovery runner claims it.
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    claimed = runtime.claim_next_job(
        access.workspace_id,
        worker_id="recovery-runner-1",
        now=clock(),
    )
    assert claimed is not None
    assert claimed.job.id == body["job_id"]
    completed, applied = runtime.complete_job_with_effect(
        access.workspace_id,
        job_id=claimed.job.id,
        lease_token=claimed.lease_token,
        sync_run_success=True,
        now=clock() + timedelta(seconds=5),
    )
    assert completed.status.value == "SUCCEEDED"
    assert applied is True

    status_resp = client.get(body["status_url"])
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "SUCCESS"
    assert status_resp.json()["import_ready"] is True


def test_api_restart_recovers_202_for_24h_without_extending_expiry(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    notifier_1 = RecordingWakeNotifier()

    # Instance 1 accepts POST and commits to PostgreSQL
    service_1 = _make_service(clean_db, clock=clock, wake_notifier=notifier_1)
    client_1 = TestClient(
        create_sync_runs_app(service=service_1, resolve_access=lambda _req: access)
    )
    payload = {
        "source_resource_id": repo_id,
        "mode": "INITIAL_BACKFILL",
        "backfill_from": "2025-09-23",
        "limits": {"max_objects": 5000},
    }
    first = client_1.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-1"},
        json=payload,
    )
    assert first.status_code == 202
    first_body = first.json()
    assert first_body["status_url"] == f"/v1/sync-runs/{first_body['target_id']}"
    assert len(notifier_1.signals) == 1

    # Simulate API process restart: brand-new service & FastAPI app instance connected to same DB
    clock.advance(timedelta(hours=24) - timedelta(seconds=1))
    notifier_2 = RecordingWakeNotifier()
    service_2 = _make_service(clean_db, clock=clock, wake_notifier=notifier_2)
    client_2 = TestClient(
        create_sync_runs_app(service=service_2, resolve_access=lambda _req: access)
    )

    # Reversed JSON key order still matches canonical request fingerprint
    reordered_payload = dict(reversed(list(payload.items())))
    replayed = client_2.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-replay"},
        json=reordered_payload,
    )
    assert replayed.status_code == 202
    assert replayed.json() == first_body
    assert notifier_2.signals == []
    assert _table_counts(clean_db)["jobs"] == 1
    assert _table_counts(clean_db)["sync_runs"] == 1

    # Different payload within 24h returns 409 IDEMPOTENCY_CONFLICT
    conflict = client_2.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-conflict"},
        json={**payload, "limits": {"max_objects": 200}},
    )
    assert conflict.status_code == 409
    assert conflict.headers["Cache-Control"] == "private, no-store"
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "details" not in conflict.json()["error"]

    # Advance 1 second -> exact 24h boundary reached (replay did NOT extend expires_at!)
    clock.advance(timedelta(seconds=1))
    expired = client_2.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-expired"},
        json=payload,
    )
    assert expired.status_code == 409
    assert expired.headers["Cache-Control"] == "private, no-store"
    assert expired.json()["error"]["code"] == "IDEMPOTENCY_KEY_EXPIRED"
    assert "details" not in expired.json()["error"]
    assert _table_counts(clean_db)["jobs"] == 1


def test_concurrent_create_submissions_open_only_one_run_across_connections(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    service = _make_service(clean_db, clock=clock)
    barrier = Barrier(4, timeout=10)

    def submit(key: str):
        app = create_sync_runs_app(service=service, resolve_access=lambda _req: access)
        with TestClient(app) as client:
            barrier.wait()
            return client.post(
                "/v1/sync-runs",
                headers={"Idempotency-Key": key},
                json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, (KEY_1, KEY_2, KEY_3, KEY_4)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 409, 409, 409]
    accepted = next(r for r in responses if r.status_code == 202)
    run_id = accepted.json()["target_id"]
    for rejected in (r for r in responses if r.status_code == 409):
        err = rejected.json()["error"]
        assert err["code"] == "SYNC_ALREADY_RUNNING"
        assert err["details"] == {"existing_run_id": run_id}
    assert _table_counts(clean_db)["sync_runs"] == 1
    assert _table_counts(clean_db)["jobs"] == 1


def test_delayed_and_concurrent_retry_version_fencing_and_idempotency_precedence(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    service = _make_service(clean_db, clock=clock)
    client = TestClient(
        create_sync_runs_app(service=service, resolve_access=lambda _req: access)
    )

    # Create initial run -> failure A (version 2)
    create_resp = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    assert create_resp.status_code == 202
    run_id = create_resp.json()["target_id"]

    service.record_worker_observation(
        access.workspace_id,
        run_id,
        _transient_partial_observation(clock()),
    )
    failure_a = client.get(f"/v1/sync-runs/{run_id}").json()
    version_a = failure_a["run_version"]
    assert version_a == 2
    assert failure_a["retry"] == {"allowed": True, "from_safe_checkpoint": True}

    # Accept retry for version A -> moves to version 3
    retry_a = client.post(
        f"/v1/sync-runs/{run_id}/retry",
        headers={"Idempotency-Key": KEY_2},
        json={"expected_run_version": version_a},
    )
    assert retry_a.status_code == 202
    retry_a_body = retry_a.json()
    assert retry_a_body["target_id"] == run_id

    # Fail again at identical checkpoint -> failure B (version 4)
    clock.advance(timedelta(minutes=5))
    service.record_worker_observation(
        access.workspace_id,
        run_id,
        _transient_partial_observation(clock()),
    )
    failure_b = client.get(f"/v1/sync-runs/{run_id}").json()
    version_b = failure_b["run_version"]
    assert version_b > version_a
    assert failure_b["streams"] == failure_a["streams"]
    assert failure_b["plan_id"] == failure_a["plan_id"]
    counts_before = _table_counts(clean_db)

    # 1. Replaying accepted KEY_2 + version_a restores original 202 BEFORE version check!
    replay_a = client.post(
        f"/v1/sync-runs/{run_id}/retry",
        headers={"Idempotency-Key": KEY_2},
        json={"expected_run_version": version_a},
    )
    assert replay_a.status_code == 202
    assert replay_a.json() == retry_a_body
    assert _table_counts(clean_db) == counts_before

    # 2. Same KEY_2 with version_b returns 409 IDEMPOTENCY_CONFLICT
    conflict_key = client.post(
        f"/v1/sync-runs/{run_id}/retry",
        headers={"Idempotency-Key": KEY_2},
        json={"expected_run_version": version_b},
    )
    assert conflict_key.status_code == 409
    assert conflict_key.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _table_counts(clean_db) == counts_before

    # 3. New unaccepted KEY_3 with stale version_a returns 409 RESOURCE_VERSION_CONFLICT (zero writes)
    stale_ver = client.post(
        f"/v1/sync-runs/{run_id}/retry",
        headers={"Idempotency-Key": KEY_3},
        json={"expected_run_version": version_a},
    )
    assert stale_ver.status_code == 409
    assert stale_ver.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"
    assert "details" not in stale_ver.json()["error"]
    assert _table_counts(clean_db) == counts_before

    # 4. Concurrent distinct retry keys for version_b: exactly one 202 and one 409 RESOURCE_VERSION_CONFLICT
    barrier = Barrier(2, timeout=10)

    def submit_retry(key: str):
        app = create_sync_runs_app(service=service, resolve_access=lambda _req: access)
        with TestClient(app) as c:
            barrier.wait()
            return c.post(
                f"/v1/sync-runs/{run_id}/retry",
                headers={"Idempotency-Key": key},
                json={"expected_run_version": version_b},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(submit_retry, (KEY_3, KEY_4)), key=lambda r: r.status_code)
    assert [r.status_code for r in results] == [202, 409]
    assert results[1].json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"
    assert _table_counts(clean_db)["jobs"] == counts_before["jobs"] + 1
    resumed = client.get(f"/v1/sync-runs/{run_id}").json()
    assert resumed["status"] == "PENDING"
    assert resumed["run_version"] == version_b + 1
    assert resumed["streams"] == failure_b["streams"]
    assert "failure_reason" not in resumed

    # 5. Concurrent identical retry keys for the next failure yield [202, 202] with 1 job
    service.record_worker_observation(
        access.workspace_id,
        run_id,
        _transient_partial_observation(clock() + timedelta(minutes=10)),
    )
    version_c = client.get(f"/v1/sync-runs/{run_id}").json()["run_version"]
    jobs_before_same = _table_counts(clean_db)["jobs"]
    same_key = str(uuid.uuid4())
    barrier_2 = Barrier(2, timeout=10)

    def submit_same_retry(_idx: int):
        app = create_sync_runs_app(service=service, resolve_access=lambda _req: access)
        with TestClient(app) as c:
            barrier_2.wait()
            return c.post(
                f"/v1/sync-runs/{run_id}/retry",
                headers={"Idempotency-Key": same_key},
                json={"expected_run_version": version_c},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_results = list(pool.map(submit_same_retry, (0, 1)))
    assert [r.status_code for r in same_results] == [202, 202]
    assert same_results[0].json() == same_results[1].json()
    assert _table_counts(clean_db)["jobs"] == jobs_before_same + 1


def test_contract_error_envelopes_422_concealment_recovery_ordering_and_states(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access_a, account_a, repo_a1 = seed_workspace_with_repo(
        clean_db, workspace_name="WS-A", repo_name="acme/repo-a1"
    )
    repo_a2 = add_repo_to_workspace(
        clean_db,
        workspace_id=access_a.workspace_id,
        source_account_id=account_a,
        repo_name="acme/repo-a2",
    )
    access_b, _account_b, repo_b = seed_workspace_with_repo(
        clean_db, workspace_name="WS-B", repo_name="other/repo-b"
    )
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    service = _make_service(clean_db, clock=clock)

    current_auth: object = access_a
    unreadable_runs: set[str] = set()

    def resolve_auth(_req):
        if isinstance(current_auth, Exception):
            raise current_auth
        return current_auth

    client_a = TestClient(
        create_sync_runs_app(
            service=service,
            resolve_access=resolve_auth,
            resolve_unreadable_runs=lambda _req: frozenset(unreadable_runs),
        )
    )
    client_b = TestClient(
        create_sync_runs_app(service=service, resolve_access=lambda _req: access_b)
    )

    # Empty recovery on valid repo with no runs
    empty_rec = client_a.get(
        "/v1/sync-runs", params={"source_resource_id": repo_a1, "limit": "1"}
    )
    assert empty_rec.status_code == 200
    assert empty_rec.headers["Cache-Control"] == "private, no-store"
    assert empty_rec.json() == {"items": []}

    # 422 validation checks (missing/invalid idempotency key, bad date, bodyless retry, bad limit)
    for bad_params in ({}, {"source_resource_id": ""}, {"source_resource_id": repo_a1, "limit": "2"}):
        r = client_a.get("/v1/sync-runs", params=bad_params)
        assert r.status_code == 422 and r.content == b""
    assert client_a.post(
        "/v1/sync-runs",
        json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
    ).status_code == 422
    assert client_a.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": "not-uuid"},
        json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
    ).status_code == 422
    assert client_a.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={
            "source_resource_id": repo_a1,
            "mode": "INITIAL_BACKFILL",
            "backfill_from": "2025-02-30",
        },
    ).status_code == 422

    # Open run on WS-A and WS-B
    run_a = client_a.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1, "X-Request-ID": "req-a"},
        json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
    ).json()["target_id"]
    run_b = client_b.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo_b, "mode": "INITIAL_BACKFILL"},
    ).json()["target_id"]

    # Cross-workspace concealment: foreign repo, unknown repo, foreign run, unknown run, unreadable run
    for resp in (
        client_a.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": KEY_2},
            json={"source_resource_id": repo_b, "mode": "INITIAL_BACKFILL"},
        ),
        client_a.get("/v1/sync-runs", params={"source_resource_id": repo_b}),
        client_a.get(f"/v1/sync-runs/{run_b}"),
        client_a.get(f"/v1/sync-runs/{uuid.uuid4()}"),
        client_a.post(
            f"/v1/sync-runs/{run_b}/retry",
            headers={"Idempotency-Key": KEY_2},
            json={"expected_run_version": 1},
        ),
    ):
        assert resp.status_code == 404
        assert resp.headers["Cache-Control"] == "private, no-store"
        err = resp.json()["error"]
        assert err["code"] == "RESOURCE_NOT_FOUND"
        assert err["retryable"] is False
        assert "details" not in err

    # Unreadable active run hides existing_run_id in SYNC_ALREADY_RUNNING
    unreadable_runs.add(run_a)
    conflict_hidden = client_a.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_2},
        json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
    )
    assert conflict_hidden.status_code == 409
    assert conflict_hidden.json()["error"]["code"] == "SYNC_ALREADY_RUNNING"
    assert "details" not in conflict_hidden.json()["error"]
    assert client_a.get("/v1/sync-runs", params={"source_resource_id": repo_a1}).json() == {"items": []}
    unreadable_runs.clear()

    # Bodyless retry / version 0 -> 422 empty
    for bad_retry in (
        client_a.post(f"/v1/sync-runs/{run_a}/retry", headers={"Idempotency-Key": KEY_2}),
        client_a.post(f"/v1/sync-runs/{run_a}/retry", headers={"Idempotency-Key": KEY_2}, json={}),
        client_a.post(
            f"/v1/sync-runs/{run_a}/retry",
            headers={"Idempotency-Key": KEY_2},
            json={"expected_run_version": 0},
        ),
    ):
        assert bad_retry.status_code == 422 and bad_retry.content == b""

    # Non-retryable states: PARTIAL_WITH_LIMIT, PLAN_DRIFT, SOURCE_POLICY_BLOCKED, policyChangedAfterSuccess
    for obs, expected_status, expected_reason, expected_drift in (
        (
            SyncRunObservation(
                status=SyncRunStatus.PARTIAL_WITH_LIMIT,
                failure_reason="SYNC_PARTIAL_WITH_LIMIT",
                remaining_range="2026-09-01 through 2026-10-05",
                processed_range="2025-09-23 through 2026-08-31",
            ),
            "PARTIAL_WITH_LIMIT",
            "SYNC_PARTIAL_WITH_LIMIT",
            False,
        ),
        (
            SyncRunObservation(
                status=SyncRunStatus.PARTIAL,
                failure_reason="PLAN_DRIFT",
                source_drift=True,
            ),
            "PARTIAL",
            "PLAN_DRIFT",
            True,
        ),
        (
            SyncRunObservation(
                status=SyncRunStatus.PARTIAL,
                failure_reason="SOURCE_POLICY_BLOCKED",
            ),
            "PARTIAL",
            "SOURCE_POLICY_BLOCKED",
            False,
        ),
    ):
        service.record_worker_observation(access_a.workspace_id, run_a, obs)
        st = client_a.get(f"/v1/sync-runs/{run_a}").json()
        assert st["status"] == expected_status
        assert st["failure_reason"] == expected_reason
        assert st["source_drift"] is expected_drift
        assert st["retry"] == {"allowed": False}
        assert client_a.post(
            f"/v1/sync-runs/{run_a}/retry",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"expected_run_version": st["run_version"]},
        ).status_code == 409
        assert client_a.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
        ).status_code == 409

    # Policy changed after SUCCESS preserves import_ready=True and rejects new import/retry
    service.record_worker_observation(
        access_a.workspace_id,
        run_a,
        SyncRunObservation(
            status=SyncRunStatus.SUCCESS,
            import_ready=True,
            processing_status=ProcessingStatus.BLOCKED_POLICY,
        ),
    )
    st_succ = client_a.get(f"/v1/sync-runs/{run_a}").json()
    assert st_succ["status"] == "SUCCESS"
    assert st_succ["import_ready"] is True
    assert st_succ["processing_status"] == "BLOCKED_POLICY"
    assert "failure_reason" not in st_succ
    assert client_a.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"source_resource_id": repo_a1, "mode": "INITIAL_BACKFILL"},
    ).status_code == 409

    # Auth error envelopes (401 SESSION_TERMINATED, 403 WORKSPACE_SCOPE_VIOLATION, 503 AUTH_VERIFICATION_UNAVAILABLE)
    counts_before_auth = _table_counts(clean_db)
    for exc, expected_code_num, expected_err_code, expected_retryable in (
        (SessionTerminatedError(), 401, "SESSION_TERMINATED", False),
        (WorkspaceMembershipDeniedError(), 403, "WORKSPACE_SCOPE_VIOLATION", False),
        (AccessVerificationUnavailableError(), 503, "AUTH_VERIFICATION_UNAVAILABLE", True),
    ):
        current_auth = exc
        for r in (
            client_a.post(
                "/v1/sync-runs",
                headers={"Idempotency-Key": str(uuid.uuid4()), "X-Request-ID": "auth-req"},
                json={"source_resource_id": repo_a2, "mode": "INITIAL_BACKFILL"},
            ),
            client_a.get(
                "/v1/sync-runs",
                headers={"X-Request-ID": "auth-req"},
                params={"source_resource_id": repo_a1},
            ),
            client_a.get(f"/v1/sync-runs/{run_a}", headers={"X-Request-ID": "auth-req"}),
            client_a.post(
                f"/v1/sync-runs/{run_a}/retry",
                headers={"Idempotency-Key": str(uuid.uuid4()), "X-Request-ID": "auth-req"},
                json={"expected_run_version": st_succ["run_version"]},
            ),
        ):
            assert r.status_code == expected_code_num
            assert r.headers["Cache-Control"] == "private, no-store"
            assert r.headers["X-Request-ID"] == "auth-req"
            err = r.json()["error"]
            assert err["code"] == expected_err_code
            assert err["retryable"] is expected_retryable
            assert err["request_id"] == "auth-req"
            assert "details" not in err
    current_auth = UnauthenticatedError()
    unauth_resp = client_a.get("/v1/sync-runs", params={"source_resource_id": repo_a1})
    assert unauth_resp.status_code == 401 and unauth_resp.content == b""
    assert _table_counts(clean_db) == counts_before_auth


def test_revocation_and_deletion_fence_block_replays_and_cancel_inflight_jobs(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, _account_id, repo_id = seed_workspace_with_repo(clean_db)
    clock = MutableClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    service = _make_service(clean_db, clock=clock)
    runtime = PostgresJobRuntime(connection_factory=clean_db.worker_connection)
    client = TestClient(
        create_sync_runs_app(service=service, resolve_access=lambda _req: access)
    )

    # 1. Create initial run and claim job on worker
    created = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    assert created.status_code == 202
    run_id = created.json()["target_id"]
    job_id = created.json()["job_id"]

    claimed = runtime.claim_next_job(
        access.workspace_id, worker_id="worker-1", now=clock()
    )
    assert claimed is not None and claimed.job.id == job_id

    # 2. Activate deletion fence while job is RUNNING!
    del_job = runtime.request_source_deletion(
        access.workspace_id,
        source_resource_id=repo_id,
        requested_by=access.user_id,
        now=clock() + timedelta(seconds=2),
    )
    assert del_job.status.value == "PENDING"
    counts_after_fence = _table_counts(clean_db)

    # Existing idempotency key replay AND new request MUST be blocked with zero writes!
    replay_blocked = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    assert replay_blocked.status_code == 404
    assert replay_blocked.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    new_blocked = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_2},
        json={"source_resource_id": repo_id, "mode": "INITIAL_BACKFILL"},
    )
    assert new_blocked.status_code == 404
    assert _table_counts(clean_db) == counts_after_fence

    # In-flight worker tries to commit side effect -> deletion fence intercepts and CANCELS job & run!
    from career_memory_api.jobs.models import DeletionFenceActiveError

    with pytest.raises(DeletionFenceActiveError):
        runtime.complete_job_with_effect(
            access.workspace_id,
            job_id=job_id,
            lease_token=claimed.lease_token,
            sync_run_success=True,
            now=clock() + timedelta(seconds=5),
        )

    with clean_db.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, error_class FROM public.jobs WHERE id = %s::uuid", (job_id,))
            assert cur.fetchone() == ("CANCELED", "CANCELED_BY_FENCE")
            cur.execute("SELECT status FROM public.sync_runs WHERE id = %s::uuid", (run_id,))
            assert cur.fetchone() == ("CANCELED",)
            cur.execute("SELECT count(*) FROM public.job_side_effect_ledger")
            assert cur.fetchone() == (0,)

    # 3. Membership revocation mid-session blocks replay & new create with 403 WORKSPACE_SCOPE_VIOLATION
    access2, _acc2, repo2 = seed_workspace_with_repo(
        clean_db, workspace_name="WS-Revoke", repo_name="acme/revoke-repo"
    )
    client2 = TestClient(
        create_sync_runs_app(service=service, resolve_access=lambda _req: access2)
    )
    ok2 = client2.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo2, "mode": "INITIAL_BACKFILL"},
    )
    assert ok2.status_code == 202
    counts_before_revoke = _table_counts(clean_db)

    revoke_membership(
        clean_db,
        workspace_id=access2.workspace_id,
        user_id=access2.user_id,
        revoked_at=clock() + timedelta(seconds=10),
    )

    # Even with stale cached WorkspaceAccessContext, DB live membership check blocks replay & new request!
    for resp in (
        client2.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": KEY_1},
            json={"source_resource_id": repo2, "mode": "INITIAL_BACKFILL"},
        ),
        client2.post(
            "/v1/sync-runs",
            headers={"Idempotency-Key": KEY_2},
            json={"source_resource_id": repo2, "mode": "INITIAL_BACKFILL"},
        ),
    ):
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "WORKSPACE_SCOPE_VIOLATION"
    assert _table_counts(clean_db) == counts_before_revoke


def test_recovery_prioritizes_active_then_latest_terminal_and_cross_run_retry_key_conflicts(
    clean_db: EphemeralPg18Cluster,
) -> None:
    access, account_id, repo_1 = seed_workspace_with_repo(
        clean_db, workspace_name="WS-Order", repo_name="acme/order-1"
    )
    repo_2 = add_repo_to_workspace(
        clean_db,
        workspace_id=access.workspace_id,
        source_account_id=account_id,
        repo_name="acme/order-2",
    )
    clock = MutableClock(datetime(2026, 10, 5, 16, 0, tzinfo=UTC))
    service = _make_service(clean_db, clock=clock)
    client = TestClient(
        create_sync_runs_app(service=service, resolve_access=lambda _req: access)
    )

    # Open run 1 on repo_1, fail transiently, mark abandoned to seed history, then open run 2
    run_1 = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_1},
        json={"source_resource_id": repo_1, "mode": "INITIAL_BACKFILL"},
    ).json()["target_id"]
    service.record_worker_observation(
        access.workspace_id, run_1, _transient_partial_observation(clock())
    )
    with clean_db.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.sync_runs
                    SET is_abandoned = true, retry_allowed = false, run_version = run_version + 1
                    WHERE id = %s::uuid
                    """,
                    (run_1,),
                )

    clock.advance(timedelta(seconds=5))
    run_2 = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_2},
        json={"source_resource_id": repo_1, "mode": "INITIAL_BACKFILL"},
    ).json()["target_id"]

    # Active run_2 wins over terminal run_1
    items = client.get("/v1/sync-runs", params={"source_resource_id": repo_1}).json()["items"]
    assert len(items) == 1 and items[0]["target_id"] == run_2

    # Transition run_2 to terminal PARTIAL at the exact same terminal timestamp as run_1 -> newest created_at wins!
    service.record_worker_observation(
        access.workspace_id,
        run_2,
        _transient_partial_observation(clock()),
        now=datetime(2026, 10, 5, 16, 0, tzinfo=UTC),
    )
    items_terminal = client.get(
        "/v1/sync-runs", params={"source_resource_id": repo_1}
    ).json()["items"]
    assert len(items_terminal) == 1 and items_terminal[0]["target_id"] == run_2

    # Same retry Idempotency-Key used on run_2 then on another run on repo_2 -> 409 IDEMPOTENCY_CONFLICT!
    run_repo2 = client.post(
        "/v1/sync-runs",
        headers={"Idempotency-Key": KEY_3},
        json={"source_resource_id": repo_2, "mode": "INITIAL_BACKFILL"},
    ).json()["target_id"]
    service.record_worker_observation(
        access.workspace_id, run_repo2, _transient_partial_observation(clock())
    )

    v2 = client.get(f"/v1/sync-runs/{run_2}").json()["run_version"]
    v_other = client.get(f"/v1/sync-runs/{run_repo2}").json()["run_version"]
    assert (
        client.post(
            f"/v1/sync-runs/{run_2}/retry",
            headers={"Idempotency-Key": KEY_4},
            json={"expected_run_version": v2},
        ).status_code
        == 202
    )
    cross_run_conflict = client.post(
        f"/v1/sync-runs/{run_repo2}/retry",
        headers={"Idempotency-Key": KEY_4},
        json={"expected_run_version": v_other},
    )
    assert cross_run_conflict.status_code == 409
    assert cross_run_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.get(f"/v1/sync-runs/{run_repo2}").json()["status"] == "PARTIAL"
