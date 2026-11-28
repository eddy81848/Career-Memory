"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  MemoryOpsAdapter,
  MemoryOpsHarnessControls,
} from "@/adapters/memory-ops-adapter";
import { MockMemoryOpsAdapter } from "@/adapters/mock-memory-ops-adapter";
import { generateUuidV4 } from "@/adapters/synthetic-http-transport";
import type {
  DeletionJobStatusResponse,
  DeletionPreviewResponse,
  MemoryOpsError,
  MemoryOpsScenarioId,
  QuotaStatusResponse,
  SearchEntityType,
  SearchHit,
  SearchResponse,
  WorkspaceSourceResourceSummary,
} from "@/domain/memory-ops-models";
import {
  evaluateOperationPermissions,
  resolveDefaultSearchScope,
} from "@/domain/memory-ops-presenter";
import { DeletionJobReceiptPanel } from "./DeletionJobReceiptPanel";
import { MemoryOpsScenarioBar } from "./MemoryOpsScenarioBar";
import { MemorySearchPanel } from "./MemorySearchPanel";
import { QuotaBudgetBanner } from "./QuotaBudgetBanner";
import { SourceDeletionModal } from "./SourceDeletionModal";

export interface MemoryOpsAppProps {
  adapter?: MemoryOpsAdapter;
  harness?: MemoryOpsHarnessControls;
  initialScenario?: MemoryOpsScenarioId;
  initialRepositories?: WorkspaceSourceResourceSummary[];
  initialQuery?: string;
}

const DEFAULT_HTTP_REPOSITORIES: WorkspaceSourceResourceSummary[] = [
  {
    source_resource_id: "src_repo_ledger_01",
    repository_full_name: "seoyeon-lab-demo/ledger-engine",
    provider: "GITHUB",
    visibility: "PUBLIC",
    fence_active: false,
    deleted: false,
    active_deletion_job_id: null,
  },
];

export function MemoryOpsApp({
  adapter: injectedAdapter,
  harness: injectedHarness,
  initialScenario = "default_active",
  initialRepositories,
  initialQuery = "webhook",
}: MemoryOpsAppProps) {
  const defaultAdapterRef = useRef<MockMemoryOpsAdapter | null>(null);
  if (!injectedAdapter && !defaultAdapterRef.current) {
    defaultAdapterRef.current = new MockMemoryOpsAdapter({
      latencyMs: 30,
      initialScenario,
    });
  }

  const adapter: MemoryOpsAdapter =
    injectedAdapter ?? defaultAdapterRef.current!;
  const harness: MemoryOpsHarnessControls | undefined =
    injectedHarness ??
    (injectedAdapter ? undefined : defaultAdapterRef.current ?? undefined);

  const [scenarioId, setScenarioId] = useState<MemoryOpsScenarioId>(
    harness?.getScenario() ?? initialScenario
  );
  const [repoCountMode, setRepoCountMode] = useState<"single" | "multi">(
    harness?.getRepositoryCountMode() ?? "multi"
  );
  const [repositories, setRepositories] = useState<
    WorkspaceSourceResourceSummary[]
  >(() =>
    initialRepositories
      ? [...initialRepositories]
      : harness
        ? harness.getRepositories()
        : [...DEFAULT_HTTP_REPOSITORIES]
  );

  // Quota state (T-503 / INF-06)
  const [quotaStatus, setQuotaStatus] = useState<QuotaStatusResponse | null>(
    null
  );
  const [isLoadingQuota, setIsLoadingQuota] = useState(true);
  const [disconnectNotice, setDisconnectNotice] = useState<string | null>(null);

  // Search state (OD-008 / T-501)
  const initialScope = useMemo(
    () => resolveDefaultSearchScope(repositories),
    // Only compute default on first mount; scenario changes update explicitly
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );
  const [selectedSourceResourceId, setSelectedSourceResourceId] = useState<
    string | null
  >(initialScope.defaultSourceResourceId);
  const [queryText, setQueryText] = useState(initialQuery);
  const [selectedEntityTypes, setSelectedEntityTypes] = useState<
    SearchEntityType[]
  >([]);
  const [searchResults, setSearchResults] = useState<SearchHit[]>([]);
  const [searchResponseMeta, setSearchResponseMeta] = useState<Pick<
    SearchResponse,
    "scope" | "query_hash" | "index_version" | "ranking_version" | "next_cursor"
  > | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [isLoadingNextPage, setIsLoadingNextPage] = useState(false);
  const [searchError, setSearchError] = useState<MemoryOpsError | null>(null);

  // Source Deletion Modal state (OD-003)
  const [deletionTargetRepoId, setDeletionTargetRepoId] = useState<
    string | null
  >(null);
  const [deletionPreview, setDeletionPreview] =
    useState<DeletionPreviewResponse | null>(null);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [isConfirmingDeletion, setIsConfirmingDeletion] = useState(false);
  const [deletionAcknowledged, setDeletionAcknowledged] = useState(false);
  const [deletionConfirmError, setDeletionConfirmError] =
    useState<MemoryOpsError | null>(null);

  // Preserved Idempotency-Key across retries of the same deletion-confirm attempt
  const confirmIdempotencyKeyRef = useRef<string | null>(null);

  // Active Deletion Job Receipt state (T-502)
  const [activeDeletionJob, setActiveDeletionJob] =
    useState<DeletionJobStatusResponse | null>(null);
  const [isRefreshingReceipt, setIsRefreshingReceipt] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const selectedRepoObj = useMemo(
    () =>
      repositories.find(
        (r) => r.source_resource_id === selectedSourceResourceId
      ) ?? null,
    [repositories, selectedSourceResourceId]
  );

  const selectedRepoFencedOrDeleted = Boolean(
    selectedRepoObj && (selectedRepoObj.fence_active || selectedRepoObj.deleted)
  );

  const deletionTargetRepo = useMemo(
    () =>
      repositories.find((r) => r.source_resource_id === deletionTargetRepoId) ??
      null,
    [repositories, deletionTargetRepoId]
  );

  // --- Quota Load ---
  const loadQuotaStatus = useCallback(
    async (scopeRepoId?: string | null) => {
      setIsLoadingQuota(true);
      const res = await adapter.getQuotaStatus(
        scopeRepoId ? { source_resource_id: scopeRepoId } : undefined
      );
      if (!isMountedRef.current) return;
      setIsLoadingQuota(false);
      if (res.ok) {
        setQuotaStatus(res.data);
      }
    },
    [adapter]
  );

  // --- Search Execution ---
  const executeSearch = useCallback(
    async (params: {
      q: string;
      sourceResourceId: string | null;
      entityTypes: SearchEntityType[];
      cursor: string | null;
      append: boolean;
    }) => {
      const trimmed = params.q.trim();
      if (!trimmed) return;

      if (params.append) {
        setIsLoadingNextPage(true);
      } else {
        setIsSearching(true);
      }
      setSearchError(null);

      const res = await adapter.searchMemory({
        q: trimmed,
        source_resource_id: params.sourceResourceId,
        entity_types:
          params.entityTypes.length > 0 ? params.entityTypes : undefined,
        limit: 3,
        cursor: params.cursor,
      });

      if (!isMountedRef.current) return;
      setIsSearching(false);
      setIsLoadingNextPage(false);

      if (!res.ok) {
        setSearchError(res.error);
        return;
      }

      setSearchResponseMeta({
        scope: res.data.scope,
        query_hash: res.data.query_hash,
        index_version: res.data.index_version,
        ranking_version: res.data.ranking_version,
        next_cursor: res.data.next_cursor,
      });
      setSearchResults((prev) =>
        params.append ? [...prev, ...res.data.items] : res.data.items
      );
    },
    [adapter]
  );

  // --- Deletion Receipt Load ---
  const loadDeletionJobReceipt = useCallback(
    async (jobId: string) => {
      setIsRefreshingReceipt(true);
      const res = await adapter.getDeletionJobStatus(jobId);
      if (!isMountedRef.current) return;
      setIsRefreshingReceipt(false);

      if (res.ok) {
        setActiveDeletionJob(res.data);
        setRepositories((prev) =>
          prev.map((repo) => {
            if (repo.source_resource_id !== res.data.source_resource_id) {
              return repo;
            }
            return {
              ...repo,
              fence_active: res.data.fence_active,
              deleted: res.data.user_visible_state === "DELETED",
              active_deletion_job_id: res.data.deletion_job_id,
            };
          })
        );
      }
    },
    [adapter]
  );

  // --- Bootstrap on mount / scenario / repoCountMode change ---
  const bootstrapWorkspace = useCallback(
    async (nextRepos: WorkspaceSourceResourceSummary[], initialJobId: string | null) => {
      const scopeRes = resolveDefaultSearchScope(nextRepos);
      const nextScopeId = scopeRes.defaultSourceResourceId;
      setSelectedSourceResourceId(nextScopeId);
      setSearchError(null);
      setDeletionTargetRepoId(null);
      setDeletionPreview(null);
      setDeletionConfirmError(null);
      setDeletionAcknowledged(false);
      setDisconnectNotice(null);
      setActionNotice(null);
      confirmIdempotencyKeyRef.current = null;

      await Promise.all([
        loadQuotaStatus(nextScopeId),
        executeSearch({
          q: initialQuery,
          sourceResourceId: nextScopeId,
          entityTypes: [],
          cursor: null,
          append: false,
        }),
        initialJobId
          ? loadDeletionJobReceipt(initialJobId)
          : Promise.resolve(setActiveDeletionJob(null)),
      ]);
    },
    [executeSearch, initialQuery, loadDeletionJobReceipt, loadQuotaStatus]
  );

  useEffect(() => {
    const initialJobId = harness?.getInitialActiveDeletionJobId() ?? null;
    void bootstrapWorkspace(repositories, initialJobId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter]);

  // --- Search Handlers ---
  const handleSelectSourceResource = (nextId: string | null) => {
    setSelectedSourceResourceId(nextId);
    setSearchError(null);
    const target = repositories.find((r) => r.source_resource_id === nextId);
    if (!target || (!target.fence_active && !target.deleted)) {
      void executeSearch({
        q: queryText,
        sourceResourceId: nextId,
        entityTypes: selectedEntityTypes,
        cursor: null,
        append: false,
      });
    } else {
      setSearchResults([]);
      setSearchResponseMeta(null);
    }
  };

  const handleToggleEntityType = (entityType: SearchEntityType) => {
    const nextTypes = selectedEntityTypes.includes(entityType)
      ? selectedEntityTypes.filter((t) => t !== entityType)
      : [...selectedEntityTypes, entityType];
    setSelectedEntityTypes(nextTypes);
    if (!selectedRepoFencedOrDeleted && queryText.trim()) {
      void executeSearch({
        q: queryText,
        sourceResourceId: selectedSourceResourceId,
        entityTypes: nextTypes,
        cursor: null,
        append: false,
      });
    }
  };

  const handleSubmitSearch = () => {
    if (selectedRepoFencedOrDeleted) return;
    void executeSearch({
      q: queryText,
      sourceResourceId: selectedSourceResourceId,
      entityTypes: selectedEntityTypes,
      cursor: null,
      append: false,
    });
  };

  const handleLoadNextPage = () => {
    if (!searchResponseMeta?.next_cursor || selectedRepoFencedOrDeleted) return;
    void executeSearch({
      q: queryText,
      sourceResourceId: selectedSourceResourceId,
      entityTypes: selectedEntityTypes,
      cursor: searchResponseMeta.next_cursor,
      append: true,
    });
  };

  const handleRestartSearchAfterCursorError = () => {
    if (selectedRepoFencedOrDeleted) return;
    setSearchError(null);
    void executeSearch({
      q: queryText,
      sourceResourceId: selectedSourceResourceId,
      entityTypes: selectedEntityTypes,
      cursor: null,
      append: false,
    });
  };

  // --- OD-003 Source Deletion Preview & Confirm Handlers ---
  const fetchDeletionPreview = async (sourceResourceId: string) => {
    setIsLoadingPreview(true);
    setDeletionConfirmError(null);
    setDeletionAcknowledged(false);
    // Fresh preview -> fresh Idempotency-Key for the upcoming confirm attempt
    confirmIdempotencyKeyRef.current = generateUuidV4();

    const res = await adapter.previewSourceDeletion(sourceResourceId);
    if (!isMountedRef.current) return;
    setIsLoadingPreview(false);

    if (!res.ok) {
      setDeletionConfirmError(res.error);
      return;
    }

    setDeletionPreview(res.data);
  };

  const handleOpenDeletionModal = (sourceResourceId: string) => {
    setDeletionTargetRepoId(sourceResourceId);
    setDeletionPreview(null);
    setDeletionConfirmError(null);
    setDeletionAcknowledged(false);
    void fetchDeletionPreview(sourceResourceId);
  };

  const handleConfirmSourceDeletion = async () => {
    if (!deletionPreview || !deletionAcknowledged || isConfirmingDeletion) {
      return;
    }
    setIsConfirmingDeletion(true);
    setDeletionConfirmError(null);

    if (!confirmIdempotencyKeyRef.current) {
      confirmIdempotencyKeyRef.current = generateUuidV4();
    }

    const res = await adapter.confirmSourceDeletion({
      source_resource_id: deletionPreview.source_resource_id,
      preview_token: deletionPreview.preview_token,
      expected_resource_version: deletionPreview.resource_version,
      expected_impact_scope_version: deletionPreview.impact_scope_version,
      acknowledge_irreversible_purge: true,
      idempotency_key: confirmIdempotencyKeyRef.current,
    });
    if (!isMountedRef.current) return;
    setIsConfirmingDeletion(false);

    if (!res.ok) {
      // On 409 DELETION_PREVIEW_EXPIRED / DELETION_PREVIEW_SCOPE_CHANGED: block deletion safely!
      setDeletionConfirmError(res.error);
      return;
    }

    // Immediate zero-window UI fence activation on 202!
    const { deletion_job_id, source_resource_id } = res.data;
    setRepositories((prev) =>
      prev.map((repo) =>
        repo.source_resource_id === source_resource_id
          ? {
              ...repo,
              fence_active: true,
              active_deletion_job_id: deletion_job_id,
            }
          : repo
      )
    );

    setDeletionTargetRepoId(null);
    setDeletionPreview(null);
    setDeletionAcknowledged(false);
    confirmIdempotencyKeyRef.current = null;
    setActionNotice(
      `저장소(${source_resource_id}) 영구 삭제가 202 Accepted(${deletion_job_id})로 접수되어 삭제 펜스(fence_active=true)가 즉시 활성화되었습니다. 검색·Export·고비용 작업이 즉시 차단되었습니다.`
    );

    // Immediately remove fenced repo hits from displayed search results
    setSearchResults((prev) =>
      prev.filter((hit) => hit.source_resource_id !== source_resource_id)
    );

    await loadDeletionJobReceipt(deletion_job_id);
  };

  // --- Scenario & Harness Handlers ---
  const handleScenarioChange = (nextScenario: MemoryOpsScenarioId) => {
    if (!harness) return;
    harness.setScenario(nextScenario);
    const nextRepos = harness.getRepositories();
    setScenarioId(nextScenario);
    setRepositories(nextRepos);
    setQueryText(initialQuery);
    setSelectedEntityTypes([]);
    void bootstrapWorkspace(nextRepos, harness.getInitialActiveDeletionJobId());
  };

  const handleRepoCountModeChange = (mode: "single" | "multi") => {
    if (!harness) return;
    harness.setRepositoryCountMode(mode);
    const nextRepos = harness.getRepositories();
    setRepoCountMode(mode);
    setRepositories(nextRepos);
    setQueryText(initialQuery);
    setSelectedEntityTypes([]);
    void bootstrapWorkspace(nextRepos, harness.getInitialActiveDeletionJobId());
  };

  const handleReset = () => {
    if (!harness) return;
    harness.reset();
    const nextRepos = harness.getRepositories();
    setRepositories(nextRepos);
    setQueryText(initialQuery);
    setSelectedEntityTypes([]);
    void bootstrapWorkspace(nextRepos, harness.getInitialActiveDeletionJobId());
  };

  const handleSimulateAdvanceDeletion = () => {
    if (!harness || !activeDeletionJob) return;
    harness.advanceDeletionJob(activeDeletionJob.deletion_job_id);
    void loadDeletionJobReceipt(activeDeletionJob.deletion_job_id);
  };

  const scenarioNotice = harness?.getScenarioNotice() ?? null;

  return (
    <div className="page-shell" data-testid="memory-ops-root">
      <a href="#memory-ops-main" className="skip-link">
        본문으로 건너뛰기
      </a>

      <header className="page-header">
        <div className="page-header__inner page-header__inner--wide">
          <div className="page-header__nav-row">
            <div className="brand-row">
              <span className="brand-mark" aria-hidden="true">
                CM
              </span>
              <div>
                <p className="brand-kicker">
                  Career Memory · M5 Memory Search · Source Deletion · Quota Guard
                </p>
                <h1 className="page-title">
                  메모리 검색 · 저장소 삭제 · 한도 관리 (/search)
                </h1>
              </div>
            </div>

            <nav className="action-row" aria-label="Career Memory 화면 이동">
              <a
                href="/"
                className="btn btn--ghost btn--sm"
                data-testid="nav-to-import-link"
              >
                가져오기 화면 (/)
              </a>
              <a
                href="/event-review"
                className="btn btn--secondary btn--sm"
                data-testid="nav-to-event-review-link"
              >
                Event Inbox (/event-review)
              </a>
              <a
                href="/claim-review"
                className="btn btn--secondary btn--sm"
                data-testid="nav-to-claim-review-link"
              >
                Claim Review (/claim-review)
              </a>
            </nav>
          </div>
          <p className="page-lead">
            워크스페이스 비원문 메모리 검색(OD-008), 저장소별 영향 범위 미리보기
            및 영구 삭제 영수증(OD-003), 무료 한도·일시정지 보호 상태(T-503 /
            INF-06)를 한 곳에서 관리합니다.
          </p>
        </div>
      </header>

      <main
        id="memory-ops-main"
        className="main-content main-content--wide"
        tabIndex={-1}
      >
        {harness && (
          <MemoryOpsScenarioBar
            scenarioId={scenarioId}
            repoCountMode={repoCountMode}
            disabled={isConfirmingDeletion || isLoadingPreview}
            onScenarioChange={handleScenarioChange}
            onRepoCountModeChange={handleRepoCountModeChange}
            onReset={handleReset}
          />
        )}

        {/* Quota / Budget / Pause Status Banner (T-503 / INF-06) */}
        <QuotaBudgetBanner
          quotaStatus={quotaStatus}
          isLoading={isLoadingQuota}
          onRefreshQuota={() => void loadQuotaStatus(selectedSourceResourceId)}
          disconnectNotice={disconnectNotice}
          onTriggerDisconnectNotice={() =>
            setDisconnectNotice(
              "안전 작업 확인: GitHub 계정 연결 해제(POST /v1/source-accounts/{id}/disconnect)는 95% 한도 차단·일시정지 상태에서도 항상 허용됩니다. 연결 해제 시 기존 저장소 데이터는 자동 삭제되지 않으며 별도 OD-003 삭제로 제어합니다."
            )
          }
        />

        {actionNotice && (
          <div
            className="inline-alert inline-alert--info"
            role="status"
            aria-live="polite"
            data-testid="memory-ops-action-notice"
          >
            {actionNotice}
          </div>
        )}

        {/* Workspace Memory Search Panel (OD-008 / T-501) */}
        <MemorySearchPanel
          repositories={repositories}
          selectedSourceResourceId={selectedSourceResourceId}
          queryText={queryText}
          selectedEntityTypes={selectedEntityTypes}
          results={searchResults}
          lastResponseMeta={searchResponseMeta}
          isSearching={isSearching}
          isLoadingNextPage={isLoadingNextPage}
          searchError={searchError}
          scenarioNotice={scenarioNotice}
          selectedRepoFencedOrDeleted={selectedRepoFencedOrDeleted}
          onSelectSourceResourceId={handleSelectSourceResource}
          onChangeQueryText={setQueryText}
          onToggleEntityType={handleToggleEntityType}
          onSubmitSearch={handleSubmitSearch}
          onLoadNextPage={handleLoadNextPage}
          onRestartSearchAfterCursorError={handleRestartSearchAfterCursorError}
        />

        {/* Source Deletion Management Section (OD-003 / T-502) */}
        <section
          className="card"
          aria-labelledby="source-deletion-section-heading"
          data-testid="source-deletion-management-section"
        >
          <div className="section-header">
            <span className="step-kicker">
              OD-003 · T-502 · 저장소 단위 영구 삭제 관리 (안전 작업 항상 허용)
            </span>
            <h2 id="source-deletion-section-heading" className="section-title">
              저장소 데이터 삭제 관리 (Source Deletion)
            </h2>
            <p className="section-subtitle">
              저장소 삭제는 95% 한도 차단·일시정지 상태에서도 항상 허용됩니다.
              확인 시 삭제 펜스가 즉시 활성화되어 해당 저장소의
              검색·Export·고비용 작업이 차단되며, 다른 독립 저장소와 공유 GitHub
              계정 연결은 유지됩니다.
            </p>
          </div>

          <div className="repo-ops-grid" data-testid="repo-ops-list">
            {repositories.map((repo) => {
              const perms = evaluateOperationPermissions({
                quotaStatus,
                selectedRepoFenced: repo.fence_active,
                selectedRepoDeleted: repo.deleted,
              });

              return (
                <div
                  key={repo.source_resource_id}
                  className="subcard repo-ops-card"
                  data-testid={`repo-ops-card-${repo.source_resource_id}`}
                  data-fence-active={String(repo.fence_active)}
                  data-deleted={String(repo.deleted)}
                >
                  <div className="subcard__header">
                    <div>
                      <h3 className="subcard__title">
                        {repo.repository_full_name}
                      </h3>
                      <p className="field-help mono">
                        {repo.source_resource_id} · {repo.provider} ·{" "}
                        {repo.visibility}
                      </p>
                    </div>
                    <span
                      className={`badge ${
                        repo.deleted
                          ? "badge--success"
                          : repo.fence_active
                            ? "badge--warning"
                            : "badge--info"
                      }`}
                      data-testid={`repo-status-badge-${repo.source_resource_id}`}
                    >
                      {repo.deleted
                        ? "영구 삭제 완료 (DELETED)"
                        : repo.fence_active
                          ? "삭제 진행 중 (DELETION_PENDING)"
                          : "활성 (ACTIVE)"}
                    </span>
                  </div>

                  {perms.blockReason && (
                    <p
                      className="field-help"
                      data-testid={`repo-block-reason-${repo.source_resource_id}`}
                    >
                      {perms.blockReason}
                    </p>
                  )}

                  <div className="action-row">
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      data-testid={`repo-high-cost-action-${repo.source_resource_id}`}
                      disabled={!perms.canRunHighCostOnSelectedRepo}
                      onClick={() =>
                        setActionNotice(
                          `${repo.repository_full_name} 저장소의 Export/재동기화 화면으로 이동 가능합니다.`
                        )
                      }
                    >
                      Export / 고비용 작업
                      {!perms.canRunHighCostOnSelectedRepo ? " (차단됨)" : ""}
                    </button>

                    <button
                      type="button"
                      className="btn btn--danger btn--sm"
                      data-testid={`open-source-deletion-${repo.source_resource_id}`}
                      disabled={!perms.canDeleteSelectedRepo}
                      onClick={() =>
                        handleOpenDeletionModal(repo.source_resource_id)
                      }
                    >
                      {repo.deleted
                        ? "삭제 완료됨 (DELETED)"
                        : repo.fence_active
                          ? "삭제 진행 중 (펜스 활성)"
                          : "저장소 데이터 삭제 관리 (Source Deletion)"}
                    </button>

                    {repo.active_deletion_job_id && (
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        data-testid={`view-deletion-receipt-${repo.source_resource_id}`}
                        onClick={() =>
                          void loadDeletionJobReceipt(
                            repo.active_deletion_job_id!
                          )
                        }
                      >
                        삭제 영수증 보기 ({repo.active_deletion_job_id})
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* Live Deletion Job Receipt Panel (T-502 / DELETE-05..06) */}
        {activeDeletionJob && (
          <DeletionJobReceiptPanel
            job={activeDeletionJob}
            repositoryName={
              repositories.find(
                (r) =>
                  r.source_resource_id === activeDeletionJob.source_resource_id
              )?.repository_full_name
            }
            isRefreshing={isRefreshingReceipt}
            canSimulateAdvance={Boolean(harness)}
            onRefreshReceipt={() =>
              void loadDeletionJobReceipt(activeDeletionJob.deletion_job_id)
            }
            onSimulateAdvance={handleSimulateAdvanceDeletion}
          />
        )}

        {/* OD-003 Source Deletion Preview & Confirm Modal */}
        {deletionTargetRepo && (
          <SourceDeletionModal
            repository={deletionTargetRepo}
            preview={deletionPreview}
            isLoadingPreview={isLoadingPreview}
            isConfirming={isConfirmingDeletion}
            acknowledged={deletionAcknowledged}
            confirmError={deletionConfirmError}
            onChangeAcknowledged={setDeletionAcknowledged}
            onRefreshPreview={() =>
              void fetchDeletionPreview(deletionTargetRepo.source_resource_id)
            }
            onConfirmDeletion={() => void handleConfirmSourceDeletion()}
            onClose={() => {
              setDeletionTargetRepoId(null);
              setDeletionPreview(null);
              setDeletionConfirmError(null);
              setDeletionAcknowledged(false);
            }}
          />
        )}
      </main>
    </div>
  );
}
