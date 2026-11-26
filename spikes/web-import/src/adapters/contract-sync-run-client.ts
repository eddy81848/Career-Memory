import type {
  ImportRunViewModel,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";
import type { AdapterResult } from "./import-service-adapter";
import {
  buildAcceptedPendingRunViewModel,
  generateUuidV4,
  isValidRelativeStatusUrl,
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
 */

export interface ContractRunSyncResult {
  run: ImportRunViewModel;
  recoveryNotice?: RecoveryNoticeViewModel | null;
}

interface PendingCreateKey {
  sourceResourceId: string;
  idempotencyKey: string;
}

interface PendingRetryKey {
  runId: string;
  idempotencyKey: string;
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

  private pendingCreateKey: PendingCreateKey | null = null;
  private pendingRetryKey: PendingRetryKey | null = null;
  private requestCounter = 0;

  constructor(options: {
    transport: SyntheticHttpTransport;
    keyGenerator?: () => string;
    nowProvider?: () => string;
  }) {
    this.transport = options.transport;
    this.keyGenerator = options.keyGenerator ?? generateUuidV4;
    this.nowProvider = options.nowProvider ?? (() => new Date().toISOString());
  }

  public registerKnownRun(
    run: ImportRunViewModel,
    repo: PublicRepositoryOption
  ): void {
    const statusUrl =
      run.statusUrl && isValidRelativeStatusUrl(run.statusUrl)
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
      // Navigate to the existing run ONLY when existing_run_id is explicitly provided!
      if (typeof existingRunId === "string" && existingRunId.trim().length > 0) {
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

  /**
   * POST /v1/sync-runs
   */
  public async createInitialImport(params: {
    repo: PublicRepositoryOption;
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const sourceResourceId = params.repo.repoId;
    const idempotencyKey =
      this.pendingCreateKey &&
      this.pendingCreateKey.sourceResourceId === sourceResourceId
        ? this.pendingCreateKey.idempotencyKey
        : this.keyGenerator();

    this.pendingCreateKey = { sourceResourceId, idempotencyKey };

    const bodyDto: InitialImportRequestDto = {
      source_resource_id: sourceResourceId,
      mode: "INITIAL_BACKFILL",
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
        body: JSON.stringify(bodyDto),
      });
    } catch {
      // Response lost: KEEP pendingCreateKey so resending the exact same logical
      // request reuses the exact same UUID Idempotency-Key!
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "네트워크 통신 실패 — 서버 응답 미수신",
          message:
            "최초 가져오기 접수 응답을 받지 못했습니다. 작업 시작 여부가 확정되지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "같은 요청을 다시 보내면 동일한 Idempotency-Key가 재사용됩니다. 먼저 서버 상태를 확인하거나 동일 저장소로 다시 요청하세요.",
        },
      };
    }

    // Any HTTP response was received -> clear pending key so future distinct
    // requests use a fresh UUID.
    this.pendingCreateKey = null;

    if (response.status === 202) {
      const parsed = parseOptionalJsonBody<SyncRunAcceptedDto>(response.rawBody);
      if (
        parsed.empty ||
        parsed.invalidJson ||
        !parsed.value?.target_id ||
        !parsed.value?.status_url ||
        !isValidRelativeStatusUrl(parsed.value.status_url)
      ) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: this.buildNeutralHttpFault(202),
        };
      }

      const accepted = parsed.value;
      const nowIso = this.nowProvider();
      // Track by target_id and status_url only (independent of optional job_id)
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

    if (response.status === 401) {
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionExpiredFault(),
      };
    }

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

    if (response.status === 401) {
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
  }): Promise<AdapterResult<ContractRunSyncResult>> {
    const idempotencyKey =
      this.pendingRetryKey && this.pendingRetryKey.runId === params.runId
        ? this.pendingRetryKey.idempotencyKey
        : this.keyGenerator();

    this.pendingRetryKey = { runId: params.runId, idempotencyKey };

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
      // Response lost: KEEP pendingRetryKey so resending the exact same retry
      // reuses the exact same UUID Idempotency-Key!
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "재시도 요청 중 네트워크 실패 — 서버 응답 미수신",
          message:
            "재시도 접수 응답을 받지 못했습니다. 재시도 시작 여부가 확정되지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "같은 재시도를 다시 보내면 동일한 Idempotency-Key가 재사용됩니다. 먼저 상태를 확인하거나 다시 시도하세요.",
        },
      };
    }

    this.pendingRetryKey = null;

    if (response.status === 202) {
      const parsed = parseOptionalJsonBody<SyncRunAcceptedDto>(response.rawBody);
      if (
        parsed.empty ||
        parsed.invalidJson ||
        !parsed.value?.target_id ||
        !parsed.value?.status_url ||
        !isValidRelativeStatusUrl(parsed.value.status_url)
      ) {
        return {
          ok: false,
          kind: "request_rejected",
          fault: this.buildNeutralHttpFault(202),
        };
      }

      const accepted = parsed.value;
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

    if (response.status === 401) {
      return {
        ok: false,
        kind: "session_expired",
        fault: this.buildSessionExpiredFault(),
      };
    }

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
