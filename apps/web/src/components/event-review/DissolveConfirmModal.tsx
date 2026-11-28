"use client";

import React, { useState } from "react";
import type { WorkEventDetailResponse } from "@/domain/event-review-models";

interface DissolveConfirmModalProps {
  event: WorkEventDetailResponse;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirmDissolve: (reason: string) => void;
}

export function DissolveConfirmModal({
  event,
  isSubmitting,
  onCancel,
  onConfirmDissolve,
}: DissolveConfirmModalProps) {
  const [reason, setReason] = useState(
    "개별 작업 단위로 다시 분리하여 검토하기 위해 병합 해제"
  );

  const memberCount =
    event.composite_details?.member_count ??
    event.composite_summary?.member_count ??
    0;

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dissolve-modal-title"
      data-testid="dissolve-confirm-modal"
    >
      <div className="modal-card">
        <div className="modal-card__header">
          <div>
            <span className="badge badge--warning">
              Dissolve (USER_COMPOSITE 병합 해제)
            </span>
            <h3 id="dissolve-modal-title" className="section-title">
              이 병합 Event({event.event_id} · v{event.version})를 해제하시겠습니까?
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

        <div
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="dissolve-impact-warning"
        >
          <p className="status-banner__headline">
            병합 해제 시 파생 산출물 STALE 처리 및 멤버 재평가 안내
          </p>
          <p className="status-banner__detail">
            이 <code className="mono">USER_COMPOSITE</code>에 연결된{" "}
            <strong className="mono">
              Fact {event.downstream_impact.fact_count}건 · Claim{" "}
              {event.downstream_impact.claim_count}건 · Export{" "}
              {event.downstream_impact.export_count}건
            </strong>
            은 <code className="mono">STALE</code> 처리되며, 포함된{" "}
            <strong>{memberCount}개 멤버 Event</strong>는 현재 소스 기준으로{" "}
            <code className="mono">CANDIDATE</code> /{" "}
            <code className="mono">CONFIRMED</code> /{" "}
            <code className="mono">STALE</code> 상태로 재평가되어 목록에 복원됩니다.
          </p>
        </div>

        <div className="form-field">
          <label htmlFor="dissolve-reason-input" className="field-label">
            병합 해제 사유
          </label>
          <textarea
            id="dissolve-reason-input"
            className="textarea-input"
            rows={2}
            data-testid="dissolve-reason-input"
            value={reason}
            disabled={isSubmitting}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>

        {isSubmitting && (
          <div
            className="inline-alert inline-alert--info"
            role="status"
            data-testid="dissolve-modal-processing-status"
          >
            <strong>병합 해제 처리 중...</strong> 서버 200 커밋 응답 수신 전에는
            상태를 변경하지 않습니다.
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
            className="btn btn--danger btn--sm"
            data-testid="confirm-dissolve-submit-button"
            disabled={isSubmitting}
            onClick={() => onConfirmDissolve(reason.trim())}
          >
            {isSubmitting ? "병합 해제 처리 중..." : "병합 해제 확정 실행"}
          </button>
        </div>
      </div>
    </div>
  );
}
