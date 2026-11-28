import type {
  ClaimAtomDetailItem,
  ClaimAtomStatus,
  ClaimAtomType,
  ClaimDetailResponse,
  ClaimReviewError,
  ClaimReviewScenarioMeta,
  ClaimReviewWorkspaceGuard,
  ClaimStatus,
  ClaimSummaryItem,
  ComputedVerification,
  EphemeralContentResponse,
  EphemeralViewerUiState,
  ExportPolicy,
  FactNormalizedValue,
  FactStatus,
  MetricDetailsDto,
  SourceResolutionMetadata,
  SupportType,
} from "./claim-review-models";

/**
 * Pure presentation, gating, and validation helpers for M4 Claim Review & Export UI (T-406).
 */

export const CLAIM_REVIEW_SCENARIOS: ClaimReviewScenarioMeta[] = [
  {
    id: "default_grounded",
    label: "기본 Grounded Claim Review (SOURCE_VERIFIED · MIXED · UNVERIFIED · Export)",
    shortDescription:
      "검증된 Claim, 사용자 확인 포함(MIXED) Claim, 미근거 차단(UNVERIFIED) Claim, Plan C 임시 원문 뷰어(6상태), 비인용 Markdown/JSON Export를 모두 검증합니다.",
  },
  {
    id: "conflict_on_next_mutation",
    label: "동시 수정·근거 충돌 (409 CLAIM_VERSION_CONFLICT / SUPPORT_INCOMPLETE)",
    shortDescription:
      "다음 Claim 편집/승인/재검증 시 409 충돌을 반환하며, 자동 재전송 없이 최신 Claim 재조회 복구를 검증합니다.",
  },
  {
    id: "manual_only_llm_blocked",
    label: "외부 LLM 기본 차단 (MANUAL_ONLY_BLOCKED · 무료 대안 안내)",
    shortDescription:
      "호스팅 LLM 기본 OFF 상태에서 Claim 생성 요청 시 정제된 사유와 4가지 무료 대안을 표시합니다.",
  },
  {
    id: "generation_synthetic_ready",
    label: "합성 픽스처 Claim 생성 성공 (SYNTHETIC_FIXTURE_OFFLINE)",
    shortDescription:
      "프로토타입 합성 픽스처 모드로 확정된 Fact 기반 신규 Claim 생성 및 검증 흐름을 테스트합니다.",
  },
  {
    id: "policy_blocked",
    label: "정책 차단 (POLICY_BLOCKED · 변경/승인/Export 즉시 잠금)",
    shortDescription:
      "공개→비공개 전환 등 정책 차단 상태에서 Fact/Claim 편집·승인·Export 및 원문 열람이 모두 차단됩니다.",
  },
  {
    id: "deletion_pending",
    label: "삭제 펜스 활성 (DELETION_PENDING · OD-003)",
    shortDescription:
      "삭제 진행 중 펜스가 활성화되어 모든 변경·승인·Export 액션이 즉시 잠깁니다.",
  },
  {
    id: "import_not_ready",
    label: "가져오기 미준비 진입 가드 (import_ready=false)",
    shortDescription:
      "SUCCESS + import_ready=true 조건을 만족하지 않을 때 Claim Review 진입이 안전하게 잠기는 가드를 검증합니다.",
  },
];

export const FREE_ALTERNATIVES_DEFAULT: string[] = [
  "Reduce scope",
  "Manual retry after quota reset",
  "Non-AI curation / manual draft",
  "Synthetic fixture preview in prototype mode",
];

export interface BadgeInfo {
  label: string;
  badgeClass: string;
  description?: string;
}

/**
 * Strictly formats workflow `status` (separate from read-only `computed_verification`).
 */
export function formatClaimStatusBadge(status: ClaimStatus): BadgeInfo {
  switch (status) {
    case "DRAFT":
      return {
        label: "초안 (DRAFT)",
        badgeClass: "badge--info",
        description: "편집 또는 생성 직후 상태이며 Atomization & Support Validation 재검증이 필요합니다.",
      };
    case "DRAFT_INVALID":
      return {
        label: "초안 검증 실패 (DRAFT_INVALID)",
        badgeClass: "badge--danger",
        description: "스키마 또는 근거 구조 검증을 통과하지 못했습니다.",
      };
    case "SUPPORT_VALIDATED":
      return {
        label: "근거 검증 완료 (SUPPORT_VALIDATED)",
        badgeClass: "badge--info",
        description: "Atom 분해 및 Support Validation이 완료되어 승인 여부를 검토할 수 있습니다.",
      };
    case "APPROVED":
      return {
        label: "승인됨 (APPROVED)",
        badgeClass: "badge--success",
        description: "사용자가 최종 승인한 Claim이며 Export 대상으로 선택할 수 있습니다.",
      };
    case "REJECTED":
      return {
        label: "제외됨 (REJECTED)",
        badgeClass: "badge--neutral",
        description: "사용자가 제외한 Claim입니다.",
      };
    case "STALE":
      return {
        label: "근거 만료 (STALE)",
        badgeClass: "badge--warning",
        description: "하위 Fact 또는 원문 리비전 변경으로 재검증이 필요합니다.",
      };
    case "SUPERSEDED":
      return {
        label: "이전 버전 (SUPERSEDED)",
        badgeClass: "badge--neutral",
        description: "편집으로 새 버전이 생성되어 보존된 과거 이력입니다.",
      };
  }
}

/**
 * Strictly formats read-only server-computed `computed_verification`.
 * Never conflated with workflow `status`.
 */
export function formatComputedVerificationBadge(
  verification: ComputedVerification
): BadgeInfo {
  switch (verification) {
    case "SOURCE_VERIFIED":
      return {
        label: "원본 근거 검증 (SOURCE_VERIFIED · 읽기 전용)",
        badgeClass: "badge--success",
        description:
          "모든 Atom이 원본 리비전 해시·위치(cm-locator-v1) 검증을 통과한 DIRECT SUPPORTED Fact에 의해 뒷받침됩니다.",
      };
    case "USER_CONFIRMED":
      return {
        label: "사용자 확인 근거 (USER_CONFIRMED · 읽기 전용)",
        badgeClass: "badge--warning",
        description:
          "원본 자동 검증 대신 사용자가 직접 확인(USER_CONFIRMED)한 Fact에 의존합니다. 승인 및 Export 시 한계 고지가 필요합니다.",
      };
    case "MIXED":
      return {
        label: "혼합 근거 (MIXED · 읽기 전용)",
        badgeClass: "badge--warning",
        description:
          "원본 검증 Atom과 사용자 확인(USER_CONFIRMED) Atom이 혼합되어 있습니다. 승인 전 사용자 확인 한계를 명시적으로 인지해야 합니다.",
      };
    case "UNVERIFIED":
      return {
        label: "미검증 (UNVERIFIED · 읽기 전용)",
        badgeClass: "badge--danger",
        description:
          "지원되지 않는(UNSUPPORTED) 또는 만료된(STALE) Atom이 있거나 아직 검증되지 않아 승인할 수 없습니다.",
      };
  }
}

export function formatFactStatusBadge(status: FactStatus): BadgeInfo {
  switch (status) {
    case "SUPPORTED":
      return { label: "근거 검증됨 (SUPPORTED)", badgeClass: "badge--success" };
    case "USER_CONFIRMED":
      return { label: "사용자 확인 (USER_CONFIRMED)", badgeClass: "badge--warning" };
    case "CANDIDATE":
      return { label: "후보 (CANDIDATE)", badgeClass: "badge--info" };
    case "CONFLICTED":
      return { label: "근거 충돌 (CONFLICTED)", badgeClass: "badge--danger" };
    case "STALE":
      return { label: "재검토 필요 (STALE)", badgeClass: "badge--warning" };
    case "REJECTED":
      return { label: "제외됨 (REJECTED)", badgeClass: "badge--neutral" };
    case "SUPERSEDED":
      return { label: "대체됨 (SUPERSEDED)", badgeClass: "badge--neutral" };
  }
}

export function formatAtomStatusBadge(status: ClaimAtomStatus): BadgeInfo {
  switch (status) {
    case "SUPPORTED":
      return { label: "SUPPORTED", badgeClass: "badge--success" };
    case "USER_CONFIRMED":
      return { label: "USER_CONFIRMED", badgeClass: "badge--warning" };
    case "UNSUPPORTED":
      return { label: "UNSUPPORTED", badgeClass: "badge--danger" };
    case "STALE":
      return { label: "STALE", badgeClass: "badge--warning" };
  }
}

export function formatAtomTypeLabel(atomType: ClaimAtomType): string {
  switch (atomType) {
    case "PROBLEM":
      return "PROBLEM (문제)";
    case "ACTION":
      return "ACTION (조치)";
    case "TECHNOLOGY":
      return "TECHNOLOGY (기술)";
    case "RESULT":
      return "RESULT (결과)";
    case "ROLE":
      return "ROLE (역할 · CLAIM-05)";
    case "METRIC":
      return "METRIC (지표 · CLAIM-06)";
  }
}

export function formatSupportTypeBadge(supportType: SupportType): BadgeInfo {
  switch (supportType) {
    case "DIRECT":
      return { label: "DIRECT (직접 근거)", badgeClass: "badge--success" };
    case "CONTEXT":
      return { label: "CONTEXT (참고 문맥)", badgeClass: "badge--neutral" };
    case "CONTRADICTS":
      return { label: "CONTRADICTS (반증/충돌)", badgeClass: "badge--danger" };
  }
}

// =============================================================================
// Entry Guard & Mutation Lock (SUCCESS + import_ready & Non-Deletion-Fenced)
// =============================================================================

export interface WorkspaceMutationGate {
  canEnter: boolean;
  mutationsLocked: boolean;
  lockReason: string | null;
  lockCode: "OK" | "IMPORT_NOT_READY" | "POLICY_BLOCKED" | "DELETION_PENDING";
}

export function evaluateWorkspaceGuard(
  guard: ClaimReviewWorkspaceGuard
): WorkspaceMutationGate {
  if (!guard.import_ready || guard.import_status !== "SUCCESS") {
    return {
      canEnter: false,
      mutationsLocked: true,
      lockCode: "IMPORT_NOT_READY",
      lockReason:
        guard.notice ??
        "완료된 최초 가져오기(SUCCESS · import_ready=true) 이후에만 Grounded Claim Review를 사용할 수 있습니다.",
    };
  }
  if (guard.policy_status === "DELETION_PENDING") {
    return {
      canEnter: true,
      mutationsLocked: true,
      lockCode: "DELETION_PENDING",
      lockReason:
        guard.notice ??
        "삭제 펜스(DELETION_PENDING · OD-003)가 활성화되어 모든 Fact/Claim 변경·승인·Export 및 원문 열람이 즉시 잠겼습니다.",
    };
  }
  if (
    guard.policy_status === "POLICY_BLOCKED" ||
    guard.policy_status === "SOURCE_ACCOUNT_REVOKED"
  ) {
    return {
      canEnter: true,
      mutationsLocked: true,
      lockCode: "POLICY_BLOCKED",
      lockReason:
        guard.notice ??
        "공개 정책 차단(POLICY_BLOCKED · 원본 삭제 아님)으로 모든 Fact/Claim 변경·승인·Export 및 원문 열람이 즉시 잠겼습니다.",
    };
  }
  return {
    canEnter: true,
    mutationsLocked: false,
    lockCode: "OK",
    lockReason: null,
  };
}

// =============================================================================
// Approval Gate Evaluation (T-406)
// =============================================================================

export interface ClaimApprovalGateResult {
  canApprove: boolean;
  requiresUserConfirmedAck: boolean;
  needsRevalidation: boolean;
  blockReasons: string[];
  perAtomBlockReasons: Array<{
    atom_id: string;
    ordinal: number;
    atom_type: ClaimAtomType;
    status: ClaimAtomStatus;
    reason: string;
  }>;
}

export function evaluateClaimApprovalGate(
  claim: ClaimDetailResponse,
  options: {
    acknowledgeUserConfirmed: boolean;
    workspaceMutationsLocked: boolean;
  }
): ClaimApprovalGateResult {
  const needsRevalidation =
    claim.status === "DRAFT" ||
    claim.status === "DRAFT_INVALID" ||
    claim.status === "STALE" ||
    Boolean(claim.needs_revalidation);

  const perAtomBlockReasons: ClaimApprovalGateResult["perAtomBlockReasons"] = [];
  for (const atom of claim.atoms) {
    if (atom.status === "UNSUPPORTED" || atom.status === "STALE") {
      perAtomBlockReasons.push({
        atom_id: atom.atom_id,
        ordinal: atom.ordinal,
        atom_type: atom.atom_type,
        status: atom.status,
        reason:
          atom.block_reason ??
          (atom.status === "UNSUPPORTED"
            ? `Atom #${atom.ordinal} (${atom.atom_type})에 유효한 DIRECT/USER_CONFIRMED 근거가 없습니다.`
            : `Atom #${atom.ordinal} (${atom.atom_type})의 하위 근거가 만료(STALE)되어 재검증이 필요합니다.`),
      });
    }
  }

  const requiresUserConfirmedAck =
    claim.computed_verification === "USER_CONFIRMED" ||
    claim.computed_verification === "MIXED" ||
    claim.user_confirmed_atom_count > 0;

  const blockReasons: string[] = [];

  if (options.workspaceMutationsLocked) {
    blockReasons.push(
      "정책 차단 또는 삭제 펜스(POLICY_BLOCKED / DELETION_PENDING)로 승인할 수 없습니다."
    );
  }
  if (claim.status === "APPROVED") {
    blockReasons.push("이미 승인(APPROVED)된 Claim입니다.");
  }
  if (claim.status === "REJECTED" || claim.status === "SUPERSEDED") {
    blockReasons.push(
      `현재 워크플로 상태(${claim.status})에서는 승인할 수 없습니다.`
    );
  }
  if (needsRevalidation) {
    blockReasons.push(
      "편집 또는 변경 후 재검증 필요(Needs Revalidation): 먼저 'Run Atomization & Support Validation'을 실행하세요."
    );
  }
  if (!claim.approvable) {
    for (const reason of claim.approval_block_reasons) {
      if (!blockReasons.includes(reason)) {
        blockReasons.push(reason);
      }
    }
    if (claim.approval_block_reasons.length === 0 && !needsRevalidation) {
      blockReasons.push(
        "서버 승인 가능 조건(approvable=true)을 만족하지 않아 승인할 수 없습니다."
      );
    }
  }
  for (const item of perAtomBlockReasons) {
    if (!blockReasons.includes(item.reason)) {
      blockReasons.push(item.reason);
    }
  }

  const ackSatisfied =
    !requiresUserConfirmedAck || options.acknowledgeUserConfirmed;

  const canApprove =
    !options.workspaceMutationsLocked &&
    claim.approvable &&
    !needsRevalidation &&
    perAtomBlockReasons.length === 0 &&
    claim.status === "SUPPORT_VALIDATED" &&
    ackSatisfied;

  return {
    canApprove,
    requiresUserConfirmedAck,
    needsRevalidation,
    blockReasons,
    perAtomBlockReasons,
  };
}

// =============================================================================
// CLAIM-06 6-Element Metric Pills & CLAIM-05 Role Grounding
// =============================================================================

export interface MetricPillItem {
  key: keyof MetricDetailsDto;
  label: string;
  value: string | null;
  present: boolean;
}

export function extractMetricSixElementPills(
  source: Partial<MetricDetailsDto> | FactNormalizedValue | null | undefined
): {
  pills: MetricPillItem[];
  completeCount: number;
  isComplete: boolean;
} {
  const nested =
    source && "metric_details" in source && source.metric_details
      ? source.metric_details
      : (source as Partial<MetricDetailsDto> | null | undefined);

  const readField = (key: keyof MetricDetailsDto): string | null => {
    const raw =
      nested?.[key] ??
      (source && key in source
        ? (source as Record<string, unknown>)[key]
        : undefined);
    if (typeof raw === "string" && raw.trim().length > 0) {
      return raw.trim();
    }
    return null;
  };

  const fields: Array<{ key: keyof MetricDetailsDto; label: string }> = [
    { key: "value", label: "수치 (value)" },
    { key: "unit", label: "단위 (unit)" },
    { key: "period", label: "기간 (period)" },
    { key: "source_basis", label: "측정 근거 (source_basis)" },
    { key: "environment_scope", label: "환경 (environment_scope)" },
    { key: "measurement_scope", label: "측정 범위 (measurement_scope)" },
  ];

  const pills: MetricPillItem[] = fields.map(({ key, label }) => {
    const value = readField(key);
    const present =
      value !== null &&
      !(key === "environment_scope" && value === "UNSPECIFIED");
    return { key, label, value, present };
  });

  const completeCount = pills.filter((p) => p.present).length;
  return {
    pills,
    completeCount,
    isComplete: completeCount === 6,
  };
}

export function extractRoleGroundingSummary(atom: ClaimAtomDetailItem): {
  roleLabel: string;
  identityStatus: string;
  sourceVerifiedEligible: boolean;
  note: string;
} | null {
  if (atom.atom_type !== "ROLE" && !atom.role_grounding) {
    return null;
  }
  const rg = atom.role_grounding;
  const roleLabel = rg?.role ?? "IMPLEMENTED";
  const identityStatus = rg?.identity_status ?? "VERIFIED_PERSON";
  const sourceVerifiedEligible = rg?.can_support_source_verified ?? atom.status === "SUPPORTED";
  const note =
    rg?.note ??
    (sourceVerifiedEligible
      ? "CLAIM-05: 활성 VERIFIED PERSON 신원 및 DIRECT SUPPORTED Fact에 의해 역할이 뒷받침됩니다."
      : "CLAIM-05: 신원 미확정(HEURISTIC/CONFLICTED) 또는 직접 역할 근거 부족으로 SOURCE_VERIFIED로 승격할 수 없습니다.");
  return {
    roleLabel,
    identityStatus,
    sourceVerifiedEligible,
    note,
  };
}

// =============================================================================
// Plan C Ephemeral Source Viewer 6-State Classifier
// =============================================================================

export interface EphemeralViewerViewModel {
  uiState: EphemeralViewerUiState;
  badgeLabel: string;
  badgeClass: string;
  bannerTone: "info" | "success" | "warning" | "danger" | "neutral";
  headline: string;
  explanation: string;
  canRetry: boolean;
  resolution: SourceResolutionMetadata | null;
  content: string | null;
  context: EphemeralContentResponse["context"];
}

export function buildEphemeralViewerStateFromSuccess(
  response: EphemeralContentResponse
): EphemeralViewerViewModel {
  return {
    uiState: "MATCHED",
    badgeLabel: "일치 확인 (MATCHED)",
    badgeClass: "badge--success",
    bannerTone: "success",
    headline: "원본 리비전 해시 및 위치(cm-locator-v1) 일치 확인 — 메모리 전용 표시",
    explanation:
      "이 원문은 Plan C(ADR-013)에 따라 React 컴포넌트 메모리에만 임시 유지되며 localStorage, sessionStorage, URL 또는 콘솔 로그에 저장되지 않습니다.",
    canRetry: false,
    resolution: response.source_resolution,
    content: response.content,
    context: response.context,
  };
}

export function buildEphemeralViewerStateFromError(
  error: ClaimReviewError
): EphemeralViewerViewModel {
  const sr = error.sourceResolution ?? null;
  const status = sr?.status;

  if (
    status === "REVISION_MISMATCH" ||
    status === "INVALID_LOCATOR" ||
    error.code === "SOURCE_REVISION_MISMATCH" ||
    error.code === "SOURCE_LOCATOR_INVALID"
  ) {
    const isLocator =
      status === "INVALID_LOCATOR" || error.code === "SOURCE_LOCATOR_INVALID";
    return {
      uiState: "CHANGED",
      badgeLabel: isLocator
        ? "위치 변경/불일치 (INVALID_LOCATOR)"
        : "원문 변경 감지 (REVISION_MISMATCH)",
      badgeClass: "badge--warning",
      bannerTone: "warning",
      headline: isLocator
        ? "저장된 스팬 위치(cm-locator-v1)가 현재 원문 구조와 일치하지 않습니다"
        : "현재 GitHub 원문이 과거 관측 리비전 해시와 달라졌습니다 (드리프트 감지)",
      explanation:
        "이는 과거 관측 시점 대비 현재 원문이 변경되었음을 뜻하며, 과거 기록이 허위이거나 사용자가 삭제했다는 의미가 아닙니다. 역사적 리비전 해시는 덮어쓰지 않고 보존됩니다.",
      canRetry: false,
      resolution: sr,
      content: null,
      context: null,
    };
  }

  if (
    status === "POLICY_BLOCKED" ||
    error.kind === "policy_blocked" ||
    error.kind === "deletion_pending"
  ) {
    return {
      uiState: "POLICY_BLOCKED",
      badgeLabel: "정책 차단 (POLICY_BLOCKED)",
      badgeClass: "badge--danger",
      bannerTone: "danger",
      headline: "공개 정책 변경·권한 회수 또는 삭제 펜스로 원문 열람이 차단되었습니다",
      explanation:
        error.message ||
        "원문 재조회 전 정책 펜스에서 차단되었습니다(원본 삭제와 별도). 메타데이터만 안전하게 표시합니다.",
      canRetry: false,
      resolution: sr,
      content: null,
      context: null,
    };
  }

  if (
    status === "PARTIAL_WITH_LIMIT" ||
    error.kind === "source_limit" ||
    error.code === "SOURCE_FETCH_LIMIT"
  ) {
    return {
      uiState: "LIMIT",
      badgeLabel: "조회 한도 초과 (PARTIAL_WITH_LIMIT)",
      badgeClass: "badge--warning",
      bannerTone: "warning",
      headline: "원문 조회 크기 또는 요청 예산 한도에 도달하여 임의 절단 없이 중단했습니다",
      explanation:
        error.message ||
        "무음 절단(silent truncation)을 방지하기 위해 원문을 표시하지 않았습니다. 출처 위치 메타데이터와 해시는 계속 확인할 수 있습니다.",
      canRetry: false,
      resolution: sr,
      content: null,
      context: null,
    };
  }

  // UNAVAILABLE (distinguish retryable 429/503/timeout vs provider 404/410 without claiming user deletion or historical falsity)
  const retryable = Boolean(sr?.retryable ?? error.retryable);
  const isProviderNotFound =
    sr?.reason_code === "PROVIDER_OBJECT_NOT_FOUND" || !retryable;

  return {
    uiState: "UNAVAILABLE",
    badgeLabel: retryable
      ? "일시적 조회 불가 (UNAVAILABLE · 재시도 가능)"
      : "공급자 응답 없음 (UNAVAILABLE · 404/410)",
    badgeClass: "badge--neutral",
    bannerTone: "neutral",
    headline: retryable
      ? "원본 공급자 일시 오류(429/503/시간 초과)로 지금 원문을 가져올 수 없습니다"
      : "현재 공급자에서 해당 객체를 조회할 수 없습니다 (404/410 — 사용자 삭제나 허위 단정 아님)",
    explanation: retryable
      ? "일시적인 네트워크 또는 공급자 제한입니다. 과거 기록의 진실성이나 사용자 삭제를 의미하지 않으며 잠시 후 다시 시도할 수 있습니다."
      : isProviderNotFound
        ? "공급자 측 404/410 응답은 권한 변경·리포지토리 이동·히스토리 재작성 등 여러 원인이 있을 수 있으므로 사용자 삭제(OD-003)나 과거 사실의 허위로 분류하지 않습니다."
        : error.message,
    canRetry: retryable,
    resolution: sr,
    content: null,
    context: null,
  };
}

// =============================================================================
// Export Policy Validation
// =============================================================================

export interface ExportSelectionEvaluation {
  valid: boolean;
  eligibleClaims: ClaimSummaryItem[];
  ineligibleReason: string | null;
  requiresLimitationNotice: boolean;
  limitationNotice: string | null;
}

export function evaluateExportSelection(
  selectedClaims: ClaimSummaryItem[],
  policy: ExportPolicy
): ExportSelectionEvaluation {
  if (selectedClaims.length === 0) {
    return {
      valid: false,
      eligibleClaims: [],
      ineligibleReason: "Export할 승인(APPROVED) Claim을 1개 이상 선택하세요.",
      requiresLimitationNotice: false,
      limitationNotice: null,
    };
  }

  const nonApproved = selectedClaims.filter((c) => c.status !== "APPROVED");
  if (nonApproved.length > 0) {
    return {
      valid: false,
      eligibleClaims: [],
      ineligibleReason: `승인되지 않은 Claim(${nonApproved.map((c) => c.claim_id).join(", ")})은 Export할 수 없습니다.`,
      requiresLimitationNotice: false,
      limitationNotice: null,
    };
  }

  const hasUserConfirmed = selectedClaims.some(
    (c) =>
      c.computed_verification === "USER_CONFIRMED" ||
      c.computed_verification === "MIXED"
  );
  const hasUnverified = selectedClaims.some(
    (c) => c.computed_verification === "UNVERIFIED"
  );

  if (hasUnverified) {
    return {
      valid: false,
      eligibleClaims: [],
      ineligibleReason: "미검증(UNVERIFIED) Claim은 Export에 포함할 수 없습니다.",
      requiresLimitationNotice: false,
      limitationNotice: null,
    };
  }

  if (policy === "SOURCE_VERIFIED_ONLY" && hasUserConfirmed) {
    return {
      valid: false,
      eligibleClaims: [],
      ineligibleReason:
        "기본 정책(SOURCE_VERIFIED_ONLY)에서는 USER_CONFIRMED / MIXED Claim을 포함할 수 없습니다. 포함하려면 ALLOW_USER_CONFIRMED 정책을 명시적으로 선택하세요.",
      requiresLimitationNotice: false,
      limitationNotice: null,
    };
  }

  const requiresLimitationNotice =
    policy === "ALLOW_USER_CONFIRMED" && hasUserConfirmed;

  return {
    valid: true,
    eligibleClaims: selectedClaims,
    ineligibleReason: null,
    requiresLimitationNotice,
    limitationNotice: requiresLimitationNotice
      ? "한계 고지: 선택된 항목 중 사용자 확인(USER_CONFIRMED/MIXED) 근거에 의존하는 Claim이 포함됩니다. Export 매니페스트에 해당 검증 수준과 비인용 출처 메타데이터(URL · locator · hash · observed_at · checked_at)가 함께 기록됩니다."
      : null,
  };
}
