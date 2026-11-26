import type {
  ImportRunViewModel,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";
import type { AdapterResult } from "./import-service-adapter";
import {
  buildAcceptedPendingRunViewModel,
  doesStatusUrlMatchTargetId,
  generateUuidV4,
  mapImportStatusDtoToViewModel,
  parseOptionalJsonBody,
  type ImportConflictDto,
  type ImportStatusDto,
  type InitialImportRequestDto,
  type SyncRunAcceptedDto,
  type SyntheticHttpResponse,
  type SyntheticHttpTransport,
} from "./synthetic-http-transport";

/**
 * OpenAPI 0.2.0-draft (CONTRACT commit a0314f2cbcb33f1d34ed486db7b1009dcb4adc59)
 * client for ONLY the 3 sync-run endpoints:
 *   - POST /v1/sync-runs
 *   - GET  <status_url> (/v1/sync-runs/{id})
 *   - POST /v1/sync-runs/{id}/retry
 *
 * Uses SyntheticHttpTransport (in-memory synthetic HTTP only; no real network).
 * Pending Idempotency-Keys are kept strictly in memory (never persisted across
 * reloads) and scoped to (authScope, request kind, target/payload).
 */

export interface ContractRunSyncResult {
  run: ImportRunViewModel;
  recoveryNotice?: RecoveryNoticeViewModel | null;
}

interface PendingCreateKey {
  authScope: string;
  requestFingerprint: string;
  idempotencyKey: string;
}

interface PendingRetryKey {
  authScope: string;
  runId: string;
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
      this.clearPendingKeys();
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

  public clearPendingKeys(): void {
    this.pendingCreateKey = null;
    this.pendingRetryKey = null;
  }

  private nextRequestId(): string {
    this.requestCounter += 1;
    return `synthetic-web-req-${this.requestCounter}`;
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
      operation === "retry" ? "동일 실행 재시도" : "동일 저장소 최초 가져오기";

    return {
      kind: "uncertain_outcome",
      httpStatus: status,
      title: `서버 응답 미확정 — 처리 여부 확인 필요 (HTTP ${status})`,
      message: `서버 오류 응답(HTTP ${status})으로 ${actionLabel} 처리 여부를 확인할 수 없습니다. 이미 서버에서 접수되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.`,
      serverConfirmed: false,
      actionGuidance: `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
    };
  }

  private buildUnverifiableAcceptedFault(
    operation: "create" | "retry",
    reason: UnverifiableAcceptedReason
  ): UnconfirmedFaultViewModel {
    const actionLabel =
      operation === "retry" ? "동일 실행 재시도" : "동일 저장소 최초 가져오기";

    if (reason === "retry_target_mismatch") {
      return {
        kind: "uncertain_outcome",
        httpStatus: 202,
        title: "재시도 접수 응답의 실행 ID 불일치 — 처리 여부 미확정 (HTTP 202)",
        message:
          "202 응답의 target_id가 재시도 대상 원래 실행 ID와 일치하지 않아 접수 결과를 검증할 수 없습니다. 해당 경로로 조회하거나 다른 실행으로 이동하지 않았으며, 성공이나 확정 실패로 표시하지 않습니다.",
        serverConfirmed: false,
        actionGuidance:
          "다른 실행으로 이동하지 않았습니다. 같은 세션에서 동일 실행 재시도를 직접 다시 요청하면 기존 Idempotency-Key가 재사용됩니다.",
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
        actionGuidance: `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 기존 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
      };
    }

    return {
      kind: "uncertain_outcome",
      httpStatus: 202,
      title: "접수 응답 검증 불가 — 처리 여부 미확정 (HTTP 202)",
      message:
        "202 응답에 필수 필드(target_id, status_url)가 누락되었거나 형식을 확인할 수 없어 접수 결과를 검증할 수 없습니다. 이미 서버에서 처리되었을 수 있으므로 성공이나 확정 실패로 표시하지 않으며, 자동으로 다시 요청하지 않습니다.",
      serverConfirmed: false,
      actionGuidance: `같은 세션에서 ${actionLabel}를 직접 다시 요청하면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 직접 다시 요청하세요.`,
    };
  }

  private buildNeutralHttpFault(status: number): UnconfirmedFaultViewModel {
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
          "선택한 저장소와 현재 화면 상태를 확인한 뒤 다시 요청해 주세요.",
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

  private buildSessionExpiredFault(): UnconfirmedFaultViewModel {
    return {
      kind: "session_expired",
      httpStatus: 401,
      title: "서비스 세션 만료 — 요청 미확정 (HTTP 401)",
      message:
        "인증이 유효하지 않아 서버가 요청을 처리하지 않았습니다. 작업은 확정되지 않았습니다.",
      serverConfirmed: false,
      actionGuidance:
        "상단에서 '모의 서비스 다시 로그인'을 완료한 뒤 다시 진행하세요.",
    };
  }

  private async handleConflictResponse(params: {
    response: SyntheticHttpResponse;
    repo: PublicRepositoryOption;
    fallbackAttemptNumber: number;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const parsed = parseOptionalJsonBody<ImportConflictDto>(
      params.response.rawBody
    );

    // Bodyless 409 or unparseable body -> neutral generic guidance without guessing!
    if (parsed.empty || parsed.invalidJson || !parsed.value?.error?.code) {
      return {
        ok: false,
        kind: "request_rejected",
        fault: this.buildNeutralHttpFault(409),
      };
    }

    const { code, details } = parsed.value.error;

    if (code === "IDEMPOTENCY_CONFLICT") {
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          httpStatus: 409,
          title: "요청 키 충돌 (IDEMPOTENCY_CONFLICT)",
          message:
            "동일한 Idempotency-Key가 다른 요청 내용과 충돌했습니다. 기존 실행으로 이동하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "새 요청으로 다시 시도하거나 현재 서버 상태를 먼저 확인하세요.",
        },
      };
    }

    if (code === "SYNC_ALREADY_RUNNING") {
      const existingRunId = details?.existing_run_id;
      // Navigate to the existing run ONLY when a valid existing_run_id is explicitly provided!
      if (isSafeRunId(existingRunId)) {
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
          return followUp;
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
          },
        };
      }

      // SYNC_ALREADY_RUNNING without existing_run_id -> do NOT fabricate or move!
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          httpStatus: 409,
          title: "진행 중인 가져오기 충돌 (SYNC_ALREADY_RUNNING)",
          message:
            "이미 실행 중인 가져오기가 있으나 서버가 기존 실행 ID를 제공하지 않아 다른 실행으로 이동하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "잠시 후 상태를 확인하거나 접근 권한을 확인해 주세요.",
        },
      };
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(409),
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
        };
      }

      // Valid 202 confirmed -> clear only this request's pending key so subsequent distinct requests use a new UUID.
      this.clearPendingCreateKeyIfMatch(idempotencyKey);

      const accepted = validated.accepted;
      const nowIso = this.nowProvider();
      // Track by validated target_id and matching status_url (independent of optional job_id)
      this.statusUrlByRunId.set(accepted.target_id, accepted.status_url);
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

      return { ok: true, data: { run } };
    }

    if (isServerErrorStatus(response.status)) {
      // 5xx (e.g. 502): server may already have committed the run before failing
      // to return the response -> KEEP pendingCreateKey, do NOT auto-POST.
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildUncertainServerErrorFault(response.status, "create"),
      };
    }

    if (response.status === 401) {
      // Auth scope rejected/expired -> never reuse pending keys across auth scopes.
      this.clearPendingKeys();
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionExpiredFault(),
      };
    }

    // Definitive non-5xx client/conflict response -> clear only this request's pending key.
    this.clearPendingCreateKeyIfMatch(idempotencyKey);

    if (response.status === 409) {
      return this.handleConflictResponse({
        response,
        repo: params.repo,
        fallbackAttemptNumber: 1,
      });
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(response.status),
    };
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
      if (parsed.empty || parsed.invalidJson || !parsed.value) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: this.buildNeutralHttpFault(200),
        };
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

      return { ok: true, data: { run } };
    }

    if (isServerErrorStatus(response.status)) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildUncertainServerErrorFault(response.status, "poll"),
      };
    }

    if (response.status === 401) {
      this.clearPendingKeys();
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionExpiredFault(),
      };
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(response.status),
    };
  }

  /**
   * POST /v1/sync-runs/{id}/retry (bodyless + UUID Idempotency-Key)
   */
  public async retryInitialImport(params: {
    runId: string;
    repo: PublicRepositoryOption;
    previousRun?: ImportRunViewModel | null;
    authScope?: string;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const startGeneration = this.scopeGeneration;
    const startCurrentScope = this.currentAuthScope;
    const effectiveAuthScope = params.authScope ?? this.currentAuthScope;

    // Different logical operation -> never keep unrelated create key
    this.pendingCreateKey = null;

    const idempotencyKey =
      this.pendingRetryKey &&
      this.pendingRetryKey.authScope === effectiveAuthScope &&
      this.pendingRetryKey.runId === params.runId
        ? this.pendingRetryKey.idempotencyKey
        : this.keyGenerator();

    this.pendingRetryKey = {
      authScope: effectiveAuthScope,
      runId: params.runId,
      idempotencyKey,
    };

    let response: SyntheticHttpResponse;
    try {
      response = await this.transport.send({
        method: "POST",
        path: `/v1/sync-runs/${encodeURIComponent(params.runId)}/retry`,
        headers: {
          "Idempotency-Key": idempotencyKey,
          "X-Request-ID": this.nextRequestId(),
        },
        // OpenAPI 0.2.0-draft: retry request MUST omit request body
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
      // Network/lost response: KEEP pendingRetryKey in memory so an explicit
      // user resend of the exact same retry in the same auth scope reuses
      // the exact same UUID Idempotency-Key. Never auto-POST.
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
            "같은 세션에서 동일 실행 재시도를 직접 다시 보내면 동일한 Idempotency-Key가 재사용됩니다. 먼저 상태를 확인하거나 직접 다시 시도하세요.",
        },
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
        // Unverifiable/mismatched 202 (including wrong target_id or status_url):
        // KEEP pendingRetryKey, do NOT poll status_url, do NOT move to another run!
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildUnverifiableAcceptedFault("retry", validated.reason),
        };
      }

      // Valid 202 for the original run confirmed -> clear only this request's pending key.
      this.clearPendingRetryKeyIfMatch(idempotencyKey);

      const accepted = validated.accepted;
      const nowIso = this.nowProvider();
      const prevMeta = this.runMetadata.get(params.runId);
      const nextAttempt =
        (params.previousRun?.attemptNumber ?? prevMeta?.attemptNumber ?? 1) + 1;

      this.statusUrlByRunId.set(accepted.target_id, accepted.status_url);
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

      return { ok: true, data: { run } };
    }

    if (isServerErrorStatus(response.status)) {
      // 5xx (e.g. 502): server may already have accepted the retry -> KEEP pendingRetryKey.
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildUncertainServerErrorFault(response.status, "retry"),
      };
    }

    if (response.status === 401) {
      this.clearPendingKeys();
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionExpiredFault(),
      };
    }

    // Definitive non-5xx client/conflict response -> clear only this request's pending key.
    this.clearPendingRetryKeyIfMatch(idempotencyKey);

    if (response.status === 409) {
      return this.handleConflictResponse({
        response,
        repo: params.repo,
        fallbackAttemptNumber: (params.previousRun?.attemptNumber ?? 1) + 1,
      });
    }

    return {
      ok: false,
      kind: "request_rejected",
      fault: this.buildNeutralHttpFault(response.status),
    };
  }
}
