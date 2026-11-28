import type {
  CircuitBreakerState,
  DeletionComponent,
  DeletionComponentStatus,
  DeletionImpactCounts,
  DeletionJobStatusResponse,
  DeletionUserVisibleState,
  MemoryOpsScenarioId,
  QuotaAdmissionState,
  QuotaMeterItem,
  QuotaMeterStatus,
  QuotaMetricKind,
  QuotaPauseState,
  QuotaStatusResponse,
  SearchEntityType,
  SearchHit,
  SearchQueryInput,
  WorkspaceSourceResourceSummary,
} from "./memory-ops-models";

/**
 * Pure presentation & invariant helpers for M5 Workspace Memory Search (OD-008),
 * Source Deletion Preview/Confirm & Receipts (OD-003), and Quota/Pause UI (T-503 / INF-06).
 */

export interface BadgeDescriptor {
  label: string;
  badgeClass:
    | "badge--neutral"
    | "badge--info"
    | "badge--success"
    | "badge--warning"
    | "badge--danger";
}

export const PROHIBITED_PROJECT_SCOPE_KEYS = new Set([
  "project_id",
  "projectid",
  "project_ids",
  "projectids",
]);

export const SEARCH_ENTITY_TYPE_ORDER: readonly SearchEntityType[] = [
  "WORK_EVENT",
  "FACT",
  "CLAIM",
];

export const DELETION_COMPONENT_ORDER: readonly DeletionComponent[] = [
  "JOBS_AND_OUTBOX",
  "EXPORTS",
  "FTS_INDEX",
  "EPHEMERAL_CACHE",
  "AUTHORITATIVE_DOMAIN",
];

export interface MemoryOpsScenarioDescriptor {
  id: MemoryOpsScenarioId;
  label: string;
  shortDescription: string;
}

export const MEMORY_OPS_SCENARIOS: readonly MemoryOpsScenarioDescriptor[] = [
  {
    id: "default_active",
    label: "1. 기본 정상 운영 (Search + OD-003 삭제 + 정상 한도)",
    shortDescription:
      "워크스페이스 비원문 검색(2페이지 커서), 저장소 영향 범위 조회/삭제, 정상(ALLOWED) 쿼터 상태를 검증합니다.",
  },
  {
    id: "index_lag_filtered",
    label: "2. 색인 지연 권위 재검증 필터링 (T-501A)",
    shortDescription:
      "FTS 색인 후보 중 STALE·REJECTED·차단 항목이 응답 직전 권위 테이블 재검증으로 제외된 상태를 보여줍니다.",
  },
  {
    id: "cursor_version_drift",
    label: "3. 검색 커서 버전 불일치 복구 (400 INVALID_SEARCH_CURSOR)",
    shortDescription:
      "다음 페이지 요청 시 색인/정렬 버전 드리프트(400)가 발생하고 '처음부터 다시 검색'으로 안전 복구합니다.",
  },
  {
    id: "quota_warning_80",
    label: "4. 한도 80% 경고 (WARNING_80_PERCENT)",
    shortDescription:
      "사용량 80% 도달 소프트 경고 배너와 무료 대안 안내를 표시하며 모든 기능을 허용합니다.",
  },
  {
    id: "quota_cutoff_95_paused",
    label: "5. 한도 95% 차단 / 일시정지 (CUTOFF_95_PERCENT · 삭제는 유지)",
    shortDescription:
      "고비용 작업(가져오기/생성/Export)이 차단되지만 유료 자동 전환은 불가하며, 저장소 삭제(OD-003)·연결 해제는 유지됩니다.",
  },
  {
    id: "deletion_scope_changed_409",
    label: "6. 삭제 미리보기 변경 충돌 (409 DELETION_PREVIEW_SCOPE_CHANGED)",
    shortDescription:
      "확인 시점의 리소스/영향 범위 버전 불일치로 409가 발생하고, 영구 삭제 없이 '영향 범위 다시 조회'로 복구합니다.",
  },
  {
    id: "deletion_partial_pending",
    label: "7. 부분 완료 삭제 작업 진행 중 (PARTIAL / DELETION_PENDING)",
    shortDescription:
      "5개 삭제 구성요소 중 일부가 FAILED/PENDING인 DELETION_PENDING 상태와 재시도 안내 및 삭제 펜스를 확인합니다.",
  },
  {
    id: "deletion_completed",
    label: "8. 저장소 영구 삭제 완료 (SUCCEEDED / DELETED)",
    shortDescription:
      "5개 구성요소 모두 정리가 완료된 DELETED 영수증을 표시하며, 다른 독립 저장소는 영향 없이 유지됩니다.",
  },
];

// =============================================================================
// OD-008 Zero-project_id Search Query Builder & Scope Resolution
// =============================================================================

export function assertNoProjectIdInRecord(
  record: Record<string, unknown> | null | undefined
): void {
  if (!record) return;
  for (const [key, value] of Object.entries(record)) {
    if (PROHIBITED_PROJECT_SCOPE_KEYS.has(key.trim().toLowerCase())) {
      throw new Error(
        `OD-008 violation: prohibited scope parameter "${key}" cannot be sent by client.`
      );
    }
    if (value && typeof value === "object" && !Array.isArray(value)) {
      assertNoProjectIdInRecord(value as Record<string, unknown>);
    }
  }
}

export function buildSafeSearchQueryParams(
  input: SearchQueryInput & Record<string, unknown>
): URLSearchParams {
  assertNoProjectIdInRecord(input);
  const trimmedQ = input.q.trim();
  if (!trimmedQ) {
    throw new Error("검색어(q)는 비어 있을 수 없습니다.");
  }

  const params = new URLSearchParams();
  params.set("q", trimmedQ);

  if (input.source_resource_id && input.source_resource_id.trim()) {
    params.set("source_resource_id", input.source_resource_id.trim());
  }

  if (Array.isArray(input.entity_types) && input.entity_types.length > 0) {
    const seen = new Set<SearchEntityType>();
    for (const et of input.entity_types) {
      if (!seen.has(et)) {
        seen.add(et);
        params.append("entity_types", et);
      }
    }
  }

  const limit = input.limit ?? 20;
  const clampedLimit = Math.max(1, Math.min(100, Math.floor(limit)));
  params.set("limit", String(clampedLimit));

  if (input.cursor && input.cursor.trim()) {
    params.set("cursor", input.cursor.trim());
  }

  return params;
}

export interface SearchScopeResolution {
  autoSelectedSingleRepo: boolean;
  defaultSourceResourceId: string | null;
  activeRepositories: WorkspaceSourceResourceSummary[];
}

export function resolveDefaultSearchScope(
  repositories: WorkspaceSourceResourceSummary[]
): SearchScopeResolution {
  const activeRepositories = repositories.filter(
    (repo) => !repo.fence_active && !repo.deleted
  );
  if (activeRepositories.length === 1) {
    return {
      autoSelectedSingleRepo: true,
      defaultSourceResourceId: activeRepositories[0].source_resource_id,
      activeRepositories,
    };
  }
  return {
    autoSelectedSingleRepo: false,
    defaultSourceResourceId: null,
    activeRepositories,
  };
}

export function formatSearchEntityBadge(
  entityType: SearchEntityType
): BadgeDescriptor {
  switch (entityType) {
    case "WORK_EVENT":
      return { label: "Work Event (WORK_EVENT)", badgeClass: "badge--info" };
    case "FACT":
      return { label: "Canonical Fact (FACT)", badgeClass: "badge--neutral" };
    case "CLAIM":
      return { label: "Grounded Claim (CLAIM)", badgeClass: "badge--success" };
  }
}

export function buildSearchHitDrilldownHref(hit: SearchHit): {
  href: string;
  label: string;
} {
  if (hit.entity_type === "WORK_EVENT") {
    return {
      href: `/event-review?event_id=${encodeURIComponent(hit.entity_id)}`,
      label: "Event Inbox에서 검토 (/event-review)",
    };
  }
  if (hit.entity_type === "FACT") {
    return {
      href: `/claim-review?fact_id=${encodeURIComponent(hit.entity_id)}`,
      label: "Claim/Fact Review에서 확인 (/claim-review)",
    };
  }
  return {
    href: `/claim-review?claim_id=${encodeURIComponent(hit.entity_id)}`,
    label: "Claim Review에서 검토 (/claim-review)",
  };
}

export function formatSearchScore(score: number): string {
  if (!Number.isFinite(score)) return "0.00";
  return score.toFixed(2);
}

// =============================================================================
// OD-003 Source Deletion Impact & Component Receipt Presenters
// =============================================================================

export interface DeletionImpactRow {
  key: keyof DeletionImpactCounts;
  label: string;
  count: number;
  highRisk: boolean;
  description: string;
}

export function buildDeletionImpactRows(
  counts: DeletionImpactCounts
): DeletionImpactRow[] {
  return [
    {
      key: "claims_approved",
      label: "승인된 Claim (claims_approved)",
      count: counts.claims_approved,
      highRisk: true,
      description: "사용자가 최종 승인한 Grounded Claim이 영구 삭제됩니다.",
    },
    {
      key: "claims_edited",
      label: "사용자 편집 Claim (claims_edited)",
      count: counts.claims_edited,
      highRisk: true,
      description: "직접 문구를 수정한 Claim 버전이 영구 삭제됩니다.",
    },
    {
      key: "claims_draft",
      label: "초안 Claim (claims_draft)",
      count: counts.claims_draft,
      highRisk: false,
      description: "검토 대기 및 초안 Claim이 영구 삭제됩니다.",
    },
    {
      key: "claims_historical_versions",
      label: "Claim 과거 이력 버전 (claims_historical_versions)",
      count: counts.claims_historical_versions,
      highRisk: true,
      description: "대체(SUPERSEDED)된 과거 Claim 버전 이력이 모두 파기됩니다.",
    },
    {
      key: "server_exports",
      label: "서버 저장 Export (server_exports)",
      count: counts.server_exports,
      highRisk: true,
      description:
        "서버 보관 Markdown/JSON Export 레코드 및 페이로드가 즉시 파기됩니다.",
    },
    {
      key: "work_events",
      label: "Work Events (work_events)",
      count: counts.work_events,
      highRisk: false,
      description: "추출·병합·분할된 모든 Work Event가 파기됩니다.",
    },
    {
      key: "facts",
      label: "Canonical Facts (facts)",
      count: counts.facts,
      highRisk: false,
      description: "추출 및 사용자 확인된 Fact 레코드가 파기됩니다.",
    },
    {
      key: "contributions",
      label: "Contributions (contributions)",
      count: counts.contributions,
      highRisk: false,
      description: "역할·기여도 파생 레코드가 파기됩니다.",
    },
    {
      key: "search_index_entries",
      label: "검색 색인 항목 (search_index_entries)",
      count: counts.search_index_entries,
      highRisk: false,
      description: "PostgreSQL FTS 비원문 검색 엔트리가 제거됩니다.",
    },
    {
      key: "ephemeral_cache_entries",
      label: "메모리 임시 캐시 항목 (ephemeral_cache_entries)",
      count: counts.ephemeral_cache_entries,
      highRisk: false,
      description: "프로세스 내 임시 조회 버퍼 항목이 즉시 제거됩니다.",
    },
  ];
}

export function sumDeletionImpactCounts(counts: DeletionImpactCounts): number {
  return (
    counts.work_events +
    counts.facts +
    counts.contributions +
    counts.claims_draft +
    counts.claims_edited +
    counts.claims_approved +
    counts.claims_historical_versions +
    counts.search_index_entries +
    counts.ephemeral_cache_entries +
    counts.server_exports
  );
}

export function formatDeletionUserVisibleStateBadge(
  state: DeletionUserVisibleState,
  internalStatus?: DeletionJobStatusResponse["status"]
): BadgeDescriptor {
  if (state === "DELETED") {
    return {
      label: "영구 삭제 완료 (DELETED · SUCCEEDED)",
      badgeClass: "badge--success",
    };
  }
  if (state === "DELETION_FAILED") {
    return {
      label: "삭제 실패 · 재시도 필요 (DELETION_FAILED)",
      badgeClass: "badge--danger",
    };
  }
  if (internalStatus === "PARTIAL") {
    return {
      label: "삭제 진행 중 · 부분 완료 (DELETION_PENDING · PARTIAL)",
      badgeClass: "badge--warning",
    };
  }
  return {
    label: `삭제 진행 중 (DELETION_PENDING${internalStatus ? ` · ${internalStatus}` : ""})`,
    badgeClass: "badge--warning",
  };
}

export function formatDeletionComponentLabel(
  component: DeletionComponent
): string {
  switch (component) {
    case "JOBS_AND_OUTBOX":
      return "대기 작업·아웃박스 취소 (JOBS_AND_OUTBOX)";
    case "EXPORTS":
      return "서버 Export 파기 (EXPORTS)";
    case "FTS_INDEX":
      return "검색 색인 제거 (FTS_INDEX)";
    case "EPHEMERAL_CACHE":
      return "메모리 임시 캐시 제거 (EPHEMERAL_CACHE)";
    case "AUTHORITATIVE_DOMAIN":
      return "권위 도메인 데이터 파기 (AUTHORITATIVE_DOMAIN)";
  }
}

export function formatDeletionComponentStatusBadge(
  status: DeletionComponentStatus
): BadgeDescriptor {
  switch (status) {
    case "SUCCEEDED":
      return { label: "완료 (SUCCEEDED)", badgeClass: "badge--success" };
    case "FAILED":
      return { label: "실패 · 재시도 대기 (FAILED)", badgeClass: "badge--danger" };
    case "RUNNING":
      return { label: "정리 중 (RUNNING)", badgeClass: "badge--info" };
    case "PENDING":
      return { label: "대기 중 (PENDING)", badgeClass: "badge--warning" };
  }
}

export interface DeletionReceiptSummary {
  totalComponents: number;
  succeededCount: number;
  failedCount: number;
  pendingOrRunningCount: number;
  totalPurgedCount: number;
  incompleteComponents: DeletionComponent[];
  isPartialPending: boolean;
  isFullyDeleted: boolean;
}

export function summarizeDeletionReceipt(
  job: DeletionJobStatusResponse
): DeletionReceiptSummary {
  let succeededCount = 0;
  let failedCount = 0;
  let pendingOrRunningCount = 0;
  let totalPurgedCount = 0;
  const incompleteComponents: DeletionComponent[] = [];

  for (const item of job.components) {
    totalPurgedCount += item.purged_count;
    if (item.status === "SUCCEEDED") {
      succeededCount += 1;
    } else if (item.status === "FAILED") {
      failedCount += 1;
      incompleteComponents.push(item.component);
    } else {
      pendingOrRunningCount += 1;
      incompleteComponents.push(item.component);
    }
  }

  return {
    totalComponents: job.components.length,
    succeededCount,
    failedCount,
    pendingOrRunningCount,
    totalPurgedCount,
    incompleteComponents,
    isPartialPending:
      job.user_visible_state === "DELETION_PENDING" &&
      (job.status === "PARTIAL" || failedCount > 0 || pendingOrRunningCount > 0),
    isFullyDeleted:
      job.user_visible_state === "DELETED" && job.status === "SUCCEEDED",
  };
}

// =============================================================================
// T-503 / INF-06 Quota, Budget & Pause Status Presenters
// =============================================================================

export function formatQuotaAdmissionBadge(
  state: QuotaAdmissionState
): BadgeDescriptor {
  switch (state) {
    case "ALLOWED":
      return { label: "정상 허용 (ALLOWED)", badgeClass: "badge--success" };
    case "WARNING_80_PERCENT":
      return {
        label: "80% 소프트 경고 (WARNING_80_PERCENT)",
        badgeClass: "badge--warning",
      };
    case "CUTOFF_95_PERCENT":
      return {
        label: "95% 하드 차단 (CUTOFF_95_PERCENT)",
        badgeClass: "badge--danger",
      };
    case "PAUSED":
      return { label: "고비용 작업 일시정지 (PAUSED)", badgeClass: "badge--danger" };
  }
}

export function formatQuotaPauseBadge(state: QuotaPauseState): BadgeDescriptor {
  switch (state) {
    case "ACTIVE":
      return { label: "운영 활성 (ACTIVE)", badgeClass: "badge--success" };
    case "PAUSED_QUOTA":
      return {
        label: "한도 도달 일시정지 (PAUSED_QUOTA)",
        badgeClass: "badge--danger",
      };
    case "PAUSED_CIRCUIT_BREAKER":
      return {
        label: "서킷 브레이커 일시정지 (PAUSED_CIRCUIT_BREAKER)",
        badgeClass: "badge--danger",
      };
    case "PAUSED_OPERATOR":
      return {
        label: "운영자 일시정지 (PAUSED_OPERATOR)",
        badgeClass: "badge--warning",
      };
  }
}

export function formatCircuitBreakerBadge(
  state: CircuitBreakerState
): BadgeDescriptor {
  switch (state) {
    case "CLOSED":
      return { label: "서킷 정상 (CLOSED)", badgeClass: "badge--success" };
    case "HALF_OPEN":
      return { label: "서킷 점검 (HALF_OPEN)", badgeClass: "badge--warning" };
    case "OPEN":
      return { label: "서킷 차단 (OPEN)", badgeClass: "badge--danger" };
  }
}

export function formatQuotaMeterStatusBadge(
  status: QuotaMeterStatus
): BadgeDescriptor {
  switch (status) {
    case "OK":
      return { label: "정상 (OK)", badgeClass: "badge--success" };
    case "WARNING":
      return { label: "80% 경고 (WARNING)", badgeClass: "badge--warning" };
    case "CUTOFF":
      return { label: "95% 차단 (CUTOFF)", badgeClass: "badge--danger" };
  }
}

export function formatQuotaMetricLabel(kind: QuotaMetricKind): string {
  switch (kind) {
    case "IMPORT_OBJECTS":
      return "가져오기 객체 수 (IMPORT_OBJECTS)";
    case "PROVIDER_REQUESTS":
      return "GitHub API 요청 수 (PROVIDER_REQUESTS)";
    case "REFETCH_BYTES":
      return "임시 원문 재조회 용량 (REFETCH_BYTES)";
    case "EXPORT_BUILDS":
      return "Export 생성 횟수 (EXPORT_BUILDS)";
    case "SEARCH_QUERIES":
      return "메모리 검색 요청 수 (SEARCH_QUERIES)";
    case "ESTIMATED_COST_KRW":
      return "예상 운영 비용 (ESTIMATED_COST_KRW)";
    case "LLM_TOKENS":
      return "LLM 토큰 사용량 (LLM_TOKENS)";
  }
}

export function formatUtilizationPercent(percent: number): string {
  if (!Number.isFinite(percent)) return "0.0%";
  const clamped = Math.max(0, Math.min(999.9, percent));
  return `${clamped.toFixed(1)}%`;
}

export function computeMeterBarWidthPercent(meter: QuotaMeterItem): number {
  if (!Number.isFinite(meter.utilization_percent)) return 0;
  return Math.max(0, Math.min(100, meter.utilization_percent));
}

export function formatRetryAfterDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) {
    return null;
  }
  if (seconds < 60) {
    return `${Math.ceil(seconds)}초 후 재시도 가능 (Retry-After: ${Math.ceil(seconds)}s)`;
  }
  const minutes = Math.ceil(seconds / 60);
  return `약 ${minutes}분 후 재시도 가능 (Retry-After: ${Math.ceil(seconds)}s)`;
}

export interface MemoryOpsOperationPermissions {
  highCostOperationsAllowed: boolean;
  canSearchSelectedRepo: boolean;
  canRunHighCostOnSelectedRepo: boolean;
  canDeleteSelectedRepo: boolean;
  canDisconnectAccount: boolean;
  paidFallbackAllowed: false;
  blockReason: string | null;
}

export function evaluateOperationPermissions(params: {
  quotaStatus: QuotaStatusResponse | null;
  selectedRepoFenced: boolean;
  selectedRepoDeleted: boolean;
}): MemoryOpsOperationPermissions {
  const quota = params.quotaStatus;
  const highCostAllowedByQuota = quota ? quota.high_cost_operations_allowed : true;
  const safetyAllowed = quota ? quota.safety_operations_allowed : true;

  let blockReason: string | null = null;
  if (params.selectedRepoDeleted) {
    blockReason =
      "선택한 저장소는 영구 삭제(DELETED)되어 검색·Export·고비용 작업이 비활성화되었습니다.";
  } else if (params.selectedRepoFenced) {
    blockReason =
      "선택한 저장소에 삭제 펜스(DELETION_PENDING · fence_active=true)가 활성화되어 검색·Export·고비용 작업이 즉시 차단되었습니다.";
  } else if (!highCostAllowedByQuota && quota) {
    blockReason = `예산/한도 보호(${quota.admission_state} · ${quota.pause_state})로 고비용 작업이 일시 중단되었습니다. 유료 자동 전환은 불가하며, 저장소 삭제(OD-003) 및 계정 연결 해제는 계속 사용할 수 있습니다.`;
  }

  return {
    highCostOperationsAllowed: highCostAllowedByQuota,
    canSearchSelectedRepo:
      !params.selectedRepoFenced && !params.selectedRepoDeleted,
    canRunHighCostOnSelectedRepo:
      highCostAllowedByQuota &&
      !params.selectedRepoFenced &&
      !params.selectedRepoDeleted,
    // Safety operations (OD-003 deletion & account disconnect) remain enabled even under 95% cutoff/pause!
    canDeleteSelectedRepo:
      safetyAllowed && !params.selectedRepoFenced && !params.selectedRepoDeleted,
    canDisconnectAccount: safetyAllowed,
    paidFallbackAllowed: false,
    blockReason,
  };
}
