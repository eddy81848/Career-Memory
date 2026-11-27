import type { PrototypeScenarioId } from "@/domain/view-models";
import {
  isValidRunVersion,
  parseOptionalJsonBody,
  UUID_V4_REGEX,
  type ImportConflictDto,
  type ImportErrorEnvelopeDto,
  type ImportStatusDto,
  type InitialImportRequestDto,
  type RetryImportRequestDto,
  type SyncRunAcceptedDto,
  type SyntheticHttpRequest,
  type SyntheticHttpResponse,
  type SyntheticHttpTransport,
} from "./synthetic-http-transport";

/**
 * In-memory synthetic HTTP responder matching OpenAPI 0.5.0-draft
 * (CONTRACT commit 87f1515cbe74bf02b0aece68904bf9a641c74c60) for ONLY:
 *   - POST /v1/sync-runs
 *   - GET  /v1/sync-runs?source_resource_id=...&limit=1
 *   - GET  /v1/sync-runs/{id}
 *   - POST /v1/sync-runs/{id}/retry
 *
 * ZERO real network calls. Used locally by MockFixtureImportAdapter and tests.
 */

export interface SyntheticOpenApiState {
  scenarioId: PrototypeScenarioId;
  pollStep: number;
  runSequence: number;
  runVersion: number;
  isRetryRecoveryFlow: boolean;
  activeRunId: string | null;
  activePlanId: string | null;
  failNextPollWithNetworkError: boolean;
}

export class SyntheticOpenApiTransport implements SyntheticHttpTransport {
  private readonly getContext: () => {
    isAuthenticated: boolean;
    state: SyntheticOpenApiState;
    onStateChange: (next: SyntheticOpenApiState) => void;
    onGitHubPermissionRevoked?: () => void;
  };

  /** Recorded synthetic HTTP requests for test inspection */
  public readonly requestLog: SyntheticHttpRequest[] = [];

  private readonly createIdempotencyRecords = new Map<
    string,
    { fingerprint: string; accepted: SyncRunAcceptedDto }
  >();
  private readonly retryIdempotencyRecords = new Map<
    string,
    { runId: string; expectedRunVersion: number; accepted: SyncRunAcceptedDto }
  >();

  constructor(
    getContext: () => {
      isAuthenticated: boolean;
      state: SyntheticOpenApiState;
      onStateChange: (next: SyntheticOpenApiState) => void;
      onGitHubPermissionRevoked?: () => void;
    }
  ) {
    this.getContext = getContext;
  }

  public clearIdempotencyLog(): void {
    this.createIdempotencyRecords.clear();
    this.retryIdempotencyRecords.clear();
  }

  private echoHeaders(request: SyntheticHttpRequest): Record<string, string> {
    const reqId = request.headers["X-Request-ID"];
    return reqId ? { "X-Request-ID": reqId } : {};
  }

  private buildErrorEnvelope(
    request: SyntheticHttpRequest,
    code: string,
    message: string,
    retryable = false,
    details?: { existing_run_id?: string }
  ): ImportErrorEnvelopeDto {
    const requestId = request.headers["X-Request-ID"] ?? "synthetic-req-0";
    return {
      error: {
        code,
        message,
        retryable,
        request_id: requestId,
        ...(details ? { details } : {}),
      },
    };
  }

  private buildSessionTerminatedResponse(
    request: SyntheticHttpRequest,
    headers: Record<string, string>
  ): SyntheticHttpResponse {
    return {
      status: 401,
      headers: { ...headers, "Content-Type": "application/json" },
      rawBody: JSON.stringify(
        this.buildErrorEnvelope(
          request,
          "SESSION_TERMINATED",
          "Service session has been terminated.",
          false
        )
      ),
    };
  }

  public async send(
    request: SyntheticHttpRequest
  ): Promise<SyntheticHttpResponse> {
    this.requestLog.push({
      ...request,
      headers: { ...request.headers },
    });

    const ctx = this.getContext();
    const headers = this.echoHeaders(request);

    if (request.method === "POST" && request.path === "/v1/sync-runs") {
      return this.handleCreateInitialImport(request, ctx, headers);
    }

    if (
      request.method === "GET" &&
      (request.path === "/v1/sync-runs" || request.path.startsWith("/v1/sync-runs?"))
    ) {
      return this.handleRecoverRuns(request, ctx, headers);
    }

    const retryMatch = request.path.match(/^\/v1\/sync-runs\/([^/?#]+)\/retry$/);
    if (request.method === "POST" && retryMatch) {
      const runId = decodeURIComponent(retryMatch[1]);
      return this.handleRetry(request, runId, ctx, headers);
    }

    const getMatch = request.path.match(/^\/v1\/sync-runs\/([^/?#]+)$/);
    if (request.method === "GET" && getMatch) {
      const runId = decodeURIComponent(getMatch[1]);
      return this.handleGetStatus(request, runId, ctx, headers);
    }

    return { status: 404, headers, rawBody: "" };
  }

  private handleRecoverRuns(
    request: SyntheticHttpRequest,
    ctx: ReturnType<SyntheticOpenApiTransport["getContext"]>,
    headers: Record<string, string>
  ): SyntheticHttpResponse {
    if (!ctx.isAuthenticated) {
      return this.buildSessionTerminatedResponse(request, headers);
    }

    const queryIndex = request.path.indexOf("?");
    const params = new URLSearchParams(
      queryIndex >= 0 ? request.path.slice(queryIndex + 1) : ""
    );
    const sourceResourceId = params.get("source_resource_id");
    const limit = params.get("limit") ?? "1";
    if (!sourceResourceId || sourceResourceId.trim().length === 0 || limit !== "1") {
      return { status: 422, headers, rawBody: "" };
    }

    return {
      status: 200,
      headers: {
        ...headers,
        "Content-Type": "application/json",
        "Cache-Control": "private, no-store",
      },
      rawBody: JSON.stringify({ items: [] }),
    };
  }

  private handleCreateInitialImport(
    request: SyntheticHttpRequest,
    ctx: ReturnType<SyntheticOpenApiTransport["getContext"]>,
    headers: Record<string, string>
  ): SyntheticHttpResponse {
    const { isAuthenticated, state, onStateChange } = ctx;

    // Lost response simulation BEFORE server response arrives
    if (state.scenarioId === "network_error_on_submit") {
      throw new Error("SYNTHETIC_NETWORK_LOST_RESPONSE");
    }

    if (!isAuthenticated || state.scenarioId === "session_expired_on_submit") {
      return this.buildSessionTerminatedResponse(request, headers);
    }

    // Empty-body review stub responses (no JSON body!)
    if (state.scenarioId === "empty_403") {
      return { status: 403, headers, rawBody: "" };
    }
    if (state.scenarioId === "empty_409") {
      return { status: 409, headers, rawBody: "" };
    }
    if (state.scenarioId === "empty_422") {
      return { status: 422, headers, rawBody: "" };
    }

    // Validate UUID Idempotency-Key & JSON body per OpenAPI 0.5.0-draft -> empty 422
    const key = request.headers["Idempotency-Key"];
    if (!key || !UUID_V4_REGEX.test(key)) {
      return { status: 422, headers, rawBody: "" };
    }

    const parsedBody = parseOptionalJsonBody<InitialImportRequestDto>(
      request.body
    );
    if (
      parsedBody.empty ||
      parsedBody.invalidJson ||
      !parsedBody.value?.source_resource_id ||
      parsedBody.value.mode !== "INITIAL_BACKFILL"
    ) {
      return { status: 422, headers, rawBody: "" };
    }

    const fingerprint = JSON.stringify({
      source_resource_id: parsedBody.value.source_resource_id,
      mode: parsedBody.value.mode,
    });

    const prior = this.createIdempotencyRecords.get(key);
    if (prior) {
      if (prior.fingerprint !== fingerprint) {
        const conflict: ImportConflictDto = {
          error: {
            code: "IDEMPOTENCY_CONFLICT",
            message: "Idempotency-Key was reused with a different request payload.",
            retryable: false,
            request_id: request.headers["X-Request-ID"] ?? "synthetic-req-0",
          },
        };
        return {
          status: 409,
          headers: { ...headers, "Content-Type": "application/json" },
          rawBody: JSON.stringify(conflict),
        };
      }
      return {
        status: 202,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(prior.accepted),
      };
    }

    // 409 SYNC_ALREADY_RUNNING with existing_run_id (OpenAPI activeRun example)
    if (state.scenarioId === "conflict_existing_run") {
      const existingRunId = "synthetic-run-existing-1";
      onStateChange({
        ...state,
        activeRunId: existingRunId,
        activePlanId: "synthetic-plan-existing-1",
        pollStep: 0,
        runVersion: 1,
        isRetryRecoveryFlow: false,
      });
      const conflict: ImportConflictDto = {
        error: {
          code: "SYNC_ALREADY_RUNNING",
          message: "An active sync run already exists for this source resource.",
          retryable: false,
          request_id: request.headers["X-Request-ID"] ?? "synthetic-req-0",
          details: { existing_run_id: existingRunId },
        },
      };
      return {
        status: 409,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(conflict),
      };
    }

    const nextSeq = state.runSequence + 1;
    const targetId = `run_demo_${nextSeq}`;
    const planId = `synthetic-plan-${nextSeq}`;
    const accepted: SyncRunAcceptedDto = {
      status: "PENDING",
      target_type: "SYNC_RUN",
      target_id: targetId,
      status_url: `/v1/sync-runs/${targetId}`,
      // Intentionally omit job_id (optional per OpenAPI 0.5.0-draft)
    };

    this.createIdempotencyRecords.set(key, { fingerprint, accepted });
    onStateChange({
      ...state,
      runSequence: nextSeq,
      pollStep: 0,
      runVersion: 1,
      isRetryRecoveryFlow: false,
      activeRunId: targetId,
      activePlanId: planId,
    });

    return {
      status: 202,
      headers: { ...headers, "Content-Type": "application/json" },
      rawBody: JSON.stringify(accepted),
    };
  }

  private handleRetry(
    request: SyntheticHttpRequest,
    runId: string,
    ctx: ReturnType<SyntheticOpenApiTransport["getContext"]>,
    headers: Record<string, string>
  ): SyntheticHttpResponse {
    const { isAuthenticated, state, onStateChange } = ctx;

    if (!isAuthenticated) {
      return this.buildSessionTerminatedResponse(request, headers);
    }

    // OpenAPI 0.5.0-draft: retry requires UUID Idempotency-Key + JSON {"expected_run_version": <int >= 1>}
    const key = request.headers["Idempotency-Key"];
    if (!key || !UUID_V4_REGEX.test(key)) {
      return { status: 422, headers, rawBody: "" };
    }

    const parsedBody = parseOptionalJsonBody<Record<string, unknown>>(
      request.body
    );
    if (
      parsedBody.empty ||
      parsedBody.invalidJson ||
      !parsedBody.value ||
      typeof parsedBody.value !== "object" ||
      Array.isArray(parsedBody.value)
    ) {
      return { status: 422, headers, rawBody: "" };
    }

    const bodyKeys = Object.keys(parsedBody.value);
    const expectedRunVersion = (parsedBody.value as Partial<RetryImportRequestDto>)
      .expected_run_version;
    if (
      bodyKeys.length !== 1 ||
      bodyKeys[0] !== "expected_run_version" ||
      !isValidRunVersion(expectedRunVersion)
    ) {
      return { status: 422, headers, rawBody: "" };
    }

    // Idempotency replay runs BEFORE version conflict check per OpenAPI 0.5.0-draft
    const prior = this.retryIdempotencyRecords.get(key);
    if (prior) {
      if (
        prior.runId !== runId ||
        prior.expectedRunVersion !== expectedRunVersion
      ) {
        const conflict: ImportConflictDto = {
          error: {
            code: "IDEMPOTENCY_CONFLICT",
            message: "Idempotency-Key was reused with a different retry request.",
            retryable: false,
            request_id: request.headers["X-Request-ID"] ?? "synthetic-req-0",
          },
        };
        return {
          status: 409,
          headers: { ...headers, "Content-Type": "application/json" },
          rawBody: JSON.stringify(conflict),
        };
      }
      return {
        status: 202,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(prior.accepted),
      };
    }

    const currentVersion = isValidRunVersion(state.runVersion)
      ? state.runVersion
      : 1;
    if (expectedRunVersion !== currentVersion) {
      const conflict: ImportConflictDto = {
        error: {
          code: "RESOURCE_VERSION_CONFLICT",
          message:
            "Sync run state changed before retry was processed; refresh run status.",
          retryable: false,
          request_id: request.headers["X-Request-ID"] ?? "synthetic-req-0",
        },
      };
      return {
        status: 409,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(conflict),
      };
    }

    // OpenAPI 0.5.0-draft: retry returns the ORIGINAL run's target_id and status_url
    const accepted: SyncRunAcceptedDto = {
      status: "PENDING",
      target_type: "SYNC_RUN",
      target_id: runId,
      status_url: `/v1/sync-runs/${encodeURIComponent(runId)}`,
    };

    const nextVersion = currentVersion + 1;
    this.retryIdempotencyRecords.set(key, {
      runId,
      expectedRunVersion,
      accepted,
    });
    onStateChange({
      ...state,
      pollStep: 0,
      runVersion: nextVersion,
      isRetryRecoveryFlow: true,
      activeRunId: runId,
    });

    return {
      status: 202,
      headers: { ...headers, "Content-Type": "application/json" },
      rawBody: JSON.stringify(accepted),
    };
  }

  private handleGetStatus(
    request: SyntheticHttpRequest,
    runId: string,
    ctx: ReturnType<SyntheticOpenApiTransport["getContext"]>,
    headers: Record<string, string>
  ): SyntheticHttpResponse {
    const { isAuthenticated, state, onStateChange, onGitHubPermissionRevoked } =
      ctx;

    if (state.failNextPollWithNetworkError) {
      onStateChange({
        ...state,
        failNextPollWithNetworkError: false,
      });
      throw new Error("SYNTHETIC_POLL_NETWORK_FAILURE");
    }

    if (!isAuthenticated) {
      return this.buildSessionTerminatedResponse(request, headers);
    }

    const nextStep = state.pollStep + 1;
    const currentVersion = isValidRunVersion(state.runVersion)
      ? state.runVersion
      : 1;
    const nextVersion = currentVersion + 1;
    const planId = state.activePlanId ?? `synthetic-plan-${runId}`;
    const observedAt = new Date().toISOString();

    // Existing run attached via 409 SYNC_ALREADY_RUNNING -> RUNNING then SUCCESS
    if (state.scenarioId === "conflict_existing_run") {
      onStateChange({ ...state, pollStep: nextStep, runVersion: nextVersion });
      const dto: ImportStatusDto =
        nextStep === 1
          ? {
              status: "RUNNING",
              run_version: nextVersion,
              plan_id: planId,
              import_ready: false,
              processing_status: "NOT_STARTED",
              observed_at: observedAt,
              source_drift: false,
              processed_range: "2025-09-23 through 2026-06-30",
              remaining_range: "2026-07-01 through 2026-10-05",
              retry: { allowed: false },
            }
          : {
              status: "SUCCESS",
              run_version: nextVersion,
              plan_id: planId,
              import_ready: true,
              processing_status: "COMPLETE",
              observed_at: observedAt,
              source_drift: false,
              processed_range: "2025-09-23 through 2026-10-05",
              retry: { allowed: false },
            };
      return {
        status: 200,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(dto),
      };
    }

    // Slow reload recovery stays RUNNING across 3 polls before SUCCESS
    if (state.scenarioId === "running_slow_for_reload" && nextStep < 4) {
      onStateChange({ ...state, pollStep: nextStep, runVersion: nextVersion });
      const dto: ImportStatusDto = {
        status: "RUNNING",
        run_version: nextVersion,
        plan_id: planId,
        import_ready: false,
        processing_status: "NOT_STARTED",
        observed_at: observedAt,
        source_drift: false,
        processed_range: "2025-09-23 through 2026-05-01",
        remaining_range: "2026-05-02 through 2026-10-05",
        retry: { allowed: false },
      };
      return {
        status: 200,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(dto),
      };
    }

    // Normal full success & retry recovery pass through RUNNING then SUCCESS(COMPLETE)
    if (
      (state.scenarioId === "success_full" || state.isRetryRecoveryFlow) &&
      nextStep === 1
    ) {
      onStateChange({ ...state, pollStep: nextStep, runVersion: nextVersion });
      const dto: ImportStatusDto = {
        status: "RUNNING",
        run_version: nextVersion,
        plan_id: planId,
        import_ready: false,
        processing_status: "NOT_STARTED",
        observed_at: observedAt,
        source_drift: false,
        streams: [
          { type: "PR", status: "RUNNING", last_committed_page: 4 },
        ],
        retry: { allowed: false },
      };
      return {
        status: 200,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(dto),
      };
    }

    onStateChange({ ...state, pollStep: nextStep, runVersion: nextVersion });

    if (state.isRetryRecoveryFlow) {
      const dto: ImportStatusDto = {
        status: "SUCCESS",
        run_version: nextVersion,
        plan_id: planId,
        import_ready: true,
        processing_status: "COMPLETE",
        observed_at: observedAt,
        source_drift: false,
        processed_range: "2025-09-23 through 2026-10-05",
        retry: { allowed: false },
      };
      return {
        status: 200,
        headers: { ...headers, "Content-Type": "application/json" },
        rawBody: JSON.stringify(dto),
      };
    }

    const dto = this.buildScenarioTerminalDto({
      scenarioId: state.scenarioId,
      planId,
      runVersion: nextVersion,
      observedAt,
      onGitHubPermissionRevoked,
    });

    return {
      status: 200,
      headers: { ...headers, "Content-Type": "application/json" },
      rawBody: JSON.stringify(dto),
    };
  }

  private buildScenarioTerminalDto(params: {
    scenarioId: PrototypeScenarioId;
    planId: string;
    runVersion: number;
    observedAt: string;
    onGitHubPermissionRevoked?: () => void;
  }): ImportStatusDto {
    const { scenarioId, planId, runVersion, observedAt } = params;

    switch (scenarioId) {
      case "success_full":
      case "running_slow_for_reload":
      case "session_expired_on_submit":
      case "network_error_on_submit":
      case "conflict_existing_run":
      case "empty_403":
      case "empty_409":
      case "empty_422":
        return {
          status: "SUCCESS",
          run_version: runVersion,
          plan_id: planId,
          import_ready: true,
          processing_status: "COMPLETE",
          observed_at: observedAt,
          source_drift: false,
          processed_range: "2025-09-23 through 2026-10-05",
          retry: { allowed: false },
        };

      case "success_import_ready_derived_pending":
        return {
          status: "SUCCESS",
          run_version: runVersion,
          plan_id: planId,
          import_ready: true,
          processing_status: "FAILED",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };

      // OpenAPI example: policyChangedAfterSuccess
      case "success_blocked_policy":
        return {
          status: "SUCCESS",
          run_version: runVersion,
          plan_id: planId,
          import_ready: true,
          processing_status: "BLOCKED_POLICY",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };

      // OpenAPI example: SUCCESS + BLOCKED_SOURCE
      case "success_blocked_source":
        return {
          status: "SUCCESS",
          run_version: runVersion,
          plan_id: planId,
          import_ready: true,
          processing_status: "BLOCKED_SOURCE",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };

      // OpenAPI example: transientPartial
      case "partial_retryable":
        return {
          status: "PARTIAL",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "TRANSIENT_PROVIDER_ERROR",
          streams: [
            {
              type: "PR",
              status: "PARTIAL",
              last_committed_page: 7,
              unresolved_failures: 1,
            },
          ],
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          processed_range: "2025-09-23 through 2026-08-31",
          remaining_range: "2026-09-01 through 2026-10-05",
          retry: { allowed: true, from_safe_checkpoint: true },
        };

      // OpenAPI example: importPolicyBlocked
      case "partial_non_retryable":
        return {
          status: "PARTIAL",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "SOURCE_POLICY_BLOCKED",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };

      // OpenAPI example: planDrift
      case "partial_plan_drift":
        return {
          status: "PARTIAL",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "PLAN_DRIFT",
          streams: [
            {
              type: "COMMIT",
              status: "PARTIAL",
              last_committed_page: 5,
              unresolved_failures: 1,
            },
            {
              type: "PR",
              status: "PARTIAL",
              last_committed_page: 3,
              unresolved_failures: 1,
            },
          ],
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: true,
          processed_range: "2025-09-23 through 2026-07-31",
          remaining_range: "2026-08-01 through 2026-10-05",
          retry: { allowed: false },
        };

      // Rate-limited transient partial retryable
      case "limit_rate_limited":
        return {
          status: "PARTIAL",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "TRANSIENT_PROVIDER_ERROR",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          processed_range: "2025-09-23 through 2026-06-30",
          remaining_range: "2026-07-01 through 2026-10-05",
          retry: { allowed: true, from_safe_checkpoint: true },
        };

      // OpenAPI example: itemLimit (PARTIAL_WITH_LIMIT -> import_ready=false, retry.allowed=false)
      case "limit_cap_blocked":
        return {
          status: "PARTIAL_WITH_LIMIT",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "SYNC_PARTIAL_WITH_LIMIT",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          processed_range: "2025-09-23 through 2026-08-31",
          remaining_range: "2026-09-01 through 2026-10-05",
          retry: { allowed: false },
        };

      // OpenAPI invariant: retry.allowed=true only for PARTIAL + TRANSIENT_PROVIDER_ERROR
      case "error_retryable":
        return {
          status: "PARTIAL",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "TRANSIENT_PROVIDER_ERROR",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: true, from_safe_checkpoint: true },
        };

      case "error_non_retryable":
        return {
          status: "FAILED",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "MOCK_REPO_UNSUPPORTED_STRUCTURE",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };

      case "canceled_run":
        return {
          status: "CANCELED",
          run_version: runVersion,
          plan_id: planId,
          streams: [
            {
              type: "PR",
              status: "CANCELED",
              last_committed_page: 2,
              unresolved_failures: 0,
            },
          ],
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          processed_range: "2025-09-23 through 2026-03-31",
          remaining_range: "2026-04-01 through 2026-10-05",
          retry: { allowed: false },
        };

      case "permission_revoked":
        params.onGitHubPermissionRevoked?.();
        return {
          status: "FAILED",
          run_version: runVersion,
          plan_id: planId,
          failure_reason: "SOURCE_PERMISSION_REVOKED",
          import_ready: false,
          processing_status: "NOT_STARTED",
          observed_at: observedAt,
          source_drift: false,
          retry: { allowed: false },
        };
    }
  }
}
