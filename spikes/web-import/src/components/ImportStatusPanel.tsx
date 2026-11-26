"use client";

import React from "react";
import {
  canExposeRetryAction,
  hasDerivedStageLag,
} from "@/domain/state-machine";
import type {
  DerivedStageStatus,
  ImportJourneyState,
  ImportRunStatus,
} from "@/domain/view-models";

interface ImportStatusPanelProps {
  state: ImportJourneyState;
  onManualRefresh: () => void;
  onRetryImport: () => void;
  onReconcileAfterFault: () => void;
  onCheckRepositoryStatus?: () => void;
  onReconnectGitHub: () => void;
  onReAuthenticateService: () => void;
}

function getRunStatusBanner(status: ImportRunStatus): {
  badgeText: string;
  headline: string;
  tone: "info" | "success" | "warning" | "danger";
} {
  switch (status) {
    case "running":
      return {
        badgeText: "실행 중",
        headline: "최초 가져오기 작업이 서버에서 실행 중입니다",
        tone: "info",
      };
    case "succeeded":
      return {
        badgeText: "스냅샷 완료",
        headline: "최초 스냅샷 가져오기가 완료되었습니다",
        tone: "success",
      };
    case "partial":
      return {
        badgeText: "부분 결과 — 전체 성공 아님",
        headline: "일부 항목만 수집된 부분 결과입니다 (성공으로 처리되지 않음)",
        tone: "warning",
      };
    case "limit_reached":
      return {
        badgeText: "한도 도달 — 제한됨",
        headline: "수집 한도에 도달하여 전체 가져오기가 완료되지 않았습니다",
        tone: "warning",
      };
    case "failed":
      return {
        badgeText: "가져오기 실패",
        headline: "최초 가져오기에 실패했습니다",
        tone: "danger",
      };
    case "permission_revoked":
      return {
        badgeText: "권한 상실로 중단",
        headline: "GitHub 수집 권한이 회수되어 가져오기가 중단되었습니다",
        tone: "danger",
      };
  }
}

function getDerivedStageLabel(status: DerivedStageStatus): {
  text: string;
  badgeClass: string;
} {
  switch (status) {
    case "ready":
      return { text: "준비 완료", badgeClass: "badge--success" };
    case "in_progress":
      return { text: "진행 중", badgeClass: "badge--info" };
    case "pending":
      return { text: "대기 중", badgeClass: "badge--neutral" };
    case "not_started":
      return { text: "미시작", badgeClass: "badge--neutral" };
    case "skipped":
      return { text: "건너뜀 (미완료)", badgeClass: "badge--warning" };
    case "failed":
      return { text: "실패 (미준비)", badgeClass: "badge--danger" };
  }
}

export function ImportStatusPanel({
  state,
  onManualRefresh,
  onRetryImport,
  onReconcileAfterFault,
  onCheckRepositoryStatus,
  onReconnectGitHub,
  onReAuthenticateService,
}: ImportStatusPanelProps) {
  const { phase, activeRun, pendingActionLabel, unconfirmedFault, recoveryNotice } =
    state;

  const isBusy =
    phase === "bootstrapping" ||
    phase === "submitting_import" ||
    phase === "retrying_import" ||
    phase === "refreshing_status";

  const showRetryButton = canExposeRetryAction(state);
  const derivedLag = hasDerivedStageLag(activeRun);

  return (
    <section
      aria-labelledby="import-status-heading"
      className="card"
      aria-busy={isBusy}
      data-testid="import-status-panel"
    >
      <div className="section-header">
        <div>
          <span className="step-kicker">STEP 3</span>
          <h2 id="import-status-heading" className="section-title">
            가져오기 진행 상태 및 준비 단계 (<code className="mono">import_ready</code>{" "}
            / 파생 단계 구분)
          </h2>
        </div>
        <p className="section-subtitle">
          원본 스냅샷 준비 여부(<code className="mono">import_ready</code>)와
          파생 단계 준비 여부는 분리하여 표시합니다.
        </p>
      </div>

      {/* Reload recovery / 409 existing-run attachment polite announcement */}
      {recoveryNotice && (
        <div
          className="inline-alert inline-alert--info"
          role="status"
          aria-live="polite"
          data-testid="reload-recovery-notice"
        >
          <strong>
            {recoveryNotice.kind === "attached_existing_run"
              ? "기존 실행 이동:"
              : "새로고침 상태 복구:"}
          </strong>{" "}
          {recoveryNotice.message}
        </div>
      )}

      {/* Pending / non-optimistic confirmation status */}
      {(phase === "bootstrapping" ||
        phase === "submitting_import" ||
        phase === "retrying_import" ||
        phase === "refreshing_status") && (
        <div
          className="status-banner status-banner--info"
          role="status"
          aria-live="polite"
          data-testid="pending-status-banner"
        >
          <div className="status-banner__top">
            <span className="badge badge--info">
              {phase === "bootstrapping"
                ? "복구 상태 확인 중"
                : phase === "submitting_import" || phase === "retrying_import"
                  ? "요청 전송 중 — 서버 미확정"
                  : "상태 조회 중"}
            </span>
          </div>
          <p className="status-banner__headline">
            {pendingActionLabel ??
              "서버 응답을 기다리고 있습니다. 응답 수신 전에는 작업이 확정되지 않습니다."}
          </p>
          {(phase === "submitting_import" || phase === "retrying_import") && (
            <p className="status-banner__detail">
              아직 서버 수락 응답을 받지 못했습니다. 중복 요청을 방지하기 위해
              응답이 도착할 때까지 추가 제출이 잠깁니다.
            </p>
          )}
        </div>
      )}

      {/* Unconfirmed transport/session/conflict/empty-body/uncertain-outcome fault */}
      {unconfirmedFault && (
        <div
          className={`status-banner ${
            unconfirmedFault.kind === "uncertain_outcome" ||
            unconfirmedFault.kind === "network_error"
              ? "status-banner--warning"
              : "status-banner--danger"
          }`}
          role="alert"
          aria-live="assertive"
          data-testid="unconfirmed-fault-banner"
          data-fault-kind={unconfirmedFault.kind}
        >
          <div className="status-banner__top">
            <span
              className={`badge ${
                unconfirmedFault.kind === "uncertain_outcome" ||
                unconfirmedFault.kind === "network_error"
                  ? "badge--warning"
                  : "badge--danger"
              }`}
            >
              {unconfirmedFault.kind === "session_expired"
                ? "세션 만료 — 요청 미확정"
                : unconfirmedFault.kind === "network_error"
                  ? "네트워크 실패 — 서버 미확정"
                  : unconfirmedFault.kind === "uncertain_outcome"
                    ? `처리 여부 미확정${unconfirmedFault.httpStatus ? ` (HTTP ${unconfirmedFault.httpStatus})` : ""}`
                    : unconfirmedFault.kind === "request_rejected"
                      ? `요청 처리 불가${unconfirmedFault.httpStatus ? ` (HTTP ${unconfirmedFault.httpStatus})` : ""} — 일반 안내`
                      : "중복 충돌 — 요청 거절"}
            </span>
          </div>
          <p className="status-banner__headline">{unconfirmedFault.title}</p>
          <p className="status-banner__detail">{unconfirmedFault.message}</p>
          <p className="status-banner__guidance">
            <strong>권장 조치:</strong> {unconfirmedFault.actionGuidance}
          </p>

          <div className="action-row">
            {unconfirmedFault.kind === "session_expired" && (
              <button
                type="button"
                className="btn btn--primary btn--sm"
                onClick={onReAuthenticateService}
                disabled={isBusy}
              >
                모의 서비스 다시 로그인
              </button>
            )}
            {(unconfirmedFault.kind === "network_error" ||
              unconfirmedFault.kind === "uncertain_outcome" ||
              unconfirmedFault.kind === "duplicate_conflict" ||
              unconfirmedFault.kind === "request_rejected") && (
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={onReconcileAfterFault}
                disabled={isBusy}
              >
                서버 상태 다시 확인
              </button>
            )}
          </div>
        </div>
      )}

      {/* Idle state when no run exists and not pending */}
      {!activeRun && !isBusy && !unconfirmedFault && (
        <div className="empty-state" role="status" data-testid="import-idle-state">
          <p className="empty-state__title">아직 요청된 최초 가져오기가 없습니다</p>
          <p className="empty-state__body">
            위에서 공개 저장소 1곳을 선택한 뒤 &ldquo;최초 가져오기 요청&rdquo;을
            실행하면 서버 응답 기반 상태가 여기에 표시됩니다.
          </p>
          {onCheckRepositoryStatus &&
            state.serviceAuth.status === "authenticated" &&
            state.githubConnection.status === "connected" &&
            state.selectedRepoId && (
              <div className="action-row">
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  data-testid="check-repository-status-button"
                  onClick={onCheckRepositoryStatus}
                  disabled={isBusy}
                >
                  서버 상태 확인
                </button>
              </div>
            )}
        </div>
      )}

      {/* Active / Terminal Server-Reported Run */}
      {activeRun && (
        <div className="run-container" data-testid="import-run-details">
          {(() => {
            const banner = getRunStatusBanner(activeRun.status);
            const isAlert =
              activeRun.status === "failed" ||
              activeRun.status === "permission_revoked" ||
              activeRun.status === "partial" ||
              activeRun.status === "limit_reached";

            return (
              <div
                className={`status-banner status-banner--${banner.tone}`}
                role={isAlert ? "alert" : "status"}
                aria-live={isAlert ? "assertive" : "polite"}
                data-testid="run-status-banner"
                data-run-status={activeRun.status}
              >
                <div className="status-banner__top">
                  <span
                    className={`badge badge--${banner.tone}`}
                    data-testid="run-status-badge"
                  >
                    {banner.badgeText}
                  </span>
                  <span className="mono run-id-chip">
                    {activeRun.runId}
                    {activeRun.planId ? ` · ${activeRun.planId}` : ""} · 시도 #
                    {activeRun.attemptNumber}
                  </span>
                </div>
                <p className="status-banner__headline">{banner.headline}</p>
                <p className="status-banner__detail">
                  {activeRun.progressMessage}
                </p>
                <p className="status-banner__meta mono">
                  대상 저장소: {activeRun.repositoryFullName} · 최근 갱신:{" "}
                  {activeRun.updatedAt}
                </p>
              </div>
            );
          })()}

          {/* Explicit separation when historical import succeeded (import_ready=true)
              but current downstream processing is BLOCKED_POLICY or BLOCKED_SOURCE */}
          {activeRun.readiness.processingBlockedMessage && (
            <div
              className="status-banner status-banner--warning"
              role="alert"
              aria-live="assertive"
              data-testid="processing-blocked-notice"
            >
              <div className="status-banner__top">
                <span
                  className="badge badge--warning"
                  data-testid="processing-status-badge"
                >
                  현재 처리 차단 ({activeRun.readiness.processingStatus})
                </span>
                <span className="badge badge--success">
                  가져온 이력: SUCCESS (import_ready=true)
                </span>
              </div>
              <p className="status-banner__headline">
                가져온 스냅샷 이력은 성공이나 현재 후속 처리가 차단되어 있습니다
              </p>
              <p className="status-banner__detail">
                {activeRun.readiness.processingBlockedMessage}
              </p>
            </div>
          )}

          {/* Explicit warning if import_ready is true while derived stages are not ready */}
          {derivedLag && !activeRun.readiness.processingBlockedMessage && (
            <div
              className="inline-alert inline-alert--warning"
              role="status"
              aria-live="polite"
              data-testid="derived-lag-notice"
            >
              <strong>준비 단계 구분 안내:</strong> 원본 스냅샷(
              <code className="mono">import_ready=true</code>)은 준비되었으나,
              파생 단계 중 미완료 또는 실패 항목이 있습니다. 전체 파이프라인
              완료와 혼동하지 마세요.
            </div>
          )}

          {/* Two-Layer Readiness Card: Historical Import (import_ready) vs Current Processing */}
          <div className="readiness-grid" data-testid="readiness-breakdown">
            <div className="subcard">
              <div className="subcard__header">
                <h3 className="subcard__title">
                  1단계: 가져온 이력 / 스냅샷 준비 (<code className="mono">import_ready</code>)
                </h3>
                <span
                  className={`badge ${
                    activeRun.readiness.importReadyState === "ready"
                      ? "badge--success"
                      : activeRun.readiness.importReadyState === "partial"
                        ? "badge--warning"
                        : activeRun.readiness.importReadyState === "failed"
                          ? "badge--danger"
                          : "badge--info"
                  }`}
                  data-testid="import-ready-badge"
                >
                  {activeRun.readiness.importReady
                    ? "import_ready: true (준비 완료)"
                    : activeRun.readiness.importReadyState === "partial"
                      ? "import_ready: false (부분 수집 — 미준비)"
                      : "import_ready: false (미준비)"}
                </span>
              </div>
              <p className="subcard__body">
                {activeRun.readiness.importReadySummary}
              </p>
              {(activeRun.processedRange || activeRun.remainingRange) && (
                <dl className="meta-list meta-list--compact">
                  {activeRun.processedRange && (
                    <div>
                      <dt>처리된 범위</dt>
                      <dd className="mono">{activeRun.processedRange}</dd>
                    </div>
                  )}
                  {activeRun.remainingRange && (
                    <div>
                      <dt>남은 범위</dt>
                      <dd className="mono">{activeRun.remainingRange}</dd>
                    </div>
                  )}
                </dl>
              )}
              {activeRun.streams && activeRun.streams.length > 0 && (
                <ul className="bullet-list" data-testid="stream-progress-list">
                  {activeRun.streams.map((stream) => (
                    <li key={`${stream.type}-${stream.status}`} className="mono">
                      stream {stream.type}: {stream.status}
                      {stream.lastCommittedPage !== undefined
                        ? ` · page ${stream.lastCommittedPage}`
                        : ""}
                      {stream.unresolvedFailures !== undefined
                        ? ` · unresolved ${stream.unresolvedFailures}`
                        : ""}
                    </li>
                  ))}
                </ul>
              )}
              {activeRun.summary && (
                <dl className="meta-list meta-list--compact">
                  <div>
                    <dt>수집된 커밋</dt>
                    <dd className="mono">{activeRun.summary.commitsCaptured}건</dd>
                  </div>
                  <div>
                    <dt>수집된 PR</dt>
                    <dd className="mono">
                      {activeRun.summary.pullRequestsCaptured}건
                    </dd>
                  </div>
                </dl>
              )}
            </div>

            <div className="subcard">
              <div className="subcard__header">
                <h3 className="subcard__title">2단계: 현재 후속 처리 상태</h3>
                <span
                  className={`badge ${
                    activeRun.readiness.derivedAllReady
                      ? "badge--success"
                      : activeRun.readiness.processingStatus === "BLOCKED_POLICY" ||
                          activeRun.readiness.processingStatus === "BLOCKED_SOURCE"
                        ? "badge--danger"
                        : "badge--warning"
                  }`}
                  data-testid="derived-ready-badge"
                >
                  {activeRun.readiness.derivedAllReady
                    ? "파생 단계 전체 준비 완료"
                    : activeRun.readiness.processingStatus === "BLOCKED_POLICY"
                      ? "현재 처리 차단 (BLOCKED_POLICY)"
                      : activeRun.readiness.processingStatus === "BLOCKED_SOURCE"
                        ? "현재 처리 차단 (BLOCKED_SOURCE)"
                        : "파생 단계 미완료"}
                </span>
              </div>

              <ul className="stage-list">
                {activeRun.readiness.derivedStages.map((stage) => {
                  const style = getDerivedStageLabel(stage.status);
                  return (
                    <li
                      key={stage.key}
                      className="stage-list__item"
                      data-testid={`derived-stage-${stage.key}`}
                    >
                      <div className="stage-list__row">
                        <span className="stage-list__name">{stage.label}</span>
                        <span className={`badge ${style.badgeClass}`}>
                          {style.text}
                        </span>
                      </div>
                      <p className="stage-list__detail">{stage.detail}</p>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>

          {/* Partial Detail */}
          {activeRun.partialDetail && (
            <div
              className="detail-box detail-box--warning"
              data-testid="partial-detail-box"
            >
              <h3 className="detail-box__title">부분 결과 상세 (누락 항목)</h3>
              <p className="detail-box__summary">
                커밋 {activeRun.partialDetail.importedCommits}건 수집 /{" "}
                <strong>{activeRun.partialDetail.skippedCommits}건 누락</strong> ·
                PR {activeRun.partialDetail.importedPullRequests}건 수집 /{" "}
                <strong>
                  {activeRun.partialDetail.skippedPullRequests}건 누락
                </strong>
              </p>
              <ul className="bullet-list">
                {activeRun.partialDetail.omissionReasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Limit Detail */}
          {activeRun.limitDetail && (
            <div
              className="detail-box detail-box--warning"
              data-testid="limit-detail-box"
            >
              <h3 className="detail-box__title">
                한도 도달 상세: {activeRun.limitDetail.limitLabel}
              </h3>
              <p className="detail-box__summary">
                수집량 {activeRun.limitDetail.capturedCount} / 기준 상한{" "}
                {activeRun.limitDetail.capCount}
                {activeRun.limitDetail.resetAt
                  ? ` · 예상 해제 시각: ${activeRun.limitDetail.resetAt}`
                  : ""}
              </p>
              <p className="detail-box__body">
                {activeRun.limitDetail.explanation}
              </p>
            </div>
          )}

          {/* Error Detail */}
          {activeRun.error && (
            <div
              className="detail-box detail-box--danger"
              data-testid="error-detail-box"
            >
              <h3 className="detail-box__title">
                오류 상세 ({activeRun.error.title})
              </h3>
              <p className="detail-box__meta mono">
                코드(UI Fixture): {activeRun.error.code}
              </p>
              <p className="detail-box__body">{activeRun.error.message}</p>
              <p className="detail-box__guidance">
                <strong>안내:</strong> {activeRun.error.actionGuidance}
              </p>
            </div>
          )}

          {/* Server-Gated Actions (Retry ONLY exposed when server allows!) */}
          <div className="run-actions-panel" data-testid="run-actions-panel">
            <div className="retry-policy-summary">
              <span className="field-label">서버 재시도 정책</span>
              <p className="field-help" data-testid="retry-policy-reason">
                {activeRun.retryPolicy.allowed
                  ? `재시도 허용됨: ${activeRun.retryPolicy.reason}`
                  : `재시도 불가: ${activeRun.retryPolicy.reason}`}
              </p>
            </div>

            <div className="action-row">
              {state.serviceAuth.status === "authenticated" &&
                state.githubConnection.status === "connected" &&
                activeRun.status !== "permission_revoked" && (
                  <button
                    type="button"
                    className="btn btn--secondary btn--sm"
                    data-testid="manual-refresh-status-button"
                    onClick={onManualRefresh}
                    disabled={isBusy}
                  >
                    상태 지금 다시 확인
                  </button>
                )}

              {showRetryButton && (
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  data-testid="retry-import-button"
                  onClick={onRetryImport}
                  disabled={isBusy}
                >
                  가져오기 재시도
                </button>
              )}

              {activeRun.status === "permission_revoked" && (
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  data-testid="reconnect-github-from-run-button"
                  onClick={onReconnectGitHub}
                  disabled={
                    isBusy || state.serviceAuth.status !== "authenticated"
                  }
                >
                  GitHub 수집 연결 다시 설정
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
