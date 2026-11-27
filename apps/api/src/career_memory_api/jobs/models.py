"""Domain models, state enums, backoff policy, and ports for durable Jobs/Outbox/Audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class JobStatus(StrEnum):
    """Durable DB Job state machine matching migration 0040 (T-105A / REL-02)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELED = "CANCELED"


class JobErrorClass(StrEnum):
    """Canonical error classification for retryable, poison, and fenced job outcomes."""

    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    POISON_BUDGET_EXHAUSTED = "POISON_BUDGET_EXHAUSTED"
    CANCELED_BY_FENCE = "CANCELED_BY_FENCE"


class OutboxStatus(StrEnum):
    """Durable outbox delivery states matching migration 0040."""

    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED_TERMINAL = "FAILED_TERMINAL"


class AuditActorType(StrEnum):
    """Canonical audit actor types matching Data Model §3.10."""

    USER = "USER"
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"
    OPERATOR = "OPERATOR"


class DeletionJobStatus(StrEnum):
    """Canonical deletion fence job states matching Data Model §3.10."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Deterministic exponential backoff policy for RETRY_WAIT and outbox retries."""

    base_seconds: int = 5
    multiplier: int = 2
    max_seconds: int = 300

    def __post_init__(self) -> None:
        if self.base_seconds < 1:
            raise ValueError("base_seconds must be >= 1")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds must be >= base_seconds")

    def delay_for_attempt(self, attempt: int) -> timedelta:
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        seconds = min(
            self.max_seconds,
            self.base_seconds * (self.multiplier ** (attempt - 1)),
        )
        return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class WakeSignal:
    """Post-commit wake notification payload dispatched only after DB commit."""

    workspace_id: str
    job_id: str
    job_type: str
    target_sync_run_id: str | None = None
    outbox_event_id: str | None = None


class PostCommitWakeNotifierPort(Protocol):
    """Port for waking a Worker strictly AFTER the PostgreSQL transaction commits.

    Implementations MUST tolerate invocation failures without rolling back
    or losing the already-committed PENDING job/outbox rows.
    """

    def notify_job_available(self, signal: WakeSignal) -> None:
        ...


class NoopWakeNotifier:
    """Default no-op notifier used when wake delivery is disabled or deferred."""

    def notify_job_available(self, signal: WakeSignal) -> None:
        return None


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Persistent row representation of ``public.jobs``."""

    id: str
    workspace_id: str
    job_type: str
    target_sync_run_id: str | None
    dedupe_key: str | None
    input_hash: str
    input_manifest: dict[str, Any]
    status: JobStatus
    attempt: int
    max_attempts: int
    lease_owner: str | None
    lease_token: str | None
    lease_until: datetime | None
    next_run_at: datetime
    checkpoint: dict[str, Any]
    error_class: JobErrorClass | None
    error_code: str | None
    execution_parent_id: str | None
    logical_effect_key: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Leased job handle carrying the fencing token required for all worker mutations."""

    job: JobRecord
    lease_token: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class OutboxEventRecord:
    """Persistent row representation of ``public.outbox_events``."""

    id: str
    workspace_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    dedupe_key: str
    payload: dict[str, Any]
    status: OutboxStatus
    attempt: int
    max_attempts: int
    next_attempt_at: datetime
    last_error_code: str | None
    created_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class DeletionJobRecord:
    """Persistent row representation of ``public.deletion_jobs``."""

    id: str
    workspace_id: str
    source_resource_id: str
    status: DeletionJobStatus
    checkpoint: dict[str, Any]
    failure_summary: dict[str, Any]
    requested_by: str | None
    requested_at: datetime
    completed_at: datetime | None


class OutboxPublisherPort(Protocol):
    """Port for delivering durable outbox events to a downstream bus or trigger."""

    def publish(self, event: OutboxEventRecord) -> None:
        ...


class JobRuntimeError(Exception):
    """Base error for durable Job/Outbox/Fence runtime violations."""


class StaleJobLeaseError(JobRuntimeError):
    """Raised when a worker attempts to mutate a job with an expired or replaced lease token."""


class DeletionFenceActiveError(JobRuntimeError):
    """Raised when an active deletion fence cancels or blocks a job."""


class JobNotReplayableError(JobRuntimeError):
    """Raised when a job cannot be replayed (not FAILED_TERMINAL or already active/succeeded)."""
