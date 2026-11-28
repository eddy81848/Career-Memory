"use client";

import React, { useEffect, useState } from "react";
import type { WorkEventDetailResponse } from "@/domain/event-review-models";
import {
  formatAnchorModeBadge,
  formatEventStatusLabel,
  isEventMutationBlockedByPolicyOrDeletion,
  REVIEW_FLAG_CATALOG,
} from "@/domain/event-review-presenter";

interface EventDetailPanelProps {
  event: WorkEventDetailResponse | null;
  isLoadingDetail: boolean;
  isMutating: boolean;
  inFlightAction: string | null;
  onConfirmKeep: (event: WorkEventDetailResponse) => void;
  onSubmitEdit: (params: {
    event: WorkEventDetailResponse;
    title: string;
    problem: string | null;
    action: string | null;
    outcome: string | null;
    reason: string;
  }) => void;
  onConfirmExclude: (params: {
    event: WorkEventDetailResponse;
    reason: string;
  }) => void;
  onOpenSplitModal: (event: WorkEventDetailResponse) => void;
  onOpenDissolveModal: (event: WorkEventDetailResponse) => void;
}

export function EventDetailPanel({
  event,
  isLoadingDetail,
  isMutating,
  inFlightAction,
  onConfirmKeep,
  onSubmitEdit,
  onConfirmExclude,
  onOpenSplitModal,
  onOpenDissolveModal,
}: EventDetailPanelProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editProblem, setEditProblem] = useState("");
  const [editAction, setEditAction] = useState("");
  const [editOutcome, setEditOutcome] = useState("");
  const [editReason, setEditReason] = useState("사용자 검토 내용 반영");

  const [showExcludeConfirm, setShowExcludeConfirm] = useState(false);
  const [excludeReason, setExcludeReason] = useState(
    "포트폴리오/경력 기록 대상에서 제외"
  );
  const [showMemberSnapshots, setShowMemberSnapshots] = useState(true);

  useEffect(() => {
    setIsEditing(false);
    setShowExcludeConfirm(false);
    if (event) {
      setEditTitle(event.title);
      setEditProblem(event.problem ?? "");
      setEditAction(event.action ?? "");
      setEditOutcome(event.outcome ?? "");
    }
  }, [event?.event_id, event?.version]);

  if (isLoadingDetail && !event) {
    return (
      <div className="subcard" role="status" data-testid="event-detail-loading">
        Event 상세 정보를 서버에서 불러오는 중입니다...
      </div>
    );
  }

  if (!event) {
    return (
      <div className="empty-state" data-testid="event-detail-empty">
        <p className="empty-state__title">검토할 Event를 선택하세요</p>
        <p className="empty-state__body">
          좌측 목록에서 Event 후보를 선택하면 소속 근거(MUST_LINK), 참고
          문맥(CONTEXT_ONLY), 검토 플래그 및 큐레이션 액션을 확인할 수 있습니다.
        </p>
      </div>
    );
  }

  const statusInfo = formatEventStatusLabel(event.status);
  const anchorInfo = formatAnchorModeBadge(event);
  const policyOrDeletionBlocked = isEventMutationBlockedByPolicyOrDeletion(event);
  const canConfirm =
    !policyOrDeletionBlocked &&
    !isMutating &&
    event.allowed_actions.includes("CONFIRM");
  const canEdit =
    !policyOrDeletionBlocked &&
    !isMutating &&
    event.allowed_actions.includes("EDIT");
  const canReject =
    !policyOrDeletionBlocked &&
    !isMutating &&
    event.allowed_actions.includes("REJECT");
  const canSplit =
    !policyOrDeletionBlocked &&
    !isMutating &&
    event.allowed_actions.includes("SPLIT") &&
    event.evidence_memberships.length >= 2;
  const canDissolve =
    !policyOrDeletionBlocked &&
    !isMutating &&
    event.anchor_mode === "USER_COMPOSITE" &&
    event.allowed_actions.includes("DISSOLVE");

  const showDerivedRegenBanner =
    event.anchor_mode === "USER_COMPOSITE" &&
    (event.downstream_impact.processing_status === "FAILED" ||
      event.downstream_impact.processing_status === "RUNNING");

  return (
    <article
      className="card event-detail-panel"
      aria-label="선택된 Event 상세 검토 패널"
      data-testid="event-detail-panel"
      data-event-id={event.event_id}
      data-event-version={event.version}
      data-event-status={event.status}
      data-anchor-mode={event.anchor_mode}
    >
      {/* Top badges & metadata */}
      <div className="event-detail__header">
        <div className="status-banner__badges">
          <span
            className={`badge ${anchorInfo.badgeClass}`}
            data-testid="detail-anchor-badge"
          >
            {anchorInfo.label}
          </span>
          <span
            className={`badge ${statusInfo.badgeClass}`}
            data-testid="detail-status-badge"
          >
            {statusInfo.label}
          </span>
          <span className="badge badge--neutral mono" data-testid="detail-version-badge">
            v{event.version} · ETag {event.etag}
          </span>
          <span className="badge badge--neutral mono">
            {event.created_by === "USER" ? "사용자 생성/편집" : "규칙 후보(RULE)"}
          </span>
        </div>

        <h3 className="event-detail__title" data-testid="detail-event-title">
          {event.title}
        </h3>
        <p className="field-help mono">
          {event.event_id} · 기간: {event.occurred_start_at} ~ {event.occurred_end_at}
          {event.reviewed_at ? ` · 검토 시각: ${event.reviewed_at}` : ""}
        </p>
      </div>

      {/* Policy / Deletion Fence Block Banner */}
      {event.policy_state.status === "POLICY_BLOCKED" && (
        <div
          className="status-banner status-banner--danger"
          role="alert"
          data-testid="detail-policy-blocked-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--danger">
              정책 차단 (POLICY_BLOCKED · 삭제 아님)
            </span>
          </div>
          <p className="status-banner__headline">
            공개→비공개 전환 등 정책 차단으로 편집·확정·분할·병합·해제가 즉시 비활성화되었습니다
          </p>
          <p className="status-banner__detail">
            {event.policy_state.message ??
              "GitHub 원본 삭제가 아니라 공개 정책 확인 실패로 인한 차단입니다. 정책 재확인 후 다시 검토할 수 있습니다."}
          </p>
        </div>
      )}

      {event.policy_state.status === "DELETION_PENDING" && (
        <div
          className="status-banner status-banner--warning"
          role="alert"
          data-testid="detail-deletion-pending-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning">
              삭제 펜스 활성 (DELETION_PENDING · OD-003)
            </span>
          </div>
          <p className="status-banner__headline">
            삭제 작업이 진행 중이어서 모든 큐레이션 변경 액션이 차단되었습니다
          </p>
          <p className="status-banner__detail">
            {event.policy_state.message ??
              "삭제 진행 중에는 확정·편집·제외·분할·병합·해제를 수행할 수 없습니다."}
          </p>
        </div>
      )}

      {/* DIRECT_COMMIT Metadata & Confirmation Notice */}
      {event.anchor_mode === "DIRECT_COMMIT" && event.direct_commit_badge && (
        <div
          className="status-banner status-banner--warning"
          role="status"
          data-testid="direct-commit-detail-box"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning">
              {event.direct_commit_badge.label}
            </span>
            <span className="mono run-id-chip" data-testid="direct-commit-meta-chip">
              default_branch: {event.direct_commit_badge.default_branch} · sha:{" "}
              {event.direct_commit_badge.abbreviated_sha} · committed_at:{" "}
              {event.direct_commit_badge.committed_at}
            </span>
          </div>
          <p className="status-banner__headline">
            PR 연결 미관측(NO_MATCH_OBSERVED) — 사용자 확인 필요
          </p>
          <p className="status-banner__detail">
            공급자가 보고한 기본 브랜치(
            <code className="mono">{event.direct_commit_badge.default_branch}</code>
            )에 직접 반영된 커밋(
            <code className="mono">{event.direct_commit_badge.abbreviated_sha}</code>
            )입니다. PR 연결이 관측되지 않았으나 &ldquo;확정적으로 PR 없음&rdquo;을
            단정하지 않으므로, 실제 작업 단위인지 확인 후{" "}
            <strong>Keep (확정)</strong> 또는 <strong>Exclude (제외)</strong>를
            선택해 주세요.
          </p>
        </div>
      )}

      {/* USER_COMPOSITE Separate Downstream Regeneration Status Banner */}
      {showDerivedRegenBanner && (
        <div
          className={`status-banner ${
            event.downstream_impact.processing_status === "FAILED"
              ? "status-banner--warning"
              : "status-banner--info"
          }`}
          role="status"
          data-testid="composite-regeneration-status-banner"
          data-processing-status={event.downstream_impact.processing_status}
        >
          <div className="status-banner__top">
            <span className="badge badge--success">
              USER_COMPOSITE 병합 커밋 성공
            </span>
            <span
              className={`badge ${
                event.downstream_impact.processing_status === "FAILED"
                  ? "badge--danger"
                  : "badge--info"
              }`}
            >
              파생 재생성 상태: {event.downstream_impact.processing_status}
            </span>
          </div>
          <p className="status-banner__headline">
            {event.downstream_impact.processing_status === "FAILED"
              ? "병합(USER_COMPOSITE) 생성은 성공했으나, 후속 파생 재생성이 부분 실패했습니다"
              : "병합(USER_COMPOSITE) 생성이 완료되었으며 후속 파생 재생성이 진행 중입니다"}
          </p>
          <p className="status-banner__detail">
            {event.downstream_impact.processing_message ??
              "USER_COMPOSITE 병합 결과 자체는 안전하게 저장되었으며, 파생 Fact/Claim 재생성 상태는 별도로 추적됩니다."}
          </p>
        </div>
      )}

      {/* Curation Action Toolbar (6 commands) */}
      <div className="run-actions-panel" data-testid="event-curation-actions">
        <div className="action-row">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            data-testid="action-keep-button"
            disabled={!canConfirm}
            onClick={() => onConfirmKeep(event)}
          >
            {inFlightAction === "confirm" ? "확정 처리 중..." : "Keep (확정)"}
          </button>

          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="action-edit-button"
            disabled={!canEdit}
            onClick={() => {
              setShowExcludeConfirm(false);
              setIsEditing((prev) => !prev);
            }}
          >
            Edit (편집)
          </button>

          <button
            type="button"
            className="btn btn--secondary btn--sm"
            data-testid="action-exclude-button"
            disabled={!canReject}
            onClick={() => {
              setIsEditing(false);
              setShowExcludeConfirm((prev) => !prev);
            }}
          >
            Exclude (제외)
          </button>

          {event.anchor_mode !== "USER_COMPOSITE" && (
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              data-testid="action-split-button"
              disabled={!canSplit}
              onClick={() => onOpenSplitModal(event)}
            >
              Split (범위 분할)
            </button>
          )}

          {event.anchor_mode === "USER_COMPOSITE" && (
            <button
              type="button"
              className="btn btn--danger btn--sm"
              data-testid="action-dissolve-button"
              disabled={!canDissolve}
              onClick={() => onOpenDissolveModal(event)}
            >
              {inFlightAction === "dissolve"
                ? "병합 해제 처리 중..."
                : "Dissolve (병합 해제)"}
            </button>
          )}
        </div>
      </div>

      {/* Inline Edit Form (PATCH /v1/work-events/{id}) */}
      {isEditing && !policyOrDeletionBlocked && (
        <form
          className="subcard"
          data-testid="event-edit-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!editTitle.trim()) return;
            onSubmitEdit({
              event,
              title: editTitle.trim(),
              problem: editProblem.trim() || null,
              action: editAction.trim() || null,
              outcome: editOutcome.trim() || null,
              reason: editReason.trim() || "사용자 편집",
            });
            setIsEditing(false);
          }}
        >
          <h4 className="subcard__title">
            Event 편집 (PATCH — 새 CANDIDATE 버전 생성)
          </h4>
          <div className="form-field">
            <label htmlFor="edit-event-title" className="field-label">
              제목
            </label>
            <input
              id="edit-event-title"
              type="text"
              className="text-input"
              data-testid="edit-title-input"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              required
            />
          </div>
          <div className="form-field">
            <label htmlFor="edit-event-problem" className="field-label">
              문제 (Problem)
            </label>
            <textarea
              id="edit-event-problem"
              className="textarea-input"
              rows={2}
              data-testid="edit-problem-input"
              value={editProblem}
              onChange={(e) => setEditProblem(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="edit-event-action" className="field-label">
              조치 (Action)
            </label>
            <textarea
              id="edit-event-action"
              className="textarea-input"
              rows={2}
              data-testid="edit-action-input"
              value={editAction}
              onChange={(e) => setEditAction(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="edit-event-outcome" className="field-label">
              결과 (Outcome)
            </label>
            <textarea
              id="edit-event-outcome"
              className="textarea-input"
              rows={2}
              data-testid="edit-outcome-input"
              value={editOutcome}
              onChange={(e) => setEditOutcome(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="edit-event-reason" className="field-label">
              편집 사유
            </label>
            <input
              id="edit-event-reason"
              type="text"
              className="text-input"
              value={editReason}
              onChange={(e) => setEditReason(e.target.value)}
            />
          </div>
          <div className="action-row">
            <button
              type="submit"
              className="btn btn--primary btn--sm"
              data-testid="submit-edit-button"
              disabled={isMutating || !editTitle.trim()}
            >
              편집 저장 (새 버전 생성)
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

      {/* Inline Exclude Confirmation (POST /v1/work-events/{id}/actions/reject) */}
      {showExcludeConfirm && !policyOrDeletionBlocked && (
        <div className="detail-box detail-box--warning" data-testid="exclude-confirm-box">
          <h4 className="detail-box__title">Event 제외 확인 (Exclude → REJECTED)</h4>
          <p className="detail-box__body" data-testid="exclude-non-deletion-notice">
            <strong>원본 GitHub 기록 및 출처 메타데이터는 삭제되지 않습니다 (Source 삭제와 별도).</strong>{" "}
            이 Event 후보만 경력 검토 대상에서 제외(REJECTED)됩니다.
          </p>
          <div className="form-field">
            <label htmlFor="exclude-reason-input" className="field-label">
              제외 사유
            </label>
            <input
              id="exclude-reason-input"
              type="text"
              className="text-input"
              data-testid="exclude-reason-input"
              value={excludeReason}
              onChange={(e) => setExcludeReason(e.target.value)}
            />
          </div>
          <div className="action-row">
            <button
              type="button"
              className="btn btn--danger btn--sm"
              data-testid="confirm-exclude-submit-button"
              disabled={isMutating}
              onClick={() => {
                onConfirmExclude({
                  event,
                  reason: excludeReason.trim() || "사용자 제외",
                });
                setShowExcludeConfirm(false);
              }}
            >
              제외 확정 (REJECT)
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setShowExcludeConfirm(false)}
            >
              취소
            </button>
          </div>
        </div>
      )}

      {/* Problem / Action / Outcome Summary */}
      <div className="subcard" data-testid="event-summary-fields">
        <h4 className="subcard__title">작업 요약 (Problem · Action · Outcome)</h4>
        <dl className="meta-list">
          <div>
            <dt>문제 (Problem)</dt>
            <dd>{event.problem ?? "미기재 (Edit으로 보완 가능)"}</dd>
          </div>
          <div>
            <dt>조치 (Action)</dt>
            <dd>{event.action ?? "미기재 (Edit으로 보완 가능)"}</dd>
          </div>
          <div>
            <dt>결과 (Outcome)</dt>
            <dd>{event.outcome ?? "미기재 (Edit으로 보완 가능)"}</dd>
          </div>
        </dl>
      </div>

      {/* Deterministic Review Flags (7 types) */}
      {event.review_flags.length > 0 && (
        <div className="subcard" data-testid="review-flags-section">
          <div className="subcard__header">
            <h4 className="subcard__title">
              결정론적 검토 보조 플래그 ({event.review_flags.length}건)
            </h4>
            <span className="badge badge--neutral">규칙 기반 · LLM 미사용</span>
          </div>
          <ul className="stage-list">
            {event.review_flags.map((flagCode) => {
              const meta = REVIEW_FLAG_CATALOG[flagCode];
              return (
                <li
                  key={flagCode}
                  className="stage-list__item"
                  data-testid={`review-flag-${flagCode}`}
                >
                  <div className="stage-list__row">
                    <span className="stage-list__name">{meta.label}</span>
                    <span className={`badge badge--${meta.tone} mono`}>
                      {meta.code}
                    </span>
                  </div>
                  <p className="stage-list__detail">{meta.description}</p>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* USER_COMPOSITE Member Snapshots Drilldown */}
      {event.anchor_mode === "USER_COMPOSITE" && event.composite_details && (
        <div
          className="subcard composite-members-card"
          data-testid="composite-member-snapshots"
        >
          <div className="subcard__header">
            <div>
              <h4 className="subcard__title">
                포함된 작업 이력 (Member Snapshots ·{" "}
                {event.composite_details.members.length}개)
              </h4>
              <p className="subcard__body">
                피병합 멤버(<code className="mono">SUPERSEDED</code>)는 기본
                목록에서 접히며, 상단 활성 카운트에서는 이{" "}
                <code className="mono">USER_COMPOSITE</code> 1개 논리 Event로만
                집계됩니다.
              </p>
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              data-testid="toggle-member-snapshots-button"
              onClick={() => setShowMemberSnapshots((prev) => !prev)}
            >
              {showMemberSnapshots ? "접기" : "펼치기"}
            </button>
          </div>
          <p className="field-help">
            <strong>병합 사유:</strong> {event.composite_details.merge_reason}
          </p>
          {showMemberSnapshots && (
            <ul className="stage-list" data-testid="composite-member-list">
              {event.composite_details.members.map((member) => (
                <li
                  key={member.member_event_id}
                  className="stage-list__item"
                  data-testid={`composite-member-${member.member_event_id}`}
                >
                  <div className="stage-list__row">
                    <span className="stage-list__name">{member.title}</span>
                    <div className="status-banner__badges">
                      <span className="badge badge--neutral mono">
                        {member.anchor_mode}
                      </span>
                      <span className="badge badge--neutral mono">
                        {member.member_event_id} · 병합 시점 v{member.member_version}
                      </span>
                      <span className="badge badge--neutral mono">
                        {member.current_status}
                      </span>
                    </div>
                  </div>
                  <p className="stage-list__detail mono">
                    병합 전 상태: {member.status_at_merge} · 근거 {member.evidence_count}건
                    {member.direct_commit_badge
                      ? ` · default_branch=${member.direct_commit_badge.default_branch} (${member.direct_commit_badge.abbreviated_sha})`
                      : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* STRICT SEPARATION 1: MUST_LINK Evidence (Top priority) */}
      <section
        className="subcard evidence-must-link"
        aria-labelledby="must-link-heading"
        data-testid="must-link-section"
      >
        <div className="subcard__header">
          <h4 id="must-link-heading" className="subcard__title">
            소속 근거 (MUST_LINK — 이 Event의 직접 증거 ·{" "}
            {event.evidence_memberships.length}건)
          </h4>
          <span className="badge badge--success mono">MUST_LINK</span>
        </div>
        {event.evidence_memberships.length === 0 ? (
          <p className="subcard__body">연결된 직접 근거가 없습니다.</p>
        ) : (
          <ul className="stage-list">
            {event.evidence_memberships.map((item) => (
              <li
                key={item.membership_id}
                className="stage-list__item"
                data-testid={`must-link-item-${item.source_object_id}`}
              >
                <div className="stage-list__row">
                  <span className="stage-list__name">{item.title}</span>
                  <div className="status-banner__badges">
                    <span className="badge badge--success mono">
                      {item.relation_type}
                    </span>
                    <span className="badge badge--neutral mono">
                      {item.provider_object_id} · {item.origin}
                    </span>
                  </div>
                </div>
                {item.subtitle && (
                  <p className="stage-list__detail">{item.subtitle}</p>
                )}
                {item.file_paths && item.file_paths.length > 0 && (
                  <p className="stage-list__detail mono">
                    범위 파일: {item.file_paths.join(", ")}
                    {item.hunk_range ? ` (${item.hunk_range})` : ""}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* STRICT SEPARATION 2: CONTEXT_ONLY (Explicitly separated from MUST_LINK) */}
      <section
        className="subcard context-only-section"
        aria-labelledby="context-only-heading"
        data-testid="context-only-section"
      >
        <div className="subcard__header">
          <h4 id="context-only-heading" className="subcard__title">
            참고 문맥 (Context Only — 소속 근거 아님 ·{" "}
            {event.context_relations.length}건)
          </h4>
          <span className="badge badge--neutral mono">CONTEXT_ONLY</span>
        </div>
        <p className="field-help">
          Issue·티켓 참조 등 참고 문맥은 자동 멤버십이나 직접 소속 근거로 승격되지
          않으며, 서로 다른 PR을 자동 병합하는 데 사용되지 않습니다.
        </p>
        {event.context_relations.length === 0 ? (
          <p className="subcard__body">연결된 참고 문맥 항목이 없습니다.</p>
        ) : (
          <ul className="stage-list">
            {event.context_relations.map((ctx) => (
              <li
                key={ctx.membership_id}
                className="stage-list__item"
                data-testid={`context-only-item-${ctx.source_object_id}`}
              >
                <div className="stage-list__row">
                  <span className="stage-list__name">{ctx.title}</span>
                  <span className="badge badge--neutral mono">
                    {ctx.provider_object_id} · {ctx.relation_type}
                  </span>
                </div>
                <p className="stage-list__detail">{ctx.rationale}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* STRICT SEPARATION 3: CANNOT_LINK Constraints */}
      {event.cannot_link_constraints.constraints.length > 0 && (
        <section
          className="subcard cannot-link-section"
          aria-labelledby="cannot-link-heading"
          data-testid="cannot-link-section"
        >
          <div className="subcard__header">
            <h4 id="cannot-link-heading" className="subcard__title">
              자동 연결 금지 제약 (CANNOT_LINK ·{" "}
              {event.cannot_link_constraints.constraints.length}건)
            </h4>
            <span className="badge badge--danger mono">CANNOT_LINK</span>
          </div>
          <ul className="stage-list">
            {event.cannot_link_constraints.constraints.map((c) => (
              <li key={c.constraint_id} className="stage-list__item">
                <div className="stage-list__row">
                  <span className="stage-list__name">{c.target_label}</span>
                  <span className="badge badge--danger mono">
                    {c.target_type} · {c.target_id}
                  </span>
                </div>
                <p className="stage-list__detail">{c.reason}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Downstream Impact & Version Audit Trail */}
      <div className="readiness-grid">
        <div className="subcard" data-testid="downstream-impact-card">
          <h4 className="subcard__title">파생 산출물 영향 요약</h4>
          <dl className="meta-list meta-list--compact">
            <div>
              <dt>Fact</dt>
              <dd className="mono">
                {event.downstream_impact.fact_count}건
                {event.downstream_impact.stale_fact_count > 0
                  ? ` (STALE ${event.downstream_impact.stale_fact_count})`
                  : ""}
              </dd>
            </div>
            <div>
              <dt>Claim</dt>
              <dd className="mono">
                {event.downstream_impact.claim_count}건
                {event.downstream_impact.stale_claim_count > 0
                  ? ` (STALE ${event.downstream_impact.stale_claim_count})`
                  : ""}
              </dd>
            </div>
            <div>
              <dt>Export</dt>
              <dd className="mono">{event.downstream_impact.export_count}건</dd>
            </div>
          </dl>
        </div>

        <div className="subcard" data-testid="version-history-card">
          <h4 className="subcard__title">
            버전 및 검토 이력 ({event.version_history.length}건)
          </h4>
          <ul className="bullet-list">
            {event.version_history.map((entry) => (
              <li key={`${entry.version}-${entry.action}`} className="mono">
                v{entry.version} · {entry.status} · {entry.action} ({entry.actor})
                {entry.reason ? ` — ${entry.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </article>
  );
}
