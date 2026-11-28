import type {
  DeletionConfirmAcceptedResponse,
  DeletionJobStatusResponse,
  DeletionPreviewResponse,
  MemoryOpsResult,
  MemoryOpsScenarioId,
  QuotaStatusResponse,
  SearchQueryInput,
  SearchResponse,
  WorkspaceSourceResourceSummary,
} from "@/domain/memory-ops-models";

/**
 * Adapter interface for M5 Workspace Memory Search (OD-008),
 * Source Deletion Preview/Confirm & Receipt polling (OD-003),
 * and Quota / Budget / Pause status (T-503 / INF-06).
 */

export interface ConfirmSourceDeletionInput {
  source_resource_id: string;
  preview_token: string;
  expected_resource_version: string;
  expected_impact_scope_version: string;
  acknowledge_irreversible_purge: true;
  /** Caller-managed UUID v4 preserved across retries of the same confirm attempt. */
  idempotency_key: string;
}

export interface GetQuotaStatusQuery {
  source_resource_id?: string | null;
}

export interface MemoryOpsAdapter {
  searchMemory(query: SearchQueryInput): Promise<MemoryOpsResult<SearchResponse>>;

  previewSourceDeletion(
    sourceResourceId: string
  ): Promise<MemoryOpsResult<DeletionPreviewResponse>>;

  confirmSourceDeletion(
    input: ConfirmSourceDeletionInput
  ): Promise<MemoryOpsResult<DeletionConfirmAcceptedResponse>>;

  getDeletionJobStatus(
    deletionJobId: string
  ): Promise<MemoryOpsResult<DeletionJobStatusResponse>>;

  getQuotaStatus(
    query?: GetQuotaStatusQuery
  ): Promise<MemoryOpsResult<QuotaStatusResponse>>;
}

export interface MemoryOpsHarnessControls {
  getScenario(): MemoryOpsScenarioId;
  setScenario(scenario: MemoryOpsScenarioId): void;
  getRepositories(): WorkspaceSourceResourceSummary[];
  getInitialActiveDeletionJobId(): string | null;
  getScenarioNotice(): string | null;
  setRepositoryCountMode(mode: "single" | "multi"): void;
  getRepositoryCountMode(): "single" | "multi";
  advanceDeletionJob(deletionJobId: string): void;
  reset(): void;
}
