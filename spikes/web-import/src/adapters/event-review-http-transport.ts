import type {
  ConfirmWorkEventInput,
  DissolveWorkEventInput,
  EditWorkEventInput,
  EventReviewAdapter,
  EventReviewResponseMeta,
  EventReviewResult,
  ListWorkEventsQuery,
  MergeWorkEventsInput,
  RejectWorkEventInput,
  SplitWorkEventInput,
} from "@/adapters/event-review-adapter";
import {
  generateUuidV4,
  parseOptionalJsonBody,
} from "@/adapters/synthetic-http-transport";
import type {
  AllowedEventAction,
  DissolveWorkEventResponse,
  EventReviewError,
  MergeWorkEventsResponse,
  SplitWorkEventResponse,
  WorkEventDetailResponse,
  WorkEventListResponse,
  WorkEventStatus,
} from "@/domain/event-review-models";

/**
 * Real `fetch`-based HTTP transport implementing the M3 Event Review wire contract
 * (`T-303` / `T-304` / `ER-004`).
 *
 * Guarantees:
 * - Sends `If-Match: "v{expected_version}"` and `Idempotency-Key` on single-event mutations
 *   and `Idempotency-Key` + member `expected_version` array on merge.
 * - Maps Keep (확정) strictly to `POST /v1/work-events/{id}/actions/confirm`.
 * - NEVER auto-retries or auto-POSTs on `409 EVENT_VERSION_CONFLICT` or network errors.
 * - Safely parses optional/empty error bodies without throwing JSON syntax errors.
 */

export interface EventReviewHttpTransportOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  keyGenerator?: () => string;
  defaultHeaders?: Record<string, string>;
}

interface RawApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: {
      current_version?: number;
      current_status?: WorkEventStatus;
      allowed_actions?: AllowedEventAction[];
      event_id?: string;
    };
  };
}

function buildVersionEtag(version: number): string {
  return `"v${version}"`;
}

function extractMeta(res: Response): EventReviewResponseMeta {
  return {
    httpStatus: res.status,
    etag: res.headers.get("etag"),
    cacheControl: res.headers.get("cache-control"),
    requestId: res.headers.get("x-request-id"),
  };
}

function classifyErrorResponse(
  status: number,
  rawBody: string,
  meta: EventReviewResponseMeta
): EventReviewError {
  const parsed = parseOptionalJsonBody<RawApiErrorEnvelope>(rawBody);
  const envelope = !parsed.empty && !parsed.invalidJson ? parsed.value?.error : undefined;
  const code = envelope?.code ?? `HTTP_${status}`;
  const requestId = envelope?.request_id ?? meta.requestId ?? undefined;

  if (status === 409 && code === "EVENT_VERSION_CONFLICT") {
    const details = envelope?.details;
    const currentVersion =
      typeof details?.current_version === "number" ? details.current_version : 0;
    const currentStatus = details?.current_status ?? "CANDIDATE";
    const allowedActions = Array.isArray(details?.allowed_actions)
      ? details.allowed_actions
      : [];

    return {
      kind: "version_conflict",
      httpStatus: status,
      code: "EVENT_VERSION_CONFLICT",
      message:
        envelope?.message ??
        `다른 탭이나 작업에서 Event가 먼저 수정되어 최신 버전(v${currentVersion})과 충돌했습니다. 자동 덮어쓰기를 수행하지 않았으니 최신 상태를 다시 불러온 뒤 재적용하세요.`,
      requestId,
      retryable: false,
      conflictDetails: {
        current_version: currentVersion,
        current_status: currentStatus,
        allowed_actions: allowedActions,
        event_id: details?.event_id,
      },
    };
  }

  if (status === 409 && code === "CANNOT_LINK_CONFLICT") {
    return {
      kind: "cannot_link_conflict",
      httpStatus: status,
      code: "CANNOT_LINK_CONFLICT",
      message:
        envelope?.message ??
        "선택한 Event 사이에 CANNOT_LINK 제약이 있어 병합할 수 없습니다.",
      requestId,
      retryable: false,
    };
  }

  if (code === "POLICY_BLOCKED" || code === "SOURCE_POLICY_BLOCKED") {
    return {
      kind: "policy_blocked",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "공개 정책 차단(POLICY_BLOCKED)으로 인해 이 Event를 수정할 수 없습니다. 원본 삭제가 아니며 정책 재확인이 필요합니다.",
      requestId,
      retryable: false,
    };
  }

  if (code === "DELETION_PENDING" || code === "DELETION_FENCE_ACTIVE") {
    return {
      kind: "deletion_pending",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "삭제 펜스(DELETION_PENDING)가 활성화되어 변경 명령이 즉시 차단되었습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 401) {
    return {
      kind: "unauthorized",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "인증 세션이 유효하지 않아 요청을 처리하지 못했습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 404) {
    return {
      kind: "not_found",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "요청한 Event를 찾을 수 없거나 접근 권한이 없습니다.",
      requestId,
      retryable: false,
    };
  }

  if (status === 400 || status === 422) {
    return {
      kind: "validation_error",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "요청 입력값이 유효하지 않아 서버가 거절했습니다.",
      requestId,
      retryable: false,
    };
  }

  return {
    kind: "request_rejected",
    httpStatus: status,
    code,
    message:
      envelope?.message ??
      `서버 요청이 처리되지 않았습니다 (HTTP ${status}).`,
    requestId,
    retryable: envelope?.retryable ?? false,
  };
}

export class FetchEventReviewHttpTransport implements EventReviewAdapter {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly keyGenerator: () => string;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: EventReviewHttpTransportOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.keyGenerator = options.keyGenerator ?? generateUuidV4;
    this.defaultHeaders = options.defaultHeaders ?? {};
  }

  private async requestJson<T>(params: {
    method: "GET" | "POST" | "PATCH";
    path: string;
    headers?: Record<string, string>;
    body?: Record<string, unknown>;
  }): Promise<EventReviewResult<T>> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Request-ID": this.keyGenerator(),
      ...this.defaultHeaders,
      ...(params.headers ?? {}),
    };

    let encodedBody: string | undefined;
    if (params.body !== undefined) {
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
      const detail = err instanceof Error ? err.message : "Network request failed";
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
        error: classifyErrorResponse(response.status, rawBody, meta),
        meta,
      };
    }

    const parsed = parseOptionalJsonBody<T>(rawBody);
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
      data: parsed.value,
      meta,
    };
  }

  public async listWorkEvents(
    query?: ListWorkEventsQuery
  ): Promise<EventReviewResult<WorkEventListResponse>> {
    const search = new URLSearchParams();
    search.set("limit", String(query?.limit ?? 50));
    if (query?.status && query.status !== "ALL" && query.status !== "ACTIVE") {
      search.set("status", query.status);
    }
    if (query?.source_resource_id) {
      search.set("source_resource_id", query.source_resource_id);
    }
    if (query?.cursor) {
      search.set("cursor", query.cursor);
    }
    return this.requestJson<WorkEventListResponse>({
      method: "GET",
      path: `/v1/work-events?${search.toString()}`,
    });
  }

  public async getWorkEventDetail(
    eventId: string
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const result = await this.requestJson<WorkEventDetailResponse>({
      method: "GET",
      path: `/v1/work-events/${encodeURIComponent(eventId)}`,
    });
    if (result.ok && result.meta?.etag && !result.data.etag) {
      return {
        ...result,
        data: {
          ...result.data,
          etag: result.meta.etag,
        },
      };
    }
    return result;
  }

  public async confirmWorkEvent(
    input: ConfirmWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/confirm`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User confirmed candidate event",
        idempotency_key: idempotencyKey,
      },
    });
  }

  public async editWorkEvent(
    input: EditWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "PATCH",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        title: input.title,
        problem: input.problem,
        action: input.action,
        outcome: input.outcome,
        reason: input.reason ?? "User edited event details",
        idempotency_key: idempotencyKey,
      },
    });
  }

  public async rejectWorkEvent(
    input: RejectWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<WorkEventDetailResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/reject`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User excluded candidate event",
        idempotency_key: idempotencyKey,
      },
    });
  }

  public async splitWorkEvent(
    input: SplitWorkEventInput
  ): Promise<EventReviewResult<SplitWorkEventResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<SplitWorkEventResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/split`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User split mixed-purpose event",
        idempotency_key: idempotencyKey,
        partitions: input.partitions,
      },
    });
  }

  public async mergeWorkEvents(
    input: MergeWorkEventsInput
  ): Promise<EventReviewResult<MergeWorkEventsResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<MergeWorkEventsResponse>({
      method: "POST",
      path: "/v1/work-events/actions/merge",
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        members: input.members,
        title: input.title,
        reason: input.reason ?? "User merged related work events",
        idempotency_key: idempotencyKey,
      },
    });
  }

  public async dissolveWorkEvent(
    input: DissolveWorkEventInput
  ): Promise<EventReviewResult<DissolveWorkEventResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<DissolveWorkEventResponse>({
      method: "POST",
      path: `/v1/work-events/${encodeURIComponent(input.event_id)}/actions/dissolve`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason ?? "User dissolved composite event",
        idempotency_key: idempotencyKey,
      },
    });
  }
}
