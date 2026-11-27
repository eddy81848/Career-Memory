"""RUNTIME-INF06 / RUNTIME-INF06-FIX isolated synthetic spend/quota guard (local spike only).

Scope & Guardrails:
- Reproduces fail-closed admission, conservative unsettled-spend deduction after job
  completion, unified request/time caps (with immediate HALTED latch on the 100th admission),
  non-cancelled in-flight reservations, verified-only teardown budgets with distinct
  in-progress/succeeded/failed teardown tracking, and post-teardown manual resume.
- Strictly separates pure metering evaluation (`evaluate_snapshot_for_action`) from
  workload/teardown execution (`SpendGuard` + `FakeExecutor`).
- Known modeling boundary: synthetic snapshots do not carry provider-side per-request
  reconciliation IDs, so completed workload and started teardown max costs remain
  conservatively deducted in `unsettled_spend` (timestamp-only refreshes never release budget).
- NOT product billing control and NOT hosted verification. All preset numbers in
  `build_synthetic_test_policy()` are synthetic test fixtures ONLY and must NOT be
  proposed or used as operational thresholds.
- Single-process `threading.Lock` reservation does NOT prove multi-instance Cloud Run
  coordination or shared GCP/Supabase billing-account protection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import threading
from typing import Callable, Mapping, Protocol


class Dimension(str, Enum):
    """Canonical physical/logical dimensions for unit-safe arithmetic."""

    BYTES = "bytes"
    CPU_TIME_MS = "cpu_time_ms"
    MEM_TIME_MIB_MS = "mem_time_mib_ms"
    COUNT = "count"


class Unit(str, Enum):
    """Supported input units mapped to exact integer multipliers in canonical units."""

    # Data / storage / egress -> canonical BYTES
    B = "B"
    KB = "KB"
    MB = "MB"
    GB = "GB"
    KIB = "KiB"
    MIB = "MiB"
    GIB = "GiB"

    # Compute CPU-time -> canonical CPU_TIME_MS
    VCPU_MS = "vcpu_ms"
    VCPU_SEC = "vcpu_sec"
    VCPU_MIN = "vcpu_min"

    # Compute memory-time -> canonical MEM_TIME_MIB_MS
    MIB_SEC = "mib_sec"
    GIB_SEC = "gib_sec"

    # Discrete count -> canonical COUNT
    COUNT = "count"


_UNIT_SPECS: dict[Unit, tuple[Dimension, Decimal]] = {
    Unit.B: (Dimension.BYTES, Decimal(1)),
    Unit.KB: (Dimension.BYTES, Decimal(1_000)),
    Unit.MB: (Dimension.BYTES, Decimal(1_000_000)),
    Unit.GB: (Dimension.BYTES, Decimal(1_000_000_000)),
    Unit.KIB: (Dimension.BYTES, Decimal(1_024)),
    Unit.MIB: (Dimension.BYTES, Decimal(1_024 * 1_024)),
    Unit.GIB: (Dimension.BYTES, Decimal(1_024 * 1_024 * 1_024)),
    Unit.VCPU_MS: (Dimension.CPU_TIME_MS, Decimal(1)),
    Unit.VCPU_SEC: (Dimension.CPU_TIME_MS, Decimal(1_000)),
    Unit.VCPU_MIN: (Dimension.CPU_TIME_MS, Decimal(60_000)),
    Unit.MIB_SEC: (Dimension.MEM_TIME_MIB_MS, Decimal(1_000)),
    Unit.GIB_SEC: (Dimension.MEM_TIME_MIB_MS, Decimal(1_024 * 1_000)),
    Unit.COUNT: (Dimension.COUNT, Decimal(1)),
}


class UnitValidationError(ValueError):
    """Raised when a quantity has an invalid value, unknown unit, or wrong dimension."""


def _coerce_finite_nonneg_seconds(value: object) -> float | None:
    """Converts a non-boolean real number to a finite >= 0 float, or returns None."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, Decimal)):
        return None
    try:
        as_float = float(value)
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return None
    if not math.isfinite(as_float) or as_float < 0.0:
        return None
    return as_float


@dataclass(frozen=True)
class Quantity:
    """Typed non-negative quantity converted with exact Decimal arithmetic."""

    amount: int | Decimal | str
    unit: Unit | str

    def to_canonical(self, expected_dimension: Dimension) -> Decimal:
        if isinstance(self.amount, (bool, float)):
            raise UnitValidationError(f"float/bool not allowed in Quantity: {self.amount!r}")
        try:
            dec = self.amount if isinstance(self.amount, Decimal) else Decimal(self.amount)
        except (InvalidOperation, TypeError) as exc:
            raise UnitValidationError(f"invalid numeric amount: {self.amount!r}") from exc
        if not dec.is_finite() or dec < 0:
            raise UnitValidationError(f"quantity must be finite and >= 0, got {dec!r}")

        try:
            resolved_unit = self.unit if isinstance(self.unit, Unit) else Unit(self.unit)
        except ValueError as exc:
            raise UnitValidationError(f"unknown unit: {self.unit!r}") from exc

        dim, factor = _UNIT_SPECS[resolved_unit]
        if dim != expected_dimension:
            raise UnitValidationError(
                f"dimension mismatch: unit {resolved_unit.value} has {dim.value}, "
                f"expected {expected_dimension.value}"
            )
        return dec * factor


class MonotonicClock(Protocol):
    """Monotonic clock interface (seconds)."""

    def now(self) -> float: ...


class ManualMonotonicClock:
    """Deterministic monotonic clock for tests (never relies on wall-clock sleep)."""

    def __init__(self, initial_s: float = 0.0) -> None:
        coerced = _coerce_finite_nonneg_seconds(initial_s)
        if coerced is None:
            raise ValueError(f"initial_s must be a finite number >= 0, got {initial_s!r}")
        self._now_s = coerced

    def now(self) -> float:
        return self._now_s

    def advance(self, delta_s: float) -> float:
        coerced = _coerce_finite_nonneg_seconds(delta_s)
        if coerced is None:
            raise ValueError(f"delta_s must be a finite number >= 0, got {delta_s!r}")
        self._now_s += coerced
        return self._now_s

    def set(self, target_s: float) -> float:
        coerced = _coerce_finite_nonneg_seconds(target_s)
        if coerced is None or coerced < self._now_s:
            raise ValueError("monotonic clock requires finite target_s >= current time")
        self._now_s = coerced
        return self._now_s


class ActionKind(str, Enum):
    """Workload request kinds sharing the unified cap and elapsed time window."""

    JOB_CREATE = "job_create"
    EXTERNAL_CALL = "external_call"
    WAKE = "wake"
    RETRY = "retry"
    RECOVER = "recover"
    PROBE = "probe"
    UNESTIMATED_OP = "unestimated_op"


class TeardownStep(str, Enum):
    """Pre-approved safe teardown actions permitted ONLY while halted."""

    STOP_CALLER = "stop_caller"
    DELETE_CLOUD_RUN_SERVICES = "delete_cloud_run_services"
    DELETE_ARTIFACT_IMAGES = "delete_artifact_images"
    DESTROY_SECRETS_AND_SA = "destroy_secrets_and_sa"
    DROP_SPIKE_SCHEMA = "drop_spike_schema"


class GuardState(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"


class DecisionReason(str, Enum):
    ALLOWED = "ALLOWED"
    TEARDOWN_ALLOWED = "TEARDOWN_ALLOWED"

    # Metering & cost uncertainty reasons (distinct, fail-closed, zero fallback)
    METERING_MISSING = "METERING_MISSING"
    METERING_INVALID = "METERING_INVALID"
    METERING_STALE = "METERING_STALE"
    COST_UNKNOWN = "COST_UNKNOWN"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    PAID_FEATURE_DETECTED = "PAID_FEATURE_DETECTED"

    # Formula headroom & cap reasons
    QUOTA_HEADROOM_EXCEEDED = "QUOTA_HEADROOM_EXCEEDED"
    TEARDOWN_BUFFER_INVADED = "TEARDOWN_BUFFER_INVADED"
    REQUEST_CAP_EXCEEDED = "REQUEST_CAP_EXCEEDED"
    WINDOW_ELAPSED = "WINDOW_ELAPSED"

    # Halt & teardown boundary reasons
    GUARD_HALTED = "GUARD_HALTED"
    TEARDOWN_NOT_HALTED = "TEARDOWN_NOT_HALTED"
    TEARDOWN_BYPASS_REJECTED = "TEARDOWN_BYPASS_REJECTED"
    TEARDOWN_BUDGET_NOT_SECURED = "TEARDOWN_BUDGET_NOT_SECURED"
    TEARDOWN_BUDGET_EXCEEDED = "TEARDOWN_BUDGET_EXCEEDED"
    TEARDOWN_STEP_IN_PROGRESS = "TEARDOWN_STEP_IN_PROGRESS"
    TEARDOWN_STEP_ALREADY_RUN = "TEARDOWN_STEP_ALREADY_RUN"
    TEARDOWN_STEP_FAILED = "TEARDOWN_STEP_FAILED"
    TEARDOWN_EXECUTION_FAILED = "TEARDOWN_EXECUTION_FAILED"


class ResumeReason(str, Enum):
    RESUMED = "RESUMED"
    NOT_HALTED = "NOT_HALTED"
    CAUSE_NOT_RESOLVED = "CAUSE_NOT_RESOLVED"
    TEARDOWN_INCOMPLETE = "TEARDOWN_INCOMPLETE"
    IN_FLIGHT_WORK_ACTIVE = "IN_FLIGHT_WORK_ACTIVE"
    MISSING_OPERATOR_APPROVAL = "MISSING_OPERATOR_APPROVAL"
    HEADROOM_RECHECK_FAILED = "HEADROOM_RECHECK_FAILED"
    PRE_TEARDOWN_SNAPSHOT = "PRE_TEARDOWN_SNAPSHOT"


_OMITTED: object = object()


@dataclass(frozen=True)
class ResourceMetricSnapshot:
    """Single resource entry in a synthetic metering snapshot."""

    verified_remaining: Quantity | None
    metering_lag_and_shared_bound: Quantity | None
    observed_at_s: float | None
    safe_teardown_buffer: Quantity | None = None


@dataclass(frozen=True)
class SyntheticMeterSnapshot:
    """Immutable synthetic metering snapshot passed into the reader layer.

    `paid_feature_active` has NO implicit `False` default: ONLY an explicit `False`
    (`type(x) is bool and x is False`) counts as verified inactive.
    """

    resources: Mapping[str, ResourceMetricSnapshot]
    paid_feature_active: bool | None | object = _OMITTED


@dataclass(frozen=True)
class ResourcePolicySpec:
    """Required resource definition and policy teardown buffer requirement."""

    resource_id: str
    dimension: Dimension
    default_safe_teardown_buffer: Quantity | None


@dataclass(frozen=True)
class SpendGuardPolicy:
    """Synthetic guard policy (test-only values; NOT operational thresholds)."""

    resources: tuple[ResourcePolicySpec, ...]
    action_max_costs: Mapping[ActionKind, Mapping[str, Quantity | None] | None]
    teardown_step_costs: Mapping[TeardownStep, Mapping[str, Quantity]]
    required_teardown_steps: frozenset[TeardownStep]
    max_staleness_s: float = 60.0
    max_total_requests: int = 100
    max_elapsed_s: float = 1_800.0  # 30 minutes
    resume_reference_action: ActionKind = ActionKind.PROBE


@dataclass(frozen=True)
class ResourceHeadroomBreakdown:
    """Canonical-unit breakdown of the conservative headroom formula for one resource."""

    resource_id: str
    dimension: Dimension
    verified_remaining: Decimal
    metering_lag_and_shared_bound: Decimal
    in_flight_reserved: Decimal
    next_request_max_cost: Decimal
    safe_teardown_buffer: Decimal
    available_before_teardown_buffer: Decimal
    headroom_after_all: Decimal
    unsettled_spend: Decimal = Decimal(0)


@dataclass(frozen=True)
class MeterEvaluation:
    """Pure output of the metering reader layer (no side effects or execution)."""

    ok: bool
    reason: DecisionReason
    detail: str
    breakdowns: dict[str, ResourceHeadroomBreakdown] = field(default_factory=dict)


def evaluate_snapshot_for_action(
    *,
    snapshot: SyntheticMeterSnapshot | None,
    policy: SpendGuardPolicy,
    action: ActionKind,
    now_s: float,
    in_flight_reserved: Mapping[str, Decimal],
    unsettled_spend: Mapping[str, Decimal] | None = None,
) -> MeterEvaluation:
    """Pure metering reader & conservative headroom evaluator.

    Verifies for EVERY required resource (after normalizing compatible units):
        verified_remaining
        - metering_lag_and_shared_bound
        - (in_flight_reserved + unsettled_spend)
        - next_request_max_cost
        - safe_teardown_buffer >= 0
    Never mutates guard state and never invokes any executor or fallback path.
    """
    now_f = _coerce_finite_nonneg_seconds(now_s)
    max_stale_f = _coerce_finite_nonneg_seconds(policy.max_staleness_s)
    if now_f is None or max_stale_f is None:
        return MeterEvaluation(
            False,
            DecisionReason.METERING_INVALID,
            f"non-finite clock or staleness bound: now_s={now_s!r}, max_staleness_s={policy.max_staleness_s!r}",
        )

    if snapshot is None:
        return MeterEvaluation(False, DecisionReason.METERING_MISSING, "snapshot is None")

    if not isinstance(snapshot, SyntheticMeterSnapshot) or not isinstance(
        snapshot.resources, Mapping
    ):
        return MeterEvaluation(
            False,
            DecisionReason.METERING_INVALID,
            f"invalid snapshot or resources container: {snapshot!r}",
        )

    if snapshot.paid_feature_active is _OMITTED or snapshot.paid_feature_active is None:
        return MeterEvaluation(
            False,
            DecisionReason.COST_UNKNOWN,
            "paid_feature_active omitted or None (billing/paid-feature status unknown)",
        )
    if type(snapshot.paid_feature_active) is not bool:
        return MeterEvaluation(
            False,
            DecisionReason.METERING_INVALID,
            f"paid_feature_active must be explicit bool without coercion, got {snapshot.paid_feature_active!r}",
        )
    if snapshot.paid_feature_active is True:
        return MeterEvaluation(
            False,
            DecisionReason.PAID_FEATURE_DETECTED,
            "paid feature (e.g. paid scanning / paid connector) is active",
        )

    if not isinstance(action, ActionKind):
        return MeterEvaluation(
            False,
            DecisionReason.COST_UNKNOWN,
            f"unrecognized action kind: {action!r}",
        )

    cost_map = policy.action_max_costs.get(action)
    if cost_map is None:
        return MeterEvaluation(
            False,
            DecisionReason.COST_UNKNOWN,
            f"no max-cost profile registered for action {action.value}",
        )

    normalized_inputs: list[
        tuple[ResourcePolicySpec, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]
    ] = []
    for spec in policy.resources:
        rid = spec.resource_id
        metric = snapshot.resources.get(rid)
        if metric is None:
            return MeterEvaluation(
                False,
                DecisionReason.METERING_MISSING,
                f"missing required resource metric: {rid}",
            )
        if not isinstance(metric, ResourceMetricSnapshot):
            return MeterEvaluation(
                False,
                DecisionReason.METERING_INVALID,
                f"invalid resource metric type for {rid}: {metric!r}",
            )
        if metric.verified_remaining is None or metric.observed_at_s is None:
            return MeterEvaluation(
                False,
                DecisionReason.METERING_MISSING,
                f"incomplete metric reading (remaining/timestamp None) for {rid}",
            )

        obs_f = _coerce_finite_nonneg_seconds(metric.observed_at_s)
        if obs_f is None:
            return MeterEvaluation(
                False,
                DecisionReason.METERING_INVALID,
                f"non-finite or invalid observed_at_s for {rid}: {metric.observed_at_s!r}",
            )

        age_s = now_f - obs_f
        if not math.isfinite(age_s) or not (0.0 <= age_s <= max_stale_f):
            return MeterEvaluation(
                False,
                DecisionReason.METERING_STALE,
                f"stale or future metric for {rid}: age_s={age_s:.3f}, max={max_stale_f:.3f}",
            )

        raw_lag = metric.metering_lag_and_shared_bound
        raw_buffer = (
            metric.safe_teardown_buffer
            if metric.safe_teardown_buffer is not None
            else spec.default_safe_teardown_buffer
        )
        raw_next_cost = cost_map.get(rid)
        if raw_lag is None or raw_buffer is None or raw_next_cost is None:
            return MeterEvaluation(
                False,
                DecisionReason.COST_UNKNOWN,
                f"unknown lag/buffer/cost bound for {rid}",
            )
        if not all(
            isinstance(q, Quantity)
            for q in (metric.verified_remaining, raw_lag, raw_buffer, raw_next_cost)
        ):
            return MeterEvaluation(
                False,
                DecisionReason.METERING_INVALID,
                f"non-Quantity input supplied on {rid}",
            )

        try:
            rem_c = metric.verified_remaining.to_canonical(spec.dimension)
            lag_c = raw_lag.to_canonical(spec.dimension)
            buf_c = raw_buffer.to_canonical(spec.dimension)
            cost_c = raw_next_cost.to_canonical(spec.dimension)
        except UnitValidationError as exc:
            return MeterEvaluation(
                False,
                DecisionReason.UNIT_MISMATCH,
                f"unit validation failed on {rid}: {exc}",
            )

        inflight_c = in_flight_reserved.get(rid, Decimal(0))
        unsettled_c = unsettled_spend.get(rid, Decimal(0)) if unsettled_spend else Decimal(0)
        normalized_inputs.append((spec, rem_c, lag_c, inflight_c, unsettled_c, cost_c, buf_c))

    breakdowns: dict[str, ResourceHeadroomBreakdown] = {}
    quota_exceeded_rid: str | None = None
    buffer_invaded_rid: str | None = None

    for spec, rem_c, lag_c, inflight_c, unsettled_c, cost_c, buf_c in normalized_inputs:
        before_buffer = rem_c - lag_c - inflight_c - unsettled_c - cost_c
        after_all = before_buffer - buf_c
        breakdowns[spec.resource_id] = ResourceHeadroomBreakdown(
            resource_id=spec.resource_id,
            dimension=spec.dimension,
            verified_remaining=rem_c,
            metering_lag_and_shared_bound=lag_c,
            in_flight_reserved=inflight_c,
            next_request_max_cost=cost_c,
            safe_teardown_buffer=buf_c,
            available_before_teardown_buffer=before_buffer,
            headroom_after_all=after_all,
            unsettled_spend=unsettled_c,
        )
        if before_buffer < 0 and quota_exceeded_rid is None:
            quota_exceeded_rid = spec.resource_id
        elif before_buffer >= 0 and after_all < 0 and buffer_invaded_rid is None:
            buffer_invaded_rid = spec.resource_id

    if quota_exceeded_rid is not None:
        b = breakdowns[quota_exceeded_rid]
        return MeterEvaluation(
            False,
            DecisionReason.QUOTA_HEADROOM_EXCEEDED,
            f"quota headroom exceeded on {quota_exceeded_rid}: before_buffer={b.available_before_teardown_buffer}",
            breakdowns,
        )
    if buffer_invaded_rid is not None:
        b = breakdowns[buffer_invaded_rid]
        return MeterEvaluation(
            False,
            DecisionReason.TEARDOWN_BUFFER_INVADED,
            f"safe teardown buffer invaded on {buffer_invaded_rid}: headroom_after_all={b.headroom_after_all}",
            breakdowns,
        )

    return MeterEvaluation(True, DecisionReason.ALLOWED, "all resource bounds verified", breakdowns)


@dataclass(frozen=True)
class ExecutionRecord:
    """Immutable log record captured by FakeExecutor."""

    seq: int
    category: str  # "workload" or "teardown"
    kind: str
    lease_id: str
    timestamp_s: float
    detail: str = ""


class FakeExecutor:
    """Pure in-memory recorder replacing external calls, jobs, wakes, retries, recovers."""

    def __init__(
        self,
        clock: MonotonicClock,
        *,
        on_workload_enter: Callable[[ActionKind, str], None] | None = None,
        on_teardown_enter: Callable[[TeardownStep, str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._on_workload_enter = on_workload_enter
        self._on_teardown_enter = on_teardown_enter
        self._lock = threading.Lock()
        self._records: list[ExecutionRecord] = []
        self._workload_counts: dict[ActionKind, int] = {k: 0 for k in ActionKind}
        self._teardown_counts: dict[TeardownStep, int] = {s: 0 for s in TeardownStep}
        self._fallback_calls = 0

    def run_workload(self, action: ActionKind, lease_id: str, detail: str = "") -> ExecutionRecord:
        if not isinstance(action, ActionKind):
            raise TypeError(f"FakeExecutor.run_workload requires ActionKind, got {action!r}")
        with self._lock:
            seq = len(self._records) + 1
            rec = ExecutionRecord(
                seq=seq,
                category="workload",
                kind=action.value,
                lease_id=lease_id,
                timestamp_s=self._clock.now(),
                detail=detail,
            )
            self._records.append(rec)
            self._workload_counts[action] = self._workload_counts.get(action, 0) + 1
        if self._on_workload_enter is not None:
            self._on_workload_enter(action, lease_id)
        return rec

    def run_teardown(self, step: TeardownStep, lease_id: str, detail: str = "") -> ExecutionRecord:
        if not isinstance(step, TeardownStep):
            raise TypeError(f"FakeExecutor.run_teardown requires TeardownStep, got {step!r}")
        with self._lock:
            seq = len(self._records) + 1
            rec = ExecutionRecord(
                seq=seq,
                category="teardown",
                kind=step.value,
                lease_id=lease_id,
                timestamp_s=self._clock.now(),
                detail=detail,
            )
            self._records.append(rec)
            self._teardown_counts[step] = self._teardown_counts.get(step, 0) + 1
        if self._on_teardown_enter is not None:
            self._on_teardown_enter(step, lease_id)
        return rec

    @property
    def records(self) -> tuple[ExecutionRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def workload_calls_total(self) -> int:
        with self._lock:
            return sum(self._workload_counts.values())

    @property
    def jobs_created(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.JOB_CREATE]

    @property
    def external_calls(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.EXTERNAL_CALL]

    @property
    def wakes(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.WAKE]

    @property
    def retries(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.RETRY]

    @property
    def recovers(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.RECOVER]

    @property
    def probes(self) -> int:
        with self._lock:
            return self._workload_counts[ActionKind.PROBE]

    @property
    def teardown_calls_total(self) -> int:
        with self._lock:
            return sum(self._teardown_counts.values())

    @property
    def fallback_calls(self) -> int:
        with self._lock:
            return self._fallback_calls

    def count_for(self, action: ActionKind) -> int:
        with self._lock:
            return self._workload_counts[action]


@dataclass(frozen=True)
class InFlightReservation:
    lease_id: str
    action: ActionKind
    reserved_canonical: dict[str, Decimal]
    started_at_s: float


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: DecisionReason
    detail: str
    guard_state: GuardState
    shared_request_count: int
    in_flight_count: int
    in_flight_reserved: dict[str, Decimal]
    lease_id: str | None = None
    evaluation: MeterEvaluation | None = None


@dataclass(frozen=True)
class ResumeDecision:
    resumed: bool
    reason: ResumeReason
    detail: str
    guard_state: GuardState
    evaluation: MeterEvaluation | None = None


class InFlightHandle:
    """Handle for an already-started workload whose reservation persists until finished."""

    def __init__(self, guard: SpendGuard, decision: GuardDecision) -> None:
        self._guard = guard
        self.decision = decision
        self._finished = False

    def finish(self) -> None:
        if not self._finished and self.decision.lease_id is not None:
            self._finished = True
            self._guard.complete_in_flight(self.decision.lease_id)


class SpendGuard:
    """Stateful fail-closed coordinator connecting pure meter evaluation to FakeExecutor."""

    def __init__(
        self,
        *,
        policy: SpendGuardPolicy,
        clock: MonotonicClock,
        executor: FakeExecutor,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._executor = executor
        self._lock = threading.Lock()
        self._state = GuardState.ACTIVE
        self._halt_reason: str | None = None
        self._halted_at_s: float | None = None
        self._last_teardown_completed_at_s: float | None = None
        self._last_in_flight_finished_at_s: float | None = None
        self._window_started_at_s = clock.now()
        self._shared_request_count = 0
        self._lease_seq = 0
        self._active_reservations: dict[str, InFlightReservation] = {}
        self._unsettled_spend: dict[str, Decimal] = {
            spec.resource_id: Decimal(0) for spec in policy.resources
        }
        self._peak_in_flight_count = 0

        # Teardown budget starts UNSECURED (0) until a valid snapshot actually verifies it.
        self._teardown_budget_secured = False
        self._secured_teardown_budget: dict[str, Decimal] = {
            spec.resource_id: Decimal(0) for spec in policy.resources
        }
        self._teardown_consumed: dict[str, Decimal] = {
            spec.resource_id: Decimal(0) for spec in policy.resources
        }
        self._in_progress_teardown_steps: set[TeardownStep] = set()
        self._completed_teardown_steps: set[TeardownStep] = set()
        self._failed_teardown_steps: set[TeardownStep] = set()

    @property
    def state(self) -> GuardState:
        with self._lock:
            return self._state

    @property
    def halt_reason(self) -> str | None:
        with self._lock:
            return self._halt_reason

    @property
    def shared_request_count(self) -> int:
        with self._lock:
            return self._shared_request_count

    @property
    def peak_in_flight_count(self) -> int:
        with self._lock:
            return self._peak_in_flight_count

    @property
    def teardown_budget_secured(self) -> bool:
        with self._lock:
            return self._teardown_budget_secured

    @property
    def in_progress_teardown_steps(self) -> frozenset[TeardownStep]:
        with self._lock:
            return frozenset(self._in_progress_teardown_steps)

    @property
    def completed_teardown_steps(self) -> frozenset[TeardownStep]:
        with self._lock:
            return frozenset(self._completed_teardown_steps)

    @property
    def failed_teardown_steps(self) -> frozenset[TeardownStep]:
        with self._lock:
            return frozenset(self._failed_teardown_steps)

    def in_flight_reserved_snapshot(self) -> dict[str, Decimal]:
        with self._lock:
            return self._sum_in_flight_locked()

    def unsettled_spend_snapshot(self) -> dict[str, Decimal]:
        with self._lock:
            return dict(self._unsettled_spend)

    def _sum_in_flight_locked(self) -> dict[str, Decimal]:
        totals = {spec.resource_id: Decimal(0) for spec in self._policy.resources}
        for res in self._active_reservations.values():
            for rid, amount in res.reserved_canonical.items():
                totals[rid] = totals.get(rid, Decimal(0)) + amount
        return totals

    def _latch_halt_locked(self, reason: str, now_s: float | None) -> None:
        self._state = GuardState.HALTED
        if self._halt_reason is None:
            self._halt_reason = reason
        if self._halted_at_s is None and now_s is not None:
            self._halted_at_s = now_s

    def _secure_teardown_budget_from_eval_locked(self, eval_res: MeterEvaluation) -> None:
        self._teardown_budget_secured = True
        self._secured_teardown_budget = {
            rid: b.safe_teardown_buffer for rid, b in eval_res.breakdowns.items()
        }

    def halt(self, reason: str = "MANUAL_KILL_SWITCH") -> None:
        """Halts new workload admissions WITHOUT cancelling in-flight reservations."""
        with self._lock:
            now_f = _coerce_finite_nonneg_seconds(self._clock.now())
            self._latch_halt_locked(reason, now_f)

    def begin_in_flight(
        self,
        action: ActionKind,
        snapshot: SyntheticMeterSnapshot | None,
        *,
        detail: str = "",
    ) -> tuple[GuardDecision, InFlightHandle | None]:
        """Admits and starts a workload; finished work stays conservatively unsettled."""
        with self._lock:
            raw_now = self._clock.now()
            now_f = _coerce_finite_nonneg_seconds(raw_now)
            max_elapsed_f = _coerce_finite_nonneg_seconds(self._policy.max_elapsed_s)
            in_flight_before = self._sum_in_flight_locked()

            if not isinstance(action, ActionKind):
                return (
                    GuardDecision(
                        allowed=False,
                        reason=DecisionReason.COST_UNKNOWN,
                        detail=f"workload path requires ActionKind, got {action!r}",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                    ),
                    None,
                )

            if now_f is None or max_elapsed_f is None:
                return (
                    GuardDecision(
                        allowed=False,
                        reason=DecisionReason.METERING_INVALID,
                        detail=f"non-finite clock or elapsed limit: now={raw_now!r}",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                    ),
                    None,
                )

            if self._state == GuardState.HALTED:
                halted_reason = (
                    DecisionReason.REQUEST_CAP_EXCEEDED
                    if (
                        self._halt_reason == DecisionReason.REQUEST_CAP_EXCEEDED.value
                        and self._shared_request_count >= self._policy.max_total_requests
                    )
                    else DecisionReason.GUARD_HALTED
                )
                return (
                    GuardDecision(
                        allowed=False,
                        reason=halted_reason,
                        detail=f"guard is HALTED ({self._halt_reason}); manual resume required",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                    ),
                    None,
                )

            elapsed_s = now_f - self._window_started_at_s
            if not math.isfinite(elapsed_s) or elapsed_s < 0 or elapsed_s >= max_elapsed_f:
                self._latch_halt_locked(DecisionReason.WINDOW_ELAPSED.value, now_f)
                return (
                    GuardDecision(
                        allowed=False,
                        reason=DecisionReason.WINDOW_ELAPSED,
                        detail=f"elapsed {elapsed_s:.1f}s >= limit {max_elapsed_f:.1f}s",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                    ),
                    None,
                )

            if self._shared_request_count >= self._policy.max_total_requests:
                self._latch_halt_locked(DecisionReason.REQUEST_CAP_EXCEEDED.value, now_f)
                return (
                    GuardDecision(
                        allowed=False,
                        reason=DecisionReason.REQUEST_CAP_EXCEEDED,
                        detail=(
                            f"unified request count {self._shared_request_count} "
                            f"reached cap {self._policy.max_total_requests}"
                        ),
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                    ),
                    None,
                )

            eval_res = evaluate_snapshot_for_action(
                snapshot=snapshot,
                policy=self._policy,
                action=action,
                now_s=now_f,
                in_flight_reserved=in_flight_before,
                unsettled_spend=self._unsettled_spend,
            )
            if not eval_res.ok:
                if eval_res.reason == DecisionReason.PAID_FEATURE_DETECTED:
                    self._latch_halt_locked(DecisionReason.PAID_FEATURE_DETECTED.value, now_f)
                return (
                    GuardDecision(
                        allowed=False,
                        reason=eval_res.reason,
                        detail=eval_res.detail,
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_before,
                        evaluation=eval_res,
                    ),
                    None,
                )

            # Valid admission verifies & secures the safe teardown buffer for this cycle.
            self._secure_teardown_budget_from_eval_locked(eval_res)

            self._lease_seq += 1
            lease_id = f"lease-{self._lease_seq:04d}"
            self._shared_request_count += 1
            reserved_costs = {
                rid: b.next_request_max_cost for rid, b in eval_res.breakdowns.items()
            }
            self._active_reservations[lease_id] = InFlightReservation(
                lease_id=lease_id,
                action=action,
                reserved_canonical=reserved_costs,
                started_at_s=now_f,
            )
            if len(self._active_reservations) > self._peak_in_flight_count:
                self._peak_in_flight_count = len(self._active_reservations)

            # Immediately transition to HALTED once the cap-th (e.g. 100th) request is admitted,
            # while still letting this admitted request proceed to completion.
            if self._shared_request_count >= self._policy.max_total_requests:
                self._latch_halt_locked(DecisionReason.REQUEST_CAP_EXCEEDED.value, now_f)

            decision = GuardDecision(
                allowed=True,
                reason=DecisionReason.ALLOWED,
                detail=eval_res.detail,
                guard_state=self._state,
                shared_request_count=self._shared_request_count,
                in_flight_count=len(self._active_reservations),
                in_flight_reserved=self._sum_in_flight_locked(),
                lease_id=lease_id,
                evaluation=eval_res,
            )

        try:
            self._executor.run_workload(action, lease_id, detail=detail)
        except Exception:
            # Even if workload execution fails mid-flight, move cost to unsettled (never assume 0).
            self.complete_in_flight(lease_id)
            raise
        return decision, InFlightHandle(self, decision)

    def execute(
        self,
        action: ActionKind,
        snapshot: SyntheticMeterSnapshot | None,
        *,
        detail: str = "",
    ) -> GuardDecision:
        """Synchronous admission + FakeExecutor invocation + transition to unsettled spend."""
        decision, handle = self.begin_in_flight(action, snapshot, detail=detail)
        if handle is None:
            return decision
        try:
            return decision
        finally:
            handle.finish()

    def complete_in_flight(self, lease_id: str) -> None:
        """Marks an active workload finished while retaining its max cost as unsettled spend."""
        with self._lock:
            res = self._active_reservations.pop(lease_id, None)
            if res is not None:
                for rid, amount in res.reserved_canonical.items():
                    self._unsettled_spend[rid] = self._unsettled_spend.get(rid, Decimal(0)) + amount
                now_f = _coerce_finite_nonneg_seconds(self._clock.now())
                if now_f is not None:
                    self._last_in_flight_finished_at_s = now_f

    def execute_teardown(
        self,
        step: object,
        *,
        requested_action: object | None = None,
        detail: str = "",
    ) -> GuardDecision:
        """Executes ONLY pre-approved TeardownStep actions while HALTED within secured budget."""
        with self._lock:
            in_flight_now = self._sum_in_flight_locked()

            if not isinstance(step, TeardownStep) or requested_action is not None:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_BYPASS_REJECTED,
                    detail=(
                        f"non-teardown or smuggled workload rejected on teardown path: "
                        f"step={step!r}, requested_action={requested_action!r}"
                    ),
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            if self._state != GuardState.HALTED:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_NOT_HALTED,
                    detail="safe teardown path is only active when guard is HALTED",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            if step in self._in_progress_teardown_steps:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_STEP_IN_PROGRESS,
                    detail=f"teardown step {step.value} is currently in progress",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            if step in self._completed_teardown_steps:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_STEP_ALREADY_RUN,
                    detail=f"teardown step {step.value} already succeeded in this halt cycle",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            if step in self._failed_teardown_steps:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_STEP_FAILED,
                    detail=f"teardown step {step.value} previously failed; automatic retry prohibited",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            step_cost_map = self._policy.teardown_step_costs.get(step)
            if step_cost_map is None:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_BYPASS_REJECTED,
                    detail=f"teardown step {step.value} is not pre-registered in policy",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            canonical_step_costs: dict[str, Decimal] = {}
            has_nonzero_cost = False
            for spec in self._policy.resources:
                rid = spec.resource_id
                q = step_cost_map.get(rid, Quantity(0, _default_unit_for(spec.dimension)))
                if spec.default_safe_teardown_buffer is None:
                    return GuardDecision(
                        allowed=False,
                        reason=DecisionReason.TEARDOWN_BUDGET_EXCEEDED,
                        detail=f"no teardown buffer configured for {rid}",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_now,
                    )
                try:
                    step_c = q.to_canonical(spec.dimension)
                except UnitValidationError as exc:
                    return GuardDecision(
                        allowed=False,
                        reason=DecisionReason.UNIT_MISMATCH,
                        detail=f"teardown unit validation failed on {rid}: {exc}",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_now,
                    )
                if step_c > 0:
                    has_nonzero_cost = True
                canonical_step_costs[rid] = step_c

            if has_nonzero_cost and not self._teardown_budget_secured:
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_BUDGET_NOT_SECURED,
                    detail=(
                        f"cost-bearing teardown step {step.value} blocked: "
                        "teardown budget was never verified/secured in this run"
                    ),
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=in_flight_now,
                )

            for rid, step_c in canonical_step_costs.items():
                if self._teardown_consumed[rid] + step_c > self._secured_teardown_budget[rid]:
                    return GuardDecision(
                        allowed=False,
                        reason=DecisionReason.TEARDOWN_BUDGET_EXCEEDED,
                        detail=f"teardown step {step.value} exceeds secured buffer on {rid}",
                        guard_state=self._state,
                        shared_request_count=self._shared_request_count,
                        in_flight_count=len(self._active_reservations),
                        in_flight_reserved=in_flight_now,
                    )

            # Charge cost upfront so failure/unknown outcome never assumes zero spend.
            for rid, step_c in canonical_step_costs.items():
                self._teardown_consumed[rid] += step_c
                self._unsettled_spend[rid] = self._unsettled_spend.get(rid, Decimal(0)) + step_c
            self._in_progress_teardown_steps.add(step)
            self._lease_seq += 1
            teardown_id = f"teardown-{self._lease_seq:04d}"

        try:
            self._executor.run_teardown(step, teardown_id, detail=detail)
        except Exception as exc:
            with self._lock:
                self._in_progress_teardown_steps.discard(step)
                self._failed_teardown_steps.add(step)
                return GuardDecision(
                    allowed=False,
                    reason=DecisionReason.TEARDOWN_EXECUTION_FAILED,
                    detail=f"teardown step {step.value} failed (no auto-retry): {exc!r}",
                    guard_state=self._state,
                    shared_request_count=self._shared_request_count,
                    in_flight_count=len(self._active_reservations),
                    in_flight_reserved=self._sum_in_flight_locked(),
                    lease_id=teardown_id,
                )

        with self._lock:
            self._in_progress_teardown_steps.discard(step)
            self._completed_teardown_steps.add(step)
            now_td = _coerce_finite_nonneg_seconds(self._clock.now())
            if now_td is not None:
                self._last_teardown_completed_at_s = now_td
            return GuardDecision(
                allowed=True,
                reason=DecisionReason.TEARDOWN_ALLOWED,
                detail=f"executed pre-secured safe teardown step {step.value}",
                guard_state=self._state,
                shared_request_count=self._shared_request_count,
                in_flight_count=len(self._active_reservations),
                in_flight_reserved=self._sum_in_flight_locked(),
                lease_id=teardown_id,
            )

    def manual_resume(
        self,
        *,
        snapshot: SyntheticMeterSnapshot | None,
        cause_resolved: bool,
        operator_approval: str,
    ) -> ResumeDecision:
        """Explicit manual resume gate requiring post-teardown valid metering & settled state."""
        with self._lock:
            if self._state != GuardState.HALTED:
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.NOT_HALTED,
                    detail="guard is already ACTIVE",
                    guard_state=self._state,
                )

            if type(cause_resolved) is not bool or cause_resolved is not True:
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.CAUSE_NOT_RESOLVED,
                    detail="root cause of halt must be explicitly verified (cause_resolved=True)",
                    guard_state=self._state,
                )

            missing_steps = self._policy.required_teardown_steps - self._completed_teardown_steps
            if (
                self._in_progress_teardown_steps
                or self._failed_teardown_steps
                or missing_steps
            ):
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.TEARDOWN_INCOMPLETE,
                    detail=(
                        f"safe teardown not cleanly completed: "
                        f"in_progress={sorted(s.value for s in self._in_progress_teardown_steps)}, "
                        f"failed={sorted(s.value for s in self._failed_teardown_steps)}, "
                        f"missing={sorted(s.value for s in missing_steps)}"
                    ),
                    guard_state=self._state,
                )

            if self._active_reservations:
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.IN_FLIGHT_WORK_ACTIVE,
                    detail=f"{len(self._active_reservations)} pre-halt reservation(s) still active",
                    guard_state=self._state,
                )

            if not isinstance(operator_approval, str) or not operator_approval.strip():
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.MISSING_OPERATOR_APPROVAL,
                    detail="explicit non-empty operator_approval is required",
                    guard_state=self._state,
                )

            now_f = _coerce_finite_nonneg_seconds(self._clock.now())
            if now_f is None:
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.HEADROOM_RECHECK_FAILED,
                    detail="clock returned non-finite timestamp during resume check",
                    guard_state=self._state,
                )

            eval_res = evaluate_snapshot_for_action(
                snapshot=snapshot,
                policy=self._policy,
                action=self._policy.resume_reference_action,
                now_s=now_f,
                in_flight_reserved=self._sum_in_flight_locked(),
                unsettled_spend=self._unsettled_spend,
            )
            if not eval_res.ok:
                return ResumeDecision(
                    resumed=False,
                    reason=ResumeReason.HEADROOM_RECHECK_FAILED,
                    detail=(
                        f"post-teardown snapshot check failed ({eval_res.reason.value}): "
                        f"{eval_res.detail}"
                    ),
                    guard_state=self._state,
                    evaluation=eval_res,
                )

            # Reject any snapshot observed before/at halt, teardown completion, or in-flight finish.
            boundary_candidates = [
                t
                for t in (
                    self._halted_at_s,
                    self._last_teardown_completed_at_s,
                    self._last_in_flight_finished_at_s,
                )
                if t is not None
            ]
            if boundary_candidates and snapshot is not None:
                min_after_s = max(boundary_candidates)
                for spec in self._policy.resources:
                    obs_f = _coerce_finite_nonneg_seconds(
                        snapshot.resources[spec.resource_id].observed_at_s
                    )
                    if obs_f is None or obs_f <= min_after_s:
                        return ResumeDecision(
                            resumed=False,
                            reason=ResumeReason.PRE_TEARDOWN_SNAPSHOT,
                            detail=(
                                f"snapshot for {spec.resource_id} observed_at_s={obs_f} "
                                f"is not strictly after teardown/settlement timestamp {min_after_s}"
                            ),
                            guard_state=self._state,
                            evaluation=eval_res,
                        )

            # All prerequisites verified -> open a fresh window while keeping unsettled_spend deducted.
            self._state = GuardState.ACTIVE
            self._halt_reason = None
            self._halted_at_s = None
            self._last_teardown_completed_at_s = None
            self._last_in_flight_finished_at_s = None
            self._window_started_at_s = now_f
            self._shared_request_count = 0
            self._in_progress_teardown_steps.clear()
            self._completed_teardown_steps.clear()
            self._failed_teardown_steps.clear()
            self._teardown_consumed = {
                spec.resource_id: Decimal(0) for spec in self._policy.resources
            }
            self._secure_teardown_budget_from_eval_locked(eval_res)
            return ResumeDecision(
                resumed=True,
                reason=ResumeReason.RESUMED,
                detail=f"manually resumed by {operator_approval.strip()}",
                guard_state=self._state,
                evaluation=eval_res,
            )


def _default_unit_for(dimension: Dimension) -> Unit:
    if dimension == Dimension.BYTES:
        return Unit.B
    if dimension == Dimension.CPU_TIME_MS:
        return Unit.VCPU_MS
    if dimension == Dimension.MEM_TIME_MIB_MS:
        return Unit.MIB_SEC
    return Unit.COUNT


def build_synthetic_test_policy(
    *,
    max_staleness_s: float = 60.0,
    max_total_requests: int = 100,
    max_elapsed_s: float = 1_800.0,
) -> SpendGuardPolicy:
    """Constructs a SYNTHETIC TEST-ONLY policy covering Cloud Run/AR/Logs/Secrets/Supabase.

    WARNING: Numbers below exist strictly for deterministic unit tests of the guard
    formula and state machine. Do NOT treat them as operational quotas or production caps.
    """
    resources = (
        ResourcePolicySpec(
            resource_id="cloud_run_cpu",
            dimension=Dimension.CPU_TIME_MS,
            default_safe_teardown_buffer=Quantity(5, Unit.VCPU_SEC),
        ),
        ResourcePolicySpec(
            resource_id="cloud_run_mem",
            dimension=Dimension.MEM_TIME_MIB_MS,
            default_safe_teardown_buffer=Quantity(2, Unit.GIB_SEC),
        ),
        ResourcePolicySpec(
            resource_id="artifact_registry_storage",
            dimension=Dimension.BYTES,
            default_safe_teardown_buffer=Quantity(100, Unit.MIB),
        ),
        ResourcePolicySpec(
            resource_id="cloud_logging_ingestion",
            dimension=Dimension.BYTES,
            default_safe_teardown_buffer=Quantity(10, Unit.MIB),
        ),
        ResourcePolicySpec(
            resource_id="secret_versions",
            dimension=Dimension.COUNT,
            default_safe_teardown_buffer=Quantity(1, Unit.COUNT),
        ),
        ResourcePolicySpec(
            resource_id="supabase_uncached_egress",
            dimension=Dimension.BYTES,
            default_safe_teardown_buffer=Quantity(50, Unit.MIB),
        ),
    )

    def _cost(
        cpu_s: int,
        mem_mib_s: int,
        ar_mib: int,
        log_kib: int,
        secrets: int,
        egress_kib: int,
    ) -> dict[str, Quantity]:
        return {
            "cloud_run_cpu": Quantity(cpu_s, Unit.VCPU_SEC),
            "cloud_run_mem": Quantity(mem_mib_s, Unit.MIB_SEC),
            "artifact_registry_storage": Quantity(ar_mib, Unit.MIB),
            "cloud_logging_ingestion": Quantity(log_kib, Unit.KIB),
            "secret_versions": Quantity(secrets, Unit.COUNT),
            "supabase_uncached_egress": Quantity(egress_kib, Unit.KIB),
        }

    action_max_costs: dict[ActionKind, Mapping[str, Quantity | None] | None] = {
        ActionKind.JOB_CREATE: _cost(2, 512, 0, 64, 0, 128),
        ActionKind.EXTERNAL_CALL: _cost(2, 512, 0, 64, 0, 128),
        ActionKind.WAKE: _cost(4, 1_024, 0, 64, 0, 128),
        ActionKind.RETRY: _cost(4, 1_024, 0, 64, 0, 128),
        ActionKind.RECOVER: _cost(5, 1_024, 0, 128, 0, 256),
        ActionKind.PROBE: _cost(1, 256, 0, 32, 0, 32),
        # ActionKind.UNESTIMATED_OP intentionally omitted -> COST_UNKNOWN
    }

    local_zero = {
        "cloud_run_cpu": Quantity(0, Unit.VCPU_SEC),
        "cloud_run_mem": Quantity(0, Unit.MIB_SEC),
        "artifact_registry_storage": Quantity(0, Unit.MIB),
        "cloud_logging_ingestion": Quantity(0, Unit.MIB),
        "secret_versions": Quantity(0, Unit.COUNT),
        "supabase_uncached_egress": Quantity(0, Unit.MIB),
    }
    cloud_admin_base = {
        **local_zero,
        "cloud_logging_ingestion": Quantity(1, Unit.MIB),
        "supabase_uncached_egress": Quantity(1, Unit.MIB),
    }
    teardown_step_costs: dict[TeardownStep, Mapping[str, Quantity]] = {
        TeardownStep.STOP_CALLER: dict(local_zero),
        TeardownStep.DELETE_CLOUD_RUN_SERVICES: {
            **cloud_admin_base,
            "cloud_run_cpu": Quantity(1, Unit.VCPU_SEC),
        },
        TeardownStep.DELETE_ARTIFACT_IMAGES: dict(cloud_admin_base),
        TeardownStep.DESTROY_SECRETS_AND_SA: dict(cloud_admin_base),
        TeardownStep.DROP_SPIKE_SCHEMA: {
            **cloud_admin_base,
            "supabase_uncached_egress": Quantity(2, Unit.MIB),
        },
    }

    return SpendGuardPolicy(
        resources=resources,
        action_max_costs=action_max_costs,
        teardown_step_costs=teardown_step_costs,
        required_teardown_steps=frozenset(TeardownStep),
        max_staleness_s=max_staleness_s,
        max_total_requests=max_total_requests,
        max_elapsed_s=max_elapsed_s,
    )
