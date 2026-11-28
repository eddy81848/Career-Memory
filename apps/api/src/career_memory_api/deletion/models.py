"""Models, error types, and ephemeral cache registry for M5 Source Deletion Engine (T-502 / T-502A)."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from career_memory_api.jobs.models import DeletionJobStatus
from career_memory_api.schema_contracts.enums import (
    DeletionComponent,
    DeletionComponentStatus,
)

DELETION_COMPONENT_ORDER: tuple[DeletionComponent, ...] = (
    DeletionComponent.JOBS_AND_OUTBOX,
    DeletionComponent.EXPORTS,
    DeletionComponent.FTS_INDEX,
    DeletionComponent.EPHEMERAL_CACHE,
    DeletionComponent.AUTHORITATIVE_DOMAIN,
)


class DeletionError(Exception):
    """Structured HTTP error raised by the source deletion service/router."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


class DeletionComponentReceiptItem(BaseModel):
    """Per-component receipt row for a source deletion job (DELETE-05 / DELETE-06)."""

    model_config = ConfigDict(extra="forbid")

    component: DeletionComponent
    status: DeletionComponentStatus
    attempt: int = Field(..., ge=0)
    purged_count: int = Field(..., ge=0)
    last_error_code: str | None = None
    receipt_metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime
    completed_at: datetime | None = None


class DeletionJobStatusResponse(BaseModel):
    """GET /v1/deletion-jobs/{deletion_job_id} response contract (T-502 / DELETE-05..06)."""

    model_config = ConfigDict(extra="forbid")

    deletion_job_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str = Field(..., min_length=1)
    status: DeletionJobStatus
    user_visible_state: Literal["DELETION_PENDING", "DELETED", "DELETION_FAILED"]
    fence_active: bool
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    failure_summary: dict[str, Any] = Field(default_factory=dict)
    components: list[DeletionComponentReceiptItem]
    requested_by: str | None = None
    requested_at: datetime
    completed_at: datetime | None = None


def project_user_visible_deletion_state(
    status: DeletionJobStatus,
) -> Literal["DELETION_PENDING", "DELETED", "DELETION_FAILED"]:
    """Map internal deletion job status to user-visible deletion state (DELETE-05)."""
    if status is DeletionJobStatus.SUCCEEDED:
        return "DELETED"
    if status is DeletionJobStatus.FAILED:
        return "DELETION_FAILED"
    return "DELETION_PENDING"


class EphemeralCacheRegistry:
    """Thread-safe in-process ephemeral cache registry keyed by ``(workspace_id, source_resource_id)``.

    Used to track short-lived in-memory entries (e.g., transient derived projections or
    operation-scoped resolver buffers) so ``deletion-preview`` reports exact counts and
    ``deletion-confirm`` / ``EPHEMERAL_CACHE`` cleanup evicts them deterministically with zero leaks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: dict[tuple[str, str], dict[str, Any]] = {}

    def put(
        self,
        workspace_id: str,
        source_resource_id: str,
        entry_key: str,
        value: Any,
    ) -> None:
        with self._lock:
            bucket = self._store.setdefault((workspace_id, source_resource_id), {})
            bucket[entry_key] = value

    def get(
        self,
        workspace_id: str,
        source_resource_id: str,
        entry_key: str,
    ) -> Any | None:
        with self._lock:
            bucket = self._store.get((workspace_id, source_resource_id))
            if bucket is None:
                return None
            return bucket.get(entry_key)

    def count_for_resource(self, workspace_id: str, source_resource_id: str) -> int:
        with self._lock:
            bucket = self._store.get((workspace_id, source_resource_id))
            return len(bucket) if bucket is not None else 0

    def purge_for_resource(self, workspace_id: str, source_resource_id: str) -> int:
        with self._lock:
            bucket = self._store.pop((workspace_id, source_resource_id), None)
            return len(bucket) if bucket is not None else 0

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


__all__ = [
    "DELETION_COMPONENT_ORDER",
    "DeletionComponentReceiptItem",
    "DeletionError",
    "DeletionJobStatusResponse",
    "EphemeralCacheRegistry",
    "project_user_visible_deletion_state",
]
