"""Pydantic contracts, cursor helpers, and domain errors for M3 Work Events (T-302 / T-303 / T-305)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from career_memory_api.schema_contracts.enums import (
    CompositeMembershipStatus,
    ContributionRole,
    EventConstraintOrigin,
    EventConstraintStatus,
    EventConstraintType,
    EventMembershipOrigin,
    PrAssociationState,
    RecordOrigin,
    ReviewActionType,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)

WORK_EVENTS_NO_STORE_HEADER = "private, no-store"
WORK_EVENTS_CURSOR_VERSION = "cm-work-events-cursor-v1"
_DEFAULT_WORK_EVENTS_CURSOR_SECRET = "career-memory-m3-work-events-cursor-key-v1"


class WorkEventPolicyState(StrEnum):
    """Real-time policy and deletion-fence projection for a WorkEvent."""

    ALLOWED = "ALLOWED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    DELETION_PENDING = "DELETION_PENDING"


class WorkEventAllowedAction(StrEnum):
    """Permitted user review actions surfaced by the WorkEvent Read API."""

    CONFIRM = "CONFIRM"
    EDIT = "EDIT"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    DISSOLVE = "DISSOLVE"
    REJECT = "REJECT"


class WorkEventError(Exception):
    """Structured HTTP error raised by the Work Events Read API boundary."""

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


class RelationSemanticsError(ValueError):
    """Domain error raised when a T-302 relation semantics invariant is violated."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class EventHistoryError(ValueError):
    """Domain error raised when a T-305 immutable history invariant is violated."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


_IF_MATCH_VERSION_PATTERN = re.compile(r'^"?v([1-9][0-9]*)"?$')
_ANCHOR_SCOPE_LIST_KEYS: tuple[str, ...] = (
    "file_paths",
    "diff_hunks",
    "hunk_ranges",
    "review_thread_ids",
    "content_unit_ids",
)


def format_work_event_etag(version: int) -> str:
    """Format the canonical quoted ETag header and body token for a WorkEvent version."""
    if version < 1:
        raise ValueError(f"WorkEvent version must be >= 1, got {version!r}.")
    return f'"v{version}"'


def parse_if_match_version(if_match: str | None) -> int | None:
    """Parse an optional ``If-Match`` header (``"v{version}"`` or ``v{version}``)."""
    if if_match is None:
        return None
    cleaned = if_match.strip()
    if not cleaned:
        return None
    match = _IF_MATCH_VERSION_PATTERN.match(cleaned)
    if match is None:
        raise WorkEventError(
            status_code=422,
            code="INVALID_IF_MATCH_HEADER",
            message='If-Match header must match format "v{version}" or v{version}.',
        )
    return int(match.group(1))


def resolve_idempotency_key(
    *,
    header_key: str | None = None,
    body_key: str | None = None,
) -> str:
    """Resolve and validate ``Idempotency-Key`` header and/or body ``idempotency_key``."""
    h_clean = header_key.strip() if isinstance(header_key, str) and header_key.strip() else None
    b_clean = body_key.strip() if isinstance(body_key, str) and body_key.strip() else None
    if h_clean is not None and b_clean is not None and h_clean != b_clean:
        raise WorkEventError(
            status_code=422,
            code="IDEMPOTENCY_KEY_MISMATCH",
            message="Idempotency-Key header and request body idempotency_key must match.",
        )
    resolved = h_clean or b_clean
    if resolved is None:
        raise WorkEventError(
            status_code=422,
            code="MISSING_IDEMPOTENCY_KEY",
            message="Idempotency-Key header or request body idempotency_key is required.",
        )
    if len(resolved) > 256:
        raise WorkEventError(
            status_code=422,
            code="INVALID_IDEMPOTENCY_KEY",
            message="idempotency_key must be at most 256 characters.",
        )
    return resolved


def has_non_empty_anchor_scope(anchor_scope: Mapping[str, Any] | None) -> bool:
    """Return True when ``anchor_scope`` defines at least one non-empty scope boundary."""
    if not isinstance(anchor_scope, Mapping) or not anchor_scope:
        return False
    for key in _ANCHOR_SCOPE_LIST_KEYS:
        val = anchor_scope.get(key)
        if isinstance(val, list) and any(isinstance(x, str) and x.strip() for x in val):
            return True
    return False


def compute_allowed_actions(
    *,
    anchor_mode: WorkEventAnchorMode,
    status: WorkEventStatus,
    policy_state: WorkEventPolicyState,
    is_active_top_level: bool,
    anchor_scope: Mapping[str, Any] | None = None,
) -> list[str]:
    """Compute deterministic allowed user actions from event mode, scope, status, and policy state."""
    if policy_state is not WorkEventPolicyState.ALLOWED:
        return []
    if not is_active_top_level or status in (
        WorkEventStatus.SUPERSEDED,
        WorkEventStatus.REJECTED,
    ):
        return []

    actions: list[WorkEventAllowedAction] = []
    if status in (WorkEventStatus.CANDIDATE, WorkEventStatus.STALE):
        actions.append(WorkEventAllowedAction.CONFIRM)

    actions.append(WorkEventAllowedAction.EDIT)

    if anchor_mode is WorkEventAnchorMode.USER_COMPOSITE:
        actions.append(WorkEventAllowedAction.DISSOLVE)
    elif anchor_mode is WorkEventAnchorMode.PR:
        actions.append(WorkEventAllowedAction.SPLIT)
        actions.append(WorkEventAllowedAction.MERGE)
    elif anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT:
        if not has_non_empty_anchor_scope(anchor_scope):
            actions.append(WorkEventAllowedAction.SPLIT)
        actions.append(WorkEventAllowedAction.MERGE)
    else:
        # PR_SEGMENT can be merged into a composite but not re-split
        actions.append(WorkEventAllowedAction.MERGE)

    actions.append(WorkEventAllowedAction.REJECT)
    return [action.value for action in actions]


# =============================================================================
# Summary & List API Models (GET /v1/work-events)
# =============================================================================
class DirectCommitSummary(BaseModel):
    """DIRECT_COMMIT confirmation badge metadata (T-301A / DELTA-EV-002)."""

    model_config = ConfigDict(extra="forbid")

    default_branch: str = Field(..., min_length=1)
    abbreviated_sha: str = Field(..., min_length=7, max_length=64)
    committed_at: datetime | None = None
    default_branch_reachable: bool
    pr_association_state: PrAssociationState
    requires_user_confirmation: bool


class CompositeSummary(BaseModel):
    """High-level summary for a USER_COMPOSITE WorkEvent."""

    model_config = ConfigDict(extra="forbid")

    member_count: int = Field(..., ge=0)
    merge_reason: str | None = None
    regeneration_status: str | None = None
    member_event_ids: list[str] = Field(default_factory=list)


class WorkEventSummaryItem(BaseModel):
    """Summary projection of a single WorkEvent returned by GET /v1/work-events."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    source_resource_id: str | None = None
    anchor_mode: WorkEventAnchorMode
    status: WorkEventStatus
    version: int = Field(..., ge=1)
    etag: str = Field(..., min_length=1)
    is_active_top_level: bool
    title: str = Field(..., min_length=1)
    problem: str | None = None
    action: str | None = None
    outcome: str | None = None
    created_by: RecordOrigin
    occurred_start_at: datetime | None = None
    occurred_end_at: datetime | None = None
    reviewed_at: datetime | None = None
    updated_at: datetime
    review_flags: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    evidence_count: int = Field(..., ge=0)
    context_count: int = Field(..., ge=0)
    direct_commit_badge: DirectCommitSummary | None = None
    composite_summary: CompositeSummary | None = None
    policy_state: WorkEventPolicyState


class WorkEventPageInfo(BaseModel):
    """Opaque cursor pagination metadata."""

    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None = None
    has_more: bool


class WorkEventStatusCounts(BaseModel):
    """Active top-level WorkEvent counts partitioned by active status."""

    model_config = ConfigDict(extra="forbid")

    CANDIDATE: int = Field(..., ge=0)
    CONFIRMED: int = Field(..., ge=0)
    STALE: int = Field(..., ge=0)


class WorkEventCounts(BaseModel):
    """Active top-level WorkEvent aggregate counters (zero double-counting)."""

    model_config = ConfigDict(extra="forbid")

    active_top_level_total: int = Field(..., ge=0)
    by_status: WorkEventStatusCounts


class WorkEventListResponse(BaseModel):
    """GET /v1/work-events response payload."""

    model_config = ConfigDict(extra="forbid")

    items: list[WorkEventSummaryItem]
    page: WorkEventPageInfo
    counts: WorkEventCounts


# =============================================================================
# Detail, Relations, Constraints & History Models (GET /v1/work-events/{id})
# =============================================================================
class EventSourceRelationItem(BaseModel):
    """Single active WorkEvent <-> SourceObject relation (separated by class)."""

    model_config = ConfigDict(extra="forbid")

    relation_id: str = Field(..., min_length=1)
    source_object_id: str = Field(..., min_length=1)
    object_type: SourceObjectType
    provider_object_id: str = Field(..., min_length=1)
    relation_type: str = Field(..., min_length=1)
    origin: EventMembershipOrigin
    rationale_code: str | None = None
    created_at: datetime


class PairwiseEventConstraintItem(BaseModel):
    """Pairwise WorkEvent <-> WorkEvent constraint projected relative to one event."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(..., min_length=1)
    peer_event_id: str = Field(..., min_length=1)
    constraint_type: EventConstraintType
    origin: EventConstraintOrigin
    status: EventConstraintStatus
    rationale_code: str | None = None
    created_by_review_action_id: str | None = None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PairwiseEventConstraintRecord:
    """Canonical stored row from public.event_constraints (left_event_id < right_event_id)."""

    constraint_id: str
    workspace_id: str
    left_event_id: str
    right_event_id: str
    constraint_type: EventConstraintType
    origin: EventConstraintOrigin
    status: EventConstraintStatus
    rationale_code: str | None
    created_by_review_action_id: str | None
    constraint_metadata: dict[str, Any]
    created_at: datetime
    revoked_at: datetime | None = None


class CannotLinkSummary(BaseModel):
    """Combined source-level and pairwise event-level CANNOT_LINK constraints (never evidence)."""

    model_config = ConfigDict(extra="forbid")

    source_object_ids: list[str] = Field(default_factory=list)
    peer_event_ids: list[str] = Field(default_factory=list)
    constraints: list[PairwiseEventConstraintItem] = Field(default_factory=list)


class CannotLinkConflictDetail(BaseModel):
    """Merge-time conflict detail detected by check_merge_cannot_link_conflicts (T-302 / T-304B)."""

    model_config = ConfigDict(extra="forbid")

    conflict_kind: Literal["PAIRWISE_EVENT_CANNOT_LINK", "SOURCE_OBJECT_CANNOT_LINK"]
    left_event_id: str = Field(..., min_length=1)
    right_event_id: str = Field(..., min_length=1)
    source_object_id: str | None = None
    constraint_id: str | None = None
    origin: str | None = None
    rationale_code: str | None = None


class CompositeMemberSnapshotItem(BaseModel):
    """Member event reference paired with its immutable work_event_versions snapshot."""

    model_config = ConfigDict(extra="forbid")

    member_event_id: str = Field(..., min_length=1)
    member_event_version: int = Field(..., ge=1)
    ordinal: int = Field(..., ge=0)
    membership_status: CompositeMembershipStatus
    title: str | None = None
    anchor_mode: WorkEventAnchorMode | None = None
    status_at_version: WorkEventStatus | None = None
    current_status: WorkEventStatus | None = None
    occurred_start_at: datetime | None = None
    occurred_end_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)
    snapshot_available: bool = False


class CompositeDetail(BaseModel):
    """Full USER_COMPOSITE breakdown (or superseding composite link for a SUPERSEDED member)."""

    model_config = ConfigDict(extra="forbid")

    member_count: int = Field(..., ge=0)
    merge_reason: str | None = None
    regeneration_status: str | None = None
    superseded_by_composite_id: str | None = None
    members: list[CompositeMemberSnapshotItem] = Field(default_factory=list)


class DownstreamImpactSummary(BaseModel):
    """Downstream Fact/Claim/Export counts and live processing/policy status."""

    model_config = ConfigDict(extra="forbid")

    fact_count: int = Field(..., ge=0)
    claim_count: int = Field(..., ge=0)
    export_count: int = Field(..., ge=0)
    stale_fact_count: int = Field(..., ge=0)
    stale_claim_count: int = Field(..., ge=0)
    processing_status: str = Field(..., min_length=1)


class EventVersionSnapshotItem(BaseModel):
    """Immutable EventVersion snapshot row from public.work_event_versions (T-305)."""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    anchor_mode: WorkEventAnchorMode
    primary_anchor_source_object_id: str | None = None
    anchor_scope: dict[str, Any] | None = None
    title: str = Field(..., min_length=1)
    problem: str | None = None
    action: str | None = None
    outcome: str | None = None
    summary_metadata: dict[str, Any] = Field(default_factory=dict)
    status: WorkEventStatus
    created_by: RecordOrigin
    created_by_review_action_id: str | None = None
    membership_snapshot: dict[str, Any] = Field(default_factory=dict)
    occurred_start_at: datetime | None = None
    occurred_end_at: datetime | None = None
    created_at: datetime


class ReviewActionHistoryItem(BaseModel):
    """Append-only WorkEvent review action record from public.review_actions (T-305)."""

    model_config = ConfigDict(extra="forbid")

    review_action_id: str = Field(..., min_length=1)
    action: ReviewActionType
    actor_user_id: str | None = None
    origin: str = Field(..., min_length=1)
    from_version: int | None = None
    to_version: int | None = None
    reason: str | None = None
    idempotency_key: str = Field(..., min_length=1)
    action_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkEventDetailResponse(WorkEventSummaryItem):
    """GET /v1/work-events/{id} full response payload (active, SUPERSEDED, or REJECTED)."""

    model_config = ConfigDict(extra="forbid")

    primary_anchor_source_object_id: str | None = None
    anchor_scope: dict[str, Any] | None = None
    evidence_memberships: list[EventSourceRelationItem] = Field(default_factory=list)
    context_relations: list[EventSourceRelationItem] = Field(default_factory=list)
    cannot_link_constraints: CannotLinkSummary
    composite_details: CompositeDetail | None = None
    downstream_impact: DownstreamImpactSummary
    version_history: list[EventVersionSnapshotItem] = Field(default_factory=list)
    review_actions: list[ReviewActionHistoryItem] = Field(default_factory=list)


# =============================================================================
# Atomic Event Review & Curation Mutation Models (T-304 / T-304A / T-304B)
# =============================================================================
class ConfirmWorkEventRequest(BaseModel):
    """POST /v1/work-events/{id}/actions/confirm request payload (UI Keep)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    reason: str | None = None
    idempotency_key: str | None = None


class EditWorkEventRequest(BaseModel):
    """PATCH /v1/work-events/{id} request payload (creates new CANDIDATE version)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    title: str = Field(..., min_length=1)
    problem: str | None = None
    action: str | None = None
    outcome: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None


class RejectWorkEventRequest(BaseModel):
    """POST /v1/work-events/{id}/actions/reject request payload (UI Exclude)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    reason: str | None = None
    idempotency_key: str | None = None


class SplitAnchorScopeInput(BaseModel):
    """Non-raw scope boundaries for a single SPLIT partition (accepts diff_hunks & hunk_ranges)."""

    model_config = ConfigDict(extra="forbid")

    file_paths: list[str] = Field(default_factory=list)
    diff_hunks: list[str] = Field(default_factory=list)
    hunk_ranges: list[str] = Field(default_factory=list)
    review_thread_ids: list[str] = Field(default_factory=list)
    content_unit_ids: list[str] = Field(default_factory=list)


class SplitPartitionInput(BaseModel):
    """Single non-overlapping child partition specification for POST .../actions/split."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    problem: str | None = None
    action: str | None = None
    outcome: str | None = None
    source_object_ids: list[str] = Field(default_factory=list)
    anchor_scope: SplitAnchorScopeInput | None = None


class SplitWorkEventRequest(BaseModel):
    """POST /v1/work-events/{id}/actions/split request payload (T-304A)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    partitions: list[SplitPartitionInput] = Field(..., min_length=2)
    reason: str | None = None
    idempotency_key: str | None = None


class SplitPairwiseConstraintItem(BaseModel):
    """Canonical pairwise CANNOT_LINK constraint created between split sibling segments."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(..., min_length=1)
    left_event_id: str = Field(..., min_length=1)
    right_event_id: str = Field(..., min_length=1)
    constraint_type: EventConstraintType
    origin: EventConstraintOrigin
    status: EventConstraintStatus
    rationale_code: str | None = None
    created_by_review_action_id: str | None = None
    created_at: datetime


class SplitWorkEventResponse(BaseModel):
    """POST /v1/work-events/{id}/actions/split response payload (T-304A)."""

    model_config = ConfigDict(extra="forbid")

    superseded_parent: WorkEventDetailResponse
    created_segments: list[WorkEventDetailResponse]
    pairwise_constraints_created: list[SplitPairwiseConstraintItem] = Field(default_factory=list)


class MergeMemberInput(BaseModel):
    """Member event reference and optimistic concurrency expected_version for MERGE."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1)


class MergeWorkEventsRequest(BaseModel):
    """POST /v1/work-events/actions/merge request payload (T-304B)."""

    model_config = ConfigDict(extra="forbid")

    members: list[MergeMemberInput] = Field(..., min_length=2)
    title: str | None = None
    composite_title: str | None = None
    problem: str | None = None
    action: str | None = None
    outcome: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None


class AffectedDownstreamImpact(BaseModel):
    """Downstream member Fact/Claim/Export counts invalidated by MERGE."""

    model_config = ConfigDict(extra="forbid")

    fact_count: int = Field(..., ge=0)
    claim_count: int = Field(..., ge=0)
    export_count: int = Field(default=0, ge=0)


class MergeWorkEventsResponse(BaseModel):
    """POST /v1/work-events/actions/merge response payload (T-304B)."""

    model_config = ConfigDict(extra="forbid")

    composite_event: WorkEventDetailResponse
    superseded_member_ids: list[str]
    invalidated_downstream_count: int = Field(..., ge=0)
    affected_downstream_impact: AffectedDownstreamImpact
    regeneration_job_status: str = Field(..., min_length=1)


class DissolveWorkEventRequest(BaseModel):
    """POST /v1/work-events/{id}/actions/dissolve request payload (T-304B)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=1)
    reason: str | None = None
    idempotency_key: str | None = None


class StaleDownstreamImpact(BaseModel):
    """Downstream composite Fact/Claim/Export counts marked STALE by DISSOLVE."""

    model_config = ConfigDict(extra="forbid")

    stale_fact_count: int = Field(..., ge=0)
    stale_claim_count: int = Field(..., ge=0)
    stale_export_count: int = Field(default=0, ge=0)


class DissolveWorkEventResponse(BaseModel):
    """POST /v1/work-events/{id}/actions/dissolve response payload (T-304B)."""

    model_config = ConfigDict(extra="forbid")

    dissolved_composite: WorkEventDetailResponse
    superseded_composite: WorkEventDetailResponse
    restored_members: list[WorkEventDetailResponse]
    invalidated_downstream_count: int = Field(..., ge=0)
    stale_downstream_impact: StaleDownstreamImpact
    cleanup_job_status: str = Field(..., min_length=1)


# =============================================================================
# HMAC-Bound Opaque Pagination Cursor Helpers (GET /v1/work-events)
# =============================================================================
class WorkEventCursorPayload(BaseModel):
    """Opaque cursor payload bound to workspace_id and normalized filter parameters."""

    model_config = ConfigDict(extra="forbid")

    v: Literal["cm-work-events-cursor-v1"] = WORK_EVENTS_CURSOR_VERSION
    workspace_id: str = Field(..., min_length=1)
    filter_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    sort_ts: datetime
    last_event_id: str = Field(..., min_length=1)


def compute_work_events_filter_hash(
    *,
    status: WorkEventStatus | None = None,
    source_resource_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    role: ContributionRole | None = None,
) -> str:
    """Compute deterministic SHA-256 fingerprint of list filter parameters for cursor binding."""
    canonical = json.dumps(
        {
            "from": from_date.isoformat() if from_date is not None else None,
            "role": role.value if role is not None else None,
            "source_resource_id": source_resource_id,
            "status": status.value if status is not None else None,
            "to": to_date.isoformat() if to_date is not None else None,
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


def encode_work_event_cursor(
    payload: WorkEventCursorPayload,
    *,
    secret: str = _DEFAULT_WORK_EVENTS_CURSOR_SECRET,
) -> str:
    """Encode an HMAC-SHA256 signed opaque cursor for GET /v1/work-events."""
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _sign_cursor_bytes(raw, secret=secret)
    return f"{_b64url_encode(raw)}.{sig}"


def decode_and_verify_work_event_cursor(
    cursor: str,
    *,
    expected_workspace_id: str,
    expected_filter_hash: str,
    secret: str = _DEFAULT_WORK_EVENTS_CURSOR_SECRET,
) -> WorkEventCursorPayload:
    """Decode and strictly verify cursor signature, workspace binding, and filter binding."""
    if "." not in cursor:
        raise WorkEventError(
            status_code=400,
            code="INVALID_WORK_EVENTS_CURSOR",
            message="Malformed work-events pagination cursor.",
        )
    encoded_body, provided_sig = cursor.split(".", 1)
    try:
        raw = _b64url_decode(encoded_body)
        expected_sig = _sign_cursor_bytes(raw, secret=secret)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise ValueError("signature mismatch")
        parsed_raw = json.loads(raw.decode("utf-8"))
        payload = WorkEventCursorPayload.model_validate(parsed_raw)
    except Exception as exc:  # noqa: BLE001
        raise WorkEventError(
            status_code=400,
            code="INVALID_WORK_EVENTS_CURSOR",
            message="Work-events cursor signature or structure is invalid.",
        ) from exc

    mismatches: list[str] = []
    if payload.workspace_id != expected_workspace_id:
        mismatches.append("workspace_id")
    if payload.filter_hash != expected_filter_hash:
        mismatches.append("filters")
    if mismatches:
        raise WorkEventError(
            status_code=400,
            code="INVALID_WORK_EVENTS_CURSOR",
            message="Work-events cursor binding mismatch for: " + ", ".join(mismatches),
            details={"mismatched_bindings": mismatches},
        )
    return payload


__all__ = [
    "WORK_EVENTS_CURSOR_VERSION",
    "WORK_EVENTS_NO_STORE_HEADER",
    "AffectedDownstreamImpact",
    "CannotLinkConflictDetail",
    "CannotLinkSummary",
    "CompositeDetail",
    "CompositeMemberSnapshotItem",
    "CompositeSummary",
    "ConfirmWorkEventRequest",
    "DirectCommitSummary",
    "DissolveWorkEventRequest",
    "DissolveWorkEventResponse",
    "DownstreamImpactSummary",
    "EditWorkEventRequest",
    "EventHistoryError",
    "EventSourceRelationItem",
    "EventVersionSnapshotItem",
    "MergeMemberInput",
    "MergeWorkEventsRequest",
    "MergeWorkEventsResponse",
    "PairwiseEventConstraintItem",
    "PairwiseEventConstraintRecord",
    "RejectWorkEventRequest",
    "RelationSemanticsError",
    "ReviewActionHistoryItem",
    "SplitAnchorScopeInput",
    "SplitPairwiseConstraintItem",
    "SplitPartitionInput",
    "SplitWorkEventRequest",
    "SplitWorkEventResponse",
    "StaleDownstreamImpact",
    "WorkEventAllowedAction",
    "WorkEventCounts",
    "WorkEventCursorPayload",
    "WorkEventDetailResponse",
    "WorkEventError",
    "WorkEventListResponse",
    "WorkEventPageInfo",
    "WorkEventPolicyState",
    "WorkEventStatusCounts",
    "WorkEventSummaryItem",
    "compute_allowed_actions",
    "compute_work_events_filter_hash",
    "decode_and_verify_work_event_cursor",
    "encode_work_event_cursor",
    "format_work_event_etag",
    "has_non_empty_anchor_scope",
    "parse_if_match_version",
    "resolve_idempotency_key",
]
