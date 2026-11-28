"use client";

import React, { useMemo, useState } from "react";
import type {
  ClaimSummaryItem,
  ExportDetailResponse,
  ExportFormat,
  ExportPolicy,
  ExportSummaryItem,
} from "@/domain/claim-review-models";
import {
  evaluateExportSelection,
  formatComputedVerificationBadge,
} from "@/domain/claim-review-presenter";

interface ExportPanelProps {
  claims: ClaimSummaryItem[];
  exportsList: ExportSummaryItem[];
  selectedExport: ExportDetailResponse | null;
  mutationsLocked: boolean;
  isBusy: boolean;
  onCreateExport: (params: {
    format: ExportFormat;
    export_policy: ExportPolicy;
    claim_ids: string[];
    expected_claim_versions: Record<string, number>;
  }) => void;
  onSelectExport: (exportId: string) => void;
  onDeleteExport: (exportId: string) => void;
}

export function ExportPanel({
  claims,
  exportsList,
  selectedExport,
  mutationsLocked,
  isBusy,
  onCreateExport,
  onSelectExport,
  onDeleteExport,
}: ExportPanelProps) {
  const approvedClaims = useMemo(
    () => claims.filter((c) => c.status === "APPROVED"),
    [claims]
  );

  const [selectedClaimIds, setSelectedClaimIds] = useState<string[]>(() =>
    approvedClaims
      .filter((c) => c.computed_verification === "SOURCE_VERIFIED")
      .map((c) => c.claim_id)
  );
  const [format, setFormat] = useState<ExportFormat>("MARKDOWN");
  const [policy, setPolicy] = useState<ExportPolicy>("SOURCE_VERIFIED_ONLY");

  const effectiveSelectedIds = useMemo(
    () =>
      selectedClaimIds.filter((id) =>
        approvedClaims.some((c) => c.claim_id === id)
      ),
    [approvedClaims, selectedClaimIds]
  );

  const selectedClaimObjects = useMemo(
    () =>
      approvedClaims.filter((c) => effectiveSelectedIds.includes(c.claim_id)),
    [approvedClaims, effectiveSelectedIds]
  );

  const evaluation = useMemo(
    () => evaluateExportSelection(selectedClaimObjects, policy),
    [selectedClaimObjects, policy]
  );

  const toggleClaim = (claimId: string) => {
    setSelectedClaimIds((prev) =>
      prev.includes(claimId)
        ? prev.filter((id) => id !== claimId)
        : [...prev, claimId]
    );
  };

  const handleSubmitExport = () => {
    if (!evaluation.valid || mutationsLocked || isBusy) return;
    const expectedVersions: Record<string, number> = {};
    for (const c of evaluation.eligibleClaims) {
      expectedVersions[c.claim_id] = c.version;
    }
    onCreateExport({
      format,
      export_policy: policy,
      claim_ids: evaluation.eligibleClaims.map((c) => c.claim_id),
      expected_claim_versions: expectedVersions,
    });
  };

  return (
    <section
      className="card"
      aria-labelledby="export-panel-heading"
      data-testid="claim-export-panel"
    >
      <div className="section-header">
        <span className="step-kicker">STAGE 4 · ZERO-QUOTE EXPORT</span>
        <h2 id="export-panel-heading" className="section-title">
          비인용 Markdown / JSON Export 및 출처 메타데이터 매니페스트
        </h2>
        <p className="section-subtitle">
          승인된(<code className="mono">APPROVED</code>) Claim만 Export할 수
          있으며, Plan C에 따라 원문 인용구(raw quote) 없이{" "}
          <code className="mono">URL + locator + hash + observed_at + checked_at</code>{" "}
          비인용 출처 메타데이터만 포함됩니다.
        </p>
      </div>

      {/* Export Builder */}
      <div className="subcard" data-testid="export-builder-card">
        <h3 className="subcard__title">1. 승인된 Claim 선택 ({approvedClaims.length}건 가능)</h3>
        {approvedClaims.length === 0 ? (
          <p className="field-help">
            승인(APPROVED) 상태인 Claim이 아직 없습니다. 먼저 Claim을 검증·승인해
            주세요.
          </p>
        ) : (
          <ul className="checkbox-list" data-testid="export-claim-selector-list">
            {approvedClaims.map((claim) => {
              const verBadge = formatComputedVerificationBadge(
                claim.computed_verification
              );
              const checked = effectiveSelectedIds.includes(claim.claim_id);
              return (
                <li key={claim.claim_id}>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      data-testid={`export-select-claim-${claim.claim_id}`}
                      checked={checked}
                      disabled={mutationsLocked || isBusy}
                      onChange={() => toggleClaim(claim.claim_id)}
                    />
                    <span>
                      <span className="status-banner__badges">
                        <span className="badge badge--neutral mono">
                          {claim.claim_id} · v{claim.version}
                        </span>
                        <span className={`badge ${verBadge.badgeClass}`}>
                          {claim.computed_verification}
                        </span>
                      </span>
                      <span className="export-claim-text">{claim.claim_text}</span>
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}

        <div className="export-options-grid">
          <div className="form-field">
            <label htmlFor="export-format-select" className="field-label">
              Export 포맷
            </label>
            <select
              id="export-format-select"
              className="select-input"
              data-testid="export-format-select"
              value={format}
              disabled={mutationsLocked || isBusy}
              onChange={(e) => setFormat(e.target.value as ExportFormat)}
            >
              <option value="MARKDOWN">MARKDOWN (비인용 이력서/포트폴리오 초안)</option>
              <option value="JSON">JSON (구조화 매니페스트 + 비인용 출처)</option>
            </select>
          </div>

          <div className="form-field">
            <label htmlFor="export-policy-select" className="field-label">
              검증 정책 (Export Policy)
            </label>
            <select
              id="export-policy-select"
              className="select-input"
              data-testid="export-policy-select"
              value={policy}
              disabled={mutationsLocked || isBusy}
              onChange={(e) => setPolicy(e.target.value as ExportPolicy)}
            >
              <option value="SOURCE_VERIFIED_ONLY">
                SOURCE_VERIFIED_ONLY (기본 — 원본 검증 Claim만 허용)
              </option>
              <option value="ALLOW_USER_CONFIRMED">
                ALLOW_USER_CONFIRMED (사용자 확인 Claim 포함 + 한계 고지)
              </option>
            </select>
          </div>
        </div>

        {/* Policy Violation or Limitation Notice */}
        {!evaluation.valid && evaluation.ineligibleReason && (
          <div
            className="inline-alert inline-alert--warning"
            role="alert"
            data-testid="export-validation-warning"
          >
            {evaluation.ineligibleReason}
          </div>
        )}

        {evaluation.requiresLimitationNotice && evaluation.limitationNotice && (
          <div
            className="inline-alert inline-alert--info"
            role="status"
            data-testid="export-user-confirmed-limitation-notice"
          >
            {evaluation.limitationNotice}
          </div>
        )}

        <div className="action-row">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            data-testid="run-export-button"
            disabled={!evaluation.valid || mutationsLocked || isBusy}
            onClick={handleSubmitExport}
          >
            {isBusy ? "Export 생성 중..." : `${format} Export 생성`}
          </button>
        </div>
      </div>

      {/* Export History & Non-Quote Citation Preview */}
      <div className="readiness-grid">
        <div className="subcard" data-testid="export-history-card">
          <h3 className="subcard__title">
            생성된 Export 목록 ({exportsList.length}건)
          </h3>
          {exportsList.length === 0 ? (
            <p className="field-help">생성된 Export가 없습니다.</p>
          ) : (
            <ul className="stage-list" data-testid="export-list">
              {exportsList.map((exp) => (
                <li
                  key={exp.export_id}
                  className="stage-list__item"
                  data-testid={`export-list-item-${exp.export_id}`}
                  data-export-status={exp.status}
                >
                  <div className="stage-list__row">
                    <span className="stage-list__name mono">
                      {exp.export_id} · {exp.format}
                    </span>
                    <div className="status-banner__badges">
                      <span
                        className={`badge ${
                          exp.status === "INVALIDATED"
                            ? "badge--warning"
                            : "badge--success"
                        } mono`}
                      >
                        {exp.status}
                      </span>
                      <span className="badge badge--neutral mono">
                        {exp.export_policy}
                      </span>
                    </div>
                  </div>
                  {exp.invalidation_reason && (
                    <p
                      className="inline-alert inline-alert--warning"
                      data-testid={`export-invalidated-reason-${exp.export_id}`}
                    >
                      {exp.invalidation_reason}
                    </p>
                  )}
                  <div className="action-row">
                    <button
                      type="button"
                      className="btn btn--secondary btn--sm"
                      data-testid={`view-export-${exp.export_id}`}
                      onClick={() => onSelectExport(exp.export_id)}
                    >
                      미리보기 / 매니페스트
                    </button>
                    <button
                      type="button"
                      className="btn btn--danger btn--sm"
                      data-testid={`delete-export-${exp.export_id}`}
                      disabled={mutationsLocked || isBusy}
                      onClick={() => onDeleteExport(exp.export_id)}
                    >
                      삭제 (DELETE)
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Non-Quote Citation & Rendered Preview */}
        <div className="subcard" data-testid="export-preview-card">
          <div className="subcard__header">
            <h3 className="subcard__title">Export 미리보기 (Zero Raw Quotes)</h3>
            {selectedExport && (
              <span className="badge badge--neutral mono">
                {selectedExport.export_id} · {selectedExport.status}
              </span>
            )}
          </div>

          {!selectedExport ? (
            <p className="field-help">
              Export를 생성하거나 좌측 목록에서 항목을 선택하면 비인용 출처
              메타데이터와 렌더링 결과를 확인할 수 있습니다.
            </p>
          ) : (
            <div className="export-preview-body">
              {selectedExport.status === "INVALIDATED" && (
                <div
                  className="status-banner status-banner--warning"
                  role="alert"
                  data-testid="selected-export-invalidated-banner"
                >
                  <p className="status-banner__headline">
                    이 Export는 무효화(INVALIDATED)되었습니다
                  </p>
                  <p className="status-banner__detail">
                    {selectedExport.invalidation_reason ??
                      "하위 Fact 또는 Claim 버전 변경으로 더 이상 최신 상태가 아닙니다."}
                  </p>
                </div>
              )}

              {/* Non-Quote Citations Table/List */}
              <div data-testid="export-citations-manifest">
                <p className="field-label">
                  비인용 출처 매니페스트 (contains_raw_quotes=false ·{" "}
                  {selectedExport.manifest.citations.length}건)
                </p>
                <ul className="bullet-list mono">
                  {selectedExport.manifest.citations.map((cite) => (
                    <li key={`${cite.claim_id}-v${cite.claim_version}`}>
                      {cite.claim_id} (v{cite.claim_version} ·{" "}
                      {cite.computed_verification}) | URL:{" "}
                      {cite.source_url ?? "N/A"} | locator:{" "}
                      {cite.locator?.file_path ??
                        cite.locator?.section ??
                        "cm-locator-v1"}
                      {cite.locator?.start_line != null
                        ? ` L${cite.locator.start_line}-L${cite.locator.end_line ?? cite.locator.start_line}`
                        : ""}{" "}
                      | hash: {cite.revision_content_hash ?? "N/A"} |
                      observed_at: {cite.observed_at ?? "N/A"} | checked_at:{" "}
                      {cite.checked_at ?? "N/A"}
                    </li>
                  ))}
                </ul>
              </div>

              {selectedExport.rendered_markdown && (
                <pre
                  className="ephemeral-pre"
                  data-testid="export-rendered-markdown"
                >
                  {selectedExport.rendered_markdown}
                </pre>
              )}

              {selectedExport.rendered_json && (
                <pre
                  className="ephemeral-pre"
                  data-testid="export-rendered-json"
                >
                  {JSON.stringify(selectedExport.rendered_json, null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
