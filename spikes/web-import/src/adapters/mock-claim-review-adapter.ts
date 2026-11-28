import type {
  ApproveClaimInput,
  ApproveFactInput,
  ClaimReviewAdapter,
  ClaimReviewHarnessControls,
  ClaimReviewResult,
  CreateExportInput,
  DeleteExportInput,
  EditClaimInput,
  EditFactInput,
  EphemeralResolutionOverrideMode,
  ExportListResponse,
  GenerateClaimsInput,
  GetContentUnitContentInput,
  ListClaimsQuery,
  ListFactsQuery,
  RejectClaimInput,
  RejectFactInput,
  RevalidateClaimInput,
} from "@/adapters/claim-review-adapter";
import type {
  ClaimDetailResponse,
  ClaimGenerationResponse,
  ClaimListResponse,
  ClaimReviewScenarioId,
  ClaimReviewWorkspaceGuard,
  ClaimSummaryItem,
  ClaimSupportsResponse,
  DeleteExportResponse,
  EditClaimPatchResponse,
  EphemeralContentResponse,
  ExportDetailResponse,
  ExportSummaryItem,
  FactListResponse,
  FactMutationResponse,
  FactSummaryItem,
  FactSupportItem,
  FactSupportsResponse,
} from "@/domain/claim-review-models";
import { FREE_ALTERNATIVES_DEFAULT } from "@/domain/claim-review-presenter";

/**
 * Local prototype fixture adapter implementing `ClaimReviewAdapter` for
 * zero-backend interactive `/claim-review` exploration, unit tests, and Playwright E2E.
 *
 * Strictly enforces Plan C (ephemeral text returned ONLY via `getContentUnitContent`,
 * zero `extracted_span` on Fact/Claim supports, zero browser storage writes).
 */

export interface MockClaimReviewAdapterOptions {
  latencyMs?: number;
  initialScenario?: ClaimReviewScenarioId;
  eventId?: string;
}

const WORKSPACE_ID = "ws_demo_seoyeon_01";
const REPO_LABEL = "seoyeon-lab-demo/ledger-engine";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function nowIso(): string {
  return "2026-11-28T04:15:00Z";
}

function toClaimSummary(detail: ClaimDetailResponse): ClaimSummaryItem {
  return {
    claim_id: detail.claim_id,
    workspace_id: detail.workspace_id,
    event_id: detail.event_id,
    version: detail.version,
    supersedes_claim_id: detail.supersedes_claim_id,
    claim_text: detail.claim_text,
    status: detail.status,
    computed_verification: detail.computed_verification,
    atom_count: detail.atom_count,
    supported_atom_count: detail.supported_atom_count,
    user_confirmed_atom_count: detail.user_confirmed_atom_count,
    unsupported_atom_count: detail.unsupported_atom_count,
    stale_atom_count: detail.stale_atom_count,
    approvable: detail.approvable,
    approval_block_reasons: [...detail.approval_block_reasons],
    support_graph_epoch: detail.support_graph_epoch,
    approved_at: detail.approved_at,
    last_verified_at: detail.last_verified_at,
    generator_run_id: detail.generator_run_id,
    last_verification_run_id: detail.last_verification_run_id,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

function toExportSummary(detail: ExportDetailResponse): ExportSummaryItem {
  return {
    export_id: detail.export_id,
    workspace_id: detail.workspace_id,
    format: detail.format,
    export_policy: detail.export_policy,
    status: detail.status,
    claim_ids: [...detail.claim_ids],
    invalidation_reason: detail.invalidation_reason,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

interface EphemeralUnitFixture {
  unit_type: string;
  content: string;
  context: EphemeralContentResponse["context"];
  expected_revision_hash: string;
  observed_revision_hash: string;
  defaultMode: EphemeralResolutionOverrideMode;
}

export class MockClaimReviewAdapter
  implements ClaimReviewAdapter, ClaimReviewHarnessControls
{
  private readonly latencyMs: number;
  private scenarioId: ClaimReviewScenarioId;
  private activeEventId: string;
  private ephemeralOverride: EphemeralResolutionOverrideMode = "DEFAULT";
  private conflictArmedForClaimIds = new Set<string>();
  private conflictArmedNextMutation = false;
  private seq = 100;

  private facts = new Map<string, FactSummaryItem>();
  private factSupports = new Map<string, FactSupportItem[]>();
  private claims = new Map<string, ClaimDetailResponse>();
  private claimSupports = new Map<string, ClaimSupportsResponse>();
  private exportsMap = new Map<string, ExportDetailResponse>();
  private ephemeralUnits = new Map<string, EphemeralUnitFixture>();

  // Test observability counters
  public requestedPaths: string[] = [];
  public mutationPayloads: Array<{ method: string; path: string; body: unknown }> =
    [];

  constructor(options: MockClaimReviewAdapterOptions = {}) {
    this.latencyMs = options.latencyMs ?? 60;
    this.scenarioId = options.initialScenario ?? "default_grounded";
    this.activeEventId = options.eventId ?? "evt_comp_301";
    this.seedState();
  }

  private async delay(): Promise<void> {
    if (this.latencyMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.latencyMs));
    }
  }

  private isPolicyOrDeletionBlocked(): boolean {
    return (
      this.scenarioId === "policy_blocked" ||
      this.scenarioId === "deletion_pending"
    );
  }

  private makeBlockedError(): ClaimReviewResult<never> {
    if (this.scenarioId === "deletion_pending") {
      return {
        ok: false,
        error: {
          kind: "deletion_pending",
          httpStatus: 409,
          code: "DELETION_PENDING",
          message:
            "삭제 펜스(DELETION_PENDING · OD-003)가 활성화되어 모든 Fact/Claim 변경·승인·Export 및 원문 열람이 차단되었습니다.",
          retryable: false,
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }
    return {
      ok: false,
      error: {
        kind: "policy_blocked",
        httpStatus: 403,
        code: "SOURCE_POLICY_BLOCKED",
        message:
          "공개 정책 차단(POLICY_BLOCKED · 원본 삭제 아님)으로 인해 변경·승인·Export 및 원문 열람이 차단되었습니다.",
        retryable: false,
      },
      meta: { httpStatus: 403, cacheControl: "private, no-store" },
    };
  }

  private seedState(): void {
    this.facts.clear();
    this.factSupports.clear();
    this.claims.clear();
    this.claimSupports.clear();
    this.exportsMap.clear();
    this.ephemeralUnits.clear();
    this.conflictArmedForClaimIds.clear();
    this.conflictArmedNextMutation =
      this.scenarioId === "conflict_on_next_mutation";
    this.seq = 100;

    const eventId = this.activeEventId;

    // --- Ephemeral Content Units (Plan C — memory-only upon GET /content) ---
    this.ephemeralUnits.set("cu_hmac_middleware_01", {
      unit_type: "DIFF_HUNK",
      content:
        "export function verifyWebhookSignature(payload: Buffer, sigHeader: string, secret: string): boolean {\n  const expected = createHmac('sha256', secret).update(payload).digest('hex');\n  return timingSafeEqual(Buffer.from(expected), Buffer.from(sigHeader));\n}",
      context: {
        section: "HMAC-SHA256 Webhook Middleware",
        file_path: "src/webhook/signature.ts",
        previous_file_path: null,
        hunk_header: "@@ -18,4 +18,12 @@",
        preceding_text: "import { createHmac, timingSafeEqual } from 'node:crypto';",
        following_text: "export const MAX_REPLAY_WINDOW_SEC = 300;",
      },
      expected_revision_hash: "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
      observed_revision_hash: "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
      defaultMode: "MATCHED",
    });

    this.ephemeralUnits.set("cu_replay_metric_02", {
      unit_type: "PR_BODY_SECTION",
      content:
        "Production telemetry (30-day window, payment-webhook-gateway): p99 signature verification latency dropped by 42% (from 19.4ms to 11.2ms) with 0 replay bypass incidents across 1.2M production webhook deliveries.",
      context: {
        section: "Benchmark & Verification Results",
        file_path: "PR#88/body.md",
        previous_file_path: null,
        hunk_header: null,
        preceding_text: "## Verification",
        following_text: "Reviewed and approved by @security-lead.",
      },
      expected_revision_hash: "sha256:7d19b4a0c6e5f20491e4a092bb41c822d3f444cc",
      observed_revision_hash: "sha256:7d19b4a0c6e5f20491e4a092bb41c822d3f444cc",
      defaultMode: "MATCHED",
    });

    this.ephemeralUnits.set("cu_drifted_unit_03", {
      unit_type: "COMMIT_MESSAGE",
      content: "chore(webhook): configure 5-minute replay tolerance window",
      context: {
        section: "Commit Message",
        file_path: "src/webhook/replay-window.ts",
        previous_file_path: null,
        hunk_header: null,
        preceding_text: null,
        following_text: null,
      },
      expected_revision_hash: "sha256:33aa91e200bb12cc889911ff22ee44dd66001122",
      observed_revision_hash: "sha256:99ff00112233445566778899aabbccddeeff0011",
      defaultMode: "REVISION_MISMATCH",
    });

    this.ephemeralUnits.set("cu_unavail_404_04", {
      unit_type: "REVIEW_COMMENT",
      content: "",
      context: null,
      expected_revision_hash: "sha256:11223344556677889900aabbccddeeff11223344",
      observed_revision_hash: "",
      defaultMode: "UNAVAILABLE_PROVIDER_404",
    });

    // --- Canonical Facts (/v1/facts*) ---
    const factAction: FactSummaryItem = {
      fact_id: "fact_hmac_impl_01",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_fact_id: null,
      fact_type: "ACTION",
      subject_identity_id: "id_seoyeon_verified_01",
      predicate: "implemented",
      object_text:
        "constant-time HMAC-SHA256 webhook signature verification middleware in src/webhook/signature.ts",
      normalized_value: {
        completion_state: "COMPLETED",
        scope: "INDIVIDUAL",
        environment_scope: "PRODUCTION",
        causal_basis: "DIRECT_CAUSATION",
        role: "IMPLEMENTED",
        identity_status: "VERIFIED_PERSON",
      },
      fact_metadata: {
        canonical_key: "sha256:fact_hmac_impl_01",
      },
      status: "SUPPORTED",
      extractor_version: "fact-extractor-v1",
      direct_support_count: 1,
      context_support_count: 0,
      contradicts_support_count: 0,
      etag: '"v1"',
      created_at: "2026-09-12T09:00:00Z",
      updated_at: "2026-09-12T09:05:00Z",
    };

    const factMetric: FactSummaryItem = {
      fact_id: "fact_latency_metric_02",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_fact_id: null,
      fact_type: "METRIC",
      subject_identity_id: "id_seoyeon_verified_01",
      predicate: "reduced_p99_verification_latency",
      object_text: "42% p99 latency reduction across production webhook traffic",
      normalized_value: {
        completion_state: "COMPLETED",
        scope: "INDIVIDUAL",
        environment_scope: "PRODUCTION",
        causal_basis: "OBSERVED_CORRELATION",
        metric_details: {
          value: "42",
          unit: "% p99 latency reduction (19.4ms -> 11.2ms)",
          period: "30 days (2026-08-22 ~ 2026-09-21)",
          source_basis: "PR #88 Production Telemetry Benchmark",
          environment_scope: "PRODUCTION",
          measurement_scope: "payment-webhook-gateway (1.2M deliveries)",
        },
      },
      fact_metadata: { canonical_key: "sha256:fact_latency_metric_02" },
      status: "SUPPORTED",
      extractor_version: "fact-extractor-v1",
      direct_support_count: 1,
      context_support_count: 0,
      contradicts_support_count: 0,
      etag: '"v1"',
      created_at: "2026-09-12T09:02:00Z",
      updated_at: "2026-09-12T09:06:00Z",
    };

    const factCandidate: FactSummaryItem = {
      fact_id: "fact_replay_window_03",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_fact_id: null,
      fact_type: "RESULT",
      subject_identity_id: "id_seoyeon_verified_01",
      predicate: "configured_replay_tolerance",
      object_text:
        "5-minute timestamp tolerance window blocking duplicate replay webhooks",
      normalized_value: {
        completion_state: "COMPLETED",
        scope: "INDIVIDUAL",
        environment_scope: "PRODUCTION",
        causal_basis: "DIRECT_CAUSATION",
      },
      fact_metadata: { canonical_key: "sha256:fact_replay_window_03" },
      status: "CANDIDATE",
      extractor_version: "fact-extractor-v1",
      direct_support_count: 0,
      context_support_count: 1,
      contradicts_support_count: 0,
      etag: '"v1"',
      created_at: "2026-09-12T09:10:00Z",
      updated_at: "2026-09-12T09:10:00Z",
    };

    const factUserConfirmed: FactSummaryItem = {
      fact_id: "fact_cross_team_coord_04",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_fact_id: null,
      fact_type: "ROLE",
      subject_identity_id: "id_seoyeon_verified_01",
      predicate: "coordinated_rollout_with_partners",
      object_text:
        "Partner webhook key rotation runbook verified during staged rollout",
      normalized_value: {
        completion_state: "COMPLETED",
        scope: "TEAM",
        environment_scope: "PRODUCTION",
        role: "IMPLEMENTED",
        identity_status: "VERIFIED_PERSON",
      },
      fact_metadata: { canonical_key: "sha256:fact_cross_team_coord_04" },
      status: "USER_CONFIRMED",
      extractor_version: "fact-extractor-v1",
      direct_support_count: 0,
      context_support_count: 1,
      contradicts_support_count: 0,
      etag: '"v1"',
      created_at: "2026-09-12T09:12:00Z",
      updated_at: "2026-09-12T09:20:00Z",
    };

    for (const f of [factAction, factMetric, factCandidate, factUserConfirmed]) {
      this.facts.set(f.fact_id, f);
    }

    this.factSupports.set(factAction.fact_id, [
      {
        support_id: "fs_hmac_01",
        content_unit_id: "cu_hmac_middleware_01",
        source_object_revision_id: "rev_pr88_sig_01",
        unit_type: "DIFF_HUNK",
        support_type: "DIRECT",
        status: "ACTIVE",
        validator: "HYBRID",
        validator_version: "fact-validator-v1",
        locator: {
          schema: "cm-locator-v1",
          file_path: "src/webhook/signature.ts",
          hunk_header: "@@ -18,4 +18,12 @@",
          start_line: 18,
          end_line: 22,
          start_char: 0,
          end_char: 248,
          span_sha256:
            "sha256:c1093ef18ab21094c0018a7b9f2213d48f0123456789abcdef0123456789abcd",
          source_url:
            "https://github.com/seoyeon-lab-demo/ledger-engine/pull/88/files#diff-signature",
        },
        revision_content_hash: "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
        revision_observed_at: "2026-08-21T06:00:00Z",
        revision_is_tombstone: false,
        last_checked_at: "2026-11-28T03:50:00Z",
        last_resolution_status: "MATCHED",
        support_metadata: { validator_run_id: "run_val_01" },
        created_at: "2026-09-12T09:05:00Z",
      },
    ]);

    this.factSupports.set(factMetric.fact_id, [
      {
        support_id: "fs_metric_02",
        content_unit_id: "cu_replay_metric_02",
        source_object_revision_id: "rev_pr88_body_02",
        unit_type: "PR_BODY_SECTION",
        support_type: "DIRECT",
        status: "ACTIVE",
        validator: "HYBRID",
        validator_version: "fact-validator-v1",
        locator: {
          schema: "cm-locator-v1",
          section: "Benchmark & Verification Results",
          start_line: 1,
          end_line: 3,
          start_char: 0,
          end_char: 215,
          span_sha256:
            "sha256:e4421bc987fa0091e234567890abcdef1234567890abcdef1234567890abcdef",
          source_url:
            "https://github.com/seoyeon-lab-demo/ledger-engine/pull/88#issue-body",
        },
        revision_content_hash: "sha256:7d19b4a0c6e5f20491e4a092bb41c822d3f444cc",
        revision_observed_at: "2026-08-21T06:00:00Z",
        revision_is_tombstone: false,
        last_checked_at: "2026-11-28T03:52:00Z",
        last_resolution_status: "MATCHED",
        support_metadata: { validator_run_id: "run_val_01" },
        created_at: "2026-09-12T09:06:00Z",
      },
    ]);

    this.factSupports.set(factCandidate.fact_id, [
      {
        support_id: "fs_replay_03",
        content_unit_id: "cu_drifted_unit_03",
        source_object_revision_id: "rev_dc091_msg_03",
        unit_type: "COMMIT_MESSAGE",
        support_type: "CONTEXT",
        status: "ACTIVE",
        validator: "RULE",
        validator_version: "fact-validator-v1",
        locator: {
          schema: "cm-locator-v1",
          file_path: "src/webhook/replay-window.ts",
          start_line: 1,
          end_line: 1,
          start_char: 0,
          end_char: 58,
          span_sha256:
            "sha256:ab12cd34ef5600112233445566778899aabbccddeeff00112233445566778899",
          source_url:
            "https://github.com/seoyeon-lab-demo/ledger-engine/commit/d501ab2",
        },
        revision_content_hash: "sha256:33aa91e200bb12cc889911ff22ee44dd66001122",
        revision_observed_at: "2026-08-21T08:00:00Z",
        revision_is_tombstone: false,
        last_checked_at: "2026-11-28T03:55:00Z",
        last_resolution_status: "REVISION_MISMATCH",
        support_metadata: { note: "Context-only support; awaits user review" },
        created_at: "2026-09-12T09:10:00Z",
      },
    ]);

    this.factSupports.set(factUserConfirmed.fact_id, [
      {
        support_id: "fs_coord_04",
        content_unit_id: "cu_unavail_404_04",
        source_object_revision_id: "rev_pr88_rev_04",
        unit_type: "REVIEW_COMMENT",
        support_type: "CONTEXT",
        status: "ACTIVE",
        validator: "USER",
        validator_version: "user-review-v1",
        locator: {
          schema: "cm-locator-v1",
          section: "Review Discussion",
          start_line: 1,
          end_line: 2,
          start_char: 0,
          end_char: 94,
          span_sha256:
            "sha256:554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766",
          source_url:
            "https://github.com/seoyeon-lab-demo/ledger-engine/pull/88#discussion_r1",
        },
        revision_content_hash: "sha256:11223344556677889900aabbccddeeff11223344",
        revision_observed_at: "2026-08-21T06:12:00Z",
        revision_is_tombstone: false,
        last_checked_at: "2026-11-28T03:58:00Z",
        last_resolution_status: "UNAVAILABLE",
        support_metadata: { note: "User confirmed during fact curation" },
        created_at: "2026-09-12T09:20:00Z",
      },
    ]);

    // --- Grounded Claims (/v1/claims*) ---
    const claimVerified: ClaimDetailResponse = {
      claim_id: "clm_verified_01",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_claim_id: null,
      claim_text:
        "Implemented constant-time HMAC-SHA256 webhook signature verification middleware, reducing production p99 verification latency by 42% (19.4ms -> 11.2ms) over 30 days across 1.2M deliveries.",
      status: "APPROVED",
      computed_verification: "SOURCE_VERIFIED",
      atom_count: 3,
      supported_atom_count: 3,
      user_confirmed_atom_count: 0,
      unsupported_atom_count: 0,
      stale_atom_count: 0,
      approvable: true,
      approval_block_reasons: [],
      support_graph_epoch: 4,
      approved_at: "2026-11-27T10:00:00Z",
      last_verified_at: "2026-11-28T03:52:00Z",
      generator_run_id: "run_gen_01",
      last_verification_run_id: "run_ver_01",
      created_at: "2026-11-27T09:00:00Z",
      updated_at: "2026-11-27T10:00:00Z",
      atoms: [
        {
          atom_id: "atom_v01_role",
          ordinal: 1,
          atom_type: "ROLE",
          atom_text: "Directly implemented the webhook signature middleware",
          status: "SUPPORTED",
          role_grounding: {
            role: "IMPLEMENTED",
            identity_status: "VERIFIED_PERSON",
            can_support_source_verified: true,
            note: "CLAIM-05 통과: 활성 VERIFIED PERSON(id_seoyeon_verified_01) 및 DIRECT SUPPORTED Fact 확인",
          },
        },
        {
          atom_id: "atom_v01_action",
          ordinal: 2,
          atom_type: "ACTION",
          atom_text:
            "Built constant-time HMAC-SHA256 verification using timingSafeEqual in src/webhook/signature.ts",
          status: "SUPPORTED",
        },
        {
          atom_id: "atom_v01_metric",
          ordinal: 3,
          atom_type: "METRIC",
          atom_text:
            "Reduced production p99 verification latency by 42% (19.4ms -> 11.2ms) over 30 days across 1.2M webhook deliveries",
          status: "SUPPORTED",
          metric_details: {
            value: "42",
            unit: "% p99 latency reduction (19.4ms -> 11.2ms)",
            period: "30 days (2026-08-22 ~ 2026-09-21)",
            source_basis: "PR #88 Production Telemetry Benchmark",
            environment_scope: "PRODUCTION",
            measurement_scope: "payment-webhook-gateway (1.2M deliveries)",
          },
        },
      ],
    };

    const claimMixed: ClaimDetailResponse = {
      claim_id: "clm_mixed_02",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_claim_id: null,
      claim_text:
        "Implemented HMAC-SHA256 webhook verification and coordinated staged partner key rotation rollout across payment webhook consumers.",
      status: "SUPPORT_VALIDATED",
      computed_verification: "MIXED",
      atom_count: 2,
      supported_atom_count: 1,
      user_confirmed_atom_count: 1,
      unsupported_atom_count: 0,
      stale_atom_count: 0,
      approvable: true,
      approval_block_reasons: [],
      support_graph_epoch: 5,
      approved_at: null,
      last_verified_at: "2026-11-28T03:58:00Z",
      generator_run_id: "run_gen_02",
      last_verification_run_id: "run_ver_02",
      created_at: "2026-11-27T11:00:00Z",
      updated_at: "2026-11-28T03:58:00Z",
      atoms: [
        {
          atom_id: "atom_m02_action",
          ordinal: 1,
          atom_type: "ACTION",
          atom_text: "Implemented HMAC-SHA256 webhook verification middleware",
          status: "SUPPORTED",
        },
        {
          atom_id: "atom_m02_role",
          ordinal: 2,
          atom_type: "ROLE",
          atom_text:
            "Coordinated staged partner key rotation rollout across payment webhook consumers",
          status: "USER_CONFIRMED",
          role_grounding: {
            role: "IMPLEMENTED",
            identity_status: "VERIFIED_PERSON",
            can_support_source_verified: false,
            note: "사용자 확인(USER_CONFIRMED) 근거 의존 — SOURCE_VERIFIED로 자동 승격되지 않음",
          },
        },
      ],
    };

    const claimBlocked: ClaimDetailResponse = {
      claim_id: "clm_blocked_03",
      workspace_id: WORKSPACE_ID,
      event_id: eventId,
      version: 1,
      supersedes_claim_id: null,
      claim_text:
        "Led organization-wide security architecture overhaul and eliminated 100% of payment fraud incidents across all global regions.",
      status: "SUPPORT_VALIDATED",
      computed_verification: "UNVERIFIED",
      atom_count: 2,
      supported_atom_count: 0,
      user_confirmed_atom_count: 0,
      unsupported_atom_count: 1,
      stale_atom_count: 1,
      approvable: false,
      approval_block_reasons: [
        "UNSUPPORTED_ATOM: Atom #1 (ROLE)의 'organization-wide security architecture overhaul'에 대한 DIRECT 근거 및 CLAIM-05 신원 권한 근거가 없습니다.",
        "STALE_ATOM: Atom #2 (METRIC)의 6요소 지표(period, source_basis, measurement_scope) 누락 및 리비전 드리프트로 STALE 처리되었습니다.",
      ],
      support_graph_epoch: 2,
      approved_at: null,
      last_verified_at: "2026-11-28T03:55:00Z",
      generator_run_id: "run_gen_03",
      last_verification_run_id: "run_ver_03",
      created_at: "2026-11-27T12:00:00Z",
      updated_at: "2026-11-28T03:55:00Z",
      atoms: [
        {
          atom_id: "atom_b03_role",
          ordinal: 1,
          atom_type: "ROLE",
          atom_text: "Led organization-wide security architecture overhaul",
          status: "UNSUPPORTED",
          block_reason:
            "CLAIM-05 위반: 광범위한 리더십/아키텍처 총괄 주장에 대응하는 DIRECT SUPPORTED Fact가 없습니다.",
          role_grounding: {
            role: "ARCHITECTED",
            identity_status: "CONFLICTED",
            can_support_source_verified: false,
            note: "신원 충돌(CONFLICTED) 및 금지된 자동 역할 추론(ARCHITECTED)으로 차단됨",
          },
        },
        {
          atom_id: "atom_b03_metric",
          ordinal: 2,
          atom_type: "METRIC",
          atom_text:
            "Eliminated 100% of payment fraud incidents across all global regions",
          status: "STALE",
          block_reason:
            "CLAIM-06 미충족(6요소 중 2개만 존재) 및 연결된 리비전 해시 드리프트(STALE)",
          metric_details: {
            value: "100",
            unit: "%",
            environment_scope: "UNSPECIFIED",
          },
        },
      ],
    };

    for (const c of [claimVerified, claimMixed, claimBlocked]) {
      this.claims.set(c.claim_id, c);
    }

    // --- Claim Supports Provenance Graphs ---
    this.claimSupports.set(claimVerified.claim_id, {
      claim_id: claimVerified.claim_id,
      version: claimVerified.version,
      event_id: eventId,
      status: claimVerified.status,
      computed_verification: claimVerified.computed_verification,
      support_graph_epoch: claimVerified.support_graph_epoch,
      atoms: [
        {
          atom_id: "atom_v01_role",
          ordinal: 1,
          atom_type: "ROLE",
          atom_text: claimVerified.atoms[0].atom_text,
          status: "SUPPORTED",
          supports: [
            {
              claim_atom_support_id: "cas_v01_1",
              support_type: "DIRECT",
              status: "ACTIVE",
              validation_run_id: "run_ver_01",
              fact: {
                fact_id: factAction.fact_id,
                version: factAction.version,
                fact_type: factAction.fact_type,
                predicate: factAction.predicate,
                object_text: factAction.object_text,
                status: factAction.status,
                normalized_value: factAction.normalized_value,
                subject_identity_id: factAction.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_hmac_01",
                  support_type: "DIRECT",
                  status: "ACTIVE",
                  validator: "HYBRID",
                  validator_version: "fact-validator-v1",
                  content_unit_id: "cu_hmac_middleware_01",
                  unit_type: "DIFF_HUNK",
                  locator: this.factSupports.get(factAction.fact_id)![0].locator!,
                  source_object_revision_id: "rev_pr88_sig_01",
                  revision_content_hash:
                    "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
                  revision_observed_at: "2026-08-21T06:00:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:50:00Z",
                  last_resolution_status: "MATCHED",
                },
              ],
            },
          ],
        },
        {
          atom_id: "atom_v01_action",
          ordinal: 2,
          atom_type: "ACTION",
          atom_text: claimVerified.atoms[1].atom_text,
          status: "SUPPORTED",
          supports: [
            {
              claim_atom_support_id: "cas_v01_2",
              support_type: "DIRECT",
              status: "ACTIVE",
              validation_run_id: "run_ver_01",
              fact: {
                fact_id: factAction.fact_id,
                version: factAction.version,
                fact_type: factAction.fact_type,
                predicate: factAction.predicate,
                object_text: factAction.object_text,
                status: factAction.status,
                normalized_value: factAction.normalized_value,
                subject_identity_id: factAction.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_hmac_01",
                  support_type: "DIRECT",
                  status: "ACTIVE",
                  validator: "HYBRID",
                  validator_version: "fact-validator-v1",
                  content_unit_id: "cu_hmac_middleware_01",
                  unit_type: "DIFF_HUNK",
                  locator: this.factSupports.get(factAction.fact_id)![0].locator!,
                  source_object_revision_id: "rev_pr88_sig_01",
                  revision_content_hash:
                    "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
                  revision_observed_at: "2026-08-21T06:00:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:50:00Z",
                  last_resolution_status: "MATCHED",
                },
              ],
            },
          ],
        },
        {
          atom_id: "atom_v01_metric",
          ordinal: 3,
          atom_type: "METRIC",
          atom_text: claimVerified.atoms[2].atom_text,
          status: "SUPPORTED",
          supports: [
            {
              claim_atom_support_id: "cas_v01_3",
              support_type: "DIRECT",
              status: "ACTIVE",
              validation_run_id: "run_ver_01",
              fact: {
                fact_id: factMetric.fact_id,
                version: factMetric.version,
                fact_type: factMetric.fact_type,
                predicate: factMetric.predicate,
                object_text: factMetric.object_text,
                status: factMetric.status,
                normalized_value: factMetric.normalized_value,
                subject_identity_id: factMetric.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_metric_02",
                  support_type: "DIRECT",
                  status: "ACTIVE",
                  validator: "HYBRID",
                  validator_version: "fact-validator-v1",
                  content_unit_id: "cu_replay_metric_02",
                  unit_type: "PR_BODY_SECTION",
                  locator: this.factSupports.get(factMetric.fact_id)![0].locator!,
                  source_object_revision_id: "rev_pr88_body_02",
                  revision_content_hash:
                    "sha256:7d19b4a0c6e5f20491e4a092bb41c822d3f444cc",
                  revision_observed_at: "2026-08-21T06:00:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:52:00Z",
                  last_resolution_status: "MATCHED",
                },
              ],
            },
          ],
        },
      ],
    });

    this.claimSupports.set(claimMixed.claim_id, {
      claim_id: claimMixed.claim_id,
      version: claimMixed.version,
      event_id: eventId,
      status: claimMixed.status,
      computed_verification: claimMixed.computed_verification,
      support_graph_epoch: claimMixed.support_graph_epoch,
      atoms: [
        {
          atom_id: "atom_m02_action",
          ordinal: 1,
          atom_type: "ACTION",
          atom_text: claimMixed.atoms[0].atom_text,
          status: "SUPPORTED",
          supports: [
            {
              claim_atom_support_id: "cas_m02_1",
              support_type: "DIRECT",
              status: "ACTIVE",
              validation_run_id: "run_ver_02",
              fact: {
                fact_id: factAction.fact_id,
                version: factAction.version,
                fact_type: factAction.fact_type,
                predicate: factAction.predicate,
                object_text: factAction.object_text,
                status: factAction.status,
                normalized_value: factAction.normalized_value,
                subject_identity_id: factAction.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_hmac_01",
                  support_type: "DIRECT",
                  status: "ACTIVE",
                  validator: "HYBRID",
                  validator_version: "fact-validator-v1",
                  content_unit_id: "cu_hmac_middleware_01",
                  unit_type: "DIFF_HUNK",
                  locator: this.factSupports.get(factAction.fact_id)![0].locator!,
                  source_object_revision_id: "rev_pr88_sig_01",
                  revision_content_hash:
                    "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
                  revision_observed_at: "2026-08-21T06:00:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:50:00Z",
                  last_resolution_status: "MATCHED",
                },
              ],
            },
          ],
        },
        {
          atom_id: "atom_m02_role",
          ordinal: 2,
          atom_type: "ROLE",
          atom_text: claimMixed.atoms[1].atom_text,
          status: "USER_CONFIRMED",
          supports: [
            {
              claim_atom_support_id: "cas_m02_2",
              support_type: "CONTEXT",
              status: "ACTIVE",
              validation_run_id: "run_ver_02",
              fact: {
                fact_id: factUserConfirmed.fact_id,
                version: factUserConfirmed.version,
                fact_type: factUserConfirmed.fact_type,
                predicate: factUserConfirmed.predicate,
                object_text: factUserConfirmed.object_text,
                status: factUserConfirmed.status,
                normalized_value: factUserConfirmed.normalized_value,
                subject_identity_id: factUserConfirmed.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_coord_04",
                  support_type: "CONTEXT",
                  status: "ACTIVE",
                  validator: "USER",
                  validator_version: "user-review-v1",
                  content_unit_id: "cu_unavail_404_04",
                  unit_type: "REVIEW_COMMENT",
                  locator:
                    this.factSupports.get(factUserConfirmed.fact_id)![0]
                      .locator!,
                  source_object_revision_id: "rev_pr88_rev_04",
                  revision_content_hash:
                    "sha256:11223344556677889900aabbccddeeff11223344",
                  revision_observed_at: "2026-08-21T06:12:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:58:00Z",
                  last_resolution_status: "UNAVAILABLE",
                },
              ],
            },
          ],
        },
      ],
    });

    this.claimSupports.set(claimBlocked.claim_id, {
      claim_id: claimBlocked.claim_id,
      version: claimBlocked.version,
      event_id: eventId,
      status: claimBlocked.status,
      computed_verification: claimBlocked.computed_verification,
      support_graph_epoch: claimBlocked.support_graph_epoch,
      atoms: [
        {
          atom_id: "atom_b03_role",
          ordinal: 1,
          atom_type: "ROLE",
          atom_text: claimBlocked.atoms[0].atom_text,
          status: "UNSUPPORTED",
          supports: [],
        },
        {
          atom_id: "atom_b03_metric",
          ordinal: 2,
          atom_type: "METRIC",
          atom_text: claimBlocked.atoms[1].atom_text,
          status: "STALE",
          supports: [
            {
              claim_atom_support_id: "cas_b03_2",
              support_type: "CONTRADICTS",
              status: "STALE",
              validation_run_id: "run_ver_03",
              fact: {
                fact_id: factCandidate.fact_id,
                version: factCandidate.version,
                fact_type: factCandidate.fact_type,
                predicate: factCandidate.predicate,
                object_text: factCandidate.object_text,
                status: factCandidate.status,
                normalized_value: factCandidate.normalized_value,
                subject_identity_id: factCandidate.subject_identity_id,
              },
              fact_supports: [
                {
                  fact_support_id: "fs_replay_03",
                  support_type: "CONTEXT",
                  status: "STALE",
                  validator: "RULE",
                  validator_version: "fact-validator-v1",
                  content_unit_id: "cu_drifted_unit_03",
                  unit_type: "COMMIT_MESSAGE",
                  locator:
                    this.factSupports.get(factCandidate.fact_id)![0].locator!,
                  source_object_revision_id: "rev_dc091_msg_03",
                  revision_content_hash:
                    "sha256:33aa91e200bb12cc889911ff22ee44dd66001122",
                  revision_observed_at: "2026-08-21T08:00:00Z",
                  revision_is_tombstone: false,
                  last_checked_at: "2026-11-28T03:55:00Z",
                  last_resolution_status: "REVISION_MISMATCH",
                },
              ],
            },
          ],
        },
      ],
    });

    // --- Pre-existing Zero-Quote Exports (including 1 INVALIDATED export) ---
    const staleExport: ExportDetailResponse = {
      export_id: "exp_invalidated_01",
      workspace_id: WORKSPACE_ID,
      format: "MARKDOWN",
      export_policy: "SOURCE_VERIFIED_ONLY",
      status: "INVALIDATED",
      claim_ids: ["clm_verified_01"],
      invalidation_reason:
        "UPSTREAM_SUPPORT_REVALIDATED: 연결된 Fact/Claim 버전이 갱신되어 이전 Export가 무효화(INVALIDATED)되었습니다. 최신 승인 Claim으로 다시 생성하세요.",
      created_at: "2026-11-26T08:00:00Z",
      rendered_markdown: null,
      rendered_json: null,
      manifest: {
        export_policy: "SOURCE_VERIFIED_ONLY",
        claim_count: 1,
        includes_user_confirmed: false,
        contains_raw_quotes: false,
        citations: [
          {
            claim_id: "clm_verified_01",
            claim_version: 1,
            computed_verification: "SOURCE_VERIFIED",
            source_url:
              "https://github.com/seoyeon-lab-demo/ledger-engine/pull/88/files#diff-signature",
            locator: this.factSupports.get(factAction.fact_id)![0].locator!,
            revision_content_hash:
              "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
            observed_at: "2026-08-21T06:00:00Z",
            checked_at: "2026-11-26T07:55:00Z",
          },
        ],
      },
    };
    this.exportsMap.set(staleExport.export_id, staleExport);
  }

  // --- Harness Controls ---

  public setScenario(scenarioId: ClaimReviewScenarioId): void {
    this.scenarioId = scenarioId;
    this.ephemeralOverride = "DEFAULT";
    this.seedState();
  }

  public getScenario(): ClaimReviewScenarioId {
    return this.scenarioId;
  }

  public getWorkspaceGuard(eventId?: string): ClaimReviewWorkspaceGuard {
    const targetEventId = eventId ?? this.activeEventId;
    if (this.scenarioId === "import_not_ready") {
      return {
        import_ready: false,
        import_status: "PARTIAL",
        policy_status: "ACTIVE",
        event_id: targetEventId,
        event_title: "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
        event_status: "CONFIRMED",
        repository_label: REPO_LABEL,
        notice:
          "초기 가져오기가 PARTIAL(import_ready=false) 상태이므로 Grounded Claim Review 및 Export가 잠겨 있습니다.",
      };
    }
    if (this.scenarioId === "policy_blocked") {
      return {
        import_ready: true,
        import_status: "SUCCESS",
        policy_status: "POLICY_BLOCKED",
        event_id: targetEventId,
        event_title: "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
        event_status: "CONFIRMED",
        repository_label: REPO_LABEL,
        notice:
          "저장소 공개→비공개 전환(POLICY_BLOCKED · 원본 삭제 아님)으로 모든 Fact/Claim 변경·승인·Export 및 원문 열람이 차단되었습니다.",
      };
    }
    if (this.scenarioId === "deletion_pending") {
      return {
        import_ready: true,
        import_status: "SUCCESS",
        policy_status: "DELETION_PENDING",
        event_id: targetEventId,
        event_title: "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
        event_status: "CONFIRMED",
        repository_label: REPO_LABEL,
        notice:
          "삭제 펜스(DELETION_PENDING · OD-003)가 활성화된 동안 모든 변경·승인·Export 및 원문 열람이 차단됩니다.",
      };
    }
    return {
      import_ready: true,
      import_status: "SUCCESS",
      policy_status: "ACTIVE",
      event_id: targetEventId,
      event_title:
        targetEventId === "evt_pr_104"
          ? "PR #104: 정산 원장 멱등성 키 검증 강화 및 배치 타임아웃 리팩터링"
          : "결제 Webhook 서명 검증 및 리플레이 방어 체계 구축",
      event_status: "CONFIRMED",
      repository_label: REPO_LABEL,
      notice: null,
    };
  }

  public simulateConcurrentClaimUpdate(claimId: string): void {
    const claim = this.claims.get(claimId);
    if (!claim) return;
    claim.version += 1;
    claim.support_graph_epoch += 1;
    claim.updated_at = nowIso();
    this.conflictArmedForClaimIds.add(claimId);
  }

  public setEphemeralResolutionOverride(
    mode: EphemeralResolutionOverrideMode
  ): void {
    this.ephemeralOverride = mode;
  }

  public getEphemeralResolutionOverride(): EphemeralResolutionOverrideMode {
    return this.ephemeralOverride;
  }

  public reset(): void {
    this.ephemeralOverride = "DEFAULT";
    this.requestedPaths = [];
    this.mutationPayloads = [];
    this.seedState();
  }

  // --- Facts (/v1/facts*) ---

  public async listFacts(
    query?: ListFactsQuery
  ): Promise<ClaimReviewResult<FactListResponse>> {
    await this.delay();
    this.requestedPaths.push("/v1/facts");
    const includeSuperseded = query?.include_superseded ?? false;
    const items = Array.from(this.facts.values())
      .filter((f) => includeSuperseded || f.status !== "SUPERSEDED")
      .map(clone);
    return {
      ok: true,
      data: { items, page: { next_cursor: null, has_more: false } },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async getFactSupports(
    factId: string
  ): Promise<ClaimReviewResult<FactSupportsResponse>> {
    await this.delay();
    this.requestedPaths.push(`/v1/facts/${factId}/supports`);
    const fact = this.facts.get(factId);
    if (!fact) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "요청한 Fact를 찾을 수 없습니다.",
        },
        meta: { httpStatus: 404, cacheControl: "private, no-store" },
      };
    }
    return {
      ok: true,
      data: {
        fact: clone(fact),
        items: clone(this.factSupports.get(factId) ?? []),
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async approveFact(
    input: ApproveFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    await this.delay();
    const path = `/v1/facts/${input.fact_id}/actions/approve`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const fact = this.facts.get(input.fact_id);
    if (!fact) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Fact를 찾을 수 없습니다.",
        },
      };
    }
    if (fact.version !== input.expected_version) {
      return {
        ok: false,
        error: {
          kind: "version_conflict",
          httpStatus: 409,
          code: "FACT_VERSION_CONFLICT",
          message: `Fact 버전 충돌: 기대 v${input.expected_version}, 현재 v${fact.version}`,
          conflictDetails: {
            fact_id: fact.fact_id,
            current_version: fact.version,
            current_status: fact.status,
          },
        },
      };
    }
    // Approve transitions CANDIDATE -> USER_CONFIRMED (never SUPPORTED!)
    fact.status = "USER_CONFIRMED";
    fact.updated_at = nowIso();
    return {
      ok: true,
      data: { fact: clone(fact), idempotent_replay: false },
      meta: { httpStatus: 200, etag: fact.etag, cacheControl: "private, no-store" },
    };
  }

  public async rejectFact(
    input: RejectFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    await this.delay();
    const path = `/v1/facts/${input.fact_id}/actions/reject`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const fact = this.facts.get(input.fact_id);
    if (!fact) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Fact를 찾을 수 없습니다.",
        },
      };
    }
    fact.status = "REJECTED";
    fact.updated_at = nowIso();
    return {
      ok: true,
      data: {
        fact: clone(fact),
        invalidated_counts: {
          contributions: 0,
          contribution_supports: 0,
          claim_atoms: 0,
          claim_atom_supports: 0,
          claims: 0,
          exports: 0,
        },
      },
      meta: { httpStatus: 200, etag: fact.etag, cacheControl: "private, no-store" },
    };
  }

  public async editFact(
    input: EditFactInput
  ): Promise<ClaimReviewResult<FactMutationResponse>> {
    await this.delay();
    const path = `/v1/facts/${input.fact_id}`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "PATCH", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const prev = this.facts.get(input.fact_id);
    if (!prev) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Fact를 찾을 수 없습니다.",
        },
      };
    }
    if (prev.version !== input.expected_version) {
      return {
        ok: false,
        error: {
          kind: "version_conflict",
          httpStatus: 409,
          code: "FACT_VERSION_CONFLICT",
          message: `Fact 버전 충돌: 기대 v${input.expected_version}, 현재 v${prev.version}`,
          conflictDetails: {
            fact_id: prev.fact_id,
            current_version: prev.version,
            current_status: prev.status,
          },
        },
      };
    }

    // ER-012 non-overwrite edit: mark prior SUPERSEDED, create new CANDIDATE v+1
    prev.status = "SUPERSEDED";
    prev.updated_at = nowIso();
    this.seq += 1;
    const newId = `fact_edited_${this.seq}`;
    const newVersion = prev.version + 1;
    const created: FactSummaryItem = {
      ...clone(prev),
      fact_id: newId,
      version: newVersion,
      supersedes_fact_id: prev.fact_id,
      predicate: input.predicate,
      object_text: input.object_text,
      normalized_value:
        input.normalized_value !== undefined
          ? clone(input.normalized_value)
          : clone(prev.normalized_value),
      status: "CANDIDATE",
      direct_support_count: 0,
      context_support_count: 0,
      contradicts_support_count: 0,
      etag: `"v${newVersion}"`,
      created_at: nowIso(),
      updated_at: nowIso(),
    };
    this.facts.set(newId, created);
    this.factSupports.set(newId, []);

    return {
      ok: true,
      data: {
        fact: clone(created),
        superseded_fact_id: prev.fact_id,
        superseded_version: prev.version,
        invalidated_counts: {
          contributions: 0,
          contribution_supports: 0,
          claim_atoms: 1,
          claim_atom_supports: 1,
          claims: 1,
          exports: 0,
        },
      },
      meta: {
        httpStatus: 200,
        etag: created.etag,
        cacheControl: "private, no-store",
      },
    };
  }

  // --- Claims (/v1/claims*) ---

  public async listClaims(
    query?: ListClaimsQuery
  ): Promise<ClaimReviewResult<ClaimListResponse>> {
    await this.delay();
    this.requestedPaths.push("/v1/claims");
    const includeSuperseded = query?.include_superseded ?? false;
    const items = Array.from(this.claims.values())
      .filter((c) => includeSuperseded || c.status !== "SUPERSEDED")
      .map(toClaimSummary);
    return {
      ok: true,
      data: { items, page: { next_cursor: null, has_more: false } },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async generateClaims(
    input: GenerateClaimsInput
  ): Promise<ClaimReviewResult<ClaimGenerationResponse>> {
    await this.delay();
    const path = "/v1/claims/generations";
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    if (this.scenarioId === "generation_synthetic_ready") {
      this.seq += 1;
      const newClaimId = `clm_synth_${this.seq}`;
      const synthClaim: ClaimDetailResponse = {
        claim_id: newClaimId,
        workspace_id: WORKSPACE_ID,
        event_id: input.confirmed_event_id,
        version: 1,
        supersedes_claim_id: null,
        claim_text:
          "Implemented HMAC-SHA256 webhook signature verification in src/webhook/signature.ts, cutting production p99 latency by 42% over 30 days.",
        status: "SUPPORT_VALIDATED",
        computed_verification: "SOURCE_VERIFIED",
        atom_count: 2,
        supported_atom_count: 2,
        user_confirmed_atom_count: 0,
        unsupported_atom_count: 0,
        stale_atom_count: 0,
        approvable: true,
        approval_block_reasons: [],
        support_graph_epoch: 1,
        approved_at: null,
        last_verified_at: nowIso(),
        generator_run_id: `run_synth_${this.seq}`,
        last_verification_run_id: `run_val_${this.seq}`,
        created_at: nowIso(),
        updated_at: nowIso(),
        atoms: [
          {
            atom_id: `${newClaimId}_a1`,
            ordinal: 1,
            atom_type: "ACTION",
            atom_text:
              "Implemented HMAC-SHA256 webhook signature verification in src/webhook/signature.ts",
            status: "SUPPORTED",
          },
          {
            atom_id: `${newClaimId}_a2`,
            ordinal: 2,
            atom_type: "METRIC",
            atom_text: "Cut production p99 latency by 42% over 30 days",
            status: "SUPPORTED",
            metric_details: {
              value: "42",
              unit: "% p99 latency reduction",
              period: "30 days",
              source_basis: "PR #88 Production Telemetry Benchmark",
              environment_scope: "PRODUCTION",
              measurement_scope: "payment-webhook-gateway",
            },
          },
        ],
      };
      this.claims.set(newClaimId, synthClaim);
      const baseSupports = this.claimSupports.get("clm_verified_01");
      if (baseSupports) {
        this.claimSupports.set(newClaimId, {
          ...clone(baseSupports),
          claim_id: newClaimId,
          version: 1,
          status: synthClaim.status,
          computed_verification: synthClaim.computed_verification,
        });
      }
      return {
        ok: true,
        data: {
          event_id: input.confirmed_event_id,
          generation_status: "SUCCEEDED",
          reason_code: null,
          reason_message: "합성 픽스처(SYNTHETIC_FIXTURE_OFFLINE) 기반 신규 Claim을 생성하고 검증했습니다.",
          free_alternatives: [],
          claims: [clone(synthClaim)],
        },
        meta: { httpStatus: 200, cacheControl: "private, no-store" },
      };
    }

    // Default safe-OFF hosted LLM gate -> MANUAL_ONLY_BLOCKED with 4 free alternatives
    return {
      ok: true,
      data: {
        event_id: input.confirmed_event_id,
        generation_status: "MANUAL_ONLY_BLOCKED",
        reason_code: "HOSTED_LLM_DISABLED_BY_DEFAULT",
        reason_message:
          "라이브 호스팅 LLM 호출이 기본 비활성화(OFF · 외부 호출 0건) 상태입니다. 아래 무료 대안을 사용하거나 기존 Fact 기반으로 수동 초안을 편집·재검증하세요.",
        free_alternatives: [...FREE_ALTERNATIVES_DEFAULT],
        claims: [],
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async getClaimDetail(
    claimId: string
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    await this.delay();
    this.requestedPaths.push(`/v1/claims/${claimId}`);
    const claim = this.claims.get(claimId);
    if (!claim) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "요청한 Claim을 찾을 수 없습니다.",
        },
      };
    }
    return {
      ok: true,
      data: clone(claim),
      meta: {
        httpStatus: 200,
        etag: `"v${claim.version}"`,
        cacheControl: "private, no-store",
      },
    };
  }

  public async getClaimSupports(
    claimId: string
  ): Promise<ClaimReviewResult<ClaimSupportsResponse>> {
    await this.delay();
    this.requestedPaths.push(`/v1/claims/${claimId}/supports`);
    const supports = this.claimSupports.get(claimId);
    if (!supports) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Claim 근거 그래프를 찾을 수 없습니다.",
        },
      };
    }
    return {
      ok: true,
      data: clone(supports),
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  private checkArmedConflict(
    claim: ClaimDetailResponse,
    expectedVersion: number
  ): ClaimReviewResult<never> | null {
    if (
      this.conflictArmedNextMutation ||
      this.conflictArmedForClaimIds.has(claim.claim_id) ||
      claim.version !== expectedVersion
    ) {
      if (this.conflictArmedNextMutation) {
        this.conflictArmedNextMutation = false;
        claim.version += 1;
        claim.support_graph_epoch += 1;
        claim.updated_at = nowIso();
      }
      this.conflictArmedForClaimIds.delete(claim.claim_id);
      return {
        ok: false,
        error: {
          kind: "version_conflict",
          httpStatus: 409,
          code: "CLAIM_VERSION_CONFLICT",
          message: `다른 세션에서 Claim(${claim.claim_id})이 먼저 수정되어 최신 버전(v${claim.version})과 충돌했습니다. 자동 재전송하지 않았으니 최신 Claim을 다시 불러오세요.`,
          retryable: false,
          conflictDetails: {
            claim_id: claim.claim_id,
            current_version: claim.version,
            current_status: claim.status,
            current_support_graph_epoch: claim.support_graph_epoch,
          },
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }
    return null;
  }

  public async editClaim(
    input: EditClaimInput
  ): Promise<ClaimReviewResult<EditClaimPatchResponse>> {
    await this.delay();
    const path = `/v1/claims/${input.claim_id}`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "PATCH", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const prev = this.claims.get(input.claim_id);
    if (!prev) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Claim을 찾을 수 없습니다.",
        },
      };
    }

    const conflict = this.checkArmedConflict(prev, input.expected_version);
    if (conflict) return conflict;

    // Non-optimistic versioned edit: prior -> SUPERSEDED, new DRAFT v+1 with UNVERIFIED & approvable=false!
    prev.status = "SUPERSEDED";
    prev.updated_at = nowIso();
    this.seq += 1;
    const newClaimId = `${input.claim_id}_v${prev.version + 1}_${this.seq}`;
    const newVersion = prev.version + 1;

    const draftDetail: ClaimDetailResponse = {
      ...clone(prev),
      claim_id: newClaimId,
      version: newVersion,
      supersedes_claim_id: prev.claim_id,
      claim_text: input.claim_text,
      status: "DRAFT",
      computed_verification: "UNVERIFIED",
      approvable: false,
      needs_revalidation: true,
      approval_block_reasons: [
        "NEEDS_REVALIDATION: 편집된 Claim은 Atomization & Support Validation을 다시 실행해야 승인할 수 있습니다.",
      ],
      approved_at: null,
      last_verified_at: null,
      support_graph_epoch: prev.support_graph_epoch + 1,
      updated_at: nowIso(),
      atoms: prev.atoms.map((atom) => ({
        ...clone(atom),
        status: "STALE",
        block_reason: "편집으로 인해 재검증 대기 중 (Needs Revalidation)",
      })),
    };

    this.claims.set(newClaimId, draftDetail);
    const prevSupports = this.claimSupports.get(prev.claim_id);
    if (prevSupports) {
      this.claimSupports.set(newClaimId, {
        ...clone(prevSupports),
        claim_id: newClaimId,
        version: newVersion,
        status: "DRAFT",
        computed_verification: "UNVERIFIED",
        support_graph_epoch: draftDetail.support_graph_epoch,
      });
    }

    return {
      ok: true,
      data: {
        claim_id: newClaimId,
        workspace_id: draftDetail.workspace_id,
        event_id: draftDetail.event_id,
        version: newVersion,
        supersedes_claim_id: prev.claim_id,
        claim_text: draftDetail.claim_text,
        status: "DRAFT",
        computed_verification: "UNVERIFIED",
      },
      meta: {
        httpStatus: 200,
        etag: `"v${newVersion}"`,
        cacheControl: "private, no-store",
      },
    };
  }

  public async revalidateClaim(
    input: RevalidateClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    await this.delay();
    const path = `/v1/claims/${input.claim_id}/actions/revalidate`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const claim = this.claims.get(input.claim_id);
    if (!claim) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Claim을 찾을 수 없습니다.",
        },
      };
    }

    const conflict = this.checkArmedConflict(claim, input.expected_version);
    if (conflict) return conflict;

    const hasUnsupportedText = /100%|fraud|organization-wide/i.test(
      claim.claim_text
    );
    const hasUserConfirmedText = /partner|rollout|coordinated/i.test(
      claim.claim_text
    );

    if (hasUnsupportedText) {
      claim.status = "SUPPORT_VALIDATED";
      claim.computed_verification = "UNVERIFIED";
      claim.approvable = false;
      claim.needs_revalidation = false;
      claim.supported_atom_count = 0;
      claim.user_confirmed_atom_count = 0;
      claim.unsupported_atom_count = claim.atoms.length;
      claim.stale_atom_count = 0;
      claim.approval_block_reasons = [
        "UNSUPPORTED_ATOM: 검증 결과 근거가 부족한 과장/미지원 표현이 포함되어 승인할 수 없습니다.",
      ];
      claim.atoms = claim.atoms.map((a) => ({
        ...a,
        status: "UNSUPPORTED",
        block_reason: "원본 Fact로 입증되지 않는 주장입니다.",
      }));
    } else if (hasUserConfirmedText) {
      claim.status = "SUPPORT_VALIDATED";
      claim.computed_verification = "MIXED";
      claim.approvable = true;
      claim.needs_revalidation = false;
      claim.supported_atom_count = Math.max(1, claim.atoms.length - 1);
      claim.user_confirmed_atom_count = 1;
      claim.unsupported_atom_count = 0;
      claim.stale_atom_count = 0;
      claim.approval_block_reasons = [];
      claim.atoms = claim.atoms.map((a, idx) => ({
        ...a,
        status: idx === claim.atoms.length - 1 ? "USER_CONFIRMED" : "SUPPORTED",
        block_reason: null,
      }));
    } else {
      claim.status = "SUPPORT_VALIDATED";
      claim.computed_verification = "SOURCE_VERIFIED";
      claim.approvable = true;
      claim.needs_revalidation = false;
      claim.supported_atom_count = claim.atoms.length;
      claim.user_confirmed_atom_count = 0;
      claim.unsupported_atom_count = 0;
      claim.stale_atom_count = 0;
      claim.approval_block_reasons = [];
      claim.atoms = claim.atoms.map((a) => ({
        ...a,
        status: "SUPPORTED",
        block_reason: null,
      }));
    }

    claim.last_verified_at = nowIso();
    claim.last_verification_run_id = `run_reval_${++this.seq}`;
    claim.support_graph_epoch += 1;
    claim.updated_at = nowIso();

    const sup = this.claimSupports.get(claim.claim_id);
    if (sup) {
      sup.status = claim.status;
      sup.computed_verification = claim.computed_verification;
      sup.support_graph_epoch = claim.support_graph_epoch;
      sup.atoms = sup.atoms.map((sa, idx) => ({
        ...sa,
        status: claim.atoms[idx]?.status ?? sa.status,
      }));
    }

    return {
      ok: true,
      data: clone(claim),
      meta: {
        httpStatus: 200,
        etag: `"v${claim.version}"`,
        cacheControl: "private, no-store",
      },
    };
  }

  public async approveClaim(
    input: ApproveClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    await this.delay();
    const path = `/v1/claims/${input.claim_id}/actions/approve`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const claim = this.claims.get(input.claim_id);
    if (!claim) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Claim을 찾을 수 없습니다.",
        },
      };
    }

    const conflict = this.checkArmedConflict(claim, input.expected_version);
    if (conflict) return conflict;

    if (
      !claim.approvable ||
      claim.unsupported_atom_count > 0 ||
      claim.stale_atom_count > 0 ||
      claim.status === "DRAFT"
    ) {
      return {
        ok: false,
        error: {
          kind: "support_incomplete",
          httpStatus: 409,
          code: "SUPPORT_INCOMPLETE",
          message:
            "미근거(UNSUPPORTED) 또는 만료(STALE) Atom이 있거나 재검증이 완료되지 않아 승인할 수 없습니다.",
          conflictDetails: {
            claim_id: claim.claim_id,
            current_version: claim.version,
            current_status: claim.status,
            block_reasons: claim.approval_block_reasons,
          },
        },
      };
    }

    if (
      (claim.computed_verification === "USER_CONFIRMED" ||
        claim.computed_verification === "MIXED") &&
      !input.acknowledge_user_confirmed_atoms
    ) {
      return {
        ok: false,
        error: {
          kind: "validation_error",
          httpStatus: 422,
          code: "USER_CONFIRMED_ACK_REQUIRED",
          message:
            "USER_CONFIRMED / MIXED Claim을 승인하려면 사용자 확인 한계 동의가 필요합니다.",
        },
      };
    }

    claim.status = "APPROVED";
    claim.approved_at = nowIso();
    claim.updated_at = nowIso();
    return {
      ok: true,
      data: clone(claim),
      meta: {
        httpStatus: 200,
        etag: `"v${claim.version}"`,
        cacheControl: "private, no-store",
      },
    };
  }

  public async rejectClaim(
    input: RejectClaimInput
  ): Promise<ClaimReviewResult<ClaimDetailResponse>> {
    await this.delay();
    const path = `/v1/claims/${input.claim_id}/actions/reject`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const claim = this.claims.get(input.claim_id);
    if (!claim) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "Claim을 찾을 수 없습니다.",
        },
      };
    }

    const conflict = this.checkArmedConflict(claim, input.expected_version);
    if (conflict) return conflict;

    claim.status = "REJECTED";
    claim.approvable = false;
    claim.updated_at = nowIso();
    return {
      ok: true,
      data: clone(claim),
      meta: {
        httpStatus: 200,
        etag: `"v${claim.version}"`,
        cacheControl: "private, no-store",
      },
    };
  }

  // --- Plan C Ephemeral Content Unit Viewer ---

  public async getContentUnitContent(
    input: GetContentUnitContentInput
  ): Promise<ClaimReviewResult<EphemeralContentResponse>> {
    await this.delay();
    this.requestedPaths.push(
      `/v1/content-units/${input.content_unit_id}/content`
    );

    if (this.isPolicyOrDeletionBlocked()) {
      return {
        ok: false,
        error: {
          kind:
            this.scenarioId === "deletion_pending"
              ? "deletion_pending"
              : "policy_blocked",
          httpStatus: 403,
          code:
            this.scenarioId === "deletion_pending"
              ? "DELETION_PENDING"
              : "SOURCE_POLICY_BLOCKED",
          message:
            this.scenarioId === "deletion_pending"
              ? "삭제 펜스(DELETION_PENDING) 활성 상태로 원문 열람이 차단되었습니다."
              : "공개 정책 차단(POLICY_BLOCKED)으로 원문 열람이 차단되었습니다.",
          retryable: false,
          sourceResolution: {
            status: "POLICY_BLOCKED",
            checked_at: nowIso(),
            expected_revision_hash: null,
            observed_revision_hash: null,
            retryable: false,
            reason_code:
              this.scenarioId === "deletion_pending"
                ? "DELETION_FENCE_ACTIVE"
                : "VISIBILITY_NOT_PUBLIC",
          },
        },
        meta: { httpStatus: 403, cacheControl: "private, no-store" },
      };
    }

    const fixture = this.ephemeralUnits.get(input.content_unit_id);
    if (!fixture) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "요청한 Content Unit을 찾을 수 없습니다.",
        },
        meta: { httpStatus: 404, cacheControl: "private, no-store" },
      };
    }

    const effectiveMode =
      this.ephemeralOverride !== "DEFAULT"
        ? this.ephemeralOverride
        : fixture.defaultMode;

    if (effectiveMode === "MATCHED" || effectiveMode === "DEFAULT") {
      return {
        ok: true,
        data: {
          content_unit_id: input.content_unit_id,
          unit_type: fixture.unit_type,
          content: fixture.content,
          context: input.include_context === false ? null : clone(fixture.context),
          source_resolution: {
            status: "MATCHED",
            checked_at: nowIso(),
            expected_revision_hash: fixture.expected_revision_hash,
            observed_revision_hash: fixture.expected_revision_hash,
            retryable: false,
            reason_code: null,
          },
        },
        meta: { httpStatus: 200, cacheControl: "private, no-store" },
      };
    }

    if (effectiveMode === "REVISION_MISMATCH") {
      return {
        ok: false,
        error: {
          kind: "source_revision_mismatch",
          httpStatus: 409,
          code: "SOURCE_REVISION_MISMATCH",
          message:
            "원격 리비전 해시가 과거 관측 리비전 해시와 다릅니다. 과거 리비전 메타데이터는 변경하지 않았습니다.",
          retryable: false,
          sourceResolution: {
            status: "REVISION_MISMATCH",
            checked_at: nowIso(),
            expected_revision_hash: fixture.expected_revision_hash,
            observed_revision_hash:
              fixture.observed_revision_hash ||
              "sha256:99ff00112233445566778899aabbccddeeff0011",
            retryable: false,
            reason_code: "REVISION_HASH_MISMATCH",
          },
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }

    if (effectiveMode === "INVALID_LOCATOR") {
      return {
        ok: false,
        error: {
          kind: "source_revision_mismatch",
          httpStatus: 409,
          code: "SOURCE_LOCATOR_INVALID",
          message:
            "cm-locator-v1 스팬 오프셋 또는 span_sha256 검증에 실패했습니다(자동 보정 안 함).",
          retryable: false,
          sourceResolution: {
            status: "INVALID_LOCATOR",
            checked_at: nowIso(),
            expected_revision_hash: fixture.expected_revision_hash,
            observed_revision_hash: fixture.expected_revision_hash,
            retryable: false,
            reason_code: "SPAN_SHA256_MISMATCH",
          },
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }

    if (effectiveMode === "POLICY_BLOCKED") {
      return {
        ok: false,
        error: {
          kind: "policy_blocked",
          httpStatus: 403,
          code: "SOURCE_POLICY_BLOCKED",
          message:
            "저장소 공개 정책 변경 또는 권한 회수로 원문 열람이 차단되었습니다.",
          retryable: false,
          sourceResolution: {
            status: "POLICY_BLOCKED",
            checked_at: nowIso(),
            expected_revision_hash: fixture.expected_revision_hash,
            observed_revision_hash: null,
            retryable: false,
            reason_code: "VISIBILITY_CHANGED_PRIVATE",
          },
        },
        meta: { httpStatus: 403, cacheControl: "private, no-store" },
      };
    }

    if (effectiveMode === "PARTIAL_WITH_LIMIT") {
      return {
        ok: false,
        error: {
          kind: "source_limit",
          httpStatus: 422,
          code: "SOURCE_FETCH_LIMIT",
          message:
            "원문 크기 또는 요청 예산 상한(max_unit_bytes)에 도달하여 무음 절단 없이 조회를 중단했습니다.",
          retryable: false,
          sourceResolution: {
            status: "PARTIAL_WITH_LIMIT",
            checked_at: nowIso(),
            expected_revision_hash: fixture.expected_revision_hash,
            observed_revision_hash: null,
            retryable: false,
            reason_code: "UNIT_BYTES_LIMIT_EXCEEDED",
          },
        },
        meta: { httpStatus: 422, cacheControl: "private, no-store" },
      };
    }

    // UNAVAILABLE: distinguish retryable 503/429 vs non-retryable provider 404
    const retryable =
      effectiveMode === "UNAVAILABLE" ||
      effectiveMode === "UNAVAILABLE_RETRYABLE_503";
    return {
      ok: false,
      error: {
        kind: "source_unavailable",
        httpStatus: 503,
        code: "SOURCE_UNAVAILABLE",
        message: retryable
          ? "공급자 일시 오류(503/429/timeout)로 지금 원문을 가져올 수 없습니다."
          : "공급자에서 해당 객체를 찾을 수 없습니다(404/410 — 사용자 삭제나 과거 허위로 분류하지 않음).",
        retryable,
        sourceResolution: {
          status: "UNAVAILABLE",
          checked_at: nowIso(),
          expected_revision_hash: fixture.expected_revision_hash,
          observed_revision_hash: null,
          retryable,
          reason_code: retryable
            ? "PROVIDER_RATE_LIMIT_OR_503"
            : "PROVIDER_OBJECT_NOT_FOUND",
        },
      },
      meta: { httpStatus: 503, cacheControl: "private, no-store" },
    };
  }

  // --- Exports (/v1/exports*) ---

  public async listExports(): Promise<ClaimReviewResult<ExportListResponse>> {
    await this.delay();
    this.requestedPaths.push("/v1/exports");
    const items = Array.from(this.exportsMap.values())
      .filter((e) => e.status !== "DELETED")
      .map(toExportSummary);
    return {
      ok: true,
      data: { items },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async createExport(
    input: CreateExportInput
  ): Promise<ClaimReviewResult<ExportDetailResponse>> {
    await this.delay();
    const path = "/v1/exports";
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "POST", path, body: input });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const selectedClaims: ClaimDetailResponse[] = [];
    for (const cid of input.claim_ids) {
      const c = this.claims.get(cid);
      if (!c) {
        return {
          ok: false,
          error: {
            kind: "not_found",
            httpStatus: 404,
            code: "RESOURCE_NOT_FOUND",
            message: `Claim(${cid})을 찾을 수 없습니다.`,
          },
        };
      }
      if (
        input.expected_claim_versions &&
        input.expected_claim_versions[cid] !== undefined &&
        input.expected_claim_versions[cid] !== c.version
      ) {
        return {
          ok: false,
          error: {
            kind: "version_conflict",
            httpStatus: 409,
            code: "CLAIM_VERSION_CONFLICT",
            message: `Export 대상 Claim(${cid})의 버전이 변경되었습니다(기대 v${input.expected_claim_versions[cid]}, 현재 v${c.version}).`,
            conflictDetails: {
              claim_id: cid,
              current_version: c.version,
              current_status: c.status,
            },
          },
        };
      }
      if (c.status !== "APPROVED") {
        return {
          ok: false,
          error: {
            kind: "support_incomplete",
            httpStatus: 409,
            code: "SUPPORT_INCOMPLETE",
            message: `승인(APPROVED) 상태가 아닌 Claim(${cid})은 Export할 수 없습니다.`,
          },
        };
      }
      if (
        input.export_policy === "SOURCE_VERIFIED_ONLY" &&
        c.computed_verification !== "SOURCE_VERIFIED"
      ) {
        return {
          ok: false,
          error: {
            kind: "validation_error",
            httpStatus: 422,
            code: "EXPORT_POLICY_VIOLATION",
            message:
              "SOURCE_VERIFIED_ONLY 정책에서는 USER_CONFIRMED / MIXED Claim을 Export할 수 없습니다.",
          },
        };
      }
      selectedClaims.push(c);
    }

    this.seq += 1;
    const exportId = `exp_${input.format.toLowerCase()}_${this.seq}`;
    const includesUserConfirmed = selectedClaims.some(
      (c) =>
        c.computed_verification === "USER_CONFIRMED" ||
        c.computed_verification === "MIXED"
    );

    const citations = selectedClaims.map((c) => {
      const sup = this.claimSupports.get(c.claim_id);
      const firstNode = sup?.atoms[0]?.supports[0]?.fact_supports[0];
      return {
        claim_id: c.claim_id,
        claim_version: c.version,
        computed_verification: c.computed_verification,
        source_url:
          firstNode?.locator?.source_url ??
          "https://github.com/seoyeon-lab-demo/ledger-engine/pull/88",
        locator: firstNode?.locator ?? {
          schema: "cm-locator-v1",
          file_path: "src/webhook/signature.ts",
          start_line: 18,
          end_line: 22,
          span_sha256: "sha256:c1093ef18ab21094",
        },
        revision_content_hash:
          firstNode?.revision_content_hash ??
          "sha256:8f41c0d9e3a2b71055b8a771cc20e911f0a111aa",
        observed_at: firstNode?.revision_observed_at ?? "2026-08-21T06:00:00Z",
        checked_at: firstNode?.last_checked_at ?? nowIso(),
      };
    });

    const renderedMarkdown =
      input.format === "MARKDOWN"
        ? [
            `# Career Memory Grounded Claims Export (${exportId})`,
            `- Policy: \`${input.export_policy}\` · Zero raw source quotes (Plan C compliant)`,
            ...(includesUserConfirmed
              ? [
                  "> Limitation Notice: Includes claims relying on USER_CONFIRMED facts.",
                ]
              : []),
            "",
            ...selectedClaims.map((c, idx) => {
              const cite = citations[idx];
              return [
                `## ${idx + 1}. ${c.claim_text}`,
                `- Verification (read-only): \`${c.computed_verification}\` · Version: \`v${c.version}\``,
                `- Citation (non-quote metadata): ${cite.source_url ?? "N/A"} | locator: \`${cite.locator?.file_path ?? cite.locator?.section ?? "cm-locator-v1"} L${cite.locator?.start_line ?? 1}-L${cite.locator?.end_line ?? 1}\` | hash: \`${cite.revision_content_hash}\` | observed_at: \`${cite.observed_at}\` | checked_at: \`${cite.checked_at}\``,
              ].join("\n");
            }),
          ].join("\n")
        : null;

    const renderedJson =
      input.format === "JSON"
        ? {
            schema_version: "cm-export-v1",
            export_id: exportId,
            export_policy: input.export_policy,
            contains_raw_quotes: false,
            includes_user_confirmed: includesUserConfirmed,
            claims: selectedClaims.map((c, idx) => ({
              claim_id: c.claim_id,
              version: c.version,
              claim_text: c.claim_text,
              computed_verification: c.computed_verification,
              citation: citations[idx],
            })),
          }
        : null;

    const created: ExportDetailResponse = {
      export_id: exportId,
      workspace_id: WORKSPACE_ID,
      format: input.format,
      export_policy: input.export_policy,
      status: "ACTIVE",
      claim_ids: [...input.claim_ids],
      invalidation_reason: null,
      created_at: nowIso(),
      rendered_markdown: renderedMarkdown,
      rendered_json: renderedJson,
      manifest: {
        export_policy: input.export_policy,
        claim_count: selectedClaims.length,
        includes_user_confirmed: includesUserConfirmed,
        contains_raw_quotes: false,
        citations,
      },
    };
    this.exportsMap.set(exportId, created);

    return {
      ok: true,
      data: clone(created),
      meta: { httpStatus: 201, cacheControl: "private, no-store" },
    };
  }

  public async getExportDetail(
    exportId: string
  ): Promise<ClaimReviewResult<ExportDetailResponse>> {
    await this.delay();
    this.requestedPaths.push(`/v1/exports/${exportId}`);
    const exp = this.exportsMap.get(exportId);
    if (!exp || exp.status === "DELETED") {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "요청한 Export를 찾을 수 없습니다.",
        },
      };
    }
    return {
      ok: true,
      data: clone(exp),
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async deleteExport(
    input: DeleteExportInput
  ): Promise<ClaimReviewResult<DeleteExportResponse>> {
    await this.delay();
    const path = `/v1/exports/${input.export_id}`;
    this.requestedPaths.push(path);
    this.mutationPayloads.push({ method: "DELETE", path, body: null });
    if (this.isPolicyOrDeletionBlocked()) return this.makeBlockedError();

    const exp = this.exportsMap.get(input.export_id);
    if (!exp) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "삭제할 Export를 찾을 수 없습니다.",
        },
      };
    }
    exp.status = "DELETED";
    return {
      ok: true,
      data: {
        export_id: input.export_id,
        status: "DELETED",
        deleted_at: nowIso(),
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }
}
