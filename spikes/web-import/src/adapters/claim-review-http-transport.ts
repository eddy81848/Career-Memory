import type {
  ApproveClaimInput,
  ApproveFactInput,
  ClaimReviewAdapter,
  ClaimReviewResponseMeta,
  ClaimReviewResult,
  CreateExportInput,
  DeleteExportInput,
  EditClaimInput,
  EditFactInput,
  ExportListResponse,
  GenerateClaimsInput,
  GetContentUnitContentInput,
  ListClaimsQuery,
  ListFactsQuery,
  RejectClaimInput,
  RejectFactInput,
  RevalidateClaimInput,
} from "@/adapters/claim-review-adapter";
import {
  generateUuidV4,
  parseOptionalJsonBody,
} from "@/adapters/synthetic-http-transport";
import type {
  ClaimAtomDetailItem,
  ClaimAtomStatus,
  ClaimAtomSupportLink,
  ClaimAtomSupportsItem,
  ClaimAtomType,
  ClaimDetailResponse,
  ClaimFactSupportNode,
  ClaimGenerationResponse,
  ClaimGenerationStatus,
  ClaimListResponse,
  ClaimReviewError,
  ClaimStatus,
  ClaimSummaryItem,
  ClaimSupportsResponse,
  ComputedVerification,
  DeleteExportResponse,
  EditClaimPatchResponse,
  EphemeralContentResponse,
  ExportDetailResponse,
  ExportFormat,
  ExportManifest,
  ExportPolicy,
  ExportStatus,
  ExportSummaryItem,
  FactListResponse,
  FactMutationResponse,
  FactNormalizedValue,
  FactStatus,
  FactSummaryItem,
  FactSupportItem,
  FactSupportsResponse,
  FactType,
  LinkStatus,
  SourceResolutionMetadata,
  SourceResolutionStatus,
  SpanLocatorV1,
  SupportType,
} from "@/domain/claim-review-models";

/**
 * Real `fetch`-based HTTP transport implementing the M4 Claim Review, Fact Review,
 * Plan C Ephemeral Source Viewer, and Export canonical backend wire contract (T-406).
 *
 * Security & correctness invariants:
 * - Always attaches `X-Request-ID`, optional `Authorization` & `X-Workspace-ID`,
 *   `Idempotency-Key` (UUID v4 on mutations), and `If-Match: "v{expected_version}"`.
 * - NEVER sends forbidden client-write fields (`verification`, `computed_verification`,
 *   `SOURCE_VERIFIED`, `status`, or raw-text keys) on any Fact or Claim write payload.
 * - NEVER auto-retries or auto-POSTs on 409 conflicts or network errors.
 * - NEVER persists or logs ephemeral source content from `/v1/content-units/{id}/content`.
 */

export interface ClaimReviewHttpTransportOptions {
  baseUrl: string;
  workspaceId?: string;
  authorization?: string;
  fetchImpl?: typeof fetch;
  keyGenerator?: () => string;
  defaultHeaders?: Record<string, string>;
}

const FORBIDDEN_WRITE_KEYS = new Set([
  "verification",
  "computed_verification",
  "source_verified",
  "verification_status",
  "claim_verification",
  "status",
  "extracted_span",
  "raw_text",
  "text_content",
]);

function assertSafeWritePayload(payload: Record<string, unknown>): void {
  for (const key of Object.keys(payload)) {
    if (FORBIDDEN_WRITE_KEYS.has(key.toLowerCase())) {
      throw new Error(`Forbidden write payload key blocked on client: ${key}`);
    }
  }
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

function buildVersionEtag(version: number): string {
  return `"v${version}"`;
}

function extractMeta(res: Response): ClaimReviewResponseMeta {
  return {
    httpStatus: res.status,
    etag: res.headers.get("etag"),
    cacheControl: res.headers.get("cache-control"),
    requestId: res.headers.get("x-request-id"),
  };
}

// =============================================================================
// Lossless Normalization Helpers
// =============================================================================

function normalizeLocator(raw: unknown): SpanLocatorV1 | null {
  const obj = asRecord(raw);
  if (!obj) return null;
  return {
    ...obj,
    schema: typeof obj.schema === "string" ? obj.schema : "cm-locator-v1",
    file_path: typeof obj.file_path === "string" ? obj.file_path : null,
    section: typeof obj.section === "string" ? obj.section : null,
    hunk_header: typeof obj.hunk_header === "string" ? obj.hunk_header : null,
    start_line: typeof obj.start_line === "number" ? obj.start_line : null,
    end_line: typeof obj.end_line === "number" ? obj.end_line : null,
    start_char: typeof obj.start_char === "number" ? obj.start_char : null,
    end_char: typeof obj.end_char === "number" ? obj.end_char : null,
    span_sha256: typeof obj.span_sha256 === "string" ? obj.span_sha256 : null,
    source_url: typeof obj.source_url === "string" ? obj.source_url : null,
  };
}

function normalizeSourceResolution(
  raw: unknown
): SourceResolutionMetadata | null {
  const obj = asRecord(raw);
  if (!obj || typeof obj.status !== "string") return null;
  return {
    status: obj.status as SourceResolutionStatus,
    checked_at: typeof obj.checked_at === "string" ? obj.checked_at : "",
    expected_revision_hash:
      typeof obj.expected_revision_hash === "string"
        ? obj.expected_revision_hash
        : null,
    observed_revision_hash:
      typeof obj.observed_revision_hash === "string"
        ? obj.observed_revision_hash
        : null,
    retryable: Boolean(obj.retryable),
    reason_code: typeof obj.reason_code === "string" ? obj.reason_code : null,
  };
}

export function normalizeFactSummary(
  raw: unknown,
  etagFallback?: string | null
): FactSummaryItem {
  const obj = asRecord(raw) ?? {};
  const version = typeof obj.version === "number" ? obj.version : 1;
  return {
    fact_id: typeof obj.fact_id === "string" ? obj.fact_id : "",
    workspace_id: typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    event_id: typeof obj.event_id === "string" ? obj.event_id : "",
    version,
    supersedes_fact_id:
      typeof obj.supersedes_fact_id === "string" ? obj.supersedes_fact_id : null,
    fact_type: (typeof obj.fact_type === "string"
      ? obj.fact_type
      : "ACTION") as FactType,
    subject_identity_id:
      typeof obj.subject_identity_id === "string"
        ? obj.subject_identity_id
        : null,
    predicate: typeof obj.predicate === "string" ? obj.predicate : "",
    object_text: typeof obj.object_text === "string" ? obj.object_text : "",
    normalized_value: (asRecord(obj.normalized_value) as FactNormalizedValue | null) ?? null,
    fact_metadata: asRecord(obj.fact_metadata) ?? {},
    status: (typeof obj.status === "string" ? obj.status : "CANDIDATE") as FactStatus,
    extractor_version:
      typeof obj.extractor_version === "string"
        ? obj.extractor_version
        : "fact-extractor-v1",
    direct_support_count:
      typeof obj.direct_support_count === "number" ? obj.direct_support_count : 0,
    context_support_count:
      typeof obj.context_support_count === "number" ? obj.context_support_count : 0,
    contradicts_support_count:
      typeof obj.contradicts_support_count === "number"
        ? obj.contradicts_support_count
        : 0,
    etag:
      typeof obj.etag === "string" && obj.etag.trim()
        ? obj.etag
        : etagFallback ?? buildVersionEtag(version),
    created_at: typeof obj.created_at === "string" ? obj.created_at : "",
    updated_at: typeof obj.updated_at === "string" ? obj.updated_at : "",
  };
}

export function normalizeFactSupportItem(raw: unknown): FactSupportItem {
  const obj = asRecord(raw) ?? {};
  const supportMeta = asRecord(obj.support_metadata) ?? {};
  const locator = normalizeLocator(obj.locator ?? supportMeta.locator);
  return {
    support_id:
      typeof obj.support_id === "string"
        ? obj.support_id
        : typeof obj.fact_support_id === "string"
          ? obj.fact_support_id
          : "",
    content_unit_id:
      typeof obj.content_unit_id === "string" ? obj.content_unit_id : "",
    source_object_revision_id:
      typeof obj.source_object_revision_id === "string"
        ? obj.source_object_revision_id
        : null,
    unit_type: typeof obj.unit_type === "string" ? obj.unit_type : "PR_BODY_SECTION",
    support_type: (typeof obj.support_type === "string"
      ? obj.support_type
      : "DIRECT") as SupportType,
    status: (typeof obj.status === "string" ? obj.status : "ACTIVE") as LinkStatus,
    validator: typeof obj.validator === "string" ? obj.validator : "RULE",
    validator_version:
      typeof obj.validator_version === "string"
        ? obj.validator_version
        : "fact-validator-v1",
    locator,
    revision_content_hash:
      typeof obj.revision_content_hash === "string"
        ? obj.revision_content_hash
        : typeof supportMeta.revision_content_hash === "string"
          ? supportMeta.revision_content_hash
          : null,
    revision_observed_at:
      typeof obj.revision_observed_at === "string"
        ? obj.revision_observed_at
        : typeof supportMeta.revision_observed_at === "string"
          ? supportMeta.revision_observed_at
          : null,
    revision_is_tombstone: Boolean(obj.revision_is_tombstone),
    last_checked_at:
      typeof obj.last_checked_at === "string" ? obj.last_checked_at : null,
    last_resolution_status:
      typeof obj.last_resolution_status === "string"
        ? (obj.last_resolution_status as SourceResolutionStatus)
        : null,
    support_metadata: supportMeta,
    created_at: typeof obj.created_at === "string" ? obj.created_at : "",
  };
}

function normalizeFactMutationResponse(
  raw: unknown,
  etagFallback?: string | null
): FactMutationResponse {
  const obj = asRecord(raw) ?? {};
  const rawFact = obj.fact ?? obj;
  const rawCounts = asRecord(obj.invalidated_counts);
  return {
    fact: normalizeFactSummary(rawFact, etagFallback),
    superseded_fact_id:
      typeof obj.superseded_fact_id === "string" ? obj.superseded_fact_id : null,
    superseded_version:
      typeof obj.superseded_version === "number" ? obj.superseded_version : null,
    invalidated_counts: rawCounts
      ? {
          contributions: Number(rawCounts.contributions ?? 0),
          contribution_supports: Number(rawCounts.contribution_supports ?? 0),
          claim_atoms: Number(rawCounts.claim_atoms ?? 0),
          claim_atom_supports: Number(rawCounts.claim_atom_supports ?? 0),
          claims: Number(rawCounts.claims ?? 0),
          exports: Number(rawCounts.exports ?? 0),
        }
      : undefined,
    idempotent_replay: Boolean(obj.idempotent_replay),
  };
}

export function normalizeClaimSummary(raw: unknown): ClaimSummaryItem {
  const obj = asRecord(raw) ?? {};
  return {
    claim_id: typeof obj.claim_id === "string" ? obj.claim_id : "",
    workspace_id: typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    event_id: typeof obj.event_id === "string" ? obj.event_id : "",
    version: typeof obj.version === "number" ? obj.version : 1,
    supersedes_claim_id:
      typeof obj.supersedes_claim_id === "string" ? obj.supersedes_claim_id : null,
    claim_text: typeof obj.claim_text === "string" ? obj.claim_text : "",
    status: (typeof obj.status === "string" ? obj.status : "DRAFT") as ClaimStatus,
    computed_verification: (typeof obj.computed_verification === "string"
      ? obj.computed_verification
      : "UNVERIFIED") as ComputedVerification,
    atom_count: typeof obj.atom_count === "number" ? obj.atom_count : 0,
    supported_atom_count:
      typeof obj.supported_atom_count === "number" ? obj.supported_atom_count : 0,
    user_confirmed_atom_count:
      typeof obj.user_confirmed_atom_count === "number"
        ? obj.user_confirmed_atom_count
        : 0,
    unsupported_atom_count:
      typeof obj.unsupported_atom_count === "number"
        ? obj.unsupported_atom_count
        : 0,
    stale_atom_count:
      typeof obj.stale_atom_count === "number" ? obj.stale_atom_count : 0,
    approvable: Boolean(obj.approvable),
    approval_block_reasons: asStringArray(obj.approval_block_reasons),
    support_graph_epoch:
      typeof obj.support_graph_epoch === "number" ? obj.support_graph_epoch : 1,
    approved_at: typeof obj.approved_at === "string" ? obj.approved_at : null,
    last_verified_at:
      typeof obj.last_verified_at === "string" ? obj.last_verified_at : null,
    generator_run_id:
      typeof obj.generator_run_id === "string" ? obj.generator_run_id : null,
    last_verification_run_id:
      typeof obj.last_verification_run_id === "string"
        ? obj.last_verification_run_id
        : null,
    created_at: typeof obj.created_at === "string" ? obj.created_at : "",
    updated_at: typeof obj.updated_at === "string" ? obj.updated_at : "",
  };
}

function normalizeClaimAtomDetail(raw: unknown, idx: number): ClaimAtomDetailItem {
  const obj = asRecord(raw) ?? {};
  const rg = asRecord(obj.role_grounding);
  const md = asRecord(obj.metric_details);
  return {
    atom_id: typeof obj.atom_id === "string" ? obj.atom_id : `atom_${idx + 1}`,
    ordinal: typeof obj.ordinal === "number" ? obj.ordinal : idx + 1,
    atom_type: (typeof obj.atom_type === "string"
      ? obj.atom_type
      : "ACTION") as ClaimAtomType,
    atom_text: typeof obj.atom_text === "string" ? obj.atom_text : "",
    status: (typeof obj.status === "string"
      ? obj.status
      : "UNSUPPORTED") as ClaimAtomStatus,
    block_reason: typeof obj.block_reason === "string" ? obj.block_reason : null,
    role_grounding: rg
      ? {
          role: typeof rg.role === "string" ? rg.role : null,
          identity_status:
            typeof rg.identity_status === "string" ? rg.identity_status : null,
          can_support_source_verified:
            typeof rg.can_support_source_verified === "boolean"
              ? rg.can_support_source_verified
              : undefined,
          note: typeof rg.note === "string" ? rg.note : null,
        }
      : null,
    metric_details: md
      ? {
          value: typeof md.value === "string" ? md.value : undefined,
          unit: typeof md.unit === "string" ? md.unit : undefined,
          period: typeof md.period === "string" ? md.period : undefined,
          source_basis:
            typeof md.source_basis === "string" ? md.source_basis : undefined,
          environment_scope:
            typeof md.environment_scope === "string"
              ? md.environment_scope
              : undefined,
          measurement_scope:
            typeof md.measurement_scope === "string"
              ? md.measurement_scope
              : undefined,
        }
      : null,
  };
}

export function normalizeClaimDetail(raw: unknown): ClaimDetailResponse {
  const obj = asRecord(raw) ?? {};
  const summary = normalizeClaimSummary(obj);
  const rawAtoms = Array.isArray(obj.atoms) ? obj.atoms : [];
  return {
    ...summary,
    atoms: rawAtoms.map((a, idx) => normalizeClaimAtomDetail(a, idx)),
    needs_revalidation:
      typeof obj.needs_revalidation === "boolean"
        ? obj.needs_revalidation
        : summary.status === "DRAFT" || summary.status === "STALE",
  };
}

function normalizeClaimFactSupportNode(raw: unknown): ClaimFactSupportNode {
  const obj = asRecord(raw) ?? {};
  return {
    fact_support_id:
      typeof obj.fact_support_id === "string"
        ? obj.fact_support_id
        : typeof obj.support_id === "string"
          ? obj.support_id
          : "",
    support_type: (typeof obj.support_type === "string"
      ? obj.support_type
      : "DIRECT") as SupportType,
    status: (typeof obj.status === "string" ? obj.status : "ACTIVE") as LinkStatus,
    validator: typeof obj.validator === "string" ? obj.validator : "RULE",
    validator_version:
      typeof obj.validator_version === "string"
        ? obj.validator_version
        : "fact-validator-v1",
    content_unit_id:
      typeof obj.content_unit_id === "string" ? obj.content_unit_id : "",
    unit_type: typeof obj.unit_type === "string" ? obj.unit_type : "PR_BODY_SECTION",
    locator: normalizeLocator(obj.locator),
    source_object_revision_id:
      typeof obj.source_object_revision_id === "string"
        ? obj.source_object_revision_id
        : null,
    revision_content_hash:
      typeof obj.revision_content_hash === "string"
        ? obj.revision_content_hash
        : null,
    revision_observed_at:
      typeof obj.revision_observed_at === "string"
        ? obj.revision_observed_at
        : null,
    revision_is_tombstone: Boolean(obj.revision_is_tombstone),
    last_checked_at:
      typeof obj.last_checked_at === "string" ? obj.last_checked_at : null,
    last_resolution_status:
      typeof obj.last_resolution_status === "string"
        ? (obj.last_resolution_status as SourceResolutionStatus)
        : null,
  };
}

function normalizeClaimAtomSupportLink(raw: unknown): ClaimAtomSupportLink {
  const obj = asRecord(raw) ?? {};
  const rawFact = asRecord(obj.fact) ?? {};
  const rawFactSupports = Array.isArray(obj.fact_supports) ? obj.fact_supports : [];
  return {
    claim_atom_support_id:
      typeof obj.claim_atom_support_id === "string"
        ? obj.claim_atom_support_id
        : "",
    support_type: (typeof obj.support_type === "string"
      ? obj.support_type
      : "DIRECT") as SupportType,
    status: (typeof obj.status === "string" ? obj.status : "ACTIVE") as LinkStatus,
    validation_run_id:
      typeof obj.validation_run_id === "string" ? obj.validation_run_id : null,
    fact: {
      fact_id: typeof rawFact.fact_id === "string" ? rawFact.fact_id : "",
      version: typeof rawFact.version === "number" ? rawFact.version : 1,
      fact_type: (typeof rawFact.fact_type === "string"
        ? rawFact.fact_type
        : "ACTION") as FactType,
      predicate: typeof rawFact.predicate === "string" ? rawFact.predicate : "",
      object_text:
        typeof rawFact.object_text === "string" ? rawFact.object_text : "",
      status: (typeof rawFact.status === "string"
        ? rawFact.status
        : "SUPPORTED") as FactStatus,
      normalized_value:
        (asRecord(rawFact.normalized_value) as FactNormalizedValue | null) ?? null,
      subject_identity_id:
        typeof rawFact.subject_identity_id === "string"
          ? rawFact.subject_identity_id
          : null,
    },
    fact_supports: rawFactSupports.map(normalizeClaimFactSupportNode),
  };
}

export function normalizeClaimSupports(raw: unknown): ClaimSupportsResponse {
  const obj = asRecord(raw) ?? {};
  const rawAtoms = Array.isArray(obj.atoms) ? obj.atoms : [];
  const atoms: ClaimAtomSupportsItem[] = rawAtoms.map((entry, idx) => {
    const a = asRecord(entry) ?? {};
    const rawSupports = Array.isArray(a.supports) ? a.supports : [];
    return {
      atom_id: typeof a.atom_id === "string" ? a.atom_id : `atom_${idx + 1}`,
      ordinal: typeof a.ordinal === "number" ? a.ordinal : idx + 1,
      atom_type: (typeof a.atom_type === "string"
        ? a.atom_type
        : "ACTION") as ClaimAtomType,
      atom_text: typeof a.atom_text === "string" ? a.atom_text : "",
      status: (typeof a.status === "string"
        ? a.status
        : "UNSUPPORTED") as ClaimAtomStatus,
      supports: rawSupports.map(normalizeClaimAtomSupportLink),
    };
  });
  return {
    claim_id: typeof obj.claim_id === "string" ? obj.claim_id : "",
    version: typeof obj.version === "number" ? obj.version : 1,
    event_id: typeof obj.event_id === "string" ? obj.event_id : "",
    status: (typeof obj.status === "string" ? obj.status : "DRAFT") as ClaimStatus,
    computed_verification: (typeof obj.computed_verification === "string"
      ? obj.computed_verification
      : "UNVERIFIED") as ComputedVerification,
    support_graph_epoch:
      typeof obj.support_graph_epoch === "number" ? obj.support_graph_epoch : 1,
    atoms,
  };
}

export function normalizeExportSummary(raw: unknown): ExportSummaryItem {
  const obj = asRecord(raw) ?? {};
  return {
    export_id: typeof obj.export_id === "string" ? obj.export_id : "",
    workspace_id: typeof obj.workspace_id === "string" ? obj.workspace_id : "",
    format: (obj.format === "JSON" ? "JSON" : "MARKDOWN") as ExportFormat,
    export_policy: (obj.export_policy === "ALLOW_USER_CONFIRMED"
      ? "ALLOW_USER_CONFIRMED"
      : "SOURCE_VERIFIED_ONLY") as ExportPolicy,
    status: (typeof obj.status === "string"
      ? obj.status
      : "ACTIVE") as ExportStatus,
    claim_ids: asStringArray(obj.claim_ids),
    invalidation_reason:
      typeof obj.invalidation_reason === "string"
        ? obj.invalidation_reason
        : null,
    created_at: typeof obj.created_at === "string" ? obj.created_at : "",
    updated_at: typeof obj.updated_at === "string" ? obj.updated_at : undefined,
  };
}

export function normalizeExportDetail(raw: unknown): ExportDetailResponse {
  const obj = asRecord(raw) ?? {};
  const summary = normalizeExportSummary(obj);
  const rawManifest = asRecord(obj.manifest) ?? {};
  const rawCitations = Array.isArray(rawManifest.citations)
    ? rawManifest.citations
    : [];
  const manifest: ExportManifest = {
    export_policy:
      rawManifest.export_policy === "ALLOW_USER_CONFIRMED"
        ? "ALLOW_USER_CONFIRMED"
        : summary.export_policy,
    claim_count:
      typeof rawManifest.claim_count === "number"
        ? rawManifest.claim_count
        : summary.claim_ids.length,
    includes_user_confirmed: Boolean(rawManifest.includes_user_confirmed),
    contains_raw_quotes: false,
    citations: rawCitations.map((c) => {
      const ci = asRecord(c) ?? {};
      return {
        claim_id: typeof ci.claim_id === "string" ? ci.claim_id : "",
        claim_version: typeof ci.claim_version === "number" ? ci.claim_version : 1,
        computed_verification: (typeof ci.computed_verification === "string"
          ? ci.computed_verification
          : "SOURCE_VERIFIED") as ComputedVerification,
        source_url: typeof ci.source_url === "string" ? ci.source_url : null,
        locator: normalizeLocator(ci.locator),
        revision_content_hash:
          typeof ci.revision_content_hash === "string"
            ? ci.revision_content_hash
            : null,
        observed_at: typeof ci.observed_at === "string" ? ci.observed_at : null,
        checked_at: typeof ci.checked_at === "string" ? ci.checked_at : null,
      };
    }),
  };

  return {
    ...summary,
    rendered_markdown:
      typeof obj.rendered_markdown === "string" ? obj.rendered_markdown : null,
    rendered_json: asRecord(obj.rendered_json),
    manifest,
  };
}

// =============================================================================
// Error Classifier
// =============================================================================

interface RawErrorBody {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    request_id?: string;
    details?: {
      claim_id?: string;
      fact_id?: string;
      current_version?: number;
      current_status?: string;
      current_support_graph_epoch?: number;
      block_reasons?: string[];
    };
  };
  source_resolution?: unknown;
}

function classifyClaimReviewError(
  status: number,
  rawBody: string,
  meta: ClaimReviewResponseMeta
): ClaimReviewError {
  const parsed = parseOptionalJsonBody<RawErrorBody>(rawBody);
  const body = !parsed.empty && !parsed.invalidJson ? parsed.value : null;
  const envelope = body?.error;
  const sourceResolution =
    normalizeSourceResolution(body?.source_resolution) ?? undefined;
  const code = envelope?.code ?? `HTTP_${status}`;
  const requestId = envelope?.request_id ?? meta.requestId ?? undefined;
  const details = envelope?.details;

  if (
    status === 409 &&
    (code === "CLAIM_VERSION_CONFLICT" ||
      code === "FACT_VERSION_CONFLICT" ||
      code === "EXPORT_CLAIM_VERSION_CONFLICT")
  ) {
    return {
      kind: "version_conflict",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "다른 작업에서 먼저 수정되어 버전이 충돌했습니다. 자동 재전송을 수행하지 않았으니 최신 상태를 다시 불러오세요.",
      requestId,
      retryable: false,
      conflictDetails: details,
      sourceResolution,
    };
  }

  if (status === 409 && code === "SUPPORT_INCOMPLETE") {
    return {
      kind: "support_incomplete",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "일부 Claim Atom의 근거가 불완전하거나 STALE 상태여서 승인/Export가 차단되었습니다.",
      requestId,
      retryable: false,
      conflictDetails: details,
      sourceResolution,
    };
  }

  if (
    (status === 409 &&
      (code === "SOURCE_REVISION_MISMATCH" || code === "SOURCE_LOCATOR_INVALID")) ||
    sourceResolution?.status === "REVISION_MISMATCH" ||
    sourceResolution?.status === "INVALID_LOCATOR"
  ) {
    return {
      kind: "source_revision_mismatch",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "현재 원문이 과거 관측된 리비전 해시 또는 스팬 위치(cm-locator-v1)와 일치하지 않습니다.",
      requestId,
      retryable: false,
      conflictDetails: details,
      sourceResolution,
    };
  }

  if (code === "DELETION_PENDING" || code === "DELETION_FENCE_ACTIVE") {
    return {
      kind: "deletion_pending",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "삭제 펜스(DELETION_PENDING)가 활성화되어 변경·승인·Export 및 원문 열람이 차단되었습니다.",
      requestId,
      retryable: false,
      sourceResolution,
    };
  }

  if (
    code === "POLICY_BLOCKED" ||
    code === "SOURCE_POLICY_BLOCKED" ||
    sourceResolution?.status === "POLICY_BLOCKED"
  ) {
    return {
      kind: "policy_blocked",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "공개 정책 변경 또는 권한 회수(POLICY_BLOCKED)로 작업이 차단되었습니다.",
      requestId,
      retryable: false,
      sourceResolution,
    };
  }

  if (
    code === "SOURCE_FETCH_LIMIT" ||
    sourceResolution?.status === "PARTIAL_WITH_LIMIT"
  ) {
    return {
      kind: "source_limit",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "원문 조회 한도(PARTIAL_WITH_LIMIT)에 도달하여 무음 절단 없이 중단했습니다.",
      requestId,
      retryable: false,
      sourceResolution,
    };
  }

  if (
    status === 503 ||
    code === "SOURCE_UNAVAILABLE" ||
    sourceResolution?.status === "UNAVAILABLE"
  ) {
    return {
      kind: "source_unavailable",
      httpStatus: status,
      code,
      message:
        envelope?.message ??
        "공급자에서 원문을 현재 조회할 수 없습니다(UNAVAILABLE).",
      requestId,
      retryable: Boolean(sourceResolution?.retryable ?? envelope?.retryable),
      sourceResolution,
    };
  }

  if (status === 401) {
    return {
      kind: "unauthorized",
      httpStatus: status,
      code,
      message:
        envelope?.message ?? "인증 세션이 유효하지 않아 요청을 처리할 수 없습니다.",
      requestId,
      retryable: false,
      sourceResolution,
    };
  }

  if (status === 404) {
    return {
      kind: "not_found",
      httpStatus: status,
      code,
      message:
        envelope?.message ?? "요청한 리소스를 찾을 수 없거나 접근 권한이 없습니다.",
      requestId,
      retryable: false,
      sourceResolution,
    };
  }

  if (status === 400 || status === 422) {
    return {
      kind: "validation_error",
      httpStatus: status,
      code,
      message: envelope?.message ?? "요청 입력값이 유효하지 않습니다.",
      requestId,
      retryable: false,
      sourceResolution,
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
    conflictDetails: details,
    sourceResolution,
  };
}

// =============================================================================
// FetchClaimReviewHttpTransport
// =============================================================================

export class FetchClaimReviewHttpTransport implements ClaimReviewAdapter {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly keyGenerator: () => string;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: ClaimReviewHttpTransportOptions) {
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
    method: "GET" | "POST" | "PATCH" | "DELETE";
    path: string;
    headers?: Record<string, string>;
    body?: Record<string, unknown>;
    normalize?: (raw: unknown, meta: ClaimReviewResponseMeta) => T;
  }): Promise<ClaimReviewResult<T>> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Request-ID": this.keyGenerator(),
      ...this.defaultHeaders,
      ...(params.headers ?? {}),
    };

    let encodedBody: string | undefined;
    if (params.body !== undefined) {
      assertSafeWritePayload(params.body);
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
        error: classifyClaimReviewError(response.status, rawBody, meta),
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

    const data = params.normalize
      ? params.normalize(parsed.value, meta)
      : (parsed.value as T);

    return { ok: true, data, meta };
  }

  // --- Facts (/v1/facts*) ---

  public async listFacts(
    query?: ListFactsQuery
  ): Promise<ClaimReviewResult<FactListResponse>> {
    const search = new URLSearchParams();
    if (query?.event_id) search.set("event_id", query.event_id);
    search.set(
      "include_superseded",
      String(query?.include_superseded ?? false)
    );
    if (query?.limit !== undefined) search.set("limit", String(query.limit));
    if (query?.cursor) search.set("cursor", query.cursor);

    return this.requestJson<FactListResponse>({
      method: "GET",
      path: `/v1/facts?${search.toString()}`,
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const rawItems = Array.isArray(obj.items) ? obj.items : [];
        const rawPage = asRecord(obj.page) ?? {};
        return {
          items: rawItems.map((item) => normalizeFactSummary(item)),
          page: {
            next_cursor:
              typeof rawPage.next_cursor === "string"
                ? rawPage.next_cursor
                : null,
            has_more: Boolean(rawPage.has_more),
          },
        };
      },
    });
  }

  public async getFactSupports(
    factId: string
  ): Promise<ClaimReviewResult<FactSupportsResponse>> {
    return this.requestJson<FactSupportsResponse>({
      method: "GET",
      path: `/v1/facts/${encodeURIComponent(factId)}/supports`,
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const rawItems = Array.isArray(obj.items)
          ? obj.items
          : Array.isArray(obj.supports)
            ? obj.supports
            : [];
        return {
          fact: normalizeFactSummary(obj.fact),
          items: rawItems.map(normalizeFactSupportItem),
        };
      },
    });
  }

  public async approveFact(
    input: ApproveFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<FactMutationResponse>({
      method: "POST",
      path: `/v1/facts/${encodeURIComponent(input.fact_id)}/actions/approve`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        ...(input.reason ? { reason: input.reason } : {}),
      },
      normalize: (raw, meta) => normalizeFactMutationResponse(raw, meta.etag),
    });
  }

  public async rejectFact(
    input: RejectFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<FactMutationResponse>({
      method: "POST",
      path: `/v1/facts/${encodeURIComponent(input.fact_id)}/actions/reject`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason,
      },
      normalize: (raw, meta) => normalizeFactMutationResponse(raw, meta.etag),
    });
  }

  public async editFact(
    input: EditFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<FactMutationResponse>({
      method: "PATCH",
      path: `/v1/facts/${encodeURIComponent(input.fact_id)}`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        predicate: input.predicate,
        object_text: input.object_text,
        ...(input.normalized_value !== undefined
          ? { normalized_value: input.normalized_value }
          : {}),
        ...(input.reason ? { reason: input.reason } : {}),
      },
      normalize: (raw, meta) => normalizeFactMutationResponse(raw, meta.etag),
    });
  }

  // --- Claims (/v1/claims*) ---

  public async listClaims(
    query?: ListClaimsQuery
  ): Promise<ClaimReviewResult<ClaimListResponse>> {
    const search = new URLSearchParams();
    if (query?.event_id) search.set("event_id", query.event_id);
    search.set(
      "include_superseded",
      String(query?.include_superseded ?? false)
    );
    if (query?.limit !== undefined) search.set("limit", String(query.limit));
    if (query?.cursor) search.set("cursor", query.cursor);

    return this.requestJson<ClaimListResponse>({
      method: "GET",
      path: `/v1/claims?${search.toString()}`,
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const rawItems = Array.isArray(obj.items) ? obj.items : [];
        const rawPage = asRecord(obj.page) ?? {};
        return {
          items: rawItems.map(normalizeClaimSummary),
          page: {
            next_cursor:
              typeof rawPage.next_cursor === "string"
                ? rawPage.next_cursor
                : null,
            has_more: Boolean(rawPage.has_more),
          },
        };
      },
    });
  }

  public async generateClaims(
    input: GenerateClaimsInput
  ): Promise<ClaimReviewResult<ClaimGenerationResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<ClaimGenerationResponse>({
      method: "POST",
      path: "/v1/claims/generations",
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        confirmed_event_id: input.confirmed_event_id,
        ...(input.input_fact_ids && input.input_fact_ids.length > 0
          ? { input_fact_ids: input.input_fact_ids }
          : {}),
      },
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const rawClaims = Array.isArray(obj.claims) ? obj.claims : [];
        return {
          event_id:
            typeof obj.event_id === "string"
              ? obj.event_id
              : input.confirmed_event_id,
          generation_status: (typeof obj.generation_status === "string"
            ? obj.generation_status
            : "MANUAL_ONLY_BLOCKED") as ClaimGenerationStatus,
          reason_code:
            typeof obj.reason_code === "string" ? obj.reason_code : null,
          reason_message:
            typeof obj.reason_message === "string" ? obj.reason_message : null,
          free_alternatives: asStringArray(obj.free_alternatives),
          claims: rawClaims.map(normalizeClaimDetail),
        };
      },
    });
  }

  public async getClaimDetail(
    claimId: string
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    return this.requestJson<ClaimDetailResponse>({
      method: "GET",
      path: `/v1/claims/${encodeURIComponent(claimId)}`,
      normalize: (raw) => normalizeClaimDetail(raw),
    });
  }

  public async getClaimSupports(
    claimId: string
  ): Promise<ClaimReviewResult<ClaimSupportsResponse>> {
    return this.requestJson<ClaimSupportsResponse>({
      method: "GET",
      path: `/v1/claims/${encodeURIComponent(claimId)}/supports`,
      normalize: (raw) => normalizeClaimSupports(raw),
    });
  }

  public async editClaim(
    input: EditClaimInput
  ): Promise<ClaimReviewResult<EditClaimPatchResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<EditClaimPatchResponse>({
      method: "PATCH",
      path: `/v1/claims/${encodeURIComponent(input.claim_id)}`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        claim_text: input.claim_text,
        ...(input.edit_reason ? { edit_reason: input.edit_reason } : {}),
      },
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        return {
          claim_id: typeof obj.claim_id === "string" ? obj.claim_id : "",
          workspace_id:
            typeof obj.workspace_id === "string" ? obj.workspace_id : "",
          event_id: typeof obj.event_id === "string" ? obj.event_id : "",
          version: typeof obj.version === "number" ? obj.version : 1,
          supersedes_claim_id:
            typeof obj.supersedes_claim_id === "string"
              ? obj.supersedes_claim_id
              : input.claim_id,
          claim_text:
            typeof obj.claim_text === "string"
              ? obj.claim_text
              : input.claim_text,
          status: (typeof obj.status === "string"
            ? obj.status
            : "DRAFT") as ClaimStatus,
          computed_verification: (typeof obj.computed_verification === "string"
            ? obj.computed_verification
            : "UNVERIFIED") as ComputedVerification,
        };
      },
    });
  }

  public async revalidateClaim(
    input: RevalidateClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<ClaimDetailResponse>({
      method: "POST",
      path: `/v1/claims/${encodeURIComponent(input.claim_id)}/actions/revalidate`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
      },
      normalize: (raw) => normalizeClaimDetail(raw),
    });
  }

  public async approveClaim(
    input: ApproveClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<ClaimDetailResponse>({
      method: "POST",
      path: `/v1/claims/${encodeURIComponent(input.claim_id)}/actions/approve`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        ...(input.expected_support_graph_epoch !== undefined
          ? {
              expected_support_graph_epoch: input.expected_support_graph_epoch,
            }
          : {}),
        acknowledge_user_confirmed_atoms: input.acknowledge_user_confirmed_atoms,
      },
      normalize: (raw) => normalizeClaimDetail(raw),
    });
  }

  public async rejectClaim(
    input: RejectClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<ClaimDetailResponse>({
      method: "POST",
      path: `/v1/claims/${encodeURIComponent(input.claim_id)}/actions/reject`,
      headers: {
        "If-Match": buildVersionEtag(input.expected_version),
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        expected_version: input.expected_version,
        reason: input.reason,
      },
      normalize: (raw) => normalizeClaimDetail(raw),
    });
  }

  // --- Plan C Ephemeral Content Unit Viewer (/v1/content-units/{id}/content) ---

  public async getContentUnitContent(
    input: GetContentUnitContentInput
  ): Promise<ClaimReviewResult<EphemeralContentResponse>> {
    const search = new URLSearchParams();
    search.set("workspace_id", input.workspace_id);
    search.set("include_context", String(input.include_context ?? true));

    return this.requestJson<EphemeralContentResponse>({
      method: "GET",
      path: `/v1/content-units/${encodeURIComponent(input.content_unit_id)}/content?${search.toString()}`,
      headers: {
        "X-Workspace-ID": input.workspace_id,
      },
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const ctx = asRecord(obj.context);
        const sr = normalizeSourceResolution(obj.source_resolution) ?? {
          status: "MATCHED" as SourceResolutionStatus,
          checked_at: "",
          expected_revision_hash: null,
          observed_revision_hash: null,
          retryable: false,
          reason_code: null,
        };
        return {
          content_unit_id:
            typeof obj.content_unit_id === "string"
              ? obj.content_unit_id
              : input.content_unit_id,
          unit_type:
            typeof obj.unit_type === "string"
              ? obj.unit_type
              : "PR_BODY_SECTION",
          content: typeof obj.content === "string" ? obj.content : "",
          context: ctx
            ? {
                section: typeof ctx.section === "string" ? ctx.section : null,
                file_path:
                  typeof ctx.file_path === "string" ? ctx.file_path : null,
                previous_file_path:
                  typeof ctx.previous_file_path === "string"
                    ? ctx.previous_file_path
                    : null,
                hunk_header:
                  typeof ctx.hunk_header === "string" ? ctx.hunk_header : null,
                preceding_text:
                  typeof ctx.preceding_text === "string"
                    ? ctx.preceding_text
                    : null,
                following_text:
                  typeof ctx.following_text === "string"
                    ? ctx.following_text
                    : null,
              }
            : null,
          source_resolution: sr,
        };
      },
    });
  }

  // --- Exports (/v1/exports*) ---

  public async listExports(): Promise<ClaimReviewResult<ExportListResponse>> {
    return this.requestJson<ExportListResponse>({
      method: "GET",
      path: "/v1/exports",
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        const rawItems = Array.isArray(obj.items) ? obj.items : [];
        return {
          items: rawItems.map(normalizeExportSummary),
        };
      },
    });
  }

  public async createExport(
    input: CreateExportInput
  ): Promise<ClaimReviewResult<ExportDetailResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<ExportDetailResponse>({
      method: "POST",
      path: "/v1/exports",
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      body: {
        format: input.format,
        claim_ids: input.claim_ids,
        ...(input.expected_claim_versions
          ? { expected_claim_versions: input.expected_claim_versions }
          : {}),
        export_policy: input.export_policy,
        ...(input.source_resource_id
          ? { source_resource_id: input.source_resource_id }
          : {}),
      },
      normalize: (raw) => normalizeExportDetail(raw),
    });
  }

  public async getExportDetail(
    exportId: string
  ): Promise<ClaimReviewResult<ExportDetailResponse>> {
    return this.requestJson<ExportDetailResponse>({
      method: "GET",
      path: `/v1/exports/${encodeURIComponent(exportId)}`,
      normalize: (raw) => normalizeExportDetail(raw),
    });
  }

  public async deleteExport(
    input: DeleteExportInput
  ): Promise<ClaimReviewResult<DeleteExportResponse>> {
    const idempotencyKey = input.idempotency_key ?? this.keyGenerator();
    return this.requestJson<DeleteExportResponse>({
      method: "DELETE",
      path: `/v1/exports/${encodeURIComponent(input.export_id)}`,
      headers: {
        "Idempotency-Key": idempotencyKey,
      },
      normalize: (raw) => {
        const obj = asRecord(raw) ?? {};
        return {
          export_id:
            typeof obj.export_id === "string" ? obj.export_id : input.export_id,
          status: "DELETED",
          deleted_at:
            typeof obj.deleted_at === "string"
              ? obj.deleted_at
              : new Date().toISOString(),
        };
      },
    });
  }
}
