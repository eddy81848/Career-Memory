"use client";

import React from "react";
import type {
  MemoryOpsError,
  SearchEntityType,
  SearchHit,
  SearchResponse,
  WorkspaceSourceResourceSummary,
} from "@/domain/memory-ops-models";
import {
  buildSearchHitDrilldownHref,
  formatSearchEntityBadge,
  formatSearchScore,
  resolveDefaultSearchScope,
  SEARCH_ENTITY_TYPE_ORDER,
} from "@/domain/memory-ops-presenter";

interface MemorySearchPanelProps {
  repositories: WorkspaceSourceResourceSummary[];
  selectedSourceResourceId: string | null;
  queryText: string;
  selectedEntityTypes: SearchEntityType[];
  results: SearchHit[];
  lastResponseMeta: Pick<
    SearchResponse,
    "scope" | "query_hash" | "index_version" | "ranking_version" | "next_cursor"
  > | null;
  isSearching: boolean;
  isLoadingNextPage: boolean;
  searchError: MemoryOpsError | null;
  scenarioNotice: string | null;
  selectedRepoFencedOrDeleted: boolean;
  onSelectSourceResourceId: (sourceResourceId: string | null) => void;
  onChangeQueryText: (value: string) => void;
  onToggleEntityType: (entityType: SearchEntityType) => void;
  onSubmitSearch: () => void;
  onLoadNextPage: () => void;
  onRestartSearchAfterCursorError: () => void;
}

export function MemorySearchPanel({
  repositories,
  selectedSourceResourceId,
  queryText,
  selectedEntityTypes,
  results,
  lastResponseMeta,
  isSearching,
  isLoadingNextPage,
  searchError,
  scenarioNotice,
  selectedRepoFencedOrDeleted,
  onSelectSourceResourceId,
  onChangeQueryText,
  onToggleEntityType,
  onSubmitSearch,
  onLoadNextPage,
  onRestartSearchAfterCursorError,
}: MemorySearchPanelProps) {
  const scopeResolution = resolveDefaultSearchScope(repositories);
  const selectedRepoObj = repositories.find(
    (r) => r.source_resource_id === selectedSourceResourceId
  );

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmitSearch();
  };

  return (
    <section
      className="card"
      aria-labelledby="memory-search-heading"
      data-testid="memory-search-panel"
    >
      <div className="section-header">
        <span className="step-kicker">
          OD-008 · T-501/A · WORKSPACE 비원문 메모리 검색 (ZERO project_id)
        </span>
        <h2 id="memory-search-heading" className="section-title">
          워크스페이스 메모리 검색 (Work Event · Fact · Claim)
        </h2>
        <p className="section-subtitle">
          인증된 워크스페이스와 선택적 저장소(
          <code className="mono">source_resource_id</code>) 범위로만 비원문
          파생 엔티티를 검색합니다. 원문 본문·인용은 색인되지 않으며{" "}
          <code className="mono">project_id</code> 범위는 사용하지 않습니다.
        </p>
      </div>

      {scenarioNotice && (
        <div
          className="inline-alert inline-alert--info"
          role="status"
          data-testid="search-scenario-notice"
        >
          {scenarioNotice}
        </div>
      )}

      <form
        onSubmit={handleFormSubmit}
        className="memory-search-form"
        data-testid="memory-search-form"
      >
        <div className="memory-search-controls">
          {/* OD-008 Repository Scope Selector */}
          <div className="form-field memory-search-scope">
            <label
              htmlFor="search-source-resource-select"
              className="field-label"
            >
              저장소 범위 (source_resource_id · OD-008)
            </label>
            {scopeResolution.autoSelectedSingleRepo &&
            scopeResolution.activeRepositories.length === 1 ? (
              <div
                className="single-repo-scope-box"
                data-testid="single-repo-auto-selected-scope"
              >
                <span className="badge badge--info">
                  단일 저장소 자동 선택
                </span>
                <span className="mono">
                  {scopeResolution.activeRepositories[0].repository_full_name} (
                  {scopeResolution.activeRepositories[0].source_resource_id})
                </span>
              </div>
            ) : (
              <select
                id="search-source-resource-select"
                name="source_resource_id"
                className="select-input"
                data-testid="search-source-resource-select"
                value={selectedSourceResourceId ?? ""}
                onChange={(e) =>
                  onSelectSourceResourceId(e.target.value || null)
                }
              >
                <option value="">
                  전체 활성 저장소 (워크스페이스 전체 범위)
                </option>
                {repositories.map((repo) => (
                  <option
                    key={repo.source_resource_id}
                    value={repo.source_resource_id}
                  >
                    {repo.repository_full_name} ({repo.source_resource_id})
                    {repo.deleted
                      ? " [삭제됨 · DELETED]"
                      : repo.fence_active
                        ? " [삭제 펜스 활성 · DELETION_PENDING]"
                        : ""}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* Search Query Input */}
          <div className="form-field memory-search-query">
            <label htmlFor="memory-search-input" className="field-label">
              검색어 (q)
            </label>
            <div className="memory-search-input-row">
              <input
                id="memory-search-input"
                name="q"
                type="search"
                className="text-input"
                data-testid="memory-search-input"
                placeholder="예: webhook, HMAC, latency, OpenTelemetry"
                value={queryText}
                onChange={(e) => onChangeQueryText(e.target.value)}
              />
              <button
                type="submit"
                className="btn btn--primary btn--sm"
                data-testid="memory-search-submit-button"
                disabled={
                  isSearching ||
                  !queryText.trim() ||
                  selectedRepoFencedOrDeleted
                }
              >
                {isSearching ? "검색 중..." : "검색 실행 (GET /v1/search)"}
              </button>
            </div>
          </div>
        </div>

        {/* Entity Type Filter Toggles */}
        <div
          className="filter-tab-row"
          role="group"
          aria-label="엔티티 유형 필터 (entity_types)"
          data-testid="search-entity-type-filters"
        >
          <span className="field-label">유형 필터:</span>
          {SEARCH_ENTITY_TYPE_ORDER.map((entityType) => {
            const active = selectedEntityTypes.includes(entityType);
            return (
              <button
                key={entityType}
                type="button"
                aria-pressed={active}
                data-testid={`entity-filter-${entityType}`}
                className={`btn btn--sm ${
                  active ? "btn--primary" : "btn--ghost"
                }`}
                onClick={() => onToggleEntityType(entityType)}
              >
                {entityType}
              </button>
            );
          })}
          <span className="field-help">
            {selectedEntityTypes.length === 0
              ? "전체 유형 (WORK_EVENT · FACT · CLAIM) 검색"
              : `선택됨: ${selectedEntityTypes.join(", ")}`}
          </span>
        </div>
      </form>

      {/* Selected Repo Deletion Fence Disabled Banner */}
      {selectedRepoFencedOrDeleted && selectedRepoObj && (
        <div
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="search-repo-fenced-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning mono">
              {selectedRepoObj.deleted
                ? "DELETED · 검색 비활성화"
                : "DELETION_PENDING · fence_active=true"}
            </span>
          </div>
          <p className="status-banner__headline">
            선택한 저장소({selectedRepoObj.repository_full_name})에 삭제 펜스가
            활성화되어 검색·Export·고비용 작업이 즉시 차단되었습니다
          </p>
          <p className="status-banner__detail">
            삭제 진행 중이거나 완료된 저장소의 엔티티는 검색 결과에서 즉시
            제외됩니다. 다른 활성 저장소를 선택하거나 전체 활성 저장소 범위로
            전환해 검색할 수 있습니다.
          </p>
        </div>
      )}

      {/* 400 INVALID_SEARCH_CURSOR Non-Destructive Recovery Banner */}
      {searchError && searchError.kind === "invalid_search_cursor" && (
        <div
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="search-cursor-recovery-banner"
          data-error-code={searchError.code}
        >
          <div className="status-banner__top">
            <span className="badge badge--warning mono">
              400 INVALID_SEARCH_CURSOR
            </span>
          </div>
          <p className="status-banner__headline">
            색인 또는 정렬 버전이 갱신되었습니다 — 처음부터 다시 검색
          </p>
          <p className="status-banner__detail">
            기존 페이지네이션 커서가 최신 색인/정렬 버전과 일치하지 않습니다(
            {searchError.message}). 기존 결과는 유지되며, 아래 버튼으로 커서를
            초기화하고 첫 페이지부터 안전하게 다시 검색할 수 있습니다.
          </p>
          <div className="action-row">
            <button
              type="button"
              className="btn btn--primary btn--sm"
              data-testid="restart-search-button"
              disabled={isSearching}
              onClick={onRestartSearchAfterCursorError}
            >
              처음부터 다시 검색 (Restart Search)
            </button>
          </div>
        </div>
      )}

      {/* Other Search Errors */}
      {searchError && searchError.kind !== "invalid_search_cursor" && (
        <div
          className="status-banner status-banner--danger"
          role="alert"
          data-testid="search-error-banner"
          data-error-code={searchError.code}
        >
          <div className="status-banner__top">
            <span className="badge badge--danger mono">{searchError.code}</span>
          </div>
          <p className="status-banner__headline">{searchError.message}</p>
        </div>
      )}

      {/* Search Response Metadata Strip */}
      {lastResponseMeta && (
        <div
          className="search-meta-strip mono"
          data-testid="search-response-meta"
        >
          <span>
            scope.workspace_id={lastResponseMeta.scope.workspace_id}
          </span>
          <span>
            scope.source_resource_id=
            {lastResponseMeta.scope.source_resource_id ?? "ALL_ACTIVE"}
          </span>
          <span data-testid="search-index-version">
            index={lastResponseMeta.index_version}
          </span>
          <span data-testid="search-ranking-version">
            ranking={lastResponseMeta.ranking_version}
          </span>
          <span>query_hash={lastResponseMeta.query_hash.slice(0, 19)}…</span>
        </div>
      )}

      {/* Result Cards */}
      {isSearching && results.length === 0 ? (
        <p className="field-help">검색 결과를 불러오는 중입니다...</p>
      ) : results.length === 0 ? (
        <div className="empty-state" data-testid="search-empty-state">
          <p className="empty-state__title">
            조건에 일치하는 비원문 검색 결과가 없습니다
          </p>
          <p className="empty-state__body">
            다른 키워드를 입력하거나 유형 필터·저장소 범위를 넓혀 다시
            검색하세요. 삭제 펜스가 활성화된 저장소 항목은 자동으로 제외됩니다.
          </p>
        </div>
      ) : (
        <div className="search-results-container">
          <ul className="search-results-list" data-testid="search-results-list">
            {results.map((hit) => {
              const badge = formatSearchEntityBadge(hit.entity_type);
              const drilldown = buildSearchHitDrilldownHref(hit);
              return (
                <li
                  key={`${hit.entity_type}:${hit.entity_id}`}
                  className="subcard search-hit-card"
                  data-testid={`search-hit-${hit.entity_id}`}
                  data-entity-type={hit.entity_type}
                >
                  <div className="subcard__header">
                    <div className="status-banner__badges">
                      <span className={`badge ${badge.badgeClass}`}>
                        {badge.label}
                      </span>
                      <span className="badge badge--neutral mono">
                        score {formatSearchScore(hit.score)}
                      </span>
                      {hit.source_resource_id && (
                        <span className="badge badge--neutral mono">
                          {hit.source_resource_id}
                        </span>
                      )}
                    </div>
                    <span className="mono run-id-chip">{hit.entity_id}</span>
                  </div>

                  <p className="search-hit-card__summary">
                    {hit.title_or_summary}
                  </p>

                  <div className="action-row">
                    <a
                      href={drilldown.href}
                      className="btn btn--secondary btn--sm"
                      data-testid={`search-hit-link-${hit.entity_id}`}
                    >
                      {drilldown.label}
                    </a>
                  </div>
                </li>
              );
            })}
          </ul>

          {lastResponseMeta?.next_cursor && (
            <div className="submit-bar">
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                data-testid="load-next-search-page-button"
                disabled={isLoadingNextPage || selectedRepoFencedOrDeleted}
                onClick={onLoadNextPage}
              >
                {isLoadingNextPage
                  ? "다음 페이지 불러오는 중..."
                  : "다음 결과 더 보기 (Load Next Page)"}
              </button>
              <span className="field-help mono">
                opaque cursor bound to {lastResponseMeta.index_version} /{" "}
                {lastResponseMeta.ranking_version}
              </span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
