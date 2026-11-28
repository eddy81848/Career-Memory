import type {
  ConfirmWorkEventInput,
  DissolveWorkEventInput,
  EditWorkEventInput,
  EventReviewAdapter,
  EventReviewResponseMeta,
  EventReviewResult,
  ListWorkEventsQuery,
  MergeWorkEventsInput,
  RejectWorkEventInput,
  SplitWorkEventInput,
} from "@/adapters/event-review-adapter";
import {
  generateUuidV4,
  parseOptionalJsonBody,
} from "@/adapters/synthetic-http-transport";
import type {
  AllowedEventAction,
  AnchorMode,
  AnchorScopeDto,
  CannotLinkConstraintEntry,
  CannotLinkConstraintsDto,
  CompositeDetailsDto,
  CompositeMemberSnapshot,
  CompositeSummaryDto,
  ContextRelationItem,
  DirectCommitBadgeDto,
  DissolveWorkEventResponse,
  DownstreamImpactDto,
  EventInboxAvailabilityState,
  EventPolicyState,
  EventReviewError,
  EvidenceMembershipItem,
  MergeWorkEventsResponse,
  ReviewActionRecord,
  ReviewFlagCode,
  SplitWorkEventResponse,
  VersionHistoryEntry,
  WorkEventDetailResponse,
  WorkEventListResponse,
  WorkEventStatus,
  WorkEventSummaryItem,
} from "@/domain/event-review-models";

/**
 * Real `fetch`-based HTTP transport implementing the M3 Event Review wire contract
 * (`T-303` / `T-304` / `ER-004`).
 *
 * Guarantees:
 * - Sends `If-Match: "v{expected_version}"` and `Idempotency-Key` on single-event mutations
 *   and `Idempotency-Key` + member `expected_version` array on merge.
 * - Maps Keep (확정) strictly to `POST /v1/work-events/{id}/actions/confirm`.
 * - Losslessly normalizes responses from BOTH synthetic contract stubs and the live Python
 *   `career_memory_api.work_events` FastAPI router into frontend view models.
 * - NEVER auto-retries or auto-POSTs on `409 EVENT_VERSION_CONFLICT` or network errors.
 * - Safely parses optional/empty error bodies without throwing JSON syntax errors.
 */

export interface EventReviewHttpTransportOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  keyGenerator?: () => string;
  defaultHeaders?: Record<string, string>;
}

interface RawApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: {
      current_version?: number;
      current_status?: WorkEventStatus;
      allowed_actions?: AllowedEventAction[];
      event_id?: string;
    };
  };
}

function buildVersionEtag(version: number): string {
  return `"v${version}"`;
}

function extractMeta(res: Response): EventReviewResponseMeta {
  return {
    httpStatus: res.status,
    etag: res.headers.get("etag"),
    cacheControl: res.headers.get("cache-control"),
    requestId: res.headers.get("x-request-id"),
  };
}

// =============================================================================
// Lossless Wire Normalization Bridge (Stub & Live career_memory_api Backend)
// =============================================================================
type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

export function normalizePolicyState(raw: unknown): EventPolicyState {
  const obj = asRecord(raw);
  if (obj && typeof obj.status === "string") {
    return {
      status: obj.status as EventPolicyState["status"],
      reason_code: (obj.reason_code as string | null | undefined) ?? null,
      message: (obj.message as string | null | undefined) ?? null,
      raw_view_blocked: Boolean(obj.raw_view_blocked),
      external_ai_blocked: Boolean(obj.external_ai_blocked),
      export_blocked: Boolean(obj.export_blocked),
    };
  }
  if (raw === "POLICY_BLOCKED") {
    return {
      status: "POLICY_BLOCKED",
      reason_code: "SOURCE_POLICY_BLOCKED",
      message:
        "공개 정책 차단(POLICY_BLOCKED)으로 인해 이 Event를 수정할 수 없습니다.",
      raw_view_blocked: true,
      external_ai_blocked: true,
      export_blocked: true,
    };
  }
  if (raw === "DELETION_PENDING") {
    return {
      status: "DELETION_PENDING",
      reason_code: "DELETION_PENDING",
      message:
        "삭제 펜스(DELETION_PENDING)가 활성화되어 변경이 차단되었습니다.",
      raw_view_blocked: true,
      external_ai_blocked: true,
      export_blocked: true,
    };
  }
  return {
    status: "ACTIVE",
    reason_code: null,
    message: null,
    raw_view_blocked: false,
    external_ai_blocked: false,
    export_blocked: false,
  };
}

function normalizeDirectCommitBadge(raw: unknown): DirectCommitBadgeDto | null {
  const obj = asRecord(raw);
  if (!obj) return null;
  const rawAssoc = obj.pr_association_state;
  const assocState: DirectCommitBadgeDto["pr_association_state"] =
    rawAssoc === "UNKNOWN" ? "UNKNOWN" : "NO_MATCH_OBSERVED";
  return {
    badge_code: "DIRECT_DEFAULT_BRANCH_NO_PR",
    label:
      typeof obj.label === "string" && obj.label.trim()
        ? obj.label
        : "PR 없음 · default branch 직접 반영",
    default_branch:
      typeof obj.default_branch === "string" ? obj.default_branch : "main",
    abbreviated_sha:
      typeof obj.abbreviated_sha === "string" ? obj.abbreviated_sha : "0000000",
    committed_at:
      typeof obj.committed_at === "string" ? obj.committed_at : "",
    pr_association_state: assocState,
    no_match_observed_means_definitely_no_pr: false,
    requires_user_confirmation:
      typeof obj.requires_user_confirmation === "boolean"
        ? obj.requires_user_confirmation
        : true,
  };
}

function normalizeRegenStatus(
  raw: unknown
): "PENDING" | "RUNNING" | "COMPLETE" | "FAILED" {
  if (
    raw === "PENDING" ||
    raw === "RUNNING" ||
    raw === "COMPLETE" ||
    raw === "FAILED"
  ) {
    return raw;
  }
  if (raw === "READY") return "COMPLETE";
  return "PENDING";
}

function normalizeCompositeSummary(raw: unknown): CompositeSummaryDto | null {
  const obj = asRecord(raw);
  if (!obj) return null;
  const memberCount =
    typeof obj.member_count === "number" ? obj.member_count : 0;
  return {
    member_count: memberCount,
    badge_label:
      typeof obj.badge_label === "string" && obj.badge_label.trim()
        ? obj.badge_label
        : `${memberCount}개 Event를 사용자가 병합`,
    merge_reason:
      typeof obj.merge_reason === "string" ? obj.merge_reason : null,
    regeneration_status:
      obj.regeneration_status !== undefined && obj.regeneration_status !== null
        ? normalizeRegenStatus(obj.regeneration_status)
        : undefined,
  };
}

function normalizeEvidenceObjectType(
  raw: unknown
): EvidenceMembershipItem["object_type"] {
  if (raw === "PR" || raw === "PULL_REQUEST") return "PULL_REQUEST";
  if (
    raw === "COMMIT" ||
    raw === "REVIEW" ||
    raw === "COMMENT" ||
    raw === "FILE_PATCH" ||
    raw === "CONTENT_UNIT"
  ) {
    return raw;
  }
  return "PULL_REQUEST";
}

function normalizeEvidenceMembership(raw: unknown): EvidenceMembershipItem {
  const obj = asRecord(raw) ?? {};
  const objectType = normalizeEvidenceObjectType(obj.object_type);
  const providerObjId =
    typeof obj.provider_object_id === "string" ? obj.provider_object_id : "";
  const relationType =
    typeof obj.relation_type === "string" ? obj.relation_type : "PRIMARY_ANCHOR";
  const origin: EvidenceMembershipItem["origin"] =
    obj.origin === "SOURCE" || obj.origin === "RULE" || obj.origin === "USER"
      ? obj.origin
      : "SOURCE";
  return {
    membership_id:
      typeof obj.membership_id === "string"
        ? obj.membership_id
        : typeof obj.relation_id === "string"
          ? obj.relation_id
          : "",
    source_object_id:
      typeof obj.source_object_id === "string" ? obj.source_object_id : "",
    object_type: objectType,
    provider_object_id: providerObjId,
    relation_class: "MUST_LINK",
    relation_type: relationType,
    origin,
    title:
      typeof obj.title === "string" && obj.title.trim()
        ? obj.title
        : `${objectType} ${providerObjId}`.trim(),
    subtitle:
      typeof obj.subtitle === "string"
        ? obj.subtitle
        : typeof obj.rationale_code === "string"
          ? obj.rationale_code
          : null,
    file_paths: Array.isArray(obj.file_paths)
      ? asStringArray(obj.file_paths)
      : undefined,
    hunk_range:
      typeof obj.hunk_range === "string" ? obj.hunk_range : null,
    content_unit_id:
      typeof obj.content_unit_id === "string" ? obj.content_unit_id : null,
    occurred_at:
      typeof obj.occurred_at === "string"
        ? obj.occurred_at
        : typeof obj.created_at === "string"
          ? obj.created_at
          : null,
  };
}

function normalizeContextRelation(raw: unknown): ContextRelationItem {
  const obj = asRecord(raw) ?? {};
  const rawType = obj.object_type;
  const objectType: ContextRelationItem["object_type"] =
    rawType === "ISSUE" ||
    rawType === "TICKET" ||
    rawType === "LABEL_GROUP" ||
    rawType === "COMMENT"
      ? rawType
      : "ISSUE";
  const providerObjId =
    typeof obj.provider_object_id === "string" ? obj.provider_object_id : "";
  const relationType =
    typeof obj.relation_type === "string"
      ? obj.relation_type
      : "ISSUE_REFERENCE";
  const origin: ContextRelationItem["origin"] =
    obj.origin === "SOURCE" || obj.origin === "RULE" || obj.origin === "USER"
      ? obj.origin
      : "SOURCE";
  return {
    membership_id:
      typeof obj.membership_id === "string"
        ? obj.membership_id
        : typeof obj.relation_id === "string"
          ? obj.relation_id
          : "",
    source_object_id:
      typeof obj.source_object_id === "string" ? obj.source_object_id : "",
    object_type: objectType,
    provider_object_id: providerObjId,
    relation_class: "CONTEXT_ONLY",
    relation_type: relationType,
    origin,
    title:
      typeof obj.title === "string" && obj.title.trim()
        ? obj.title
        : `${objectType} ${providerObjId}`.trim(),
    rationale:
      typeof obj.rationale === "string" && obj.rationale.trim()
        ? obj.rationale
        : typeof obj.rationale_code === "string" && obj.rationale_code.trim()
          ? obj.rationale_code
          : relationType,
  };
}

function normalizeCannotLinkConstraints(
  raw: unknown
): CannotLinkConstraintsDto {
  const obj = asRecord(raw) ?? {};
  const rawConstraints = Array.isArray(obj.constraints) ? obj.constraints : [];
  const constraints: CannotLinkConstraintEntry[] = rawConstraints.map(
    (entry) => {
      const e = asRecord(entry) ?? {};
      if (typeof e.target_type === "string" && typeof e.target_id === "string") {
        return {
          constraint_id:
            typeof e.constraint_id === "string" ? e.constraint_id : "",
          target_type:
            e.target_type === "SOURCE_OBJECT" ? "SOURCE_OBJECT" : "PEER_EVENT",
          target_id: e.target_id,
          target_label:
            typeof e.target_label === "string" ? e.target_label : e.target_id,
          reason: typeof e.reason === "string" ? e.reason : "CANNOT_LINK",
          created_at: typeof e.created_at === "string" ? e.created_at : "",
        };
      }
      const peerId =
        typeof e.peer_event_id === "string" ? e.peer_event_id : "";
      const originStr = typeof e.origin === "string" ? e.origin : "USER";
      const rationale =
        typeof e.rationale_code === "string" && e.rationale_code.trim()
          ? e.rationale_code
          : `${originStr} CANNOT_LINK`;
      return {
        constraint_id:
          typeof e.constraint_id === "string" ? e.constraint_id : "",
        target_type: "PEER_EVENT",
        target_id: peerId,
        target_label: peerId,
        reason: rationale,
        created_at: typeof e.created_at === "string" ? e.created_at : "",
      };
    }
  );
  return {
    source_object_ids: asStringArray(obj.source_object_ids),
    peer_event_ids: asStringArray(obj.peer_event_ids),
    constraints,
  };
}

function normalizeCompositeDetails(raw: unknown): CompositeDetailsDto | null {
  const obj = asRecord(raw);
  if (!obj) return null;
  const rawMembers = Array.isArray(obj.members) ? obj.members : [];
  const members: CompositeMemberSnapshot[] = rawMembers.map((item) => {
    const m = asRecord(item) ?? {};
    const rawAnchor = m.anchor_mode;
    const anchorMode: CompositeMemberSnapshot["anchor_mode"] =
      rawAnchor === "PR" ||
      rawAnchor === "PR_SEGMENT" ||
      rawAnchor === "DIRECT_COMMIT"
        ? rawAnchor
        : "PR";
    const memberEventId =
      typeof m.member_event_id === "string" ? m.member_event_id : "";
    return {
      member_event_id: memberEventId,
      member_version:
        typeof m.member_version === "number"
          ? m.member_version
          : typeof m.member_event_version === "number"
            ? m.member_event_version
            : 1,
      anchor_mode: anchorMode,
      title:
        typeof m.title === "string" && m.title.trim() ? m.title : memberEventId,
      status_at_merge:
        (typeof m.status_at_merge === "string"
          ? m.status_at_merge
          : typeof m.status_at_version === "string"
            ? m.status_at_version
            : "CANDIDATE") as WorkEventStatus,
      current_status: (typeof m.current_status === "string"
        ? m.current_status
        : "SUPERSEDED") as WorkEventStatus,
      evidence_count:
        typeof m.evidence_count === "number" ? m.evidence_count : 0,
      occurred_start_at:
        typeof m.occurred_start_at === "string" ? m.occurred_start_at : "",
      occurred_end_at:
        typeof m.occurred_end_at === "string" ? m.occurred_end_at : "",
      direct_commit_badge: normalizeDirectCommitBadge(m.direct_commit_badge),
    };
  });
  return {
    member_count:
      typeof obj.member_count === "number" ? obj.member_count : members.length,
    merge_reason:
      typeof obj.merge_reason === "string" ? obj.merge_reason : "",
    regeneration_status: normalizeRegenStatus(obj.regeneration_status),
    superseded_by_composite_id:
      typeof obj.superseded_by_composite_id === "string"
        ? obj.superseded_by_composite_id
        : null,
    members,
  };
}

function normalizeDownstreamImpact(raw: unknown): DownstreamImpactDto {
  const obj = asRecord(raw) ?? {};
  const rawProc =
    typeof obj.processing_status === "string"
      ? obj.processing_status
      : "NOT_STARTED";
  let processingStatus: DownstreamImpactDto["processing_status"] =
    "NOT_STARTED";
  let processingMessage: string | null =
    typeof obj.processing_message === "string" ? obj.processing_message : null;

  if (
    rawProc === "NOT_STARTED" ||
    rawProc === "RUNNING" ||
    rawProc === "COMPLETE" ||
    rawProc === "FAILED" ||
    rawProc === "BLOCKED_POLICY" ||
    rawProc === "BLOCKED_SOURCE"
  ) {
    processingStatus = rawProc;
  } else if (rawProc === "READY") {
    processingStatus = "COMPLETE";
  } else if (rawProc === "POLICY_BLOCKED") {
    processingStatus = "BLOCKED_POLICY";
  } else if (rawProc === "DELETION_PENDING") {
    processingStatus = "BLOCKED_SOURCE";
  } else if (rawProc === "STALE_REGENERATION_REQUIRED") {
    processingStatus = "NOT_STARTED";
    processingMessage = processingMessage ?? "STALE_REGENERATION_REQUIRED";
  }

  return {
    fact_count: typeof obj.fact_count === "number" ? obj.fact_count : 0,
    claim_count: typeof obj.claim_count === "number" ? obj.claim_count : 0,
    export_count: typeof obj.export_count === "number" ? obj.export_count : 0,
    stale_fact_count:
      typeof obj.stale_fact_count === "number" ? obj.stale_fact_count : 0,
    stale_claim_count:
      typeof obj.stale_claim_count === "number" ? obj.stale_claim_count : 0,
    processing_status: processingStatus,
    processing_message: processingMessage,
  };
}

function normalizeVersionHistory(raw: unknown): VersionHistoryEntry[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const v = asRecord(item) ?? {};
    const version = typeof v.version === "number" ? v.version : 1;
    const status = (
      typeof v.status === "string" ? v.status : "CANDIDATE"
    ) as WorkEventStatus;
    const isBackendSnapshot = typeof v.version_id === "string";
    if (isBackendSnapshot) {
      const actor: "RULE" | "USER" =
        v.created_by === "USER" ? "USER" : "RULE";
      return {
        version,
        status,
        action: version === 1 ? "CREATE_CANDIDATE" : status,
        actor,
        reason: null,
        created_at: typeof v.created_at === "string" ? v.created_at : "",
      };
    }
    return {
      version,
      status,
      action: typeof v.action === "string" ? v.action : status,
      actor: v.actor === "USER" ? "USER" : "RULE",
      reason: typeof v.reason === "string" ? v.reason : null,
      created_at: typeof v.created_at === "string" ? v.created_at : "",
    };
  });
}

function normalizeReviewActions(raw: unknown): ReviewActionRecord[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const r = asRecord(item) ?? {};
    const rawActor = r.actor ?? r.origin;
    const actor: "RULE" | "USER" = rawActor === "RULE" ? "RULE" : "USER";
    return {
      action_id:
        typeof r.action_id === "string"
          ? r.action_id
          : typeof r.review_action_id === "string"
            ? r.review_action_id
            : "",
      action: (typeof r.action === "string"
        ? r.action
        : "CONFIRM") as AllowedEventAction,
      actor,
      from_version:
        typeof r.from_version === "number" ? r.from_version : null,
      to_version: typeof r.to_version === "number" ? r.to_version : null,
      reason: typeof r.reason === "string" ? r.reason : null,
      created_at: typeof r.created_at === "string" ? r.created_at : "",
    };
  });
}

function normalizeAnchorScope(
  raw: unknown,
  anchorMode: AnchorMode
): AnchorScopeDto | null {
  const obj = asRecord(raw);
  if (!obj) return null;
  const kind = (
    typeof obj.kind === "string" ? obj.kind : anchorMode
  ) as AnchorMode;
  const hunkRanges = Array.isArray(obj.hunk_ranges)
    ? asStringArray(obj.hunk_ranges)
    : Array.isArray(obj.diff_hunks)
      ? asStringArray(obj.diff_hunks)
      : undefined;
  return {
    kind,
    file_paths: Array.isArray(obj.file_paths)
      ? asStringArray(obj.file_paths)
      : undefined,
    hunk_ranges: hunkRanges,
    content_unit_ids: Array.isArray(obj.content_unit_ids)
      ? asStringArray(obj.content_unit_ids)
      : undefined,
  };
}

export function normalizeWorkEventSummary(
  raw: unknown,
  etagFallback?: string | null
): WorkEventSummaryItem {
  const obj = asRecord(raw) ?? {};
  const version = typeof obj.version === "number" ? obj.version : 1;
  const updatedAt =
    typeof obj.updated_at === "string" ? obj.updated_at : "";
  const etag =
    typeof obj.etag === "string" && obj.etag.trim()
      ? obj.etag
      : etagFallback && etagFallback.trim()
        ? etagFallback
        : buildVersionEtag(version);
  return {
    event_id: typeof obj.event_id === "string" ? obj.event_id : "",
    workspace_id: typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    source_resource_id:
      typeof obj.source_resource_id === "string"
        ? obj.source_resource_id
        : "",
    anchor_mode: (typeof obj.anchor_mode === "string"
      ? obj.anchor_mode
      : "PR") as AnchorMode,
    status: (typeof obj.status === "string"
      ? obj.status
      : "CANDIDATE") as WorkEventStatus,
    version,
    etag,
    is_active_top_level: Boolean(obj.is_active_top_level),
    title: typeof obj.title === "string" ? obj.title : "",
    problem: typeof obj.problem === "string" ? obj.problem : null,
    action: typeof obj.action === "string" ? obj.action : null,
    outcome: typeof obj.outcome === "string" ? obj.outcome : null,
    created_by: obj.created_by === "USER" ? "USER" : "RULE",
    occurred_start_at:
      typeof obj.occurred_start_at === "string"
        ? obj.occurred_start_at
        : updatedAt,
    occurred_end_at:
      typeof obj.occurred_end_at === "string" ? obj.occurred_end_at : updatedAt,
    reviewed_at: typeof obj.reviewed_at === "string" ? obj.reviewed_at : null,
    updated_at: updatedAt,
    review_flags: asStringArray(obj.review_flags) as ReviewFlagCode[],
    allowed_actions: asStringArray(obj.allowed_actions) as AllowedEventAction[],
    evidence_count:
      typeof obj.evidence_count === "number" ? obj.evidence_count : 0,
    context_count:
      typeof obj.context_count === "number" ? obj.context_count : 0,
    direct_commit_badge: normalizeDirectCommitBadge(obj.direct_commit_badge),
    composite_summary: normalizeCompositeSummary(obj.composite_summary),
    policy_state: normalizePolicyState(obj.policy_state),
  };
}

export function normalizeWorkEventDetail(
  raw: unknown,
  etagFallback?: string | null
): WorkEventDetailResponse {
  const obj = asRecord(raw) ?? {};
  const summary = normalizeWorkEventSummary(obj, etagFallback);
  const rawEvidence = Array.isArray(obj.evidence_memberships)
    ? obj.evidence_memberships
    : [];
  const rawContext = Array.isArray(obj.context_relations)
    ? obj.context_relations
    : [];
  return {
    ...summary,
    primary_anchor_source_object_id:
      typeof obj.primary_anchor_source_object_id === "string"
        ? obj.primary_anchor_source_object_id
        : null,
    anchor_scope: normalizeAnchorScope(obj.anchor_scope, summary.anchor_mode),
    evidence_memberships: rawEvidence.map(normalizeEvidenceMembership),
    context_relations: rawContext.map(normalizeContextRelation),
    cannot_link_constraints: normalizeCannotLinkConstraints(
      obj.cannot_link_constraints
    ),
    composite_details: normalizeCompositeDetails(obj.composite_details),
    downstream_impact: normalizeDownstreamImpact(obj.downstream_impact),
    version_history: normalizeVersionHistory(obj.version_history),
    review_actions: normalizeReviewActions(obj.review_actions),
  };
}

export function normalizeWorkEventListResponse(
  raw: unknown
): WorkEventListResponse {
  const obj = asRecord(raw) ?? {};
  const rawItems = Array.isArray(obj.items) ? obj.items : [];
  const items = rawItems.map((item) => normalizeWorkEventSummary(item));
  const rawPage = asRecord(obj.page) ?? {};
  const rawCounts = asRecord(obj.counts) ?? {};
  const rawByStatus = asRecord(rawCounts.by_status) ?? {};
  const byStatus: Record<string, number> = {};
  for (const [k, v] of Object.entries(rawByStatus)) {
    if (typeof v === "number") byStatus[k] = v;
  }

  let inboxState = obj.inbox_state as EventInboxAvailabilityState | undefined;
  if (!inboxState) {
    if (items.length === 0) {
      inboxState = "EMPTY_NO_ELIGIBLE_EVENTS";
    } else if (
      items.every((it) => it.policy_state.status === "DELETION_PENDING")
    ) {
      inboxState = "DELETION_PENDING";
    } else if (
      items.every((it) => it.policy_state.status === "POLICY_BLOCKED")
    ) {
      inboxState = "POLICY_BLOCKED";
    } else {
      inboxState = "READY";
    }
  }

  return {
    items,
    page: {
      next_cursor:
        typeof rawPage.next_cursor === "string" ? rawPage.next_cursor : null,
      has_more: Boolean(rawPage.has_more),
    },
    counts: {
      active_top_level_total:
        typeof rawCounts.active_top_level_total === "number"
          ? rawCounts.active_top_level_total
          : items.length,
      by_status: byStatus,
    },
    inbox_state: inboxState,
    inbox_notice:
      typeof obj.inbox_notice === "string" ? obj.inbox_notice : null,
    default_branch:
      typeof obj.default_branch === "string" ? obj.default_branch : null,
  };
}

export function normalizeSplitWorkEventResponse(
  raw: unknown
): SplitWorkEventResponse {
  const obj = asRecord(raw) ?? {};
  const rawSegments = Array.isArray(obj.created_segments)
    ? obj.created_segments
    : [];
  return {
    superseded_parent: normalizeWorkEventDetail(obj.superseded_parent),
    created_segments: rawSegments.map((s) => normalizeWorkEventDetail(s)),
  };
}

export function normalizeMergeWorkEventsResponse(
  raw: unknown
): MergeWorkEventsResponse {
  const obj = asRecord(raw) ?? {};
  const rawImpact = asRecord(obj.affected_downstream_impact) ?? {};
  return {
    composite_event: normalizeWorkEventDetail(obj.composite_event),
    superseded_member_ids: asStringArray(obj.superseded_member_ids),
    affected_downstream_impact: {
      fact_count:
        typeof rawImpact.fact_count === "number" ? rawImpact.fact_count : 0,
      claim_count:
        typeof rawImpact.claim_count === "number" ? rawImpact.claim_count : 0,
      export_count:
        typeof rawImpact.export_count === "number" ? rawImpact.export_count : 0,
    },
  };
}

export function normalizeDissolveWorkEventResponse(
  raw: unknown
): DissolveWorkEventResponse {
  const obj = asRecord(raw) ?? {};
  const rawRestored = Array.isArray(obj.restored_members)
    ? obj.restored_members
    : [];
  const rawImpact = asRecord(obj.stale_downstream_impact) ?? {};
  return {
    dissolved_composite: normalizeWorkEventDetail(
      obj.dissolved_composite ?? obj.superseded_composite
    ),
    restored_members: rawRestored.map((m) => normalizeWorkEventDetail(m)),
    stale_downstream_impact: {
      stale_fact_count:
        typeof rawImpact.stale_fact_count === "number"
          ? rawImpact.stale_fact_count
          : 0,
      stale_claim_count:
        typeof rawImpact.stale_claim_count === "number"
          ? rawImpact.stale_claim_count
          : 0,
      stale_export_count:
        typeof rawImpact.stale_export_count === "number"
          ? rawImpact.stale_export_count
          : 0,
    },
  };
}

function classifyErrorResponse(
  status: number,
  rawBody: string,
  meta: EventReviewResponseMeta
): EventReviewError {
  const parsed = parseOptionalJsonBody<RawApiErrorEnvelope>(rawBody);
  const envelope =
    !parsed.empty && !parsed.invalidJson ? parsed.value?.error : undefined;
  const code = envelope?.code ?? `HTTP_${status}`;
  const requestId = envelope?.request_id ?? meta.requestId ?? undefined;

  if (status === 409 && code === "EVENT_VERSION_CONFLICT") {
    const details = envelope?.details;
    const currentVersion =
      typeof details?.current_version === "number"
        ? details.current_version
        : 0;
    const currentStatus = details?.current_status ?? "CANDIDATE";
    const allowedActions = Array.isArray(details?.allowed_actions)
      ? details.allowed_actions
      : [];

    return {
      kind: "version_conflict",
      httpStatus: status,
      code: "EVENT_VERSION_CONFLICT",
      message:
        envelope?.message ??
        `다른 탭이나 작업에서 Event가 먼저 수정되어 최신 버전(v${currentVersion})과 충돌했습니다. 자동 덮어쓰기를 수행하지 않았으니 최신 상태를 다시 불러온 뒤 재적용하세요.`,
      requestId,
      retryable: false,
      conflictDetails: {
        current_version: currentVersion,
        current_status: currentStatus,
        allowed_actions: allowedActions,
        event_id: details?.event_id,
      },
    };
  }

  if (status === 409 && code === "CANNOT_LINK_CONFLICT") {
    return {
      kind: "cannot_link_conflict",
      httpStatus: status,
      code: "CANNOT_LINK_CONFLICT",
      message:
        envelope?.message ??
        "선택한 Event 사이에 CANNOT_LINK 제약이 있어 병합할 수 없습니다.",
      requestId,
      retryable: false,
    };
  }

  if (code === "POLICY_BLOCKED" || code === "SOURCE_POLICY_BLOCKED") {
    return {
      kind: "policy_blocked",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "공개 정책 차단(POLICY_BLOCKED)으로 인해 이 Event를 수정할 수 없습니다. 원본 삭제가 아니며 정책 재확인이 필요합니다.",
      requestId,
      retryable: false,
    };
  }

  if (code === "DELETION_PENDING" || code === "DELETION_FENCE_ACTIVE") {
    return {
      kind: "deletion_pending",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "삭제 펜스(DELETION_PENDING)가 활성화되어 변경 명령이 즉시 차단되었습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 401) {
    return {
      kind: "unauthorized",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "인증 세션이 유효하지 않아 요청을 처리하지 못했습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 404) {
    return {
      kind: "not_found",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "요청한 Event를 찾을 수 없거나 접근 권한이 없습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 400 || status === 422) {
    return {
      kind: "validation_error",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "요청 입력값이 유효하지 않아 서버가 거절했습니다.",
      requestId,
      retryable: false,
    };
  }

  return {
    kind: "request_rejected",
    httpStatus: status,
    code,
    message:
      envelope?.message ??
      `서버 요청이 처리되지 않았습니다 (HTTP ${status}).`,
    requestId,
    retryable: envelope?.retryable ?? false,
  };
}

export class FetchEventReviewHttpTransport implements EventReviewAdapter {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly keyGenerator: () => string;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: EventReviewHttpTransportOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.keyGenerator = options.keyGenerator ?? generateUuidV4;
    this.defaultHeaders = options.defaultHeaders ?? {};
  }

  private async requestJson<T>(params: {
    method: "GET" | "POST" | "PATCH";
    path: string;
    headers?: Record<string, string>;
    body?: Record<string, unknown>;
    normalize?: (raw: unknown, meta: EventReviewResponseMeta) => T;
  }): Promise<EventReviewResult<T>> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Request-ID": this.keyGenerator(),
      ...this.defaultHeaders,
      ...(params.headers ?? {}),
    };

    let encodedBody: string | undefined;
    if (params.body !== undefined) {
      headers["Content-Type"] = "application/json";
      encodedBody = JSON.stringify(params.body);
    }

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${params.path}`, {
        method: params.method,
        headers,
        body: encodedBody,
      });
    } catch (err) {
      const detail =
        err instanceof Error ? err.message : "Network request failed";
      return {
        ok: false,
        error: {
          kind: "network_error",
          code: "NETWORK_ERROR",
          message: `네트워크 오류로 서버 응답을 수신하지 못했습니다(${detail}). 자동 재전송하지 않았습니다.`,
          retryable: true,
        },
      };
    }

    const meta = extractMeta(response);
    const rawBody = await response.text();

    if (!response.ok) {
      return {
        ok: false,
        error: classifyErrorResponse(response.status, rawBody, meta),
        meta,
      };
    }

    const parsed = parseOptionalJsonBody<unknown>(rawBody);
    if (parsed.empty || parsed.invalidJson || parsed.value === null) {
      return {
        ok: false,
        error: {
          kind: "request_rejected",
          httpStatus: response.status,
          code: "INVALID_RESPONSE_BODY",
          message: "서버 응답 JSON을 해석할 수 없습니다.",
          retryable: false,
        },
        meta,
      };
    }

    const data = params.normalize
      ? params.normalize(parsed.value, meta)
      : (parsed.value as T);

    return {
      ok: true,
      data,
      meta,
    };
  }

  public async listWorkEvents(
    query?: ListWorkEventsQuery
  ): Promise<EventReviewResult<WorkEventListResponse>> {
    const search = new URLSearchParams();
    search.set("limit", String(query?.limit ?? 50));
    if (query?.status && query.status !== "ALL" && query.status !== "ACTIVE") {
      search.set("status", query.status);
    }
    if (query?.source_resource_id) {
      search.set("source_resource_id", query.source_resource_id);
    }
    if (query?.cursor) {
      search.set("cursor", query.cursor);
    }
    return this.requestJson<WorkEventListResponse>({
      method: "GET",
      path: `/v1/work-events?${search.toString()}`,
      normalize: (raw) => normalizeWorkEventListResponse(raw),
    });
  }

  public async getWorkEventDetail(
    eventId: string
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    return this.requestJson<WorkEventDetailResponse>({
      method: "GET",
      path: `/v1/work-events/${encodeURIComponent(eventId)}`,
      normalize: (raw, meta) => normalizeWorkEventDetail(raw, meta.etag),
    });
  }

  public async confirmWorkEvent(
    input: ConfirmWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/confirm`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User confirmed candidate event",
        idempotency_key: idempotencyKey,
      },
      normalize: (raw, meta) => normalizeWorkEventDetail(raw, meta.etag),
    });
  }

  public async editWorkEvent(
    input: EditWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "PATCH",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        title: input.title,
        problem: input.problem,
        action: input.action,
        outcome: input.outcome,
        reason: input.reason ?? "User edited event details",
        idempotency_key: idempotencyKey,
      },
      normalize: (raw, meta) => normalizeWorkEventDetail(raw, meta.etag),
    });
  }

  public async rejectWorkEvent(
    input: RejectWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/reject`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User excluded candidate event",
        idempotency_key: idempotencyKey,
      },
      normalize: (raw, meta) => normalizeWorkEventDetail(raw, meta.etag),
    });
  }

  public async splitWorkEvent(
    input: SplitWorkEventInput
  ): Promise<EventReviewResult<SplitWorkEventResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<SplitWorkEventResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/split`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User split mixed-purpose event",
        idempotency_key: idempotencyKey,
        partitions: input.partitions,
      },
      normalize: (raw) => normalizeSplitWorkEventResponse(raw),
    });
  }

  public async mergeWorkEvents(
    input: MergeWorkEventsInput
  ): Promise<EventReviewResult<MergeWorkEventsResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<MergeWorkEventsResponse>({
      method: "POST",
      path: "/v1/work-events/actions/merge",
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        members: input.members,
        title: input.title,
        reason: input.reason ?? "User merged related work events",
        idempotency_key: idempotencyKey,
      },
      normalize: (raw) => normalizeMergeWorkEventsResponse(raw),
    });
  }

  public async dissolveWorkEvent(
    input: DissolveWorkEventInput
  ): Promise<EventReviewResult<DissolveWorkEventResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<DissolveWorkEventResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/dissolve`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User dissolved composite event",
        idempotency_key: idempotencyKey,
      },
      normalize: (raw) => normalizeDissolveWorkEventResponse(raw),
    });
  }
}
