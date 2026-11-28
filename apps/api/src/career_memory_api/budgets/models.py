"""Wire models, policy config, operation budget checks, and structured errors for M5 Quotas & Budgets (T-503)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from career_memory_api.auth.redaction import redact_text
from career_memory_api.resolver.policy import OperationBudgetConfig
from career_memory_api.schema_contracts.enums import (
    QuotaMetricKind,
    RuntimePauseState,
)

DEFAULT_BUDGET_FREE_ALTERNATIVES: tuple[str, ...] = (
    "REDUCE_REPOSITORY_OR_BACKFILL_SCOPE",
    "USE_NON_AI_DETERMINISTIC_REVIEW",
    "USE_LOCAL_SYNTHETIC_FIXTURES",
    "WAIT_FOR_QUOTA_WINDOW_OR_MANUAL_OPERATOR_RESUME",
)


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


def format_period_key(ts: datetime | None = None) -> str:
    """Format monthly UTC period key ``YYYY-MM`` for ``public.workspace_quota_ledgers``."""
    now = ts or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).strftime("%Y-%m")


class QuotaAdmissionState(StrEnum):
    """Workspace-level quota admission state (1:1 parity with Wave 2B Frontend)."""

    ALLOWED = "ALLOWED"
    WARNING_80_PERCENT = "WARNING_80_PERCENT"
    CUTOFF_95_PERCENT = "CUTOFF_95_PERCENT"
    PAUSED = "PAUSED"


class QuotaMeterStatus(StrEnum):
    """Per-metric threshold status (1:1 parity with Wave 2B Frontend)."""

    OK = "OK"
    WARNING = "WARNING"
    CUTOFF = "CUTOFF"


class CircuitBreakerState(StrEnum):
    """Provider/operation circuit breaker state (1:1 parity with Wave 2B Frontend)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class BudgetOperationKind(StrEnum):
    """Named operation categories for budget & safety-reservation admission."""

    # Essential safety & user-rights operations (ALWAYS ALLOWED under cutoff/pause/circuit-open)
    SOURCE_DISCONNECT = "SOURCE_DISCONNECT"
    DELETION_PREVIEW = "DELETION_PREVIEW"
    DELETION_CONFIRM = "DELETION_CONFIRM"
    DELETION_EXECUTE = "DELETION_EXECUTE"
    AUTH_REVOKE = "AUTH_REVOKE"
    HEALTH_CHECK = "HEALTH_CHECK"
    QUOTA_READ = "QUOTA_READ"

    # High-cost / mutable growth operations (BLOCKED at cutoff/pause/circuit-open)
    INITIAL_IMPORT = "INITIAL_IMPORT"
    IMPORT_RETRY = "IMPORT_RETRY"
    FACT_EXTRACTION = "FACT_EXTRACTION"
    CLAIM_GENERATION = "CLAIM_GENERATION"
    EXPORT_CREATE = "EXPORT_CREATE"
    NONESSENTIAL_WAKE = "NONESSENTIAL_WAKE"

    # Metered standard operations
    SEARCH_QUERY = "SEARCH_QUERY"
    REFETCH_RESOLUTION = "REFETCH_RESOLUTION"


SAFETY_OPERATIONS: frozenset[BudgetOperationKind] = frozenset(
    {
        BudgetOperationKind.SOURCE_DISCONNECT,
        BudgetOperationKind.DELETION_PREVIEW,
        BudgetOperationKind.DELETION_CONFIRM,
        BudgetOperationKind.DELETION_EXECUTE,
        BudgetOperationKind.AUTH_REVOKE,
        BudgetOperationKind.HEALTH_CHECK,
        BudgetOperationKind.QUOTA_READ,
    }
)

HIGH_COST_OPERATIONS: frozenset[BudgetOperationKind] = frozenset(
    {
        BudgetOperationKind.INITIAL_IMPORT,
        BudgetOperationKind.IMPORT_RETRY,
        BudgetOperationKind.FACT_EXTRACTION,
        BudgetOperationKind.CLAIM_GENERATION,
        BudgetOperationKind.EXPORT_CREATE,
        BudgetOperationKind.NONESSENTIAL_WAKE,
    }
)


@dataclass(frozen=True, slots=True)
class QuotaMetricLimit:
    """Soft warning (80%) and hard cutoff (95%) bounds for one quota metric."""

    soft_warning_limit: int
    hard_cutoff_limit: int

    def __post_init__(self) -> None:
        if self.soft_warning_limit < 0:
            raise ValueError("soft_warning_limit must be >= 0.")
        if self.hard_cutoff_limit < self.soft_warning_limit:
            raise ValueError("hard_cutoff_limit must be >= soft_warning_limit.")


def _default_metric_limits() -> dict[QuotaMetricKind, QuotaMetricLimit]:
    """Conservative ₩0 / <20,000 KRW ceiling monthly defaults (OD-015 / ADR-020 / Eng Ops Playbook §15)."""
    return {
        QuotaMetricKind.IMPORT_OBJECTS: QuotaMetricLimit(
            soft_warning_limit=800,
            hard_cutoff_limit=950,
        ),
        QuotaMetricKind.PROVIDER_REQUESTS: QuotaMetricLimit(
            soft_warning_limit=400,
            hard_cutoff_limit=475,
        ),
        QuotaMetricKind.REFETCH_BYTES: QuotaMetricLimit(
            soft_warning_limit=40 * 1024 * 1024,  # 40 MiB (80% of 50 MiB)
            hard_cutoff_limit=47_500 * 1024 + 512 * 1024,  # 47.5 MiB (95% of 50 MiB = 49,807,360)
        ),
        QuotaMetricKind.EXPORT_BUILDS: QuotaMetricLimit(
            soft_warning_limit=40,
            hard_cutoff_limit=47,
        ),
        QuotaMetricKind.SEARCH_QUERIES: QuotaMetricLimit(
            soft_warning_limit=800,
            hard_cutoff_limit=950,
        ),
        # OD-015 / Eng Ops Playbook §15: 20,000 KRW cap -> 80% warn (16,000), 95% hard cutoff (19,000).
        # Local ₩0 mode starts with used_value = 0 so fresh workspaces stay ALLOWED.
        QuotaMetricKind.ESTIMATED_COST_KRW: QuotaMetricLimit(
            soft_warning_limit=16_000,
            hard_cutoff_limit=19_000,
        ),
        # OD-002 / OD-014: live external LLM is OFF by default (0/0 limit; any delta > 0 blocked).
        QuotaMetricKind.LLM_TOKENS: QuotaMetricLimit(
            soft_warning_limit=0,
            hard_cutoff_limit=0,
        ),
    }


@dataclass(frozen=True, slots=True)
class DefaultQuotaPolicyConfig:
    """Configurable per-workspace quota thresholds and retry/pause policy."""

    limits: Mapping[QuotaMetricKind, QuotaMetricLimit] = field(
        default_factory=_default_metric_limits
    )
    auto_pause_on_cutoff: bool = True
    default_retry_after_seconds: int = 300
    max_items_per_operation: int = 250

    def __post_init__(self) -> None:
        merged = _default_metric_limits()
        for kind, lim in self.limits.items():
            resolved_kind = kind if isinstance(kind, QuotaMetricKind) else QuotaMetricKind(str(kind))
            merged[resolved_kind] = lim
        object.__setattr__(self, "limits", merged)
        if self.default_retry_after_seconds < 1:
            raise ValueError("default_retry_after_seconds must be >= 1.")
        if self.max_items_per_operation < 1:
            raise ValueError("max_items_per_operation must be >= 1.")

    def limit_for(self, metric_kind: QuotaMetricKind) -> QuotaMetricLimit:
        return self.limits[metric_kind]


class QuotaMeterItem(BaseModel):
    """Single quota meter snapshot matching Wave 2B Frontend contract 1:1."""

    model_config = ConfigDict(extra="forbid")

    metric_kind: QuotaMetricKind
    used_value: int = Field(..., ge=0)
    soft_warning_limit: int = Field(..., ge=0)
    hard_cutoff_limit: int = Field(..., ge=0)
    utilization_percent: float = Field(..., ge=0.0)
    status: QuotaMeterStatus


class QuotaStatusResponse(BaseModel):
    """GET /v1/quota-status response contract matching Wave 2B Frontend 1:1."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    admission_state: QuotaAdmissionState
    pause_state: RuntimePauseState
    pause_reason_code: str | None = None
    circuit_breaker_state: CircuitBreakerState
    high_cost_operations_allowed: bool
    safety_operations_allowed: Literal[True] = True
    paid_fallback_allowed: Literal[False] = False
    retry_after_seconds: int | None = None
    free_alternatives: list[str]
    meters: list[QuotaMeterItem]
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetReservationResult:
    """Outcome of a pre-execution budget admission & atomic reservation check."""

    allowed: bool
    operation_kind: BudgetOperationKind
    admission_state: QuotaAdmissionState
    safety_bypass: bool
    reserved_deltas: dict[QuotaMetricKind, int]
    period_key: str


@dataclass(frozen=True, slots=True)
class OperationUsageObservation:
    """Observed single-operation metrics validated against OperationBudgetConfig (PERF-01)."""

    item_count: int = 0
    request_count: int = 0
    total_bytes: int = 0
    unit_bytes: int = 0
    concurrency: int = 1
    tokens: int = 0


class BudgetError(Exception):
    """Base structured HTTP error raised by the quota/budget/pause subsystem."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        free_alternatives: tuple[str, ...] | list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_message = redact_text(message)
        super().__init__(f"{code}: {safe_message}")
        self.status_code = status_code
        self.code = code
        self.message = safe_message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.paid_fallback_allowed: Literal[False] = False
        self.free_alternatives = (
            list(free_alternatives)
            if free_alternatives is not None
            else list(DEFAULT_BUDGET_FREE_ALTERNATIVES)
        )
        self.details = details


class BudgetCutoffError(BudgetError):
    """Raised when a non-safety operation is blocked by 95% cutoff, pause, circuit breaker, or fail-closed error."""

    def __init__(
        self,
        *,
        code: str = "BUDGET_CUTOFF_REACHED",
        message: str,
        status_code: int = 429,
        retryable: bool = True,
        retry_after_seconds: int = 300,
        free_alternatives: tuple[str, ...] | list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            code=code,
            message=message,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            free_alternatives=free_alternatives,
            details=details,
        )


class ManualResumeVerificationError(BudgetError):
    """Raised when operator resume is attempted without ``budget_verified=True`` (ADR-020 / OD-015)."""

    def __init__(
        self,
        message: str = (
            "Manual runtime resume requires explicit budget_verified=True confirmation "
            "after verifying spend and quota headroom."
        ),
    ) -> None:
        super().__init__(
            status_code=409,
            code="MANUAL_RESUME_VERIFICATION_REQUIRED",
            message=message,
            retryable=False,
            retry_after_seconds=None,
        )


class OperationLimitExceededError(BudgetError):
    """Raised when single-operation item/size/call/byte/concurrency bounds are exceeded (PERF-01)."""

    def __init__(
        self,
        *,
        limit_name: str,
        observed: int,
        maximum: int,
    ) -> None:
        super().__init__(
            status_code=422,
            code="OPERATION_BUDGET_LIMIT_EXCEEDED",
            message=(
                f"Operation limit {limit_name!r} exceeded: observed={observed}, maximum={maximum}. "
                "Reduce input scope before retrying."
            ),
            retryable=False,
            retry_after_seconds=None,
            details={
                "limit_name": limit_name,
                "observed": int(observed),
                "maximum": int(maximum),
            },
        )


def compute_meter_item(
    *,
    metric_kind: QuotaMetricKind,
    used_value: int,
    soft_warning_limit: int,
    hard_cutoff_limit: int,
) -> QuotaMeterItem:
    """Compute deterministic utilization percentage & threshold status for a quota meter.

    ``hard_cutoff_limit`` represents the 95% cutoff threshold of the nominal cap (``hard / 0.95``),
    so ``used == soft_warning_limit`` maps to ~80% (``WARNING``) and ``used == hard_cutoff_limit``
    maps to 95.0% (``CUTOFF``). Disabled zero-spend meters (``hard_cutoff_limit == 0``) report
    ``OK`` while ``used_value == 0`` so fresh workspaces start in ``ALLOWED`` state, while any
    positive reservation delta is blocked fail-closed.
    """
    if hard_cutoff_limit == 0:
        if used_value == 0:
            return QuotaMeterItem(
                metric_kind=metric_kind,
                used_value=0,
                soft_warning_limit=soft_warning_limit,
                hard_cutoff_limit=0,
                utilization_percent=0.0,
                status=QuotaMeterStatus.OK,
            )
        return QuotaMeterItem(
            metric_kind=metric_kind,
            used_value=used_value,
            soft_warning_limit=soft_warning_limit,
            hard_cutoff_limit=0,
            utilization_percent=100.0,
            status=QuotaMeterStatus.CUTOFF,
        )

    nominal_cap = hard_cutoff_limit / 0.95
    utilization = round((used_value / nominal_cap) * 100.0, 2)
    if used_value >= hard_cutoff_limit:
        status = QuotaMeterStatus.CUTOFF
    elif soft_warning_limit > 0 and used_value >= soft_warning_limit:
        status = QuotaMeterStatus.WARNING
    else:
        status = QuotaMeterStatus.OK

    return QuotaMeterItem(
        metric_kind=metric_kind,
        used_value=used_value,
        soft_warning_limit=soft_warning_limit,
        hard_cutoff_limit=hard_cutoff_limit,
        utilization_percent=utilization,
        status=status,
    )


def enforce_operation_budget(
    config: OperationBudgetConfig,
    observation: OperationUsageObservation,
    *,
    max_items: int | None = None,
) -> None:
    """Enforce deterministic single-operation item/size/call/byte/concurrency bounds (PERF-01)."""
    if max_items is not None and observation.item_count > max_items:
        raise OperationLimitExceededError(
            limit_name="max_items",
            observed=observation.item_count,
            maximum=max_items,
        )
    if observation.request_count > config.max_requests:
        raise OperationLimitExceededError(
            limit_name="max_requests",
            observed=observation.request_count,
            maximum=config.max_requests,
        )
    if observation.total_bytes > config.max_total_bytes:
        raise OperationLimitExceededError(
            limit_name="max_total_bytes",
            observed=observation.total_bytes,
            maximum=config.max_total_bytes,
        )
    if observation.unit_bytes > config.max_unit_bytes:
        raise OperationLimitExceededError(
            limit_name="max_unit_bytes",
            observed=observation.unit_bytes,
            maximum=config.max_unit_bytes,
        )
    if observation.concurrency > config.max_concurrency:
        raise OperationLimitExceededError(
            limit_name="max_concurrency",
            observed=observation.concurrency,
            maximum=config.max_concurrency,
        )
    if config.max_tokens is not None and observation.tokens > config.max_tokens:
        raise OperationLimitExceededError(
            limit_name="max_tokens",
            observed=observation.tokens,
            maximum=config.max_tokens,
        )


__all__ = [
    "BudgetCutoffError",
    "BudgetError",
    "BudgetOperationKind",
    "BudgetReservationResult",
    "CircuitBreakerState",
    "DEFAULT_BUDGET_FREE_ALTERNATIVES",
    "DefaultQuotaPolicyConfig",
    "HIGH_COST_OPERATIONS",
    "ManualResumeVerificationError",
    "OperationLimitExceededError",
    "OperationUsageObservation",
    "QuotaAdmissionState",
    "QuotaMeterItem",
    "QuotaMeterStatus",
    "QuotaMetricLimit",
    "QuotaStatusResponse",
    "SAFETY_OPERATIONS",
    "compute_meter_item",
    "enforce_operation_budget",
    "format_period_key",
    "utc_now",
]
