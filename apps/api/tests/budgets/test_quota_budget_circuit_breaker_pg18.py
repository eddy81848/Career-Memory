"""Real PostgreSQL 18 & HTTP tests for M5 Quotas, Budgets, Circuit Breaker & Pause Controls (T-503 / PERF-01 / INF-06 / INF-03)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.budgets import (
    BudgetCutoffError,
    BudgetOperationKind,
    CircuitBreakerConfig,
    CircuitBreakerState,
    DEFAULT_BUDGET_FREE_ALTERNATIVES,
    DefaultQuotaPolicyConfig,
    DeliveryAmbiguityOutcome,
    ManualResumeVerificationError,
    OperationLimitExceededError,
    OperationUsageObservation,
    PostgresQuotaBudgetService,
    ProviderCircuitBreaker,
    QuotaAdmissionState,
    QuotaMeterStatus,
    QuotaMetricLimit,
    QuotaStatusResponse,
    classify_external_call_outcome,
    create_budgets_router,
    enforce_operation_budget,
)
from career_memory_api.conformance.openapi_spec import generate_integrated_openapi_spec
from career_memory_api.contracts_api.models import DeletionConfirmRequest
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.deletion import PostgresDeletionService
from career_memory_api.edge_guard import (
    CorsCsrfConfig,
    EdgeGuardConfig,
    EdgeRuntimeMode,
    ManualEdgePauseController,
    OriginAuthConfig,
    RouteTrustClass,
    classify_route,
)
from career_memory_api.resolver.policy import OperationBudgetConfig
from career_memory_api.schema_contracts.enums import QuotaMetricKind, RuntimePauseState
from tests.backfill_engine.pg_harness import (
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_0010_TO_0080 = (
    *MIGRATIONS_0010_TO_0050,
    REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql",
    REPO_ROOT / "db" / "migrations" / "0070_m4_fact_versions_and_exports.sql",
    REPO_ROOT / "db" / "migrations" / "0080_m5_search_deletion_and_budgets.sql",
)
BASE_NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)


class _MutableMonotonicClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _MutableUtcClock:
    def __init__(self, start: datetime = BASE_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


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


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralBackfillPg18Cluster]:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m5-budgets-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0080:
            cluster.apply_sql_file(mig)
        cluster.provision_runtime_logins()
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture()
def clean_db(pg18_cluster: EphemeralBackfillPg18Cluster) -> None:
    pg18_cluster.truncate_all()


# =============================================================================
# 1. Pure Unit Tests (INF-03 Circuit Breaker, Ambiguous Timeout, PERF-01 Bounds)
# =============================================================================
def test_circuit_breaker_closed_open_half_open_closed_and_ambiguous_billable_guard() -> None:
    mono = _MutableMonotonicClock(1000.0)
    cb = ProviderCircuitBreaker(
        config=CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout_seconds=30.0,
            half_open_max_probes=1,
        ),
        clock=mono,
    )

    assert cb.state is CircuitBreakerState.CLOSED
    assert cb.allow_request() == (True, None)

    # INF-03: Ambiguous timeout on potentially billable/mutating call MUST NEVER auto-retry!
    ambiguous_timeout = classify_external_call_outcome(
        timed_out=True,
        is_billable_or_mutating=True,
        idempotent_retry_safe=False,
    )
    assert ambiguous_timeout.outcome is DeliveryAmbiguityOutcome.AMBIGUOUS_DELIVERY_NO_AUTO_RETRY
    assert ambiguous_timeout.auto_retry_allowed is False
    assert ambiguous_timeout.counts_as_circuit_failure is True
    assert ambiguous_timeout.reason_code == "AMBIGUOUS_DELIVERY_NO_AUTO_RETRY"

    ambiguous_504 = classify_external_call_outcome(
        http_status=504,
        is_billable_or_mutating=True,
    )
    assert ambiguous_504.outcome is DeliveryAmbiguityOutcome.AMBIGUOUS_DELIVERY_NO_AUTO_RETRY
    assert ambiguous_504.auto_retry_allowed is False

    # Non-billable idempotent read timeout may be retried within budget
    read_timeout = classify_external_call_outcome(
        timed_out=True,
        is_billable_or_mutating=False,
    )
    assert read_timeout.outcome is DeliveryAmbiguityOutcome.CONFIRMED_FAILURE_RETRYABLE
    assert read_timeout.auto_retry_allowed is True

    # 3 consecutive failures trip CLOSED -> OPEN
    cb.record_outcome(ambiguous_timeout)
    assert cb.state is CircuitBreakerState.CLOSED
    cb.record_outcome(classify_external_call_outcome(http_status=503))
    assert cb.state is CircuitBreakerState.CLOSED
    cb.record_outcome(classify_external_call_outcome(http_status=429))
    assert cb.state is CircuitBreakerState.OPEN

    allowed, retry_after = cb.allow_request()
    assert allowed is False
    assert retry_after == 30

    # Advance past recovery_timeout_seconds -> HALF_OPEN admits exactly 1 probe
    mono.advance(30.0)
    assert cb.state is CircuitBreakerState.HALF_OPEN
    assert cb.allow_request() == (True, None)
    # Concurrent 2nd request while probe in flight is blocked
    allowed_second, retry_second = cb.allow_request()
    assert allowed_second is False
    assert retry_second is not None and retry_second >= 1

    # Probe failure re-opens circuit
    cb.record_failure(reason_code="PROBE_503")
    assert cb.state is CircuitBreakerState.OPEN
    mono.advance(30.0)
    assert cb.allow_request() == (True, None)

    # Probe success restores CLOSED
    cb.record_success()
    assert cb.state is CircuitBreakerState.CLOSED
    assert cb.snapshot().consecutive_failures == 0


def test_operation_budget_config_enforces_perf01_item_size_call_byte_and_concurrency_bounds() -> None:
    op_budget = OperationBudgetConfig(
        max_requests=5,
        max_total_bytes=4096,
        max_unit_bytes=1024,
        max_concurrency=2,
        max_tokens=500,
    )
    # Valid observation passes cleanly
    enforce_operation_budget(
        op_budget,
        OperationUsageObservation(
            item_count=10,
            request_count=2,
            total_bytes=2048,
            unit_bytes=512,
            concurrency=2,
            tokens=100,
        ),
        max_items=50,
    )

    # Oversized unit bytes, item count, requests, concurrency each fail deterministically
    cases = [
        (OperationUsageObservation(item_count=51), "max_items"),
        (OperationUsageObservation(request_count=6), "max_requests"),
        (OperationUsageObservation(total_bytes=5000), "max_total_bytes"),
        (OperationUsageObservation(unit_bytes=2048), "max_unit_bytes"),
        (OperationUsageObservation(concurrency=3), "max_concurrency"),
        (OperationUsageObservation(tokens=600), "max_tokens"),
    ]
    for obs, expected_limit in cases:
        with pytest.raises(OperationLimitExceededError) as exc_info:
            enforce_operation_budget(op_budget, obs, max_items=50)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "OPERATION_BUDGET_LIMIT_EXCEEDED"
        assert exc_info.value.details is not None
        assert exc_info.value.details["limit_name"] == expected_limit
        assert exc_info.value.paid_fallback_allowed is False


# =============================================================================
# 2. Real PostgreSQL 18 Quota Ledger, 80%/95% Transitions & Safety Preservation
# =============================================================================
@pytest.mark.postgres
def test_pg18_quota_transitions_80_warning_95_cutoff_and_safety_reservation(
    pg18_cluster: EphemeralBackfillPg18Cluster,
    clean_db: None,  # noqa: ARG001
) -> None:
    seed_a = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/quota-alpha")
    seed_b = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/quota-beta")
    access_a = _access_for(seed_a)
    access_b = _access_for(seed_b)

    policy = DefaultQuotaPolicyConfig(
        limits={
            QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
                soft_warning_limit=80,
                hard_cutoff_limit=95,
            ),
            QuotaMetricKind.ESTIMATED_COST_KRW: QuotaMetricLimit(
                soft_warning_limit=16_000,
                hard_cutoff_limit=19_000,
            ),
        },
        auto_pause_on_cutoff=True,
        default_retry_after_seconds=120,
    )
    clock = _MutableUtcClock(BASE_NOW)
    service = PostgresQuotaBudgetService(
        api_connection_factory=pg18_cluster.api_connection,
        policy_config=policy,
        now_fn=clock,
    )

    # 1. Fresh workspace starts at 0% -> ALLOWED
    status_0 = service.get_quota_status(access_a)
    assert status_0.workspace_id == access_a.workspace_id
    assert status_0.period_key == "2026-11"
    assert status_0.admission_state is QuotaAdmissionState.ALLOWED
    assert status_0.pause_state is RuntimePauseState.ACTIVE
    assert status_0.circuit_breaker_state is CircuitBreakerState.CLOSED
    assert status_0.high_cost_operations_allowed is True
    assert status_0.safety_operations_allowed is True
    assert status_0.paid_fallback_allowed is False
    assert len(status_0.meters) == 7

    # Live LLM tokens default to 0/0: any positive LLM token reservation is blocked fail-closed!
    with pytest.raises(BudgetCutoffError) as llm_exc:
        service.check_and_reserve_budget(
            access_a,
            operation_kind=BudgetOperationKind.CLAIM_GENERATION,
            deltas={QuotaMetricKind.LLM_TOKENS: 100},
        )
    assert llm_exc.value.status_code == 429
    assert llm_exc.value.code == "BUDGET_CUTOFF_REACHED"
    assert llm_exc.value.paid_fallback_allowed is False

    # 2. Cross 80% threshold -> WARNING_80_PERCENT (still allowed)
    res_80 = service.check_and_reserve_budget(
        access_a,
        operation_kind=BudgetOperationKind.INITIAL_IMPORT,
        deltas={QuotaMetricKind.IMPORT_OBJECTS: 80},
    )
    assert res_80.allowed is True
    assert res_80.admission_state is QuotaAdmissionState.WARNING_80_PERCENT

    status_80 = service.get_quota_status(access_a)
    assert status_80.admission_state is QuotaAdmissionState.WARNING_80_PERCENT
    assert status_80.high_cost_operations_allowed is True
    import_meter_80 = next(
        m for m in status_80.meters if m.metric_kind is QuotaMetricKind.IMPORT_OBJECTS
    )
    assert import_meter_80.used_value == 80
    assert import_meter_80.utilization_percent == 80.0
    assert import_meter_80.status is QuotaMeterStatus.WARNING

    # 3. Cross 95% hard cutoff -> CUTOFF_95_PERCENT & auto-pause PAUSED_QUOTA
    res_95 = service.check_and_reserve_budget(
        access_a,
        operation_kind=BudgetOperationKind.INITIAL_IMPORT,
        deltas={QuotaMetricKind.IMPORT_OBJECTS: 15},
    )
    assert res_95.allowed is True
    assert res_95.admission_state is QuotaAdmissionState.CUTOFF_95_PERCENT

    status_95 = service.get_quota_status(access_a)
    assert status_95.admission_state is QuotaAdmissionState.CUTOFF_95_PERCENT
    assert status_95.pause_state is RuntimePauseState.PAUSED_QUOTA
    assert status_95.high_cost_operations_allowed is False
    assert status_95.safety_operations_allowed is True
    assert status_95.paid_fallback_allowed is False
    assert status_95.retry_after_seconds == 120
    assert set(DEFAULT_BUDGET_FREE_ALTERNATIVES).issubset(set(status_95.free_alternatives))

    # 4. Subsequent high-cost admission is blocked BEFORE execution (zero side effects!)
    side_effect_executed = False
    with pytest.raises(BudgetCutoffError) as cutoff_exc:
        service.check_and_reserve_budget(
            access_a,
            operation_kind=BudgetOperationKind.EXPORT_CREATE,
            deltas={QuotaMetricKind.EXPORT_BUILDS: 1},
        )
        side_effect_executed = True  # pragma: no cover
    assert side_effect_executed is False
    assert cutoff_exc.value.status_code == 429
    assert cutoff_exc.value.code == "BUDGET_CUTOFF_REACHED"
    assert cutoff_exc.value.retry_after_seconds == 120
    assert cutoff_exc.value.paid_fallback_allowed is False
    assert len(cutoff_exc.value.free_alternatives) >= 3

    # 5. Safety Reservation Rule: ALL safety operations stay ALLOWED under 95% cutoff & PAUSED_QUOTA!
    for safety_op in (
        BudgetOperationKind.SOURCE_DISCONNECT,
        BudgetOperationKind.DELETION_PREVIEW,
        BudgetOperationKind.DELETION_CONFIRM,
        BudgetOperationKind.DELETION_EXECUTE,
        BudgetOperationKind.AUTH_REVOKE,
        BudgetOperationKind.HEALTH_CHECK,
        BudgetOperationKind.QUOTA_READ,
    ):
        sres = service.check_and_reserve_budget(
            access_a,
            operation_kind=safety_op,
            deltas={QuotaMetricKind.IMPORT_OBJECTS: 10},
        )
        assert sres.allowed is True
        assert sres.safety_bypass is True

    # 6. Cross-workspace RLS isolation: Workspace B stays completely unaffected (ALLOWED)!
    status_b = service.get_quota_status(access_b)
    assert status_b.workspace_id == access_b.workspace_id
    assert status_b.admission_state is QuotaAdmissionState.ALLOWED
    assert status_b.pause_state is RuntimePauseState.ACTIVE
    assert status_b.high_cost_operations_allowed is True


@pytest.mark.postgres
def test_pg18_operator_pause_manual_resume_verification_and_fail_closed(
    pg18_cluster: EphemeralBackfillPg18Cluster,
    clean_db: None,  # noqa: ARG001
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/operator-pause")
    access = _access_for(seed)

    service = PostgresQuotaBudgetService(
        api_connection_factory=pg18_cluster.api_connection,
        policy_config=DefaultQuotaPolicyConfig(
            limits={
                QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
                    soft_warning_limit=16,
                    hard_cutoff_limit=19,
                )
            }
        ),
        now_fn=lambda: BASE_NOW,
    )

    # 1. Manual operator pause
    paused = service.pause_runtime(
        access,
        pause_state=RuntimePauseState.PAUSED_OPERATOR,
        reason_code="OPERATOR_COST_INVESTIGATION",
    )
    assert paused.admission_state is QuotaAdmissionState.PAUSED
    assert paused.pause_state is RuntimePauseState.PAUSED_OPERATOR
    assert paused.pause_reason_code == "OPERATOR_COST_INVESTIGATION"
    assert paused.high_cost_operations_allowed is False
    assert paused.safety_operations_allowed is True

    # High-cost blocked with 503 RUNTIME_PAUSED
    with pytest.raises(BudgetCutoffError) as exc_info:
        service.check_and_reserve_budget(
            access,
            operation_kind=BudgetOperationKind.INITIAL_IMPORT,
            deltas={QuotaMetricKind.IMPORT_OBJECTS: 1},
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "RUNTIME_PAUSED"

    # 2. Resume with budget_verified=False MUST fail with 409 MANUAL_RESUME_VERIFICATION_REQUIRED
    with pytest.raises(ManualResumeVerificationError) as resume_err:
        service.resume_runtime(access, budget_verified=False)
    assert resume_err.value.status_code == 409
    assert resume_err.value.code == "MANUAL_RESUME_VERIFICATION_REQUIRED"

    # Still paused!
    assert service.get_quota_status(access).pause_state is RuntimePauseState.PAUSED_OPERATOR

    # 3. Resume with budget_verified=True succeeds when under cutoff & writes audit_events
    resumed = service.resume_runtime(
        access,
        budget_verified=True,
        operator_note="Verified ₩0 spend and reset headroom",
    )
    assert resumed.admission_state is QuotaAdmissionState.ALLOWED
    assert resumed.pause_state is RuntimePauseState.ACTIVE
    assert resumed.high_cost_operations_allowed is True

    with pg18_cluster.admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action
                FROM public.audit_events
                WHERE workspace_id = %s::uuid
                  AND action IN ('RUNTIME_PAUSED', 'RUNTIME_RESUMED')
                ORDER BY created_at ASC, action ASC
                """,
                (access.workspace_id,),
            )
            actions = {str(r[0]) for r in cur.fetchall()}
    assert actions == {"RUNTIME_PAUSED", "RUNTIME_RESUMED"}

    # 4. Hit hard cutoff -> resume(budget_verified=True) MUST be rejected until headroom is raised!
    service.check_and_reserve_budget(
        access,
        operation_kind=BudgetOperationKind.INITIAL_IMPORT,
        deltas={QuotaMetricKind.IMPORT_OBJECTS: 19},
    )
    with pytest.raises(BudgetCutoffError) as still_cut:
        service.resume_runtime(access, budget_verified=True)
    assert still_cut.value.status_code == 409
    assert still_cut.value.code == "BUDGET_CUTOFF_STILL_EXCEEDED"

    # Raise limit headroom, then resume(budget_verified=True) succeeds
    service.update_quota_limit(
        access,
        metric_kind=QuotaMetricKind.IMPORT_OBJECTS,
        soft_warning_limit=80,
        hard_cutoff_limit=95,
    )
    after_headroom = service.resume_runtime(access, budget_verified=True)
    assert after_headroom.pause_state is RuntimePauseState.ACTIVE
    assert after_headroom.admission_state is QuotaAdmissionState.ALLOWED

    # 5. Fail-closed test: broken DB connection factory blocks non-safety ops, allows safety ops
    @contextmanager
    def _broken_conn() -> Iterator[psycopg.Connection]:
        raise RuntimeError("DB_CONNECTION_POOL_EXHAUSTED")
        yield  # pragma: no cover

    broken_svc = PostgresQuotaBudgetService(api_connection_factory=_broken_conn)
    with pytest.raises(BudgetCutoffError) as fail_closed:
        broken_svc.check_and_reserve_budget(
            access,
            operation_kind=BudgetOperationKind.CLAIM_GENERATION,
            deltas={QuotaMetricKind.PROVIDER_REQUESTS: 1},
        )
    assert fail_closed.value.status_code == 503
    assert fail_closed.value.code == "BUDGET_VERIFICATION_UNAVAILABLE"
    assert fail_closed.value.paid_fallback_allowed is False

    safety_ok = broken_svc.check_and_reserve_budget(
        access,
        operation_kind=BudgetOperationKind.DELETION_CONFIRM,
    )
    assert safety_ok.allowed is True
    assert safety_ok.safety_bypass is True


# =============================================================================
# 3. HTTP GET /v1/quota-status, Safety Routes under Pause & OpenAPI Conformance
# =============================================================================
@pytest.mark.postgres
def test_http_quota_status_safety_routes_under_pause_and_openapi_conformance(
    pg18_cluster: EphemeralBackfillPg18Cluster,
    clean_db: None,  # noqa: ARG001
) -> None:
    seed = seed_workspace_repo_and_sync_run(pg18_cluster, repo_name="acme/http-quota-repo")
    access = _access_for(seed)

    budgets_svc = PostgresQuotaBudgetService(
        api_connection_factory=pg18_cluster.api_connection,
        now_fn=lambda: BASE_NOW,
    )
    deletion_svc = PostgresDeletionService(
        api_connection_factory=pg18_cluster.api_connection,
        worker_connection_factory=pg18_cluster.worker_connection,
        now_fn=lambda: BASE_NOW,
    )
    pause_ctrl = ManualEdgePauseController()
    edge_cfg = EdgeGuardConfig(
        origin_auth=OriginAuthConfig(require_origin_auth=False),
        cors_csrf=CorsCsrfConfig(enforce_csrf_on_state_changing=False),
    )

    app = create_m1_contract_app(
        deletion_service=deletion_svc,
        budgets_service=budgets_svc,
        resolve_access=lambda _req: access,
        edge_guard_config=edge_cfg,
        edge_pause_controller=pause_ctrl,
    )

    # Mount a lightweight disconnect handler to prove POST /v1/source-accounts/{id}/disconnect stays 200
    @app.post("/v1/source-accounts/{account_id}/disconnect")
    async def _disconnect_route(account_id: str) -> dict[str, Any]:
        return {"account_id": account_id, "disconnected": True}

    client = TestClient(app)

    # Verify edge_guard route classification for /v1/quota-status & /v1/search
    assert classify_route("GET", "/v1/quota-status").trust_class is RouteTrustClass.AUTHENTICATED_API
    assert classify_route("GET", "/v1/search").trust_class is RouteTrustClass.AUTHENTICATED_API

    # Engage BOTH DB PAUSED_OPERATOR and Edge PAUSED_COST_GUARD!
    budgets_svc.pause_runtime(
        access,
        pause_state=RuntimePauseState.PAUSED_OPERATOR,
        reason_code="EMERGENCY_SPEND_CAP",
    )
    pause_ctrl.pause("EMERGENCY_SPEND_CAP", access.user_id, mode=EdgeRuntimeMode.PAUSED_COST_GUARD)

    # 1. GET /healthz -> 200 ALLOWED
    r_health = client.get("/healthz")
    assert r_health.status_code == 200

    # 2. GET /v1/quota-status -> 200 ALLOWED with private, no-store and exact wire schema
    r_quota = client.get("/v1/quota-status")
    assert r_quota.status_code == 200
    assert r_quota.headers["cache-control"] == "private, no-store"
    parsed_quota = QuotaStatusResponse.model_validate(r_quota.json())
    assert parsed_quota.workspace_id == access.workspace_id
    assert parsed_quota.admission_state is QuotaAdmissionState.PAUSED
    assert parsed_quota.pause_state is RuntimePauseState.PAUSED_OPERATOR
    assert parsed_quota.high_cost_operations_allowed is False
    assert parsed_quota.safety_operations_allowed is True
    assert parsed_quota.paid_fallback_allowed is False
    assert parsed_quota.retry_after_seconds is not None and parsed_quota.retry_after_seconds >= 1

    # 3. High-cost route POST /v1/exports -> 503 RUNTIME_PAUSED_COST_GUARD
    r_export = client.post("/v1/exports", json={})
    assert r_export.status_code == 503
    assert r_export.json()["error"]["code"] == "RUNTIME_PAUSED_COST_GUARD"
    assert r_export.json()["error"]["paid_fallback_allowed"] is False

    # 4. Essential safety routes remain 200/202 ALLOWED under PAUSED_COST_GUARD + PAUSED_OPERATOR!
    r_disc = client.post(f"/v1/source-accounts/{seed['source_account_id']}/disconnect")
    assert r_disc.status_code == 200

    r_prev = client.post(f"/v1/source-resources/{seed['source_resource_id']}/deletion-preview")
    assert r_prev.status_code == 200
    prev_body = r_prev.json()

    r_conf = client.post(
        f"/v1/source-resources/{seed['source_resource_id']}/deletion-confirm",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json=DeletionConfirmRequest(
            preview_token=prev_body["preview_token"],
            expected_resource_version=prev_body["resource_version"],
            expected_impact_scope_version=prev_body["impact_scope_version"],
            acknowledge_irreversible_purge=True,
        ).model_dump(mode="json"),
    )
    assert r_conf.status_code == 202
    del_job_id = r_conf.json()["deletion_job_id"]

    r_del_status = client.get(f"/v1/deletion-jobs/{del_job_id}")
    assert r_del_status.status_code == 200
    assert r_del_status.json()["fence_active"] is True

    # 5. OpenAPI conformance check: GET /v1/quota-status & QuotaStatusResponse present
    spec = generate_integrated_openapi_spec()
    assert "/v1/quota-status" in spec["paths"]
    assert "get" in spec["paths"]["/v1/quota-status"]
    assert spec["paths"]["/v1/quota-status"]["get"]["operationId"] == "getWorkspaceQuotaStatus"
    for schema_name in (
        "QuotaStatusResponse",
        "QuotaMeterItem",
        "QuotaAdmissionState",
        "QuotaMeterStatus",
        "CircuitBreakerState",
        "QuotaMetricKind",
        "RuntimePauseState",
    ):
        assert schema_name in spec["components"]["schemas"]

    # 6. Verify standalone create_app(budgets_router=..., install_budgets_handlers=True) handles BudgetCutoffError
    standalone = create_app(
        budgets_router=create_budgets_router(service=budgets_svc, resolve_access=lambda _r: access),
        install_budgets_handlers=True,
    )

    @standalone.post("/v1/test-high-cost")
    async def _test_high_cost() -> dict[str, bool]:
        budgets_svc.check_and_reserve_budget(
            access,
            operation_kind=BudgetOperationKind.INITIAL_IMPORT,
            deltas={QuotaMetricKind.IMPORT_OBJECTS: 1},
        )
        return {"ok": True}

    sclient = TestClient(standalone)
    r_blk = sclient.post("/v1/test-high-cost")
    assert r_blk.status_code == 503
    assert "retry-after" in r_blk.headers
    assert r_blk.headers["cache-control"] == "private, no-store"
    assert r_blk.json()["error"]["paid_fallback_allowed"] is False
    assert len(r_blk.json()["error"]["free_alternatives"]) >= 3
