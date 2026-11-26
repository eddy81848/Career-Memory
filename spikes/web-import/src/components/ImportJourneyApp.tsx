"use client";

import React, { useCallback, useEffect, useReducer, useRef } from "react";
import type {
  ImportServiceAdapter,
  PrototypeHarnessControls,
} from "@/adapters/import-service-adapter";
import { MockFixtureImportAdapter } from "@/adapters/mock-fixture-adapter";
import {
  canExposeRetryAction,
  canSubmitInitialImport,
  createInitialImportJourneyState,
  importJourneyReducer,
  isImportRequestLocked,
} from "@/domain/state-machine";
import type { PrototypeScenarioId } from "@/domain/view-models";
import { ConnectionStatusPanel } from "./ConnectionStatusPanel";
import { ImportStatusPanel } from "./ImportStatusPanel";
import { PrototypeScenarioBar } from "./PrototypeScenarioBar";
import { RepositorySelector } from "./RepositorySelector";

export interface ImportJourneyAppProps {
  /** Optional injected adapter for testing or future contract implementation. */
  adapter?: ImportServiceAdapter;
  /** Optional prototype-only controls; omitted when connected to a real backend. */
  harness?: PrototypeHarnessControls;
  /** Polling interval while a run is actively running (ms). */
  pollIntervalMs?: number;
}

export function ImportJourneyApp({
  adapter: injectedAdapter,
  harness: injectedHarness,
  pollIntervalMs = 500,
}: ImportJourneyAppProps) {
  const defaultAdapterRef = useRef<MockFixtureImportAdapter | null>(null);
  if (!injectedAdapter && !defaultAdapterRef.current) {
    defaultAdapterRef.current = new MockFixtureImportAdapter({
      latencyMs: 180,
    });
  }

  const adapter: ImportServiceAdapter =
    injectedAdapter ?? defaultAdapterRef.current!;
  const harness: PrototypeHarnessControls | undefined =
    injectedHarness ??
    (injectedAdapter ? undefined : defaultAdapterRef.current ?? undefined);

  const [state, dispatch] = useReducer(
    importJourneyReducer,
    undefined,
    createInitialImportJourneyState
  );

  // Synchronous locks to prevent rapid double-click / double-Enter races
  const inFlightSubmitRef = useRef(false);
  const inFlightPollRef = useRef(false);
  const requestNonceRef = useRef(0);

  const runBootstrap = useCallback(
    async (simulateReload = false) => {
      dispatch({ type: "BOOTSTRAP_START" });
      const result = await adapter.bootstrapSession({ simulateReload });
      if (result.ok) {
        dispatch({ type: "BOOTSTRAP_SUCCESS", payload: result.data });
      } else {
        dispatch({ type: "BOOTSTRAP_NETWORK_ERROR", fault: result.fault });
      }
    },
    [adapter]
  );

  useEffect(() => {
    void runBootstrap(false);
  }, [runBootstrap]);

  const pollOnce = useCallback(
    async (runId: string, showRefreshingBanner: boolean) => {
      if (inFlightPollRef.current) {
        return;
      }
      inFlightPollRef.current = true;
      if (showRefreshingBanner) {
        dispatch({
          type: "REFRESH_STATUS_START",
          label: `서버에서 작업(${runId}) 최신 상태를 확인하고 있습니다.`,
        });
      }
      try {
        const result = await adapter.pollImportRun(runId);
        if (result.ok) {
          dispatch({
            type: "RUN_UPDATED_FROM_SERVER",
            run: result.data.run,
            githubConnection: result.data.githubConnection,
          });
        } else {
          dispatch({
            type: "UNCONFIRMED_FAULT",
            fault: result.fault,
            serviceAuth: result.serviceAuth,
          });
        }
      } finally {
        inFlightPollRef.current = false;
      }
    },
    [adapter]
  );

  // Auto-poll when a run is actively running and there is no active unconfirmed fault
  useEffect(() => {
    if (
      state.phase !== "ready" ||
      state.serviceAuth.status !== "authenticated" ||
      state.activeRun?.status !== "running" ||
      state.unconfirmedFault !== null
    ) {
      return;
    }

    const activeRunId = state.activeRun.runId;
    const timer = window.setTimeout(() => {
      void pollOnce(activeRunId, false);
    }, pollIntervalMs);

    return () => {
      window.clearTimeout(timer);
    };
  }, [
    state.phase,
    state.serviceAuth.status,
    state.activeRun?.runId,
    state.activeRun?.status,
    state.activeRun?.updatedAt,
    state.unconfirmedFault,
    pollIntervalMs,
    pollOnce,
  ]);

  const handleScenarioChange = (scenarioId: PrototypeScenarioId) => {
    harness?.setScenario(scenarioId);
    dispatch({ type: "SET_SCENARIO", scenarioId });
  };

  const handleSignInService = async () => {
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 로그인 응답을 기다리고 있습니다...",
    });
    const result = await adapter.signInService();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: result.data.latestRun,
        clearFault: true,
      });
    } else {
      dispatch({
        type: "UNCONFIRMED_FAULT",
        fault: result.fault,
        serviceAuth: result.serviceAuth,
      });
    }
  };

  const handleSignOutService = async () => {
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 로그아웃 상태를 반영하는 중입니다...",
    });
    const result = await adapter.signOutService();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        clearFault: true,
      });
    } else {
      dispatch({ type: "UNCONFIRMED_FAULT", fault: result.fault });
    }
  };

  const handleSimulateSessionExpired = async () => {
    if (!harness) return;
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 세션 만료 상태를 반영하는 중입니다...",
    });
    const result = await harness.simulateSessionExpired();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
      });
    }
  };

  const handleConnectGitHub = async () => {
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 연결 응답을 기다리고 있습니다...",
    });
    const result = await adapter.connectGitHub();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: result.data.latestRun,
        clearFault: true,
      });
    } else {
      dispatch({
        type: "UNCONFIRMED_FAULT",
        fault: result.fault,
        serviceAuth: result.serviceAuth,
      });
    }
  };

  const handleDisconnectGitHub = async () => {
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 연결 해제 상태를 반영하는 중입니다...",
    });
    const result = await adapter.disconnectGitHub();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
      });
    }
  };

  const handleSimulateGitHubRevoked = async () => {
    if (!harness) return;
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 권한 상실 상태를 반영하는 중입니다...",
    });
    const result = await harness.simulateGitHubPermissionRevoked();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: result.data.latestRun,
      });
    }
  };

  const handleSubmitImport = async () => {
    // Guard 1: Synchronous in-flight or active-run lock blocks duplicate calls!
    if (inFlightSubmitRef.current || isImportRequestLocked(state)) {
      dispatch({
        type: "DUPLICATE_ATTEMPT_BLOCKED",
        message:
          state.activeRun?.status === "running"
            ? `이미 작업(${state.activeRun.runId})이 실행 중입니다. 중복 실행을 방지했으며 추가 요청을 보내지 않았습니다.`
            : "이미 최초 가져오기 요청을 전송 중입니다. 서버 응답을 기다려 주세요. 중복 요청을 방지했습니다.",
      });
      return;
    }

    if (!canSubmitInitialImport(state) || !state.selectedRepoId) {
      return;
    }

    inFlightSubmitRef.current = true;
    requestNonceRef.current += 1;
    const idempotencyKey = `ui_req_${state.selectedRepoId}_${requestNonceRef.current}`;

    dispatch({
      type: "SUBMIT_IMPORT_START",
      label:
        "최초 가져오기 요청을 서버로 전송 중입니다. 서버 응답을 받기 전에는 시작이 확정되지 않습니다.",
    });

    try {
      const result = await adapter.startInitialImport({
        repoId: state.selectedRepoId,
        idempotencyKey,
      });

      if (result.ok) {
        dispatch({
          type: "RUN_UPDATED_FROM_SERVER",
          run: result.data.run,
          githubConnection: result.data.githubConnection,
          recoveryNotice: null,
        });
      } else {
        dispatch({
          type: "UNCONFIRMED_FAULT",
          fault: result.fault,
          serviceAuth: result.serviceAuth,
        });
      }
    } finally {
      inFlightSubmitRef.current = false;
    }
  };

  const handleRetryImport = async () => {
    if (inFlightSubmitRef.current || !state.activeRun) {
      return;
    }
    if (!canExposeRetryAction(state)) {
      return;
    }

    inFlightSubmitRef.current = true;
    const targetRunId = state.activeRun.runId;
    dispatch({
      type: "RETRY_IMPORT_START",
      label: `작업(${targetRunId}) 재시도 요청을 서버로 전송 중입니다. 서버 수락 응답 전에는 확정되지 않습니다.`,
    });

    try {
      const result = await adapter.retryImportRun(targetRunId);
      if (result.ok) {
        dispatch({
          type: "RUN_UPDATED_FROM_SERVER",
          run: result.data.run,
          githubConnection: result.data.githubConnection,
          recoveryNotice: null,
        });
      } else {
        dispatch({
          type: "UNCONFIRMED_FAULT",
          fault: result.fault,
          serviceAuth: result.serviceAuth,
        });
      }
    } finally {
      inFlightSubmitRef.current = false;
    }
  };

  const handleResetActiveRun = async () => {
    if (inFlightSubmitRef.current || isImportRequestLocked(state)) {
      dispatch({
        type: "DUPLICATE_ATTEMPT_BLOCKED",
        message: "진행 중인 작업이 완료된 후 초기화할 수 있습니다.",
      });
      return;
    }
    const result = await adapter.resetActiveRun();
    if (result.ok) {
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: null,
        clearFault: true,
      });
      dispatch({ type: "RESET_TO_IDLE_SELECTION" });
    }
  };

  const isBusyConnection =
    state.phase === "bootstrapping" || state.phase === "mutating_connection";

  return (
    <div className="page-shell">
      <a href="#main-content" className="skip-link">
        본문으로 건너뛰기
      </a>

      <header className="page-header">
        <div className="page-header__inner">
          <div className="brand-row">
            <span className="brand-mark" aria-hidden="true">
              CM
            </span>
            <div>
              <p className="brand-kicker">Career Memory · Spike Prototype</p>
              <h1 className="page-title">
                GitHub 연결 및 일회성 최초 가져오기
              </h1>
            </div>
          </div>
          <p className="page-lead">
            서비스 로그인과 별도의 GitHub 수집 연결을 확인한 뒤, 공개 저장소
            한 곳의 초기 스냅샷을 요청하고 준비 단계를 추적합니다.
          </p>
        </div>
      </header>

      <main id="main-content" className="main-content" tabIndex={-1}>
        {harness && (
          <PrototypeScenarioBar
            scenarioId={state.scenarioId}
            disabled={
              state.phase === "bootstrapping" ||
              state.phase === "submitting_import" ||
              state.phase === "retrying_import"
            }
            onScenarioChange={handleScenarioChange}
            onSimulateReloadRecovery={() => void runBootstrap(true)}
            onSimulatePollNetworkError={() => {
              harness.simulateNextPollNetworkFailure();
              if (state.activeRun?.status === "running") {
                void pollOnce(state.activeRun.runId, true);
              }
            }}
            onResetPrototype={() => void handleResetActiveRun()}
          />
        )}

        <ConnectionStatusPanel
          serviceAuth={state.serviceAuth}
          githubConnection={state.githubConnection}
          isBusy={isBusyConnection}
          onSignInService={() => void handleSignInService()}
          onSignOutService={() => void handleSignOutService()}
          onSimulateSessionExpired={() => void handleSimulateSessionExpired()}
          onConnectGitHub={() => void handleConnectGitHub()}
          onDisconnectGitHub={() => void handleDisconnectGitHub()}
          onSimulateGitHubRevoked={() => void handleSimulateGitHubRevoked()}
        />

        <RepositorySelector
          serviceAuth={state.serviceAuth}
          githubConnection={state.githubConnection}
          repositories={state.repositories}
          selectedRepoId={state.selectedRepoId}
          isSubmitting={
            state.phase === "submitting_import" ||
            state.phase === "retrying_import"
          }
          isRunActive={state.activeRun?.status === "running"}
          canSubmit={canSubmitInitialImport(state)}
          duplicateGuardNotice={state.duplicateGuardNotice}
          onSelectRepo={(repoId) => dispatch({ type: "SELECT_REPO", repoId })}
          onSubmitImport={() => void handleSubmitImport()}
        />

        <ImportStatusPanel
          state={state}
          onManualRefresh={() => {
            if (state.activeRun) {
              void pollOnce(state.activeRun.runId, true);
            }
          }}
          onRetryImport={() => void handleRetryImport()}
          onReconcileAfterFault={() => void runBootstrap(true)}
          onReconnectGitHub={() => void handleConnectGitHub()}
          onReAuthenticateService={() => void handleSignInService()}
        />

        <footer className="scope-footer">
          <p>
            <strong>범위 경계 안내:</strong> 이 화면은 승인된 API 계약 연결 전
            로컬 UI Fixture 기반 프로토타입입니다. 삭제, Claim 생성, LLM 연동,
            Event 편집 및 실제 외부 GitHub API/토큰 처리는 포함하지 않습니다.
          </p>
        </footer>
      </main>
    </div>
  );
}
