"use client";

import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
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
  isFullSuccessRun,
  isImportRequestLocked,
  isSelectedRepositoryImportSucceeded,
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

  // Monotonic poll tick incremented after each completed "running" poll so
  // the next poll is always scheduled even when runId/status/updatedAt are unchanged.
  const [pollCycle, setPollCycle] = useState(0);

  // Lifecycle, concurrency, and stale-response guards
  const isMountedRef = useRef(true);
  const inFlightSubmitTokenRef = useRef<number | null>(null);
  const inFlightPollRef = useRef(false);
  const pollTimerRef = useRef<number | null>(null);
  const operationTokenRef = useRef(0);
  const activeRunIdRef = useRef<string | null>(null);
  const selectedRepoIdRef = useRef<string | null>(null);
  const isAuthScopeReadyRef = useRef(false);

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const invalidateInFlightOperations = useCallback((): number => {
    clearPollTimer();
    inFlightSubmitTokenRef.current = null;
    operationTokenRef.current += 1;
    return operationTokenRef.current;
  }, [clearPollTimer]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      operationTokenRef.current += 1;
      clearPollTimer();
    };
  }, [clearPollTimer]);

  const runBootstrap = useCallback(
    async (simulateReload = false) => {
      const token = invalidateInFlightOperations();
      const requestedRepoId = selectedRepoIdRef.current;
      dispatch({ type: "BOOTSTRAP_START" });
      const result = await adapter.bootstrapSession({
        simulateReload,
        selectedRepoId: requestedRepoId,
      });
      if (!isMountedRef.current || token !== operationTokenRef.current) {
        return;
      }
      if (result.ok) {
        const isScopeReady =
          !result.data.workspaceAccessRevoked &&
          result.data.serviceAuth.status === "authenticated" &&
          result.data.githubConnection.status === "connected";
        isAuthScopeReadyRef.current = isScopeReady;
        const resolvedRepoId =
          result.data.latestRun?.repositoryId ??
          (result.data.selectedRepoId &&
          result.data.repositories.some(
            (repo) => repo.repoId === result.data.selectedRepoId
          )
            ? result.data.selectedRepoId
            : result.data.repositories[0]?.repoId ?? null);
        selectedRepoIdRef.current = isScopeReady ? resolvedRepoId : null;
        activeRunIdRef.current = isScopeReady
          ? result.data.latestRun?.runId ?? null
          : null;
        dispatch({ type: "BOOTSTRAP_SUCCESS", payload: result.data });
      } else {
        dispatch({ type: "BOOTSTRAP_NETWORK_ERROR", fault: result.fault });
      }
    },
    [adapter, invalidateInFlightOperations]
  );

  useEffect(() => {
    void runBootstrap(false);
  }, [runBootstrap]);

  const pollOnce = useCallback(
    async (runId: string, showRefreshingBanner: boolean) => {
      // Strictly allow only one status poll in flight at a time, and never poll
      // outside an authenticated/connected scope or with a stale runId ref.
      if (
        inFlightPollRef.current ||
        !isAuthScopeReadyRef.current ||
        activeRunIdRef.current !== runId
      ) {
        return;
      }
      // Cancel any pending scheduled timer before starting (e.g. manual refresh).
      clearPollTimer();
      inFlightPollRef.current = true;

      const token = operationTokenRef.current;
      const requestedRepoId = selectedRepoIdRef.current;
      if (showRefreshingBanner) {
        dispatch({
          type: "REFRESH_STATUS_START",
          label: `서버에서 작업(${runId}) 최신 상태를 확인하고 있습니다.`,
        });
      }

      try {
        const result = await adapter.pollImportRun(runId);

        // Discard late/stale responses after unmount, run switch, repo switch, reset, or auth/connection change.
        if (
          !isMountedRef.current ||
          token !== operationTokenRef.current ||
          activeRunIdRef.current !== runId ||
          (requestedRepoId !== null &&
            selectedRepoIdRef.current !== requestedRepoId)
        ) {
          return;
        }

        if (result.ok) {
          const updatedRun = result.data.run;
          const updatedGitHubStatus = result.data.githubConnection?.status;

          // Double-check runId and repository consistency against current selection.
          if (
            activeRunIdRef.current !== updatedRun.runId ||
            (selectedRepoIdRef.current !== null &&
              selectedRepoIdRef.current !== updatedRun.repositoryId)
          ) {
            return;
          }

          if (
            updatedGitHubStatus === "permission_revoked" ||
            updatedGitHubStatus === "disconnected"
          ) {
            isAuthScopeReadyRef.current = false;
          }

          selectedRepoIdRef.current = updatedRun.repositoryId;
          dispatch({
            type: "RUN_UPDATED_FROM_SERVER",
            run: updatedRun,
            githubConnection: result.data.githubConnection,
            pendingRetry: result.data.pendingRetry,
          });

          // Schedule next poll even if runId/status/updatedAt did not change,
          // unless the run reached a non-running state or GitHub permission was revoked.
          if (
            updatedRun.status === "running" &&
            updatedGitHubStatus !== "permission_revoked" &&
            updatedGitHubStatus !== "disconnected"
          ) {
            setPollCycle((cycle) => cycle + 1);
          }
        } else {
          if (
            (result.serviceAuth &&
              result.serviceAuth.status !== "authenticated") ||
            result.workspaceAccessRevoked
          ) {
            isAuthScopeReadyRef.current = false;
            activeRunIdRef.current = null;
            selectedRepoIdRef.current = null;
          } else if (result.clearActiveRun) {
            activeRunIdRef.current = null;
          }
          // Never auto-convert a poll error into a new import request; stop polling
          // and surface the unconfirmed fault for explicit user recovery.
          dispatch({
            type: "UNCONFIRMED_FAULT",
            fault: result.fault,
            serviceAuth: result.serviceAuth,
            workspaceAccessRevoked: result.workspaceAccessRevoked,
            clearActiveRun: result.clearActiveRun,
            pendingRetry: result.pendingRetry,
          });
        }
      } finally {
        inFlightPollRef.current = false;
      }
    },
    [adapter, clearPollTimer]
  );

  // Schedule the next single-flight status poll while actively running.
  // Stops immediately on terminal state, session expiration, Workspace revocation,
  // permission revocation, unconfirmed fault, or screen unmount.
  useEffect(() => {
    clearPollTimer();

    if (
      state.phase !== "ready" ||
      state.workspaceAccessRevoked ||
      state.serviceAuth.status !== "authenticated" ||
      state.githubConnection.status !== "connected" ||
      state.activeRun?.status !== "running" ||
      state.unconfirmedFault !== null
    ) {
      return;
    }

    const activeRunId = state.activeRun.runId;
    pollTimerRef.current = window.setTimeout(() => {
      pollTimerRef.current = null;
      void pollOnce(activeRunId, false);
    }, pollIntervalMs);

    return () => {
      clearPollTimer();
    };
  }, [
    state.phase,
    state.workspaceAccessRevoked,
    state.serviceAuth.status,
    state.githubConnection.status,
    state.activeRun?.runId,
    state.activeRun?.status,
    state.unconfirmedFault,
    pollCycle,
    pollIntervalMs,
    pollOnce,
    clearPollTimer,
  ]);

  const handleScenarioChange = (scenarioId: PrototypeScenarioId) => {
    harness?.setScenario(scenarioId);
    dispatch({ type: "SET_SCENARIO", scenarioId });
  };

  const handleSelectRepo = (repoId: string) => {
    if (isImportRequestLocked(state)) {
      dispatch({ type: "SELECT_REPO", repoId });
      return;
    }
    if (selectedRepoIdRef.current !== repoId) {
      invalidateInFlightOperations();
      selectedRepoIdRef.current = repoId;
      adapter.selectRepository?.(repoId);
      if (state.activeRun && state.activeRun.repositoryId !== repoId) {
        activeRunIdRef.current = null;
      }
    }
    dispatch({ type: "SELECT_REPO", repoId });
  };

  const handleRecoverStatus = useCallback(async () => {
    if (
      !isAuthScopeReadyRef.current ||
      state.workspaceAccessRevoked ||
      state.serviceAuth.status !== "authenticated" ||
      state.githubConnection.status !== "connected"
    ) {
      return;
    }

    const targetRepoId = selectedRepoIdRef.current ?? state.selectedRepoId;
    if (!targetRepoId || !adapter.recoverRepositoryRun) {
      await runBootstrap(true);
      return;
    }

    const knownRunId =
      activeRunIdRef.current &&
      state.activeRun &&
      state.activeRun.runId === activeRunIdRef.current &&
      state.activeRun.repositoryId === targetRepoId
        ? state.activeRun.runId
        : null;

    const token = invalidateInFlightOperations();
    dispatch({
      type: "REFRESH_STATUS_START",
      label: knownRunId
        ? `서버에서 실행(${knownRunId})의 최신 상태를 상세 조회하고 있습니다.`
        : "선택한 저장소의 최근 실행 상태를 서버에서 복구 조회하고 있습니다.",
    });

    const result = await adapter.recoverRepositoryRun({
      repoId: targetRepoId,
      knownRunId,
    });

    // Discard late lookup responses after unmount, repo switch, or session change.
    if (
      !isMountedRef.current ||
      token !== operationTokenRef.current ||
      !isAuthScopeReadyRef.current ||
      selectedRepoIdRef.current !== targetRepoId
    ) {
      return;
    }

    if (result.ok) {
      activeRunIdRef.current = result.data.run?.runId ?? null;
      if (result.data.run) {
        selectedRepoIdRef.current = result.data.run.repositoryId;
      }
      dispatch({
        type: "RECOVERY_LOOKUP_SYNCED",
        run: result.data.run,
        githubConnection: result.data.githubConnection,
        recoveryNotice: result.data.recoveryNotice,
        pendingRetry: result.data.pendingRetry,
      });
    } else {
      if (
        (result.serviceAuth &&
          result.serviceAuth.status !== "authenticated") ||
        result.workspaceAccessRevoked
      ) {
        isAuthScopeReadyRef.current = false;
        activeRunIdRef.current = null;
        selectedRepoIdRef.current = null;
      } else if (result.clearActiveRun) {
        activeRunIdRef.current = null;
      }
      dispatch({
        type: "UNCONFIRMED_FAULT",
        fault: result.fault,
        serviceAuth: result.serviceAuth,
        workspaceAccessRevoked: result.workspaceAccessRevoked,
        clearActiveRun: result.clearActiveRun,
        pendingRetry: result.pendingRetry,
      });
    }
  }, [
    adapter,
    invalidateInFlightOperations,
    runBootstrap,
    state.activeRun,
    state.githubConnection.status,
    state.selectedRepoId,
    state.serviceAuth.status,
    state.workspaceAccessRevoked,
  ]);

  const handleSignInService = async () => {
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 로그인 응답을 기다리고 있습니다...",
    });
    const result = await adapter.signInService();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      const isScopeReady =
        result.data.serviceAuth.status === "authenticated" &&
        result.data.githubConnection.status === "connected";
      isAuthScopeReadyRef.current = isScopeReady;
      const confirmedRun = isScopeReady ? result.data.latestRun ?? null : null;
      activeRunIdRef.current = confirmedRun?.runId ?? null;
      selectedRepoIdRef.current =
        confirmedRun?.repositoryId ??
        (selectedRepoIdRef.current &&
        result.data.repositories.some(
          (repo) => repo.repoId === selectedRepoIdRef.current
        )
          ? selectedRepoIdRef.current
          : result.data.repositories[0]?.repoId ?? null);
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: confirmedRun,
        clearFault: true,
      });
    } else {
      if (
        result.serviceAuth &&
        result.serviceAuth.status !== "authenticated"
      ) {
        isAuthScopeReadyRef.current = false;
        activeRunIdRef.current = null;
        selectedRepoIdRef.current = null;
      }
      dispatch({
        type: "UNCONFIRMED_FAULT",
        fault: result.fault,
        serviceAuth: result.serviceAuth,
      });
    }
  };

  const handleSignOutService = async () => {
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    selectedRepoIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 로그아웃 상태를 반영하는 중입니다...",
    });
    const result = await adapter.signOutService();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      isAuthScopeReadyRef.current = false;
      activeRunIdRef.current = null;
      selectedRepoIdRef.current = null;
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: null,
        clearFault: true,
      });
    } else {
      dispatch({ type: "UNCONFIRMED_FAULT", fault: result.fault });
    }
  };

  const handleSimulateSessionExpired = async () => {
    if (!harness) return;
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    selectedRepoIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "모의 서비스 세션 만료 상태를 반영하는 중입니다...",
    });
    const result = await harness.simulateSessionExpired();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      isAuthScopeReadyRef.current = false;
      activeRunIdRef.current = null;
      selectedRepoIdRef.current = null;
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: result.data.latestRun,
      });
    }
  };

  const handleConnectGitHub = async () => {
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 연결 응답을 기다리고 있습니다...",
    });
    const result = await adapter.connectGitHub();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      const isScopeReady =
        result.data.serviceAuth.status === "authenticated" &&
        result.data.githubConnection.status === "connected";
      isAuthScopeReadyRef.current = isScopeReady;
      const confirmedRun = isScopeReady ? result.data.latestRun ?? null : null;
      activeRunIdRef.current = confirmedRun?.runId ?? null;
      selectedRepoIdRef.current =
        confirmedRun?.repositoryId ??
        (selectedRepoIdRef.current &&
        result.data.repositories.some(
          (repo) => repo.repoId === selectedRepoIdRef.current
        )
          ? selectedRepoIdRef.current
          : result.data.repositories[0]?.repoId ?? null);
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: confirmedRun,
        clearFault: true,
      });
    } else {
      if (
        result.serviceAuth &&
        result.serviceAuth.status !== "authenticated"
      ) {
        isAuthScopeReadyRef.current = false;
        activeRunIdRef.current = null;
        selectedRepoIdRef.current = null;
      }
      dispatch({
        type: "UNCONFIRMED_FAULT",
        fault: result.fault,
        serviceAuth: result.serviceAuth,
      });
    }
  };

  const handleDisconnectGitHub = async () => {
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    selectedRepoIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 연결 해제 상태를 반영하는 중입니다...",
    });
    const result = await adapter.disconnectGitHub();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      isAuthScopeReadyRef.current = false;
      activeRunIdRef.current = null;
      selectedRepoIdRef.current = null;
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: result.data.latestRun,
      });
    }
  };

  const handleSimulateGitHubRevoked = async () => {
    if (!harness) return;
    const token = invalidateInFlightOperations();
    isAuthScopeReadyRef.current = false;
    activeRunIdRef.current = null;
    selectedRepoIdRef.current = null;
    dispatch({
      type: "MUTATION_START",
      label: "GitHub 수집 권한 상실 상태를 반영하는 중입니다...",
    });
    const result = await harness.simulateGitHubPermissionRevoked();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      isAuthScopeReadyRef.current = false;
      const revokedRun =
        result.data.latestRun?.status === "permission_revoked"
          ? result.data.latestRun
          : null;
      activeRunIdRef.current = revokedRun?.runId ?? null;
      selectedRepoIdRef.current = null;
      dispatch({
        type: "AUTH_AND_CONNECTION_SYNCED",
        serviceAuth: result.data.serviceAuth,
        githubConnection: result.data.githubConnection,
        repositories: result.data.repositories,
        latestRun: revokedRun,
      });
    }
  };

  const handleSubmitImport = async () => {
    // Guard 1: Synchronous in-flight or active-run lock blocks duplicate calls!
    if (inFlightSubmitTokenRef.current !== null || isImportRequestLocked(state)) {
      dispatch({
        type: "DUPLICATE_ATTEMPT_BLOCKED",
        message:
          state.activeRun?.status === "running"
            ? `이미 작업(${state.activeRun.runId})이 실행 중입니다. 중복 실행을 방지했으며 추가 요청을 보내지 않았습니다.`
            : "이미 최초 가져오기 요청을 전송 중입니다. 서버 응답을 기다려 주세요. 중복 요청을 방지했습니다.",
      });
      return;
    }

    // Guard 2: Block a new initial import request for the same repository
    // when a succeeded run is already confirmed for the currently selected repo
    // (including when `activeRun` is null after switching A -> B -> A).
    // UI-level UX guard only; backend remains authoritative for duplicate/auth checks.
    if (isSelectedRepositoryImportSucceeded(state)) {
      const matchingSucceededRun =
        state.activeRun &&
        state.activeRun.repositoryId === state.selectedRepoId &&
        isFullSuccessRun(state.activeRun)
          ? state.activeRun
          : null;
      const selectedRepo = state.repositories.find(
        (repo) => repo.repoId === state.selectedRepoId
      );
      const repoLabel =
        matchingSucceededRun?.repositoryFullName ??
        selectedRepo?.fullName ??
        state.selectedRepoId ??
        "선택한 저장소";
      const runSuffix = matchingSucceededRun
        ? `(${matchingSucceededRun.runId})`
        : "";
      dispatch({
        type: "DUPLICATE_ATTEMPT_BLOCKED",
        message: `선택한 저장소(${repoLabel})의 최초 가져오기가 이미 성공${runSuffix}으로 확인되었습니다. 같은 저장소에 새 최초 가져오기를 다시 요청하지 않았습니다.`,
      });
      return;
    }

    if (
      !isAuthScopeReadyRef.current ||
      !canSubmitInitialImport(state) ||
      !state.selectedRepoId
    ) {
      return;
    }

    const token = invalidateInFlightOperations();
    inFlightSubmitTokenRef.current = token;

    dispatch({
      type: "SUBMIT_IMPORT_START",
      label:
        "최초 가져오기 요청을 서버로 전송 중입니다. 서버 응답을 받기 전에는 시작이 확정되지 않습니다.",
    });

    try {
      const result = await adapter.startInitialImport({
        repoId: state.selectedRepoId,
      });

      if (!isMountedRef.current || token !== operationTokenRef.current) {
        return;
      }

      if (result.ok) {
        activeRunIdRef.current = result.data.run.runId;
        selectedRepoIdRef.current = result.data.run.repositoryId;
        dispatch({
          type: "RUN_UPDATED_FROM_SERVER",
          run: result.data.run,
          githubConnection: result.data.githubConnection,
          recoveryNotice: result.data.recoveryNotice ?? null,
          pendingRetry: result.data.pendingRetry,
        });
      } else {
        if (
          (result.serviceAuth &&
            result.serviceAuth.status !== "authenticated") ||
          result.workspaceAccessRevoked
        ) {
          isAuthScopeReadyRef.current = false;
          activeRunIdRef.current = null;
          selectedRepoIdRef.current = null;
        } else if (result.clearActiveRun) {
          activeRunIdRef.current = null;
        }
        dispatch({
          type: "UNCONFIRMED_FAULT",
          fault: result.fault,
          serviceAuth: result.serviceAuth,
          workspaceAccessRevoked: result.workspaceAccessRevoked,
          clearActiveRun: result.clearActiveRun,
          pendingRetry: result.pendingRetry,
        });
      }
    } finally {
      if (inFlightSubmitTokenRef.current === token) {
        inFlightSubmitTokenRef.current = null;
      }
    }
  };

  /**
   * Starts a brand-new retry against the latest observed retryable run state
   * (fresh UUID Idempotency-Key + JSON {"expected_run_version": activeRun.runVersion}).
   * Distinct from `handleResendPendingRetry`.
   */
  const handleRetryImport = async () => {
    if (
      inFlightSubmitTokenRef.current !== null ||
      !isAuthScopeReadyRef.current ||
      state.workspaceAccessRevoked ||
      !state.activeRun ||
      activeRunIdRef.current !== state.activeRun.runId
    ) {
      return;
    }
    if (!canExposeRetryAction(state)) {
      return;
    }

    const token = invalidateInFlightOperations();
    inFlightSubmitTokenRef.current = token;
    const targetRunId = state.activeRun.runId;
    const expectedRunVersion = state.activeRun.runVersion;
    dispatch({
      type: "RETRY_IMPORT_START",
      label: `작업(${targetRunId})의 최신 상태(v${expectedRunVersion ?? "?"})로 새 재시도 요청을 전송 중입니다. 서버 수락 응답 전에는 확정되지 않습니다.`,
    });

    try {
      const result = await adapter.retryImportRun(targetRunId, {
        mode: "new_retry",
        expectedRunVersion,
      });
      if (!isMountedRef.current || token !== operationTokenRef.current) {
        return;
      }
      if (result.ok) {
        activeRunIdRef.current = result.data.run.runId;
        selectedRepoIdRef.current = result.data.run.repositoryId;
        dispatch({
          type: "RUN_UPDATED_FROM_SERVER",
          run: result.data.run,
          githubConnection: result.data.githubConnection,
          recoveryNotice: result.data.recoveryNotice ?? null,
          pendingRetry: result.data.pendingRetry,
        });
      } else {
        if (
          (result.serviceAuth &&
            result.serviceAuth.status !== "authenticated") ||
          result.workspaceAccessRevoked
        ) {
          isAuthScopeReadyRef.current = false;
          activeRunIdRef.current = null;
          selectedRepoIdRef.current = null;
        } else if (result.clearActiveRun) {
          activeRunIdRef.current = null;
        }
        dispatch({
          type: "UNCONFIRMED_FAULT",
          fault: result.fault,
          serviceAuth: result.serviceAuth,
          workspaceAccessRevoked: result.workspaceAccessRevoked,
          clearActiveRun: result.clearActiveRun,
          pendingRetry: result.pendingRetry,
        });
      }
    } finally {
      if (inFlightSubmitTokenRef.current === token) {
        inFlightSubmitTokenRef.current = null;
      }
    }
  };

  /**
   * Explicitly resends the exact previously unconfirmed retry request
   * (same runId, same Idempotency-Key, same original expected_run_version body —
   * never overwritten by intermediate GET status refreshes).
   */
  const handleResendPendingRetry = async () => {
    if (
      inFlightSubmitTokenRef.current !== null ||
      !isAuthScopeReadyRef.current ||
      state.workspaceAccessRevoked ||
      !state.pendingRetry
    ) {
      return;
    }

    const pending = state.pendingRetry;
    const token = invalidateInFlightOperations();
    inFlightSubmitTokenRef.current = token;
    dispatch({
      type: "RETRY_IMPORT_START",
      label: `작업(${pending.runId})의 이전 미확정 재시도(expected_run_version=${pending.expectedRunVersion})를 동일한 키와 본문으로 다시 전송 중입니다.`,
    });

    try {
      const result = await adapter.retryImportRun(pending.runId, {
        mode: "resend_pending",
      });
      if (!isMountedRef.current || token !== operationTokenRef.current) {
        return;
      }
      if (result.ok) {
        activeRunIdRef.current = result.data.run.runId;
        selectedRepoIdRef.current = result.data.run.repositoryId;
        dispatch({
          type: "RUN_UPDATED_FROM_SERVER",
          run: result.data.run,
          githubConnection: result.data.githubConnection,
          recoveryNotice: result.data.recoveryNotice ?? null,
          pendingRetry: result.data.pendingRetry,
        });
      } else {
        if (
          (result.serviceAuth &&
            result.serviceAuth.status !== "authenticated") ||
          result.workspaceAccessRevoked
        ) {
          isAuthScopeReadyRef.current = false;
          activeRunIdRef.current = null;
          selectedRepoIdRef.current = null;
        } else if (result.clearActiveRun) {
          activeRunIdRef.current = null;
        }
        dispatch({
          type: "UNCONFIRMED_FAULT",
          fault: result.fault,
          serviceAuth: result.serviceAuth,
          workspaceAccessRevoked: result.workspaceAccessRevoked,
          clearActiveRun: result.clearActiveRun,
          pendingRetry: result.pendingRetry,
        });
      }
    } finally {
      if (inFlightSubmitTokenRef.current === token) {
        inFlightSubmitTokenRef.current = null;
      }
    }
  };

  const handleResetActiveRun = async () => {
    if (inFlightSubmitTokenRef.current !== null || isImportRequestLocked(state)) {
      dispatch({
        type: "DUPLICATE_ATTEMPT_BLOCKED",
        message: "진행 중인 작업이 완료된 후 초기화할 수 있습니다.",
      });
      return;
    }
    const token = invalidateInFlightOperations();
    dispatch({
      type: "MUTATION_START",
      label: "이전 진행 결과를 초기화하고 있습니다...",
    });
    const result = await adapter.resetActiveRun();
    if (!isMountedRef.current || token !== operationTokenRef.current) {
      return;
    }
    if (result.ok) {
      activeRunIdRef.current = null;
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
          workspaceAccessRevoked={state.workspaceAccessRevoked}
          repositories={state.repositories}
          selectedRepoId={state.selectedRepoId}
          isSubmitting={
            state.phase === "submitting_import" ||
            state.phase === "retrying_import"
          }
          isRunActive={state.activeRun?.status === "running"}
          isSelectedRepoSucceeded={isSelectedRepositoryImportSucceeded(state)}
          canSubmit={canSubmitInitialImport(state)}
          duplicateGuardNotice={state.duplicateGuardNotice}
          onSelectRepo={handleSelectRepo}
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
          onResendPendingRetry={() => void handleResendPendingRetry()}
          onReconcileAfterFault={() => void handleRecoverStatus()}
          onCheckRepositoryStatus={() => void handleRecoverStatus()}
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
