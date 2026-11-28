"""Deterministic provider/operation circuit breaker & ambiguous timeout guard (T-503 / INF-03)."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from career_memory_api.budgets.models import CircuitBreakerState


class DeliveryAmbiguityOutcome(StrEnum):
    """Classification of external/provider/billable call outcomes (INF-03)."""

    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILURE_RETRYABLE = "CONFIRMED_FAILURE_RETRYABLE"
    CONFIRMED_FAILURE_NON_RETRYABLE = "CONFIRMED_FAILURE_NON_RETRYABLE"
    AMBIGUOUS_DELIVERY_NO_AUTO_RETRY = "AMBIGUOUS_DELIVERY_NO_AUTO_RETRY"


@dataclass(frozen=True, slots=True)
class DeliveryOutcomeDecision:
    """Explicit retry & circuit-breaker policy decision for an external call outcome."""

    outcome: DeliveryAmbiguityOutcome
    auto_retry_allowed: bool
    counts_as_circuit_failure: bool
    reason_code: str
    message: str


def classify_external_call_outcome(
    *,
    timed_out: bool = False,
    connection_reset_after_send: bool = False,
    unknown_delivery_state: bool = False,
    is_billable_or_mutating: bool = False,
    http_status: int | None = None,
    idempotent_retry_safe: bool = False,
) -> DeliveryOutcomeDecision:
    """Classify an external provider or billable call outcome under INF-03 no-blind-retry rules.

    If a call is potentially billable or externally side-effecting (``is_billable_or_mutating=True``)
    and its delivery/completion outcome is ambiguous (timeout, connection reset after request
    transmission, unknown delivery state, or HTTP 408/504) without an idempotent provider-side
    receipt guarantee, the call MUST be classified ``AMBIGUOUS_DELIVERY_NO_AUTO_RETRY`` with
    ``auto_retry_allowed=False`` so the runtime NEVER auto-repeats an uncertain billable call.
    """
    is_ambiguous = (
        timed_out
        or connection_reset_after_send
        or unknown_delivery_state
        or http_status in (408, 504)
    )

    if is_ambiguous and is_billable_or_mutating and not idempotent_retry_safe:
        return DeliveryOutcomeDecision(
            outcome=DeliveryAmbiguityOutcome.AMBIGUOUS_DELIVERY_NO_AUTO_RETRY,
            auto_retry_allowed=False,
            counts_as_circuit_failure=True,
            reason_code="AMBIGUOUS_DELIVERY_NO_AUTO_RETRY",
            message=(
                "Potentially billable or side-effecting call ended in an ambiguous delivery state; "
                "automatic retry is forbidden (INF-03)."
            ),
        )

    if http_status is not None:
        if 200 <= http_status < 300:
            return DeliveryOutcomeDecision(
                outcome=DeliveryAmbiguityOutcome.CONFIRMED_SUCCESS,
                auto_retry_allowed=False,
                counts_as_circuit_failure=False,
                reason_code="DELIVERY_CONFIRMED_SUCCESS",
                message="External call succeeded.",
            )
        if http_status in (429, 500, 502, 503):
            return DeliveryOutcomeDecision(
                outcome=DeliveryAmbiguityOutcome.CONFIRMED_FAILURE_RETRYABLE,
                auto_retry_allowed=True,
                counts_as_circuit_failure=True,
                reason_code=f"RETRYABLE_HTTP_{http_status}",
                message=f"Confirmed retryable upstream failure (HTTP {http_status}).",
            )
        if 400 <= http_status < 500:
            return DeliveryOutcomeDecision(
                outcome=DeliveryAmbiguityOutcome.CONFIRMED_FAILURE_NON_RETRYABLE,
                auto_retry_allowed=False,
                counts_as_circuit_failure=False,
                reason_code=f"NON_RETRYABLE_HTTP_{http_status}",
                message=f"Confirmed non-retryable client/policy error (HTTP {http_status}).",
            )

    if is_ambiguous:
        return DeliveryOutcomeDecision(
            outcome=DeliveryAmbiguityOutcome.CONFIRMED_FAILURE_RETRYABLE,
            auto_retry_allowed=True,
            counts_as_circuit_failure=True,
            reason_code="IDEMPOTENT_READ_TIMEOUT_RETRYABLE",
            message="Idempotent non-billable read timed out and may be retried within budget.",
        )

    return DeliveryOutcomeDecision(
        outcome=DeliveryAmbiguityOutcome.CONFIRMED_SUCCESS,
        auto_retry_allowed=False,
        counts_as_circuit_failure=False,
        reason_code="DELIVERY_CONFIRMED_SUCCESS",
        message="External call succeeded.",
    )


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Configuration for deterministic provider/operation circuit breaker."""

    failure_threshold: int = 3
    recovery_timeout_seconds: float = 60.0
    half_open_max_probes: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1.")
        if self.recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be > 0.")
        if self.half_open_max_probes < 1:
            raise ValueError("half_open_max_probes must be >= 1.")


@dataclass(frozen=True, slots=True)
class CircuitBreakerSnapshot:
    """Immutable snapshot of current circuit breaker state."""

    state: CircuitBreakerState
    consecutive_failures: int
    retry_after_seconds: int | None
    last_failure_reason: str | None
    half_open_in_flight: int


class ProviderCircuitBreaker:
    """Thread-safe injectable-clock ``CLOSED -> OPEN -> HALF_OPEN -> CLOSED`` circuit breaker.

    Protects upstream provider, refetch, and DB paths against cascading retry storms (`T-503`).
    """

    def __init__(
        self,
        *,
        config: CircuitBreakerConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self._state = CircuitBreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = 0
        self._last_failure_reason: str | None = None

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    def _maybe_advance_from_open(self, now: float) -> None:
        if (
            self._state is CircuitBreakerState.OPEN
            and self._opened_at is not None
            and (now - self._opened_at) >= self._config.recovery_timeout_seconds
        ):
            self._state = CircuitBreakerState.HALF_OPEN
            self._half_open_in_flight = 0

    def snapshot(self) -> CircuitBreakerSnapshot:
        """Return current circuit breaker state, advancing OPEN -> HALF_OPEN if timeout elapsed."""
        with self._lock:
            now = float(self._clock())
            self._maybe_advance_from_open(now)
            retry_after: int | None = None
            if self._state is CircuitBreakerState.OPEN and self._opened_at is not None:
                remaining = (self._opened_at + self._config.recovery_timeout_seconds) - now
                retry_after = max(1, math.ceil(remaining))
            return CircuitBreakerSnapshot(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                retry_after_seconds=retry_after,
                last_failure_reason=self._last_failure_reason,
                half_open_in_flight=self._half_open_in_flight,
            )

    @property
    def state(self) -> CircuitBreakerState:
        return self.snapshot().state

    def allow_request(self) -> tuple[bool, int | None]:
        """Return ``(True, None)`` if a request/probe may proceed, else ``(False, retry_after_s)``."""
        with self._lock:
            now = float(self._clock())
            self._maybe_advance_from_open(now)

            if self._state is CircuitBreakerState.CLOSED:
                return True, None

            if self._state is CircuitBreakerState.OPEN:
                assert self._opened_at is not None
                remaining = (self._opened_at + self._config.recovery_timeout_seconds) - now
                return False, max(1, math.ceil(remaining))

            # HALF_OPEN: admit only bounded probe(s)
            if self._half_open_in_flight < self._config.half_open_max_probes:
                self._half_open_in_flight += 1
                return True, None
            return False, max(1, math.ceil(self._config.recovery_timeout_seconds))

    def record_success(self) -> CircuitBreakerSnapshot:
        """Record a successful call, closing a HALF_OPEN probe and clearing consecutive failures."""
        with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_in_flight = 0
            self._last_failure_reason = None
            return self.snapshot()

    def record_failure(self, *, reason_code: str = "UPSTREAM_FAILURE") -> CircuitBreakerSnapshot:
        """Record a retryable/ambiguous upstream failure; trip OPEN at threshold or on HALF_OPEN fail."""
        with self._lock:
            now = float(self._clock())
            self._maybe_advance_from_open(now)
            self._last_failure_reason = reason_code
            if self._state is CircuitBreakerState.HALF_OPEN:
                self._half_open_in_flight = 0
                self._state = CircuitBreakerState.OPEN
                self._opened_at = now
                self._consecutive_failures = max(
                    self._consecutive_failures + 1, self._config.failure_threshold
                )
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._config.failure_threshold:
                    self._state = CircuitBreakerState.OPEN
                    self._opened_at = now
            return self.snapshot()

    def record_outcome(self, decision: DeliveryOutcomeDecision) -> CircuitBreakerSnapshot:
        """Update circuit state according to a classified ``DeliveryOutcomeDecision``."""
        if decision.outcome is DeliveryAmbiguityOutcome.CONFIRMED_SUCCESS:
            return self.record_success()
        if decision.counts_as_circuit_failure:
            return self.record_failure(reason_code=decision.reason_code)
        # Confirmed non-retryable client error does not open the upstream circuit.
        with self._lock:
            if self._state is CircuitBreakerState.HALF_OPEN and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            return self.snapshot()

    def reset(self) -> CircuitBreakerSnapshot:
        """Explicitly reset the circuit breaker to CLOSED (used on verified operator resume)."""
        return self.record_success()


__all__ = [
    "CircuitBreakerConfig",
    "CircuitBreakerSnapshot",
    "DeliveryAmbiguityOutcome",
    "DeliveryOutcomeDecision",
    "ProviderCircuitBreaker",
    "classify_external_call_outcome",
]
