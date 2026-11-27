"""Typed domain models separating Option C non-raw metadata from ephemeral memory handles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import (
    FORBIDDEN_PERSISTED_RAW_TEXT_KEYS,
    ContentUnitType,
    EphemeralResolvedUnit,
    EphemeralVerifiedSpan,
    SpanLocator,
)

_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")

# Union of 0020 DB cm_jsonb_contains_forbidden_raw_key and T-103A forbidden raw-text keys.
FORBIDDEN_RAW_PERSISTENCE_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "raw_body",
        "raw_text",
        "text_content",
        "diff",
        "patch",
        "comment_body",
        "message_body",
        "quote",
        "extracted_span",
        "prompt",
        "raw_payload",
        "raw_content",
        "full_text",
        "source_text",
        "raw_diff",
        "commit_message_body",
        "prompt_text",
        "system_prompt",
        "user_prompt",
        "completion",
        "raw_response",
        "content_text",
        "excerpt",
        "snippet",
        "verbatim_quote",
        "pr_body",
        "issue_body",
        "review_body",
        "patch_text",
        "diff_text",
        "hunk_text",
        "untrusted_text",
        "body_text",
    }
    | set(FORBIDDEN_PERSISTED_RAW_TEXT_KEYS)
)


def normalize_sha256_hash(value: str, *, field_name: str = "content_hash") -> str:
    """Normalize a 64-hex or 'sha256:<64-hex>' hash into canonical 'sha256:<64-hex>' form."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 string.")
    match = _SHA256_RE.match(value.strip().lower())
    if not match:
        raise ValueError(f"{field_name} must be a valid lowercase SHA-256 digest.")
    return f"sha256:{match.group(1)}"


def format_utc_timestamp(value: datetime) -> str:
    """Format a timezone-aware datetime as canonical ISO-8601 UTC ending with 'Z'."""
    dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return dt.isoformat().replace("+00:00", "Z")


def contains_forbidden_raw_key(payload: Any) -> bool:
    """Return True if any nested dict key matches the Option C forbidden raw-text key set."""
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, val in current.items():
                if str(key).strip().lower() in FORBIDDEN_RAW_PERSISTENCE_KEYS:
                    return True
                stack.append(val)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return False


def assert_no_forbidden_raw_keys(payload: Any, *, context: str) -> None:
    """Fail closed if a persisted metadata/manifest payload contains a forbidden raw key."""
    if contains_forbidden_raw_key(payload):
        raise ValueError(
            f"Option C (REF-04) violation: forbidden raw-text key detected in {context}."
        )


class ResolutionStatus(StrEnum):
    """Internal 6-state refetch resolution status (distinct from Claim/SyncRun domain enums)."""

    MATCHED = "MATCHED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    INVALID_LOCATOR = "INVALID_LOCATOR"
    PARTIAL_WITH_LIMIT = "PARTIAL_WITH_LIMIT"


class ProviderFetchOutcomeKind(StrEnum):
    """Deterministic classification returned by ProviderContentFetcherPort implementations."""

    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    PARTIAL_CONTENT = "PARTIAL_CONTENT"
    POLICY_DENIED = "POLICY_DENIED"
    REDIRECT = "REDIRECT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class GateOperationKind(StrEnum):
    """Supported downstream gate operations requiring live refetch verification."""

    APPROVAL = "APPROVAL"
    CURRENT_SOURCE_VERIFIED = "CURRENT_SOURCE_VERIFIED"
    EXPORT_DOWNLOAD = "EXPORT_DOWNLOAD"
    EVIDENCE_VIEW = "EVIDENCE_VIEW"


class DerivedUnitProjectionStatus(StrEnum):
    """Derived unit status projected after IMPORT_READY without mutating import_run SUCCESS."""

    READY = "READY"
    BLOCKED_SOURCE = "BLOCKED_SOURCE"


@dataclass(frozen=True, slots=True)
class ResolutionErrorMapping:
    """Stable HTTP / gate error mapping for non-MATCHED ResolutionStatus values (Option C §7)."""

    http_status: int
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ResolutionMetadata:
    """Persistence-, audit-, log-, and error-safe resolution metadata (strictly ZERO raw text)."""

    status: ResolutionStatus
    checked_at: datetime
    operation_id: str
    source_object_revision_id: str
    content_unit_id: str
    expected_revision_hash: str
    observed_revision_hash: str | None
    expected_unit_hash: str
    observed_unit_hash: str | None
    canonicalization_version: str
    parser_version: str
    retryable: bool
    reason_code: str | None
    requests_used: int
    bytes_read: int
    deduped: bool
    fetch_attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status.value,
            "checked_at": format_utc_timestamp(self.checked_at),
            "operation_id": self.operation_id,
            "source_object_revision_id": self.source_object_revision_id,
            "content_unit_id": self.content_unit_id,
            "expected_revision_hash": self.expected_revision_hash,
            "observed_revision_hash": self.observed_revision_hash,
            "expected_unit_hash": self.expected_unit_hash,
            "observed_unit_hash": self.observed_unit_hash,
            "canonicalization_version": self.canonicalization_version,
            "parser_version": self.parser_version,
            "retryable": self.retryable,
            "reason_code": self.reason_code,
            "requests_used": self.requests_used,
            "bytes_read": self.bytes_read,
            "deduped": self.deduped,
            "fetch_attempted": self.fetch_attempted,
        }
        assert_no_forbidden_raw_keys(payload, context="ResolutionMetadata")
        return payload

    def to_http_source_resolution_dict(self) -> dict[str, Any]:
        """Serialize the safe ``source_resolution`` block for HTTP responses."""
        return {
            "status": self.status.value,
            "checked_at": format_utc_timestamp(self.checked_at),
            "expected_revision_hash": self.expected_revision_hash,
            "observed_revision_hash": self.observed_revision_hash,
            "retryable": self.retryable,
            "reason_code": self.reason_code,
        }


def map_resolution_status_to_error(metadata: ResolutionMetadata) -> ResolutionErrorMapping:
    """Map non-MATCHED ResolutionMetadata to Option C §7 stable HTTP/gate error contracts."""
    if metadata.status is ResolutionStatus.MATCHED:
        raise ValueError("Cannot map ResolutionStatus.MATCHED to an error response.")
    if metadata.status is ResolutionStatus.REVISION_MISMATCH:
        return ResolutionErrorMapping(
            http_status=409,
            code="SOURCE_REVISION_MISMATCH",
            message="The remote source content no longer matches the recorded revision hash.",
            retryable=False,
        )
    if metadata.status is ResolutionStatus.INVALID_LOCATOR:
        return ResolutionErrorMapping(
            http_status=409,
            code="SOURCE_LOCATOR_INVALID",
            message="The recorded unit or span locator could not be reproduced on the source revision.",
            retryable=False,
        )
    if metadata.status is ResolutionStatus.POLICY_BLOCKED:
        return ResolutionErrorMapping(
            http_status=403,
            code="SOURCE_POLICY_BLOCKED",
            message="Source refetch is blocked by workspace, visibility, deletion, or endpoint security policy.",
            retryable=False,
        )
    if metadata.status is ResolutionStatus.UNAVAILABLE:
        return ResolutionErrorMapping(
            http_status=503,
            code="SOURCE_UNAVAILABLE",
            message="The remote source could not be retrieved to verify this content unit.",
            retryable=metadata.retryable,
        )
    if metadata.status is ResolutionStatus.PARTIAL_WITH_LIMIT:
        return ResolutionErrorMapping(
            http_status=422,
            code="SOURCE_FETCH_LIMIT",
            message="Source refetch exceeded operation limits or returned a truncated response.",
            retryable=False,
        )
    raise ValueError(f"Unsupported resolution status: {metadata.status!r}")


@dataclass(frozen=True, slots=True)
class DerivedUnitProjection:
    """Post-IMPORT_READY derived unit projection preserving immutable import_run status."""

    import_run_status: str
    derived_unit_status: DerivedUnitProjectionStatus
    resolution_status: ResolutionStatus
    reason_code: str | None
    retryable: bool


def project_derived_unit_state_after_import_ready(
    resolution: ResolutionMetadata,
    *,
    import_run_status: str = "SUCCESS",
) -> DerivedUnitProjection:
    """Project derived unit status without ever mutating historical import_run SUCCESS."""
    derived = (
        DerivedUnitProjectionStatus.READY
        if resolution.status is ResolutionStatus.MATCHED
        else DerivedUnitProjectionStatus.BLOCKED_SOURCE
    )
    return DerivedUnitProjection(
        import_run_status=import_run_status,
        derived_unit_status=derived,
        resolution_status=resolution.status,
        reason_code=resolution.reason_code,
        retryable=resolution.retryable,
    )


@dataclass(frozen=True, slots=True)
class PersistedUnitResolutionTarget:
    """Non-raw persisted provenance chain for a single content unit inside one Workspace."""

    workspace_id: str
    source_account_id: str
    source_account_provider: str
    source_account_status: str
    source_account_token_secret_ref: str
    source_resource_id: str
    provider_resource_id: str
    resource_type: str
    resource_name: str
    canonical_url: str
    visibility: str
    processing_mode: str
    raw_storage_mode: str
    resource_status: str
    source_object_id: str
    object_type: str
    provider_object_id: str
    object_lifecycle_state: str
    source_object_revision_id: str
    provider_revision_id: str | None
    expected_revision_hash: str
    hash_policy_version: str
    revision_is_tombstone: bool
    revision_normalized_metadata: dict[str, Any]
    content_unit_id: str
    unit_type: ContentUnitType
    anchor: dict[str, Any]
    expected_unit_hash: str
    canonicalization_version: str
    parser_version: str
    unit_ordinal: int
    unit_policy_state: str
    text_content: str | None = None
    content_ref: str | None = None
    retention_class: str = "NONE"
    deletion_fence_active: bool = False
    last_verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefetchRequestInput:
    """Caller request to resolve a persisted Content Unit. Strictly accepts NO raw URL."""

    access: WorkspaceAccessContext
    content_unit_id: str
    operation_id: str
    span_locator: SpanLocator | None = None
    include_context: bool = True
    expected_revision_hash: str | None = None
    expected_unit_hash: str | None = None
    source_resource_id: str | None = None
    source_object_id: str | None = None
    source_object_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderFetchRequest:
    """Server-owned provider fetch instruction built solely from persisted provider IDs."""

    provider: str
    endpoint_url: str
    provider_resource_id: str
    resource_name: str
    object_type: str
    provider_object_id: str
    provider_revision_id: str | None
    token_secret_ref: str
    operation_id: str
    max_bytes: int
    timeout_seconds: float
    source_object_revision_id: str
    expected_revision_hash: str
    file_path: str | None = None
    is_diff: bool = False


@dataclass(frozen=True, slots=True)
class ProviderFetchResponse:
    """Ephemeral provider fetch response whose raw bytes are redacted from repr/str."""

    kind: ProviderFetchOutcomeKind
    http_status: int | None = None
    raw_bytes: bytes | None = field(default=None, repr=False)
    bytes_read: int = 0
    mime_type: str | None = None
    redirect_location: str | None = None
    provider_visibility: str | None = None
    reason_code: str | None = None
    retryable: bool = False
    truncated: bool = False

    def __repr__(self) -> str:
        byte_state = "<REDACTED_EPHEMERAL_BYTES>" if self.raw_bytes is not None else "None"
        return (
            "ProviderFetchResponse("
            f"kind={self.kind.value!r}, "
            f"http_status={self.http_status!r}, "
            f"bytes_read={self.bytes_read}, "
            f"retryable={self.retryable}, "
            f"truncated={self.truncated}, "
            f"raw_bytes={byte_state})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class EphemeralUnitContext:
    """Ephemeral adjacent section/hunk context for in-memory review/display only."""

    section: str | None = None
    file_path: str | None = None
    previous_file_path: str | None = None
    hunk_header: str | None = None
    _preceding_unit_text: str | None = field(default=None, repr=False)
    _following_unit_text: str | None = field(default=None, repr=False)

    @property
    def preceding_text(self) -> str | None:
        return self._preceding_unit_text

    @property
    def following_text(self) -> str | None:
        return self._following_unit_text

    def to_ephemeral_http_dict(self) -> dict[str, Any]:
        """Serialize strictly for ephemeral 200 HTTP responses with private, no-store."""
        return {
            "section": self.section,
            "file_path": self.file_path,
            "previous_file_path": self.previous_file_path,
            "hunk_header": self.hunk_header,
            "preceding_text": self._preceding_unit_text,
            "following_text": self._following_unit_text,
        }

    def __getstate__(self) -> Any:
        raise TypeError("EphemeralUnitContext is strictly ephemeral and cannot be serialized.")

    def __repr__(self) -> str:
        return (
            "EphemeralUnitContext("
            f"section={self.section!r}, "
            f"file_path={self.file_path!r}, "
            "preceding_text='<REDACTED_EPHEMERAL_TEXT>', "
            "following_text='<REDACTED_EPHEMERAL_TEXT>')"
        )

    def __str__(self) -> str:
        return repr(self)


class OperationMemoryResolution:
    """Operation-scoped ephemeral resolution handle with explicit reference clearing.

    Safe ``metadata`` remains accessible after ``close()``, whereas all raw-text
    references (``ephemeral_unit``, ``ephemeral_span``, ``ephemeral_context``)
    are cleared immediately on ``close()`` or context-manager exit (including
    exceptions and cancellation).
    """

    __slots__ = (
        "_metadata",
        "_ephemeral_unit",
        "_ephemeral_span",
        "_ephemeral_context",
        "_closed",
    )

    def __init__(
        self,
        *,
        metadata: ResolutionMetadata,
        ephemeral_unit: EphemeralResolvedUnit | None = None,
        ephemeral_span: EphemeralVerifiedSpan | None = None,
        ephemeral_context: EphemeralUnitContext | None = None,
    ) -> None:
        self._metadata = metadata
        self._ephemeral_unit = ephemeral_unit
        self._ephemeral_span = ephemeral_span
        self._ephemeral_context = ephemeral_context
        self._closed = False

    @property
    def metadata(self) -> ResolutionMetadata:
        return self._metadata

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ephemeral_unit(self) -> EphemeralResolvedUnit | None:
        if self._closed:
            return None
        return self._ephemeral_unit

    @property
    def ephemeral_span(self) -> EphemeralVerifiedSpan | None:
        if self._closed:
            return None
        return self._ephemeral_span

    @property
    def ephemeral_context(self) -> EphemeralUnitContext | None:
        if self._closed:
            return None
        return self._ephemeral_context

    def close(self) -> None:
        """Immediately release all references to ephemeral raw source text."""
        self._ephemeral_unit = None
        self._ephemeral_span = None
        self._ephemeral_context = None
        self._closed = True

    def __enter__(self) -> OperationMemoryResolution:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def __getstate__(self) -> Any:
        raise TypeError("OperationMemoryResolution is strictly ephemeral and cannot be serialized.")

    def __repr__(self) -> str:
        return (
            "OperationMemoryResolution("
            f"status={self._metadata.status.value!r}, "
            f"operation_id={self._metadata.operation_id!r}, "
            f"content_unit_id={self._metadata.content_unit_id!r}, "
            f"closed={self._closed})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class JobRefetchManifestItem:
    """Single non-raw Job handoff entry containing strictly IDs, versions, and hashes."""

    source_resource_id: str
    source_object_id: str
    source_object_revision_id: str
    content_unit_id: str
    expected_revision_hash: str
    expected_unit_hash: str
    canonicalization_version: str
    parser_version: str
    span_locator: SpanLocator | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_resource_id": self.source_resource_id,
            "source_object_id": self.source_object_id,
            "source_object_revision_id": self.source_object_revision_id,
            "content_unit_id": self.content_unit_id,
            "expected_revision_hash": normalize_sha256_hash(
                self.expected_revision_hash, field_name="expected_revision_hash"
            ),
            "expected_unit_hash": normalize_sha256_hash(
                self.expected_unit_hash, field_name="expected_unit_hash"
            ),
            "canonicalization_version": self.canonicalization_version,
            "parser_version": self.parser_version,
            "span_locator": self.span_locator.to_dict() if self.span_locator is not None else None,
        }
        assert_no_forbidden_raw_keys(payload, context="JobRefetchManifestItem")
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRefetchManifestItem:
        assert_no_forbidden_raw_keys(data, context="JobRefetchManifestItem")
        raw_locator = data.get("span_locator")
        locator = SpanLocator.from_mapping(dict(raw_locator)) if isinstance(raw_locator, Mapping) else None
        return cls(
            source_resource_id=str(data["source_resource_id"]),
            source_object_id=str(data["source_object_id"]),
            source_object_revision_id=str(data["source_object_revision_id"]),
            content_unit_id=str(data["content_unit_id"]),
            expected_revision_hash=normalize_sha256_hash(
                str(data["expected_revision_hash"]), field_name="expected_revision_hash"
            ),
            expected_unit_hash=normalize_sha256_hash(
                str(data["expected_unit_hash"]), field_name="expected_unit_hash"
            ),
            canonicalization_version=str(data["canonicalization_version"]),
            parser_version=str(data["parser_version"]),
            span_locator=locator,
        )


@dataclass(frozen=True, slots=True)
class JobRefetchManifest:
    """Cross-job boundary payload carrying strictly non-raw IDs & hashes (REF-08b)."""

    operation_id: str
    workspace_id: str
    items: tuple[JobRefetchManifestItem, ...]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation_id": self.operation_id,
            "workspace_id": self.workspace_id,
            "items": [item.to_dict() for item in self.items],
        }
        assert_no_forbidden_raw_keys(payload, context="JobRefetchManifest")
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRefetchManifest:
        assert_no_forbidden_raw_keys(data, context="JobRefetchManifest")
        items = tuple(JobRefetchManifestItem.from_dict(item) for item in data.get("items", ()))
        return cls(
            operation_id=str(data["operation_id"]),
            workspace_id=str(data["workspace_id"]),
            items=items,
        )


@dataclass(frozen=True, slots=True)
class ExportGatePrecondition:
    """Preconditions required for EXPORT_DOWNLOAD gates (REF-07f / REF-07g)."""

    claim_version_approved: bool
    requested_claim_version: int
    approved_claim_version: int | None


@dataclass(frozen=True, slots=True)
class GateVerificationResult:
    """Outcome of verify_required_supports_for_gate for approval, verified, and export gates."""

    allowed: bool
    operation_kind: GateOperationKind
    operation_id: str
    checked_at: datetime
    resolutions: tuple[ResolutionMetadata, ...]
    error: ResolutionErrorMapping | None = None
    blocked_unit_id: str | None = None
    reason_code: str | None = None
