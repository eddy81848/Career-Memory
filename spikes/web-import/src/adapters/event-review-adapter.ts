import type {
  DissolveWorkEventResponse,
  EventReviewError,
  EventReviewScenarioId,
  MergeWorkEventsResponse,
  SplitPartitionInput,
  SplitWorkEventResponse,
  WorkEventDetailResponse,
  WorkEventListResponse,
  WorkEventStatus,
} from "@/domain/event-review-models";

/**
 * 1:1 backend wire adapter contract for M3 Event Review & Curation (T-303 / T-304).
 *
 * All mutating commands require optimistic concurrency (`expected_version` ->
 * `If-Match: "v{expected_version}"` + JSON `expected_version`) and explicit
 * `Idempotency-Key`. Keep (확정) ALWAYS maps to `confirmWorkEvent`
 * (`POST /v1/work-events/{id}/actions/confirm`) — never a separate `/keep` route.
 */

export interface EventReviewResponseMeta {
  httpStatus?: number;
  etag?: string | null;
  cacheControl?: string | null;
  requestId?: string | null;
}

export type EventReviewResult<T> =
  | {
      ok: true;
      data: T;
      meta?: EventReviewResponseMeta;
    }
  | {
      ok: false;
      error: EventReviewError;
      meta?: EventReviewResponseMeta;
    };

export interface ListWorkEventsQuery {
  limit?: number;
  status?: WorkEventStatus | "ALL" | "ACTIVE";
  source_resource_id?: string;
  cursor?: string;
}

export interface ConfirmWorkEventInput {
  event_id: string;
  expected_version: number;
  reason?: string;
  idempotency_key?: string;
}

export interface EditWorkEventInput {
  event_id: string;
  expected_version: number;
  title: string;
  problem: string | null;
  action: string | null;
  outcome: string | null;
  reason?: string;
  idempotency_key?: string;
}

export interface RejectWorkEventInput {
  event_id: string;
  expected_version: number;
  reason?: string;
  idempotency_key?: string;
}

export interface SplitWorkEventInput {
  event_id: string;
  expected_version: number;
  reason?: string;
  idempotency_key?: string;
  partitions: SplitPartitionInput[];
}

export interface MergeWorkEventsInput {
  members: Array<{
    event_id: string;
    expected_version: number;
  }>;
  title: string;
  reason?: string;
  idempotency_key?: string;
}

export interface DissolveWorkEventInput {
  event_id: string;
  expected_version: number;
  reason?: string;
  idempotency_key?: string;
}

export interface EventReviewAdapter {
  listWorkEvents(
    query?: ListWorkEventsQuery
  ): Promise<EventReviewResult<WorkEventListResponse>>;

  getWorkEventDetail(
    eventId: string
  ): Promise<EventReviewResult<WorkEventDetailResponse>>;

  /** Keep (확정) -> POST /v1/work-events/{id}/actions/confirm */
  confirmWorkEvent(
    input: ConfirmWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>>;

  /** Edit (편집) -> PATCH /v1/work-events/{id} */
  editWorkEvent(
    input: EditWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>>;

  /** Exclude (제외) -> POST /v1/work-events/{id}/actions/reject */
  rejectWorkEvent(
    input: RejectWorkEventInput
  ): Promise<EventReviewResult<WorkEventDetailResponse>>;

  /** Split (범위 분할) -> POST /v1/work-events/{id}/actions/split */
  splitWorkEvent(
    input: SplitWorkEventInput
  ): Promise<EventReviewResult<SplitWorkEventResponse>>;

  /** Merge (USER_COMPOSITE 수동 병합) -> POST /v1/work-events/actions/merge */
  mergeWorkEvents(
    input: MergeWorkEventsInput
  ): Promise<EventReviewResult<MergeWorkEventsResponse>>;

  /** Dissolve (USER_COMPOSITE 병합 해제) -> POST /v1/work-events/{id}/actions/dissolve */
  dissolveWorkEvent(
    input: DissolveWorkEventInput
  ): Promise<EventReviewResult<DissolveWorkEventResponse>>;
}

export interface EventReviewHarnessControls {
  setScenario(scenarioId: EventReviewScenarioId): void;
  getScenario(): EventReviewScenarioId;
  simulateConcurrentUpdate(eventId: string): void;
  reset(): void;
}
