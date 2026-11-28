/**
 * Domain models and canonical wire DTOs for M4 Wave 3B Grounded Claim Review,
 * Provenance Citation Drill-down, Plan C Ephemeral Source Viewer, and Export UI (T-406).
 */

import type { PolicyBlockStatus } from "./event-review-models";

// =============================================================================
// Facts (/v1/facts*)
// =============================================================================

export type FactType =
  | "PROBLEM"
  | "ACTION"
  | "TECHNOLOGY"
  | "RESULT"
  | "ROLE"
  | "METRIC"
  | "DECISION"
  | "CONTRIBUTION";

export type FactStatus =
  | "CANDIDATE"
  | "SUPPORTED"
  | "USER_CONFIRMED"
  | "CONFLICTED"
  | "REJECTED"
  | "STALE"
  | "SUPERSEDED";

export type SupportType = "DIRECT" | "CONTEXT" | "CONTRADICTS";

export type LinkStatus = "ACTIVE" | "INVALIDATED" | "STALE";

export type SupportValidator = "RULE" | "LLM" | "USER" | "HYBRID";

export type ContentUnitType =
  | "PR_TITLE"
  | "PR_BODY_SECTION"
  | "COMMIT_MESSAGE"
  | "DIFF_HUNK"
  | "REVIEW_COMMENT"
  | "ISSUE_SECTION"
  | "FILE_SNIPPET";

export interface SpanLocatorV1 {
  schema?: "cm-locator-v1" | string;
  file_path?: string | null;
  section?: string | null;
  hunk_header?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  start_char?: number | null;
  end_char?: number | null;
  span_sha256?: string | null;
  source_url?: string | null;
  [key: string]: unknown;
}

export interface MetricDetailsDto {
  value: string;
  unit: string;
  period: string;
  source_basis: string;
  environment_scope: "PRODUCTION" | "STAGING_TEST" | "UNSPECIFIED" | string;
  measurement_scope: string;
}

export interface FactNormalizedValue {
  completion_state?: "COMPLETED" | "PLANNED" | "NEGATED" | "TARGET_UNMET" | string;
  scope?: "INDIVIDUAL" | "TEAM" | "UNSPECIFIED" | string;
  environment_scope?: "PRODUCTION" | "STAGING_TEST" | "UNSPECIFIED" | string;
  causal_basis?: "DIRECT_CAUSATION" | "OBSERVED_CORRELATION" | "UNSPECIFIED" | string;
  uncertainty_notes?: string | null;
  metric_details?: Partial<MetricDetailsDto> | null;
  // Flat metric fields supported by some extractors
  value?: string;
  unit?: string;
  period?: string;
  source_basis?: string;
  measurement_scope?: string;
  role?: string;
  identity_status?: string;
  [key: string]: unknown;
}

export interface FactSummaryItem {
  fact_id: string;
  workspace_id: string;
  event_id: string;
  version: number;
  supersedes_fact_id: string | null;
  fact_type: FactType;
  subject_identity_id: string | null;
  predicate: string;
  object_text: string;
  normalized_value: FactNormalizedValue | null;
  fact_metadata: Record<string, unknown>;
  status: FactStatus;
  extractor_version: string;
  direct_support_count: number;
  context_support_count: number;
  contradicts_support_count: number;
  etag: string;
  created_at: string;
  updated_at: string;
}

export interface FactListResponse {
  items: FactSummaryItem[];
  page: {
    next_cursor: string | null;
    has_more: boolean;
  };
}

/**
 * Plan C (ADR-013 / REF-04): `extracted_span` is strictly absent from wire DTOs.
 */
export interface FactSupportItem {
  support_id: string;
  content_unit_id: string;
  source_object_revision_id: string | null;
  unit_type: ContentUnitType | string;
  support_type: SupportType;
  status: LinkStatus;
  validator: SupportValidator | string;
  validator_version: string;
  locator?: SpanLocatorV1 | null;
  revision_content_hash?: string | null;
  revision_observed_at?: string | null;
  revision_is_tombstone?: boolean;
  last_checked_at?: string | null;
  last_resolution_status?: SourceResolutionStatus | null;
  support_metadata: Record<string, unknown>;
  created_at: string;
}

export interface FactSupportsResponse {
  fact: FactSummaryItem;
  items: FactSupportItem[];
}

export interface FactInvalidationCounts {
  contributions: number;
  contribution_supports: number;
  claim_atoms: number;
  claim_atom_supports: number;
  claims: number;
  exports: number;
}

export interface FactMutationResponse {
  fact: FactSummaryItem;
  superseded_fact_id?: string | null;
  superseded_version?: number | null;
  invalidated_counts?: FactInvalidationCounts;
  idempotent_replay?: boolean;
}

// =============================================================================
// Claims (/v1/claims*)
// =============================================================================

export type ClaimStatus =
  | "DRAFT"
  | "DRAFT_INVALID"
  | "SUPPORT_VALIDATED"
  | "APPROVED"
  | "REJECTED"
  | "STALE"
  | "SUPERSEDED";

/**
 * Strictly read-only server-computed provenance verification level.
 * NEVER writable by UI inputs or mutation payloads.
 */
export type ComputedVerification =
  | "SOURCE_VERIFIED"
  | "USER_CONFIRMED"
  | "MIXED"
  | "UNVERIFIED";

export type ClaimAtomType =
  | "PROBLEM"
  | "ACTION"
  | "TECHNOLOGY"
  | "RESULT"
  | "ROLE"
  | "METRIC";

export type ClaimAtomStatus =
  | "SUPPORTED"
  | "USER_CONFIRMED"
  | "UNSUPPORTED"
  | "STALE";

export interface ClaimSummaryItem {
  claim_id: string;
  workspace_id: string;
  event_id: string;
  version: number;
  supersedes_claim_id: string | null;
  claim_text: string;
  status: ClaimStatus;
  computed_verification: ComputedVerification;
  atom_count: number;
  supported_atom_count: number;
  user_confirmed_atom_count: number;
  unsupported_atom_count: number;
  stale_atom_count: number;
  approvable: boolean;
  approval_block_reasons: string[];
  support_graph_epoch: number;
  approved_at: string | null;
  last_verified_at: string | null;
  generator_run_id: string | null;
  last_verification_run_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClaimListResponse {
  items: ClaimSummaryItem[];
  page: {
    next_cursor: string | null;
    has_more: boolean;
  };
}

export interface ClaimAtomDetailItem {
  atom_id: string;
  ordinal: number;
  atom_type: ClaimAtomType;
  atom_text: string;
  status: ClaimAtomStatus;
  block_reason?: string | null;
  role_grounding?: {
    role?: string | null;
    identity_status?: string | null;
    can_support_source_verified?: boolean;
    note?: string | null;
  } | null;
  metric_details?: Partial<MetricDetailsDto> | null;
}

export interface ClaimDetailResponse extends ClaimSummaryItem {
  atoms: ClaimAtomDetailItem[];
  needs_revalidation?: boolean;
  policy_status?: PolicyBlockStatus;
}

export interface ClaimFactSupportNode {
  fact_support_id: string;
  support_type: SupportType;
  status: LinkStatus;
  validator: SupportValidator | string;
  validator_version: string;
  content_unit_id: string;
  unit_type: ContentUnitType | string;
  locator: SpanLocatorV1 | null;
  source_object_revision_id: string | null;
  revision_content_hash: string | null;
  revision_observed_at: string | null;
  revision_is_tombstone: boolean;
  last_checked_at: string | null;
  last_resolution_status: SourceResolutionStatus | null;
}

export interface ClaimAtomSupportLink {
  claim_atom_support_id: string;
  support_type: SupportType;
  status: LinkStatus;
  validation_run_id: string | null;
  fact: {
    fact_id: string;
    version: number;
    fact_type: FactType;
    predicate: string;
    object_text: string;
    status: FactStatus;
    normalized_value: FactNormalizedValue | null;
    subject_identity_id?: string | null;
  };
  fact_supports: ClaimFactSupportNode[];
}

export interface ClaimAtomSupportsItem {
  atom_id: string;
  ordinal: number;
  atom_type: ClaimAtomType;
  atom_text: string;
  status: ClaimAtomStatus;
  supports: ClaimAtomSupportLink[];
}

export interface ClaimSupportsResponse {
  claim_id: string;
  version: number;
  event_id: string;
  status: ClaimStatus;
  computed_verification: ComputedVerification;
  support_graph_epoch: number;
  atoms: ClaimAtomSupportsItem[];
}

export type ClaimGenerationStatus =
  | "SUCCEEDED"
  | "MANUAL_ONLY_BLOCKED"
  | "DRAFT_INVALID";

export interface ClaimGenerationResponse {
  event_id: string;
  generation_status: ClaimGenerationStatus;
  reason_code: string | null;
  reason_message?: string | null;
  free_alternatives: string[];
  claims: ClaimDetailResponse[];
}

export interface EditClaimPatchResponse {
  claim_id: string;
  workspace_id: string;
  event_id: string;
  version: number;
  supersedes_claim_id?: string | null;
  claim_text: string;
  status: "DRAFT" | ClaimStatus;
  computed_verification: ComputedVerification;
}

// =============================================================================
// Plan C Ephemeral Content Unit Viewer (/v1/content-units/{id}/content)
// =============================================================================

export type SourceResolutionStatus =
  | "MATCHED"
  | "REVISION_MISMATCH"
  | "INVALID_LOCATOR"
  | "POLICY_BLOCKED"
  | "UNAVAILABLE"
  | "PARTIAL_WITH_LIMIT";

export interface SourceResolutionMetadata {
  status: SourceResolutionStatus;
  checked_at: string;
  expected_revision_hash: string | null;
  observed_revision_hash: string | null;
  retryable: boolean;
  reason_code: string | null;
}

export interface EphemeralContentContext {
  section: string | null;
  file_path: string | null;
  previous_file_path: string | null;
  hunk_header: string | null;
  preceding_text: string | null;
  following_text: string | null;
}

/**
 * Ephemeral text MUST remain in React component memory ONLY —
 * never written to localStorage, sessionStorage, IndexedDB, URL params, or console.
 */
export interface EphemeralContentResponse {
  content_unit_id: string;
  unit_type: ContentUnitType | string;
  content: string;
  context: EphemeralContentContext | null;
  source_resolution: SourceResolutionMetadata;
}

export type EphemeralViewerUiState =
  | "IDLE_METADATA_ONLY"
  | "LOADING"
  | "MATCHED"
  | "CHANGED"
  | "UNAVAILABLE"
  | "POLICY_BLOCKED"
  | "LIMIT";

// =============================================================================
// Exports (/v1/exports*)
// =============================================================================

export type ExportFormat = "MARKDOWN" | "JSON";

export type ExportPolicy = "SOURCE_VERIFIED_ONLY" | "ALLOW_USER_CONFIRMED";

export type ExportStatus = "ACTIVE" | "READY" | "INVALIDATED" | "DELETED";

export interface ExportCitationManifestItem {
  claim_id: string;
  claim_version: number;
  computed_verification: ComputedVerification;
  source_url: string | null;
  locator: SpanLocatorV1 | null;
  revision_content_hash: string | null;
  observed_at: string | null;
  checked_at: string | null;
}

export interface ExportManifest {
  export_policy: ExportPolicy;
  claim_count: number;
  includes_user_confirmed: boolean;
  contains_raw_quotes: false;
  citations: ExportCitationManifestItem[];
}

export interface ExportSummaryItem {
  export_id: string;
  workspace_id: string;
  format: ExportFormat;
  export_policy: ExportPolicy;
  status: ExportStatus;
  claim_ids: string[];
  invalidation_reason: string | null;
  created_at: string;
  updated_at?: string;
}

export interface ExportDetailResponse extends ExportSummaryItem {
  rendered_markdown: string | null;
  rendered_json: Record<string, unknown> | null;
  manifest: ExportManifest;
}

export interface DeleteExportResponse {
  export_id: string;
  status: "DELETED";
  deleted_at: string;
}

// =============================================================================
// Entry Guard, Errors & Scenarios
// =============================================================================

export interface ClaimReviewWorkspaceGuard {
  import_ready: boolean;
  import_status: "SUCCESS" | "PARTIAL" | "FAILED" | "RUNNING";
  policy_status: PolicyBlockStatus;
  event_id: string;
  event_title: string;
  event_status: "CONFIRMED" | "CANDIDATE" | "STALE" | "REJECTED" | "SUPERSEDED";
  repository_label: string;
  notice?: string | null;
}

export type ClaimReviewErrorKind =
  | "version_conflict"
  | "support_incomplete"
  | "source_revision_mismatch"
  | "policy_blocked"
  | "deletion_pending"
  | "source_unavailable"
  | "source_limit"
  | "validation_error"
  | "unauthorized"
  | "not_found"
  | "network_error"
  | "request_rejected";

export interface ClaimConflictDetails {
  claim_id?: string;
  fact_id?: string;
  current_version?: number;
  current_status?: string;
  current_support_graph_epoch?: number;
  block_reasons?: string[];
}

export interface ClaimReviewError {
  kind: ClaimReviewErrorKind;
  httpStatus?: number;
  code: string;
  message: string;
  requestId?: string;
  retryable?: boolean;
  conflictDetails?: ClaimConflictDetails;
  sourceResolution?: SourceResolutionMetadata;
}

export type ClaimReviewScenarioId =
  | "default_grounded"
  | "conflict_on_next_mutation"
  | "manual_only_llm_blocked"
  | "generation_synthetic_ready"
  | "policy_blocked"
  | "deletion_pending"
  | "import_not_ready";

export interface ClaimReviewScenarioMeta {
  id: ClaimReviewScenarioId;
  label: string;
  shortDescription: string;
}
