import type {
  DerivedStageViewModel,
  ImportRunViewModel,
  ProcessingStatusViewModel,
  PublicRepositoryOption,
  StreamProgressViewModel,
} from "@/domain/view-models";

/**
 * Synthetic HTTP transport types and OpenAPI 0.2.0-draft (CONTRACT commit
 * a0314f2cbcb33f1d34ed486db7b1009dcb4adc59) DTO definitions & pure mappers.
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
  /** Raw body string; empty string or undefined for bodyless 401/403/409/422. */
  rawBody?: string;
}

export interface SyntheticHttpTransport {
  send(request: SyntheticHttpRequest): Promise<SyntheticHttpResponse>;
}

/** OpenAPI 0.2.0-draft DTOs for the 3 sync-run paths ONLY */
export interface InitialImportRequestDto {
  source_resource_id: string;
  mode: "INITIAL_BACKFILL";
  backfill_from?: string;
  limits?: {
    max_objects?: number;
  };
}

export interface SyncRunAcceptedDto {
  job_id?: string;
  status?: "PENDING";
  target_type?: "SYNC_RUN";
  target_id: string;
  status_url: string;
}

export type OpenApiImportState =
  | "PENDING"
  | "RUNNING"
  | "SUCCESS"
  | "PARTIAL"
  | "PARTIAL_WITH_LIMIT"
  | "FAILED"
  | "CANCELED";

export type OpenApiProcessingStatus = ProcessingStatusViewModel;

export interface StreamProgressDto {
  type: string;
  status: string;
  last_committed_page?: number;
  unresolved_failures?: number;
}

export interface ImportStatusDto {
  status: OpenApiImportState;
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

export interface ImportConflictDto {
  error: {
    code: "IDEMPOTENCY_CONFLICT" | "SYNC_ALREADY_RUNNING";
    details?: {
      existing_run_id?: string;
    };
  };
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

export function isValidRelativeStatusUrl(statusUrl: string): boolean {
  return /^\/v1\/sync-runs\/[^/?#]+$/.test(statusUrl);
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
    status: "running",
    attemptNumber: params.attemptNumber,
    startedAt: params.previousRun?.startedAt ?? params.nowIso,
    updatedAt: params.nowIso,
    progressMessage: params.isRetry
      ? "재시도 요청이 접수되었습니다(202 Accepted). 아직 완료되지 않았으며 기존 실행 상태를 다시 조회합니다."
      : "최초 가져오기 요청이 접수되었습니다(202 Accepted). 아직 완료되지 않았으며 상태 조회를 시작합니다.",
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
      reason: "가져오기 요청이 접수되어 실행 대기/진행 중이므로 재시도할 수 없습니다.",
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

  // Enforce contract invariant: PARTIAL / PARTIAL_WITH_LIMIT / FAILED / CANCELED / PENDING / RUNNING => import_ready=false
  const importReady = dto.status === "SUCCESS" ? Boolean(dto.import_ready) : false;

  const base = {
    runId,
    statusUrl,
    planId: dto.plan_id,
    repositoryId: repo.repoId,
    repositoryFullName: repo.fullName,
    attemptNumber,
    startedAt,
    updatedAt,
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
        reason: "가져오기 작업이 진행 중이므로 재시도할 수 없습니다.",
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
      ? "가져온 원본 스냅샷 이력(SUCCESS / import_ready=true)은 보존되어 있지만, 현재 저장소 정책 변경으로 후속 처리가 차단되었습니다(BLOCKED_POLICY)."
      : isBlockedSource
        ? "가져온 원본 스냅샷 이력(SUCCESS / import_ready=true)은 보존되어 있지만, 현재 원본 저장소 상태·접근 차단으로 후속 처리가 중단되었습니다(BLOCKED_SOURCE)."
        : null;

    let progressMessage =
      "원본 스냅샷 가져오기(import_ready=true)와 후속 처리(COMPLETE)가 모두 완료되었습니다.";
    if (isBlockedPolicy) {
      progressMessage =
        "과거 최초 가져오기는 성공(import_ready=true)했으나, 현재 정책 변경으로 후속 처리가 차단(BLOCKED_POLICY)되어 있습니다.";
    } else if (isBlockedSource) {
      progressMessage =
        "과거 최초 가져오기는 성공(import_ready=true)했으나, 현재 원본 저장소 차단(BLOCKED_SOURCE)으로 후속 처리가 중단되어 있습니다.";
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
        "수집 상한에 도달한 부분 결과(PARTIAL_WITH_LIMIT)입니다. 성공이 아니며 같은 계획으로 재시도할 수 없습니다.",
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
          "PARTIAL_WITH_LIMIT 결과는 서버 정책상 같은 계획에서 재시도가 허용되지 않습니다 (retry.allowed=false).",
        nextAllowedAt: null,
      },
      summary: null,
      partialDetail: null,
      limitDetail: {
        kind: "import_cap_reached",
        limitLabel: `수집 상한 도달 (${dto.failure_reason ?? "SYNC_PARTIAL_WITH_LIMIT"})`,
        capturedCount: 200,
        capCount: 200,
        resetAt: null,
        explanation: [
          dto.processed_range ? `처리된 범위: ${dto.processed_range}` : null,
          dto.remaining_range ? `남은 범위: ${dto.remaining_range}` : null,
          "전체 가져오기가 완료되지 않았으므로 성공으로 표시하지 않습니다.",
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

    const omissionReasons: string[] = [`실패 사유 코드: ${failureReason}`];
    if (dto.source_drift) {
      omissionReasons.push(
        "계획 불일치(PLAN_DRIFT / source_drift=true)로 동일 계획 재개가 불가능합니다."
      );
    }
    if (dto.processed_range) {
      omissionReasons.push(`처리된 범위: ${dto.processed_range}`);
    }
    if (dto.remaining_range) {
      omissionReasons.push(`남은 범위: ${dto.remaining_range}`);
    }

    const importedCommits = Math.max(10, Math.floor(repo.estimatedCommits * 0.72));
    const skippedCommits = repo.estimatedCommits - importedCommits;
    const importedPrs = Math.max(2, Math.floor(repo.estimatedPullRequests * 0.65));
    const skippedPrs = repo.estimatedPullRequests - importedPrs;

    return {
      ...base,
      status: "partial",
      progressMessage:
        "일부 데이터만 수집된 부분 결과(PARTIAL)입니다. import_ready=false이며 전체 성공으로 처리되지 않습니다.",
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
              ? "안전한 체크포인트(from_safe_checkpoint=true)에서 동일 실행 재시도가 허용되었습니다."
              : "서버가 동일 실행 재시도를 허용했습니다.",
            nextAllowedAt: null,
          }
        : {
            allowed: false,
            reason: dto.source_drift
              ? "계획 변경(PLAN_DRIFT)으로 같은 계획에서 재시도할 수 없습니다 (retry.allowed=false)."
              : `서버 정책상 재시도가 허용되지 않습니다 (${failureReason}, retry.allowed=false).`,
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

  // FAILED or CANCELED
  const isCanceled = dto.status === "CANCELED";
  const failureCode = dto.failure_reason ?? (isCanceled ? "CANCELED" : "FAILED");

  return {
    ...base,
    status: "failed",
    progressMessage: isCanceled
      ? "최초 가져오기 작업이 취소되었습니다(CANCELED)."
      : "최초 가져오기에 실패했습니다(FAILED).",
    readiness: {
      importReady: false,
      importReadyState: "failed",
      importReadySummary: isCanceled
        ? "작업 취소됨 (CANCELED · import_ready=false)"
        : `가져오기 실패 (FAILED · ${failureCode} · import_ready=false)`,
      processingStatus: dto.processing_status,
      processingBlockedMessage: null,
      ...derived,
    },
    retryPolicy: {
      allowed: false,
      reason: isCanceled
        ? "취소된 작업은 재시도할 수 없습니다 (retry.allowed=false)."
        : `서버가 재시도를 허용하지 않았습니다 (${failureCode}, retry.allowed=false).`,
      nextAllowedAt: null,
    },
    summary: null,
    partialDetail: null,
    limitDetail: null,
    error: {
      code: failureCode,
      title: isCanceled ? "가져오기 취소됨" : "가져오기 실패",
      message: isCanceled
        ? "작업이 취소 상태로 종료되었습니다."
        : `서버가 실패 상태(${failureCode})를 반환했습니다.`,
      actionGuidance:
        "서버 정책상 이 실행에서는 재시도할 수 없습니다. 상태와 저장소 설정을 확인하세요.",
    },
  };
}
