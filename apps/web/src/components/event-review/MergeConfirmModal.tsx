"use client";

import React, { useState } from "react";
import type { WorkEventSummaryItem } from "@/domain/event-review-models";

interface MergeConfirmModalProps {
  selectedEvents: WorkEventSummaryItem[];
  affectedImpact: {
    fact_count: number;
    claim_count: number;
    export_count: number;
  };
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirmMerge: (params: { title: string; reason: string }) => void;
}

export function MergeConfirmModal({
  selectedEvents,
  affectedImpact,
  isSubmitting,
  onCancel,
  onConfirmMerge,
}: MergeConfirmModalProps) {
  const defaultTitle =
    selectedEvents.length > 0
      ? `${selectedEvents[0].title} 외 ${selectedEvents.length - 1}건 통합 작업`
      : "사용자 병합 Event";

  const [title, setTitle] = useState(defaultTitle);
  const [reason, setReason] = useState(
    "서로 연결된 단일 업무 단위로 통합 검토"
  );

  const canSubmit = title.trim().length > 0 && !isSubmitting;

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="merge-modal-title"
      data-testid="merge-confirm-modal"
    >
      <div className="modal-card">
        <div className="modal-card__header">
          <div>
            <span className="badge badge--info">USER_COMPOSITE 병합 확인 (T-304B)</span>
            <h3 id="merge-modal-title" className="section-title">
              선택한 {selectedEvents.length}개 Event를 하나의 논리 Event로 병합하시겠습니까?
            </h3>
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            닫기
          </button>
        </div>

        <div className="subcard" data-testid="merge-modal-member-versions">
          <h4 className="subcard__title">병합 대상 멤버 및 낙관적 잠금 버전 목록</h4>
          <ul className="bullet-list">
            {selectedEvents.map((item) => (
              <li key={item.event_id} className="mono">
                {item.event_id} · <strong>v{item.version}</strong> ({item.anchor_mode} ·{" "}
                {item.status}) — {item.title}
              </li>
            ))}
          </ul>
        </div>

        <div
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="merge-modal-impact-warning"
        >
          <p className="status-banner__headline">
            파생 데이터 영향 안내 — 기존 Fact / Claim / Export는 자동 승계되지 않습니다
          </p>
          <p className="status-banner__detail">
            영향받는 기존 파생 산출물:{" "}
            <strong className="mono">
              Fact {affectedImpact.fact_count}건 · Claim {affectedImpact.claim_count}건 ·
              Export {affectedImpact.export_count}건
            </strong>
            . 병합이 커밋되면 기존 개별 멤버의 산출물은 자동 승계되지 않고 새{" "}
            <code className="mono">USER_COMPOSITE</code> 기준으로 재생성·재검증됩니다.
          </p>
          <p className="status-banner__detail">
            원래 멤버들은 삭제되지 않고 <code className="mono">SUPERSEDED</code> 이력으로
            보존되며, 상단 활성 카운트에서는 새 <code className="mono">USER_COMPOSITE</code>{" "}
            1개 논리 Event로만 집계됩니다.
          </p>
        </div>

        <div className="form-field">
          <label htmlFor="merge-title-input" className="field-label">
            새 USER_COMPOSITE 제목
          </label>
          <input
            id="merge-title-input"
            type="text"
            className="text-input"
            data-testid="merge-title-input"
            value={title}
            disabled={isSubmitting}
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>

        <div className="form-field">
          <label htmlFor="merge-reason-input" className="field-label">
            병합 사유 (감사 이력 기록)
          </label>
          <textarea
            id="merge-reason-input"
            className="textarea-input"
            rows={2}
            data-testid="merge-reason-input"
            value={reason}
            disabled={isSubmitting}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>

        {isSubmitting && (
          <div
            className="inline-alert inline-alert--info"
            role="status"
            data-testid="merge-modal-processing-status"
          >
            <strong>병합 처리 중...</strong> 서버 트랜잭션 200 응답이 도착하기 전에는
            목록을 변경하지 않습니다.
          </div>
        )}

        <div className="action-row modal-card__footer">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            취소
          </button>
          <button
            type="button"
            className="btn btn--primary btn--sm"
            data-testid="confirm-merge-submit-button"
            disabled={!canSubmit}
            onClick={() => onConfirmMerge({ title: title.trim(), reason: reason.trim() })}
          >
            {isSubmitting ? "병합 처리 중..." : "병합 확정 실행"}
          </button>
        </div>
      </div>
    </div>
  );
}
