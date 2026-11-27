import type {
  ImportRunViewModel,
  PendingUnconfirmedRetryViewModel,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";
import type { AdapterResult } from "./import-service-adapter";
import {
  buildAcceptedPendingRunViewModel,
  doesStatusUrlMatchTargetId,
  generateUuidV4,
  isValidImportStatusDto,
  isValidRunVersion,
  mapImportStatusDtoToViewModel,
  parseOptionalJsonBody,
  type ImportErrorEnvelopeDto,
  type ImportStatusDto,
  type InitialImportRequestDto,
  type RecoveredSyncRunsDto,
  type RetryImportRequestDto,
  type SyncRunAcceptedDto,
  type SyntheticHttpResponse,
  type SyntheticHttpTransport,
} from "./synthetic-http-transport";

/**
 * OpenAPI 0.5.0-draft (CONTRACT commit 87f1515cbe74bf02b0aece68904bf9a641c74c60)
 * client for ONLY the 4 sync-run endpoints:
 *   - POST /v1/sync-runs
 *   - GET  /v1/sync-runs?source_resource_id=...&limit=1
 *   - GET  <status_url> (/v1/sync-runs/{id})
 *   - POST /v1/sync-runs/{id}/retry (JSON {"expected_run_version": <int >= 1>})
 *
 * Pending Idempotency-Keys and retry bodies are kept strictly in memory (never
 * persisted across reloads) and cleared on auth/session/workspace/connection/target
 * changes. Late responses from previous scopes never revive cleared caches or keys.
 */

export interface ContractRunSyncResult {
  run: ImportRunViewModel;
  recoveryNotice?: RecoveryNoticeViewModel | null;
  pendingRetry?: PendingUnconfirmedRetryViewModel | null;
}

export interface ContractRecoveryLookupResult {
  run: ImportRunViewModel | null;
  pendingRetry?: PendingUnconfirmedRetryViewModel | null;
}

interface PendingCreateKey {
  authScope: string;
  repoId: string;
  requestFingerprint: string;
  idempotencyKey: string;
}

interface PendingRetryKey {
  authScope: string;
  repoId: string;
  runId: string;
  expectedRunVersion: number;
  requestBody: string;
  idempotencyKey: string;
}

type UnverifiableAcceptedReason =
  | "missing_required_fields"
  | "status_url_mismatch"
  | "retry_target_mismatch";

function isServerErrorStatus(status: number): boolean {
  return status >= 500 && status <= 599;
}

function isSafeRunId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

export class ContractSyncRunClient {
  private readonly transport: SyntheticHttpTransport;
  private readonly keyGenerator: () => string;
  private readonly nowProvider: () => string;

  private readonly statusUrlByRunId = new Map<string, string>();
  private readonly runVersionByRunId = new Map<string, number>();
  private readonly runMetadata = new Map<
    string,
    { repo: PublicRepositoryOption; attemptNumber: number; startedAt: string }
  >();

  private currentAuthScope: string;
  private scopeGeneration = 0;
  private pendingCreateKey: PendingCreateKey | null = null;
  private pendingRetryKey: PendingRetryKey | null = null;
  private requestCounter = 0;

  constructor(options: {
    transport: SyntheticHttpTransport;
    keyGenerator?: () => string;
    nowProvider?: () => string;
    initialAuthScope?: string;
  }) {
    this.transport = options.transport;
    this.keyGenerator = options.keyGenerator ?? generateUuidV4;
    this.nowProvider = options.nowProvider ?? (() => new Date().toISOString());
    this.currentAuthScope = options.initialAuthScope ?? "default";
  }

  public setAuthScope(authScope: string): void {
    if (authScope !== this.currentAuthScope) {
      this.currentAuthScope = authScope;
      this.scopeGeneration += 1;
      this.clearAllCachesAndPendingKeys();
    }
  }

  public clearAllCachesAndPendingKeys(): void {
    this.clearPendingKeys();
    this.statusUrlByRunId.clear();
    this.runVersionByRunId.clear();
    this.runMetadata.clear();
  }

  public clearPendingKeysForTargetChange(nextRepoId: string): void {
    if (this.pendingCreateKey && this.pendingCreateKey.repoId !== nextRepoId) {
      this.pendingCreateKey = null;
    }
    if (this.pendingRetryKey && this.pendingRetryKey.repoId !== nextRepoId) {
      this.pendingRetryKey = null;
    }
  }

  public registerKnownRun(
    run: ImportRunViewModel,
    repo: PublicRepositoryOption
  ): void {
    const statusUrl =
      run.statusUrl && doesStatusUrlMatchTargetId(run.statusUrl, run.runId)
        ? run.statusUrl
        : `/v1/sync-runs/${encodeURIComponent(run.runId)}`;
    this.statusUrlByRunId.set(run.runId, statusUrl);
    if (isValidRunVersion(run.runVersion)) {
      this.runVersionByRunId.set(run.runId, run.runVersion);
    } else {
      this.runVersionByRunId.delete(run.runId);
    }
    this.runMetadata.set(run.runId, {
      repo,
      attemptNumber: run.attemptNumber,
      startedAt: run.startedAt,
    });
  }

  public getStatusUrl(runId: string): string {
    return (
      this.statusUrlByRunId.get(runId) ??
      `/v1/sync-runs/${encodeURIComponent(runId)}`
    );
  }

  public getKnownRunVersion(runId: string): number | undefined {
    return this.runVersionByRunId.get(runId);
  }

  public getPendingRetrySummary(
    authScope?: string
  ): PendingUnconfirmedRetryViewModel | null {
    if (!this.pendingRetryKey) {
      return null;
    }
    const effectiveAuthScope = authScope ?? this.currentAuthScope;
    if (this.pendingRetryKey.authScope !== effectiveAuthScope) {
      return null;
    }
    return {
      runId: this.pendingRetryKey.runId,
      expectedRunVersion: this.pendingRetryKey.expectedRunVersion,
    };
  }

  public clearPendingKeys(): void {
    this.pendingCreateKey = null;
    this.pendingRetryKey = null;
  }

  private nextRequestId(): string {
    this.requestCounter += 1;
    return `synthetic-web-req-${this.requestCounter}`;
  }

  private parseErrorEnvelope(rawBody?: string): {
    code: string | null;
    message?: string;
    retryable?: boolean;
    requestId?: string;
    existingRunId?: string;
  } {
    const parsed = parseOptionalJsonBody<ImportErrorEnvelopeDto>(rawBody);
    if (
      parsed.empty ||
      parsed.invalidJson ||
      !parsed.value ||
      typeof parsed.value !== "object" ||
      Array.isArray(parsed.value) ||
      !parsed.value.error ||
      typeof parsed.value.error !== "object" ||
      typeof parsed.value.error.code !== "string" ||
      parsed.value.error.code.trim().length === 0
    ) {
      return { code: null };
    }
    const err = parsed.value.error;
    return {
      code: err.code,
      message: typeof err.message === "string" ? err.message : undefined,
      retryable: typeof err.retryable === "boolean" ? err.retryable : undefined,
      requestId: typeof err.request_id === "string" ? err.request_id : undefined,
      existingRunId:
        typeof err.details?.existing_run_id === "string"
          ? err.details.existing_run_id
          : undefined,
    };
  }

  private validateAcceptedResponse(
    rawBody: string | undefined,
    expectedRunId?: string
  ):
    | { ok: true; accepted: SyncRunAcceptedDto }
    | { ok: false; reason: UnverifiableAcceptedReason } {
    const parsed = parseOptionalJsonBody<SyncRunAcceptedDto>(rawBody);
    if (
      parsed.empty ||
      parsed.invalidJson ||
      !parsed.value ||
      typeof parsed.value !== "object" ||
      Array.isArray(parsed.value)
    ) {
      return { ok: false, reason: "missing_required_fields" };
    }

    const { target_id: targetId, status_url: statusUrl } = parsed.value;
    if (
      typeof targetId !== "string" ||
      targetId.trim().length === 0 ||
      typeof statusUrl !== "string" ||
      statusUrl.trim().length === 0
    ) {
      return { ok: false, reason: "missing_required_fields" };
    }

    if (!doesStatusUrlMatchTargetId(statusUrl, targetId)) {
      return { ok: false, reason: "status_url_mismatch" };
    }

    if (expectedRunId !== undefined && targetId !== expectedRunId) {
      return { ok: false, reason: "retry_target_mismatch" };
    }

    return { ok: true, accepted: parsed.value };
  }

  private buildUncertainServerErrorFault(
    status: number,
    operation: "create" | "retry" | "poll"
  ): UnconfirmedFaultViewModel {
    if (operation === "poll") {
      return {
        kind: "uncertain_outcome",
        httpStatus: status,
        title: `상태 조회 응답 미확정 (HTTP ${status})`,
        message: `서버 오류 응답(HTTP ${status})으로 실행 상태를 확인할 수 없습니다. 성공이나 확정 실패로 표시하지 않았으며, 새 가져오기 요청은 보내지 않았습니다.`,
        serverConfirmed: false,
        actionGuidance:
          "새 가져오기를 다시 요청하지 말고 잠시 후 상태를 직접 다시 조회해 주세요.",
      };
    }

    const actionLabel =
      operation === "retry" ? "이전 미확정 재시도" : "동일 저장소 최초 가져오기";

    return {
      kind: "uncertain_outcome",
      httpStatus: status,
      title: `서버 응답 미확정 — 처리 여부 확인 필요 (HTTP ${status})`,
      message: `서버 오류 응답(HTTP ${status})으로 ${actionLabel} 처리 여부를 확인할 수 없습니다. 이미 서버에서 접수되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.`,
      serverConfirmed: false,
      actionGuidance:
        operation === "retry"
          ? "먼저 '서버 상태 다시 확인'(GET)으로 최신 상태를 조회하세요. 이전 미확정 재시도를 그대로 다시 보내려면 별도의 재전송 버튼을 사용하세요(원래 요청이 미접수였다면 새 작업이 시작될 수 있습니다)."
          : `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
    };
  }

  private buildUnverifiableAcceptedFault(
    operation: "create" | "retry",
    reason: UnverifiableAcceptedReason
  ): UnconfirmedFaultViewModel {
    const actionLabel =
      operation === "retry" ? "이전 미확정 재시도" : "동일 저장소 최초 가져오기";

    if (reason === "retry_target_mismatch") {
      return {
        kind: "uncertain_outcome",
        httpStatus: 202,
        title: "재시도 접수 응답의 실행 ID 불일치 — 처리 여부 미확정 (HTTP 202)",
        message:
          "202 응답의 target_id가 재시도 대상 원래 실행 ID와 일치하지 않아 접수 결과를 검증할 수 없습니다. 해당 경로로 조회하거나 다른 실행으로 이동하지 않았으며, 성공이나 확정 실패로 표시하지 않습니다.",
        serverConfirmed: false,
        actionGuidance:
          "다른 실행으로 이동하지 않았습니다. 먼저 GET으로 서버 상태를 확인하거나, 별도의 재전송 버튼으로 이전 미확정 재시도를 그대로 다시 보낼 수 있습니다.",
      };
    }

    if (reason === "status_url_mismatch") {
      return {
        kind: "uncertain_outcome",
        httpStatus: 202,
        title: "접수 응답 경로 불일치 — 처리 여부 미확정 (HTTP 202)",
        message:
          "202 응답의 status_url이 target_id와 같은 실행을 가리키지 않아 접수 결과를 검증할 수 없습니다. 해당 경로로 조회하거나 다른 실행으로 이동하지 않았으며, 성공이나 확정 실패로 표시하지 않습니다.",
        serverConfirmed: false,
        actionGuidance:
          operation === "retry"
            ? "먼저 GET으로 서버 상태를 확인하거나, 별도의 재전송 버튼으로 이전 미확정 재시도를 그대로 다시 보낼 수 있습니다."
            : `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 기존 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
      };
    }

    return {
      kind: "uncertain_outcome",
      httpStatus: 202,
      title: "접수 응답 검증 불가 — 처리 여부 미확정 (HTTP 202)",
      message:
        "202 응답에 필수 필드(target_id, status_url)가 누락되었거나 형식을 확인할 수 없어 접수 결과를 검증할 수 없습니다. 이미 서버에서 처리되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.",
      serverConfirmed: false,
      actionGuidance:
        operation === "retry"
          ? "먼저 GET으로 서버 상태를 확인하거나, 별도의 재전송 버튼으로 이전 미확정 재시도를 그대로 다시 보낼 수 있습니다."
          : `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
    };
  }

  private buildNeutralHttpFault(status: number): UnconfirmedFaultViewModel {
    if (status === 401) {
      return {
        kind: "request_rejected",
        httpStatus: 401,
        title: "인증 응답 확인 불가 — 일반 오류 (HTTP 401)",
        message:
          "서버가 SESSION_TERMINATED 오류 코드 없이 HTTP 401 응답을 반환했습니다. 확정된 세션 종료·실행 성공·미접수로 단정하지 않으며, 자동으로 다시 요청하지 않았습니다.",
        serverConfirmed: false,
        actionGuidance:
          "먼저 '서버 상태 다시 확인'(GET)으로 최신 상태를 조회하거나 인증 상태를 확인해 주세요.",
      };
    }

    if (status === 403) {
      return {
        kind: "request_rejected",
        httpStatus: 403,
        title: "요청이 거부되었습니다 (HTTP 403)",
        message:
          "서버가 상세 본문 없이 요청을 거부했습니다. 응답에 원인이 포함되지 않아 세부 사유를 특정할 수 없습니다.",
        serverConfirmed: false,
        actionGuidance:
          "로그인·연결 상태와 대상 접근 권한을 확인한 뒤 필요 시 상태를 다시 조회해 주세요.",
      };
    }

    if (status === 409) {
      return {
        kind: "request_rejected",
        httpStatus: 409,
        title: "현재 상태와 충돌하여 요청이 처리되지 않았습니다 (HTTP 409)",
        message:
          "서버가 상세 본문 없이 충돌 응답을 반환했습니다. 응답에 세부 원인이 없어 이유를 단정할 수 없습니다.",
        serverConfirmed: false,
        actionGuidance:
          "화면에 조회된 최신 상태와 서버의 재시도 가능 여부(retry.allowed)를 확인해 주세요.",
      };
    }

    if (status === 422) {
      return {
        kind: "request_rejected",
        httpStatus: 422,
        title: "요청을 처리할 수 없습니다 (HTTP 422)",
        message:
          "서버가 상세 본문 없이 유효하지 않은 요청 응답을 반환했습니다. 세부 원인은 응답에 포함되지 않았습니다.",
        serverConfirmed: false,
        actionGuidance:
          "선택한 저장소와 현재 화면 상태를 확인한 뒤 필요 시 상태를 다시 조회해 주세요.",
      };
    }

    return {
      kind: "request_rejected",
      httpStatus: status,
      title: `요청이 처리되지 않았습니다 (HTTP ${status})`,
      message:
        "서버로부터 예상하지 못한 응답을 받았습니다. 작업 시작 또는 상태 변경은 확정되지 않았습니다.",
      serverConfirmed: false,
      actionGuidance: "서버 상태를 다시 확인해 주세요.",
    };
  }

  private buildSessionTerminatedFault(
    requestId?: string
  ): UnconfirmedFaultViewModel {
    return {
      kind: "session_expired",
      httpStatus: 401,
      errorCode: "SESSION_TERMINATED",
      ...(requestId ? { requestId } : {}),
      title: "서비스 세션 종료 — 재인증 필요 (SESSION_TERMINATED)",
      message:
        "서버가 세션 종료(401 SESSION_TERMINATED)를 확정 반환하여 이전 세션의 보호 상태와 실행 상세를 정리했습니다. 자동으로 재요청하지 않았습니다.",
      serverConfirmed: false,
      actionGuidance:
        "상단에서 '모의 서비스 다시 로그인'을 완료한 뒤 다시 진행하세요.",
    };
  }

  private buildWorkspaceScopeViolationFault(
    requestId?: string
  ): UnconfirmedFaultViewModel {
    return {
      kind: "workspace_scope_violation",
      httpStatus: 403,
      errorCode: "WORKSPACE_SCOPE_VIOLATION",
      ...(requestId ? { requestId } : {}),
      title: "Workspace 접근 권한 없음 (WORKSPACE_SCOPE_VIOLATION)",
      message:
        "현재 Workspace에 대한 접근 권한이 없어 Workspace 저장소 목록·실행 상세·자동 상태 조회를 닫았습니다. Career Memory 서비스 로그인은 유지됩니다.",
      serverConfirmed: false,
      actionGuidance:
        "자동 재요청을 보내지 않았습니다. 권한이 있는 세션/연결로 전환하거나 접근 권한을 확인해 주세요.",
    };
  }

  private buildResourceNotFoundFault(
    requestId?: string
  ): UnconfirmedFaultViewModel {
    return {
      kind: "resource_not_found",
      httpStatus: 404,
      errorCode: "RESOURCE_NOT_FOUND",
      ...(requestId ? { requestId } : {}),
      title: "대상을 찾을 수 없거나 접근할 수 없습니다 (RESOURCE_NOT_FOUND)",
      message:
        "요청한 저장소 또는 실행을 찾을 수 없거나 접근할 수 없습니다. 대상이 존재하지 않는지 권한이 없는지는 구별하지 않으며, 이전 대상 상세 표시를 중단했습니다.",
      serverConfirmed: false,
      actionGuidance:
        "자동 재요청을 보내지 않았습니다. 저장소 목록과 현재 상태를 다시 확인해 주세요.",
    };
  }

  private buildAuthVerificationUnavailableFault(
    requestId?: string
  ): UnconfirmedFaultViewModel {
    return {
      kind: "auth_verification_unavailable",
      httpStatus: 503,
      errorCode: "AUTH_VERIFICATION_UNAVAILABLE",
      ...(requestId ? { requestId } : {}),
      title: "일시적 인증 검증 장애 (AUTH_VERIFICATION_UNAVAILABLE)",
      message:
        "서버의 인증 검증 서비스가 일시적으로 응답하지 않아 요청을 완료할 수 없습니다. 서비스 로그인 상태는 유지되며, retryable 응답이어도 자동으로 POST 재전송하지 않았습니다.",
      serverConfirmed: false,
      actionGuidance:
        "로그아웃되지 않았습니다. 잠시 후 '서버 상태 다시 확인'(GET)으로 상태를 확인하거나 필요한 요청을 직접 다시 시도해 주세요.",
    };
  }

  private buildStaleSessionFault(): UnconfirmedFaultViewModel {
    return {
      kind: "uncertain_outcome",
      title: "이전 세션의 늦은 응답 무시됨",
      message:
        "요청 전송 중 로그아웃 또는 세션 변경이 발생하여 이전 요청의 늦은 응답을 현재 세션에 반영하지 않았습니다.",
      serverConfirmed: false,
      actionGuidance: "현재 세션 상태에서 필요한 작업을 진행해 주세요.",
    };
  }

  private clearRunCachesForId(runId: string): void {
    this.statusUrlByRunId.delete(runId);
    this.runVersionByRunId.delete(runId);
    this.runMetadata.delete(runId);
    if (this.pendingRetryKey?.runId === runId) {
      this.pendingRetryKey = null;
    }
  }

  private clearPendingCreateKeyIfMatch(idempotencyKey: string): void {
    if (this.pendingCreateKey?.idempotencyKey === idempotencyKey) {
      this.pendingCreateKey = null;
    }
  }

  private clearPendingRetryKeyIfMatch(idempotencyKey: string): void {
    if (this.pendingRetryKey?.idempotencyKey === idempotencyKey) {
      this.pendingRetryKey = null;
    }
  }

  private async handleNonSuccessResponse<T>(params: {
    response: SyntheticHttpResponse;
    operation: "create" | "retry" | "poll";
    repo: PublicRepositoryOption;
    runId?: string;
    fallbackAttemptNumber: number;
    matchedCreateKey?: string;
    matchedRetryKey?: string;
    attachExistingRunOnConflict?: boolean;
  }): Promise<AdapterResult<T>> {
    const { response, operation } = params;
    const envelope = this.parseErrorEnvelope(response.rawBody);

    // 503 AUTH_VERIFICATION_UNAVAILABLE -> keep login, temporary auth outage, 0 auto-POST
    if (
      response.status === 503 &&
      envelope.code === "AUTH_VERIFICATION_UNAVAILABLE"
    ) {
      return {
        ok: false,
        kind: "auth_verification_unavailable",
        fault: this.buildAuthVerificationUnavailableFault(envelope.requestId),
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    // Other 5xx -> uncertain outcome (keep pending mutation keys in memory, 0 auto-POST)
    if (isServerErrorStatus(response.status)) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildUncertainServerErrorFault(response.status, operation),
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    // 401 SESSION_TERMINATED -> confirmed session termination; clear protected caches & pending keys
    if (response.status === 401 && envelope.code === "SESSION_TERMINATED") {
      this.clearAllCachesAndPendingKeys();
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionTerminatedFault(envelope.requestId),
        pendingRetry: null,
      };
    }

    // Generic / bodyless 401 -> do NOT assume session termination, success, or non-acceptance
    if (response.status === 401) {
      return {
        ok: false,
        kind: "request_rejected",
        fault: this.buildNeutralHttpFault(401),
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    // 403 WORKSPACE_SCOPE_VIOLATION -> close Workspace data/polling while keeping login
    if (
      response.status === 403 &&
      envelope.code === "WORKSPACE_SCOPE_VIOLATION"
    ) {
      this.clearAllCachesAndPendingKeys();
      return {
        ok: false,
        kind: "workspace_scope_violation",
        workspaceAccessRevoked: true,
        fault: this.buildWorkspaceScopeViolationFault(envelope.requestId),
        pendingRetry: null,
      };
    }

    // Definitive non-5xx client/conflict response -> clear matched pending mutation key if applicable
    if (params.matchedCreateKey) {
      this.clearPendingCreateKeyIfMatch(params.matchedCreateKey);
    }
    if (params.matchedRetryKey) {
      this.clearPendingRetryKeyIfMatch(params.matchedRetryKey);
    }

    // 404 RESOURCE_NOT_FOUND (or empty 404) -> neutral message without distinguishing absent vs unauthorized; hide previous details
    if (response.status === 404) {
      if (params.runId) {
        this.clearRunCachesForId(params.runId);
      }
      return {
        ok: false,
        kind: "resource_not_found",
        clearActiveRun: true,
        fault: this.buildResourceNotFoundFault(envelope.requestId),
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (response.status === 409) {
      return this.handleConflictResponse<T>({
        envelope,
        repo: params.repo,
        fallbackAttemptNumber: params.fallbackAttemptNumber,
        attachExistingRunOnConflict: params.attachExistingRunOnConflict === true,
      });
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(response.status),
      pendingRetry: this.getPendingRetrySummary(),
    };
  }

  private async handleConflictResponse<T>(params: {
    envelope: ReturnType<ContractSyncRunClient["parseErrorEnvelope"]>;
    repo: PublicRepositoryOption;
    fallbackAttemptNumber: number;
    attachExistingRunOnConflict: boolean;
  }): Promise<AdapterResult<T>> {
    const { envelope } = params;

    // Bodyless 409 or unparseable body -> neutral generic guidance without guessing!
    if (!envelope.code) {
      return {
        ok: false,
        kind: "request_rejected",
        fault: this.buildNeutralHttpFault(409),
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (envelope.code === "RESOURCE_VERSION_CONFLICT") {
      return {
        ok: false,
        kind: "version_conflict",
        fault: {
          kind: "version_conflict",
          httpStatus: 409,
          errorCode: "RESOURCE_VERSION_CONFLICT",
          ...(envelope.requestId ? { requestId: envelope.requestId } : {}),
          title: "실행 상태 변경 충돌 (RESOURCE_VERSION_CONFLICT)",
          message:
            "서버의 실행 상태(run_version)가 변경되어 재시도 요청이 거절되었습니다. 이전 재전송이나 새 키/버전 재시도를 자동으로 보내지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 '서버 상태 다시 확인'(GET)으로 최신 실행 상태와 run_version을 갱신한 뒤 필요 시 새 재시도를 직접 요청하세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (envelope.code === "IDEMPOTENCY_CONFLICT") {
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          httpStatus: 409,
          errorCode: "IDEMPOTENCY_CONFLICT",
          ...(envelope.requestId ? { requestId: envelope.requestId } : {}),
          title: "요청 키 충돌 (IDEMPOTENCY_CONFLICT)",
          message:
            "동일한 Idempotency-Key가 다른 요청 내용(대상 또는 expected_run_version)과 충돌했습니다. 기존 실행으로 이동하거나 새 키로 자동 재전송하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 '서버 상태 다시 확인'(GET)으로 최신 상태를 조회한 뒤 필요 시 새 요청을 직접 실행하세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (envelope.code === "IDEMPOTENCY_KEY_EXPIRED") {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          httpStatus: 409,
          errorCode: "IDEMPOTENCY_KEY_EXPIRED",
          ...(envelope.requestId ? { requestId: envelope.requestId } : {}),
          title: "요청 키 보존 기간 만료 (IDEMPOTENCY_KEY_EXPIRED)",
          message:
            "이전 요청의 Idempotency-Key 보존 기간(24시간)이 만료되어 재확인할 수 없습니다. 새 키를 자동으로 발급하거나 다시 전송하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 '서버 상태 다시 확인'(GET)으로 현재 저장소 또는 실행의 최신 상태를 조회해 주세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (envelope.code === "SYNC_ALREADY_RUNNING") {
      const existingRunId = envelope.existingRunId;
      // Navigate to the existing run ONLY when a valid existing_run_id is explicitly provided!
      if (params.attachExistingRunOnConflict && isSafeRunId(existingRunId)) {
        const existingStatusUrl = `/v1/sync-runs/${encodeURIComponent(existingRunId)}`;
        this.statusUrlByRunId.set(existingRunId, existingStatusUrl);
        this.runMetadata.set(existingRunId, {
          repo: params.repo,
          attemptNumber: params.fallbackAttemptNumber,
          startedAt: this.nowProvider(),
        });

        const followUp = await this.getImportStatus({
          runId: existingRunId,
          repo: params.repo,
        });
        if (!followUp.ok) {
          return followUp as AdapterResult<T>;
        }
        return {
          ok: true,
          data: {
            run: followUp.data.run,
            recoveryNotice: {
              kind: "attached_existing_run",
              message: `이미 진행 중인 기존 실행(${existingRunId})이 반환되어 해당 실행 상태 조회로 이동했습니다.`,
              restoredAt: this.nowProvider(),
            },
            pendingRetry: this.getPendingRetrySummary(),
          } as T,
        };
      }

      // SYNC_ALREADY_RUNNING without existing_run_id -> do NOT fabricate or move!
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          httpStatus: 409,
          errorCode: "SYNC_ALREADY_RUNNING",
          ...(envelope.requestId ? { requestId: envelope.requestId } : {}),
          title: "진행 중인 가져오기 충돌 (SYNC_ALREADY_RUNNING)",
          message:
            "이미 실행 중인 가져오기가 있으나 서버가 기존 실행 ID를 제공하지 않아 다른 실행으로 이동하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 '서버 상태 다시 확인'(GET)으로 최신 상태를 확인해 주세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(409),
      pendingRetry: this.getPendingRetrySummary(),
    };
  }

  /**
   * POST /v1/sync-runs
   */
  public async createInitialImport(params: {
    repo: PublicRepositoryOption;
    authScope?: string;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const startGeneration = this.scopeGeneration;
    const startCurrentScope = this.currentAuthScope;
    const effectiveAuthScope = params.authScope ?? this.currentAuthScope;
    const sourceResourceId = params.repo.repoId;
    const bodyDto: InitialImportRequestDto = {
      source_resource_id: sourceResourceId,
      mode: "INITIAL_BACKFILL",
    };
    const requestFingerprint = JSON.stringify(bodyDto);

    // Different logical operation -> never keep unrelated retry key
    this.pendingRetryKey = null;

    const idempotencyKey =
      this.pendingCreateKey &&
      this.pendingCreateKey.authScope === effectiveAuthScope &&
      this.pendingCreateKey.requestFingerprint === requestFingerprint
        ? this.pendingCreateKey.idempotencyKey
        : this.keyGenerator();

    this.pendingCreateKey = {
      authScope: effectiveAuthScope,
      repoId: sourceResourceId,
      requestFingerprint,
      idempotencyKey,
    };

    let response: SyntheticHttpResponse;
    try {
      response = await this.transport.send({
        method: "POST",
        path: "/v1/sync-runs",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
          "X-Request-ID": this.nextRequestId(),
        },
        body: requestFingerprint,
      });
    } catch {
      if (
        this.scopeGeneration !== startGeneration ||
        this.currentAuthScope !== startCurrentScope
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildStaleSessionFault(),
        };
      }
      // Network/lost response: KEEP pendingCreateKey in memory so an explicit
      // user resend of the exact same request in the same auth scope reuses
      // the exact same UUID Idempotency-Key. Never auto-POST.
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "네트워크 통신 실패 — 서버 응답 미수신",
          message:
            "최초 가져오기 접수 응답을 받지 못했습니다. 이미 서버에서 처리되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.",
          serverConfirmed: false,
          actionGuidance:
            "같은 세션에서 동일 저장소로 직접 다시 요청하면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.",
        },
        pendingRetry: null,
      };
    }

    // If logout or session change occurred while in-flight, NEVER mutate the new
    // session's pending keys, run map, or state!
    if (
      this.scopeGeneration !== startGeneration ||
      this.currentAuthScope !== startCurrentScope
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleSessionFault(),
      };
    }

    if (response.status === 202) {
      const validated = this.validateAcceptedResponse(response.rawBody);
      if (!validated.ok) {
        // Unverifiable/mismatched 202: outcome is uncertain!
        // KEEP pendingCreateKey, do NOT poll status_url, do NOT move runs.
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildUnverifiableAcceptedFault("create", validated.reason),
          pendingRetry: null,
        };
      }

      // Valid 202 confirmed -> clear only this request's pending key so subsequent distinct requests use a new UUID.
      this.clearPendingCreateKeyIfMatch(idempotencyKey);

      const accepted = validated.accepted;
      const nowIso = this.nowProvider();
      // Track by validated target_id and matching status_url (independent of optional job_id)
      this.statusUrlByRunId.set(accepted.target_id, accepted.status_url);
      this.runVersionByRunId.delete(accepted.target_id);
      this.runMetadata.set(accepted.target_id, {
        repo: params.repo,
        attemptNumber: 1,
        startedAt: nowIso,
      });

      const run = buildAcceptedPendingRunViewModel({
        accepted,
        repo: params.repo,
        attemptNumber: 1,
        nowIso,
        isRetry: false,
      });

      return { ok: true, data: { run, pendingRetry: null } };
    }

    return this.handleNonSuccessResponse<ContractRunSyncResult>({
      response,
      operation: "create",
      repo: params.repo,
      fallbackAttemptNumber: 1,
      matchedCreateKey: idempotencyKey,
      attachExistingRunOnConflict: true,
    });
  }

  /**
   * GET <status_url> (/v1/sync-runs/{id})
   */
  public async getImportStatus(params: {
    runId: string;
    repo: PublicRepositoryOption;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const startGeneration = this.scopeGeneration;
    const startCurrentScope = this.currentAuthScope;
    const statusUrl = this.getStatusUrl(params.runId);

    let response: SyntheticHttpResponse;
    try {
      response = await this.transport.send({
        method: "GET",
        path: statusUrl,
        headers: {
          "X-Request-ID": this.nextRequestId(),
        },
      });
    } catch {
      if (
        this.scopeGeneration !== startGeneration ||
        this.currentAuthScope !== startCurrentScope
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildStaleSessionFault(),
        };
      }
      // Never convert a poll failure into a new POST /v1/sync-runs!
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "상태 조회 중 네트워크 실패",
          message:
            "실행 중인 가져오기 상태를 서버에서 가져오지 못했습니다. 새 가져오기 요청은 보내지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "새 가져오기를 다시 요청하지 말고 '상태 지금 다시 확인' 또는 '서버 상태 다시 확인'을 눌러 조회하세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (
      this.scopeGeneration !== startGeneration ||
      this.currentAuthScope !== startCurrentScope
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleSessionFault(),
      };
    }

    if (response.status === 200) {
      const parsed = parseOptionalJsonBody<ImportStatusDto>(response.rawBody);
      if (
        parsed.empty ||
        parsed.invalidJson ||
        !isValidImportStatusDto(parsed.value)
      ) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: this.buildNeutralHttpFault(200),
          pendingRetry: this.getPendingRetrySummary(),
        };
      }

      // Attribute run_version strictly to params.runId; delete if absent/invalid.
      if (isValidRunVersion(parsed.value.run_version)) {
        this.runVersionByRunId.set(params.runId, parsed.value.run_version);
      } else {
        this.runVersionByRunId.delete(params.runId);
      }

      const meta = this.runMetadata.get(params.runId) ?? {
        repo: params.repo,
        attemptNumber: 1,
        startedAt: this.nowProvider(),
      };

      const run = mapImportStatusDtoToViewModel({
        runId: params.runId,
        statusUrl,
        dto: parsed.value,
        repo: meta.repo,
        attemptNumber: meta.attemptNumber,
        startedAt: meta.startedAt,
        nowIso: this.nowProvider(),
      });

      // NOTE: Intermediate GETs MUST NEVER overwrite pendingRetryKey!
      return {
        ok: true,
        data: { run, pendingRetry: this.getPendingRetrySummary() },
      };
    }

    return this.handleNonSuccessResponse<ContractRunSyncResult>({
      response,
      operation: "poll",
      repo: params.repo,
      runId: params.runId,
      fallbackAttemptNumber: 1,
      attachExistingRunOnConflict: false,
    });
  }

  /**
   * GET /v1/sync-runs?source_resource_id=...&limit=1
   * Read-only recovery lookup when no run ID is available for the current session
   * and repository. Validates target_id / status_url agreement, attributes
   * `items[0].import_status.run_version` strictly to `target_id`, and never
   * enqueues or auto-POSTs on empty lists, 403, 404, 503, or 5xx.
   */
  public async recoverLatestRunForRepository(params: {
    repo: PublicRepositoryOption;
  }): Promise<AdapterResult<ContractRecoveryLookupResult>> {
    const startGeneration = this.scopeGeneration;
    const startCurrentScope = this.currentAuthScope;
    const path = `/v1/sync-runs?source_resource_id=${encodeURIComponent(
      params.repo.repoId
    )}&limit=1`;

    let response: SyntheticHttpResponse;
    try {
      response = await this.transport.send({
        method: "GET",
        path,
        headers: {
          "X-Request-ID": this.nextRequestId(),
        },
      });
    } catch {
      if (
        this.scopeGeneration !== startGeneration ||
        this.currentAuthScope !== startCurrentScope
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildStaleSessionFault(),
        };
      }
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "복구 상태 조회 중 네트워크 실패",
          message:
            "저장소의 최근 가져오기 이력을 서버에서 조회하지 못했습니다. 이전 요청의 실패를 확정하지 않으며, 새 가져오기 요청은 자동으로 보내지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "새 가져오기를 자동으로 재전송하지 않았습니다. 잠시 후 '서버 상태 다시 확인'을 눌러 조회해 주세요.",
        },
        pendingRetry: this.getPendingRetrySummary(),
      };
    }

    if (
      this.scopeGeneration !== startGeneration ||
      this.currentAuthScope !== startCurrentScope
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleSessionFault(),
      };
    }

    if (response.status === 200) {
      const parsed = parseOptionalJsonBody<RecoveredSyncRunsDto>(response.rawBody);
      if (
        parsed.empty ||
        parsed.invalidJson ||
        !parsed.value ||
        typeof parsed.value !== "object" ||
        Array.isArray(parsed.value) ||
        !Array.isArray(parsed.value.items) ||
        parsed.value.items.length > 1
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: {
            kind: "uncertain_outcome",
            httpStatus: 200,
            title: "복구 조회 응답 검증 불가 — 상태 미확정 (HTTP 200)",
            message:
              "복구 조회 응답 형식이 계약과 일치하지 않아 상태를 확인할 수 없습니다. 새 가져오기 요청은 보내지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "자동 재요청을 수행하지 않았습니다. 잠시 후 서버 상태를 다시 확인해 주세요.",
          },
          pendingRetry: this.getPendingRetrySummary(),
        };
      }

      if (parsed.value.items.length === 0) {
        return {
          ok: true,
          data: { run: null, pendingRetry: this.getPendingRetrySummary() },
        };
      }

      const item = parsed.value.items[0];
      if (
        !item ||
        typeof item !== "object" ||
        typeof item.target_id !== "string" ||
        item.target_id.trim().length === 0 ||
        typeof item.status_url !== "string" ||
        item.status_url.trim().length === 0 ||
        !isValidImportStatusDto(item.import_status)
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: {
            kind: "uncertain_outcome",
            httpStatus: 200,
            title: "복구 조회 응답 필수 필드 누락 — 상태 미확정 (HTTP 200)",
            message:
              "복구 조회 항목에 필수 필드(target_id, status_url, import_status)가 누락되어 상태를 검증할 수 없습니다. 해당 경로로 조회하지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "다른 실행으로 이동하지 않았습니다. 잠시 후 서버 상태를 다시 확인해 주세요.",
          },
          pendingRetry: this.getPendingRetrySummary(),
        };
      }

      if (!doesStatusUrlMatchTargetId(item.status_url, item.target_id)) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: {
            kind: "uncertain_outcome",
            httpStatus: 200,
            title: "복구 조회 응답 경로 불일치 — 상태 미확정 (HTTP 200)",
            message:
              "복구 조회 응답의 status_url이 target_id와 같은 실행을 가리키지 않아 검증에 실패했습니다. 해당 경로로 상태 조회를 이어가지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "불일치 경로로 조회하거나 새 가져오기를 자동 전송하지 않았습니다. 서버 상태를 다시 확인해 주세요.",
          },
          pendingRetry: this.getPendingRetrySummary(),
        };
      }

      this.statusUrlByRunId.set(item.target_id, item.status_url);
      // Attribute run_version strictly to item.target_id
      if (isValidRunVersion(item.import_status.run_version)) {
        this.runVersionByRunId.set(item.target_id, item.import_status.run_version);
      } else {
        this.runVersionByRunId.delete(item.target_id);
      }

      const existingMeta = this.runMetadata.get(item.target_id);
      const startedAt =
        existingMeta?.startedAt ??
        item.import_status.observed_at ??
        this.nowProvider();
      const attemptNumber = existingMeta?.attemptNumber ?? 1;
      this.runMetadata.set(item.target_id, {
        repo: params.repo,
        attemptNumber,
        startedAt,
      });

      const run = mapImportStatusDtoToViewModel({
        runId: item.target_id,
        statusUrl: item.status_url,
        dto: item.import_status,
        repo: params.repo,
        attemptNumber,
        startedAt,
        nowIso: this.nowProvider(),
      });

      return {
        ok: true,
        data: { run, pendingRetry: this.getPendingRetrySummary() },
      };
    }

    return this.handleNonSuccessResponse<ContractRecoveryLookupResult>({
      response,
      operation: "poll",
      repo: params.repo,
      fallbackAttemptNumber: 1,
      attachExistingRunOnConflict: false,
    });
  }

  /**
   * POST /v1/sync-runs/{id}/retry
   * OpenAPI 0.5.0-draft: JSON {"expected_run_version": <int >= 1>} + UUID Idempotency-Key.
   *
   * Two explicitly separated modes:
   * - `mode === "new_retry"` (default): starts a NEW retry against the latest
   *   observed run_version using a fresh UUID Idempotency-Key. Sends 0 POST if
   *   run_version is missing/invalid or target mismatches.
   * - `mode === "resend_pending"`: explicitly resends the EXACT previously
   *   unconfirmed retry (same runId, same Idempotency-Key, same original
   *   expected_run_version body — never overwritten by intermediate GETs).
   */
  public async retryInitialImport(params: {
    runId: string;
    repo: PublicRepositoryOption;
    expectedRunVersion?: number;
    mode?: "new_retry" | "resend_pending";
    previousRun?: ImportRunViewModel | null;
    authScope?: string;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const startGeneration = this.scopeGeneration;
    const startCurrentScope = this.currentAuthScope;
    const effectiveAuthScope = params.authScope ?? this.currentAuthScope;
    const mode = params.mode ?? "new_retry";

    // Different logical operation -> never keep unrelated create key
    this.pendingCreateKey = null;

    let idempotencyKey: string;
    let expectedRunVersion: number;
    let requestBody: string;

    if (mode === "resend_pending") {
      if (
        !this.pendingRetryKey ||
        this.pendingRetryKey.authScope !== effectiveAuthScope ||
        this.pendingRetryKey.runId !== params.runId
      ) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: {
            kind: "request_rejected",
            title: "재전송할 미확정 재시도 요청이 없습니다",
            message:
              "현재 세션과 대상 실행에 일치하는 이전 미확정 재시도 키·본문이 없어 POST 요청을 보내지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "먼저 '서버 상태 다시 확인'(GET)으로 최신 실행 상태를 조회한 뒤 필요 시 새 재시도를 실행하세요.",
          },
          pendingRetry: this.getPendingRetrySummary(effectiveAuthScope),
        };
      }

      idempotencyKey = this.pendingRetryKey.idempotencyKey;
      expectedRunVersion = this.pendingRetryKey.expectedRunVersion;
      requestBody = this.pendingRetryKey.requestBody;
    } else {
      // Target attribution check: never borrow a version from a different run!
      if (params.previousRun && params.previousRun.runId !== params.runId) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: {
            kind: "request_rejected",
            title: "재시도 대상 실행 ID 불일치 — 요청 차단",
            message:
              "화면에 표시된 실행과 재시도 요청 대상 실행 ID가 일치하지 않아 POST 요청을 보내지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "먼저 '서버 상태 다시 확인'(GET)으로 현재 실행의 최신 상태와 버전을 다시 조회해 주세요.",
          },
          pendingRetry: this.getPendingRetrySummary(effectiveAuthScope),
        };
      }

      const candidateVersion =
        params.expectedRunVersion !== undefined
          ? params.expectedRunVersion
          : params.previousRun !== undefined && params.previousRun !== null
            ? params.previousRun.runVersion
            : this.runVersionByRunId.get(params.runId);

      // OpenAPI 0.5.0-draft: never send bodyless retry, default 1, timestamp, or guessed version!
      if (!isValidRunVersion(candidateVersion)) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: {
            kind: "request_rejected",
            title: "실행 버전(run_version) 확인 필요 — 재시도 요청 차단",
            message:
              "현재 실행의 유효한 run_version(1 이상의 정수)이 확인되지 않아 POST 재시도 요청을 보내지 않았습니다.",
            serverConfirmed: false,
            actionGuidance:
              "먼저 '서버 상태 다시 확인'(GET)으로 최신 실행 상태와 run_version을 조회한 뒤 다시 시도해 주세요.",
          },
          pendingRetry: this.getPendingRetrySummary(effectiveAuthScope),
        };
      }

      expectedRunVersion = candidateVersion;
      idempotencyKey = this.keyGenerator();
      const bodyDto: RetryImportRequestDto = {
        expected_run_version: expectedRunVersion,
      };
      requestBody = JSON.stringify(bodyDto);
    }

    const candidatePendingRetry: PendingRetryKey = {
      authScope: effectiveAuthScope,
      repoId: params.repo.repoId,
      runId: params.runId,
      expectedRunVersion,
      requestBody,
      idempotencyKey,
    };

    let response: SyntheticHttpResponse;
    try {
      response = await this.transport.send({
        method: "POST",
        path: `/v1/sync-runs/${encodeURIComponent(params.runId)}/retry`,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
          "X-Request-ID": this.nextRequestId(),
        },
        body: requestBody,
      });
    } catch {
      if (
        this.scopeGeneration !== startGeneration ||
        this.currentAuthScope !== startCurrentScope
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildStaleSessionFault(),
        };
      }
      // Network/lost response: preserve exact (runId, idempotencyKey, expectedRunVersion, requestBody)
      // in memory so an explicit user resend uses the EXACT original key + body. Never auto-POST.
      this.pendingRetryKey = candidatePendingRetry;
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "재시도 요청 중 네트워크 실패 — 서버 응답 미수신",
          message:
            "재시도 접수 응답을 받지 못했습니다. 이미 서버에서 처리되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 '서버 상태 다시 확인'(GET)으로 최신 상태를 조회하세요. 이전 미확정 재시도를 그대로 다시 보내려면 별도의 재전송 버튼을 사용하세요(원래 요청이 미접수였다면 새 작업이 시작될 수 있습니다).",
        },
        pendingRetry: this.getPendingRetrySummary(effectiveAuthScope),
      };
    }

    // If logout or session change occurred while in-flight, NEVER mutate the new
    // session's pending keys, run map, or state!
    if (
      this.scopeGeneration !== startGeneration ||
      this.currentAuthScope !== startCurrentScope
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleSessionFault(),
      };
    }

    if (response.status === 202) {
      const validated = this.validateAcceptedResponse(
        response.rawBody,
        params.runId
      );
      if (!validated.ok) {
        // Unverifiable/mismatched 202: preserve exact key + original body in memory,
        // do NOT poll status_url, do NOT move to another run!
        this.pendingRetryKey = candidatePendingRetry;
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildUnverifiableAcceptedFault("retry", validated.reason),
          pendingRetry: this.getPendingRetrySummary(effectiveAuthScope),
        };
      }

      // Valid 202 for the original run confirmed -> clear pending retry.
      this.pendingRetryKey = null;

      const accepted = validated.accepted;
      const nowIso = this.nowProvider();
      const prevMeta = this.runMetadata.get(params.runId);
      const nextAttempt =
        (params.previousRun?.attemptNumber ?? prevMeta?.attemptNumber ?? 1) + 1;

      this.statusUrlByRunId.set(accepted.target_id, accepted.status_url);
      this.runVersionByRunId.delete(accepted.target_id);
      this.runMetadata.set(accepted.target_id, {
        repo: params.repo,
        attemptNumber: nextAttempt,
        startedAt:
          params.previousRun?.startedAt ?? prevMeta?.startedAt ?? nowIso,
      });

      const run = buildAcceptedPendingRunViewModel({
        accepted,
        repo: params.repo,
        attemptNumber: nextAttempt,
        nowIso,
        previousRun: params.previousRun,
        isRetry: true,
      });

      return { ok: true, data: { run, pendingRetry: null } };
    }

    // For 5xx (including 503 AUTH_VERIFICATION_UNAVAILABLE), keep candidatePendingRetry in memory
    // so the user can still explicitly resend if they choose (never auto-POST).
    if (isServerErrorStatus(response.status)) {
      this.pendingRetryKey = candidatePendingRetry;
    }

    return this.handleNonSuccessResponse<ContractRunSyncResult>({
      response,
      operation: "retry",
      repo: params.repo,
      runId: params.runId,
      fallbackAttemptNumber: (params.previousRun?.attemptNumber ?? 1) + 1,
      matchedRetryKey: idempotencyKey,
      attachExistingRunOnConflict: true,
    });
  }
}
