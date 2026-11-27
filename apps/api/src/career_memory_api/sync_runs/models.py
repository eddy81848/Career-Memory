"""Domain models, request parsers, canonical fingerprints, and API error types for SyncRuns."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

READ_NO_STORE_CACHE_CONTROL = "private, no-store"

CONTRACT_ERROR_MESSAGES: dict[str, str] = {
    "SESSION_TERMINATED": "The session has ended.",
    "WORKSPACE_SCOPE_VIOLATION": "Access to the selected workspace has been revoked.",
    "RESOURCE_NOT_FOUND": "The requested resource was not found.",
    "AUTH_VERIFICATION_UNAVAILABLE": "Authentication could not be verified temporarily.",
    "SYNC_ALREADY_RUNNING": "An import is already active for the repository.",
    "RESOURCE_VERSION_CONFLICT": "The expected run version does not match the current run.",
    "IDEMPOTENCY_CONFLICT": "The request key identifies a different request.",
    "IDEMPOTENCY_KEY_EXPIRED": "The request key has expired.",
}


class SyncRunMode(StrEnum):
    """Supported SyncRun execution modes (MVP initial import only)."""

    INITIAL_BACKFILL = "INITIAL_BACKFILL"


class SyncRunStatus(StrEnum):
    """Canonical SyncRun import states matching CONTRACT@87f1515 and migration 0040."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    PARTIAL_WITH_LIMIT = "PARTIAL_WITH_LIMIT"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


ACTIVE_SYNC_RUN_STATUSES: frozenset[SyncRunStatus] = frozenset(
    {SyncRunStatus.PENDING, SyncRunStatus.RUNNING}
)


class ProcessingStatus(StrEnum):
    """Derived downstream processing states matching CONTRACT@87f1515."""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    BLOCKED_SOURCE = "BLOCKED_SOURCE"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class InitialImportRequest:
    """Validated POST /v1/sync-runs request payload."""

    source_resource_id: str
    mode: SyncRunMode
    backfill_from: str | None = None
    limits: dict[str, int] | None = None

    def to_canonical_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_resource_id": self.source_resource_id,
            "mode": self.mode.value,
        }
        if self.backfill_from is not None:
            payload["backfill_from"] = self.backfill_from
        if self.limits is not None:
            payload["limits"] = dict(self.limits)
        return payload


@dataclass(frozen=True, slots=True)
class RetryImportRequest:
    """Validated POST /v1/sync-runs/{id}/retry request payload."""

    expected_run_version: int


@dataclass(frozen=True, slots=True)
class StreamProgress:
    """Per-stream checkpoint summary serialized in ImportStatus.streams."""

    type: str
    status: str
    last_committed_page: int | None = None
    unresolved_failures: int | None = None

    def to_contract_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.type,
            "status": self.status,
        }
        if self.last_committed_page is not None:
            result["last_committed_page"] = self.last_committed_page
        if self.unresolved_failures is not None:
            result["unresolved_failures"] = self.unresolved_failures
        return result


@dataclass(frozen=True, slots=True)
class SyncRunObservation:
    """Worker observation applied to a SyncRun (used by M2 workers and integration tests)."""

    status: SyncRunStatus
    failure_reason: str | None = None
    streams: tuple[StreamProgress, ...] | None = None
    import_ready: bool | None = None
    processing_status: ProcessingStatus = ProcessingStatus.NOT_STARTED
    observed_at: datetime | None = None
    source_drift: bool = False
    processed_range: str | None = None
    remaining_range: str | None = None
    retry_allowed: bool = False
    has_safe_checkpoint: bool = False


@dataclass(frozen=True, slots=True)
class AcceptedSyncRunReceipt:
    """202 Accepted receipt payload persisted in api_idempotency_records."""

    target_id: str
    status_url: str
    job_id: str | None = None
    status: str = "PENDING"
    target_type: str = "SYNC_RUN"

    def to_contract_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "status_url": self.status_url,
        }
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptedSyncRunReceipt:
        return cls(
            target_id=str(data["target_id"]),
            status_url=str(data["status_url"]),
            job_id=str(data["job_id"]) if data.get("job_id") is not None else None,
            status=str(data.get("status", "PENDING")),
            target_type=str(data.get("target_type", "SYNC_RUN")),
        )


@dataclass(frozen=True, slots=True)
class SyncRunStatusSnapshot:
    """Contract-aligned ImportStatus view for GET /v1/sync-runs/{id} and recovery items."""

    id: str
    source_resource_id: str
    plan_id: str
    run_version: int
    status: SyncRunStatus
    import_ready: bool
    processing_status: ProcessingStatus
    source_drift: bool
    retry_allowed: bool
    failure_reason: str | None = None
    streams: tuple[StreamProgress, ...] = ()
    observed_at: datetime | None = None
    processed_range: str | None = None
    remaining_range: str | None = None

    def to_contract_dict(self) -> dict[str, Any]:
        retry: dict[str, Any] = {"allowed": self.retry_allowed}
        if self.retry_allowed:
            retry["from_safe_checkpoint"] = True

        payload: dict[str, Any] = {
            "status": self.status.value,
            "plan_id": self.plan_id,
            "run_version": self.run_version,
            "import_ready": self.import_ready,
            "processing_status": self.processing_status.value,
            "source_drift": self.source_drift,
            "retry": retry,
        }
        if self.failure_reason is not None:
            payload["failure_reason"] = self.failure_reason
        if self.streams:
            payload["streams"] = [s.to_contract_dict() for s in self.streams]
        if self.observed_at is not None:
            payload["observed_at"] = (
                self.observed_at.astimezone(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        if self.processed_range is not None:
            payload["processed_range"] = self.processed_range
        if self.remaining_range is not None:
            payload["remaining_range"] = self.remaining_range
        return payload


class SyncRunApiError(Exception):
    """Contract-aligned HTTP rejection for the 4 initial-import routes."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str | None = None,
        existing_run_id: str | None = None,
    ) -> None:
        super().__init__(code or str(status_code))
        self.status_code = status_code
        self.code = code
        self.existing_run_id = existing_run_id


def parse_idempotency_key(raw: str | None) -> str:
    """Validate and normalize a required UUID Idempotency-Key header."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Idempotency-Key header is required.")
    candidate = raw.strip()
    try:
        parsed = uuid.UUID(candidate)
    except ValueError as exc:
        raise ValueError("Idempotency-Key must be a valid UUID.") from exc
    if str(parsed) != candidate.lower():
        raise ValueError("Idempotency-Key must use canonical UUID format.")
    return str(parsed)


def parse_initial_import_request(raw: Any) -> InitialImportRequest:
    """Validate POST /v1/sync-runs JSON body against CONTRACT@87f1515."""
    if not isinstance(raw, dict):
        raise ValueError("Request body must be a JSON object.")

    source_resource_id = raw.get("source_resource_id")
    if not isinstance(source_resource_id, str) or not source_resource_id.strip():
        raise ValueError("source_resource_id must be a non-empty string.")

    mode_raw = raw.get("mode")
    if mode_raw != SyncRunMode.INITIAL_BACKFILL.value:
        raise ValueError("mode must be INITIAL_BACKFILL.")

    backfill_from: str | None = None
    if "backfill_from" in raw:
        bf = raw["backfill_from"]
        if not isinstance(bf, str) or len(bf) != 10:
            raise ValueError("backfill_from must be a YYYY-MM-DD date string.")
        try:
            parsed_date = date.fromisoformat(bf)
        except ValueError as exc:
            raise ValueError("backfill_from is not a valid calendar date.") from exc
        if parsed_date.isoformat() != bf:
            raise ValueError("backfill_from must be ISO YYYY-MM-DD.")
        backfill_from = bf

    limits: dict[str, int] | None = None
    if "limits" in raw:
        lim_raw = raw["limits"]
        if not isinstance(lim_raw, dict):
            raise ValueError("limits must be a JSON object.")
        parsed_limits: dict[str, int] = {}
        if "max_objects" in lim_raw:
            max_obj = lim_raw["max_objects"]
            if isinstance(max_obj, bool) or not isinstance(max_obj, int):
                raise ValueError("limits.max_objects must be an integer.")
            parsed_limits["max_objects"] = max_obj
        limits = parsed_limits

    return InitialImportRequest(
        source_resource_id=source_resource_id.strip(),
        mode=SyncRunMode.INITIAL_BACKFILL,
        backfill_from=backfill_from,
        limits=limits,
    )


def parse_retry_import_request(raw: Any) -> RetryImportRequest:
    """Validate POST /v1/sync-runs/{id}/retry JSON body (additionalProperties: false)."""
    if not isinstance(raw, dict):
        raise ValueError("Request body must be a JSON object.")
    if set(raw.keys()) != {"expected_run_version"}:
        raise ValueError("Retry request must contain strictly expected_run_version.")
    version = raw["expected_run_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("expected_run_version must be an integer >= 1.")
    return RetryImportRequest(expected_run_version=version)


def compute_create_request_fingerprint(req: InitialImportRequest) -> str:
    """Compute a deterministic SHA-256 hex digest over the parsed initial import request."""
    canonical = json.dumps(
        req.to_canonical_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_retry_request_fingerprint(run_id: str, expected_run_version: int) -> str:
    """Compute a deterministic SHA-256 hex digest over (run_id, expected_run_version)."""
    canonical = json.dumps(
        {"expected_run_version": expected_run_version, "run_id": run_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
