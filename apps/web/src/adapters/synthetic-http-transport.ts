import {
  buildDefaultPlanScope,
  DEFAULT_PLAN_MAX_OBJECTS,
  describeFailureReason,
} from "@/domain/import-status-presenter";
import type {
  ContractImportState,
  DerivedStageViewModel,
  ImportRunViewModel,
  ProcessingStatusViewModel,
  PublicRepositoryOption,
  StreamProgressViewModel,
} from "@/domain/view-models";

/**
 * Synthetic HTTP transport types and OpenAPI 0.5.0-draft (CONTRACT commit
 * 87f1515cbe74bf02b0aece68904bf9a641c74c60) DTO definitions & pure mappers.
 *
 * Strictly synthetic / in-memory — NO real network calls are performed here.
 */

export interface SyntheticHttpRequest {
  method: "GET" | "POST";
  path: string;
  headers: Record<string, string>;
  body?: string;
}

export interface SyntheticHttpResponse {
  status: number;
  headers?: Record<string, string>;
  /** Raw body string; empty string or undefined for bodyless fixture errors. */
  rawBody?: string;
}

export interface SyntheticHttpTransport {
  send(request: SyntheticHttpRequest): Promise<SyntheticHttpResponse>;
}

/** OpenAPI 0.5.0-draft DTOs for the 4 sync-run paths ONLY */
export interface InitialImportRequestDto {
  source_resource_id: string;
  mode: "INITIAL_BACKFILL";
  backfill_from?: string;
  limits?: {
    max_objects?: number;
  };
}

export interface RetryImportRequestDto {
  expected_run_version: number;
}

export interface SyncRunAcceptedDto {
  job_id?: string;
  status?: "PENDING";
  target_type?: "SYNC_RUN";
  target_id: string;
  status_url: string;
}

export type OpenApiImportState = ContractImportState;

export type OpenApiProcessingStatus = ProcessingStatusViewModel;

export interface StreamProgressDto {
  type: string;
  status: string;
  last_committed_page?: number;
  unresolved_failures?: number;
}

export interface ImportStatusDto {
  status: OpenApiImportState;
  /** Optimistic concurrency version attributed to this run (integer >= 1). */
  run_version?: number;
  plan_id: string;
  failure_reason?: string;
  streams?: StreamProgressDto[];
  import_ready: boolean;
  processing_status: OpenApiProcessingStatus;
  observed_at?: string;
  source_drift: boolean;
  processed_range?: string;
  remaining_range?: string;
  retry: {
    allowed: boolean;
    from_safe_checkpoint?: true;
  };
}

export interface ImportErrorEnvelopeDto {
  error: {
    code: string;
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: {
      existing_run_id?: string;
    };
  };
}

export interface ImportConflictDto {
  error: {
    code:
      | "IDEMPOTENCY_CONFLICT"
      | "SYNC_ALREADY_RUNNING"
      | "IDEMPOTENCY_KEY_EXPIRED"
      | "RESOURCE_VERSION_CONFLICT";
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: {
      existing_run_id?: string;
    };
  };
}

export interface RecoveredSyncRunItemDto {
  target_id: string;
  status_url: string;
  import_status: ImportStatusDto;
}

export interface RecoveredSyncRunsDto {
  items: RecoveredSyncRunItemDto[];
}

export function isValidRunVersion(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}

const VALID_IMPORT_STATES = new Set<string>([
  "PENDING",
  "RUNNING",
  "SUCCESS",
  "PARTIAL",
  "PARTIAL_WITH_LIMIT",
  "FAILED",
  "CANCELED",
]);

const VALID_PROCESSING_STATUSES = new Set<string>([
  "NOT_STARTED",
  "RUNNING",
  "BLOCKED_POLICY",
  "BLOCKED_SOURCE",
  "FAILED",
  "COMPLETE",
]);

export function isValidImportStatusDto(
  value: unknown
): value is ImportStatusDto {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.status !== "string" ||
    !VALID_IMPORT_STATES.has(candidate.status)
  ) {
    return false;
  }
  if (typeof candidate.plan_id !== "string" || candidate.plan_id.trim().length === 0) {
    return false;
  }
  if (typeof candidate.import_ready !== "boolean") {
    return false;
  }
  if (
    typeof candidate.processing_status !== "string" ||
    !VALID_PROCESSING_STATUSES.has(candidate.processing_status)
  ) {
    return false;
  }
  if (typeof candidate.source_drift !== "boolean") {
    return false;
  }
  if (
    !candidate.retry ||
    typeof candidate.retry !== "object" ||
    Array.isArray(candidate.retry) ||
    typeof (candidate.retry as Record<string, unknown>).allowed !== "boolean"
  ) {
    return false;
  }
  return true;
}

export const UUID_V4_REGEX =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function generateUuidV4(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  // RFC 4122 v4 fallback for environments without crypto.randomUUID
  const template = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx";
  return template.replace(/[xy]/g, (char) => {
    const rand = Math.floor(Math.random() * 16);
    const value = char === "x" ? rand : (rand & 0x3) | 0x8;
    return value.toString(16);
  });
}

/**
 * Safely parse optional JSON response body.
 * Returns `{ empty: true }` when body is absent or whitespace-only so bodyless
 * 401/403/409/422 responses NEVER throw JSON syntax errors.
 */
export function parseOptionalJsonBody<T>(
  rawBody?: string
): { empty: true; value: null } | { empty: false; value: T | null; invalidJson: boolean } {
  if (rawBody === undefined || rawBody === null || rawBody.trim() === "") {
    return { empty: true, value: null };
  }
  try {
    return { empty: false, value: JSON.parse(rawBody) as T, invalidJson: false };
  } catch {
    return { empty: false, value: null, invalidJson: true };
  }
}

export function extractRunIdFromRelativeStatusUrl(
  statusUrl: string
): string | null {
  if (typeof statusUrl !== "string") {
    return null;
  }
  const match = statusUrl.match(/^\/v1\/sync-runs\/([^/?#]+)$/);
  if (!match) {
    return null;
  }
  try {
    const decoded = decodeURIComponent(match[1]);
    if (
      decoded.trim().length === 0 ||
      decoded === "." ||
      decoded === ".." ||
      decoded.includes("/") ||
      decoded.includes("\\")
    ) {
      return null;
    }
    return decoded;
  } catch {
    return null;
  }
}

export function isValidRelativeStatusUrl(statusUrl: string): boolean {
  return extractRunIdFromRelativeStatusUrl(statusUrl) !== null;
}

export function doesStatusUrlMatchTargetId(
  statusUrl: string,
  targetId: string
): boolean {
  if (typeof targetId !== "string" || targetId.trim().length === 0) {
    return false;
  }
  const extractedRunId = extractRunIdFromRelativeStatusUrl(statusUrl);
  return extractedRunId !== null && extractedRunId === targetId;
}

function mapStreams(streams?: StreamProgressDto[]): StreamProgressViewModel[] | undefined {
  if (!streams || streams.length === 0) {
    return undefined;
  }
  return streams.map((stream) => ({
    type: stream.type,
    status: stream.status,
    ...(stream.last_committed_page !== undefined
      ? { lastCommittedPage: stream.last_committed_page }
      : {}),
    ...(stream.unresolved_failures !== undefined
      ? { unresolvedFailures: stream.unresolved_failures }
      : {}),
  }));
}

function buildDerivedStagesFromProcessingStatus(
  processingStatus: OpenApiProcessingStatus
): { derivedAllReady: boolean; derivedStages: DerivedStageViewModel[] } {
  switch (processingStatus) {
    case "COMPLETE":
      return {
        derivedAllReady: true,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "ready",
            detail: "후속 처리 완료 (processing_status=COMPLETE)",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "ready",
            detail: "후속 처리 완료 (processing_status=COMPLETE)",
          },
        ],
      };

    case "RUNNING":
      return {
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "in_progress",
            detail: "후속 처리가 진행 중입니다 (processing_status=RUNNING).",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "pending",
            detail: "후속 처리 완료를 대기 중입니다.",
          },
        ],
      };

    case "BLOCKED_POLICY":
      return {
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "skipped",
            detail:
              "현재 공개 정책 차단(BLOCKED_POLICY)으로 후속 처리가 중단되었습니다.",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "skipped",
            detail:
              "현재 공개 정책 차단(BLOCKED_POLICY)으로 후속 처리가 중단되었습니다.",
          },
        ],
      };

    case "BLOCKED_SOURCE":
      return {
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "skipped",
            detail:
              "현재 원본 저장소 상태 차단(BLOCKED_SOURCE)으로 후속 처리가 중단되었습니다.",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "skipped",
            detail:
              "현재 원본 저장소 상태 차단(BLOCKED_SOURCE)으로 후속 처리가 중단되었습니다.",
          },
        ],
      };

    case "FAILED":
      return {
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "ready",
            detail: "원본 스냅샷 기준 초기 정규화는 완료되었습니다.",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "failed",
            detail:
              "후속 처리 실패(processing_status=FAILED). 원본 스냅샷(import_ready) 자체는 보존되어 있습니다.",
          },
        ],
      };

    case "NOT_STARTED":
    default:
      return {
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "not_started",
            detail: "후속 처리가 아직 시작되지 않았습니다 (NOT_STARTED).",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "not_started",
            detail: "후속 처리가 아직 시작되지 않았습니다 (NOT_STARTED).",
          },
        ],
      };
  }
}

export function buildAcceptedPendingRunViewModel(params: {
  accepted: SyncRunAcceptedDto;
  repo: PublicRepositoryOption;
  attemptNumber: number;
  nowIso: string;
  previousRun?: ImportRunViewModel | null;
  isRetry?: boolean;
}): ImportRunViewModel {
  return {
    runId: params.accepted.target_id,
    statusUrl: params.accepted.status_url,
    planId: params.previousRun?.planId,
    repositoryId: params.repo.repoId,
    repositoryFullName: params.repo.fullName,
    repositoryVisibility: "PUBLIC",
    planScope:
      params.previousRun?.planScope ??
      buildDefaultPlanScope(params.repo, params.nowIso),
    contractStatus: "PENDING",
    status: "running",
    attemptNumber: params.attemptNumber,
    startedAt: params.previousRun?.startedAt ?? params.nowIso,
    observedAt: null,
    completedAt: null,
    updatedAt: params.nowIso,
    progressMessage: params.isRetry
      ? "재시도 요청이 접수되었습니다(PENDING · 202 Accepted). 아직 완료되지 않았으며 기존 실행 상태를 다시 조회합니다."
      : "최초 가져오기 요청이 접수되었습니다(PENDING · 202 Accepted). 아직 완료되지 않았으며 상태 조회를 시작합니다.",
    failureReason: null,
    sourceDrift: false,
    processedRange: params.previousRun?.processedRange ?? null,
    remainingRange: params.previousRun?.remainingRange ?? null,
    streams: params.previousRun?.streams,
    readiness: {
      importReady: false,
      importReadyState: "pending",
      importReadySummary:
        "요청 접수됨(PENDING, 202 Accepted) — 아직 완료되지 않았습니다 (import_ready=false).",
      processingStatus: "NOT_STARTED",
      processingBlockedMessage: null,
      ...buildDerivedStagesFromProcessingStatus("NOT_STARTED"),
    },
    retryPolicy: {
      allowed: false,
      reason: "가져오기 요청이 접수되어 실행 대기 중(PENDING)이므로 재시도할 수 없습니다.",
      nextAllowedAt: null,
    },
    summary: null,
    partialDetail: null,
    limitDetail: null,
    error: null,
  };
}

export function mapImportStatusDtoToViewModel(params: {
  runId: string;
  statusUrl: string;
  dto: ImportStatusDto;
  repo: PublicRepositoryOption;
  attemptNumber: number;
  startedAt: string;
  nowIso: string;
}): ImportRunViewModel {
  const { dto, runId, statusUrl, repo, attemptNumber, startedAt, nowIso } = params;
  const updatedAt = dto.observed_at ?? nowIso;
  const streams = mapStreams(dto.streams);
  const derived = buildDerivedStagesFromProcessingStatus(dto.processing_status);
  const isActiveState = dto.status === "PENDING" || dto.status === "RUNNING";

  // Enforce contract invariant: PARTIAL / PARTIAL_WITH_LIMIT / FAILED / CANCELED / PENDING / RUNNING => import_ready=false
  const importReady = dto.status === "SUCCESS" ? Boolean(dto.import_ready) : false;

  const runVersion = isValidRunVersion(dto.run_version)
    ? dto.run_version
    : undefined;

  const isPolicyOrSourceBlocked =
    dto.processing_status === "BLOCKED_POLICY" ||
    dto.processing_status === "BLOCKED_SOURCE" ||
    dto.failure_reason === "SOURCE_POLICY_BLOCKED" ||
    dto.failure_reason === "POLICY_REVIEW_REQUIRED" ||
    dto.failure_reason === "SOURCE_PERMISSION_REVOKED";

  const base = {
    runId,
    ...(runVersion !== undefined ? { runVersion } : {}),
    statusUrl,
    planId: dto.plan_id,
    repositoryId: repo.repoId,
    repositoryFullName: repo.fullName,
    repositoryVisibility: isPolicyOrSourceBlocked
      ? ("PRIVATE_OR_RESTRICTED" as const)
      : ("PUBLIC" as const),
    planScope: buildDefaultPlanScope(repo, dto.observed_at ?? nowIso),
    contractStatus: dto.status,
    attemptNumber,
    startedAt,
    observedAt: dto.observed_at ?? null,
    completedAt: isActiveState ? null : updatedAt,
    updatedAt,
    failureReason: dto.failure_reason ?? null,
    sourceDrift: dto.source_drift,
    processedRange: dto.processed_range ?? null,
    remainingRange: dto.remaining_range ?? null,
    streams,
  };

  if (dto.status === "PENDING" || dto.status === "RUNNING") {
    return {
      ...base,
      status: "running",
      progressMessage:
        dto.status === "PENDING"
          ? "최초 가져오기 작업이 대기 중입니다(PENDING). 완료 전까지 상태를 계속 조회합니다."
          : "공개 저장소 원본 스냅샷을 수집 중입니다(RUNNING).",
      readiness: {
        importReady: false,
        importReadyState: dto.status === "PENDING" ? "pending" : "in_progress",
        importReadySummary:
          dto.status === "PENDING"
            ? "스냅샷 수집 대기 중 (PENDING · import_ready=false)"
            : "원본 스냅샷 수집 진행 중 (RUNNING · import_ready=false)",
        processingStatus: dto.processing_status,
        processingBlockedMessage: null,
        ...derived,
      },
      retryPolicy: {
        allowed: false,
        reason:
          dto.status === "PENDING"
            ? "가져오기 작업이 대기 중(PENDING)이므로 재시도할 수 없습니다."
            : "가져오기 작업이 실행 중(RUNNING)이므로 재시도할 수 없습니다.",
        nextAllowedAt: null,
      },
      summary: null,
      partialDetail: null,
      limitDetail: null,
      error: null,
    };
  }

  if (dto.status === "SUCCESS") {
    const isBlockedPolicy = dto.processing_status === "BLOCKED_POLICY";
    const isBlockedSource = dto.processing_status === "BLOCKED_SOURCE";
    const processingBlockedMessage = isBlockedPolicy
      ? "가져온 원본 스냅샷 이력(SUCCESS / import_ready=true)은 보존되어 있지만, 현재 정책 또는 공개 상태 확인 실패(BLOCKED_POLICY · POLICY_REVIEW_REQUIRED)로 원문 열람 · 외부 AI 전송 · Export가 즉시 차단되었습니다(GitHub 원본이 삭제된 것으로 처리하지 않으며 재확인/재연결이 필요합니다)."
      : isBlockedSource
        ? "가져온 원본 스냅샷 이력(SUCCESS / import_ready=true)은 보존되어 있지만, 현재 원본 저장소 상태·접근 차단(BLOCKED_SOURCE · Public → Private 전환 또는 권한 회수)으로 원문 열람 · 외부 AI 전송 · Export가 즉시 차단되었습니다(GitHub 원본이 삭제된 것으로 처리하지 않으며 재확인/재연결이 필요합니다)."
        : null;

    let progressMessage =
      "원본 스냅샷 가져오기(import_ready=true)와 후속 처리(COMPLETE)가 모두 완료되었습니다.";
    if (isBlockedPolicy) {
      progressMessage =
        "과거 최초 가져오기는 성공(import_ready=true)했으나, 현재 정책 변경으로 원문 열람·외부 AI 전송·Export가 차단(BLOCKED_POLICY)되어 있습니다.";
    } else if (isBlockedSource) {
      progressMessage =
        "과거 최초 가져오기는 성공(import_ready=true)했으나, 현재 원본 저장소 차단(BLOCKED_SOURCE)으로 원문 열람·외부 AI 전송·Export가 차단되어 있습니다.";
    } else if (dto.processing_status === "RUNNING") {
      progressMessage =
        "원본 스냅샷 가져오기(import_ready=true)는 완료되었으며, 후속 처리가 진행 중입니다(RUNNING).";
    } else if (dto.processing_status === "FAILED") {
      progressMessage =
        "원본 스냅샷(import_ready=true)은 준비되었으나, 후속 처리가 실패(FAILED)하여 확인이 필요합니다.";
    } else if (dto.processing_status === "NOT_STARTED") {
      progressMessage =
        "원본 스냅샷(import_ready=true)은 준비되었으며, 후속 처리는 아직 시작되지 않았습니다(NOT_STARTED).";
    }

    return {
      ...base,
      status: "succeeded",
      progressMessage,
      readiness: {
        importReady,
        importReadyState: "ready",
        importReadySummary:
          "원본 스냅샷 가져오기 이력 완료 (SUCCESS · import_ready=true)",
        processingStatus: dto.processing_status,
        processingBlockedMessage,
        ...derived,
      },
      retryPolicy: {
        allowed: false,
        reason:
          "일회성 최초 가져오기가 이미 성공(SUCCESS)하여 재시도 대상이 아닙니다.",
        nextAllowedAt: null,
      },
      summary: {
        commitsCaptured: repo.estimatedCommits,
        pullRequestsCaptured: repo.estimatedPullRequests,
        capturedAt: updatedAt,
      },
      partialDetail: null,
      limitDetail: null,
      error: null,
    };
  }

  if (dto.status === "PARTIAL_WITH_LIMIT") {
    return {
      ...base,
      status: "limit_reached",
      progressMessage:
        "수집 상한에 도달한 부분 결과(PARTIAL_WITH_LIMIT)입니다. 성공이 아니며 같은 실행에서 단순 재개할 수 없고 Event/Claim으로 승격되지 않습니다.",
      readiness: {
        importReady: false,
        importReadyState: "partial",
        importReadySummary:
          "수집 상한 도달로 미완료 (PARTIAL_WITH_LIMIT · import_ready=false)",
        processingStatus: dto.processing_status,
        processingBlockedMessage: null,
        ...derived,
      },
      retryPolicy: {
        allowed: false,
        reason:
          "PARTIAL_WITH_LIMIT 결과는 같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다 (retry.allowed=false).",
        nextAllowedAt: null,
      },
      summary: null,
      partialDetail: null,
      limitDetail: {
        kind: "import_cap_reached",
        limitLabel: `수집 상한 도달 (${dto.failure_reason ?? "SYNC_PARTIAL_WITH_LIMIT"})`,
        capturedCount: DEFAULT_PLAN_MAX_OBJECTS,
        capCount: DEFAULT_PLAN_MAX_OBJECTS,
        resetAt: null,
        explanation: [
          dto.processed_range ? `가져온 범위: ${dto.processed_range}` : null,
          dto.remaining_range ? `빠진/남은 범위: ${dto.remaining_range}` : null,
          "최대 항목 한도(max_objects: 5,000건)에 도달해 전체 가져오기가 완료되지 않았으므로 성공으로 표시하거나 Event/Claim으로 승격하지 않습니다.",
        ]
          .filter(Boolean)
          .join(" · "),
      },
      error: null,
    };
  }

  if (dto.status === "PARTIAL") {
    const retryAllowed = dto.retry.allowed === true;
    const fromSafeCheckpoint = dto.retry.from_safe_checkpoint === true;
    const failureReason = dto.failure_reason ?? "PARTIAL";
    const reasonInfo = describeFailureReason(failureReason, dto.source_drift);

    const omissionReasons: string[] = [
      reasonInfo
        ? `실패 사유: ${reasonInfo.label} (${reasonInfo.code}) — ${reasonInfo.description}`
        : `실패 사유 코드: ${failureReason}`,
    ];
    if (dto.source_drift) {
      omissionReasons.push(
        "계획 변동(PLAN_DRIFT / source_drift=true): 기본 브랜치·HEAD 변경 또는 2차 PR 열거 변동으로 같은 실행에서 단순 재개할 수 없습니다."
      );
    }
    if (dto.processed_range) {
      omissionReasons.push(`가져온 범위: ${dto.processed_range}`);
    }
    if (dto.remaining_range) {
      omissionReasons.push(`빠진/남은 범위: ${dto.remaining_range}`);
    }

    const importedCommits = Math.max(10, Math.floor(repo.estimatedCommits * 0.72));
    const skippedCommits = repo.estimatedCommits - importedCommits;
    const importedPrs = Math.max(2, Math.floor(repo.estimatedPullRequests * 0.65));
    const skippedPrs = repo.estimatedPullRequests - importedPrs;

    return {
      ...base,
      status: "partial",
      progressMessage:
        "일부 데이터만 수집된 부분 결과(PARTIAL)입니다. import_ready=false이며 전체 성공으로 처리되거나 Event/Claim으로 승격되지 않습니다.",
      readiness: {
        importReady: false,
        importReadyState: "partial",
        importReadySummary: `부분 결과 미준비 (PARTIAL · ${failureReason} · import_ready=false)`,
        processingStatus: dto.processing_status,
        processingBlockedMessage: null,
        ...derived,
      },
      retryPolicy: retryAllowed
        ? {
            allowed: true,
            fromSafeCheckpoint,
            reason: fromSafeCheckpoint
              ? "안전 체크포인트(from_safe_checkpoint=true)가 확인된 일시 실패입니다. 같은 계획·마지막 안전 체크포인트에서 재개할 수 있습니다."
              : "서버가 동일 실행 재개를 허용했습니다.",
            nextAllowedAt: null,
          }
        : {
            allowed: false,
            reason: dto.source_drift
              ? "계획 변동(PLAN_DRIFT)으로 같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다 (retry.allowed=false)."
              : `같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다 (${failureReason}, retry.allowed=false).`,
            nextAllowedAt: null,
          },
      summary: null,
      partialDetail: {
        importedCommits,
        skippedCommits,
        importedPullRequests: importedPrs,
        skippedPullRequests: skippedPrs,
        omissionReasons,
      },
      limitDetail: null,
      error: null,
    };
  }

  if (dto.status === "CANCELED") {
    return {
      ...base,
      status: "canceled",
      progressMessage:
        "최초 가져오기 작업이 취소(CANCELED)되어 중립 종료되었습니다. import_ready=false이며 부분 수집 결과는 Event/Claim으로 승격되지 않습니다.",
      readiness: {
        importReady: false,
        importReadyState: "failed",
        importReadySummary: "작업 취소됨 (CANCELED · import_ready=false)",
        processingStatus: dto.processing_status,
        processingBlockedMessage: null,
        ...derived,
      },
      retryPolicy: {
        allowed: false,
        reason:
          "취소된 작업(CANCELED)은 같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다 (retry.allowed=false).",
        nextAllowedAt: null,
      },
      summary: null,
      partialDetail: null,
      limitDetail: null,
      error: null,
    };
  }

  // FAILED
  const failureCode = dto.failure_reason ?? "FAILED";
  const reasonInfo = describeFailureReason(failureCode, dto.source_drift);

  return {
    ...base,
    status: "failed",
    progressMessage: "최초 가져오기에 실패했습니다(FAILED).",
    readiness: {
      importReady: false,
      importReadyState: "failed",
      importReadySummary: `가져오기 실패 (FAILED · ${failureCode} · import_ready=false)`,
      processingStatus: dto.processing_status,
      processingBlockedMessage: null,
      ...derived,
    },
    retryPolicy: {
      allowed: false,
      reason: `같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다 (${failureCode}, retry.allowed=false).`,
      nextAllowedAt: null,
    },
    summary: null,
    partialDetail: null,
    limitDetail: null,
    error: {
      code: failureCode,
      title: reasonInfo ? reasonInfo.label : "가져오기 실패",
      message: reasonInfo
        ? reasonInfo.description
        : `서버가 실패 상태(${failureCode})를 반환했습니다.`,
      actionGuidance:
        "같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간 미제공), 상태와 저장소 정책·연결 설정을 확인하세요.",
    },
  };
}
