"""Typed domain models, enums, cursor helpers, and error types for M4 Wave 2 Facts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from career_memory_api.auth.redaction import redact_text
from career_memory_api.canonicalization import (
    SpanLocator,
    extract_verified_span,
    sha256_prefixed_text,
)
from career_memory_api.conformance.ai_schemas import FactPromotionDecision
from career_memory_api.contracts_api.models import (
    assert_no_plan_c_raw_text_keys,
    assert_no_prohibited_project_id,
)
from career_memory_api.resolver.models import ResolutionStatus
from career_memory_api.schema_contracts.enums import (
    ContentUnitType,
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    GenerationRunStatus,
    LinkStatus,
    RecordOrigin,
    SupportType,
    SupportValidator,
)

FACTS_NO_STORE_HEADER = "private, no-store"
FACTS_CURSOR_VERSION = "cm-facts-cursor-v1"
_DEFAULT_FACTS_CURSOR_SECRET = "career-memory-m4-facts-cursor-key-v1"

DEFAULT_EXTRACTOR_VERSION = "fact-extractor-v1"
DEFAULT_EXTRACTOR_PROMPT_VERSION = "fact-extractor-prompt-v1"
DEFAULT_EXTRACTOR_MODEL = "synthetic-fact-extractor-v1"

DEFAULT_VALIDATOR_VERSION = "fact-validator-v1"
DEFAULT_VALIDATOR_PROMPT_VERSION = "fact-validator-prompt-v1"
DEFAULT_VALIDATOR_MODEL = "synthetic-fact-validator-v1"

DEFAULT_MAX_UNIT_BYTES = 256 * 1024

FORBIDDEN_FACT_WRITE_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "status",
        "verification",
        "verification_status",
        "claim_verification",
        "computed_verification",
        "support_type",
        "fact_support",
        "supports",
        "source_verified",
        "direct_support_count",
        "supersedes_fact_id",
        "version",
    }
)

_IF_MATCH_VERSION_PATTERN = re.compile(r'^"?v([1-9][0-9]*)"?$')


class FactCompletionState(StrEnum):
    """Structured completion state preserved across Stage 2A/2B (SCN-06, SCN-07)."""

    COMPLETED = "COMPLETED"
    PLANNED = "PLANNED"
    NEGATED = "NEGATED"
    TARGET_UNMET = "TARGET_UNMET"


class FactAttributionScope(StrEnum):
    """Personal vs team attribution boundary (SCN-05, SCN-08)."""

    INDIVIDUAL = "INDIVIDUAL"
    TEAM = "TEAM"
    UNSPECIFIED = "UNSPECIFIED"


class FactEnvironmentScope(StrEnum):
    """Environment qualification aligned with MetricEnvironmentScope (SCN-09, CLAIM-06)."""

    PRODUCTION = "PRODUCTION"
    STAGING_TEST = "STAGING_TEST"
    UNSPECIFIED = "UNSPECIFIED"


class FactCausalBasis(StrEnum):
    """Causation vs observed correlation qualifier (SCN-10)."""

    DIRECT_CAUSATION = "DIRECT_CAUSATION"
    OBSERVED_CORRELATION = "OBSERVED_CORRELATION"
    UNSPECIFIED = "UNSPECIFIED"


class FactError(Exception):
    """Structured domain & HTTP error raised by the M4 Facts pipeline and API."""

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


# =============================================================================
# Semantic Qualifiers & Metric Details (CLAIM-06, SCN-06..16)
# =============================================================================
class MetricDetailsModel(BaseModel):
    """Strict 6-element metric details required for METRIC facts (CLAIM-06 / SCN-09 / SCN-16)."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(..., min_length=1)
    unit: str = Field(..., min_length=1)
    period: str = Field(..., min_length=1)
    source_basis: str = Field(..., min_length=1)
    environment_scope: FactEnvironmentScope
    measurement_scope: str = Field(..., min_length=1)

    @field_validator("value", "unit", "period", "source_basis", "measurement_scope")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Metric detail field must not be blank.")
        return trimmed


class FactSemanticAttributes(BaseModel):
    """Structured semantic qualifiers stored in normalized_value / fact_metadata."""

    model_config = ConfigDict(extra="forbid")

    completion_state: FactCompletionState = FactCompletionState.COMPLETED
    scope: FactAttributionScope = FactAttributionScope.UNSPECIFIED
    environment_scope: FactEnvironmentScope = FactEnvironmentScope.UNSPECIFIED
    causal_basis: FactCausalBasis = FactCausalBasis.UNSPECIFIED
    uncertainty_notes: str | None = None


def extract_semantic_attributes(
    normalized_value: Mapping[str, Any] | None,
    *,
    metric_details: MetricDetailsModel | None = None,
    uncertainty_notes: str | None = None,
) -> FactSemanticAttributes:
    """Parse semantic qualifiers from normalized_value / metric_details safely."""
    nv = normalized_value if isinstance(normalized_value, Mapping) else {}

    def _parse_enum(enum_cls: type[StrEnum], raw: Any, default: Any) -> Any:
        if isinstance(raw, enum_cls):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return enum_cls(raw.strip().upper())
            except ValueError as exc:
                raise FactError(
                    status_code=422,
                    code="AI_SCHEMA_INVALID",
                    message=f"Invalid semantic qualifier {raw!r} for {enum_cls.__name__}.",
                ) from exc
        return default

    completion_state = _parse_enum(
        FactCompletionState,
        nv.get("completion_state"),
        FactCompletionState.COMPLETED,
    )
    scope = _parse_enum(
        FactAttributionScope,
        nv.get("scope"),
        FactAttributionScope.UNSPECIFIED,
    )
    env_default = (
        metric_details.environment_scope
        if metric_details is not None
        else FactEnvironmentScope.UNSPECIFIED
    )
    environment_scope = _parse_enum(
        FactEnvironmentScope,
        nv.get("environment_scope"),
        env_default,
    )
    if (
        metric_details is not None
        and environment_scope is not FactEnvironmentScope.UNSPECIFIED
        and environment_scope is not metric_details.environment_scope
    ):
        raise FactError(
            status_code=422,
            code="AI_SCHEMA_INVALID",
            message=(
                "normalized_value.environment_scope conflicts with metric_details.environment_scope."
            ),
        )
    causal_basis = _parse_enum(
        FactCausalBasis,
        nv.get("causal_basis"),
        FactCausalBasis.UNSPECIFIED,
    )
    notes = uncertainty_notes
    if notes is None and isinstance(nv.get("uncertainty_notes"), str):
        notes = nv["uncertainty_notes"]
    return FactSemanticAttributes(
        completion_state=completion_state,
        scope=scope,
        environment_scope=environment_scope,
        causal_basis=causal_basis,
        uncertainty_notes=notes,
    )


# =============================================================================
# Stage 2A / Stage 2B / Normalizer Internal Domain Records
# =============================================================================
@dataclass(frozen=True, slots=True)
class FactCandidateDraft:
    """Validated Stage 2A candidate item (always status=CANDIDATE, zero raw text)."""

    schema_version: str
    extractor_run_id: str
    extractor_version: str
    source_object_revision_id: str
    content_unit_id: str
    locator: dict[str, Any]
    fact_type: FactType
    predicate: str
    object_text: str
    normalized_value: dict[str, Any] | None
    status: FactStatus
    subject_identity_id: str | None
    metric_details: MetricDetailsModel | None
    uncertainty_notes: str | None
    semantic: FactSemanticAttributes

    def to_stage2a_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "extractor_run_id": self.extractor_run_id,
            "extractor_version": self.extractor_version,
            "source_object_revision_id": self.source_object_revision_id,
            "content_unit_id": self.content_unit_id,
            "locator": dict(self.locator),
            "fact_type": self.fact_type.value,
            "predicate": self.predicate,
            "object_text": self.object_text,
            "normalized_value": (
                dict(self.normalized_value) if self.normalized_value is not None else None
            ),
            "status": self.status.value,
            "subject_identity_id": self.subject_identity_id,
            "uncertainty_notes": self.uncertainty_notes,
        }
        if self.metric_details is not None:
            payload["metric_details"] = self.metric_details.model_dump(mode="json")
        return payload


@dataclass(frozen=True, slots=True)
class FactExtractionUnitResult:
    """Outcome of Stage 2A FactCandidateExtractor for a single ContentUnit."""

    extractor_run_id: str
    event_id: str
    content_unit_id: str
    source_object_revision_id: str
    status: GenerationRunStatus
    error_code: str | None
    candidates: tuple[FactCandidateDraft, ...]
    input_hash: str
    extractor_version: str
    prompt_version: str
    model: str
    input_artifact_versions: dict[str, Any]
    run_metadata: dict[str, Any]
    persisted_fact_ids: tuple[str, ...] = ()
    idempotent_reuse: bool = False


@dataclass(frozen=True, slots=True)
class FactValidationOutcome:
    """Outcome of Stage 2B FactSupportValidator for a single candidate (distinct validator_run_id)."""

    validator_run_id: str
    extractor_run_id: str
    fact_candidate_id: str
    source_object_revision_id: str
    content_unit_id: str
    verified_locator: dict[str, Any]
    support_type: SupportType
    validator: SupportValidator
    validator_version: str
    promotion_decision: FactPromotionDecision
    revision_and_locator_verified: bool
    rejection_reason: str | None
    checked_at: datetime
    resolution_status: ResolutionStatus
    guard_override_applied: bool = False
    guard_reason_code: str | None = None
    support_metadata: dict[str, Any] = field(default_factory=dict)
    run_status: GenerationRunStatus = GenerationRunStatus.SUCCEEDED
    run_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ContributionRecord:
    """Persisted or evaluated Contribution row (Data Model §3.8, CLAIM-05)."""

    contribution_id: str
    workspace_id: str
    event_id: str
    identity_id: str
    role: ContributionRole
    origin: RecordOrigin
    status: ContributionStatus
    supporting_fact_ids: tuple[str, ...]
    created_at: datetime | None = None


# =============================================================================
# Locator Verification Helper (Unit-Relative OR Document-Relative Sub-Span)
# =============================================================================
def verify_locator_in_unit_text(
    unit_text: str,
    candidate_locator_mapping: Mapping[str, Any],
    *,
    unit_locator: SpanLocator | None = None,
) -> tuple[bool, str | None, tuple[str, ...]]:
    """Verify a CmLocatorV1 strictly inside the in-memory ContentUnit canonical text.

    Supports both:
    - Unit-relative coordinates (``0 <= char_start < char_end <= len(unit_text)``), verified
      with full ``extract_verified_span(unit_text, locator)``.
    - Document-relative coordinates (when ``unit_locator`` starts at ``> 0`` and the candidate
      locator lies strictly within ``[unit_locator.char_start, unit_locator.char_end]``),
      verifying relative char/utf8/utf16/line lengths and exact ``span_sha256``.
    """
    try:
        loc = SpanLocator.from_mapping(dict(candidate_locator_mapping))
    except Exception:  # noqa: BLE001
        return False, None, ("MALFORMED_LOCATOR",)

    # Try direct unit-relative verification first when bounds fit inside unit_text
    if 0 <= loc.char_start < loc.char_end <= len(unit_text):
        direct = extract_verified_span(unit_text, loc)
        if direct.verified and direct.span is not None:
            return True, direct.span.text, ()
        # If unit_locator starts at 0, there is no alternative coordinate frame
        if unit_locator is None or unit_locator.char_start == 0:
            return False, None, direct.error_codes

    # Document-relative sub-span strictly contained in unit_locator
    if unit_locator is not None and unit_locator.char_start > 0:
        if (
            loc.char_start < unit_locator.char_start
            or loc.char_end > unit_locator.char_end
            or loc.utf8_byte_start < unit_locator.utf8_byte_start
            or loc.utf8_byte_end > unit_locator.utf8_byte_end
            or loc.utf16_start < unit_locator.utf16_start
            or loc.utf16_end > unit_locator.utf16_end
            or loc.line_start < unit_locator.line_start
            or loc.line_end > unit_locator.line_end
        ):
            return False, None, ("LOCATOR_OUTSIDE_CONTENT_UNIT",)

        rel_start = loc.char_start - unit_locator.char_start
        rel_end = loc.char_end - unit_locator.char_start
        if rel_start < 0 or rel_end <= rel_start or rel_end > len(unit_text):
            return False, None, ("CHAR_OFFSET_OUT_OF_BOUNDS",)

        slice_text = unit_text[rel_start:rel_end]
        errors: list[str] = []
        if len(slice_text) != loc.char_length:
            errors.append("CHAR_LENGTH_MISMATCH")
        prefix_text = unit_text[:rel_start]
        if (
            unit_locator.utf8_byte_start + len(prefix_text.encode("utf-8")) != loc.utf8_byte_start
            or len(slice_text.encode("utf-8")) != loc.utf8_byte_length
        ):
            errors.append("UTF8_BYTE_OFFSET_MISMATCH")
        prefix_u16 = len(prefix_text.encode("utf-16-le")) // 2
        slice_u16 = len(slice_text.encode("utf-16-le")) // 2
        if (
            unit_locator.utf16_start + prefix_u16 != loc.utf16_start
            or loc.utf16_end - loc.utf16_start != slice_u16
        ):
            errors.append("UTF16_OFFSET_MISMATCH")
        if sha256_prefixed_text(slice_text) != loc.span_sha256:
            errors.append("SPAN_HASH_MISMATCH")
        if errors:
            return False, None, tuple(errors)
        return True, slice_text, ()

    return False, None, ("CHAR_OFFSET_OUT_OF_BOUNDS",)


# =============================================================================
# HTTP Wire Contract Models (/v1/facts*, MVP API §6, ER-012)
# =============================================================================
def format_fact_etag(version: int) -> str:
    if version < 1:
        raise ValueError(f"Fact version must be >= 1, got {version!r}.")
    return f'"v{version}"'


def parse_fact_if_match_version(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    cleaned = if_match.strip()
    if not cleaned:
        return None
    match = _IF_MATCH_VERSION_PATTERN.match(cleaned)
    if match is None:
        raise FactError(
            status_code=422,
            code="INVALID_IF_MATCH_HEADER",
            message='If-Match header must match format "v{version}" or v{version}.',
        )
    return int(match.group(1))


def resolve_expected_version(
    *,
    if_match_header: str | None,
    body_expected_version: int | None,
) -> int:
    """Resolve expected_version from If-Match header and/or request body."""
    header_ver = parse_fact_if_match_version(if_match_header)
    if header_ver is not None and body_expected_version is not None:
        if header_ver != body_expected_version:
            raise FactError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message="If-Match version does not match body expected_version.",
            )
        return header_ver
    resolved = header_ver if header_ver is not None else body_expected_version
    if resolved is None:
        raise FactError(
            status_code=422,
            code="MISSING_EXPECTED_VERSION",
            message="Either If-Match header or body expected_version is required.",
        )
    return resolved


def assert_no_forbidden_fact_write_keys(payload: Mapping[str, Any]) -> None:
    """Reject OD-008 project_id, Plan C raw keys, and writable verification/status overrides."""
    assert_no_prohibited_project_id(payload)
    assert_no_plan_c_raw_text_keys(payload)
    bad = sorted(
        str(k)
        for k in payload.keys()
        if str(k).strip().lower() in FORBIDDEN_FACT_WRITE_STATUS_KEYS
    )
    if bad:
        raise FactError(
            status_code=422,
            code="PROHIBITED_FACT_STATUS_WRITE",
            message=(
                "Fact write requests must not directly assign verification, support, or status "
                f"fields: {', '.join(bad)}."
            ),
            details={"forbidden_fields": bad},
        )


class FactSummaryItem(BaseModel):
    """Single Fact projection returned by GET /v1/facts and mutation endpoints."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    supersedes_fact_id: str | None = None
    fact_type: FactType
    subject_identity_id: str | None = None
    predicate: str = Field(..., min_length=1)
    object_text: str = Field(..., min_length=1)
    normalized_value: dict[str, Any] | None = None
    fact_metadata: dict[str, Any] = Field(default_factory=dict)
    status: FactStatus
    extractor_version: str = Field(..., min_length=1)
    direct_support_count: int = Field(..., ge=0)
    context_support_count: int = Field(..., ge=0)
    contradicts_support_count: int = Field(..., ge=0)
    etag: str = Field(..., min_length=1)
    created_at: datetime
    updated_at: datetime


class FactPageInfo(BaseModel):
    """Pagination envelope for GET /v1/facts."""

    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None = None
    has_more: bool


class FactListResponse(BaseModel):
    """GET /v1/facts response payload."""

    model_config = ConfigDict(extra="forbid")

    items: list[FactSummaryItem]
    page: FactPageInfo


class FactSupportItem(BaseModel):
    """Single non-raw FactSupport item returned by GET /v1/facts/{fact_id}/supports.

    Plan C (ADR-013 / REF-04): ``extracted_span`` is strictly omitted from the wire schema
    and always ``NULL`` in PostgreSQL.
    """

    model_config = ConfigDict(extra="forbid")

    support_id: str = Field(..., min_length=1)
    content_unit_id: str = Field(..., min_length=1)
    source_object_revision_id: str | None = None
    unit_type: ContentUnitType
    support_type: SupportType
    status: LinkStatus
    validator: SupportValidator
    validator_version: str = Field(..., min_length=1)
    support_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class FactSupportsResponse(BaseModel):
    """GET /v1/facts/{fact_id}/supports response payload."""

    model_config = ConfigDict(extra="forbid")

    fact: FactSummaryItem
    supports: list[FactSupportItem]


class FactInvalidationCounts(BaseModel):
    """Counts of downstream artifacts marked STALE or INVALIDATED atomically."""

    model_config = ConfigDict(extra="forbid")

    contributions: int = Field(default=0, ge=0)
    contribution_supports: int = Field(default=0, ge=0)
    claim_atoms: int = Field(default=0, ge=0)
    claim_atom_supports: int = Field(default=0, ge=0)
    claims: int = Field(default=0, ge=0)
    exports: int = Field(default=0, ge=0)


class ApproveFactRequest(BaseModel):
    """POST /v1/facts/{fact_id}/actions/approve request payload."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)


class ApproveFactResponse(BaseModel):
    """POST /v1/facts/{fact_id}/actions/approve response payload."""

    model_config = ConfigDict(extra="forbid")

    fact: FactSummaryItem
    idempotent_replay: bool = False


class RejectFactRequest(BaseModel):
    """POST /v1/facts/{fact_id}/actions/reject request payload."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _non_blank_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Rejection reason must not be blank.")
        return trimmed


class RejectFactResponse(BaseModel):
    """POST /v1/facts/{fact_id}/actions/reject response payload."""

    model_config = ConfigDict(extra="forbid")

    fact: FactSummaryItem
    invalidated_counts: FactInvalidationCounts
    idempotent_replay: bool = False


class EditFactRequest(BaseModel):
    """PATCH /v1/facts/{fact_id} request payload (ER-012 non-overwrite versioned edit)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    predicate: str | None = Field(default=None, min_length=1, max_length=500)
    object_text: str | None = Field(default=None, min_length=1, max_length=4000)
    normalized_value: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("predicate", "object_text")
    @classmethod
    def _strip_optional_non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Edited text field must not be blank.")
        return trimmed


class EditFactResponse(BaseModel):
    """PATCH /v1/facts/{fact_id} response payload returning the newly versioned CANDIDATE row."""

    model_config = ConfigDict(extra="forbid")

    fact: FactSummaryItem
    superseded_fact_id: str = Field(..., min_length=1)
    superseded_version: int = Field(..., ge=1)
    invalidated_counts: FactInvalidationCounts
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class EventFactPipelineResult:
    """Aggregate result returned by PostgresFactService.extract_and_validate_event_facts."""

    event_id: str
    extractor_run_ids: tuple[str, ...]
    validator_run_ids: tuple[str, ...]
    extraction_results: tuple[FactExtractionUnitResult, ...]
    validation_outcomes: tuple[FactValidationOutcome, ...]
    facts: tuple[FactSummaryItem, ...]


# =============================================================================
# HMAC-Bound Opaque Pagination Cursor Helpers (GET /v1/facts)
# =============================================================================
class FactCursorPayload(BaseModel):
    """Opaque cursor payload bound to workspace_id and normalized filter parameters."""

    model_config = ConfigDict(extra="forbid")

    v: Literal["cm-facts-cursor-v1"] = FACTS_CURSOR_VERSION
    workspace_id: str = Field(..., min_length=1)
    filter_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    sort_ts: datetime
    last_fact_id: str = Field(..., min_length=1)


def compute_facts_filter_hash(
    *,
    event_id: str | None = None,
    status: FactStatus | None = None,
    fact_type: FactType | None = None,
    include_superseded: bool = False,
) -> str:
    canonical = json.dumps(
        {
            "event_id": event_id,
            "fact_type": fact_type.value if fact_type is not None else None,
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


def encode_fact_cursor(
    payload: FactCursorPayload,
    *,
    secret: str = _DEFAULT_FACTS_CURSOR_SECRET,
) -> str:
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _sign_cursor_bytes(raw, secret=secret)
    return f"{_b64url_encode(raw)}.{sig}"


def decode_and_verify_fact_cursor(
    cursor: str,
    *,
    expected_workspace_id: str,
    expected_filter_hash: str,
    secret: str = _DEFAULT_FACTS_CURSOR_SECRET,
) -> FactCursorPayload:
    if "." not in cursor:
        raise FactError(
            status_code=400,
            code="INVALID_FACTS_CURSOR",
            message="Malformed facts pagination cursor.",
        )
    encoded_body, provided_sig = cursor.split(".", 1)
    try:
        raw = _b64url_decode(encoded_body)
        expected_sig = _sign_cursor_bytes(raw, secret=secret)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise ValueError("signature mismatch")
        parsed_raw = json.loads(raw.decode("utf-8"))
        payload = FactCursorPayload.model_validate(parsed_raw)
    except Exception as exc:  # noqa: BLE001
        raise FactError(
            status_code=400,
            code="INVALID_FACTS_CURSOR",
            message="Facts cursor signature or structure is invalid.",
        ) from exc

    mismatches: list[str] = []
    if payload.workspace_id != expected_workspace_id:
        mismatches.append("workspace_id")
    if payload.filter_hash != expected_filter_hash:
        mismatches.append("filters")
    if mismatches:
        raise FactError(
            status_code=400,
            code="INVALID_FACTS_CURSOR",
            message="Facts cursor binding mismatch for: " + ", ".join(mismatches),
            details={"mismatched_bindings": mismatches},
        )
    return payload


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_EXTRACTOR_MODEL",
    "DEFAULT_EXTRACTOR_PROMPT_VERSION",
    "DEFAULT_EXTRACTOR_VERSION",
    "DEFAULT_MAX_UNIT_BYTES",
    "DEFAULT_VALIDATOR_MODEL",
    "DEFAULT_VALIDATOR_PROMPT_VERSION",
    "DEFAULT_VALIDATOR_VERSION",
    "FACTS_CURSOR_VERSION",
    "FACTS_NO_STORE_HEADER",
    "FORBIDDEN_FACT_WRITE_STATUS_KEYS",
    "ApproveFactRequest",
    "ApproveFactResponse",
    "ContributionRecord",
    "EditFactRequest",
    "EditFactResponse",
    "EventFactPipelineResult",
    "FactAttributionScope",
    "FactCandidateDraft",
    "FactCausalBasis",
    "FactCompletionState",
    "FactCursorPayload",
    "FactEnvironmentScope",
    "FactError",
    "FactExtractionUnitResult",
    "FactInvalidationCounts",
    "FactListResponse",
    "FactPageInfo",
    "FactSemanticAttributes",
    "FactSummaryItem",
    "FactSupportItem",
    "FactSupportsResponse",
    "FactValidationOutcome",
    "MetricDetailsModel",
    "RejectFactRequest",
    "RejectFactResponse",
    "assert_no_forbidden_fact_write_keys",
    "compute_facts_filter_hash",
    "decode_and_verify_fact_cursor",
    "encode_fact_cursor",
    "extract_semantic_attributes",
    "format_fact_etag",
    "parse_fact_if_match_version",
    "resolve_expected_version",
    "utc_now",
    "verify_locator_in_unit_text",
]
