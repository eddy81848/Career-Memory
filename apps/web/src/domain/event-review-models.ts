/**
 * Domain models and wire DTOs for M3 Event Review & Curation Inbox
 * (Product & UX Spec §5, MVP API §5, Direct Commit & USER_COMPOSITE §10,
 * T-304B UI Handoff, ER-004).
 */

export type AnchorMode = "PR" | "PR_SEGMENT" | "DIRECT_COMMIT" | "USER_COMPOSITE";

export type WorkEventStatus =
  | "CANDIDATE"
  | "CONFIRMED"
  | "REJECTED"
  | "STALE"
  | "SUPERSEDED";

export type WorkEventCreatedBy = "RULE" | "USER";

/**
 * 7 deterministic review-assist flag codes aligned 1:1 with backend
 * `career_memory_api.event_candidates.models.ReviewFlagCode`.
 */
export type ReviewFlagCode =
  | "AMBIGUOUS_AUTHOR_IDENTITY"
  | "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"
  | "GENERIC_TITLE_OR_SUMMARY"
  | "MIXED_PURPOSE_SIGNALS"
  | "MULTIPLE_LINKED_ISSUES"
  | "PARTIAL_SOURCE_SKIPPED"
  | "REVIEW_REQUIRED_MULTI_PARENT";

export type AllowedEventAction =
  | "CONFIRM"
  | "EDIT"
  | "REJECT"
  | "SPLIT"
  | "MERGE"
  | "DISSOLVE"
  | "REOPEN";

export type PolicyBlockStatus =
  | "ACTIVE"
  | "POLICY_BLOCKED"
  | "DELETION_PENDING"
  | "SOURCE_ACCOUNT_REVOKED";

export interface EventPolicyState {
  status: PolicyBlockStatus;
  reason_code?: string | null;
  message?: string | null;
  raw_view_blocked?: boolean;
  external_ai_blocked?: boolean;
  export_blocked?: boolean;
}

export interface DirectCommitBadgeDto {
  badge_code: "DIRECT_DEFAULT_BRANCH_NO_PR";
  label: string;
  default_branch: string;
  abbreviated_sha: string;
  committed_at: string;
  pr_association_state: "NO_MATCH_OBSERVED" | "UNKNOWN";
  no_match_observed_means_definitely_no_pr: false;
  requires_user_confirmation: boolean;
}

export interface CompositeSummaryDto {
  member_count: number;
  badge_label: string;
  merge_reason?: string | null;
  regeneration_status?: "PENDING" | "RUNNING" | "COMPLETE" | "FAILED";
}

export interface WorkEventSummaryItem {
  event_id: string;
  workspace_id: string;
  source_resource_id: string;
  anchor_mode: AnchorMode;
  status: WorkEventStatus;
  version: number;
  etag: string;
  is_active_top_level: boolean;
  title: string;
  problem: string | null;
  action: string | null;
  outcome: string | null;
  created_by: WorkEventCreatedBy;
  occurred_start_at: string;
  occurred_end_at: string;
  reviewed_at: string | null;
  updated_at: string;
  review_flags: ReviewFlagCode[];
  allowed_actions: AllowedEventAction[];
  evidence_count: number;
  context_count: number;
  direct_commit_badge: DirectCommitBadgeDto | null;
  composite_summary: CompositeSummaryDto | null;
  policy_state: EventPolicyState;
}

export type EventInboxAvailabilityState =
  | "READY"
  | "EMPTY_NO_ELIGIBLE_EVENTS"
  | "ASSOCIATION_UNKNOWN_INCOMPLETE"
  | "POLICY_BLOCKED"
  | "DELETION_PENDING";

export interface WorkEventListResponse {
  items: WorkEventSummaryItem[];
  page: {
    next_cursor: string | null;
    has_more: boolean;
  };
  counts: {
    active_top_level_total: number;
    by_status: Record<string, number>;
  };
  inbox_state?: EventInboxAvailabilityState;
  inbox_notice?: string | null;
  default_branch?: string | null;
}

export interface EvidenceMembershipItem {
  membership_id: string;
  source_object_id: string;
  object_type: "PULL_REQUEST" | "COMMIT" | "REVIEW" | "COMMENT" | "FILE_PATCH" | "CONTENT_UNIT";
  provider_object_id: string;
  relation_class: "MUST_LINK";
  relation_type: string;
  origin: "SOURCE" | "RULE" | "USER";
  title: string;
  subtitle?: string | null;
  file_paths?: string[];
  hunk_range?: string | null;
  content_unit_id?: string | null;
  occurred_at?: string | null;
}

export interface ContextRelationItem {
  membership_id: string;
  source_object_id: string;
  object_type: "ISSUE" | "TICKET" | "LABEL_GROUP" | "COMMENT";
  provider_object_id: string;
  relation_class: "CONTEXT_ONLY";
  relation_type: string;
  origin: "SOURCE" | "RULE" | "USER";
  title: string;
  rationale: string;
}

export interface CannotLinkConstraintEntry {
  constraint_id: string;
  target_type: "SOURCE_OBJECT" | "PEER_EVENT";
  target_id: string;
  target_label: string;
  reason: string;
  created_at: string;
}

export interface CannotLinkConstraintsDto {
  source_object_ids: string[];
  peer_event_ids: string[];
  constraints: CannotLinkConstraintEntry[];
}

export interface CompositeMemberSnapshot {
  member_event_id: string;
  member_version: number;
  anchor_mode: "PR" | "PR_SEGMENT" | "DIRECT_COMMIT";
  title: string;
  status_at_merge: WorkEventStatus;
  current_status: WorkEventStatus;
  evidence_count: number;
  occurred_start_at: string;
  occurred_end_at: string;
  direct_commit_badge?: DirectCommitBadgeDto | null;
}

export interface CompositeDetailsDto {
  member_count: number;
  merge_reason: string;
  regeneration_status: "PENDING" | "RUNNING" | "COMPLETE" | "FAILED";
  superseded_by_composite_id: string | null;
  members: CompositeMemberSnapshot[];
}

export interface DownstreamImpactDto {
  fact_count: number;
  claim_count: number;
  export_count: number;
  stale_fact_count: number;
  stale_claim_count: number;
  processing_status:
    | "NOT_STARTED"
    | "RUNNING"
    | "COMPLETE"
    | "FAILED"
    | "BLOCKED_POLICY"
    | "BLOCKED_SOURCE";
  processing_message?: string | null;
}

export interface VersionHistoryEntry {
  version: number;
  status: WorkEventStatus;
  action: string;
  actor: "RULE" | "USER";
  reason: string | null;
  created_at: string;
}

export interface ReviewActionRecord {
  action_id: string;
  action: AllowedEventAction;
  actor: "RULE" | "USER";
  from_version: number | null;
  to_version: number | null;
  reason: string | null;
  created_at: string;
}

export interface AnchorScopeDto {
  kind: "PR" | "PR_SEGMENT" | "DIRECT_COMMIT" | "USER_COMPOSITE";
  file_paths?: string[];
  hunk_ranges?: string[];
  content_unit_ids?: string[];
}

export interface WorkEventDetailResponse extends WorkEventSummaryItem {
  primary_anchor_source_object_id: string | null;
  anchor_scope: AnchorScopeDto | null;
  evidence_memberships: EvidenceMembershipItem[];
  context_relations: ContextRelationItem[];
  cannot_link_constraints: CannotLinkConstraintsDto;
  composite_details: CompositeDetailsDto | null;
  downstream_impact: DownstreamImpactDto;
  version_history: VersionHistoryEntry[];
  review_actions: ReviewActionRecord[];
}

export interface SplitPartitionInput {
  title: string;
  source_object_ids: string[];
  anchor_scope?: {
    file_paths?: string[];
    hunk_ranges?: string[];
    content_unit_ids?: string[];
  };
}

export interface SplitWorkEventResponse {
  superseded_parent: WorkEventDetailResponse;
  created_segments: WorkEventDetailResponse[];
}

export interface MergeWorkEventsResponse {
  composite_event: WorkEventDetailResponse;
  superseded_member_ids: string[];
  affected_downstream_impact: {
    fact_count: number;
    claim_count: number;
    export_count: number;
  };
}

export interface DissolveWorkEventResponse {
  dissolved_composite: WorkEventDetailResponse;
  restored_members: WorkEventDetailResponse[];
  stale_downstream_impact: {
    stale_fact_count: number;
    stale_claim_count: number;
    stale_export_count: number;
  };
}

export interface EventVersionConflictDetails {
  current_version: number;
  current_status: WorkEventStatus;
  allowed_actions: AllowedEventAction[];
  event_id?: string;
}

export type EventReviewErrorKind =
  | "version_conflict"
  | "cannot_link_conflict"
  | "policy_blocked"
  | "deletion_pending"
  | "validation_error"
  | "unauthorized"
  | "not_found"
  | "network_error"
  | "request_rejected";

export interface EventReviewError {
  kind: EventReviewErrorKind;
  httpStatus?: number;
  code: string;
  message: string;
  requestId?: string;
  retryable?: boolean;
  conflictDetails?: EventVersionConflictDetails;
}

export type EventReviewScenarioId =
  | "default_inbox"
  | "regeneration_partial_failure"
  | "conflict_on_next_mutation"
  | "policy_blocked"
  | "deletion_pending"
  | "empty_no_eligible_events"
  | "association_unknown_incomplete";

export interface EventReviewScenarioMeta {
  id: EventReviewScenarioId;
  label: string;
  shortDescription: string;
}
