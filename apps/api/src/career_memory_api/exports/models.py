"""Typed domain models, wire schemas, and Plan C guards for M4 Wave 3A Exports (T-407, EXPORT-02)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from career_memory_api.auth.redaction import redact_text
from career_memory_api.contracts_api.models import (
    assert_no_plan_c_raw_text_keys,
    assert_no_prohibited_project_id,
    assert_no_readonly_verification_write,
)
from career_memory_api.schema_contracts.enums import (
    ExportFormat,
    ExportPolicy,
    ExportStatus,
)

EXPORTS_NO_STORE_HEADER = "private, no-store"
EXPORT_POLICY_VERSION = "cm-export-policy-v1"


class ExportError(Exception):
    """Structured domain & HTTP error raised by M4 Exports service and API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_message = redact_text(message)
        super().__init__(f"{code}: {safe_message}")
        self.status_code = status_code
        self.code = code
        self.message = safe_message
        self.retryable = retryable
        self.details = details


def utc_now() -> datetime:
    return datetime.now(UTC)


def assert_no_forbidden_export_write_keys(payload: Mapping[str, Any]) -> None:
    """Reject OD-008 project_id, direct verification/SOURCE_VERIFIED writes, and Plan C raw keys."""
    assert_no_prohibited_project_id(payload)
    assert_no_readonly_verification_write(payload)
    assert_no_plan_c_raw_text_keys(payload)


class CreateExportRequest(BaseModel):
    """POST /v1/exports request payload."""

    model_config = ConfigDict(extra="forbid")

    format: ExportFormat
    claim_ids: list[str] = Field(..., min_length=1)
    expected_claim_versions: dict[str, int] | None = None
    export_policy: ExportPolicy = ExportPolicy.SOURCE_VERIFIED_ONLY
    source_resource_id: str | None = None

    @field_validator("claim_ids")
    @classmethod
    def _validate_claim_ids(cls, value: list[str]) -> list[str]:
        cleaned = [str(cid).strip() for cid in value if str(cid).strip()]
        if not cleaned:
            raise ValueError("claim_ids must contain at least one claim_id.")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("claim_ids must not contain duplicates.")
        return cleaned

    @field_validator("expected_claim_versions")
    @classmethod
    def _validate_versions(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        for cid, ver in value.items():
            if ver < 1:
                raise ValueError(f"Expected version for claim {cid!r} must be >= 1.")
        return value


class ExportSummaryItem(BaseModel):
    """Summary projection returned by GET /v1/exports."""

    model_config = ConfigDict(extra="forbid")

    export_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None
    format: ExportFormat
    export_policy: ExportPolicy
    status: ExportStatus
    policy_version: str = Field(..., min_length=1)
    input_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    claim_count: int = Field(..., ge=0)
    invalidation_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    invalidated_at: datetime | None = None
    deleted_at: datetime | None = None


class ExportListResponse(BaseModel):
    """GET /v1/exports response payload."""

    model_config = ConfigDict(extra="forbid")

    items: list[ExportSummaryItem]


class ExportDetailResponse(BaseModel):
    """POST /v1/exports and GET /v1/exports/{export_id} response payload (Plan C: zero quotes)."""

    model_config = ConfigDict(extra="forbid")

    export_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None
    format: ExportFormat
    export_policy: ExportPolicy
    status: ExportStatus
    policy_version: str = Field(..., min_length=1)
    input_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    manifest: dict[str, Any] = Field(default_factory=dict)
    rendered_markdown: str | None = None
    rendered_json: dict[str, Any] | None = None
    invalidation_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    invalidated_at: datetime | None = None
    deleted_at: datetime | None = None


class DeleteExportResponse(BaseModel):
    """DELETE /v1/exports/{export_id} response payload."""

    model_config = ConfigDict(extra="forbid")

    export_id: str = Field(..., min_length=1)
    status: Literal["DELETED"] = "DELETED"
    deleted_at: datetime


__all__ = [
    "EXPORTS_NO_STORE_HEADER",
    "EXPORT_POLICY_VERSION",
    "CreateExportRequest",
    "DeleteExportResponse",
    "ExportDetailResponse",
    "ExportError",
    "ExportListResponse",
    "ExportSummaryItem",
    "assert_no_forbidden_export_write_keys",
    "utc_now",
]
