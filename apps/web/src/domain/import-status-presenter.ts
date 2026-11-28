import type {
  ContractImportState,
  ImportPlanScopeViewModel,
  ImportRunViewModel,
  PublicRepositoryOption,
} from "./view-models";

/**
 * Pure presentation helpers for T-205 initial import progress, structured
 * failure reasons, fixed plan scope, and policy/source block classification.
 */

export const DEFAULT_PLAN_BACKFILL_FROM = "2025-09-23";
export const DEFAULT_PLAN_CUTOFF_AT = "2026-10-05";
export const DEFAULT_PLAN_MAX_OBJECTS = 5000;

export interface StructuredFailureReasonInfo {
  code: string;
  label: string;
  description: string;
  isPolicyOrSourceBlock: boolean;
}

export function buildDefaultPlanScope(
  repo?: PublicRepositoryOption | null,
  observedAt?: string | null
): ImportPlanScopeViewModel {
  const cutoffDate =
    observedAt && observedAt.length >= 10
      ? observedAt.slice(0, 10)
      : repo?.lastPushedAt && repo.lastPushedAt.length >= 10
        ? repo.lastPushedAt.slice(0, 10)
        : DEFAULT_PLAN_CUTOFF_AT;

  return {
    backfillFrom: DEFAULT_PLAN_BACKFILL_FROM,
    cutoffAt: cutoffDate,
    windowLabel: "기본 최근 12개월 고정 계획",
    maxObjects: DEFAULT_PLAN_MAX_OBJECTS,
    storageClass: "PUBLIC_CLOUD",
    rawStorageMode: "NONE",
  };
}

export function resolveContractStatus(
  run: ImportRunViewModel
): ContractImportState {
  if (run.contractStatus) {
    return run.contractStatus;
  }
  switch (run.status) {
    case "running":
      return run.readiness.importReadyState === "pending"
        ? "PENDING"
        : "RUNNING";
    case "succeeded":
      return "SUCCESS";
    case "partial":
      return "PARTIAL";
    case "limit_reached":
      return "PARTIAL_WITH_LIMIT";
    case "canceled":
      return "CANCELED";
    case "failed":
    case "permission_revoked":
      return "FAILED";
  }
}

export function describeFailureReason(
  code?: string | null,
  sourceDrift?: boolean
): StructuredFailureReasonInfo | null {
  const normalizedCode = code?.trim() || (sourceDrift ? "PLAN_DRIFT" : "");
  if (!normalizedCode) {
    return null;
  }

  switch (normalizedCode) {
    case "PLAN_DRIFT":
    case "IMPORT_SOURCE_DRIFT":
      return {
        code: normalizedCode,
        label: "계획 변동 (PLAN_DRIFT)",
        description:
          "기본 브랜치·HEAD 변경 또는 2차 PR 열거 변동이 감지되어 고정 계획 스냅샷의 일관성을 유지할 수 없습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "INCOMPLETE_PR_ASSOCIATION":
      return {
        code: normalizedCode,
        label: "PR 연결 미확정 (UNKNOWN PR 연결)",
        description:
          "커밋과 Pull Request 간의 연결 상태가 UNKNOWN으로 남아 있어 완전한 스냅샷으로 승격할 수 없습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "CURSOR_CYCLE_DETECTED":
      return {
        code: normalizedCode,
        label: "페이지 커서 순환 감지 (CURSOR_CYCLE_DETECTED)",
        description:
          "GitHub 페이지네이션 응답에서 중복·순환 커서가 감지되어 안전을 위해 수집을 중단했습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "MALFORMED_OR_PARTIAL_PAGE":
      return {
        code: normalizedCode,
        label: "비정상·불완전 페이지 응답 (MALFORMED_OR_PARTIAL_PAGE)",
        description:
          "공급자 페이지 응답 형식이 손상되었거나 불완전하여 해당 페이지를 커밋하지 않고 중단했습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "RATE_LIMITED":
      return {
        code: normalizedCode,
        label: "GitHub API 호출 한도 초과 (RATE_LIMITED)",
        description:
          "GitHub API 요청 한도에 도달해 수집이 일시 중단되었습니다. 마지막 확정 커밋 페이지까지 안전하게 보존되었습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "TRANSIENT_PROVIDER_ERROR":
      return {
        code: normalizedCode,
        label: "일시적 공급자 오류 (TRANSIENT_PROVIDER_ERROR)",
        description:
          "외부 공급자(GitHub)의 일시적 오류로 중단되었습니다. 안전 체크포인트가 확인된 경우 같은 계획에서 재개할 수 있습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "SYNC_PARTIAL_WITH_LIMIT":
      return {
        code: normalizedCode,
        label: "초기 가져오기 항목 상한 도달 (SYNC_PARTIAL_WITH_LIMIT)",
        description:
          "고정 계획의 최대 항목 한도(max_objects, 기본 5,000건)에 도달하여 남은 범위가 수집되지 않았습니다.",
        isPolicyOrSourceBlock: false,
      };
    case "SOURCE_POLICY_BLOCKED":
    case "POLICY_REVIEW_REQUIRED":
      return {
        code: normalizedCode,
        label: "정책·공개 상태 확인 실패 (POLICY_REVIEW_REQUIRED)",
        description:
          "저장소가 비공개(Public → Private)로 전환되었거나 공개 정책 확인에 실패했습니다. GitHub 원본이 삭제된 것으로 처리하지 않고 차단했습니다.",
        isPolicyOrSourceBlock: true,
      };
    case "SOURCE_PERMISSION_REVOKED":
    case "MOCK_GITHUB_PERMISSION_REVOKED":
      return {
        code: normalizedCode,
        label: "GitHub 수집 권한 회수 (SOURCE_PERMISSION_REVOKED)",
        description:
          "GitHub OAuth App 최소 읽기 연결(read:user) 권한이 회수되어 원문 접근 및 후속 처리를 차단했습니다.",
        isPolicyOrSourceBlock: true,
      };
    default:
      return {
        code: normalizedCode,
        label: `구조화된 실패 사유 (${normalizedCode})`,
        description: `서버가 실패 사유 코드 ${normalizedCode}를 반환했습니다.`,
        isPolicyOrSourceBlock: false,
      };
  }
}

export function isPolicyOrSourceBlockedRun(run: ImportRunViewModel): boolean {
  if (
    run.readiness.processingStatus === "BLOCKED_POLICY" ||
    run.readiness.processingStatus === "BLOCKED_SOURCE"
  ) {
    return true;
  }
  if (run.status === "permission_revoked") {
    return true;
  }
  const code = run.failureReason ?? run.error?.code ?? null;
  return (
    code === "SOURCE_POLICY_BLOCKED" ||
    code === "POLICY_REVIEW_REQUIRED" ||
    code === "SOURCE_PERMISSION_REVOKED" ||
    code === "MOCK_GITHUB_PERMISSION_REVOKED"
  );
}
