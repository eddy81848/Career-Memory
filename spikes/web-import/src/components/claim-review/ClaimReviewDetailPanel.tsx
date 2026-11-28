"use client";

import React, { useEffect, useState } from "react";
import type { ClaimReviewAdapter } from "@/adapters/claim-review-adapter";
import type {
  ClaimDetailResponse,
  ClaimSupportsResponse,
} from "@/domain/claim-review-models";
import {
  evaluateClaimApprovalGate,
  formatClaimStatusBadge,
  formatComputedVerificationBadge,
} from "@/domain/claim-review-presenter";
import { ClaimProvenanceDrilldown } from "./ClaimProvenanceDrilldown";

interface ClaimReviewDetailPanelProps {
  adapter: ClaimReviewAdapter;
  claim: ClaimDetailResponse | null;
  supports: ClaimSupportsResponse | null;
  isLoadingDetail: boolean;
  isLoadingSupports: boolean;
  mutationsLocked: boolean;
  inFlightAction: string | null;
  onEditClaim: (params: {
    claim: ClaimDetailResponse;
    claim_text: string;
    edit_reason: string;
  }) => void;
  onRevalidateClaim: (claim: ClaimDetailResponse) => void;
  onApproveClaim: (params: {
    claim: ClaimDetailResponse;
    acknowledge_user_confirmed_atoms: boolean;
  }) => void;
  onRejectClaim: (params: {
    claim: ClaimDetailResponse;
    reason: string;
  }) => void;
}

export function ClaimReviewDetailPanel({
  adapter,
  claim,
  supports,
  isLoadingDetail,
  isLoadingSupports,
  mutationsLocked,
  inFlightAction,
  onEditClaim,
  onRevalidateClaim,
  onApproveClaim,
  onRejectClaim,
}: ClaimReviewDetailPanelProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [editReason, setEditReason] = useState("표현 명확화 및 근거 범위 일치 수정");
  const [ackUserConfirmed, setAckUserConfirmed] = useState(false);
  const [showRejectBox, setShowRejectBox] = useState(false);
  const [rejectReason, setRejectReason] = useState("포트폴리오 기재 대상에서 제외");

  useEffect(() => {
    setIsEditing(false);
    setShowRejectBox(false);
    setAckUserConfirmed(false);
    if (claim) {
      setEditText(claim.claim_text);
    }
  }, [claim?.claim_id, claim?.version]);

  if (isLoadingDetail && !claim) {
    return (
      <div className="subcard" role="status" data-testid="claim-detail-loading">
        Claim 상세 정보를 불러오는 중입니다...
      </div>
    );
  }

  if (!claim) {
    return (
      <div className="empty-state" data-testid="claim-detail-empty">
        <p className="empty-state__title">검토할 Claim을 선택하세요</p>
        <p className="empty-state__body">
          좌측 목록에서 Claim을 선택하면 워크플로 상태(status)와 읽기 전용
          검증 수준(computed_verification), Atom 근거 드릴다운 및 승인 게이트를
          확인할 수 있습니다.
        </p>
      </div>
    );
  }

  const workflowBadge = formatClaimStatusBadge(claim.status);
  const verificationBadge = formatComputedVerificationBadge(
    claim.computed_verification
  );
  const gate = evaluateClaimApprovalGate(claim, {
    acknowledgeUserConfirmed: ackUserConfirmed,
    workspaceMutationsLocked: mutationsLocked,
  });

  const isBusy = inFlightAction !== null;
  const canEdit =
    !mutationsLocked &&
    !isBusy &&
    claim.status !== "SUPERSEDED" &&
    claim.status !== "REJECTED";
  const canRevalidate =
    !mutationsLocked &&
    !isBusy &&
    claim.status !== "SUPERSEDED" &&
    claim.status !== "REJECTED";
  const canReject =
    !mutationsLocked &&
    !isBusy &&
    claim.status !== "SUPERSEDED" &&
    claim.status !== "REJECTED";

  return (
    <article
      className="card claim-detail-panel"
      aria-label="선택된 Claim 상세 검토 패널"
      data-testid="claim-detail-panel"
      data-claim-id={claim.claim_id}
      data-claim-version={claim.version}
      data-claim-status={claim.status}
      data-computed-verification={claim.computed_verification}
      data-approvable={String(claim.approvable)}
    >
      {/* Strict Separation: Workflow Status vs Read-Only Computed Verification */}
      <div className="claim-status-split" data-testid="claim-status-verification-split">
        <div className="subcard" data-testid="claim-workflow-status-box">
          <span className="step-kicker">WORKFLOW STATUS (진행 상태)</span>
          <div className="status-banner__badges">
            <span
              className={`badge ${workflowBadge.badgeClass}`}
              data-testid="claim-workflow-status-badge"
            >
              {workflowBadge.label}
            </span>
            <span
              className="badge badge--neutral mono"
              data-testid="claim-version-badge"
            >
              v{claim.version} · {claim.claim_id}
            </span>
            {claim.supersedes_claim_id && (
              <span className="badge badge--info mono">
                supersedes: {claim.supersedes_claim_id}
              </span>
            )}
          </div>
          <p className="field-help">{workflowBadge.description}</p>
        </div>

        <div className="subcard" data-testid="claim-computed-verification-box">
          <span className="step-kicker">
            COMPUTED VERIFICATION (서버 계산 · 읽기 전용)
          </span>
          <div className="status-banner__badges">
            <span
              className={`badge ${verificationBadge.badgeClass}`}
              data-testid="claim-computed-verification-badge"
            >
              {verificationBadge.label}
            </span>
          </div>
          <p className="field-help">{verificationBadge.description}</p>
          <p className="field-help mono">
            Atoms: 총 {claim.atom_count} · SUPPORTED {claim.supported_atom_count} ·
            USER_CONFIRMED {claim.user_confirmed_atom_count} · UNSUPPORTED{" "}
            {claim.unsupported_atom_count} · STALE {claim.stale_atom_count}
          </p>
        </div>
      </div>

      {/* Claim Statement */}
      <div className="subcard" data-testid="claim-text-card">
        <span className="step-kicker">CLAIM STATEMENT</span>
        <p className="claim-statement-text" data-testid="claim-detail-text">
          {claim.claim_text}
        </p>
        <p className="field-help mono">
          event_id: {claim.event_id} · support_graph_epoch: #
          {claim.support_graph_epoch}
          {claim.last_verified_at
            ? ` · last_verified_at: ${claim.last_verified_at}`
            : ""}
          {claim.approved_at ? ` · approved_at: ${claim.approved_at}` : ""}
        </p>
      </div>

      {/* Non-Optimistic Edit Revalidation Callout */}
      {gate.needsRevalidation && (
        <div
          className="status-banner status-banner--warning"
          role="status"
          data-testid="claim-needs-revalidation-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning mono">
              Needs Revalidation · approvable=false
            </span>
          </div>
          <p className="status-banner__headline">
            재검증 필요 (Needs Revalidation) — 편집된 Claim은 이전 승인·원본
            검증 상태를 유지하지 않습니다
          </p>
          <p className="status-banner__detail">
            새 초안 버전(v{claim.version} · DRAFT ·{" "}
            {claim.computed_verification})이 생성되었습니다. 아래{" "}
            <strong>Run Atomization &amp; Support Validation</strong>을 실행하여
            서버에서 근거를 다시 검증한 뒤 승인할 수 있습니다.
          </p>
          <div className="action-row">
            <button
              type="button"
              className="btn btn--primary btn--sm"
              data-testid="run-revalidation-button"
              disabled={!canRevalidate}
              onClick={() => onRevalidateClaim(claim)}
            >
              {inFlightAction === "revalidate"
                ? "Atomization & Support Validation 실행 중..."
                : "Run Atomization & Support Validation"}
            </button>
          </div>
        </div>
      )}

      {/* Explicit Approval Block Reasons (when approvable == false) */}
      {!claim.approvable && (
        <div
          className="status-banner status-banner--danger"
          role="alert"
          data-testid="claim-approval-blocked-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--danger mono">
              APPROVAL BLOCKED (approvable=false)
            </span>
          </div>
          <p className="status-banner__headline">
            승인 차단: 미근거·만료 Atom 또는 미검증 상태가 해소되기 전까지 승인할
            수 없습니다
          </p>
          <ul
            className="bullet-list"
            data-testid="claim-approval-block-reasons"
          >
            {gate.blockReasons.map((reason, idx) => (
              <li key={idx}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {/* User-Confirmed Limitation Acknowledgement (required for USER_CONFIRMED / MIXED) */}
      {gate.requiresUserConfirmedAck &&
        claim.status === "SUPPORT_VALIDATED" &&
        claim.approvable && (
          <div
            className="detail-box detail-box--warning"
            data-testid="user-confirmed-ack-box"
          >
            <p className="detail-box__title">
              사용자 확인(USER_CONFIRMED / MIXED) 한계 동의 필요
            </p>
            <p className="detail-box__body">
              이 Claim은 원본 자동 검증 대신 사용자가 직접 확인한 Fact에 일부 또는
              전부 의존합니다. 승인하더라도{" "}
              <code className="mono">computed_verification</code>은{" "}
              <code className="mono">SOURCE_VERIFIED</code>로 바뀌지 않습니다.
            </p>
            <label className="checkbox-row">
              <input
                type="checkbox"
                data-testid="acknowledge-user-confirmed-checkbox"
                checked={ackUserConfirmed}
                disabled={mutationsLocked || isBusy}
                onChange={(e) => setAckUserConfirmed(e.target.checked)}
              />
              <span>
                <strong>Acknowledge user-confirmed limitation</strong> (사용자 확인
                근거 한계를 인지하고 승인합니다)
              </span>
            </label>
          </div>
        )}

      {/* Action Toolbar (ZERO verification setter inputs!) */}
      <div className="run-actions-panel" data-testid="claim-action-toolbar">
        <div className="action-row">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            data-testid="claim-approve-button"
            disabled={!gate.canApprove || isBusy}
            onClick={() =>
              onApproveClaim({
                claim,
                acknowledge_user_confirmed_atoms: ackUserConfirmed,
              })
            }
          >
            {inFlightAction === "approve"
              ? "승인 처리 중..."
              : "Claim 승인 (Approve)"}
          </button>

          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="claim-edit-button"
            disabled={!canEdit}
            onClick={() => {
              setShowRejectBox(false);
              setIsEditing((prev) => !prev);
            }}
          >
            문구 편집 (PATCH → 새 DRAFT v+1)
          </button>

          {!gate.needsRevalidation && (
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              data-testid="claim-revalidate-secondary-button"
              disabled={!canRevalidate}
              onClick={() => onRevalidateClaim(claim)}
            >
              {inFlightAction === "revalidate"
                ? "재검증 중..."
                : "Run Atomization & Support Validation"}
            </button>
          )}

          <button
            type="button"
            className="btn btn--ghost btn--sm"
            data-testid="claim-reject-button"
            disabled={!canReject}
            onClick={() => {
              setIsEditing(false);
              setShowRejectBox((prev) => !prev);
            }}
          >
            제외 (Reject)
          </button>
        </div>
      </div>

      {/* Inline Non-Optimistic Edit Form (NEVER sends verification or SOURCE_VERIFIED) */}
      {isEditing && !mutationsLocked && (
        <form
          className="subcard"
          data-testid="claim-edit-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!editText.trim()) return;
            onEditClaim({
              claim,
              claim_text: editText.trim(),
              edit_reason: editReason.trim() || "사용자 편집",
            });
            setIsEditing(false);
          }}
        >
          <h4 className="subcard__title">
            Claim 편집 (PATCH /v1/claims/{claim.claim_id} — 새 DRAFT v
            {claim.version + 1} 생성)
          </h4>
          <p className="field-help">
            편집 저장 시 기존 승인·검증 상태는 유지되지 않으며, 새{" "}
            <code className="mono">DRAFT</code> 버전이 생성되어 재검증(
            <code className="mono">/actions/revalidate</code>)이 필요합니다.
          </p>
          <div className="form-field">
            <label htmlFor="claim-edit-textarea" className="field-label">
              Claim 문구
            </label>
            <textarea
              id="claim-edit-textarea"
              className="textarea-input"
              rows={3}
              data-testid="claim-edit-textarea"
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              required
            />
          </div>
          <div className="form-field">
            <label htmlFor="claim-edit-reason-input" className="field-label">
              편집 사유
            </label>
            <input
              id="claim-edit-reason-input"
              type="text"
              className="text-input"
              data-testid="claim-edit-reason-input"
              value={editReason}
              onChange={(e) => setEditReason(e.target.value)}
            />
          </div>
          <div className="action-row">
            <button
              type="submit"
              className="btn btn--primary btn--sm"
              data-testid="claim-edit-submit-button"
              disabled={isBusy || !editText.trim()}
            >
              편집 저장 (새 DRAFT 생성)
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setIsEditing(false)}
            >
              취소
            </button>
          </div>
        </form>
      )}

      {/* Inline Reject Form */}
      {showRejectBox && !mutationsLocked && (
        <div
          className="detail-box detail-box--warning"
          data-testid="claim-reject-form"
        >
          <p className="detail-box__title">Claim 제외 확인 (REJECTED)</p>
          <div className="form-field">
            <label htmlFor="claim-reject-reason-input" className="field-label">
              제외 사유
            </label>
            <input
              id="claim-reject-reason-input"
              type="text"
              className="text-input"
              data-testid="claim-reject-reason-input"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
            />
          </div>
          <div className="action-row">
            <button
              type="button"
              className="btn btn--danger btn--sm"
              data-testid="claim-reject-confirm-button"
              disabled={isBusy || !rejectReason.trim()}
              onClick={() => {
                onRejectClaim({ claim, reason: rejectReason.trim() });
                setShowRejectBox(false);
              }}
            >
              Claim 제외 확정
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setShowRejectBox(false)}
            >
              취소
            </button>
          </div>
        </div>
      )}

      {/* Interactive Provenance Citation Drill-down & Plan C Ephemeral Source Viewer */}
      <ClaimProvenanceDrilldown
        adapter={adapter}
        claim={claim}
        supports={supports}
        isLoadingSupports={isLoadingSupports}
        mutationsLocked={mutationsLocked}
      />
    </article>
  );
}
