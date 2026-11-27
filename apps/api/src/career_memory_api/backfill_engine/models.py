"""Domain models, enums, and value objects for the M2-T203 Backfill Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from career_memory_api.schema_contracts.enums import PrAssociationState, SourceObjectType
from career_memory_api.sync_runs.models import SyncRunStatus


class BackfillStreamName(StrEnum):
    """Canonical initial-backfill pagination streams."""

    PRS = "PRS"
    PR_REVIEWS = "PR_REVIEWS"
    DEFAULT_BRANCH_COMMITS = "DEFAULT_BRANCH_COMMITS"
    COMMIT_PR_ASSOCIATIONS = "COMMIT_PR_ASSOCIATIONS"
    CONTEXT_ISSUES = "CONTEXT_ISSUES"


REQUIRED_BACKFILL_STREAMS: frozenset[BackfillStreamName] = frozenset(
    {
        BackfillStreamName.PRS,
        BackfillStreamName.PR_REVIEWS,
        BackfillStreamName.DEFAULT_BRANCH_COMMITS,
        BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
    }
)

OPTIONAL_BACKFILL_STREAMS: frozenset[BackfillStreamName] = frozenset(
    {BackfillStreamName.CONTEXT_ISSUES}
)


class BackfillFailureReasonCode(StrEnum):
    """Canonical failure_reason_code values aligned with 0040 & Snapshot Import §2-4."""

    CURSOR_CYCLE_DETECTED = "CURSOR_CYCLE_DETECTED"
    MALFORMED_OR_PARTIAL_PAGE = "MALFORMED_OR_PARTIAL_PAGE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    PLAN_DRIFT = "PLAN_DRIFT"
    INCOMPLETE_PR_ASSOCIATION = "INCOMPLETE_PR_ASSOCIATION"
    TRANSIENT_PROVIDER_ERROR = "TRANSIENT_PROVIDER_ERROR"
    POLICY_VISIBILITY_CHANGED = "POLICY_VISIBILITY_CHANGED"
    SOURCE_DELETED = "SOURCE_DELETED"


class SkipReasonCode(StrEnum):
    """Explicit item-level skip and exclusion codes persisted in sync_item_outcomes."""

    BINARY_FILE = "BINARY_FILE"
    OVERSIZED_FILE = "OVERSIZED_FILE"
    GENERATED_FILE = "GENERATED_FILE"
    OUT_OF_WINDOW_PR_LINKED = "OUT_OF_WINDOW_PR_LINKED"
    OPTIONAL_CONTEXT_UNAVAILABLE = "OPTIONAL_CONTEXT_UNAVAILABLE"
    UNSUPPORTED_OBJECT_TYPE = "UNSUPPORTED_OBJECT_TYPE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class ItemOutcomeKind(StrEnum):
    """Classification of non-promoted or excluded item outcomes."""

    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    EXCLUDED = "EXCLUDED"
    ASSOCIATION_UNKNOWN = "ASSOCIATION_UNKNOWN"


class AssociationMatchMechanism(StrEnum):
    """How a default-branch commit was proven to belong to a PR."""

    IN_WINDOW_PR_COMMIT = "IN_WINDOW_PR_COMMIT"
    PR_HEAD_SHA = "PR_HEAD_SHA"
    PR_MERGE_COMMIT_SHA = "PR_MERGE_COMMIT_SHA"
    SQUASH_COMMIT_SHA = "SQUASH_COMMIT_SHA"
    REBASE_COMMIT_SHA = "REBASE_COMMIT_SHA"
    PROVIDER_COMMIT_PR_API = "PROVIDER_COMMIT_PR_API"
    OUT_OF_WINDOW_PR_API = "OUT_OF_WINDOW_PR_API"


class PageCommitStatus(StrEnum):
    """Outcome of an atomic PostgreSQL page batch commit."""

    COMMITTED = "COMMITTED"
    STALE_CHECKPOINT_VERSION = "STALE_CHECKPOINT_VERSION"
    BLOCKED_BY_DELETION_FENCE = "BLOCKED_BY_DELETION_FENCE"
    BLOCKED_BY_RUN_STATE = "BLOCKED_BY_RUN_STATE"


class FinalizeOutcomeStatus(StrEnum):
    """Outcome of the final single-transaction SUCCESS promotion gate."""

    SUCCESS = "SUCCESS"
    ALREADY_SUCCEEDED = "ALREADY_SUCCEEDED"
    BLOCKED_PARTIAL = "BLOCKED_PARTIAL"
    BLOCKED_PARTIAL_WITH_LIMIT = "BLOCKED_PARTIAL_WITH_LIMIT"
    BLOCKED_BY_DELETION_FENCE = "BLOCKED_BY_DELETION_FENCE"
    BLOCKED_BY_POLICY_FENCE = "BLOCKED_BY_POLICY_FENCE"


DIRECT_COMMIT_ASSOCIATION_NOTE = "NO_MATCH_OBSERVED_REQUIRES_CONFIRMATION"


@dataclass(frozen=True, slots=True)
class BackfillLimits:
    """Bounded resource budgets enforced by PaginationSafeguard (T-203A)."""

    max_objects: int = 5_000
    max_requests: int = 500
    max_bytes: int = 50_000_000
    max_cursor_history_per_stream: int = 10_000

    def __post_init__(self) -> None:
        if self.max_objects < 1:
            raise ValueError("max_objects must be >= 1.")
        if self.max_requests < 1:
            raise ValueError("max_requests must be >= 1.")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1.")
        if self.max_cursor_history_per_stream < 1:
            raise ValueError("max_cursor_history_per_stream must be >= 1.")


@dataclass(frozen=True, slots=True)
class ItemOutcomeRecord:
    """Explicit per-item skip, exclusion, failure, or UNKNOWN association observation."""

    stream_name: BackfillStreamName
    object_type: str
    provider_object_id: str
    outcome_kind: ItemOutcomeKind
    reason_code: str
    affected_range: str | None = None
    retryable: bool = False
    outcome_metadata: dict[str, Any] = field(default_factory=dict)
    source_object_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedPageItem:
    """Plan-C-safe normalized object/revision ready for atomic DB persistence."""

    object_type: SourceObjectType
    provider_object_id: str
    content_hash: str
    normalized_metadata: dict[str, Any] = field(default_factory=dict)
    hash_policy_version: str = "v1"
    connector_version: str = "github-m2-v1"
    schema_version: str = "v1"
    provider_revision_id: str | None = None
    provider_updated_at: datetime | None = None
    is_tombstone: bool = False
    pr_association_state: PrAssociationState | None = None
    commit_sha: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderPageEnvelope:
    """Raw page response envelope validated by PaginationSafeguard before DB commit."""

    stream_name: BackfillStreamName
    page_number: int
    http_status: int
    request_cursor: str | None
    next_cursor: str | None
    items: tuple[dict[str, Any], ...] = ()
    declared_item_count: int | None = None
    is_truncated: bool = False
    has_next_page: bool = False
    payload_bytes: int = 0
    skipped_items: tuple[ItemOutcomeRecord, ...] = ()
    optional_access_error: str | None = None


@dataclass(frozen=True, slots=True)
class PageValidationDecision:
    """Result returned by PaginationSafeguard.evaluate_page."""

    accepted: bool
    page_fingerprint: str
    deduplicated_items: tuple[NormalizedPageItem, ...] = ()
    item_outcomes: tuple[ItemOutcomeRecord, ...] = ()
    terminal_status: SyncRunStatus | None = None
    failure_reason_code: BackfillFailureReasonCode | None = None
    retry_allowed: bool = False
    processed_range: str | None = None
    remaining_range: str | None = None
    unique_objects_total: int = 0
    requests_total: int = 0
    bytes_total: int = 0
    diagnostic_message: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryHeadSnapshot:
    """Repository branch/HEAD/enumeration snapshot captured at start and end of backfill."""

    default_branch: str
    head_sha: str
    start_head_reachable: bool = True
    pr_enumeration_digest: str = ""
    visibility: str = "PUBLIC"
    is_deleted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.default_branch, str) or not self.default_branch.strip():
            raise ValueError("default_branch must be a non-empty string.")
        if not isinstance(self.head_sha, str) or not self.head_sha.strip():
            raise ValueError("head_sha must be a non-empty string.")


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    """Metadata-only PR representation used for PR-first attribution and SHA cross-checking."""

    provider_object_id: str
    pr_number: int
    title: str
    merged: bool
    in_window: bool
    base_branch: str
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    squash_commit_sha: str | None = None
    rebase_commit_shas: tuple[str, ...] = ()
    commit_shas: tuple[str, ...] = ()
    merged_at: datetime | None = None
    created_at: datetime | None = None
    review_provider_object_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DefaultBranchCommitRecord:
    """Metadata-only commit observed on the repository default branch."""

    provider_object_id: str
    commit_sha: str
    title: str
    authored_at: datetime | None = None
    committed_at: datetime | None = None
    reachable_at_start: bool = True
    reachable_at_end: bool = True
    parent_shas: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitPrLookupObservation:
    """Provider commit-to-PR association API lookup result for a single commit SHA."""

    commit_sha: str
    lookup_succeeded: bool
    associated_pr_ids: tuple[str, ...] = ()
    associated_pr_in_window: dict[str, bool] = field(default_factory=dict)
    error_code: str | None = None
    is_ambiguous: bool = False
    has_missing_fields: bool = False
    is_conflicting: bool = False


@dataclass(frozen=True, slots=True)
class CommitAssociationDecision:
    """Three-state (MATCHED_PR / NO_MATCH_OBSERVED / UNKNOWN) decision for one commit."""

    commit_provider_object_id: str
    commit_sha: str
    default_branch: str
    state: PrAssociationState
    matched_pr_provider_id: str | None = None
    matched_pr_in_window: bool | None = None
    match_mechanism: AssociationMatchMechanism | None = None
    unknown_reason_code: str | None = None
    direct_commit_candidate: bool = False
    direct_commit_badge_metadata: dict[str, Any] | None = None
    exclusion_outcome: ItemOutcomeRecord | None = None
    reachable_at_start: bool = True
    reachable_at_end: bool = True
    lookup_completed: bool = False


@dataclass(frozen=True, slots=True)
class PlanIntegrityEvaluation:
    """Start-vs-end repository plan drift verification result."""

    drift_detected: bool
    drift_reasons: tuple[str, ...] = ()
    failure_reason_code: BackfillFailureReasonCode | None = None
    retry_allowed: bool = False


@dataclass(frozen=True, slots=True)
class AssociationBatchResult:
    """Aggregate T-203B classification result for an initial backfill run."""

    plan_integrity: PlanIntegrityEvaluation
    decisions: tuple[CommitAssociationDecision, ...]
    eligible_pr_ids: tuple[str, ...]
    direct_commit_shas: tuple[str, ...]
    out_of_window_excluded_shas: tuple[str, ...]
    unknown_commit_shas: tuple[str, ...]
    item_outcomes: tuple[ItemOutcomeRecord, ...]
    can_finalize_success: bool
    blocking_failure_reason: BackfillFailureReasonCode | None = None


@dataclass(frozen=True, slots=True)
class StreamCheckpointSnapshot:
    """Current persisted checkpoint state for a single (sync_run_id, stream_name)."""

    stream_name: BackfillStreamName
    stream_status: SyncRunStatus
    last_committed_page: int | None
    unresolved_failures: int
    checkpoint_version: int
    current_cursor: str | None
    cursor_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PageCommitRequest:
    """Input payload for PostgresBackfillPageCommitter.commit_page_batch."""

    workspace_id: str
    sync_run_id: str
    source_resource_id: str
    stream_name: BackfillStreamName
    page_number: int
    expected_checkpoint_version: int
    expected_cursor: str | None
    next_cursor: str | None
    page_fingerprint: str
    items: tuple[NormalizedPageItem, ...] = ()
    item_outcomes: tuple[ItemOutcomeRecord, ...] = ()
    commit_associations: tuple[CommitAssociationDecision, ...] = ()
    stream_completed: bool = False
    unresolved_failures: int = 0
    cursor_metadata_extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PageCommitResult:
    """Result of PostgresBackfillPageCommitter.commit_page_batch."""

    status: PageCommitStatus
    stream_name: BackfillStreamName
    checkpoint_version: int
    last_committed_page: int | None
    current_cursor: str | None
    objects_upserted: int = 0
    revisions_inserted: int = 0
    outcomes_recorded: int = 0
    associations_recorded: int = 0
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizationPrCandidate:
    """Eligible merged PR candidate promoted into a RULE work_event on SUCCESS."""

    provider_object_id: str
    title: str
    occurred_start_at: datetime | None = None
    occurred_end_at: datetime | None = None
    member_commit_provider_ids: tuple[str, ...] = ()
    member_review_provider_ids: tuple[str, ...] = ()
    summary_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FinalizationDirectCommitCandidate:
    """NO_MATCH_OBSERVED direct commit promoted 1:1 into a RULE work_event on SUCCESS."""

    provider_object_id: str
    commit_sha: str
    default_branch: str
    title: str
    occurred_at: datetime | None = None
    summary_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FinalizeRunRequest:
    """Input payload for PostgresBackfillPageCommitter.finalize_import_run."""

    workspace_id: str
    sync_run_id: str
    source_resource_id: str
    start_snapshot: RepositoryHeadSnapshot
    end_snapshot: RepositoryHeadSnapshot
    required_streams: frozenset[BackfillStreamName] = REQUIRED_BACKFILL_STREAMS
    pr_candidates: tuple[FinalizationPrCandidate, ...] = ()
    direct_commit_candidates: tuple[FinalizationDirectCommitCandidate, ...] = ()
    expected_visibility: str | None = None
    processed_range: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizeRunResult:
    """Outcome of the atomic finalization and promotion transaction."""

    outcome: FinalizeOutcomeStatus
    sync_run_status: SyncRunStatus
    import_ready: bool
    import_ready_outbox_id: str | None = None
    promoted_pr_event_ids: tuple[str, ...] = ()
    promoted_direct_commit_event_ids: tuple[str, ...] = ()
    failure_reason_code: str | None = None
    source_drift: bool = False
    retry_allowed: bool = False
    diagnostic_message: str | None = None
