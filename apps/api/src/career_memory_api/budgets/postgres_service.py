"""PostgreSQL 18 & In-Memory Quota, Budget Admission, and Runtime Pause Services (T-503 / PERF-01 / INF-06)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.auth.redaction import redact_text
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.budgets.circuit_breaker import ProviderCircuitBreaker
from career_memory_api.budgets.models import (
    DEFAULT_BUDGET_FREE_ALTERNATIVES,
    SAFETY_OPERATIONS,
    BudgetCutoffError,
    BudgetOperationKind,
    BudgetReservationResult,
    CircuitBreakerState,
    DefaultQuotaPolicyConfig,
    ManualResumeVerificationError,
    OperationUsageObservation,
    QuotaAdmissionState,
    QuotaMeterItem,
    QuotaMeterStatus,
    QuotaMetricLimit,
    QuotaStatusResponse,
    compute_meter_item,
    enforce_operation_budget,
    format_period_key,
    utc_now,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.resolver.policy import OperationBudgetConfig
from career_memory_api.schema_contracts.enums import (
    QuotaMetricKind,
    RuntimePauseScopeKind,
    RuntimePauseState,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

_ORDERED_METRIC_KINDS: tuple[QuotaMetricKind, ...] = (
    QuotaMetricKind.IMPORT_OBJECTS,
    QuotaMetricKind.PROVIDER_REQUESTS,
    QuotaMetricKind.REFETCH_BYTES,
    QuotaMetricKind.EXPORT_BUILDS,
    QuotaMetricKind.SEARCH_QUERIES,
    QuotaMetricKind.ESTIMATED_COST_KRW,
    QuotaMetricKind.LLM_TOKENS,
)


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _derive_admission_state(
    *,
    pause_state: RuntimePauseState,
    circuit_breaker_state: CircuitBreakerState,
    meters: list[QuotaMeterItem],
) -> tuple[QuotaAdmissionState, bool]:
    """Derive ``(admission_state, high_cost_operations_allowed)`` from pause, circuit, and meters."""
    if (
        pause_state in (RuntimePauseState.PAUSED_OPERATOR, RuntimePauseState.PAUSED_CIRCUIT_BREAKER)
        or circuit_breaker_state is CircuitBreakerState.OPEN
    ):
        return QuotaAdmissionState.PAUSED, False

    if any(m.status is QuotaMeterStatus.CUTOFF for m in meters):
        return QuotaAdmissionState.CUTOFF_95_PERCENT, False

    if pause_state is RuntimePauseState.PAUSED_QUOTA:
        return QuotaAdmissionState.PAUSED, False

    if any(m.status is QuotaMeterStatus.WARNING for m in meters):
        return QuotaAdmissionState.WARNING_80_PERCENT, True

    return QuotaAdmissionState.ALLOWED, True


class QuotaBudgetPort(Protocol):
    """Port implemented by both PostgreSQL 18 and in-memory quota/budget services."""

    def get_quota_status(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,
    ) -> QuotaStatusResponse: ...


class InMemoryQuotaBudgetService:
    """Deterministic in-memory fallback service for OpenAPI generation and contract-app testing."""

    def __init__(
        self,
        *,
        policy_config: DefaultQuotaPolicyConfig | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy_config or DefaultQuotaPolicyConfig()
        self._cb = circuit_breaker or ProviderCircuitBreaker()
        self._now_fn = now_fn or utc_now
        self._lock = threading.RLock()
        self._usage: dict[tuple[str, str, QuotaMetricKind], int] = {}
        self._pause_state: dict[str, tuple[RuntimePauseState, str | None]] = {}

    def get_quota_status(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,  # noqa: ARG002
    ) -> QuotaStatusResponse:
        with self._lock:
            now = self._now_fn()
            period_key = format_period_key(now)
            pause_state, reason_code = self._pause_state.get(
                access.workspace_id, (RuntimePauseState.ACTIVE, None)
            )
            cb_snap = self._cb.snapshot()
            meters: list[QuotaMeterItem] = []
            for kind in _ORDERED_METRIC_KINDS:
                lim = self._policy.limit_for(kind)
                used = self._usage.get((access.workspace_id, period_key, kind), 0)
                meters.append(
                    compute_meter_item(
                        metric_kind=kind,
                        used_value=used,
                        soft_warning_limit=lim.soft_warning_limit,
                        hard_cutoff_limit=lim.hard_cutoff_limit,
                    )
                )
            admission_state, high_cost_allowed = _derive_admission_state(
                pause_state=pause_state,
                circuit_breaker_state=cb_snap.state,
                meters=meters,
            )
            retry_after: int | None = None
            if not high_cost_allowed:
                retry_after = (
                    cb_snap.retry_after_seconds
                    if cb_snap.retry_after_seconds is not None
                    else self._policy.default_retry_after_seconds
                )
            return QuotaStatusResponse(
                workspace_id=access.workspace_id,
                period_key=period_key,
                admission_state=admission_state,
                pause_state=pause_state,
                pause_reason_code=reason_code,
                circuit_breaker_state=cb_snap.state,
                high_cost_operations_allowed=high_cost_allowed,
                safety_operations_allowed=True,
                paid_fallback_allowed=False,
                retry_after_seconds=retry_after,
                free_alternatives=list(DEFAULT_BUDGET_FREE_ALTERNATIVES),
                meters=meters,
                checked_at=now,
            )


class PostgresQuotaBudgetService:
    """PostgreSQL 18 workspace quota ledger, 80%/95% admission guard, circuit breaker & manual pause service.

    Enforces ``PERF-01``, ``INF-06``, ``INF-03``, ``ADR-019``, ``ADR-020``, and ``OD-015`` on
    ``public.workspace_quota_ledgers`` and ``public.runtime_pause_controls`` (Migration 0080)
    strictly inside ``workspace_scoped_transaction`` with ``FORCE RLS``.
    """

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        policy_config: DefaultQuotaPolicyConfig | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._policy = policy_config or DefaultQuotaPolicyConfig()
        self.circuit_breaker = circuit_breaker or ProviderCircuitBreaker()
        self._now_fn = now_fn or utc_now

    # =========================================================================
    # 1. Read Quota Status (GET /v1/quota-status)
    # =========================================================================
    def get_quota_status(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,
    ) -> QuotaStatusResponse:
        """Read current workspace quota meters, admission state, pause state, and circuit state."""
        res_uuid = _parse_uuid_or_none(source_resource_id) if source_resource_id else None
        now = self._now_fn()
        period_key = format_period_key(now)

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._ensure_period_ledgers(cur, access.workspace_id, period_key, now)
                self._ensure_workspace_pause_control(cur, access.workspace_id, now)
                meters = self._load_meters(cur, access.workspace_id, period_key, for_update=False)
                pause_state, pause_reason = self._load_effective_pause_state(
                    cur,
                    workspace_id=access.workspace_id,
                    source_resource_id=res_uuid,
                    for_update=False,
                )

        cb_snap = self.circuit_breaker.snapshot()
        admission_state, high_cost_allowed = _derive_admission_state(
            pause_state=pause_state,
            circuit_breaker_state=cb_snap.state,
            meters=meters,
        )
        retry_after: int | None = None
        if not high_cost_allowed:
            retry_after = (
                cb_snap.retry_after_seconds
                if cb_snap.retry_after_seconds is not None
                else self._policy.default_retry_after_seconds
            )

        return QuotaStatusResponse(
            workspace_id=access.workspace_id,
            period_key=period_key,
            admission_state=admission_state,
            pause_state=pause_state,
            pause_reason_code=pause_reason,
            circuit_breaker_state=cb_snap.state,
            high_cost_operations_allowed=high_cost_allowed,
            safety_operations_allowed=True,
            paid_fallback_allowed=False,
            retry_after_seconds=retry_after,
            free_alternatives=list(DEFAULT_BUDGET_FREE_ALTERNATIVES),
            meters=meters,
            checked_at=now,
        )

    # =========================================================================
    # 2. Pre-Execution Budget Admission & Atomic Reservation (PERF-01 / INF-06)
    # =========================================================================
    def check_and_reserve_budget(
        self,
        access: WorkspaceAccessContext,
        *,
        operation_kind: BudgetOperationKind | str,
        deltas: Mapping[QuotaMetricKind, int] | None = None,
        is_safety_operation: bool = False,
        source_resource_id: str | None = None,
        operation_budget: OperationBudgetConfig | None = None,
        observed_operation_usage: OperationUsageObservation | None = None,
    ) -> BudgetReservationResult:
        """Atomically verify pause/circuit/quota state and reserve budget deltas before execution.

        Safety operations (``SOURCE_DISCONNECT``, ``DELETION_PREVIEW``, ``DELETION_CONFIRM``,
        ``DELETION_EXECUTE``, ``AUTH_REVOKE``, ``HEALTH_CHECK``, ``QUOTA_READ``) are ALWAYS
        ALLOWED even under 95% cutoff, manual operator pause, or an open circuit breaker.
        Non-safety / high-cost operations fail closed BEFORE any external or billable work runs.
        """
        op_enum = (
            operation_kind
            if isinstance(operation_kind, BudgetOperationKind)
            else BudgetOperationKind(str(operation_kind))
        )
        now = self._now_fn()
        period_key = format_period_key(now)

        # Safety Reservation Rule (ADR-020 / OD-015 / INF-06): ALWAYS ALLOWED!
        if is_safety_operation or op_enum in SAFETY_OPERATIONS:
            return BudgetReservationResult(
                allowed=True,
                operation_kind=op_enum,
                admission_state=QuotaAdmissionState.ALLOWED,
                safety_bypass=True,
                reserved_deltas={},
                period_key=period_key,
            )

        # Single-operation item/size/call/byte/concurrency budget check (PERF-01)
        if operation_budget is not None and observed_operation_usage is not None:
            enforce_operation_budget(
                operation_budget,
                observed_operation_usage,
                max_items=self._policy.max_items_per_operation,
            )

        normalized_deltas: dict[QuotaMetricKind, int] = {}
        for k, v in (deltas or {}).items():
            kind = k if isinstance(k, QuotaMetricKind) else QuotaMetricKind(str(k))
            iv = int(v)
            if iv < 0:
                raise ValueError(f"Budget delta for {kind.value} must be >= 0, got {iv}.")
            if iv > 0:
                normalized_deltas[kind] = iv

        # Circuit breaker pre-check (INF-03 / T-503)
        cb_allowed, cb_retry_after = self.circuit_breaker.allow_request()
        if not cb_allowed:
            raise BudgetCutoffError(
                status_code=503,
                code="CIRCUIT_BREAKER_OPEN",
                message=(
                    f"Operation {op_enum.value} blocked because provider circuit breaker is OPEN."
                ),
                retryable=True,
                retry_after_seconds=cb_retry_after or self._policy.default_retry_after_seconds,
                details={"operation_kind": op_enum.value},
            )

        res_uuid = _parse_uuid_or_none(source_resource_id) if source_resource_id else None
        cutoff_block_info: tuple[str, int, str, dict[str, Any]] | None = None

        try:
            with self._api_connection_factory() as conn:
                with workspace_scoped_transaction(conn, access) as cur:
                    self._assert_live_workspace_membership(cur, access)
                    self._ensure_period_ledgers(cur, access.workspace_id, period_key, now)
                    self._ensure_workspace_pause_control(cur, access.workspace_id, now)

                    pause_state, pause_reason = self._load_effective_pause_state(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_id=res_uuid,
                        for_update=True,
                    )
                    meters_before = self._load_meters(
                        cur, access.workspace_id, period_key, for_update=True
                    )
                    meter_by_kind = {m.metric_kind: m for m in meters_before}

                    # 1. Check active runtime pause control
                    if pause_state is not RuntimePauseState.ACTIVE:
                        code = (
                            "BUDGET_CUTOFF_REACHED"
                            if pause_state is RuntimePauseState.PAUSED_QUOTA
                            and any(m.status is QuotaMeterStatus.CUTOFF for m in meters_before)
                            else "RUNTIME_PAUSED"
                        )
                        status_code = 429 if code == "BUDGET_CUTOFF_REACHED" else 503
                        raise BudgetCutoffError(
                            status_code=status_code,
                            code=code,
                            message=(
                                f"Operation {op_enum.value} blocked: workspace runtime is "
                                f"{pause_state.value}."
                            ),
                            retryable=True,
                            retry_after_seconds=self._policy.default_retry_after_seconds,
                            details={
                                "operation_kind": op_enum.value,
                                "pause_state": pause_state.value,
                                "pause_reason_code": pause_reason,
                            },
                        )

                    # 2. Check if any meter is already at 95% hard cutoff
                    already_cutoff = [
                        m for m in meters_before if m.status is QuotaMeterStatus.CUTOFF
                    ]
                    if already_cutoff:
                        tripped = already_cutoff[0]
                        if self._policy.auto_pause_on_cutoff:
                            self._set_pause_control_in_tx(
                                cur,
                                workspace_id=access.workspace_id,
                                actor_user_id=access.user_id,
                                pause_state=RuntimePauseState.PAUSED_QUOTA,
                                reason_code=f"QUOTA_CUTOFF_{tripped.metric_kind.value}",
                                source_resource_id=None,
                                now=now,
                            )
                        cutoff_block_info = (
                            "BUDGET_CUTOFF_REACHED",
                            429,
                            (
                                f"Operation {op_enum.value} blocked: quota meter "
                                f"{tripped.metric_kind.value} has reached 95% hard cutoff "
                                f"({tripped.used_value}/{tripped.hard_cutoff_limit})."
                            ),
                            {
                                "operation_kind": op_enum.value,
                                "metric_kind": tripped.metric_kind.value,
                                "used_value": tripped.used_value,
                                "hard_cutoff_limit": tripped.hard_cutoff_limit,
                            },
                        )
                    else:
                        # 3. Check if requested deltas would exceed hard cutoff limit
                        exceeding: tuple[QuotaMetricKind, int, int, int] | None = None
                        for kind, delta in normalized_deltas.items():
                            meter = meter_by_kind[kind]
                            projected = meter.used_value + delta
                            if projected > meter.hard_cutoff_limit:
                                exceeding = (kind, meter.used_value, delta, meter.hard_cutoff_limit)
                                break

                        if exceeding is not None:
                            kind, used_val, delta_val, hard_lim = exceeding
                            if self._policy.auto_pause_on_cutoff and hard_lim > 0:
                                self._set_pause_control_in_tx(
                                    cur,
                                    workspace_id=access.workspace_id,
                                    actor_user_id=access.user_id,
                                    pause_state=RuntimePauseState.PAUSED_QUOTA,
                                    reason_code=f"QUOTA_CUTOFF_{kind.value}",
                                    source_resource_id=None,
                                    now=now,
                                )
                            cutoff_block_info = (
                                "BUDGET_CUTOFF_REACHED",
                                429,
                                (
                                    f"Operation {op_enum.value} blocked before execution: "
                                    f"{kind.value} projected usage ({used_val} + {delta_val}) "
                                    f"exceeds 95% hard cutoff ({hard_lim})."
                                ),
                                {
                                    "operation_kind": op_enum.value,
                                    "metric_kind": kind.value,
                                    "used_value": used_val,
                                    "requested_delta": delta_val,
                                    "hard_cutoff_limit": hard_lim,
                                },
                            )
                        else:
                            # 4. Atomically reserve deltas in public.workspace_quota_ledgers
                            hit_cutoff_kind: QuotaMetricKind | None = None
                            updated_meters: list[QuotaMeterItem] = []
                            for kind in _ORDERED_METRIC_KINDS:
                                m = meter_by_kind[kind]
                                delta = normalized_deltas.get(kind, 0)
                                new_used = m.used_value + delta
                                if delta > 0:
                                    cur.execute(
                                        """
                                        UPDATE public.workspace_quota_ledgers
                                        SET used_value = %s,
                                            updated_at = %s
                                        WHERE workspace_id = %s::uuid
                                          AND period_key = %s
                                          AND metric_kind = %s
                                        """,
                                        (new_used, now, access.workspace_id, period_key, kind.value),
                                    )
                                updated = compute_meter_item(
                                    metric_kind=kind,
                                    used_value=new_used,
                                    soft_warning_limit=m.soft_warning_limit,
                                    hard_cutoff_limit=m.hard_cutoff_limit,
                                )
                                if updated.status is QuotaMeterStatus.CUTOFF and hit_cutoff_kind is None:
                                    hit_cutoff_kind = kind
                                updated_meters.append(updated)

                            effective_pause = pause_state
                            if hit_cutoff_kind is not None and self._policy.auto_pause_on_cutoff:
                                self._set_pause_control_in_tx(
                                    cur,
                                    workspace_id=access.workspace_id,
                                    actor_user_id=access.user_id,
                                    pause_state=RuntimePauseState.PAUSED_QUOTA,
                                    reason_code=f"QUOTA_CUTOFF_{hit_cutoff_kind.value}",
                                    source_resource_id=None,
                                    now=now,
                                )
                                effective_pause = RuntimePauseState.PAUSED_QUOTA

                            admission_after, _ = _derive_admission_state(
                                pause_state=effective_pause,
                                circuit_breaker_state=self.circuit_breaker.state,
                                meters=updated_meters,
                            )
                            return BudgetReservationResult(
                                allowed=True,
                                operation_kind=op_enum,
                                admission_state=admission_after,
                                safety_bypass=False,
                                reserved_deltas=normalized_deltas,
                                period_key=period_key,
                            )
        except (BudgetCutoffError, WorkspaceMembershipDeniedError):
            raise
        except Exception as exc:  # noqa: BLE001
            # Fail-closed (ADR-020 / INF-06): if budget state cannot be verified, block non-safety work!
            raise BudgetCutoffError(
                status_code=503,
                code="BUDGET_VERIFICATION_UNAVAILABLE",
                message="Quota and budget state could not be verified; blocking high-cost operation fail-closed.",
                retryable=True,
                retry_after_seconds=self._policy.default_retry_after_seconds,
                details={"operation_kind": op_enum.value, "error_type": type(exc).__name__},
            ) from exc

        assert cutoff_block_info is not None
        err_code, http_status, msg, details = cutoff_block_info
        raise BudgetCutoffError(
            status_code=http_status,
            code=err_code,
            message=msg,
            retryable=True,
            retry_after_seconds=self._policy.default_retry_after_seconds,
            details=details,
        )

    # =========================================================================
    # 3. Operator Pause, Verified Manual Resume & Limit Configuration
    # =========================================================================
    def pause_runtime(
        self,
        access: WorkspaceAccessContext,
        *,
        pause_state: RuntimePauseState,
        reason_code: str,
        source_resource_id: str | None = None,
    ) -> QuotaStatusResponse:
        """Manually pause workspace or source-resource runtime and record an immutable audit event."""
        if pause_state is RuntimePauseState.ACTIVE:
            raise ValueError("pause_runtime() requires a PAUSED_* state, not ACTIVE.")
        cleaned_reason = redact_text(reason_code.strip())
        if not cleaned_reason:
            raise ValueError("reason_code must be non-empty.")

        res_uuid = _parse_uuid_or_none(source_resource_id) if source_resource_id else None
        if source_resource_id is not None and res_uuid is None:
            raise ValueError("source_resource_id must be a valid UUID when provided.")

        now = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._set_pause_control_in_tx(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user_id,
                    pause_state=pause_state,
                    reason_code=cleaned_reason,
                    source_resource_id=res_uuid,
                    now=now,
                )
        return self.get_quota_status(access, source_resource_id=res_uuid)

    def resume_runtime(
        self,
        access: WorkspaceAccessContext,
        *,
        budget_verified: bool,
        operator_note: str | None = None,
        source_resource_id: str | None = None,
    ) -> QuotaStatusResponse:
        """Manually resume runtime ONLY when ``budget_verified=True`` and hard cutoff is not exceeded."""
        if budget_verified is not True:
            raise ManualResumeVerificationError()

        res_uuid = _parse_uuid_or_none(source_resource_id) if source_resource_id else None
        if source_resource_id is not None and res_uuid is None:
            raise ValueError("source_resource_id must be a valid UUID when provided.")

        now = self._now_fn()
        period_key = format_period_key(now)
        safe_note = redact_text(operator_note.strip()) if operator_note else None

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._ensure_period_ledgers(cur, access.workspace_id, period_key, now)
                self._ensure_workspace_pause_control(cur, access.workspace_id, now)

                meters = self._load_meters(cur, access.workspace_id, period_key, for_update=True)
                exceeded = [m for m in meters if m.status is QuotaMeterStatus.CUTOFF]
                if exceeded:
                    first = exceeded[0]
                    raise BudgetCutoffError(
                        status_code=409,
                        code="BUDGET_CUTOFF_STILL_EXCEEDED",
                        message=(
                            f"Cannot resume runtime while {first.metric_kind.value} remains at/above "
                            f"hard cutoff ({first.used_value}/{first.hard_cutoff_limit})."
                        ),
                        retryable=False,
                        retry_after_seconds=self._policy.default_retry_after_seconds,
                        details={
                            "metric_kind": first.metric_kind.value,
                            "used_value": first.used_value,
                            "hard_cutoff_limit": first.hard_cutoff_limit,
                        },
                    )

                scope_kind = (
                    RuntimePauseScopeKind.SOURCE_RESOURCE.value
                    if res_uuid is not None
                    else RuntimePauseScopeKind.WORKSPACE.value
                )
                meta: dict[str, Any] = {"budget_verified": True}
                if safe_note:
                    meta["operator_note"] = safe_note
                assert_no_forbidden_raw_keys(meta, field_name="runtime_pause_controls.control_metadata")
                assert_no_forbidden_secret_keys(meta, field_name="runtime_pause_controls.control_metadata")

                if res_uuid is None:
                    cur.execute(
                        """
                        UPDATE public.runtime_pause_controls
                        SET pause_state = 'ACTIVE',
                            reason_code = 'OPERATOR_BUDGET_VERIFIED_RESUME',
                            control_metadata = %s,
                            resumed_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND scope_kind = 'WORKSPACE'
                        """,
                        (Jsonb(meta), now, now, access.workspace_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE public.runtime_pause_controls
                        SET pause_state = 'ACTIVE',
                            reason_code = 'OPERATOR_BUDGET_VERIFIED_RESUME',
                            control_metadata = %s,
                            resumed_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND scope_kind = 'SOURCE_RESOURCE'
                          AND source_resource_id = %s::uuid
                        """,
                        (Jsonb(meta), now, now, access.workspace_id, res_uuid),
                    )

                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_id=access.user_id,
                    action="RUNTIME_RESUMED",
                    target_type=scope_kind,
                    target_id=res_uuid or access.workspace_id,
                    redacted_metadata={
                        "budget_verified": True,
                        "scope_kind": scope_kind,
                        **({"operator_note": safe_note} if safe_note else {}),
                    },
                    created_at=now,
                )

        self.circuit_breaker.reset()
        return self.get_quota_status(access, source_resource_id=res_uuid)

    def update_quota_limit(
        self,
        access: WorkspaceAccessContext,
        *,
        metric_kind: QuotaMetricKind,
        soft_warning_limit: int,
        hard_cutoff_limit: int,
    ) -> QuotaStatusResponse:
        """Update soft/hard limits for a workspace metric in the current period (validated by DB CHECK)."""
        QuotaMetricLimit(
            soft_warning_limit=soft_warning_limit,
            hard_cutoff_limit=hard_cutoff_limit,
        )
        now = self._now_fn()
        period_key = format_period_key(now)
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._ensure_period_ledgers(cur, access.workspace_id, period_key, now)
                cur.execute(
                    """
                    UPDATE public.workspace_quota_ledgers
                    SET soft_warning_limit = %s,
                        hard_cutoff_limit = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND period_key = %s
                      AND metric_kind = %s
                    """,
                    (
                        soft_warning_limit,
                        hard_cutoff_limit,
                        now,
                        access.workspace_id,
                        period_key,
                        metric_kind.value,
                    ),
                )
        return self.get_quota_status(access)

    # =========================================================================
    # Internal PostgreSQL Helpers
    # =========================================================================
    def _assert_live_workspace_membership(
        self, cur: psycopg.Cursor, access: WorkspaceAccessContext
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.workspace_memberships
            WHERE workspace_id = %s::uuid
              AND user_id = %s::uuid
              AND revoked_at IS NULL
            """,
            (access.workspace_id, access.user_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceMembershipDeniedError(
                "Active workspace membership required for quota operations."
            )

    def _ensure_period_ledgers(
        self,
        cur: psycopg.Cursor,
        workspace_id: str,
        period_key: str,
        now: datetime,
    ) -> None:
        for kind in _ORDERED_METRIC_KINDS:
            lim = self._policy.limit_for(kind)
            cur.execute(
                """
                INSERT INTO public.workspace_quota_ledgers (
                    id, workspace_id, period_key, metric_kind,
                    used_value, soft_warning_limit, hard_cutoff_limit,
                    ledger_metadata, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s,
                    0, %s, %s, '{}'::jsonb, %s
                )
                ON CONFLICT (workspace_id, period_key, metric_kind) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    workspace_id,
                    period_key,
                    kind.value,
                    lim.soft_warning_limit,
                    lim.hard_cutoff_limit,
                    now,
                ),
            )

    def _ensure_workspace_pause_control(
        self,
        cur: psycopg.Cursor,
        workspace_id: str,
        now: datetime,
    ) -> None:
        cur.execute(
            """
            INSERT INTO public.runtime_pause_controls (
                id, workspace_id, scope_kind, source_resource_id,
                pause_state, reason_code, control_metadata, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, 'WORKSPACE', NULL,
                'ACTIVE', NULL, '{}'::jsonb, %s
            )
            ON CONFLICT (workspace_id) WHERE scope_kind = 'WORKSPACE' DO NOTHING
            """,
            (str(uuid.uuid4()), workspace_id, now),
        )

    def _load_meters(
        self,
        cur: psycopg.Cursor,
        workspace_id: str,
        period_key: str,
        *,
        for_update: bool,
    ) -> list[QuotaMeterItem]:
        lock_clause = " FOR UPDATE" if for_update else ""
        cur.execute(
            f"""
            SELECT metric_kind, used_value, soft_warning_limit, hard_cutoff_limit
            FROM public.workspace_quota_ledgers
            WHERE workspace_id = %s::uuid
              AND period_key = %s
            ORDER BY metric_kind ASC
            {lock_clause}
            """,
            (workspace_id, period_key),
        )
        by_kind: dict[QuotaMetricKind, QuotaMeterItem] = {}
        for kind_str, used_val, soft_lim, hard_lim in cur.fetchall():
            kind = QuotaMetricKind(str(kind_str))
            by_kind[kind] = compute_meter_item(
                metric_kind=kind,
                used_value=int(used_val),
                soft_warning_limit=int(soft_lim),
                hard_cutoff_limit=int(hard_lim),
            )
        return [by_kind[k] for k in _ORDERED_METRIC_KINDS if k in by_kind]

    def _load_effective_pause_state(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str | None,
        for_update: bool,
    ) -> tuple[RuntimePauseState, str | None]:
        lock_clause = " FOR UPDATE" if for_update else ""
        cur.execute(
            f"""
            SELECT scope_kind, pause_state, reason_code
            FROM public.runtime_pause_controls
            WHERE workspace_id = %s::uuid
              AND (
                    scope_kind = 'WORKSPACE'
                    OR (scope_kind = 'SOURCE_RESOURCE' AND source_resource_id = %s::uuid)
                  )
            ORDER BY CASE WHEN scope_kind = 'WORKSPACE' THEN 0 ELSE 1 END
            {lock_clause}
            """,
            (workspace_id, source_resource_id),
        )
        rows = cur.fetchall()
        effective_state = RuntimePauseState.ACTIVE
        effective_reason: str | None = None
        for _scope, st_str, reason in rows:
            st = RuntimePauseState(str(st_str))
            if st is not RuntimePauseState.ACTIVE:
                effective_state = st
                effective_reason = str(reason) if reason is not None else None
                # Operator pause takes highest precedence
                if st is RuntimePauseState.PAUSED_OPERATOR:
                    break
        return effective_state, effective_reason

    def _set_pause_control_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_user_id: str | None,
        pause_state: RuntimePauseState,
        reason_code: str,
        source_resource_id: str | None,
        now: datetime,
    ) -> None:
        safe_reason = redact_text(reason_code.strip())[:200] or "RUNTIME_PAUSED"
        actor_uuid = _parse_uuid_or_none(actor_user_id)
        scope_kind = (
            RuntimePauseScopeKind.SOURCE_RESOURCE.value
            if source_resource_id is not None
            else RuntimePauseScopeKind.WORKSPACE.value
        )
        control_meta = {"reason_code": safe_reason, "pause_state": pause_state.value}
        assert_no_forbidden_raw_keys(
            control_meta, field_name="runtime_pause_controls.control_metadata"
        )
        assert_no_forbidden_secret_keys(
            control_meta, field_name="runtime_pause_controls.control_metadata"
        )

        if source_resource_id is None:
            cur.execute(
                """
                INSERT INTO public.runtime_pause_controls (
                    id, workspace_id, scope_kind, source_resource_id,
                    pause_state, reason_code, paused_by_actor_id,
                    control_metadata, paused_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, 'WORKSPACE', NULL,
                    %s, %s, %s::uuid,
                    %s, %s, %s
                )
                ON CONFLICT (workspace_id) WHERE scope_kind = 'WORKSPACE'
                DO UPDATE SET
                    pause_state = EXCLUDED.pause_state,
                    reason_code = EXCLUDED.reason_code,
                    paused_by_actor_id = EXCLUDED.paused_by_actor_id,
                    control_metadata = EXCLUDED.control_metadata,
                    paused_at = EXCLUDED.paused_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    workspace_id,
                    pause_state.value,
                    safe_reason,
                    actor_uuid,
                    Jsonb(control_meta),
                    now,
                    now,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO public.runtime_pause_controls (
                    id, workspace_id, scope_kind, source_resource_id,
                    pause_state, reason_code, paused_by_actor_id,
                    control_metadata, paused_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, 'SOURCE_RESOURCE', %s::uuid,
                    %s, %s, %s::uuid,
                    %s, %s, %s
                )
                ON CONFLICT (workspace_id, source_resource_id) WHERE scope_kind = 'SOURCE_RESOURCE'
                DO UPDATE SET
                    pause_state = EXCLUDED.pause_state,
                    reason_code = EXCLUDED.reason_code,
                    paused_by_actor_id = EXCLUDED.paused_by_actor_id,
                    control_metadata = EXCLUDED.control_metadata,
                    paused_at = EXCLUDED.paused_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    workspace_id,
                    source_resource_id,
                    pause_state.value,
                    safe_reason,
                    actor_uuid,
                    Jsonb(control_meta),
                    now,
                    now,
                ),
            )

        self._insert_audit_event(
            cur,
            workspace_id=workspace_id,
            actor_id=actor_uuid,
            action="RUNTIME_PAUSED",
            target_type=scope_kind,
            target_id=source_resource_id or workspace_id,
            redacted_metadata={
                "pause_state": pause_state.value,
                "reason_code": safe_reason,
                "scope_kind": scope_kind,
            },
            created_at=now,
        )

    def _insert_audit_event(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_id: str | None,
        action: str,
        target_type: str,
        target_id: str,
        redacted_metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(redacted_metadata, field_name="audit_events.redacted_metadata")
        assert_no_forbidden_secret_keys(
            redacted_metadata, field_name="audit_events.redacted_metadata"
        )
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id, workspace_id, actor_type, actor_id,
                action, target_type, target_id,
                redacted_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s, %s,
                %s, %s
            )
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                AuditActorType.USER.value if actor_id else AuditActorType.SYSTEM.value,
                _parse_uuid_or_none(actor_id),
                action,
                target_type,
                target_id,
                Jsonb(redacted_metadata),
                created_at,
            ),
        )


__all__ = [
    "InMemoryQuotaBudgetService",
    "PostgresQuotaBudgetService",
    "QuotaBudgetPort",
]
