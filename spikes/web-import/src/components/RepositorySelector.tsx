"use client";

import React from "react";
import {
  DEFAULT_PLAN_BACKFILL_FROM,
  DEFAULT_PLAN_CUTOFF_AT,
  DEFAULT_PLAN_MAX_OBJECTS,
} from "@/domain/import-status-presenter";
import type {
  GitHubConnectionViewModel,
  PublicRepositoryOption,
  ServiceAuthViewModel,
} from "@/domain/view-models";

interface RepositorySelectorProps {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  workspaceAccessRevoked?: boolean;
  repositories: PublicRepositoryOption[];
  selectedRepoId: string | null;
  isSubmitting: boolean;
  isRunActive: boolean;
  isSelectedRepoSucceeded?: boolean;
  canSubmit: boolean;
  duplicateGuardNotice: string | null;
  onSelectRepo: (repoId: string) => void;
  onSubmitImport: () => void;
}

export function RepositorySelector({
  serviceAuth,
  githubConnection,
  workspaceAccessRevoked = false,
  repositories,
  selectedRepoId,
  isSubmitting,
  isRunActive,
  isSelectedRepoSucceeded = false,
  canSubmit,
  duplicateGuardNotice,
  onSelectRepo,
  onSubmitImport,
}: RepositorySelectorProps) {
  const isConnectionReady =
    !workspaceAccessRevoked &&
    serviceAuth.status === "authenticated" &&
    githubConnection.status === "connected";

  const isLockedByActiveRequest = isSubmitting || isRunActive;

  let blockingReason: string | null = null;
  if (serviceAuth.status === "session_expired") {
    blockingReason =
      "서비스 세션이 만료되어 가져오기를 요청할 수 없습니다. 상단에서 서비스에 다시 로그인하세요.";
  } else if (serviceAuth.status !== "authenticated") {
    blockingReason =
      "Career Memory 모의 서비스에 로그인해야 공개 저장소를 조회하고 가져올 수 있습니다.";
  } else if (workspaceAccessRevoked) {
    blockingReason =
      "현재 Workspace에 대한 접근 권한이 없어 저장소 목록과 가져오기 요청이 닫혔습니다. Career Memory 서비스 로그인은 유지됩니다.";
  } else if (githubConnection.status === "permission_revoked") {
    blockingReason =
      "GitHub 수집 권한이 상실되었습니다. 상단에서 'GitHub 수집 연결 다시 설정'을 먼저 완료하세요.";
  } else if (githubConnection.status !== "connected") {
    blockingReason =
      "GitHub 수집 연결이 필요합니다. 서비스 로그인과 별개로 수집 연결을 완료하세요.";
  }

  return (
    <section
      aria-labelledby="repo-selector-heading"
      className="card"
      aria-busy={isSubmitting}
    >
      <div className="section-header">
        <div>
          <span className="step-kicker">STEP 2</span>
          <h2 id="repo-selector-heading" className="section-title">
            공개 저장소 1곳 선택 및 일회성 최초 가져오기 요청
          </h2>
        </div>
        <p className="section-subtitle">
          최초 가져오기(One-time initial snapshot import)는 선택한 공개 저장소
          1곳만 대상으로 합니다. Event 편집·Claim 생성·LLM 연동·삭제는 이 화면의
          범위가 아닙니다.
        </p>
        <div
          className="inline-alert inline-alert--info"
          data-testid="repo-plan-policy-summary"
        >
          <strong>고정 가져오기 계획 및 보관 원칙:</strong> 공개 저장소 1곳
          Allowlist (<code className="mono">PUBLIC</code> 확인 ·{" "}
          <code className="mono">PUBLIC_CLOUD</code> ·{" "}
          <code className="mono">raw_storage_mode: NONE</code> 원문 비보관) ·
          기본 최근 12개월 고정 기간 (
          <code className="mono">
            [{DEFAULT_PLAN_BACKFILL_FROM}, {DEFAULT_PLAN_CUTOFF_AT}]
          </code>
          ) · 최대 항목 한도{" "}
          <code className="mono">
            max_objects: {DEFAULT_PLAN_MAX_OBJECTS.toLocaleString("en-US")}건
          </code>
        </div>
      </div>

      {!isConnectionReady ? (
        <div
          className="empty-state"
          role="status"
          data-testid="repo-selector-blocked"
        >
          <p className="empty-state__title">저장소 선택 대기 중</p>
          <p className="empty-state__body">{blockingReason}</p>
        </div>
      ) : (
        <fieldset
          className="repo-fieldset"
          disabled={isLockedByActiveRequest}
          aria-describedby="repo-selection-hint"
        >
          <legend className="sr-only">
            최초 가져오기를 실행할 공개 저장소 1곳 선택
          </legend>
          <p id="repo-selection-hint" className="field-help">
            방향키(↑/↓) 또는 Tab·Space로 공개 저장소 한 곳을 선택한 뒤 아래
            버튼을 눌러 요청할 수 있습니다.
          </p>

          <div className="repo-list">
            {repositories.map((repo) => {
              const checked = selectedRepoId === repo.repoId;
              const inputId = `repo-option-${repo.repoId}`;
              return (
                <label
                  key={repo.repoId}
                  htmlFor={inputId}
                  className={`repo-option ${checked ? "repo-option--selected" : ""}`}
                >
                  <input
                    id={inputId}
                    type="radio"
                    name="selected-public-repository"
                    value={repo.repoId}
                    checked={checked}
                    onChange={() => onSelectRepo(repo.repoId)}
                    className="repo-option__radio"
                  />
                  <div className="repo-option__body">
                    <div className="repo-option__top">
                      <span className="repo-option__name mono">
                        {repo.fullName}
                      </span>
                      <div className="repo-option__badges">
                        <span className="badge badge--neutral">
                          {repo.visibility}
                        </span>
                        <span className="badge badge--neutral">
                          {repo.primaryLanguage}
                        </span>
                        <span className="badge badge--neutral mono">
                          {repo.defaultBranch}
                        </span>
                      </div>
                    </div>
                    <p className="repo-option__desc">{repo.description}</p>
                    <p className="repo-option__meta">
                      예상 커밋 {repo.estimatedCommits}건 · PR{" "}
                      {repo.estimatedPullRequests}건 · 최근 푸시{" "}
                      {repo.lastPushedAt}
                    </p>
                    {repo.advisoryNote && (
                      <p className="repo-option__advisory">
                        참고: {repo.advisoryNote}
                      </p>
                    )}
                  </div>
                </label>
              );
            })}
          </div>
        </fieldset>
      )}

      {duplicateGuardNotice && (
        <div
          className="inline-alert inline-alert--warning"
          role="alert"
          aria-live="assertive"
          data-testid="duplicate-guard-alert"
        >
          <strong>중복 실행 방지 안내:</strong> {duplicateGuardNotice}
        </div>
      )}

      <div className="submit-bar">
        <button
          type="button"
          className="btn btn--primary"
          data-testid="start-import-button"
          aria-disabled={!canSubmit}
          aria-busy={isSubmitting}
          disabled={
            isSelectedRepoSucceeded ||
            (!isConnectionReady && !isLockedByActiveRequest)
          }
          onClick={onSubmitImport}
        >
          {isSubmitting
            ? "최초 가져오기 요청 전송 중 (서버 응답 대기)..."
            : isRunActive
              ? "최초 가져오기 실행 중 (중복 요청 방지됨)"
              : isSelectedRepoSucceeded
                ? "선택한 저장소 최초 가져오기 완료 (재요청 불가)"
                : "최초 가져오기 요청"}
        </button>

        <p className="field-help" id="submit-confirm-policy">
          {isSelectedRepoSucceeded
            ? "현재 선택한 저장소의 최초 가져오기가 이미 성공으로 확인되어 새 최초 가져오기 요청이 차단되었습니다. 최신 상태 재조회는 아래 상태 확인 버튼을 사용하거나 다른 공개 저장소를 선택하세요."
            : "버튼을 누르더라도 서버 응답을 받기 전에는 가져오기 시작이 확정된 것으로 표시하지 않습니다."}
        </p>
      </div>
    </section>
  );
}
