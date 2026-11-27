"use client";

import React from "react";
import type {
  GitHubConnectionViewModel,
  ServiceAuthViewModel,
} from "@/domain/view-models";

interface ConnectionStatusPanelProps {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  isBusy: boolean;
  onSignInService: () => void;
  onSignOutService: () => void;
  onSimulateSessionExpired: () => void;
  onConnectGitHub: () => void;
  onDisconnectGitHub: () => void;
  onSimulateGitHubRevoked: () => void;
}

function getAuthBadge(status: ServiceAuthViewModel["status"]): {
  label: string;
  tone: string;
} {
  switch (status) {
    case "authenticated":
      return { label: "로그인 활성", tone: "badge--success" };
    case "authenticating":
      return { label: "로그인 상태 확인 중", tone: "badge--info" };
    case "session_expired":
      return { label: "세션 만료", tone: "badge--danger" };
    case "signed_out":
      return { label: "로그아웃", tone: "badge--neutral" };
  }
}

function getGitHubBadge(status: GitHubConnectionViewModel["status"]): {
  label: string;
  tone: string;
} {
  switch (status) {
    case "connected":
      return { label: "수집 연결됨", tone: "badge--success" };
    case "connecting":
      return { label: "연결 확인 중", tone: "badge--info" };
    case "permission_revoked":
      return { label: "권한 상실", tone: "badge--danger" };
    case "disconnected":
      return { label: "미연결", tone: "badge--warning" };
  }
}

export function ConnectionStatusPanel({
  serviceAuth,
  githubConnection,
  isBusy,
  onSignInService,
  onSignOutService,
  onSimulateSessionExpired,
  onConnectGitHub,
  onDisconnectGitHub,
  onSimulateGitHubRevoked,
}: ConnectionStatusPanelProps) {
  const authBadge = getAuthBadge(serviceAuth.status);
  const ghBadge = getGitHubBadge(githubConnection.status);
  const isAuthenticated = serviceAuth.status === "authenticated";

  return (
    <section
      aria-labelledby="connection-section-heading"
      className="card"
      aria-busy={isBusy}
    >
      <div className="section-header">
        <div>
          <span className="step-kicker">STEP 1</span>
          <h2 id="connection-section-heading" className="section-title">
            서비스 로그인 및 별도 GitHub 수집 연결 상태
          </h2>
        </div>
        <p className="section-subtitle">
          Career Memory 서비스 로그인과 공개 저장소 읽기용 GitHub 수집 연결은
          서로 독립된 상태입니다.
        </p>
      </div>

      <div className="connection-grid">
        {/* Card A: Career Memory Service Auth */}
        <div
          className="subcard"
          data-testid="service-auth-card"
          aria-labelledby="service-auth-heading"
        >
          <div className="subcard__header">
            <h3 id="service-auth-heading" className="subcard__title">
              1-A. Career Memory 서비스 로그인
            </h3>
            <span
              className={`badge ${authBadge.tone}`}
              data-testid="service-auth-badge"
            >
              {authBadge.label}
            </span>
          </div>

          <dl className="meta-list">
            <div>
              <dt>사용자 (합성)</dt>
              <dd>
                {serviceAuth.account
                  ? `${serviceAuth.account.displayName} (${serviceAuth.account.email})`
                  : "로그인된 계정 없음"}
              </dd>
            </div>
            <div>
              <dt>세션 상태 설명</dt>
              <dd>{serviceAuth.sessionNote}</dd>
            </div>
          </dl>

          {serviceAuth.status === "session_expired" && (
            <div className="inline-alert inline-alert--danger" role="alert">
              <strong>서비스 세션 만료 안내:</strong> 서비스 로그인 세션이
              만료되어 저장소 목록 조회 및 가져오기 요청을 실행할 수 없습니다.
              GitHub 수집 연결을 삭제하지 않고 서비스에 다시 로그인하면 복구할 수
              있습니다.
            </div>
          )}

          <div className="action-row">
            {serviceAuth.status !== "authenticated" ? (
              <button
                type="button"
                className="btn btn--primary btn--sm"
                onClick={onSignInService}
                disabled={isBusy}
              >
                {serviceAuth.status === "session_expired"
                  ? "모의 서비스 다시 로그인"
                  : "모의 서비스 로그인"}
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  onClick={onSimulateSessionExpired}
                  disabled={isBusy}
                >
                  세션 만료 시뮬레이션
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={onSignOutService}
                  disabled={isBusy}
                >
                  모의 로그아웃
                </button>
              </>
            )}
          </div>
        </div>

        {/* Card B: Separate GitHub Collection Connection */}
        <div
          className="subcard"
          data-testid="github-connection-card"
          aria-labelledby="github-connection-heading"
        >
          <div className="subcard__header">
            <h3 id="github-connection-heading" className="subcard__title">
              1-B. GitHub 수집 연결 (서비스 로그인과 별도)
            </h3>
            <span
              className={`badge ${ghBadge.tone}`}
              data-testid="github-connection-badge"
            >
              {ghBadge.label}
            </span>
          </div>

          <dl className="meta-list">
            <div>
              <dt>수집 계정 (합성)</dt>
              <dd className="mono">
                {githubConnection.accountHandle ?? "연결된 GitHub 계정 없음"}
              </dd>
            </div>
            <div>
              <dt>OAuth 요청 스코프 (OD-011)</dt>
              <dd className="mono" data-testid="github-oauth-scope">
                scope=&quot;read:user&quot; 단독 요청 · 저장소/쓰기 스코프 0개
              </dd>
            </div>
            <div>
              <dt>수집 권한 범위 및 정책</dt>
              <dd data-testid="github-od011-policy">
                {githubConnection.scopeSummary}
              </dd>
            </div>
            {githubConnection.connectionRef && (
              <div>
                <dt>연결 식별자 (합성)</dt>
                <dd className="mono">{githubConnection.connectionRef}</dd>
              </div>
            )}
          </dl>

          <p className="field-help">
            서비스 로그인(Supabase)과 분리된 GitHub OAuth App 최소 읽기 연결입니다.
            선택한 공개 저장소 1곳 Allowlist 전용이며, 저장소가{" "}
            <code className="mono">Public → Private</code>로 전환되면 삭제 오판
            없이 <code className="mono">POLICY_REVIEW_REQUIRED</code>로 즉시
            차단합니다.
          </p>

          {githubConnection.status === "permission_revoked" && (
            <div className="inline-alert inline-alert--danger" role="alert">
              <strong>GitHub 수집 권한 상실:</strong>{" "}
              {githubConnection.revocationReason ??
                "GitHub 수집 권한이 회수되었습니다. 재연결 전까지 가져오기를 실행할 수 없습니다."}
            </div>
          )}

          {githubConnection.status === "disconnected" && isAuthenticated && (
            <div className="inline-alert inline-alert--warning" role="status">
              서비스에는 로그인되어 있으나 <strong>GitHub 수집 연결이 미완료</strong>
              입니다. 공개 저장소를 선택하려면 별도로 GitHub 수집을 연결하세요.
            </div>
          )}

          <div className="action-row">
            {githubConnection.status !== "connected" ? (
              <button
                type="button"
                className="btn btn--primary btn--sm"
                onClick={onConnectGitHub}
                disabled={isBusy || !isAuthenticated}
              >
                {githubConnection.status === "permission_revoked"
                  ? "GitHub 수집 연결 다시 설정"
                  : "GitHub 수집 연결"}
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn btn--secondary btn--sm"
                  onClick={onSimulateGitHubRevoked}
                  disabled={isBusy}
                >
                  권한 상실 시뮬레이션
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={onDisconnectGitHub}
                  disabled={isBusy}
                >
                  수집 연결 해제
                </button>
              </>
            )}
          </div>

          {!isAuthenticated && (
            <p className="field-help">
              먼저 Career Memory 서비스 로그인을 활성화해야 GitHub 수집 연결을
              변경할 수 있습니다.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
