"use client";

import React, { useState } from "react";
import type { ClaimReviewAdapter } from "@/adapters/claim-review-adapter";
import type {
  FactSummaryItem,
  FactSupportItem,
} from "@/domain/claim-review-models";
import {
  extractMetricSixElementPills,
  formatFactStatusBadge,
  formatSupportTypeBadge,
} from "@/domain/claim-review-presenter";
import { EphemeralSourceViewer } from "./EphemeralSourceViewer";

interface FactReviewPanelProps {
  adapter: ClaimReviewAdapter;
  facts: FactSummaryItem[];
  supportsByFactId: Record<string, FactSupportItem[] | undefined>;
  loadingSupportsForId: string | null;
  mutationsLocked: boolean;
  isMutating: boolean;
  onLoadFactSupports: (factId: string) => void;
  onApproveFact: (fact: FactSummaryItem) => void;
  onRejectFact: (fact: FactSummaryItem, reason: string) => void;
  onEditFact: (params: {
    fact: FactSummaryItem;
    predicate: string;
    object_text: string;
  }) => void;
}

export function FactReviewPanel({
  adapter,
  facts,
  supportsByFactId,
  loadingSupportsForId,
  mutationsLocked,
  isMutating,
  onLoadFactSupports,
  onApproveFact,
  onRejectFact,
  onEditFact,
}: FactReviewPanelProps) {
  const [expandedFactId, setExpandedFactId] = useState<string | null>(null);
  const [editingFactId, setEditingFactId] = useState<string | null>(null);
  const [editPredicate, setEditPredicate] = useState("");
  const [editObjectText, setEditObjectText] = useState("");
  const [rejectingFactId, setRejectingFactId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("근거 범위와 맞지 않아 제외");

  const handleToggleSupports = (factId: string) => {
    if (expandedFactId === factId) {
      setExpandedFactId(null);
      return;
    }
    setExpandedFactId(factId);
    if (!supportsByFactId[factId]) {
      onLoadFactSupports(factId);
    }
  };

  if (facts.length === 0) {
    return (
      <section className="empty-state" data-testid="fact-review-empty">
        <p className="empty-state__title">검토할 Canonical Fact가 없습니다</p>
        <p className="empty-state__body">
          확정된 Work Event에서 추출된 Fact가 여기에 표시됩니다.
        </p>
      </section>
    );
  }

  return (
    <section
      className="card"
      aria-labelledby="fact-review-heading"
      data-testid="fact-review-panel"
    >
      <div className="section-header">
        <span className="step-kicker">STAGE 2 · CANONICAL FACTS</span>
        <h2 id="fact-review-heading" className="section-title">
          Canonical Fact 검토 및 비덮어쓰기 편집 ({facts.length}건)
        </h2>
        <p className="section-subtitle">
          Fact 승인(<code className="mono">/actions/approve</code>)은{" "}
          <code className="mono">CANDIDATE → USER_CONFIRMED</code>로만 전환되며
          자동으로 <code className="mono">SUPPORTED</code>가 되지 않습니다. 편집(
          <code className="mono">PATCH</code>)은 기존 버전을{" "}
          <code className="mono">SUPERSEDED</code>로 보존하고 새{" "}
          <code className="mono">CANDIDATE (v+1)</code>를 생성합니다(ER-012).
        </p>
      </div>

      <ul className="stage-list" data-testid="fact-list">
        {facts.map((fact) => {
          const statusBadge = formatFactStatusBadge(fact.status);
          const isExpanded = expandedFactId === fact.fact_id;
          const isEditing = editingFactId === fact.fact_id;
          const isRejecting = rejectingFactId === fact.fact_id;
          const supports = supportsByFactId[fact.fact_id];
          const metricInfo =
            fact.fact_type === "METRIC"
              ? extractMetricSixElementPills(fact.normalized_value)
              : null;

          const canApproveFact =
            !mutationsLocked && !isMutating && fact.status === "CANDIDATE";
          const canEditOrReject =
            !mutationsLocked &&
            !isMutating &&
            fact.status !== "SUPERSEDED" &&
            fact.status !== "REJECTED";

          return (
            <li
              key={fact.fact_id}
              className="subcard fact-card"
              data-testid={`fact-item-${fact.fact_id}`}
              data-fact-status={fact.status}
              data-fact-version={fact.version}
            >
              <div className="status-banner__top">
                <div className="status-banner__badges">
                  <span className="badge badge--neutral mono">
                    {fact.fact_type}
                  </span>
                  <span
                    className={`badge ${statusBadge.badgeClass}`}
                    data-testid={`fact-status-badge-${fact.fact_id}`}
                  >
                    {statusBadge.label}
                  </span>
                  <span className="badge badge--neutral mono">
                    v{fact.version} · {fact.fact_id}
                  </span>
                  {fact.supersedes_fact_id && (
                    <span className="badge badge--info mono">
                      supersedes: {fact.supersedes_fact_id}
                    </span>
                  )}
                </div>
                <span className="mono field-help">
                  DIRECT {fact.direct_support_count} · CONTEXT{" "}
                  {fact.context_support_count} · CONTRADICTS{" "}
                  {fact.contradicts_support_count}
                </span>
              </div>

              <p className="fact-card__statement">
                <strong className="mono">{fact.predicate}</strong>:{" "}
                {fact.object_text}
              </p>

              {/* Semantic qualifiers */}
              {fact.normalized_value && (
                <div className="status-banner__badges">
                  {fact.normalized_value.completion_state && (
                    <span className="badge badge--neutral mono">
                      completion: {fact.normalized_value.completion_state}
                    </span>
                  )}
                  {fact.normalized_value.scope && (
                    <span className="badge badge--neutral mono">
                      scope: {fact.normalized_value.scope}
                    </span>
                  )}
                  {fact.normalized_value.environment_scope && (
                    <span className="badge badge--neutral mono">
                      env: {fact.normalized_value.environment_scope}
                    </span>
                  )}
                </div>
              )}

              {/* CLAIM-06 6-Element Metric Pills */}
              {metricInfo && (
                <div
                  className="metric-pills-box"
                  data-testid={`fact-metric-pills-${fact.fact_id}`}
                >
                  <p className="field-label">
                    CLAIM-06 6요소 지표 구성 ({metricInfo.completeCount}/6)
                  </p>
                  <div className="metric-pills-row">
                    {metricInfo.pills.map((pill) => (
                      <span
                        key={pill.key}
                        className={`badge ${
                          pill.present ? "badge--success" : "badge--danger"
                        } mono`}
                      >
                        {pill.key}: {pill.value ?? "누락"}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Fact actions */}
              <div className="action-row">
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  data-testid={`fact-toggle-supports-${fact.fact_id}`}
                  onClick={() => handleToggleSupports(fact.fact_id)}
                >
                  {isExpanded ? "근거 메타데이터 접기" : "근거 위치 보기 (Supports)"}
                </button>
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  data-testid={`fact-approve-button-${fact.fact_id}`}
                  disabled={!canApproveFact}
                  onClick={() => onApproveFact(fact)}
                >
                  사용자 확인 승인 (USER_CONFIRMED)
                </button>
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  data-testid={`fact-edit-button-${fact.fact_id}`}
                  disabled={!canEditOrReject}
                  onClick={() => {
                    setRejectingFactId(null);
                    setEditingFactId(isEditing ? null : fact.fact_id);
                    setEditPredicate(fact.predicate);
                    setEditObjectText(fact.object_text);
                  }}
                >
                  새 버전 편집 (PATCH v+1)
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  data-testid={`fact-reject-button-${fact.fact_id}`}
                  disabled={!canEditOrReject}
                  onClick={() => {
                    setEditingFactId(null);
                    setRejectingFactId(isRejecting ? null : fact.fact_id);
                  }}
                >
                  제외 (Reject)
                </button>
              </div>

              {/* Inline Non-Overwrite Fact Edit Form */}
              {isEditing && !mutationsLocked && (
                <form
                  className="detail-box detail-box--neutral"
                  data-testid={`fact-edit-form-${fact.fact_id}`}
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!editPredicate.trim() || !editObjectText.trim()) return;
                    onEditFact({
                      fact,
                      predicate: editPredicate.trim(),
                      object_text: editObjectText.trim(),
                    });
                    setEditingFactId(null);
                  }}
                >
                  <p className="detail-box__title">
                    ER-012 비덮어쓰기 편집 (새 CANDIDATE v{fact.version + 1} 생성)
                  </p>
                  <div className="form-field">
                    <label
                      htmlFor={`fact-predicate-${fact.fact_id}`}
                      className="field-label"
                    >
                      술어 (predicate)
                    </label>
                    <input
                      id={`fact-predicate-${fact.fact_id}`}
                      type="text"
                      className="text-input"
                      data-testid={`fact-edit-predicate-${fact.fact_id}`}
                      value={editPredicate}
                      onChange={(e) => setEditPredicate(e.target.value)}
                      required
                    />
                  </div>
                  <div className="form-field">
                    <label
                      htmlFor={`fact-object-${fact.fact_id}`}
                      className="field-label"
                    >
                      내용 (object_text)
                    </label>
                    <textarea
                      id={`fact-object-${fact.fact_id}`}
                      rows={2}
                      className="textarea-input"
                      data-testid={`fact-edit-object-${fact.fact_id}`}
                      value={editObjectText}
                      onChange={(e) => setEditObjectText(e.target.value)}
                      required
                    />
                  </div>
                  <div className="action-row">
                    <button
                      type="submit"
                      className="btn btn--primary btn--sm"
                      data-testid={`fact-edit-submit-${fact.fact_id}`}
                      disabled={isMutating}
                    >
                      새 CANDIDATE 버전 저장
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => setEditingFactId(null)}
                    >
                      취소
                    </button>
                  </div>
                </form>
              )}

              {/* Inline Fact Reject Form */}
              {isRejecting && !mutationsLocked && (
                <div
                  className="detail-box detail-box--warning"
                  data-testid={`fact-reject-form-${fact.fact_id}`}
                >
                  <p className="detail-box__title">Fact 제외 (REJECTED)</p>
                  <div className="form-field">
                    <label
                      htmlFor={`fact-reject-reason-${fact.fact_id}`}
                      className="field-label"
                    >
                      제외 사유
                    </label>
                    <input
                      id={`fact-reject-reason-${fact.fact_id}`}
                      type="text"
                      className="text-input"
                      data-testid={`fact-reject-reason-${fact.fact_id}`}
                      value={rejectReason}
                      onChange={(e) => setRejectReason(e.target.value)}
                    />
                  </div>
                  <div className="action-row">
                    <button
                      type="button"
                      className="btn btn--danger btn--sm"
                      data-testid={`fact-reject-confirm-${fact.fact_id}`}
                      disabled={isMutating || !rejectReason.trim()}
                      onClick={() => {
                        onRejectFact(fact, rejectReason.trim());
                        setRejectingFactId(null);
                      }}
                    >
                      Fact 제외 확정
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => setRejectingFactId(null)}
                    >
                      취소
                    </button>
                  </div>
                </div>
              )}

              {/* Fact Supports (Zero extracted_span, cm-locator-v1 + Plan C Ephemeral Viewer) */}
              {isExpanded && (
                <div
                  className="provenance-subpanel"
                  data-testid={`fact-supports-panel-${fact.fact_id}`}
                >
                  {loadingSupportsForId === fact.fact_id ? (
                    <p className="field-help">근거 메타데이터를 불러오는 중...</p>
                  ) : !supports || supports.length === 0 ? (
                    <p className="field-help">
                      연결된 Fact Support 메타데이터가 없습니다.
                    </p>
                  ) : (
                    <ul className="stage-list">
                      {supports.map((sup) => {
                        const supBadge = formatSupportTypeBadge(sup.support_type);
                        return (
                          <li
                            key={sup.support_id}
                            className="stage-list__item"
                            data-testid={`fact-support-item-${sup.support_id}`}
                          >
                            <div className="status-banner__badges">
                              <span className={`badge ${supBadge.badgeClass}`}>
                                {supBadge.label}
                              </span>
                              <span className="badge badge--neutral mono">
                                {sup.status} · {sup.validator} ({sup.validator_version})
                              </span>
                              <span className="badge badge--neutral mono">
                                {sup.locator?.schema ?? "cm-locator-v1"}
                              </span>
                            </div>
                            <EphemeralSourceViewer
                              adapter={adapter}
                              workspaceId={fact.workspace_id}
                              contentUnitId={sup.content_unit_id}
                              unitType={sup.unit_type}
                              locator={sup.locator ?? null}
                              revisionContentHash={sup.revision_content_hash ?? null}
                              revisionObservedAt={sup.revision_observed_at ?? null}
                              lastCheckedAt={sup.last_checked_at ?? null}
                              disabledByPolicyOrFence={mutationsLocked}
                            />
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
