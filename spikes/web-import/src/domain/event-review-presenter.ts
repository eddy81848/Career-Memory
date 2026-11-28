import type {
  AnchorMode,
  EventReviewScenarioMeta,
  ReviewFlagCode,
  SplitPartitionInput,
  WorkEventDetailResponse,
  WorkEventStatus,
  WorkEventSummaryItem,
} from "./event-review-models";

/**
 * Pure presentation & validation helpers for M3 Event Review Inbox
 * (Product Spec §5~8, EV-007, EV-010, T-304B, ER-004).
 */

export interface ReviewFlagMeta {
  code: ReviewFlagCode;
  label: string;
  description: string;
  tone: "warning" | "info" | "danger";
}

export const REVIEW_FLAG_CATALOG: Record<ReviewFlagCode, ReviewFlagMeta> = {
  AMBIGUOUS_AUTHOR_IDENTITY: {
    code: "AMBIGUOUS_AUTHOR_IDENTITY",
    label: "작성자 신원 확인 필요",
    description:
      "커밋/PR 작성자 매핑이 휴리스틱 또는 미확정 상태입니다. 본인 기여 범위를 확인해 주세요.",
    tone: "warning",
  },
  DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED: {
    code: "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED",
    label: "직접 커밋 PR 연결 미관측 — 확인 필요",
    description:
      "기본 브랜치 직접 커밋에서 연결된 PR이 관측되지 않았습니다(NO_MATCH_OBSERVED). 실제로 PR 없이 반영된 작업인지 확인해 주세요.",
    tone: "warning",
  },
  GENERIC_TITLE_OR_SUMMARY: {
    code: "GENERIC_TITLE_OR_SUMMARY",
    label: "일반적·불명확한 제목",
    description:
      "제목이 너무 짧거나 일반적인 단어(fix/update/wip 등)로 구성되어 있습니다. Edit으로 구체적인 작업 제목을 보완하는 것을 권장합니다.",
    tone: "info",
  },
  MIXED_PURPOSE_SIGNALS: {
    code: "MIXED_PURPOSE_SIGNALS",
    label: "복합 목적 신호 감지 (분할 검토)",
    description:
      "서로 다른 컴포넌트·디렉터리 또는 독립 작업 신호가 함께 관측되었습니다. 자동 분할하지 않았으니 필요 시 Split으로 범위를 나누세요.",
    tone: "warning",
  },
  MULTIPLE_LINKED_ISSUES: {
    code: "MULTIPLE_LINKED_ISSUES",
    label: "복수 Issue 참조 관측",
    description:
      "2개 이상의 Issue/티켓 참조가 참고 문맥(CONTEXT_ONLY)으로 관측되었습니다. 하나의 작업 단위인지 검토해 주세요.",
    tone: "info",
  },
  PARTIAL_SOURCE_SKIPPED: {
    code: "PARTIAL_SOURCE_SKIPPED",
    label: "일부 부속 출처 생략됨",
    description:
      "일부 부속 변경 파일 또는 대용량 패치가 스냅샷 정책상 생략되었습니다. 핵심 근거를 확인한 뒤 확정해 주세요.",
    tone: "warning",
  },
  REVIEW_REQUIRED_MULTI_PARENT: {
    code: "REVIEW_REQUIRED_MULTI_PARENT",
    label: "멀티 부모 커밋 검토 필요",
    description:
      "2개 이상의 부모를 가진 병합형 직접 커밋입니다. 단일 작업 단위인지 확인 후 확정하거나 제외해 주세요.",
    tone: "warning",
  },
};

export const EVENT_REVIEW_SCENARIOS: EventReviewScenarioMeta[] = [
  {
    id: "default_inbox",
    label: "기본 Event Inbox (PR · DIRECT_COMMIT · USER_COMPOSITE · CANNOT_LINK)",
    shortDescription:
      "PR, 기본 브랜치 직접 커밋(main/trunk), 기존 USER_COMPOSITE, CANNOT_LINK 제약을 모두 검토·편집·분할·병합·해제할 수 있습니다.",
  },
  {
    id: "regeneration_partial_failure",
    label: "USER_COMPOSITE 생성 성공 + 파생 재생성 부분 실패",
    shortDescription:
      "Composite 생성 성공 자체와 파생 Fact/Claim 재생성 실패 상태를 분리 배너로 표시합니다.",
  },
  {
    id: "conflict_on_next_mutation",
    label: "동시 수정 충돌 (409 EVENT_VERSION_CONFLICT)",
    shortDescription:
      "다음 확정/편집/병합/해제 시 409 버전 충돌을 반환하여 자동 덮어쓰기 없이 최신 상태 복구를 검증합니다.",
  },
  {
    id: "policy_blocked",
    label: "정책 차단 (POLICY_BLOCKED · 공개→비공개 전환)",
    shortDescription:
      "삭제가 아닌 정책 차단으로 표시하며 편집·확정·분할·병합·해제 액션을 즉시 비활성화합니다.",
  },
  {
    id: "deletion_pending",
    label: "삭제 펜스 활성 (DELETION_PENDING · OD-003)",
    shortDescription:
      "삭제 진행 중 펜스 상태를 표시하며 모든 큐레이션 변경 액션을 즉시 차단합니다.",
  },
  {
    id: "empty_no_eligible_events",
    label: "정상 빈 상태 (적격 PR 0건 · 적격 직접 커밋 0건)",
    shortDescription:
      "완료된 가져오기(SUCCESS) 이후 적격 작업 후보가 0건인 정상 빈 상태를 안내합니다.",
  },
  {
    id: "association_unknown_incomplete",
    label: "연결 확인 미완료 (PR 연결 UNKNOWN 부분 상태)",
    shortDescription:
      "PR-커밋 연결 조회가 UNKNOWN으로 남아 Event 후보 승격이 보류된 상태를 정상 0건과 구분 안내합니다.",
  },
];

export function formatEventStatusLabel(status: WorkEventStatus): {
  label: string;
  badgeClass: string;
} {
  switch (status) {
    case "CANDIDATE":
      return { label: "검토 대기 (CANDIDATE)", badgeClass: "badge--info" };
    case "CONFIRMED":
      return { label: "확정됨 (CONFIRMED)", badgeClass: "badge--success" };
    case "REJECTED":
      return { label: "제외됨 (REJECTED)", badgeClass: "badge--neutral" };
    case "STALE":
      return { label: "재검토 필요 (STALE)", badgeClass: "badge--warning" };
    case "SUPERSEDED":
      return { label: "이력 대체됨 (SUPERSEDED)", badgeClass: "badge--neutral" };
  }
}

export function formatAnchorModeBadge(event: Pick<
  WorkEventSummaryItem,
  "anchor_mode" | "composite_summary" | "direct_commit_badge"
>): {
  label: string;
  badgeClass: string;
} {
  switch (event.anchor_mode) {
    case "DIRECT_COMMIT":
      return {
        label:
          event.direct_commit_badge?.label ??
          "PR 없음 · default branch 직접 반영",
        badgeClass: "badge--warning",
      };
    case "USER_COMPOSITE": {
      const count = event.composite_summary?.member_count ?? 2;
      return {
        label:
          event.composite_summary?.badge_label ??
          `${count}개 Event를 사용자가 병합`,
        badgeClass: "badge--info",
      };
    }
    case "PR_SEGMENT":
      return {
        label: "PR 범위 분할 (PR_SEGMENT)",
        badgeClass: "badge--info",
      };
    case "PR":
    default:
      return {
        label: "PR 기준 Event",
        badgeClass: "badge--neutral",
      };
  }
}

export function isEventMutationBlockedByPolicyOrDeletion(
  event: Pick<WorkEventSummaryItem, "policy_state"> | null | undefined
): boolean {
  if (!event) {
    return false;
  }
  return (
    event.policy_state.status === "POLICY_BLOCKED" ||
    event.policy_state.status === "DELETION_PENDING" ||
    event.policy_state.status === "SOURCE_ACCOUNT_REVOKED"
  );
}

/**
 * EV-010 single logical event counting invariant:
 * Active top-level count includes active PR, PR_SEGMENT, DIRECT_COMMIT,
 * and USER_COMPOSITE (each USER_COMPOSITE counts as exactly 1 logical Event),
 * strictly excluding SUPERSEDED merged/split history members.
 */
export function computeActiveTopLevelCount(
  items: WorkEventSummaryItem[]
): number {
  return items.filter(
    (item) => item.is_active_top_level && item.status !== "SUPERSEDED"
  ).length;
}

export function isMergeableAnchorMode(mode: AnchorMode): boolean {
  return mode === "PR" || mode === "PR_SEGMENT" || mode === "DIRECT_COMMIT";
}

export interface MergeSelectionValidationResult {
  valid: boolean;
  code:
    | "OK"
    | "TOO_FEW_EVENTS"
    | "NESTED_COMPOSITE_FORBIDDEN"
    | "INACTIVE_OR_SUPERSEDED"
    | "CROSS_REPOSITORY_FORBIDDEN"
    | "POLICY_OR_DELETION_BLOCKED"
    | "CANNOT_LINK_CONFLICT";
  reason: string | null;
  conflictingPair?: [string, string];
  affectedImpact: {
    fact_count: number;
    claim_count: number;
    export_count: number;
  };
}

export function validateMergeSelection(
  selectedEvents: WorkEventSummaryItem[],
  detailsById: Record<string, WorkEventDetailResponse | undefined>
): MergeSelectionValidationResult {
  const affectedImpact = selectedEvents.reduce(
    (acc, item) => {
      const detail = detailsById[item.event_id];
      return {
        fact_count: acc.fact_count + (detail?.downstream_impact.fact_count ?? 0),
        claim_count: acc.claim_count + (detail?.downstream_impact.claim_count ?? 0),
        export_count: acc.export_count + (detail?.downstream_impact.export_count ?? 0),
      };
    },
    { fact_count: 0, claim_count: 0, export_count: 0 }
  );

  if (selectedEvents.length < 2) {
    return {
      valid: false,
      code: "TOO_FEW_EVENTS",
      reason: "동일 저장소의 활성 Event를 2개 이상 선택해야 병합할 수 있습니다.",
      affectedImpact,
    };
  }

  for (const item of selectedEvents) {
    if (isEventMutationBlockedByPolicyOrDeletion(item)) {
      return {
        valid: false,
        code: "POLICY_OR_DELETION_BLOCKED",
        reason:
          item.policy_state.status === "DELETION_PENDING"
            ? "삭제 진행 중(DELETION_PENDING) 상태에서는 병합을 실행할 수 없습니다."
            : "정책 차단(POLICY_BLOCKED) 상태에서는 병합을 실행할 수 없습니다.",
        affectedImpact,
      };
    }

    if (item.anchor_mode === "USER_COMPOSITE") {
      return {
        valid: false,
        code: "NESTED_COMPOSITE_FORBIDDEN",
        reason: `중첩 USER_COMPOSITE 병합은 허용되지 않습니다 ("${item.title}"). 먼저 기존 병합을 해제(Dissolve)한 뒤 다시 병합하세요.`,
        affectedImpact,
      };
    }

    if (
      !item.is_active_top_level ||
      item.status === "SUPERSEDED" ||
      item.status === "REJECTED"
    ) {
      return {
        valid: false,
        code: "INACTIVE_OR_SUPERSEDED",
        reason: `활성 상태의 PR / PR_SEGMENT / DIRECT_COMMIT Event만 병합할 수 있습니다 ("${item.title}" · ${item.status}).`,
        affectedImpact,
      };
    }
  }

  const repoIds = new Set(selectedEvents.map((e) => e.source_resource_id));
  if (repoIds.size > 1) {
    return {
      valid: false,
      code: "CROSS_REPOSITORY_FORBIDDEN",
      reason: "서로 다른 저장소의 Event는 하나의 USER_COMPOSITE로 병합할 수 없습니다.",
      affectedImpact,
    };
  }

  const selectedIds = new Set(selectedEvents.map((e) => e.event_id));
  for (const item of selectedEvents) {
    const detail = detailsById[item.event_id];
    const peerIds = detail?.cannot_link_constraints.peer_event_ids ?? [];
    for (const peerId of peerIds) {
      if (selectedIds.has(peerId) && peerId !== item.event_id) {
        const constraint = detail?.cannot_link_constraints.constraints.find(
          (entry) =>
            entry.target_type === "PEER_EVENT" && entry.target_id === peerId
        );
        const peerTitle =
          selectedEvents.find((e) => e.event_id === peerId)?.title ?? peerId;
        return {
          valid: false,
          code: "CANNOT_LINK_CONFLICT",
          reason: `CANNOT_LINK 제약 충돌: "${item.title}"와 "${peerTitle}"은(는) 함께 병합할 수 없습니다${
            constraint?.reason ? ` (${constraint.reason})` : ""
          }.`,
          conflictingPair: [item.event_id, peerId],
          affectedImpact,
        };
      }
    }
  }

  return {
    valid: true,
    code: "OK",
    reason: null,
    affectedImpact,
  };
}

export interface SplitPartitionValidationResult {
  valid: boolean;
  reason: string | null;
}

export function validateSplitPartitions(
  partitions: SplitPartitionInput[],
  allowedEvidenceObjectIds: string[]
): SplitPartitionValidationResult {
  if (partitions.length < 2) {
    return {
      valid: false,
      reason: "분할(Split)하려면 최소 2개 이상의 분할 범위가 필요합니다.",
    };
  }

  const allowedSet = new Set(allowedEvidenceObjectIds);
  const seenIds = new Set<string>();

  for (let idx = 0; idx < partitions.length; idx += 1) {
    const part = partitions[idx];
    if (!part.title.trim()) {
      return {
        valid: false,
        reason: `분할 #${idx + 1}의 제목을 입력해 주세요.`,
      };
    }
    if (!part.source_object_ids || part.source_object_ids.length === 0) {
      return {
        valid: false,
        reason: `분할 #${idx + 1}("${part.title}")에 최소 1개 이상의 소속 근거(MUST_LINK)를 선택해 주세요.`,
      };
    }

    for (const objId of part.source_object_ids) {
      if (!allowedSet.has(objId)) {
        return {
          valid: false,
          reason: `허용되지 않은 근거 항목(${objId})이 분할에 포함되었습니다. MUST_LINK 항목만 분할할 수 있습니다.`,
        };
      }
      if (seenIds.has(objId)) {
        return {
          valid: false,
          reason: `분할 범위가 서로 겹칩니다(${objId}). 각 근거 항목은 하나의 분할에만 속해야 합니다(disjoint).`,
        };
      }
      seenIds.add(objId);
    }
  }

  return { valid: true, reason: null };
}
