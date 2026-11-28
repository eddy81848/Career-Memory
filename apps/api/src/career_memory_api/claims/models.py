"""Typed domain models, wire schemas, cursor helpers, and guards for M4 Wave 3A Claims."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from career_memory_api.auth.redaction import redact_text
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    ContentUnitType,
    EphemeralResolvedUnit,
    PersistedContentUnitMetadata,
    build_span_locator,
    sha256_prefixed_text,
)
from career_memory_api.contracts_api.models import (
    assert_no_plan_c_raw_text_keys,
    assert_no_prohibited_project_id,
    assert_no_readonly_verification_write,
)
from career_memory_api.resolver.models import (
    OperationMemoryResolution,
    ResolutionMetadata,
    ResolutionStatus,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    FactStatus,
    FactType,
    LinkStatus,
    SupportType,
    SupportValidator,
)

CLAIMS_NO_STORE_HEADER = "private, no-store"
CLAIMS_CURSOR_VERSION = "cm-claims-cursor-v1"
_DEFAULT_CLAIMS_CURSOR_SECRET = "career-memory-m4-claims-cursor-key-v1"

DEFAULT_CLAIM_GENERATOR_VERSION = "claim-generator-v1"
DEFAULT_CLAIM_GENERATOR_PROMPT_VERSION = "claim-generator-prompt-v1"
DEFAULT_CLAIM_GENERATOR_MODEL = "synthetic-claim-generator-v1"

DEFAULT_CLAIM_ATOMIZER_VERSION = "claim-atomizer-v1"
DEFAULT_CLAIM_ATOMIZER_PROMPT_VERSION = "claim-atomizer-prompt-v1"
DEFAULT_CLAIM_ATOMIZER_MODEL = "synthetic-claim-atomizer-v1"

DEFAULT_CLAIM_VALIDATOR_VERSION = "claim-validator-v1"
DEFAULT_CLAIM_VALIDATOR_PROMPT_VERSION = "claim-validator-prompt-v1"
DEFAULT_CLAIM_VALIDATOR_MODEL = "synthetic-claim-validator-v1"

DEFAULT_CONTEXT_MAX_BYTES = 64 * 1024
DEFAULT_CONTEXT_MAX_TOKENS = 12_000


class ContextBuildStatus(StrEnum):
    """Deterministic outcome of Stage 3A ContextBuilder budget and eligibility filtering."""

    READY = "READY"
    EMPTY = "EMPTY"
    PARTIAL_WITH_LIMIT = "PARTIAL_WITH_LIMIT"
    MANUAL_ONLY = "MANUAL_ONLY"


class ClaimGenerationStatus(StrEnum):
    """Top-level outcome of POST /v1/claims/generations."""

    SUCCEEDED = "SUCCEEDED"
    MANUAL_ONLY_BLOCKED = "MANUAL_ONLY_BLOCKED"
    DRAFT_INVALID = "DRAFT_INVALID"


class ClaimError(Exception):
    """Structured domain & HTTP error raised by the M4 Claims pipeline and API."""

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


def assert_no_forbidden_claim_write_keys(payload: Mapping[str, Any]) -> None:
    """Reject OD-008 project_id, read-only verification/SOURCE_VERIFIED writes, and Plan C raw keys."""
    assert_no_prohibited_project_id(payload)
    assert_no_readonly_verification_write(payload)
    assert_no_plan_c_raw_text_keys(payload)


def build_ephemeral_manifest_resolution(
    *,
    operation_id: str,
    manifest_text: str,
    revision_id: str,
    content_unit_id: str,
    checked_at: datetime | None = None,
) -> tuple[OperationMemoryResolution, str, str]:
    """Wrap canonical non-raw stage manifest text in an ephemeral OperationMemoryResolution.

    Used by Stage 3A/3B/3C when executing through ``BoundedStageRunner`` so every AI stage
    shares the single fail-closed T-408 gate while maintaining Plan C zero raw-source storage.
    """
    ts = checked_at or utc_now()
    canonical_text = manifest_text if manifest_text.strip() else "{}"
    locator = build_span_locator(canonical_text, 0, len(canonical_text))
    digest = sha256_prefixed_text(canonical_text)
    meta = ResolutionMetadata(
        status=ResolutionStatus.MATCHED,
        checked_at=ts,
        operation_id=operation_id,
        source_object_revision_id=revision_id,
        content_unit_id=content_unit_id,
        expected_revision_hash=digest,
        observed_revision_hash=digest,
        expected_unit_hash=locator.span_sha256,
        observed_unit_hash=locator.span_sha256,
        canonicalization_version=CANONICALIZATION_VERSION,
        parser_version=PARSER_VERSION,
        retryable=False,
        reason_code=None,
        requests_used=0,
        bytes_read=len(canonical_text.encode("utf-8")),
        deduped=True,
        fetch_attempted=False,
    )
    persisted_meta = PersistedContentUnitMetadata(
        unit_id=content_unit_id,
        unit_index=0,
        unit_type=ContentUnitType.PR_BODY_SECTION,
        source_content_sha256=digest,
        locator=locator,
        original_locator=None,
        parser_version=PARSER_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
    )
    ephemeral_unit = EphemeralResolvedUnit(metadata=persisted_meta, _text=canonical_text)
    resolution = OperationMemoryResolution(
        metadata=meta,
        ephemeral_unit=ephemeral_unit,
    )
    return resolution, digest, locator.span_sha256


# =============================================================================
# HTTP Wire Contract Models (/v1/claims*)
# =============================================================================
class ClaimSummaryItem(BaseModel):
    """Summary projection of a single Claim version with read-only computed verification."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    supersedes_claim_id: str | None = None
    claim_text: str = Field(..., min_length=1)
    status: ClaimStatus
    computed_verification: ClaimVerification = Field(
        ...,
        description="Read-only computed verification derived from active verified provenance.",
        json_schema_extra={"readOnly": True},
    )
    atom_count: int = Field(..., ge=0)
    supported_atom_count: int = Field(..., ge=0)
    user_confirmed_atom_count: int = Field(..., ge=0)
    unsupported_atom_count: int = Field(..., ge=0)
    stale_atom_count: int = Field(..., ge=0)
    approvable: bool
    approval_block_reasons: list[str] = Field(default_factory=list)
    support_graph_epoch: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    approved_at: datetime | None = None
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ClaimPageInfo(BaseModel):
    """Pagination metadata for GET /v1/claims."""

    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None = None
    has_more: bool


class ClaimListResponse(BaseModel):
    """GET /v1/claims response envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[ClaimSummaryItem]
    page: ClaimPageInfo


class ClaimAtomDetailItem(BaseModel):
    """Detailed ClaimAtom item returned inside ClaimDetailResponse."""

    model_config = ConfigDict(extra="forbid")

    atom_id: str = Field(..., min_length=1)
    ordinal: int = Field(..., ge=0)
    atom_type: ClaimAtomType
    atom_text: str = Field(..., min_length=1)
    status: ClaimAtomStatus
    unsupported_reason: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    role_contribution_grounding: dict[str, Any] | None = None
    metric_grounding: dict[str, Any] | None = None


class ClaimDetailResponse(ClaimSummaryItem):
    """Full Claim detail returned by GET /v1/claims/{claim_id} and action endpoints."""

    model_config = ConfigDict(extra="forbid")

    generator_run_id: str | None = None
    last_verification_run_id: str | None = None
    atoms: list[ClaimAtomDetailItem] = Field(default_factory=list)


class ClaimSupportFactRef(BaseModel):
    """Non-raw Fact summary embedded inside GET /v1/claims/{claim_id}/supports."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    fact_type: FactType
    predicate: str = Field(..., min_length=1)
    object_text: str = Field(..., min_length=1)
    status: FactStatus
    normalized_value: dict[str, Any] | None = None


class ClaimSupportFactSupportRef(BaseModel):
    """Non-raw FactSupport + ContentUnit + SourceObjectRevision anchor (Plan C: zero quotes)."""

    model_config = ConfigDict(extra="forbid")

    fact_support_id: str = Field(..., min_length=1)
    support_type: SupportType
    status: LinkStatus
    validator: SupportValidator
    validator_version: str = Field(..., min_length=1)
    content_unit_id: str = Field(..., min_length=1)
    unit_type: ContentUnitType
    locator: dict[str, Any] = Field(default_factory=dict)
    source_object_revision_id: str = Field(..., min_length=1)
    revision_content_hash: str = Field(..., min_length=1)
    revision_observed_at: datetime
    revision_is_tombstone: bool
    last_checked_at: str | None = None
    last_resolution_status: str | None = None


class ClaimAtomSupportProvenanceItem(BaseModel):
    """Single ClaimAtomSupport link and its Fact -> FactSupport provenance chain."""

    model_config = ConfigDict(extra="forbid")

    claim_atom_support_id: str = Field(..., min_length=1)
    support_type: SupportType
    status: LinkStatus
    validation_run_id: str | None = None
    fact: ClaimSupportFactRef
    fact_supports: list[ClaimSupportFactSupportRef] = Field(default_factory=list)


class ClaimAtomProvenanceItem(BaseModel):
    """ClaimAtom with its full non-raw provenance chain."""

    model_config = ConfigDict(extra="forbid")

    atom_id: str = Field(..., min_length=1)
    ordinal: int = Field(..., ge=0)
    atom_type: ClaimAtomType
    atom_text: str = Field(..., min_length=1)
    status: ClaimAtomStatus
    supports: list[ClaimAtomSupportProvenanceItem] = Field(default_factory=list)


class ClaimSupportsResponse(BaseModel):
    """GET /v1/claims/{claim_id}/supports response payload."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    event_id: str = Field(..., min_length=1)
    status: ClaimStatus
    computed_verification: ClaimVerification = Field(
        ...,
        json_schema_extra={"readOnly": True},
    )
    support_graph_epoch: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    atoms: list[ClaimAtomProvenanceItem] = Field(default_factory=list)


class GenerateEventClaimsRequest(BaseModel):
    """POST /v1/claims/generations request payload (verification strictly absent)."""

    model_config = ConfigDict(extra="forbid")

    confirmed_event_id: str = Field(..., min_length=1)
    input_fact_ids: list[str] | None = None


class GenerateEventClaimsResponse(BaseModel):
    """POST /v1/claims/generations response payload."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1)
    generation_status: ClaimGenerationStatus
    reason_code: str | None = None
    free_alternatives: list[str] = Field(default_factory=list)
    claims: list[ClaimDetailResponse] = Field(default_factory=list)


class RevalidateClaimRequest(BaseModel):
    """POST /v1/claims/{claim_id}/actions/revalidate request payload."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)


class ApproveClaimActionRequest(BaseModel):
    """POST /v1/claims/{claim_id}/actions/approve request payload (verification strictly absent)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    expected_support_graph_epoch: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    acknowledge_user_confirmed_atoms: bool = False


class RejectClaimActionRequest(BaseModel):
    """POST /v1/claims/{claim_id}/actions/reject request payload."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _non_blank_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Rejection reason must not be blank.")
        return trimmed


# =============================================================================
# HMAC-Bound Opaque Pagination Cursor Helpers (GET /v1/claims)
# =============================================================================
class ClaimCursorPayload(BaseModel):
    """Opaque cursor bound to workspace_id and normalized filter parameters."""

    model_config = ConfigDict(extra="forbid")

    v: Literal["cm-claims-cursor-v1"] = CLAIMS_CURSOR_VERSION
    workspace_id: str = Field(..., min_length=1)
    filter_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    sort_ts: datetime
    last_claim_id: str = Field(..., min_length=1)


def compute_claims_filter_hash(
    *,
    event_id: str | None = None,
    status: ClaimStatus | None = None,
    include_superseded: bool = False,
) -> str:
    canonical = json.dumps(
        {
            "event_id": event_id,
            "include_superseded": bool(include_superseded),
            "status": status.value if status is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode((encoded + padding).encode("ascii"))


def _sign_cursor_bytes(payload_bytes: bytes, *, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return _b64url_encode(digest)


def encode_claim_cursor(
    payload: ClaimCursorPayload,
    *,
    secret: str = _DEFAULT_CLAIMS_CURSOR_SECRET,
) -> str:
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _sign_cursor_bytes(raw, secret=secret)
    return f"{_b64url_encode(raw)}.{sig}"


def decode_and_verify_claim_cursor(
    cursor: str,
    *,
    expected_workspace_id: str,
    expected_filter_hash: str,
    secret: str = _DEFAULT_CLAIMS_CURSOR_SECRET,
) -> ClaimCursorPayload:
    if "." not in cursor:
        raise ClaimError(
            status_code=400,
            code="INVALID_CLAIMS_CURSOR",
            message="Malformed claims pagination cursor.",
        )
    encoded_body, provided_sig = cursor.split(".", 1)
    try:
        raw = _b64url_decode(encoded_body)
        expected_sig = _sign_cursor_bytes(raw, secret=secret)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise ValueError("signature mismatch")
        parsed_raw = json.loads(raw.decode("utf-8"))
        payload = ClaimCursorPayload.model_validate(parsed_raw)
    except Exception as exc:  # noqa: BLE001
        raise ClaimError(
            status_code=400,
            code="INVALID_CLAIMS_CURSOR",
            message="Claims cursor signature or structure is invalid.",
        ) from exc

    mismatches: list[str] = []
    if payload.workspace_id != expected_workspace_id:
        mismatches.append("workspace_id")
    if payload.filter_hash != expected_filter_hash:
        mismatches.append("filters")
    if mismatches:
        raise ClaimError(
            status_code=400,
            code="INVALID_CLAIMS_CURSOR",
            message="Claims cursor binding mismatch for: " + ", ".join(mismatches),
            details={"mismatched_bindings": mismatches},
        )
    return payload


__all__ = [
    "CLAIMS_CURSOR_VERSION",
    "CLAIMS_NO_STORE_HEADER",
    "DEFAULT_CLAIM_ATOMIZER_MODEL",
    "DEFAULT_CLAIM_ATOMIZER_PROMPT_VERSION",
    "DEFAULT_CLAIM_ATOMIZER_VERSION",
    "DEFAULT_CLAIM_GENERATOR_MODEL",
    "DEFAULT_CLAIM_GENERATOR_PROMPT_VERSION",
    "DEFAULT_CLAIM_GENERATOR_VERSION",
    "DEFAULT_CLAIM_VALIDATOR_MODEL",
    "DEFAULT_CLAIM_VALIDATOR_PROMPT_VERSION",
    "DEFAULT_CLAIM_VALIDATOR_VERSION",
    "DEFAULT_CONTEXT_MAX_BYTES",
    "DEFAULT_CONTEXT_MAX_TOKENS",
    "ApproveClaimActionRequest",
    "ClaimAtomDetailItem",
    "ClaimAtomProvenanceItem",
    "ClaimAtomSupportProvenanceItem",
    "ClaimCursorPayload",
    "ClaimDetailResponse",
    "ClaimError",
    "ClaimGenerationStatus",
    "ClaimListResponse",
    "ClaimPageInfo",
    "ClaimSummaryItem",
    "ClaimSupportFactRef",
    "ClaimSupportFactSupportRef",
    "ClaimSupportsResponse",
    "ContextBuildStatus",
    "GenerateEventClaimsRequest",
    "GenerateEventClaimsResponse",
    "RejectClaimActionRequest",
    "RevalidateClaimRequest",
    "assert_no_forbidden_claim_write_keys",
    "build_ephemeral_manifest_resolution",
    "compute_claims_filter_hash",
    "decode_and_verify_claim_cursor",
    "encode_claim_cursor",
    "utc_now",
]
