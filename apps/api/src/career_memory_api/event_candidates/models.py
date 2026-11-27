"""Domain models, enums, and value objects for M3 Event Candidate Builder (T-301 + T-301A)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from career_memory_api.schema_contracts.enums import (
    EventMembershipOrigin,
    EventMembershipRelationClass,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)

DIRECT_COMMIT_BADGE_CODE = "DIRECT_DEFAULT_BRANCH_NO_PR"
DIRECT_COMMIT_ASSOCIATION_NOTE = "NO_MATCH_OBSERVED_REQUIRES_CONFIRMATION"


class ReviewFlagCode(StrEnum):
    """Deterministic review-assist flag codes persisted in work_events.summary_metadata['review_flags']."""

    AMBIGUOUS_AUTHOR_IDENTITY = "AMBIGUOUS_AUTHOR_IDENTITY"
    DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED = "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"
    GENERIC_TITLE_OR_SUMMARY = "GENERIC_TITLE_OR_SUMMARY"
    MIXED_PURPOSE_SIGNALS = "MIXED_PURPOSE_SIGNALS"
    MULTIPLE_LINKED_ISSUES = "MULTIPLE_LINKED_ISSUES"
    PARTIAL_SOURCE_SKIPPED = "PARTIAL_SOURCE_SKIPPED"
    REVIEW_REQUIRED_MULTI_PARENT = "REVIEW_REQUIRED_MULTI_PARENT"


class CandidateBuildStatus(StrEnum):
    """Top-level outcome of PostgresEventCandidateBuilder execution."""

    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class CandidateBuildErrorCode(StrEnum):
    """Stable machine-readable domain error codes when candidate promotion is rejected or blocked."""

    SYNC_RUN_NOT_IMPORT_READY = "SYNC_RUN_NOT_IMPORT_READY"
    PR_ASSOCIATION_INCOMPLETE = "PR_ASSOCIATION_INCOMPLETE"
    DEFAULT_BRANCH_UNRESOLVED = "DEFAULT_BRANCH_UNRESOLVED"
    DELETION_FENCE_ACTIVE = "DELETION_FENCE_ACTIVE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    SOURCE_ACCOUNT_REVOKED = "SOURCE_ACCOUNT_REVOKED"


class EventRelationType(StrEnum):
    """Canonical event_memberships.relation_type values created by the candidate builder."""

    PRIMARY_ANCHOR = "PRIMARY_ANCHOR"
    PR_COMMIT = "PR_COMMIT"
    PR_REVIEW = "PR_REVIEW"
    PR_COMMENT = "PR_COMMENT"
    ISSUE_REFERENCE = "ISSUE_REFERENCE"


@dataclass(frozen=True, slots=True)
class CandidateBuildRequest:
    """Input request for PostgresEventCandidateBuilder.build_candidates."""

    workspace_id: str
    sync_run_id: str


@dataclass(frozen=True, slots=True)
class BuiltMembershipSummary:
    """Summary of one active source-object membership attached to a candidate event."""

    source_object_id: str
    object_type: SourceObjectType
    provider_object_id: str
    relation_class: EventMembershipRelationClass
    relation_type: str
    origin: EventMembershipOrigin
    rationale_code: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltCandidateSummary:
    """Summary of one PR or DIRECT_COMMIT work_event candidate evaluated by the builder."""

    event_id: str
    workspace_id: str
    primary_anchor_source_object_id: str
    anchor_mode: WorkEventAnchorMode
    status: WorkEventStatus
    version: int
    title: str
    created: bool
    enriched: bool
    preserved_user_decision: bool
    review_flags: tuple[ReviewFlagCode, ...] = ()
    memberships: tuple[BuiltMembershipSummary, ...] = ()
    preserved_cannot_link_object_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    """Structured result returned by PostgresEventCandidateBuilder."""

    status: CandidateBuildStatus
    workspace_id: str
    sync_run_id: str
    source_resource_id: str | None = None
    default_branch: str | None = None
    error_code: CandidateBuildErrorCode | None = None
    diagnostic_message: str | None = None
    candidates: tuple[BuiltCandidateSummary, ...] = ()
    pr_event_ids: tuple[str, ...] = ()
    direct_commit_event_ids: tuple[str, ...] = ()
    created_event_count: int = 0
    enriched_event_count: int = 0
    preserved_user_event_count: int = 0
    memberships_inserted_count: int = 0
    memberships_upgraded_count: int = 0
    preserved_cannot_link_count: int = 0
    excluded_non_default_commit_count: int = 0
    excluded_out_of_window_pr_commit_count: int = 0


__all__ = [
    "BuiltCandidateSummary",
    "BuiltMembershipSummary",
    "CandidateBuildErrorCode",
    "CandidateBuildRequest",
    "CandidateBuildResult",
    "CandidateBuildStatus",
    "DIRECT_COMMIT_ASSOCIATION_NOTE",
    "DIRECT_COMMIT_BADGE_CODE",
    "EventRelationType",
    "ReviewFlagCode",
]
