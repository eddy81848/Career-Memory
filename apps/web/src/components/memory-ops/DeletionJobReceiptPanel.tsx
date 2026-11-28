"use client";

import React from "react";
import type { DeletionJobStatusResponse } from "@/domain/memory-ops-models";
import {
  formatDeletionComponentLabel,
  formatDeletionComponentStatusBadge,
  formatDeletionUserVisibleStateBadge,
  summarizeDeletionReceipt,
} from "@/domain/memory-ops-presenter";

interface DeletionJobReceiptPanelProps {
  job: DeletionJobStatusResponse;
  repositoryName?: string;
  isRefreshing: boolean;
  canSimulateAdvance?: boolean;
  onRefreshReceipt: () => void;
  onSimulateAdvance?: () => void;
}

export function DeletionJobReceiptPanel({
  job,
  repositoryName,
  isRefreshing,
  canSimulateAdvance = false,
  onRefreshReceipt,
  onSimulateAdvance,
}: DeletionJobReceiptPanelProps) {
  const stateBadge = formatDeletionUserVisibleStateBadge(
    job.user_visible_state,
    job.status
  );
  const summary = summarizeDeletionReceipt(job);

  const bannerToneClass = summary.isFullyDeleted
    ? "status-banner--success"
    : job.user_visible_state === "DELETION_FAILED"
      ? "status-banner--danger"
      : "status-banner--warning";

  return (
    <section
      className="card"
      aria-labelledby="deletion-receipt-heading"
      data-testid="deletion-job-receipt-panel"
      data-user-visible-state={job.user_visible_state}
      data-internal-status={job.status}
      data-fence-active={String(job.fence_active)}
    >
      <div className="event-inbox-topbar">
        <div>
          <span className="step-kicker">
            OD-003 · T-502 · 삭제 작업 영수증 (GET /v1/deletion-jobs/
            {job.deletion_job_id})
          </span>
          <h2 id="deletion-receipt-heading" className="section-title">
            저장소 삭제 진행 및 구성요소 영수증{" "}
            {repositoryName ? `(${repositoryName})` : ""}
          </h2>
          <p className="section-subtitle mono">
            deletion_job_id={job.deletion_job_id} · source_resource_id=
            {job.source_resource_id} · fence_active={String(job.fence_active)}
          </p>
        </div>

        <div className="action-row">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="refresh-deletion-receipt-button"
            disabled={isRefreshing}
            onClick={onRefreshReceipt}
          >
            {isRefreshing
              ? "영수증 조회 중..."
              : "삭제 영수증 새로고침 (Refresh Receipt)"}
          </button>
          {canSimulateAdvance && !summary.isFullyDeleted && onSimulateAdvance && (
            <button
              type="button"
              className="btn btn--primary btn--sm"
              data-testid="simulate-deletion-complete-button"
              disabled={isRefreshing}
              onClick={onSimulateAdvance}
            >
              미완료 구성요소 정리 완료 시뮬레이션
            </button>
          )}
        </div>
      </div>

      {/* Top User-Visible State Summary */}
      <div
        className={`status-banner ${bannerToneClass}`}
        role={summary.isFullyDeleted ? "status" : "alert"}
        data-testid="deletion-receipt-state-banner"
      >
        <div className="status-banner__top">
          <div className="status-banner__badges">
            <span
              className={`badge ${stateBadge.badgeClass}`}
              data-testid="deletion-user-visible-badge"
            >
              {stateBadge.label}
            </span>
            <span className="badge badge--neutral mono">
              완료 구성요소 {summary.succeededCount}/{summary.totalComponents} ·
              총 파기 {summary.totalPurgedCount}건
            </span>
            {job.fence_active && (
              <span className="badge badge--warning mono">
                삭제 펜스 활성 (fence_active=true)
              </span>
            )}
          </div>
        </div>

        {summary.isFullyDeleted ? (
          <>
            <p className="status-banner__headline">
              5개 삭제 구성요소 모두 정리가 완료되어 저장소가 DELETED 상태로
              전환되었습니다
            </p>
            <p className="status-banner__detail">
              완료 시각: <span className="mono">{job.completed_at}</span> · 다른
              독립 저장소와 공유 GitHub 계정 연결은 그대로 유지됩니다.
            </p>
          </>
        ) : (
          <>
            <p className="status-banner__headline">
              미완료 구성요소 재시도 안내 — 모든 5개 구성요소가 완료될 때까지
              DELETED로 표시되지 않으며 삭제 펜스가 유지됩니다
            </p>
            <p
              className="status-banner__detail"
              data-testid="deletion-partial-retry-guidance"
            >
              현재 미완료 구성요소:{" "}
              <strong className="mono">
                {summary.incompleteComponents.join(", ") || "없음"}
              </strong>
              . 서버 워커가 멱등 재시도를 수행하는 동안 해당 저장소의
              검색·Export·고비용 작업은 계속 차단됩니다.
            </p>
          </>
        )}
      </div>

      {/* 5-Component Receipt List */}
      <div
        className="deletion-components-list"
        data-testid="deletion-components-list"
      >
        {job.components.map((comp) => {
          const compBadge = formatDeletionComponentStatusBadge(comp.status);
          return (
            <div
              key={comp.component}
              className="subcard deletion-component-row"
              data-testid={`deletion-component-${comp.component}`}
              data-component-status={comp.status}
            >
              <div className="subcard__header">
                <div>
                  <strong className="deletion-component-row__name">
                    {formatDeletionComponentLabel(comp.component)}
                  </strong>
                  <p className="field-help mono">
                    attempt={comp.attempt} · purged_count={comp.purged_count} ·
                    updated_at={comp.updated_at}
                    {comp.completed_at
                      ? ` · completed_at=${comp.completed_at}`
                      : ""}
                  </p>
                </div>
                <span className={`badge ${compBadge.badgeClass}`}>
                  {compBadge.label}
                </span>
              </div>
              {comp.last_error_code && (
                <p
                  className="inline-alert inline-alert--danger mono"
                  data-testid={`deletion-component-error-${comp.component}`}
                >
                  last_error_code: {comp.last_error_code} (백그라운드 재시도 대상)
                </p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
