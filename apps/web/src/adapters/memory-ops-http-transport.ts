import type {
  ConfirmSourceDeletionInput,
  GetQuotaStatusQuery,
  MemoryOpsAdapter,
} from "@/adapters/memory-ops-adapter";
import {
  generateUuidV4,
  parseOptionalJsonBody,
} from "@/adapters/synthetic-http-transport";
import type {
  CircuitBreakerState,
  DeletionComponent,
  DeletionComponentReceiptItem,
  DeletionComponentStatus,
  DeletionConfirmAcceptedResponse,
  DeletionImpactCounts,
  DeletionJobStatus,
  DeletionJobStatusResponse,
  DeletionPreviewResponse,
  DeletionUserVisibleState,
  MemoryOpsError,
  MemoryOpsResponseMeta,
  MemoryOpsResult,
  QuotaAdmissionState,
  QuotaMeterItem,
  QuotaMeterStatus,
  QuotaMetricKind,
  QuotaPauseState,
  QuotaStatusResponse,
  SearchEntityType,
  SearchHit,
  SearchQueryInput,
  SearchResponse,
} from "@/domain/memory-ops-models";
import {
  assertNoProjectIdInRecord,
  buildSafeSearchQueryParams,
} from "@/domain/memory-ops-presenter";

/**
 * Real `fetch`-based HTTP transport for M5 Workspace Memory Search (OD-008),
 * Source Deletion Preview/Confirm & Receipt (OD-003), and Quota/Pause Status (T-503 / INF-06).
 *
 * Security & correctness invariants:
 * - Always attaches `X-Request-ID`, optional `Authorization` & `X-Workspace-ID`.
 * - Attaches caller-preserved `Idempotency-Key` (UUID v4) on `deletion-confirm`.
 * - Strictly enforces OD-008: NEVER sends `project_id` on any query or payload.
 * - Normalizes structured `ErrorEnvelope` responses across 400, 401, 403, 404, 409, 422, 429, 503.
 * - Zero browser storage writes (`localStorage`/`sessionStorage`) and zero auto-retry on errors.
 */

export interface MemoryOpsHttpTransportOptions {
  baseUrl: string;
  workspaceId?: string;
  authorization?: string;
  fetchImpl?: typeof fetch;
  keyGenerator?: () => string;
  defaultHeaders?: Record<string, string>;
}

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function parseRetryAfterHeader(headerValue: string | null): number | null {
  if (!headerValue) return null;
  const parsed = Number(headerValue.trim());
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function extractMeta(res: Response): MemoryOpsResponseMeta {
  return {
    httpStatus: res.status,
    cacheControl: res.headers.get("cache-control"),
    requestId: res.headers.get("x-request-id"),
    retryAfterSeconds: parseRetryAfterHeader(res.headers.get("retry-after")),
  };
}

// =============================================================================
// Lossless Response Normalizers
// =============================================================================

export function normalizeSearchHit(raw: unknown): SearchHit {
  const obj = asRecord(raw) ?? {};
  return {
    entity_type: (typeof obj.entity_type === "string"
      ? obj.entity_type
      : "WORK_EVENT") as SearchEntityType,
    entity_id: typeof obj.entity_id === "string" ? obj.entity_id : "",
    source_resource_id:
      typeof obj.source_resource_id === "string" ? obj.source_resource_id : null,
    title_or_summary:
      typeof obj.title_or_summary === "string" ? obj.title_or_summary : "",
    score: asNumber(obj.score, 0),
  };
}

export function normalizeSearchResponse(raw: unknown): SearchResponse {
  const obj = asRecord(raw) ?? {};
  const scope = asRecord(obj.scope) ?? {};
  const rawItems = Array.isArray(obj.items) ? obj.items : [];
  return {
    scope: {
      workspace_id:
        typeof scope.workspace_id === "string" ? scope.workspace_id : "",
      source_resource_id:
        typeof scope.source_resource_id === "string"
          ? scope.source_resource_id
          : null,
    },
    query_hash: typeof obj.query_hash === "string" ? obj.query_hash : "",
    index_version:
      typeof obj.index_version === "string" ? obj.index_version : "fts-index-v1",
    ranking_version:
      typeof obj.ranking_version === "string"
        ? obj.ranking_version
        : "rank-bm25-v1",
    items: rawItems.map(normalizeSearchHit),
    next_cursor: typeof obj.next_cursor === "string" ? obj.next_cursor : null,
  };
}

export function normalizeDeletionPreviewResponse(
  raw: unknown
): DeletionPreviewResponse {
  const obj = asRecord(raw) ?? {};
  const counts = asRecord(obj.impact_counts) ?? {};
  const nonRec = asRecord(obj.non_recoverable_scopes) ?? {};

  const impact_counts: DeletionImpactCounts = {
    work_events: asNumber(counts.work_events, 0),
    facts: asNumber(counts.facts, 0),
    contributions: asNumber(counts.contributions, 0),
    claims_draft: asNumber(counts.claims_draft, 0),
    claims_edited: asNumber(counts.claims_edited, 0),
    claims_approved: asNumber(counts.claims_approved, 0),
    claims_historical_versions: asNumber(counts.claims_historical_versions, 0),
    search_index_entries: asNumber(counts.search_index_entries, 0),
    ephemeral_cache_entries: asNumber(counts.ephemeral_cache_entries, 0),
    server_exports: asNumber(counts.server_exports, 0),
  };

  return {
    source_resource_id:
      typeof obj.source_resource_id === "string" ? obj.source_resource_id : "",
    resource_version:
      typeof obj.resource_version === "string" ? obj.resource_version : "",
    impact_scope_version:
      typeof obj.impact_scope_version === "string"
        ? obj.impact_scope_version
        : "",
    impact_counts,
    non_recoverable_scopes: {
      already_downloaded_exports: true,
      provider_hosted_originals: true,
      notice:
        typeof nonRec.notice === "string" && nonRec.notice.trim()
          ? nonRec.notice
          : "Previously downloaded local export files and remote provider-hosted source originals cannot be recalled by server-side deletion.",
    },
    preview_token:
      typeof obj.preview_token === "string" ? obj.preview_token : "",
    expires_at: typeof obj.expires_at === "string" ? obj.expires_at : "",
  };
}

export function normalizeDeletionConfirmAccepted(
  raw: unknown,
  fallbackSourceResourceId: string
): DeletionConfirmAcceptedResponse {
  const obj = asRecord(raw) ?? {};
  return {
    deletion_job_id:
      typeof obj.deletion_job_id === "string" ? obj.deletion_job_id : "",
    source_resource_id:
      typeof obj.source_resource_id === "string"
        ? obj.source_resource_id
        : fallbackSourceResourceId,
    status: "PENDING",
    fence_active: true,
  };
}

export function normalizeDeletionComponentReceipt(
  raw: unknown
): DeletionComponentReceiptItem {
  const obj = asRecord(raw) ?? {};
  return {
    component: (typeof obj.component === "string"
      ? obj.component
      : "JOBS_AND_OUTBOX") as DeletionComponent,
    status: (typeof obj.status === "string"
      ? obj.status
      : "PENDING") as DeletionComponentStatus,
    attempt: asNumber(obj.attempt, 0),
    purged_count: asNumber(obj.purged_count, 0),
    last_error_code:
      typeof obj.last_error_code === "string" ? obj.last_error_code : null,
    receipt_metadata: asRecord(obj.receipt_metadata) ?? {},
    updated_at: typeof obj.updated_at === "string" ? obj.updated_at : "",
    completed_at:
      typeof obj.completed_at === "string" ? obj.completed_at : null,
  };
}

export function normalizeDeletionJobStatusResponse(
  raw: unknown
): DeletionJobStatusResponse {
  const obj = asRecord(raw) ?? {};
  const rawComponents = Array.isArray(obj.components) ? obj.components : [];
  return {
    deletion_job_id:
      typeof obj.deletion_job_id === "string" ? obj.deletion_job_id : "",
    workspace_id:
      typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    source_resource_id:
      typeof obj.source_resource_id === "string" ? obj.source_resource_id : "",
    status: (typeof obj.status === "string"
      ? obj.status
      : "PENDING") as DeletionJobStatus,
    user_visible_state: (typeof obj.user_visible_state === "string"
      ? obj.user_visible_state
      : "DELETION_PENDING") as DeletionUserVisibleState,
    fence_active: Boolean(obj.fence_active),
    checkpoint: asRecord(obj.checkpoint) ?? {},
    failure_summary: asRecord(obj.failure_summary) ?? {},
    components: rawComponents.map(normalizeDeletionComponentReceipt),
    requested_by:
      typeof obj.requested_by === "string" ? obj.requested_by : null,
    requested_at: typeof obj.requested_at === "string" ? obj.requested_at : "",
    completed_at:
      typeof obj.completed_at === "string" ? obj.completed_at : null,
  };
}

export function normalizeQuotaMeterItem(raw: unknown): QuotaMeterItem {
  const obj = asRecord(raw) ?? {};
  return {
    metric_kind: (typeof obj.metric_kind === "string"
      ? obj.metric_kind
      : "SEARCH_QUERIES") as QuotaMetricKind,
    used_value: asNumber(obj.used_value, 0),
    soft_warning_limit: asNumber(obj.soft_warning_limit, 0),
    hard_cutoff_limit: asNumber(obj.hard_cutoff_limit, 0),
    utilization_percent: asNumber(obj.utilization_percent, 0),
    status: (typeof obj.status === "string"
      ? obj.status
      : "OK") as QuotaMeterStatus,
  };
}

export function normalizeQuotaStatusResponse(raw: unknown): QuotaStatusResponse {
  const obj = asRecord(raw) ?? {};
  const rawMeters = Array.isArray(obj.meters) ? obj.meters : [];
  return {
    workspace_id:
      typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    period_key: typeof obj.period_key === "string" ? obj.period_key : "",
    admission_state: (typeof obj.admission_state === "string"
      ? obj.admission_state
      : "ALLOWED") as QuotaAdmissionState,
    pause_state: (typeof obj.pause_state === "string"
      ? obj.pause_state
      : "ACTIVE") as QuotaPauseState,
    pause_reason_code:
      typeof obj.pause_reason_code === "string" ? obj.pause_reason_code : null,
    circuit_breaker_state: (typeof obj.circuit_breaker_state === "string"
      ? obj.circuit_breaker_state
      : "CLOSED") as CircuitBreakerState,
    high_cost_operations_allowed: Boolean(obj.high_cost_operations_allowed),
    safety_operations_allowed: true,
    paid_fallback_allowed: false,
    retry_after_seconds:
      typeof obj.retry_after_seconds === "number"
        ? obj.retry_after_seconds
        : null,
    free_alternatives: asStringArray(obj.free_alternatives),
    meters: rawMeters.map(normalizeQuotaMeterItem),
    checked_at: typeof obj.checked_at === "string" ? obj.checked_at : "",
  };
}

// =============================================================================
// Structured ErrorEnvelope Classifier (400, 401, 403, 404, 409, 422, 429, 503)
// =============================================================================

interface RawErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: Record<string, unknown>;
  };
}

export function classifyMemoryOpsError(
  status: number,
  rawBody: string,
  meta: MemoryOpsResponseMeta
): MemoryOpsError {
  const parsed = parseOptionalJsonBody<RawErrorEnvelope>(rawBody);
  const body = !parsed.empty && !parsed.invalidJson ? parsed.value : null;
  const envelope = body?.error;
  const code = envelope?.code ?? `HTTP_${status}`;
  const requestId = envelope?.request_id ?? meta.requestId ?? undefined;
  const details = asRecord(envelope?.details) ?? undefined;
  const retryAfterSeconds =
    meta.retryAfterSeconds ??
    (typeof details?.retry_after_seconds === "number"
      ? details.retry_after_seconds
      : null);

  if (status === 400 && code === "INVALID_SEARCH_CURSOR") {
    return {
      kind: "invalid_search_cursor",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "색인 또는 정렬 버전이 갱신되어 검색 커서가 만료되었습니다. 처음부터 다시 검색해 주세요.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 422 && code === "PROHIBITED_PROJECT_ID_SCOPE") {
    return {
      kind: "prohibited_project_id_scope",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "OD-008: project_id 범위 지정은 허용되지 않습니다. 워크스페이스와 source_resource_id만 사용할 수 있습니다.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 409 && code === "DELETION_PREVIEW_EXPIRED") {
    return {
      kind: "deletion_preview_expired",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "삭제 영향 범위 미리보기 토큰이 만료되었습니다. 삭제를 진행하지 않았으니 영향 범위를 다시 조회해 주세요.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (
    status === 409 &&
    (code === "DELETION_PREVIEW_SCOPE_CHANGED" ||
      code === "DELETION_PREVIEW_INVALID")
  ) {
    return {
      kind: "deletion_preview_scope_changed",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "미리보기 이후 저장소 버전 또는 삭제 영향 범위가 변경되어 삭제를 차단했습니다. 영향 범위를 다시 조회해 주세요.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (
    code === "DELETION_PENDING" ||
    code === "DELETION_FENCE_ACTIVE" ||
    code === "SOURCE_DELETION_PENDING"
  ) {
    return {
      kind: "deletion_pending",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "해당 저장소에 삭제 펜스(DELETION_PENDING)가 활성화되어 작업이 차단되었습니다.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 429) {
    return {
      kind: "quota_cutoff",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "사용량 한도에 도달하여 고비용 요청이 일시 제한되었습니다. 저장소 삭제(OD-003) 및 계정 연결 해제는 계속 허용됩니다.",
      requestId,
      retryable: true,
      retryAfterSeconds,
      details,
    };
  }

  if (status === 503) {
    return {
      kind: "runtime_paused",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "비용·한도 보호 일시정지 상태로 고비용 요청이 중단되었습니다. 저장소 삭제(OD-003) 및 계정 연결 해제는 계속 허용됩니다.",
      requestId,
      retryable: Boolean(envelope?.retryable ?? true),
      retryAfterSeconds,
      details,
    };
  }

  if (status === 401) {
    return {
      kind: "unauthorized",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "인증 세션이 만료되었거나 유효하지 않습니다. 다시 로그인해 주세요.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 403) {
    return {
      kind: "forbidden",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "현재 워크스페이스 권한으로 이 작업을 수행할 수 없습니다.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 404) {
    return {
      kind: "not_found",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "요청한 저장소 또는 삭제 작업을 찾을 수 없거나 접근할 수 없습니다.",
      requestId,
      retryable: false,
      details,
    };
  }

  if (status === 400 || status === 422) {
    return {
      kind: "validation_error",
      httpStatus: status,
      code,
      message: envelope?.message ?? "요청 파라미터가 유효하지 않습니다.",
      requestId,
      retryable: false,
      details,
    };
  }

  return {
    kind: "request_rejected",
    httpStatus: status,
    code,
    message:
      envelope?.message ?? `서버 요청이 거절되었습니다 (HTTP ${status}).`,
    requestId,
    retryable: Boolean(envelope?.retryable),
    retryAfterSeconds,
    details,
  };
}

// =============================================================================
// FetchMemoryOpsHttpTransport
// =============================================================================

export class FetchMemoryOpsHttpTransport implements MemoryOpsAdapter {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly keyGenerator: () => string;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: MemoryOpsHttpTransportOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.keyGenerator = options.keyGenerator ?? generateUuidV4;
    this.defaultHeaders = {
      ...(options.authorization ? { Authorization: options.authorization } : {}),
      ...(options.workspaceId ? { "X-Workspace-ID": options.workspaceId } : {}),
      ...(options.defaultHeaders ?? {}),
    };
  }

  private async requestJson<T>(params: {
    method: "GET" | "POST";
    path: string;
    headers?: Record<string, string>;
    body?: Record<string, unknown>;
    normalize: (raw: unknown, meta: MemoryOpsResponseMeta) => T;
  }): Promise<MemoryOpsResult<T>> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Request-ID": this.keyGenerator(),
      ...this.defaultHeaders,
      ...(params.headers ?? {}),
    };

    let encodedBody: string | undefined;
    if (params.body !== undefined) {
      assertNoProjectIdInRecord(params.body);
      headers["Content-Type"] = "application/json";
      encodedBody = JSON.stringify(params.body);
    }

    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${params.path}`, {
        method: params.method,
        headers,
        body: encodedBody,
      });
    } catch (err) {
      const detail =
        err instanceof Error ? err.message : "Network request failed";
      return {
        ok: false,
        error: {
          kind: "network_error",
          code: "NETWORK_ERROR",
          message: `네트워크 오류로 서버 응답을 수신하지 못했습니다(${detail}). 자동 재전송하지 않았습니다.`,
          retryable: true,
        },
      };
    }

    const meta = extractMeta(response);
    const rawBody = await response.text();

    if (!response.ok) {
      return {
        ok: false,
        error: classifyMemoryOpsError(response.status, rawBody, meta),
        meta,
      };
    }

    const parsed = parseOptionalJsonBody<unknown>(rawBody);
    if (parsed.empty || parsed.invalidJson || parsed.value === null) {
      return {
        ok: false,
        error: {
          kind: "request_rejected",
          httpStatus: response.status,
          code: "INVALID_RESPONSE_BODY",
          message: "서버 응답 JSON을 해석할 수 없습니다.",
          retryable: false,
        },
        meta,
      };
    }

    return {
      ok: true,
      data: params.normalize(parsed.value, meta),
      meta,
    };
  }

  public async searchMemory(
    query: SearchQueryInput
  ): Promise<MemoryOpsResult<SearchResponse>> {
    const searchParams = buildSafeSearchQueryParams(
      query as SearchQueryInput & Record<string, unknown>
    );
    return this.requestJson<SearchResponse>({
      method: "GET",
      path: `/v1/search?${searchParams.toString()}`,
      normalize: (raw) => normalizeSearchResponse(raw),
    });
  }

  public async previewSourceDeletion(
    sourceResourceId: string
  ): Promise<MemoryOpsResult<DeletionPreviewResponse>> {
    return this.requestJson<DeletionPreviewResponse>({
      method: "POST",
      path: `/v1/source-resources/${encodeURIComponent(sourceResourceId)}/deletion-preview`,
      normalize: (raw) => normalizeDeletionPreviewResponse(raw),
    });
  }

  public async confirmSourceDeletion(
    input: ConfirmSourceDeletionInput
  ): Promise<MemoryOpsResult<DeletionConfirmAcceptedResponse>> {
    return this.requestJson<DeletionConfirmAcceptedResponse>({
      method: "POST",
      path: `/v1/source-resources/${encodeURIComponent(input.source_resource_id)}/deletion-confirm`,
      headers: {
        "Idempotency-Key": input.idempotency_key,
      },
      body: {
        preview_token: input.preview_token,
        expected_resource_version: input.expected_resource_version,
        expected_impact_scope_version: input.expected_impact_scope_version,
        acknowledge_irreversible_purge: input.acknowledge_irreversible_purge,
      },
      normalize: (raw) =>
        normalizeDeletionConfirmAccepted(raw, input.source_resource_id),
    });
  }

  public async getDeletionJobStatus(
    deletionJobId: string
  ): Promise<MemoryOpsResult<DeletionJobStatusResponse>> {
    return this.requestJson<DeletionJobStatusResponse>({
      method: "GET",
      path: `/v1/deletion-jobs/${encodeURIComponent(deletionJobId)}`,
      normalize: (raw) => normalizeDeletionJobStatusResponse(raw),
    });
  }

  public async getQuotaStatus(
    query?: GetQuotaStatusQuery
  ): Promise<MemoryOpsResult<QuotaStatusResponse>> {
    assertNoProjectIdInRecord(
      query as unknown as Record<string, unknown> | undefined
    );
    const searchParams = new URLSearchParams();
    if (query?.source_resource_id && query.source_resource_id.trim()) {
      searchParams.set("source_resource_id", query.source_resource_id.trim());
    }
    const qs = searchParams.toString();
    return this.requestJson<QuotaStatusResponse>({
      method: "GET",
      path: qs ? `/v1/quota-status?${qs}` : "/v1/quota-status",
      normalize: (raw) => normalizeQuotaStatusResponse(raw),
    });
  }
}
