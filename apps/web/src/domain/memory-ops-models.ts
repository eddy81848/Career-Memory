/**
 * M5 Workspace Memory Search (OD-008 / T-501), Source Deletion Preview/Confirm
 * & Job Receipt (OD-003 / T-502), and Quota/Budget/Pause Status (T-503 / INF-06)
 * canonical wire-contract TypeScript models.
 *
 * Strictly enforces:
 * - OD-008: Search is scoped by authenticated Workspace + optional `source_resource_id` ONLY.
 *   `project_id` is globally prohibited and absent from all types and requests.
 * - Plan C: Zero raw source bodies, diffs, or quotes in search hits or browser storage.
 * - OD-003: Version-bound deletion preview token, 10 explicit impact counts, non-recoverable
 *   scopes notice, immediate 202 deletion fence, and 5-component deletion receipt tracking.
 * - T-503 / INF-06: `safety_operations_allowed: true` & `paid_fallback_allowed: false`
 *   invariants across 80% soft warning and 95% / PAUSED hard cutoff states.
 */

// =============================================================================
// 1. Workspace Memory Search (GET /v1/search — OD-008)
// =============================================================================

export type SearchEntityType = "WORK_EVENT" | "FACT" | "CLAIM";

export interface SearchScope {
  workspace_id: string;
  source_resource_id: string | null;
}

export interface SearchHit {
  entity_type: SearchEntityType;
  entity_id: string;
  source_resource_id: string | null;
  title_or_summary: string;
  score: number;
}

export interface SearchResponse {
  scope: SearchScope;
  query_hash: string;
  index_version: string;
  ranking_version: string;
  items: SearchHit[];
  next_cursor: string | null;
}

export interface SearchQueryInput {
  q: string;
  source_resource_id?: string | null;
  entity_types?: SearchEntityType[];
  limit?: number;
  cursor?: string | null;
}

// =============================================================================
// 2. Source Deletion Preview, Confirm & Job Receipt (OD-003 / T-502)
// =============================================================================

export interface DeletionImpactCounts {
  work_events: number;
  facts: number;
  contributions: number;
  claims_draft: number;
  claims_edited: number;
  claims_approved: number;
  claims_historical_versions: number;
  search_index_entries: number;
  ephemeral_cache_entries: number;
  server_exports: number;
}

export interface NonRecoverableScopes {
  already_downloaded_exports: true;
  provider_hosted_originals: true;
  notice: string;
}

export interface DeletionPreviewResponse {
  source_resource_id: string;
  resource_version: string;
  impact_scope_version: string;
  impact_counts: DeletionImpactCounts;
  non_recoverable_scopes: NonRecoverableScopes;
  preview_token: string;
  expires_at: string;
}

export interface DeletionConfirmRequest {
  preview_token: string;
  expected_resource_version: string;
  expected_impact_scope_version: string;
  acknowledge_irreversible_purge: true;
}

export interface DeletionConfirmAcceptedResponse {
  deletion_job_id: string;
  source_resource_id: string;
  status: "PENDING";
  fence_active: true;
}

export type DeletionJobStatus =
  | "PENDING"
  | "RUNNING"
  | "PARTIAL"
  | "SUCCEEDED"
  | "FAILED";

export type DeletionUserVisibleState =
  | "DELETION_PENDING"
  | "DELETED"
  | "DELETION_FAILED";

export type DeletionComponent =
  | "JOBS_AND_OUTBOX"
  | "EXPORTS"
  | "FTS_INDEX"
  | "EPHEMERAL_CACHE"
  | "AUTHORITATIVE_DOMAIN";

export type DeletionComponentStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED";

export interface DeletionComponentReceiptItem {
  component: DeletionComponent;
  status: DeletionComponentStatus;
  attempt: number;
  purged_count: number;
  last_error_code: string | null;
  receipt_metadata: Record<string, unknown>;
  updated_at: string;
  completed_at: string | null;
}

export interface DeletionJobStatusResponse {
  deletion_job_id: string;
  workspace_id: string;
  source_resource_id: string;
  status: DeletionJobStatus;
  user_visible_state: DeletionUserVisibleState;
  fence_active: boolean;
  checkpoint: Record<string, unknown>;
  failure_summary: Record<string, unknown>;
  components: DeletionComponentReceiptItem[];
  requested_by: string | null;
  requested_at: string;
  completed_at: string | null;
}

// =============================================================================
// 3. Quota / Budget / Pause Status (GET /v1/quota-status — T-503 / INF-06)
// =============================================================================

export type QuotaAdmissionState =
  | "ALLOWED"
  | "WARNING_80_PERCENT"
  | "CUTOFF_95_PERCENT"
  | "PAUSED";

export type QuotaPauseState =
  | "ACTIVE"
  | "PAUSED_QUOTA"
  | "PAUSED_CIRCUIT_BREAKER"
  | "PAUSED_OPERATOR";

export type CircuitBreakerState = "CLOSED" | "OPEN" | "HALF_OPEN";

export type QuotaMetricKind =
  | "IMPORT_OBJECTS"
  | "PROVIDER_REQUESTS"
  | "REFETCH_BYTES"
  | "EXPORT_BUILDS"
  | "SEARCH_QUERIES"
  | "ESTIMATED_COST_KRW"
  | "LLM_TOKENS";

export type QuotaMeterStatus = "OK" | "WARNING" | "CUTOFF";

export interface QuotaMeterItem {
  metric_kind: QuotaMetricKind;
  used_value: number;
  soft_warning_limit: number;
  hard_cutoff_limit: number;
  utilization_percent: number;
  status: QuotaMeterStatus;
}

export interface QuotaStatusResponse {
  workspace_id: string;
  period_key: string;
  admission_state: QuotaAdmissionState;
  pause_state: QuotaPauseState;
  pause_reason_code: string | null;
  circuit_breaker_state: CircuitBreakerState;
  high_cost_operations_allowed: boolean;
  safety_operations_allowed: true;
  paid_fallback_allowed: false;
  retry_after_seconds: number | null;
  free_alternatives: string[];
  meters: QuotaMeterItem[];
  checked_at: string;
}

// =============================================================================
// 4. Workspace Repository Metadata, Scenarios & Structured Errors
// =============================================================================

export interface WorkspaceSourceResourceSummary {
  source_resource_id: string;
  repository_full_name: string;
  provider: "GITHUB";
  visibility: "PUBLIC";
  fence_active: boolean;
  deleted: boolean;
  active_deletion_job_id: string | null;
}

export type MemoryOpsScenarioId =
  | "default_active"
  | "index_lag_filtered"
  | "cursor_version_drift"
  | "quota_warning_80"
  | "quota_cutoff_95_paused"
  | "deletion_scope_changed_409"
  | "deletion_partial_pending"
  | "deletion_completed";

export type MemoryOpsErrorKind =
  | "invalid_search_cursor"
  | "prohibited_project_id_scope"
  | "deletion_preview_expired"
  | "deletion_preview_scope_changed"
  | "deletion_pending"
  | "quota_cutoff"
  | "runtime_paused"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "validation_error"
  | "network_error"
  | "request_rejected";

export interface MemoryOpsError {
  kind: MemoryOpsErrorKind;
  httpStatus?: number;
  code: string;
  message: string;
  requestId?: string;
  retryable: boolean;
  retryAfterSeconds?: number | null;
  details?: Record<string, unknown>;
}

export interface MemoryOpsResponseMeta {
  httpStatus: number;
  cacheControl?: string | null;
  requestId?: string | null;
  retryAfterSeconds?: number | null;
}

export type MemoryOpsResult<T> =
  | { ok: true; data: T; meta?: MemoryOpsResponseMeta }
  | { ok: false; error: MemoryOpsError; meta?: MemoryOpsResponseMeta };
