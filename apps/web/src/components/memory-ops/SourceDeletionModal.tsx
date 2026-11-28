"use client";

import React from "react";
import type {
  DeletionPreviewResponse,
  MemoryOpsError,
  WorkspaceSourceResourceSummary,
} from "@/domain/memory-ops-models";
import {
  buildDeletionImpactRows,
  sumDeletionImpactCounts,
} from "@/domain/memory-ops-presenter";

interface SourceDeletionModalProps {
  repository: WorkspaceSourceResourceSummary;
  preview: DeletionPreviewResponse | null;
  isLoadingPreview: boolean;
  isConfirming: boolean;
  acknowledged: boolean;
  confirmError: MemoryOpsError | null;
  onChangeAcknowledged: (checked: boolean) => void;
  onRefreshPreview: () => void;
  onConfirmDeletion: () => void;
  onClose: () => void;
}

export function SourceDeletionModal({
  repository,
  preview,
  isLoadingPreview,
  isConfirming,
  acknowledged,
  confirmError,
  onChangeAcknowledged,
  onRefreshPreview,
  onConfirmDeletion,
  onClose,
}: SourceDeletionModalProps) {
  const isStaleOrExpiredPreview =
    confirmError !== null &&
    (confirmError.kind === "deletion_preview_expired" ||
      confirmError.kind === "deletion_preview_scope_changed");

  const impactRows = preview
    ? buildDeletionImpactRows(preview.impact_counts)
    : [];
  const totalAffected = preview
    ? sumDeletionImpactCounts(preview.impact_counts)
    : 0;

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-deletion-modal-title"
      data-testid="source-deletion-modal"
    >
      <div className="modal-card">
        <div className="modal-card__header">
          <div>
            <span className="step-kicker">
              OD-003 · 저장소 데이터 영구 삭제 미리보기 및 확인
            </span>
            <h2 id="source-deletion-modal-title" className="section-title">
              저장소 데이터 삭제 관리 (Source Deletion) —{" "}
              {repository.repository_full_name}
            </h2>
            <p className="section-subtitle mono">
              source_resource_id={repository.source_resource_id}
            </p>
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            data-testid="close-deletion-modal-button"
            disabled={isConfirming}
            onClick={onClose}
          >
            닫기
          </button>
        </div>

        {/* Loading state */}
        {isLoadingPreview && !preview && (
          <p className="field-help" data-testid="deletion-preview-loading">
            삭제 영향 범위(10개 항목) 및 버전 바인딩 토큰을 조회하고 있습니다...
          </p>
        )}

        {/* 409 DELETION_PREVIEW_EXPIRED / DELETION_PREVIEW_SCOPE_CHANGED Recovery Banner */}
        {isStaleOrExpiredPreview && confirmError && (
          <div
            className="status-banner status-banner--warning"
            role="alert"
            data-testid="deletion-preview-conflict-banner"
            data-error-code={confirmError.code}
          >
            <div className="status-banner__top">
              <span className="badge badge--warning mono">
                409 {confirmError.code}
              </span>
            </div>
            <p className="status-banner__headline">
              {confirmError.kind === "deletion_preview_expired"
                ? "삭제 미리보기 토큰이 만료되어 영구 삭제가 차단되었습니다"
                : "미리보기 이후 저장소 또는 영향 범위가 변경되어 영구 삭제가 차단되었습니다"}
            </p>
            <p className="status-banner__detail">{confirmError.message}</p>
            <div className="action-row">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                data-testid="refresh-deletion-preview-button"
                disabled={isLoadingPreview}
                onClick={onRefreshPreview}
              >
                {isLoadingPreview
                  ? "영향 범위 다시 조회 중..."
                  : "영향 범위 다시 조회 (Refresh Preview)"}
              </button>
            </div>
          </div>
        )}

        {/* Generic error banner */}
        {confirmError && !isStaleOrExpiredPreview && (
          <div
            className="status-banner status-banner--danger"
            role="alert"
            data-testid="deletion-modal-error-banner"
            data-error-code={confirmError.code}
          >
            <div className="status-banner__top">
              <span className="badge badge--danger mono">
                {confirmError.code}
              </span>
            </div>
            <p className="status-banner__headline">{confirmError.message}</p>
          </div>
        )}

        {preview && (
          <>
            {/* Version metadata */}
            <dl
              className="plan-meta-grid"
              data-testid="deletion-preview-version-meta"
            >
              <div>
                <dt>resource_version</dt>
                <dd className="mono" data-testid="preview-resource-version">
                  {preview.resource_version}
                </dd>
              </div>
              <div>
                <dt>impact_scope_version</dt>
                <dd className="mono" data-testid="preview-impact-scope-version">
                  {preview.impact_scope_version}
                </dd>
              </div>
              <div>
                <dt>미리보기 만료 시각 (expires_at)</dt>
                <dd className="mono">{preview.expires_at}</dd>
              </div>
            </dl>

            {/* All 10 Impact Counts (highlighting user-edited/approved Claims & server Exports) */}
            <section
              className="subcard"
              aria-label="영구 삭제 영향 범위 10개 항목"
              data-testid="deletion-impact-counts-section"
            >
              <div className="subcard__header">
                <h3 className="subcard__title">
                  영구 삭제 대상 항목 (총 {totalAffected}건 · 10개 카테고리)
                </h3>
                <span className="badge badge--danger">복구 불가 영구 파기</span>
              </div>
              <p className="subcard__body">
                사용자가 직접 <strong>편집한 Claim(claims_edited)</strong>,{" "}
                <strong>최종 승인한 Claim(claims_approved)</strong>,{" "}
                <strong>과거 버전 이력</strong> 및{" "}
                <strong>서버 저장 Export(server_exports)</strong>까지 모두
                영구 파기됩니다.
              </p>

              <div className="deletion-impact-grid">
                {impactRows.map((row) => (
                  <div
                    key={row.key}
                    className={`deletion-impact-cell ${
                      row.highRisk ? "deletion-impact-cell--highlight" : ""
                    }`}
                    data-testid={`impact-count-${row.key}`}
                  >
                    <div className="deletion-impact-cell__top">
                      <span className="deletion-impact-cell__label">
                        {row.label}
                      </span>
                      <strong className="deletion-impact-cell__value mono">
                        {row.count}건
                      </strong>
                    </div>
                    <p className="field-help">{row.description}</p>
                  </div>
                ))}
              </div>
            </section>

            {/* Non-Recoverable Scopes Notice */}
            <div
              className="detail-box detail-box--warning"
              data-testid="deletion-non-recoverable-notice"
            >
              <h3 className="detail-box__title">
                회수 불가 범위 안내 (Non-Recoverable Scopes)
              </h3>
              <p className="detail-box__body">
                {preview.non_recoverable_scopes.notice}
              </p>
              <ul className="bullet-list mono">
                <li>
                  already_downloaded_exports=
                  {String(
                    preview.non_recoverable_scopes.already_downloaded_exports
                  )}{" "}
                  (이미 다운로드한 로컬 파일 회수 불가)
                </li>
                <li>
                  provider_hosted_originals=
                  {String(
                    preview.non_recoverable_scopes.provider_hosted_originals
                  )}{" "}
                  (GitHub 원본 저장소·PR·커밋은 삭제되지 않음)
                </li>
              </ul>
            </div>

            {/* Sibling Repo & Shared Account Preservation Notice */}
            <div
              className="detail-box detail-box--neutral"
              data-testid="deletion-preservation-notice"
            >
              <h3 className="detail-box__title">
                유지되는 독립 자산 안내 (Preservation Scope)
              </h3>
              <p className="detail-box__body">
                워크스페이스에 연결된 <strong>다른 독립 저장소</strong>의 데이터와
                공유 <strong>GitHub 계정 연결(source_account)</strong>은 영향
                받지 않고 그대로 유지됩니다.
              </p>
            </div>

            {/* Explicit Irreversible Acknowledgement Checkbox */}
            <label
              className="checkbox-row deletion-ack-box"
              data-testid="deletion-ack-label"
            >
              <input
                type="checkbox"
                data-testid="deletion-ack-checkbox"
                checked={acknowledged}
                disabled={isConfirming || isStaleOrExpiredPreview}
                onChange={(e) => onChangeAcknowledged(e.target.checked)}
              />
              <span>
                <strong>
                  위 영향 범위 및 다운로드 파일 회수 불가를 확인했으며 영구
                  삭제에 동의합니다
                </strong>{" "}
                (확인 시 즉시 삭제 펜스가 활성화되며{" "}
                <code className="mono">acknowledge_irreversible_purge=true</code>
                로 접수됩니다.)
              </span>
            </label>
          </>
        )}

        <div className="action-row modal-card__footer">
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            disabled={isLoadingPreview || isConfirming}
            onClick={onRefreshPreview}
          >
            영향 범위 새로고침
          </button>
          <button
            type="button"
            className="btn btn--danger btn--sm"
            data-testid="confirm-source-deletion-button"
            disabled={
              !preview ||
              !acknowledged ||
              isConfirming ||
              isLoadingPreview ||
              isStaleOrExpiredPreview
            }
            onClick={onConfirmDeletion}
          >
            {isConfirming
              ? "영구 삭제 접수 중 (202 대기)..."
              : "저장소 데이터 영구 삭제 확정 (deletion-confirm)"}
          </button>
        </div>
      </div>
    </div>
  );
}
