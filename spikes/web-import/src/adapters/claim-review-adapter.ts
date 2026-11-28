import type {
  ClaimDetailResponse,
  ClaimGenerationResponse,
  ClaimListResponse,
  ClaimReviewError,
  ClaimReviewScenarioId,
  ClaimReviewWorkspaceGuard,
  ClaimSupportsResponse,
  DeleteExportResponse,
  EditClaimPatchResponse,
  EphemeralContentResponse,
  ExportDetailResponse,
  ExportFormat,
  ExportPolicy,
  ExportSummaryItem,
  FactListResponse,
  FactMutationResponse,
  FactNormalizedValue,
  FactSupportsResponse,
  SourceResolutionStatus,
} from "@/domain/claim-review-models";

/**
 * 1:1 backend wire adapter contract for M4 Grounded Claim Review,
 * Fact Review, Plan C Ephemeral Source Viewer, and Zero-Quote Export (T-406).
 */

export interface ClaimReviewResponseMeta {
  httpStatus?: number;
  etag?: string | null;
  cacheControl?: string | null;
  requestId?: string | null;
}

export type ClaimReviewResult<T> =
  | {
      ok: true;
      data: T;
      meta?: ClaimReviewResponseMeta;
    }
  | {
      ok: false;
      error: ClaimReviewError;
      meta?: ClaimReviewResponseMeta;
    };

export interface ListFactsQuery {
  event_id?: string;
  include_superseded?: boolean;
  cursor?: string;
  limit?: number;
}

export interface ApproveFactInput {
  fact_id: string;
  expected_version: number;
  reason?: string;
  idempotency_key?: string;
}

export interface RejectFactInput {
  fact_id: string;
  expected_version: number;
  reason: string;
  idempotency_key?: string;
}

export interface EditFactInput {
  fact_id: string;
  expected_version: number;
  predicate: string;
  object_text: string;
  normalized_value?: FactNormalizedValue | null;
  reason?: string;
  idempotency_key?: string;
}

export interface ListClaimsQuery {
  event_id?: string;
  include_superseded?: boolean;
  cursor?: string;
  limit?: number;
}

export interface GenerateClaimsInput {
  confirmed_event_id: string;
  input_fact_ids?: string[];
  idempotency_key?: string;
}

export interface EditClaimInput {
  claim_id: string;
  expected_version: number;
  claim_text: string;
  edit_reason?: string;
  idempotency_key?: string;
}

export interface RevalidateClaimInput {
  claim_id: string;
  expected_version: number;
  idempotency_key?: string;
}

export interface ApproveClaimInput {
  claim_id: string;
  expected_version: number;
  expected_support_graph_epoch?: number;
  acknowledge_user_confirmed_atoms: boolean;
  idempotency_key?: string;
}

export interface RejectClaimInput {
  claim_id: string;
  expected_version: number;
  reason: string;
  idempotency_key?: string;
}

export interface GetContentUnitContentInput {
  content_unit_id: string;
  workspace_id: string;
  include_context?: boolean;
}

export interface CreateExportInput {
  format: ExportFormat;
  claim_ids: string[];
  expected_claim_versions?: Record<string, number>;
  export_policy: ExportPolicy;
  source_resource_id?: string;
  idempotency_key?: string;
}

export interface DeleteExportInput {
  export_id: string;
  idempotency_key?: string;
}

export interface ExportListResponse {
  items: ExportSummaryItem[];
}

export interface ClaimReviewAdapter {
  // --- Facts (/v1/facts*) ---
  listFacts(query?: ListFactsQuery): Promise<ClaimReviewResult<FactListResponse>>;
  getFactSupports(factId: string): Promise<ClaimReviewResult<FactSupportsResponse>>;
  approveFact(input: ApproveFactInput): Promise<ClaimReviewResult<FactMutationResponse>>;
  rejectFact(input: RejectFactInput): Promise<ClaimReviewResult<FactMutationResponse>>;
  editFact(input: EditFactInput): Promise<ClaimReviewResult<FactMutationResponse>>;

  // --- Claims (/v1/claims*) ---
  listClaims(query?: ListClaimsQuery): Promise<ClaimReviewResult<ClaimListResponse>>;
  generateClaims(
    input: GenerateClaimsInput
  ): Promise<ClaimReviewResult<ClaimGenerationResponse>>;
  getClaimDetail(claimId: string): Promise<ClaimReviewResult<ClaimDetailResponse>>;
  getClaimSupports(claimId: string): Promise<ClaimReviewResult<ClaimSupportsResponse>>;
  editClaim(input: EditClaimInput): Promise<ClaimReviewResult<EditClaimPatchResponse>>;
  revalidateClaim(
    input: RevalidateClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>>;
  approveClaim(
    input: ApproveClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>>;
  rejectClaim(input: RejectClaimInput): Promise<ClaimReviewResult<ClaimDetailResponse>>;

  // --- Plan C Ephemeral Content Unit Viewer (/v1/content-units/{id}/content) ---
  getContentUnitContent(
    input: GetContentUnitContentInput
  ): Promise<ClaimReviewResult<EphemeralContentResponse>>;

  // --- Exports (/v1/exports*) ---
  listExports(): Promise<ClaimReviewResult<ExportListResponse>>;
  createExport(input: CreateExportInput): Promise<ClaimReviewResult<ExportDetailResponse>>;
  getExportDetail(exportId: string): Promise<ClaimReviewResult<ExportDetailResponse>>;
  deleteExport(input: DeleteExportInput): Promise<ClaimReviewResult<DeleteExportResponse>>;
}

export type EphemeralResolutionOverrideMode =
  | "DEFAULT"
  | SourceResolutionStatus
  | "UNAVAILABLE_PROVIDER_404"
  | "UNAVAILABLE_RETRYABLE_503";

export interface ClaimReviewHarnessControls {
  setScenario(scenarioId: ClaimReviewScenarioId): void;
  getScenario(): ClaimReviewScenarioId;
  getWorkspaceGuard(eventId?: string): ClaimReviewWorkspaceGuard;
  simulateConcurrentClaimUpdate(claimId: string): void;
  setEphemeralResolutionOverride(mode: EphemeralResolutionOverrideMode): void;
  getEphemeralResolutionOverride(): EphemeralResolutionOverrideMode;
  reset(): void;
}
