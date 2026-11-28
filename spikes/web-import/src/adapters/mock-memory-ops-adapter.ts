import type {
  ConfirmSourceDeletionInput,
  GetQuotaStatusQuery,
  MemoryOpsAdapter,
  MemoryOpsHarnessControls,
} from "@/adapters/memory-ops-adapter";
import type {
  DeletionComponentReceiptItem,
  DeletionConfirmAcceptedResponse,
  DeletionImpactCounts,
  DeletionJobStatusResponse,
  DeletionPreviewResponse,
  MemoryOpsResult,
  MemoryOpsScenarioId,
  QuotaStatusResponse,
  SearchHit,
  SearchQueryInput,
  SearchResponse,
  WorkspaceSourceResourceSummary,
} from "@/domain/memory-ops-models";
import { assertNoProjectIdInRecord } from "@/domain/memory-ops-presenter";

/**
 * Deterministic zero-backend fixture adapter for M5 Workspace Memory Search (OD-008),
 * Source Deletion Preview/Confirm & Receipts (OD-003), and Quota/Pause Status (T-503 / INF-06).
 *
 * Implements all 8 interactive & automated test scenarios:
 * - "default_active"
 * - "index_lag_filtered"
 * - "cursor_version_drift"
 * - "quota_warning_80"
 * - "quota_cutoff_95_paused"
 * - "deletion_scope_changed_409"
 * - "deletion_partial_pending"
 * - "deletion_completed"
 */

export interface MockMemoryOpsAdapterOptions {
  latencyMs?: number;
  initialScenario?: MemoryOpsScenarioId;
  repositoryCountMode?: "single" | "multi";
}

const WORKSPACE_ID = "ws_demo_seoyeon_01";
export const DEMO_PRIMARY_REPO_ID = "src_repo_ledger_01";
export const DEMO_SECONDARY_REPO_ID = "src_repo_portfolio_02";

const DEFAULT_FREE_ALTERNATIVES: string[] = [
  "무료 범위 내 저장소 1건 또는 기간 범위를 축소해 다시 시도하세요 (Reduce scope).",
  "호스팅 LLM 호출 없이 규칙 기반 Fact 검토 및 수동 Claim 편집 모드를 계속 사용하세요 (Manual edit mode).",
  "이미 승인된 Claim의 비인용 Markdown/JSON 확인 및 로컬 편집을 진행하세요.",
  "저장소 데이터 삭제(OD-003)와 GitHub 계정 연결 해제는 한도 차단 중에도 즉시 실행할 수 있습니다.",
];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function makeQueryHash(q: string, entityTypes?: string[]): string {
  const normalized = `${q.trim().toLowerCase()}|${(entityTypes ?? [])
    .slice()
    .sort()
    .join(",")}`;
  let acc = 0x811c9dc5;
  for (let i = 0; i < normalized.length; i += 1) {
    acc ^= normalized.charCodeAt(i);
    acc = Math.imul(acc, 0x01000193);
  }
  const hex = (acc >>> 0).toString(16).padStart(8, "0");
  return `sha256:${hex.repeat(8)}`;
}

interface InternalRepoRecord extends WorkspaceSourceResourceSummary {
  resource_version: string;
  impact_scope_version: string;
  impact_counts: DeletionImpactCounts;
}

export class MockMemoryOpsAdapter
  implements MemoryOpsAdapter, MemoryOpsHarnessControls
{
  private readonly latencyMs: number;
  private scenarioId: MemoryOpsScenarioId;
  private repoCountMode: "single" | "multi";
  private repos = new Map<string, InternalRepoRecord>();
  private deletionJobs = new Map<string, DeletionJobStatusResponse>();
  private previewTokens = new Map<
    string,
    {
      source_resource_id: string;
      resource_version: string;
      impact_scope_version: string;
      expired: boolean;
    }
  >();
  private idempotencyConfirmCache = new Map<
    string,
    DeletionConfirmAcceptedResponse
  >();
  private searchCatalog: SearchHit[] = [];
  private searchIndexVersion = "fts-index-v1";
  private searchRankingVersion = "rank-bm25-v1";
  private cursorDriftTriggered = false;
  private scopeDriftPendingForRepo = new Set<string>();
  private seq = 1;

  // Test observability
  public searchRequests: SearchQueryInput[] = [];
  public confirmRequests: ConfirmSourceDeletionInput[] = [];

  constructor(options: MockMemoryOpsAdapterOptions = {}) {
    this.latencyMs = options.latencyMs ?? 40;
    this.scenarioId = options.initialScenario ?? "default_active";
    this.repoCountMode = options.repositoryCountMode ?? "multi";
    this.seedState();
  }

  private async delay(): Promise<void> {
    if (this.latencyMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.latencyMs));
    }
  }

  private seedState(): void {
    this.repos.clear();
    this.deletionJobs.clear();
    this.previewTokens.clear();
    this.idempotencyConfirmCache.clear();
    this.scopeDriftPendingForRepo.clear();
    this.searchRequests = [];
    this.confirmRequests = [];
    this.searchIndexVersion = "fts-index-v1";
    this.searchRankingVersion = "rank-bm25-v1";
    this.cursorDriftTriggered = false;
    this.seq = 1;

    const primaryRepo: InternalRepoRecord = {
      source_resource_id: DEMO_PRIMARY_REPO_ID,
      repository_full_name: "seoyeon-lab-demo/ledger-engine",
      provider: "GITHUB",
      visibility: "PUBLIC",
      fence_active: false,
      deleted: false,
      active_deletion_job_id: null,
      resource_version: "rv_ledger_001",
      impact_scope_version: "isv_ledger_001",
      impact_counts: {
        work_events: 6,
        facts: 18,
        contributions: 7,
        claims_draft: 2,
        claims_edited: 3,
        claims_approved: 4,
        claims_historical_versions: 5,
        search_index_entries: 28,
        ephemeral_cache_entries: 4,
        server_exports: 3,
      },
    };

    const secondaryRepo: InternalRepoRecord = {
      source_resource_id: DEMO_SECONDARY_REPO_ID,
      repository_full_name: "seoyeon-lab-demo/portfolio-telemetry",
      provider: "GITHUB",
      visibility: "PUBLIC",
      fence_active: false,
      deleted: false,
      active_deletion_job_id: null,
      resource_version: "rv_portfolio_001",
      impact_scope_version: "isv_portfolio_001",
      impact_counts: {
        work_events: 3,
        facts: 9,
        contributions: 3,
        claims_draft: 1,
        claims_edited: 1,
        claims_approved: 2,
        claims_historical_versions: 2,
        search_index_entries: 12,
        ephemeral_cache_entries: 1,
        server_exports: 1,
      },
    };

    this.repos.set(primaryRepo.source_resource_id, primaryRepo);
    if (this.repoCountMode === "multi") {
      this.repos.set(secondaryRepo.source_resource_id, secondaryRepo);
    }

    // Non-raw derived domain search catalog (Plan C: zero raw bodies/quotes)
    this.searchCatalog = [
      {
        entity_type: "WORK_EVENT",
        entity_id: "evt_comp_301",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "결제 Webhook HMAC-SHA256 서명 검증 및 리플레이 방어 체계 구축 (PR #88)",
        score: 0.98,
      },
      {
        entity_type: "CLAIM",
        entity_id: "clm_verified_01",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "Constant-time HMAC-SHA256 webhook verification reduced p99 latency by 42% over 30 days.",
        score: 0.94,
      },
      {
        entity_type: "FACT",
        entity_id: "fact_hmac_impl_01",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "Implemented constant-time HMAC-SHA256 webhook signature verification middleware",
        score: 0.91,
      },
      {
        entity_type: "FACT",
        entity_id: "fact_latency_metric_02",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "42% p99 webhook signature latency reduction (19.4ms -> 11.2ms) across 1.2M deliveries",
        score: 0.88,
      },
      {
        entity_type: "CLAIM",
        entity_id: "clm_mixed_02",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "Coordinated staged webhook partner key rotation rollout across payment consumers.",
        score: 0.84,
      },
      {
        entity_type: "WORK_EVENT",
        entity_id: "evt_direct_302",
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        title_or_summary:
          "Webhook 재시도 백오프 정책 및 데드레터 큐 경보 임계값 정비",
        score: 0.79,
      },
      {
        entity_type: "WORK_EVENT",
        entity_id: "evt_portfolio_401",
        source_resource_id: DEMO_SECONDARY_REPO_ID,
        title_or_summary:
          "OpenTelemetry 트레이스 샘플러 및 Webhook 지연 대시보드 지표 정규화",
        score: 0.86,
      },
      {
        entity_type: "CLAIM",
        entity_id: "clm_portfolio_01",
        source_resource_id: DEMO_SECONDARY_REPO_ID,
        title_or_summary:
          "Standardized OpenTelemetry span attributes for webhook delivery latency dashboards.",
        score: 0.82,
      },
    ];

    // Apply scenario-specific preconditions
    if (this.scenarioId === "deletion_scope_changed_409") {
      this.scopeDriftPendingForRepo.add(DEMO_PRIMARY_REPO_ID);
    }

    if (this.scenarioId === "deletion_partial_pending") {
      const jobId = "del_job_partial_01";
      primaryRepo.fence_active = true;
      primaryRepo.active_deletion_job_id = jobId;
      this.deletionJobs.set(jobId, {
        deletion_job_id: jobId,
        workspace_id: WORKSPACE_ID,
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        status: "PARTIAL",
        user_visible_state: "DELETION_PENDING",
        fence_active: true,
        checkpoint: {
          last_completed_component: "EPHEMERAL_CACHE",
          pending_retry_components: ["FTS_INDEX", "AUTHORITATIVE_DOMAIN"],
        },
        failure_summary: {
          failed_component: "FTS_INDEX",
          last_error_code: "FTS_CLEANUP_TRANSIENT_TIMEOUT",
          retry_scheduled: true,
        },
        components: [
          {
            component: "JOBS_AND_OUTBOX",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 4,
            last_error_code: null,
            receipt_metadata: { canceled_sync_runs: 1, canceled_outbox: 3 },
            updated_at: "2026-11-28T05:10:02Z",
            completed_at: "2026-11-28T05:10:02Z",
          },
          {
            component: "EXPORTS",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 3,
            last_error_code: null,
            receipt_metadata: { payload_nulled: true },
            updated_at: "2026-11-28T05:10:03Z",
            completed_at: "2026-11-28T05:10:03Z",
          },
          {
            component: "FTS_INDEX",
            status: "FAILED",
            attempt: 2,
            purged_count: 14,
            last_error_code: "FTS_CLEANUP_TRANSIENT_TIMEOUT",
            receipt_metadata: { remaining_entries: 14 },
            updated_at: "2026-11-28T05:10:08Z",
            completed_at: null,
          },
          {
            component: "EPHEMERAL_CACHE",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 4,
            last_error_code: null,
            receipt_metadata: { evicted_immediately: true },
            updated_at: "2026-11-28T05:10:01Z",
            completed_at: "2026-11-28T05:10:01Z",
          },
          {
            component: "AUTHORITATIVE_DOMAIN",
            status: "PENDING",
            attempt: 0,
            purged_count: 0,
            last_error_code: null,
            receipt_metadata: { waiting_for: "FTS_INDEX" },
            updated_at: "2026-11-28T05:10:08Z",
            completed_at: null,
          },
        ],
        requested_by: "usr_seoyeon_01",
        requested_at: "2026-11-28T05:10:00Z",
        completed_at: null,
      });
    }

    if (this.scenarioId === "deletion_completed") {
      const jobId = "del_job_completed_01";
      primaryRepo.fence_active = true;
      primaryRepo.deleted = true;
      primaryRepo.active_deletion_job_id = jobId;
      this.deletionJobs.set(jobId, {
        deletion_job_id: jobId,
        workspace_id: WORKSPACE_ID,
        source_resource_id: DEMO_PRIMARY_REPO_ID,
        status: "SUCCEEDED",
        user_visible_state: "DELETED",
        fence_active: true,
        checkpoint: { all_components_succeeded: true },
        failure_summary: {},
        components: [
          {
            component: "JOBS_AND_OUTBOX",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 4,
            last_error_code: null,
            receipt_metadata: { canceled_sync_runs: 1 },
            updated_at: "2026-11-28T05:10:02Z",
            completed_at: "2026-11-28T05:10:02Z",
          },
          {
            component: "EXPORTS",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 3,
            last_error_code: null,
            receipt_metadata: { payload_nulled: true },
            updated_at: "2026-11-28T05:10:03Z",
            completed_at: "2026-11-28T05:10:03Z",
          },
          {
            component: "FTS_INDEX",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 28,
            last_error_code: null,
            receipt_metadata: { remaining_entries: 0 },
            updated_at: "2026-11-28T05:10:05Z",
            completed_at: "2026-11-28T05:10:05Z",
          },
          {
            component: "EPHEMERAL_CACHE",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 4,
            last_error_code: null,
            receipt_metadata: { evicted_immediately: true },
            updated_at: "2026-11-28T05:10:01Z",
            completed_at: "2026-11-28T05:10:01Z",
          },
          {
            component: "AUTHORITATIVE_DOMAIN",
            status: "SUCCEEDED",
            attempt: 1,
            purged_count: 45,
            last_error_code: null,
            receipt_metadata: {
              purged_work_events: 6,
              purged_facts: 18,
              purged_claims_all_versions: 14,
            },
            updated_at: "2026-11-28T05:10:09Z",
            completed_at: "2026-11-28T05:10:09Z",
          },
        ],
        requested_by: "usr_seoyeon_01",
        requested_at: "2026-11-28T05:10:00Z",
        completed_at: "2026-11-28T05:10:09Z",
      });
    }
  }

  // --- Harness Controls ---

  public getScenario(): MemoryOpsScenarioId {
    return this.scenarioId;
  }

  public setScenario(scenario: MemoryOpsScenarioId): void {
    this.scenarioId = scenario;
    this.seedState();
  }

  public getRepositories(): WorkspaceSourceResourceSummary[] {
    return Array.from(this.repos.values()).map((r) => ({
      source_resource_id: r.source_resource_id,
      repository_full_name: r.repository_full_name,
      provider: r.provider,
      visibility: r.visibility,
      fence_active: r.fence_active,
      deleted: r.deleted,
      active_deletion_job_id: r.active_deletion_job_id,
    }));
  }

  public getInitialActiveDeletionJobId(): string | null {
    if (this.scenarioId === "deletion_partial_pending") {
      return "del_job_partial_01";
    }
    if (this.scenarioId === "deletion_completed") {
      return "del_job_completed_01";
    }
    return null;
  }

  public getScenarioNotice(): string | null {
    if (this.scenarioId === "index_lag_filtered") {
      return "T-501A 권위 재검증 적용: 색인 후보 5건 중 STALE/REJECTED/차단된 2건이 응답 직전 제외되어 최신 권위 상태 3건만 반환되었습니다.";
    }
    if (this.scenarioId === "cursor_version_drift") {
      return "검색 커서 드리프트 시나리오: 첫 페이지 검색 후 '다음 결과 더 보기'를 누르면 400 INVALID_SEARCH_CURSOR가 발생하며 '처음부터 다시 검색'으로 복구됩니다.";
    }
    if (this.scenarioId === "deletion_scope_changed_409") {
      return "OD-003 버전 불일치 시나리오: 첫 번째 삭제 확인 시 409 DELETION_PREVIEW_SCOPE_CHANGED가 반환되며 삭제를 차단합니다. '영향 범위 다시 조회' 후 확인하면 202로 접수됩니다.";
    }
    return null;
  }

  public setRepositoryCountMode(mode: "single" | "multi"): void {
    this.repoCountMode = mode;
    this.seedState();
  }

  public getRepositoryCountMode(): "single" | "multi" {
    return this.repoCountMode;
  }

  public advanceDeletionJob(deletionJobId: string): void {
    const job = this.deletionJobs.get(deletionJobId);
    if (!job || job.status === "SUCCEEDED") return;

    const now = "2026-11-28T05:12:00Z";
    const updatedComponents: DeletionComponentReceiptItem[] = job.components.map(
      (comp) => {
        if (comp.status === "SUCCEEDED") return comp;
        return {
          ...comp,
          status: "SUCCEEDED",
          attempt: Math.max(1, comp.attempt + 1),
          purged_count: comp.component === "AUTHORITATIVE_DOMAIN" ? 45 : 28,
          last_error_code: null,
          receipt_metadata: {
            ...comp.receipt_metadata,
            retried_and_verified: true,
          },
          updated_at: now,
          completed_at: now,
        };
      }
    );

    const updatedJob: DeletionJobStatusResponse = {
      ...job,
      status: "SUCCEEDED",
      user_visible_state: "DELETED",
      fence_active: true,
      checkpoint: { all_components_succeeded: true },
      failure_summary: {},
      components: updatedComponents,
      completed_at: now,
    };
    this.deletionJobs.set(deletionJobId, updatedJob);

    const repo = this.repos.get(job.source_resource_id);
    if (repo) {
      repo.fence_active = true;
      repo.deleted = true;
    }
  }

  public reset(): void {
    this.seedState();
  }

  // --- Adapter API ---

  public async searchMemory(
    query: SearchQueryInput
  ): Promise<MemoryOpsResult<SearchResponse>> {
    await this.delay();
    assertNoProjectIdInRecord(
      query as unknown as Record<string, unknown>
    );
    this.searchRequests.push(clone(query));

    const trimmedQ = query.q.trim();
    if (!trimmedQ) {
      return {
        ok: false,
        error: {
          kind: "validation_error",
          httpStatus: 422,
          code: "INVALID_SEARCH_QUERY",
          message: "검색어(q)를 입력해 주세요.",
          retryable: false,
        },
        meta: { httpStatus: 422, cacheControl: "private, no-store" },
      };
    }

    // Resolve repository scope (OD-008 auto-select when 1 active repo exists)
    const activeRepos = Array.from(this.repos.values()).filter(
      (r) => !r.fence_active && !r.deleted
    );
    let resolvedSourceResourceId: string | null =
      query.source_resource_id?.trim() || null;

    if (resolvedSourceResourceId) {
      const targetRepo = this.repos.get(resolvedSourceResourceId);
      if (!targetRepo) {
        return {
          ok: false,
          error: {
            kind: "not_found",
            httpStatus: 404,
            code: "RESOURCE_NOT_FOUND",
            message: "지정한 저장소를 찾을 수 없습니다.",
            retryable: false,
          },
          meta: { httpStatus: 404, cacheControl: "private, no-store" },
        };
      }
      if (targetRepo.fence_active || targetRepo.deleted) {
        return {
          ok: false,
          error: {
            kind: "deletion_pending",
            httpStatus: 409,
            code: "DELETION_FENCE_ACTIVE",
            message:
              "해당 저장소에 삭제 펜스(DELETION_PENDING)가 활성화되어 검색이 차단되었습니다.",
            retryable: false,
          },
          meta: { httpStatus: 409, cacheControl: "private, no-store" },
        };
      }
    } else if (activeRepos.length === 1) {
      resolvedSourceResourceId = activeRepos[0].source_resource_id;
    }

    const queryHash = makeQueryHash(trimmedQ, query.entity_types);

    // Handle cursor version drift scenario deterministically
    let offset = 0;
    if (query.cursor) {
      if (
        this.scenarioId === "cursor_version_drift" &&
        !this.cursorDriftTriggered
      ) {
        this.cursorDriftTriggered = true;
        this.searchIndexVersion = "fts-index-v2";
        this.searchRankingVersion = "rank-bm25-v2";
        return {
          ok: false,
          error: {
            kind: "invalid_search_cursor",
            httpStatus: 400,
            code: "INVALID_SEARCH_CURSOR",
            message:
              "Search cursor binding mismatch for: index_version, ranking_version",
            retryable: false,
            details: {
              mismatched_bindings: ["index_version", "ranking_version"],
            },
          },
          meta: { httpStatus: 400, cacheControl: "private, no-store" },
        };
      }

      const match = /^cur_offset_(\d+)_(fts-index-v\d+)$/.exec(query.cursor);
      if (!match || match[2] !== this.searchIndexVersion) {
        return {
          ok: false,
          error: {
            kind: "invalid_search_cursor",
            httpStatus: 400,
            code: "INVALID_SEARCH_CURSOR",
            message: "Search cursor binding mismatch for: index_version",
            retryable: false,
            details: { mismatched_bindings: ["index_version"] },
          },
          meta: { httpStatus: 400, cacheControl: "private, no-store" },
        };
      }
      offset = Number(match[1]);
    }

    const tokens = trimmedQ.toLowerCase().split(/\s+/).filter(Boolean);
    const allowedTypes =
      query.entity_types && query.entity_types.length > 0
        ? new Set(query.entity_types)
        : null;

    let filtered = this.searchCatalog.filter((hit) => {
      const repo = hit.source_resource_id
        ? this.repos.get(hit.source_resource_id)
        : null;
      if (!repo || repo.fence_active || repo.deleted) {
        return false;
      }
      if (
        resolvedSourceResourceId &&
        hit.source_resource_id !== resolvedSourceResourceId
      ) {
        return false;
      }
      if (allowedTypes && !allowedTypes.has(hit.entity_type)) {
        return false;
      }
      const haystack =
        `${hit.title_or_summary} ${hit.entity_id} ${hit.entity_type}`.toLowerCase();
      return tokens.every((tok) => haystack.includes(tok));
    });

    if (this.scenarioId === "index_lag_filtered") {
      // Simulate authoritative recheck dropping stale/lagged entries
      filtered = filtered.filter(
        (hit) =>
          hit.entity_id !== "clm_mixed_02" && hit.entity_id !== "evt_direct_302"
      );
    }

    const pageSize = Math.max(1, Math.min(100, query.limit ?? 3));
    const pageItems = filtered.slice(offset, offset + pageSize);
    const hasMore = offset + pageSize < filtered.length;

    return {
      ok: true,
      data: {
        scope: {
          workspace_id: WORKSPACE_ID,
          source_resource_id: resolvedSourceResourceId,
        },
        query_hash: queryHash,
        index_version: this.searchIndexVersion,
        ranking_version: this.searchRankingVersion,
        items: clone(pageItems),
        next_cursor: hasMore
          ? `cur_offset_${offset + pageSize}_${this.searchIndexVersion}`
          : null,
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async previewSourceDeletion(
    sourceResourceId: string
  ): Promise<MemoryOpsResult<DeletionPreviewResponse>> {
    await this.delay();
    const repo = this.repos.get(sourceResourceId);
    if (!repo) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "삭제 대상 저장소를 찾을 수 없습니다.",
          retryable: false,
        },
        meta: { httpStatus: 404, cacheControl: "private, no-store" },
      };
    }

    const token = `delprev_${sourceResourceId}_${repo.resource_version}_${this.seq++}`;
    this.previewTokens.set(token, {
      source_resource_id: sourceResourceId,
      resource_version: repo.resource_version,
      impact_scope_version: repo.impact_scope_version,
      expired: false,
    });

    return {
      ok: true,
      data: {
        source_resource_id: repo.source_resource_id,
        resource_version: repo.resource_version,
        impact_scope_version: repo.impact_scope_version,
        impact_counts: clone(repo.impact_counts),
        non_recoverable_scopes: {
          already_downloaded_exports: true,
          provider_hosted_originals: true,
          notice:
            "이미 사용자 기기로 다운로드한 로컬 Export 파일과 GitHub에 호스팅된 원본 저장소·PR·커밋은 서버 영구 삭제를 통해 회수하거나 삭제할 수 없습니다.",
        },
        preview_token: token,
        expires_at: "2026-11-28T06:45:00Z",
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async confirmSourceDeletion(
    input: ConfirmSourceDeletionInput
  ): Promise<MemoryOpsResult<DeletionConfirmAcceptedResponse>> {
    await this.delay();
    this.confirmRequests.push(clone(input));

    const cached = this.idempotencyConfirmCache.get(input.idempotency_key);
    if (cached) {
      return {
        ok: true,
        data: clone(cached),
        meta: { httpStatus: 202, cacheControl: "private, no-store" },
      };
    }

    const repo = this.repos.get(input.source_resource_id);
    if (!repo) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "삭제 대상 저장소를 찾을 수 없습니다.",
          retryable: false,
        },
        meta: { httpStatus: 404, cacheControl: "private, no-store" },
      };
    }

    // Simulate 409 scope changed on first confirm attempt in `deletion_scope_changed_409`
    if (this.scopeDriftPendingForRepo.has(input.source_resource_id)) {
      this.scopeDriftPendingForRepo.delete(input.source_resource_id);
      repo.resource_version = "rv_ledger_002";
      repo.impact_scope_version = "isv_ledger_002";
      repo.impact_counts = {
        ...repo.impact_counts,
        claims_edited: repo.impact_counts.claims_edited + 1,
        server_exports: repo.impact_counts.server_exports + 1,
      };
      return {
        ok: false,
        error: {
          kind: "deletion_preview_scope_changed",
          httpStatus: 409,
          code: "DELETION_PREVIEW_SCOPE_CHANGED",
          message:
            "미리보기 이후 새 사용자 편집 Claim 1건 및 서버 Export 1건이 추가되어 영향 범위 버전이 변경되었습니다. 삭제를 실행하지 않았으니 영향 범위를 다시 조회해 주세요.",
          retryable: false,
          details: {
            expected_resource_version: input.expected_resource_version,
            current_resource_version: repo.resource_version,
            current_impact_scope_version: repo.impact_scope_version,
          },
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }

    const tokenMeta = this.previewTokens.get(input.preview_token);
    if (!tokenMeta || tokenMeta.source_resource_id !== input.source_resource_id) {
      return {
        ok: false,
        error: {
          kind: "deletion_preview_scope_changed",
          httpStatus: 409,
          code: "DELETION_PREVIEW_INVALID",
          message: "유효하지 않은 삭제 미리보기 토큰입니다. 영향 범위를 다시 조회해 주세요.",
          retryable: false,
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }
    if (tokenMeta.expired) {
      return {
        ok: false,
        error: {
          kind: "deletion_preview_expired",
          httpStatus: 409,
          code: "DELETION_PREVIEW_EXPIRED",
          message:
            "삭제 미리보기 유효 시간이 만료되었습니다. 영향 범위를 다시 조회해 주세요.",
          retryable: false,
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }
    if (
      tokenMeta.resource_version !== repo.resource_version ||
      tokenMeta.impact_scope_version !== repo.impact_scope_version ||
      input.expected_resource_version !== repo.resource_version ||
      input.expected_impact_scope_version !== repo.impact_scope_version
    ) {
      return {
        ok: false,
        error: {
          kind: "deletion_preview_scope_changed",
          httpStatus: 409,
          code: "DELETION_PREVIEW_SCOPE_CHANGED",
          message:
            "저장소 버전 또는 영향 범위가 변경되어 삭제를 차단했습니다. 영향 범위를 다시 조회해 주세요.",
          retryable: false,
        },
        meta: { httpStatus: 409, cacheControl: "private, no-store" },
      };
    }

    // Immediate zero-window deletion fence activation on 202!
    const jobId = `del_job_${this.seq++}`;
    repo.fence_active = true;
    repo.active_deletion_job_id = jobId;

    const accepted: DeletionConfirmAcceptedResponse = {
      deletion_job_id: jobId,
      source_resource_id: repo.source_resource_id,
      status: "PENDING",
      fence_active: true,
    };
    this.idempotencyConfirmCache.set(input.idempotency_key, accepted);

    const now = "2026-11-28T05:15:00Z";
    this.deletionJobs.set(jobId, {
      deletion_job_id: jobId,
      workspace_id: WORKSPACE_ID,
      source_resource_id: repo.source_resource_id,
      status: "PARTIAL",
      user_visible_state: "DELETION_PENDING",
      fence_active: true,
      checkpoint: {
        immediate_fence_components_done: [
          "JOBS_AND_OUTBOX",
          "EXPORTS",
          "EPHEMERAL_CACHE",
        ],
      },
      failure_summary: {},
      components: [
        {
          component: "JOBS_AND_OUTBOX",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: 2,
          last_error_code: null,
          receipt_metadata: { canceled_in_confirm_tx: true },
          updated_at: now,
          completed_at: now,
        },
        {
          component: "EXPORTS",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: repo.impact_counts.server_exports,
          last_error_code: null,
          receipt_metadata: { payload_nulled_immediately: true },
          updated_at: now,
          completed_at: now,
        },
        {
          component: "FTS_INDEX",
          status: "PENDING",
          attempt: 0,
          purged_count: 0,
          last_error_code: null,
          receipt_metadata: { queued_for_worker: true },
          updated_at: now,
          completed_at: null,
        },
        {
          component: "EPHEMERAL_CACHE",
          status: "SUCCEEDED",
          attempt: 1,
          purged_count: repo.impact_counts.ephemeral_cache_entries,
          last_error_code: null,
          receipt_metadata: { evicted_immediately: true },
          updated_at: now,
          completed_at: now,
        },
        {
          component: "AUTHORITATIVE_DOMAIN",
          status: "PENDING",
          attempt: 0,
          purged_count: 0,
          last_error_code: null,
          receipt_metadata: { queued_for_worker: true },
          updated_at: now,
          completed_at: null,
        },
      ],
      requested_by: "usr_seoyeon_01",
      requested_at: now,
      completed_at: null,
    });

    return {
      ok: true,
      data: clone(accepted),
      meta: { httpStatus: 202, cacheControl: "private, no-store" },
    };
  }

  public async getDeletionJobStatus(
    deletionJobId: string
  ): Promise<MemoryOpsResult<DeletionJobStatusResponse>> {
    await this.delay();
    const job = this.deletionJobs.get(deletionJobId);
    if (!job) {
      return {
        ok: false,
        error: {
          kind: "not_found",
          httpStatus: 404,
          code: "RESOURCE_NOT_FOUND",
          message: "삭제 작업 영수증을 찾을 수 없습니다.",
          retryable: false,
        },
        meta: { httpStatus: 404, cacheControl: "private, no-store" },
      };
    }
    return {
      ok: true,
      data: clone(job),
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }

  public async getQuotaStatus(
    query?: GetQuotaStatusQuery
  ): Promise<MemoryOpsResult<QuotaStatusResponse>> {
    await this.delay();
    assertNoProjectIdInRecord(
      query as unknown as Record<string, unknown> | undefined
    );

    if (this.scenarioId === "quota_warning_80") {
      return {
        ok: true,
        data: {
          workspace_id: WORKSPACE_ID,
          period_key: "2026-11",
          admission_state: "WARNING_80_PERCENT",
          pause_state: "ACTIVE",
          pause_reason_code: "BUDGET_80_PERCENT_WARNING",
          circuit_breaker_state: "CLOSED",
          high_cost_operations_allowed: true,
          safety_operations_allowed: true,
          paid_fallback_allowed: false,
          retry_after_seconds: null,
          free_alternatives: [...DEFAULT_FREE_ALTERNATIVES],
          meters: [
            {
              metric_kind: "IMPORT_OBJECTS",
              used_value: 840,
              soft_warning_limit: 800,
              hard_cutoff_limit: 950,
              utilization_percent: 84.0,
              status: "WARNING",
            },
            {
              metric_kind: "SEARCH_QUERIES",
              used_value: 410,
              soft_warning_limit: 400,
              hard_cutoff_limit: 475,
              utilization_percent: 82.0,
              status: "WARNING",
            },
            {
              metric_kind: "EXPORT_BUILDS",
              used_value: 12,
              soft_warning_limit: 40,
              hard_cutoff_limit: 48,
              utilization_percent: 24.0,
              status: "OK",
            },
            {
              metric_kind: "ESTIMATED_COST_KRW",
              used_value: 8200,
              soft_warning_limit: 8000,
              hard_cutoff_limit: 9500,
              utilization_percent: 82.0,
              status: "WARNING",
            },
          ],
          checked_at: "2026-11-28T05:14:00Z",
        },
        meta: { httpStatus: 200, cacheControl: "private, no-store" },
      };
    }

    if (this.scenarioId === "quota_cutoff_95_paused") {
      return {
        ok: true,
        data: {
          workspace_id: WORKSPACE_ID,
          period_key: "2026-11",
          admission_state: "CUTOFF_95_PERCENT",
          pause_state: "PAUSED_QUOTA",
          pause_reason_code: "BUDGET_95_PERCENT_CUTOFF",
          circuit_breaker_state: "OPEN",
          high_cost_operations_allowed: false,
          safety_operations_allowed: true,
          paid_fallback_allowed: false,
          retry_after_seconds: 900,
          free_alternatives: [...DEFAULT_FREE_ALTERNATIVES],
          meters: [
            {
              metric_kind: "IMPORT_OBJECTS",
              used_value: 960,
              soft_warning_limit: 800,
              hard_cutoff_limit: 950,
              utilization_percent: 96.0,
              status: "CUTOFF",
            },
            {
              metric_kind: "PROVIDER_REQUESTS",
              used_value: 1920,
              soft_warning_limit: 1600,
              hard_cutoff_limit: 1900,
              utilization_percent: 96.0,
              status: "CUTOFF",
            },
            {
              metric_kind: "EXPORT_BUILDS",
              used_value: 48,
              soft_warning_limit: 40,
              hard_cutoff_limit: 48,
              utilization_percent: 96.0,
              status: "CUTOFF",
            },
            {
              metric_kind: "ESTIMATED_COST_KRW",
              used_value: 9600,
              soft_warning_limit: 8000,
              hard_cutoff_limit: 9500,
              utilization_percent: 96.0,
              status: "CUTOFF",
            },
          ],
          checked_at: "2026-11-28T05:14:30Z",
        },
        meta: { httpStatus: 200, cacheControl: "private, no-store" },
      };
    }

    return {
      ok: true,
      data: {
        workspace_id: WORKSPACE_ID,
        period_key: "2026-11",
        admission_state: "ALLOWED",
        pause_state: "ACTIVE",
        pause_reason_code: null,
        circuit_breaker_state: "CLOSED",
        high_cost_operations_allowed: true,
        safety_operations_allowed: true,
        paid_fallback_allowed: false,
        retry_after_seconds: null,
        free_alternatives: [],
        meters: [
          {
            metric_kind: "IMPORT_OBJECTS",
            used_value: 180,
            soft_warning_limit: 800,
            hard_cutoff_limit: 950,
            utilization_percent: 18.0,
            status: "OK",
          },
          {
            metric_kind: "SEARCH_QUERIES",
            used_value: 42,
            soft_warning_limit: 400,
            hard_cutoff_limit: 475,
            utilization_percent: 8.4,
            status: "OK",
          },
          {
            metric_kind: "EXPORT_BUILDS",
            used_value: 3,
            soft_warning_limit: 40,
            hard_cutoff_limit: 48,
            utilization_percent: 6.0,
            status: "OK",
          },
          {
            metric_kind: "ESTIMATED_COST_KRW",
            used_value: 1200,
            soft_warning_limit: 8000,
            hard_cutoff_limit: 9500,
            utilization_percent: 12.0,
            status: "OK",
          },
        ],
        checked_at: "2026-11-28T05:14:00Z",
      },
      meta: { httpStatus: 200, cacheControl: "private, no-store" },
    };
  }
}
