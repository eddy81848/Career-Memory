"use client";

import React from "react";
import {
  buildDefaultPlanScope,
  describeFailureReason,
  isPolicyOrSourceBlockedRun,
  resolveContractStatus,
} from "@/domain/import-status-presenter";
import {
  canExposeRetryAction,
  hasDerivedStageLag,
  isSelectedRepositoryImportSucceeded,
} from "@/domain/state-machine";
import type {
  ContractImportState,
  DerivedStageStatus,
  ImportJourneyState,
  ImportRunStatus,
} from "@/domain/view-models";

interface ImportStatusPanelProps {
  state: ImportJourneyState;
  canOpenEventInbox?: boolean;
  onEnterEventInbox?: () => void;
  onManualRefresh: () => void;
  onRetryImport: () => void;
  onResendPendingRetry?: () => void;
  onReconcileAfterFault: () => void;
  onCheckRepositoryStatus?: () => void;
  onReconnectGitHub: () => void;
  onReAuthenticateService: () => void;
}

function getRunStatusBanner(
  status: ImportRunStatus,
  contractStatus: ContractImportState
): {
  badgeText: string;
  headline: string;
  tone: "info" | "success" | "warning" | "danger" | "neutral";
} {
  switch (status) {
    case "running":
      if (contractStatus === "PENDING") {
        return {
          badgeText: "대기 중",
          headline: "최초 가져오기 요청이 접수되어 실행 대기 중입니다 (PENDING)",
          tone: "info",
        };
      }
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
    case "canceled":
      return {
        badgeText: "가져오기 취소됨",
        headline: "최초 가져오기 작업이 취소되어 중립 종료되었습니다 (CANCELED)",
        tone: "neutral",
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
  canOpenEventInbox = false,
  onEnterEventInbox,
  onManualRefresh,
  onRetryImport,
  onResendPendingRetry,
  onReconcileAfterFault,
  onCheckRepositoryStatus,
  onReconnectGitHub,
  onReAuthenticateService,
}: ImportStatusPanelProps) {
  const {
    phase,
    activeRun,
    pendingActionLabel,
    unconfirmedFault,
    recoveryNotice,
    pendingRetry,
    workspaceAccessRevoked,
  } = state;

  const isBusy =
    phase === "bootstrapping" ||
    phase === "submitting_import" ||
    phase === "retrying_import" ||
    phase === "refreshing_status";

  const showRetryButton = canExposeRetryAction(state);
  const showResendPendingRetryPanel = Boolean(
    pendingRetry &&
      onResendPendingRetry &&
      !workspaceAccessRevoked &&
      state.serviceAuth.status === "authenticated" &&
      state.githubConnection.status === "connected"
  );
  const derivedLag = hasDerivedStageLag(activeRun);
  const selectedRepoAlreadySucceeded = isSelectedRepositoryImportSucceeded(state);

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

      {/* Unconfirmed transport/session/scope/conflict/empty-body/uncertain-outcome fault */}
      {unconfirmedFault && (
        <div
          className={`status-banner ${
            unconfirmedFault.kind === "uncertain_outcome" ||
            unconfirmedFault.kind === "network_error" ||
            unconfirmedFault.kind === "version_conflict" ||
            unconfirmedFault.kind === "auth_verification_unavailable" ||
            unconfirmedFault.kind === "resource_not_found"
              ? "status-banner--warning"
              : "status-banner--danger"
          }`}
          role="alert"
          aria-live="assertive"
          data-testid="unconfirmed-fault-banner"
          data-fault-kind={unconfirmedFault.kind}
          data-error-code={unconfirmedFault.errorCode ?? ""}
        >
          <div className="status-banner__top">
            <span
              className={`badge ${
                unconfirmedFault.kind === "uncertain_outcome" ||
                unconfirmedFault.kind === "network_error" ||
                unconfirmedFault.kind === "version_conflict" ||
                unconfirmedFault.kind === "auth_verification_unavailable" ||
                unconfirmedFault.kind === "resource_not_found"
                  ? "badge--warning"
                  : "badge--danger"
              }`}
            >
              {unconfirmedFault.kind === "session_expired"
                ? "세션 만료 — 요청 미확정"
                : unconfirmedFault.kind === "workspace_scope_violation"
                  ? "Workspace 접근 권한 없음 (로그인 유지)"
                  : unconfirmedFault.kind === "resource_not_found"
                    ? "대상 확인 불가 (HTTP 404)"
                    : unconfirmedFault.kind === "auth_verification_unavailable"
                      ? "인증 검증 일시 장애 (HTTP 503 · 로그인 유지)"
                      : unconfirmedFault.kind === "version_conflict"
                        ? "실행 상태 변경 충돌 (HTTP 409)"
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
            {!workspaceAccessRevoked &&
              (unconfirmedFault.kind === "network_error" ||
                unconfirmedFault.kind === "uncertain_outcome" ||
                unconfirmedFault.kind === "version_conflict" ||
                unconfirmedFault.kind === "auth_verification_unavailable" ||
                unconfirmedFault.kind === "resource_not_found" ||
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

      {/* Explicit resend panel for a previous unconfirmed retry (separated from new retry on latest failure) */}
      {showResendPendingRetryPanel && pendingRetry && (
        <div
          className="status-banner status-banner--warning"
          role="region"
          aria-label="이전 미확정 재시도 재전송"
          data-testid="pending-retry-resend-panel"
        >
          <div className="status-banner__top">
            <span className="badge badge--warning">
              이전 미확정 재시도 재전송 (별도 POST 동작)
            </span>
            <span className="mono run-id-chip">
              {pendingRetry.runId} · expected_run_version=
              {pendingRetry.expectedRunVersion}
            </span>
          </div>
          <p className="status-banner__headline">
            이전 미확정 재시도 요청을 원래 키와 본문 그대로 다시 보낼 수 있습니다
          </p>
          <p className="status-banner__detail">
            이 버튼은 읽기 전용 상태 조회(GET)가 아니라{" "}
            <strong>동일한 Idempotency-Key와 원래 expected_run_version({pendingRetry.expectedRunVersion}) 본문으로 POST를 다시 전송</strong>
            합니다. 서버가 원래 요청을 이미 접수했다면 추가 작업 없이 기존 202
            응답만 복구하지만, 원래 요청이 접수되지 않았다면 이 재전송이 새
            작업의 최초 접수가 될 수 있습니다.
          </p>
          <div className="action-row">
            <button
              type="button"
              className="btn btn--secondary btn--sm"
              data-testid="resend-pending-retry-button"
              onClick={onResendPendingRetry}
              disabled={isBusy}
            >
              이전 미확정 재시도 그대로 재전송 (v{pendingRetry.expectedRunVersion})
            </button>
          </div>
        </div>
      )}

      {/* Idle state when no run exists and not pending */}
      {!activeRun && !isBusy && !unconfirmedFault && (
        <div className="empty-state" role="status" data-testid="import-idle-state">
          <p className="empty-state__title">
            {selectedRepoAlreadySucceeded
              ? "선택한 저장소의 최초 가져오기가 이미 성공으로 확인되었습니다"
              : "아직 요청된 최초 가져오기가 없습니다"}
          </p>
          <p className="empty-state__body">
            {selectedRepoAlreadySucceeded
              ? "같은 저장소에 새 최초 가져오기를 다시 요청할 수 없습니다. 최신 상태나 후속 처리 차단 여부를 확인하려면 아래 '서버 상태 확인'을 사용하세요."
              : "위에서 공개 저장소 1곳을 선택한 뒤 \"최초 가져오기 요청\"을 실행하면 서버 응답 기반 상태가 여기에 표시됩니다."}
          </p>
          {onCheckRepositoryStatus &&
            !workspaceAccessRevoked &&
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

      {/* Active / Terminal Server-Reported Run (never rendered when session is unauthenticated/expired or Workspace access revoked) */}
      {activeRun &&
        state.serviceAuth.status === "authenticated" &&
        !workspaceAccessRevoked && (() => {
        const contractStatus = resolveContractStatus(activeRun);
        const banner = getRunStatusBanner(activeRun.status, contractStatus);
        const planScope =
          activeRun.planScope ??
          buildDefaultPlanScope(
            state.repositories.find((repo) => repo.repoId === activeRun.repositoryId) ??
              null,
            activeRun.observedAt ?? activeRun.updatedAt
          );
        const policyOrSourceBlocked = isPolicyOrSourceBlockedRun(activeRun);
        const failureReasonInfo = describeFailureReason(
          activeRun.failureReason ?? activeRun.error?.code ?? null,
          activeRun.sourceDrift
        );
        const isTerminalRun =
          activeRun.status !== "running";
        const isNonResumableTerminal =
          !activeRun.retryPolicy.allowed &&
          (activeRun.status === "partial" ||
            activeRun.status === "limit_reached" ||
            activeRun.status === "failed" ||
            activeRun.status === "canceled");
        const isAlert =
          activeRun.status === "failed" ||
          activeRun.status === "permission_revoked" ||
          activeRun.status === "partial" ||
          activeRun.status === "limit_reached";

        return (
        <div className="run-container" data-testid="import-run-details">
          <div
            className={`status-banner status-banner--${banner.tone}`}
            role={isAlert ? "alert" : "status"}
            aria-live={isAlert ? "assertive" : "polite"}
            data-testid="run-status-banner"
            data-run-status={activeRun.status}
            data-contract-status={contractStatus}
          >
            <div className="status-banner__top">
              <div className="status-banner__badges">
                <span
                  className={`badge badge--${banner.tone}`}
                  data-testid="run-status-badge"
                >
                  {banner.badgeText}
                </span>
                <span
                  className="badge badge--neutral mono"
                  data-testid="contract-status-badge"
                >
                  {contractStatus}
                </span>
              </div>
              <span className="mono run-id-chip">
                {activeRun.runId}
                {activeRun.runVersion !== undefined
                  ? ` · v${activeRun.runVersion}`
                  : ""}
                {activeRun.planId ? ` · ${activeRun.planId}` : ""} · 시도 #
                {activeRun.attemptNumber}
              </span>
            </div>
            <p className="status-banner__headline">{banner.headline}</p>
            <p className="status-banner__detail">
              {activeRun.progressMessage}
            </p>
            <p className="status-banner__meta mono">
              대상 저장소: {activeRun.repositoryFullName} · 시작 관찰:{" "}
              {activeRun.startedAt} ·{" "}
              {isTerminalRun
                ? `종료/최신 관찰: ${activeRun.observedAt ?? activeRun.completedAt ?? activeRun.updatedAt}`
                : `최근 갱신: ${activeRun.observedAt ?? activeRun.updatedAt}`}
            </p>
          </div>

          {/* T-205 #1: Repository visibility, fixed plan period, max_objects limit, start/end observation timestamps */}
          <div
            className="subcard"
            data-testid="import-plan-observation-meta"
          >
            <div className="subcard__header">
              <h3 className="subcard__title">
                저장소 공개 상태 · 고정 계획 범위 · 한도 · 관찰 시각
              </h3>
              <span
                className={`badge ${
                  policyOrSourceBlocked ||
                  activeRun.repositoryVisibility === "PRIVATE_OR_RESTRICTED"
                    ? "badge--danger"
                    : "badge--success"
                }`}
                data-testid="repo-visibility-badge"
              >
                {policyOrSourceBlocked ||
                activeRun.repositoryVisibility === "PRIVATE_OR_RESTRICTED"
                  ? "POLICY_REVIEW_REQUIRED (공개 확인 실패)"
                  : "PUBLIC (공개 확인)"}
              </span>
            </div>
            <dl className="plan-meta-grid">
              <div data-testid="repo-visibility-policy">
                <dt>공개 여부 및 원문 보관 정책</dt>
                <dd className="mono">
                  {policyOrSourceBlocked ||
                  activeRun.repositoryVisibility === "PRIVATE_OR_RESTRICTED"
                    ? "공개 상태 확인 실패 / 비공개 전환 의심 (POLICY_REVIEW_REQUIRED)"
                    : "PUBLIC 확인"}
                  {" · "}
                  {planScope.storageClass} · raw_storage_mode:{" "}
                  {planScope.rawStorageMode} (원문 비보관)
                </dd>
              </div>
              <div data-testid="plan-window-and-limit">
                <dt>고정 가져오기 계획 기간 및 한도</dt>
                <dd className="mono">
                  [{planScope.backfillFrom}, {planScope.cutoffAt}] (
                  {planScope.windowLabel}) · max_objects:{" "}
                  {planScope.maxObjects.toLocaleString("en-US")}건
                </dd>
              </div>
              <div data-testid="observation-timestamps">
                <dt>시작 / 종료(최신) 관찰 시각</dt>
                <dd className="mono">
                  시작: {activeRun.startedAt} ·{" "}
                  {isTerminalRun
                    ? `종료 관찰: ${activeRun.completedAt ?? activeRun.updatedAt}`
                    : "종료 관찰: 진행 중 (미종료)"}
                  {activeRun.observedAt
                    ? ` · observed_at: ${activeRun.observedAt}`
                    : ` · updatedAt: ${activeRun.updatedAt}`}
                </dd>
              </div>
            </dl>
          </div>

          {/* T-205 #4 & Snapshot Import §1: Always show non-auto-sync snapshot notice after SUCCESS */}
          {activeRun.status === "succeeded" && (
            <div
              className="inline-alert inline-alert--info"
              role="status"
              data-testid="snapshot-no-auto-sync-notice"
            >
              <strong>일회성 관찰 스냅샷 안내 (자동 동기화 미수행):</strong> 이
              결과는 가져오기 시점의 일회성 관찰 스냅샷이며, 이후 GitHub에서
              발생한 새 커밋·PR·수정·삭제와 자동으로 동기화되지 않습니다(원문은
              저장하지 않으며 근거 검증 시 재조회함).
            </div>
          )}

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
                정책 또는 공개 상태 확인 실패로 원문 열람 · 외부 AI 전송 ·
                Export가 즉시 차단되었습니다
              </p>
              <p className="status-banner__detail">
                {activeRun.readiness.processingBlockedMessage}
              </p>
              <div
                className="blocked-surfaces-row"
                data-testid="blocked-surfaces-badges"
              >
                <span className="badge badge--danger">원문 열람 차단</span>
                <span className="badge badge--danger">외부 AI 전송 차단</span>
                <span className="badge badge--danger">Export 차단</span>
                <span className="badge badge--neutral">
                  삭제 오판 금지 (POLICY_REVIEW_REQUIRED)
                </span>
              </div>
            </div>
          )}

          {/* T-205 #5 & T-201A: Policy/source block notice for non-SUCCESS blocked runs (SOURCE_POLICY_BLOCKED, permission_revoked) */}
          {policyOrSourceBlocked &&
            !activeRun.readiness.processingBlockedMessage && (
              <div
                className="status-banner status-banner--danger"
                role="alert"
                aria-live="assertive"
                data-testid="policy-export-block-notice"
              >
                <div className="status-banner__top">
                  <span className="badge badge--danger">
                    정책·공개 상태 확인 실패 (POLICY_REVIEW_REQUIRED)
                  </span>
                </div>
                <p className="status-banner__headline">
                  정책 또는 공개 상태 확인 실패로 원문 열람 · 외부 AI 전송 ·
                  Export가 즉시 차단되었습니다
                </p>
                <p className="status-banner__detail">
                  저장소가 <code className="mono">Public → Private</code>로
                  전환되었거나 GitHub 수집 권한이 회수되었습니다. GitHub 원본이
                  삭제된 것으로 처리하지 않으며 재확인/재연결이 필요합니다.
                </p>
                <div
                  className="blocked-surfaces-row"
                  data-testid="blocked-surfaces-badges"
                >
                  <span className="badge badge--danger">원문 열람 차단</span>
                  <span className="badge badge--danger">외부 AI 전송 차단</span>
                  <span className="badge badge--danger">Export 차단</span>
                  <span className="badge badge--neutral">
                    삭제 오판 금지 (POLICY_REVIEW_REQUIRED)
                  </span>
                </div>
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
                    <div data-testid="processed-range">
                      <dt>가져온 범위 (processed_range)</dt>
                      <dd className="mono">{activeRun.processedRange}</dd>
                    </div>
                  )}
                  {activeRun.remainingRange && (
                    <div data-testid="remaining-range">
                      <dt>빠진/남은 범위 (remaining_range)</dt>
                      <dd className="mono">{activeRun.remainingRange}</dd>
                    </div>
                  )}
                </dl>
              )}
              {activeRun.streams && activeRun.streams.length > 0 && (
                <div className="stream-section">
                  <span className="field-label">
                    스트림별 마지막 안전 페이지 (last_committed_page) 및 미해결
                    실패
                  </span>
                  <ul className="bullet-list" data-testid="stream-progress-list">
                    {activeRun.streams.map((stream, index) => (
                      <li
                        key={`${stream.type}-${stream.status}-${index}`}
                        className="mono"
                      >
                        stream {stream.type}: {stream.status}
                        {stream.lastCommittedPage !== undefined
                          ? ` · 마지막 확정 커밋 페이지: page ${stream.lastCommittedPage} (last_committed_page=${stream.lastCommittedPage})`
                          : ""}
                        {stream.unresolvedFailures !== undefined
                          ? ` · 미해결 실패: ${stream.unresolvedFailures}건 (unresolved_failures=${stream.unresolvedFailures})`
                          : ""}
                      </li>
                    ))}
                  </ul>
                </div>
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

          {/* T-205 #3: Structured Failure Reason block */}
          {failureReasonInfo && (
            <div
              className={`detail-box ${
                failureReasonInfo.isPolicyOrSourceBlock ||
                activeRun.status === "failed"
                  ? "detail-box--danger"
                  : "detail-box--warning"
              }`}
              data-testid="failure-reason-summary"
              data-failure-code={failureReasonInfo.code}
            >
              <h3 className="detail-box__title">
                구조화된 실패·중단 사유: {failureReasonInfo.label}
              </h3>
              <p className="detail-box__meta mono">
                사유 코드: {failureReasonInfo.code}
                {activeRun.sourceDrift ? " · source_drift: true" : ""}
              </p>
              <p className="detail-box__body">
                {failureReasonInfo.description}
              </p>
            </div>
          )}

          {/* T-205 #2: Dedicated neutral CANCELED detail box */}
          {activeRun.status === "canceled" && (
            <div
              className="detail-box detail-box--neutral"
              data-testid="canceled-detail-box"
            >
              <h3 className="detail-box__title">
                취소 종료 안내 (CANCELED — 일반 오류 FAILED와 구분됨)
              </h3>
              <p className="detail-box__body">
                이 최초 가져오기 실행은 취소 상태(
                <code className="mono">CANCELED</code>)로 종료되었습니다.
                <code className="mono">import_ready=false</code>이며 부분 수집
                결과는 Event/Claim으로 승격되지 않고, 같은 실행에서 단순
                재개할 수 없습니다.
              </p>
            </div>
          )}

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
                수집량 {activeRun.limitDetail.capturedCount.toLocaleString("en-US")}{" "}
                / 기준 상한{" "}
                {activeRun.limitDetail.capCount.toLocaleString("en-US")}
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
              <span className="field-label">서버 재시도 / 같은 실행 재개 정책</span>
              <p className="field-help" data-testid="retry-policy-reason">
                {activeRun.retryPolicy.allowed
                  ? `재개 허용됨: ${activeRun.retryPolicy.reason}`
                  : `재시도 불가: ${activeRun.retryPolicy.reason}`}
              </p>
              {isNonResumableTerminal && (
                <p
                  className="field-help"
                  data-testid="non-resumable-plan-notice"
                >
                  같은 실행에서 단순 재개할 수 없으며(계획 폐기/교체는 현 구간
                  미제공), 부분 수집 결과는 Event/Claim으로 승격되지 않습니다.
                </p>
              )}
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
                  {pendingRetry
                    ? `최신 상태로 새 재시도${
                        activeRun.runVersion !== undefined
                          ? ` (v${activeRun.runVersion})`
                          : ""
                      }`
                    : "같은 계획·마지막 안전 체크포인트에서 재개"}
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

              {canOpenEventInbox && onEnterEventInbox && (
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  data-testid="enter-event-inbox-cta"
                  onClick={onEnterEventInbox}
                  disabled={isBusy}
                >
                  Event 후보 검토하기 (Event Inbox)
                </button>
              )}
            </div>
          </div>
        </div>
        );
      })()}
    </section>
  );
}
