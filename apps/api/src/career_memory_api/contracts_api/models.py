"""Pydantic contract schemas and fail-closed boundary validators for M1 OpenAPI.

Enforces:
- Claim write boundary: rejects direct writes to ``verification`` or ``SOURCE_VERIFIED``
- OD-008 Search contract: Workspace + optional ``source_resource_id`` only; globally forbids ``project_id``
- OD-003 Source deletion preview & confirm contract: version-bound ``preview_token`` & impact counts
- Plan C raw-text non-persistence across mutation payloads
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from career_memory_api.schema_contracts.enums import ClaimStatus, ClaimVerification
from career_memory_api.schema_contracts.metadata_guard import (
    RawTextPersistenceViolationError,
    find_forbidden_raw_keys,
)

PRIVATE_NO_STORE_HEADER = "private, no-store"
DEFAULT_SEARCH_INDEX_VERSION = "fts-index-v1"
DEFAULT_SEARCH_RANKING_VERSION = "rank-bm25-v1"
SEARCH_CURSOR_VERSION = "cm-search-cursor-v1"
DELETION_PREVIEW_TOKEN_VERSION = "cm-del-preview-v1"
_DEFAULT_CONTRACT_SIGNING_SECRET = "career-memory-m1-contract-token-key-v1"

FORBIDDEN_VERIFICATION_WRITE_FIELDS: frozenset[str] = frozenset(
    {
        "verification",
        "computed_verification",
        "verification_status",
        "claim_verification",
    }
)

FORBIDDEN_PROJECT_SCOPE_FIELDS: frozenset[str] = frozenset(
    {
        "project_id",
        "projectid",
        "project_ids",
        "projectids",
    }
)


class ContractBoundaryError(Exception):
    """Structured HTTP error raised by M1 contract boundary validators."""

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


class ErrorBody(BaseModel):
    """Standard Career Memory API error payload (MVP API §3)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    retryable: bool
    request_id: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """Standard Career Memory API error envelope."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


# =============================================================================
# Claim Write Boundary (Read-Only Computed Verification Guard)
# =============================================================================
def _contains_source_verified_literal(payload: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(payload, Mapping):
        for k, v in payload.items():
            hits.extend(_contains_source_verified_literal(v, f"{path}.{k}"))
    elif isinstance(payload, (list, tuple)):
        for idx, item in enumerate(payload):
            hits.extend(_contains_source_verified_literal(item, f"{path}[{idx}]"))
    elif isinstance(payload, str) and payload.strip().upper() == ClaimVerification.SOURCE_VERIFIED.value:
        hits.append(path)
    return hits


def _find_forbidden_keys_recursive(
    payload: Any, forbidden: frozenset[str], path: str = "$"
) -> list[str]:
    hits: list[str] = []
    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key_str = str(raw_key)
            key_path = f"{path}.{key_str}"
            if key_str.strip().lower() in forbidden:
                hits.append(key_path)
            hits.extend(_find_forbidden_keys_recursive(value, forbidden, key_path))
    elif isinstance(payload, (list, tuple)):
        for idx, item in enumerate(payload):
            hits.extend(_find_forbidden_keys_recursive(item, forbidden, f"{path}[{idx}]"))
    return hits


def assert_no_readonly_verification_write(payload: Any) -> None:
    """Reject any Claim write payload that specifies ``verification`` or ``SOURCE_VERIFIED``."""
    forbidden_field_paths = _find_forbidden_keys_recursive(
        payload, FORBIDDEN_VERIFICATION_WRITE_FIELDS
    )
    if forbidden_field_paths:
        raise ContractBoundaryError(
            status_code=422,
            code="PROHIBITED_READONLY_VERIFICATION_WRITE",
            message=(
                "Claim verification is strictly read-only and computed from verified provenance; "
                "direct write fields are forbidden."
            ),
            details={"forbidden_paths": forbidden_field_paths},
        )

    sv_paths = _contains_source_verified_literal(payload)
    if sv_paths:
        raise ContractBoundaryError(
            status_code=422,
            code="PROHIBITED_READONLY_VERIFICATION_WRITE",
            message=(
                "SOURCE_VERIFIED cannot be set directly by client or generator write payloads."
            ),
            details={"forbidden_paths": sv_paths},
        )


def assert_no_prohibited_project_id(payload: Any) -> None:
    """Enforce OD-008: ``project_id`` is globally prohibited from search and API contracts."""
    hits = _find_forbidden_keys_recursive(payload, FORBIDDEN_PROJECT_SCOPE_FIELDS)
    if hits:
        raise ContractBoundaryError(
            status_code=422,
            code="PROHIBITED_PROJECT_ID_SCOPE",
            message=(
                "OD-008: project_id is not a valid scope parameter; scope strictly by "
                "authenticated workspace_id and optional source_resource_id."
            ),
            details={"forbidden_paths": hits},
        )


def assert_no_plan_c_raw_text_keys(payload: Any) -> None:
    """Enforce Plan C raw-text non-persistence across write payloads."""
    violations = find_forbidden_raw_keys(payload)
    if violations:
        raise ContractBoundaryError(
            status_code=422,
            code="PROHIBITED_RAW_TEXT_PERSISTENCE",
            message="Plan C violation: raw source text or quote fields are forbidden.",
            details={
                "forbidden_paths": [path for path, _ in violations],
            },
        )


class ClaimDraftCreateRequest(BaseModel):
    """POST /v1/claims/drafts request contract (read-only verification strictly absent)."""

    model_config = ConfigDict(extra="forbid")

    confirmed_event_id: str = Field(..., min_length=1)
    input_fact_ids: list[str] = Field(..., min_length=1)
    claim_text: str = Field(..., min_length=1)
    status: Literal["DRAFT"] = "DRAFT"


class ClaimEditRequest(BaseModel):
    """PATCH /v1/claims/{claim_id} request contract (creates new version, verification absent)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    claim_text: str = Field(..., min_length=1)
    edit_reason: str | None = None


class ClaimApproveRequest(BaseModel):
    """POST /v1/claims/{claim_id}/approve request contract (verification absent)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    acknowledge_unverified_atoms: bool = False


class ClaimBoundaryResponse(BaseModel):
    """Claim API response exposing strictly read-only computed verification."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    claim_text: str = Field(..., min_length=1)
    status: ClaimStatus
    computed_verification: ClaimVerification = Field(
        ...,
        description="Read-only computed verification status derived from verified provenance.",
        json_schema_extra={"readOnly": True},
    )


# =============================================================================
# OD-008 Search Contract (Workspace + Optional source_resource_id, NO project_id)
# =============================================================================
class SearchEntityType(StrEnum):
    """Searchable non-raw domain entity categories (OD-008 / T-501)."""

    WORK_EVENT = "WORK_EVENT"
    FACT = "FACT"
    CLAIM = "CLAIM"


class SearchScope(BaseModel):
    """Authoritative search scope (OD-008: workspace_id + optional source_resource_id only)."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None


class SearchCursorPayload(BaseModel):
    """Versioned opaque cursor bound to workspace, source_resource, query, index, and ranking."""

    model_config = ConfigDict(extra="forbid")

    v: Literal["cm-search-cursor-v1"] = SEARCH_CURSOR_VERSION
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None
    query_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    index_version: str = Field(..., min_length=1)
    ranking_version: str = Field(..., min_length=1)
    offset: int = Field(..., ge=0)


class SearchHit(BaseModel):
    """Single non-raw search hit."""

    model_config = ConfigDict(extra="forbid")

    entity_type: SearchEntityType
    entity_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None
    title_or_summary: str = Field(..., min_length=1)
    score: float = Field(..., ge=0.0)


class SearchResponse(BaseModel):
    """GET /v1/search response contract (OD-008)."""

    model_config = ConfigDict(extra="forbid")

    scope: SearchScope
    query_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    index_version: str = Field(..., min_length=1)
    ranking_version: str = Field(..., min_length=1)
    items: list[SearchHit]
    next_cursor: str | None = None


def compute_query_hash(
    query: str,
    entity_types: list[SearchEntityType] | tuple[SearchEntityType, ...] | None = None,
) -> str:
    """Compute canonical SHA-256 query hash for cursor binding."""
    normalized_types = sorted({et.value for et in (entity_types or ())})
    canonical = json.dumps(
        {"entity_types": normalized_types, "q": " ".join(query.strip().split())},
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


def _sign_payload(payload_bytes: bytes, *, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return _b64url_encode(digest)


def encode_search_cursor(
    payload: SearchCursorPayload,
    *,
    secret: str = _DEFAULT_CONTRACT_SIGNING_SECRET,
) -> str:
    """Encode a versioned opaque search cursor bound to workspace/query/index/ranking."""
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _sign_payload(raw, secret=secret)
    return f"{_b64url_encode(raw)}.{sig}"


def decode_and_verify_search_cursor(
    cursor: str,
    *,
    expected_workspace_id: str,
    expected_source_resource_id: str | None,
    expected_query_hash: str,
    expected_index_version: str,
    expected_ranking_version: str,
    secret: str = _DEFAULT_CONTRACT_SIGNING_SECRET,
) -> SearchCursorPayload:
    """Decode and strictly verify all 5 cursor binding dimensions (OD-008)."""
    if "." not in cursor:
        raise ContractBoundaryError(
            status_code=400,
            code="INVALID_SEARCH_CURSOR",
            message="Malformed opaque search cursor.",
        )
    encoded_body, provided_sig = cursor.split(".", 1)
    try:
        raw = _b64url_decode(encoded_body)
        expected_sig = _sign_payload(raw, secret=secret)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise ValueError("signature mismatch")
        parsed_raw = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ContractBoundaryError(
            status_code=400,
            code="INVALID_SEARCH_CURSOR",
            message="Search cursor signature or encoding is invalid.",
        ) from exc

    assert_no_prohibited_project_id(parsed_raw)
    try:
        payload = SearchCursorPayload.model_validate(parsed_raw)
    except Exception as exc:  # noqa: BLE001
        raise ContractBoundaryError(
            status_code=400,
            code="INVALID_SEARCH_CURSOR",
            message="Search cursor structure is invalid.",
        ) from exc

    mismatches: list[str] = []
    if payload.workspace_id != expected_workspace_id:
        mismatches.append("workspace_id")
    if payload.source_resource_id != expected_source_resource_id:
        mismatches.append("source_resource_id")
    if payload.query_hash != expected_query_hash:
        mismatches.append("query_hash")
    if payload.index_version != expected_index_version:
        mismatches.append("index_version")
    if payload.ranking_version != expected_ranking_version:
        mismatches.append("ranking_version")
    if mismatches:
        raise ContractBoundaryError(
            status_code=400,
            code="INVALID_SEARCH_CURSOR",
            message=(
                "Search cursor binding mismatch for: " + ", ".join(mismatches)
            ),
            details={"mismatched_bindings": mismatches},
        )
    return payload


# =============================================================================
# OD-003 Source Deletion Preview & Confirm Contract
# =============================================================================
class DeletionImpactCounts(BaseModel):
    """Affected object counts returned by deletion preview (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    work_events: int = Field(..., ge=0)
    facts: int = Field(..., ge=0)
    contributions: int = Field(..., ge=0)
    claims_draft: int = Field(..., ge=0)
    claims_edited: int = Field(..., ge=0)
    claims_approved: int = Field(..., ge=0)
    claims_historical_versions: int = Field(..., ge=0)
    search_index_entries: int = Field(..., ge=0)
    ephemeral_cache_entries: int = Field(..., ge=0)
    server_exports: int = Field(..., ge=0)


class NonRecoverableScopes(BaseModel):
    """Explicit disclosure of copies outside server purge reach (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    already_downloaded_exports: Literal[True] = True
    provider_hosted_originals: Literal[True] = True
    notice: str = Field(
        default=(
            "Previously downloaded local export files and remote provider-hosted "
            "source originals cannot be recalled by server-side deletion."
        ),
        min_length=1,
    )


class DeletionPreviewTokenPayload(BaseModel):
    """Version-bound deletion preview token payload (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    v: Literal["cm-del-preview-v1"] = DELETION_PREVIEW_TOKEN_VERSION
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str = Field(..., min_length=1)
    resource_version: str = Field(..., min_length=1)
    impact_scope_version: str = Field(..., min_length=1)
    expires_at: datetime


class DeletionPreviewResponse(BaseModel):
    """POST /v1/source-resources/{source_resource_id}/deletion-preview response (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    source_resource_id: str = Field(..., min_length=1)
    resource_version: str = Field(..., min_length=1)
    impact_scope_version: str = Field(..., min_length=1)
    impact_counts: DeletionImpactCounts
    non_recoverable_scopes: NonRecoverableScopes
    preview_token: str = Field(..., min_length=1)
    expires_at: datetime


class DeletionConfirmRequest(BaseModel):
    """POST /v1/source-resources/{source_resource_id}/deletion-confirm request (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    preview_token: str = Field(..., min_length=1)
    expected_resource_version: str = Field(..., min_length=1)
    expected_impact_scope_version: str = Field(..., min_length=1)
    acknowledge_irreversible_purge: Literal[True]


class DeletionConfirmAcceptedResponse(BaseModel):
    """202 Accepted response for confirmed source deletion (OD-003)."""

    model_config = ConfigDict(extra="forbid")

    deletion_job_id: str = Field(..., min_length=1)
    source_resource_id: str = Field(..., min_length=1)
    status: Literal["PENDING"] = "PENDING"
    fence_active: Literal[True] = True


def encode_deletion_preview_token(
    payload: DeletionPreviewTokenPayload,
    *,
    secret: str = _DEFAULT_CONTRACT_SIGNING_SECRET,
) -> str:
    """Encode a signed, version-bound deletion preview token."""
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _sign_payload(raw, secret=secret)
    return f"{_b64url_encode(raw)}.{sig}"


def decode_and_verify_deletion_preview_token(
    token: str,
    *,
    expected_workspace_id: str,
    expected_source_resource_id: str,
    current_resource_version: str,
    current_impact_scope_version: str,
    now: datetime | None = None,
    secret: str = _DEFAULT_CONTRACT_SIGNING_SECRET,
) -> DeletionPreviewTokenPayload:
    """Verify preview_token signature, expiry, and version/scope binding (409 on stale/expired)."""
    if "." not in token:
        raise ContractBoundaryError(
            status_code=409,
            code="DELETION_PREVIEW_INVALID",
            message="The deletion preview token is malformed or invalid.",
        )
    encoded_body, provided_sig = token.split(".", 1)
    try:
        raw = _b64url_decode(encoded_body)
        expected_sig = _sign_payload(raw, secret=secret)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise ValueError("signature mismatch")
        payload = DeletionPreviewTokenPayload.model_validate(json.loads(raw.decode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        raise ContractBoundaryError(
            status_code=409,
            code="DELETION_PREVIEW_INVALID",
            message="The deletion preview token could not be verified.",
        ) from exc

    check_time = now if now is not None else datetime.now(UTC)
    if check_time.tzinfo is None:
        check_time = check_time.replace(tzinfo=UTC)
    expires_at = (
        payload.expires_at.replace(tzinfo=UTC)
        if payload.expires_at.tzinfo is None
        else payload.expires_at.astimezone(UTC)
    )
    if check_time >= expires_at:
        raise ContractBoundaryError(
            status_code=409,
            code="DELETION_PREVIEW_EXPIRED",
            message="The deletion preview token has expired; request a fresh deletion preview.",
        )

    if (
        payload.workspace_id != expected_workspace_id
        or payload.source_resource_id != expected_source_resource_id
        or payload.resource_version != current_resource_version
        or payload.impact_scope_version != current_impact_scope_version
    ):
        raise ContractBoundaryError(
            status_code=409,
            code="DELETION_PREVIEW_SCOPE_CHANGED",
            message=(
                "The target resource version or deletion impact scope changed since preview; "
                "request a fresh deletion preview before confirming."
            ),
        )
    return payload


__all__ = [
    "ClaimApproveRequest",
    "ClaimBoundaryResponse",
    "ClaimDraftCreateRequest",
    "ClaimEditRequest",
    "ContractBoundaryError",
    "DEFAULT_SEARCH_INDEX_VERSION",
    "DEFAULT_SEARCH_RANKING_VERSION",
    "DELETION_PREVIEW_TOKEN_VERSION",
    "DeletionConfirmAcceptedResponse",
    "DeletionConfirmRequest",
    "DeletionImpactCounts",
    "DeletionPreviewResponse",
    "DeletionPreviewTokenPayload",
    "ErrorBody",
    "ErrorEnvelope",
    "FORBIDDEN_PROJECT_SCOPE_FIELDS",
    "FORBIDDEN_VERIFICATION_WRITE_FIELDS",
    "NonRecoverableScopes",
    "PRIVATE_NO_STORE_HEADER",
    "RawTextPersistenceViolationError",
    "SEARCH_CURSOR_VERSION",
    "SearchCursorPayload",
    "SearchEntityType",
    "SearchHit",
    "SearchResponse",
    "SearchScope",
    "assert_no_plan_c_raw_text_keys",
    "assert_no_prohibited_project_id",
    "assert_no_readonly_verification_write",
    "compute_query_hash",
    "decode_and_verify_deletion_preview_token",
    "decode_and_verify_search_cursor",
    "encode_deletion_preview_token",
    "encode_search_cursor",
]
