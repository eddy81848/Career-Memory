import { describe, expect, it } from "vitest";
import {
  SYNTHETIC_AUTHENTICATED_AUTH,
  SYNTHETIC_CONNECTED_GITHUB,
  SYNTHETIC_PUBLIC_REPOSITORIES,
  SYNTHETIC_REVOKED_GITHUB,
} from "@/adapters/fixtures";
import {
  describeFailureReason,
  isPolicyOrSourceBlockedRun,
  resolveContractStatus,
} from "./import-status-presenter";
import {
  canExposeRetryAction,
  canSubmitInitialImport,
  createInitialImportJourneyState,
  hasDerivedStageLag,
  importJourneyReducer,
  isFullSuccessRun,
  isImportRequestLocked,
  isSelectedRepositoryImportSucceeded,
} from "./state-machine";
import type { ImportRunViewModel } from "./view-models";

/**
 * NOTE: These unit tests verify pure UI view-model state transitions and
 * display guards using local UI fixtures — NOT HTTP contracts or backend logic.
 */

function makeRun(overrides: Partial<ImportRunViewModel>): ImportRunViewModel {
  return {
    runId: "run_demo_101",
    repositoryId: SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId,
    repositoryFullName: SYNTHETIC_PUBLIC_REPOSITORIES[0].fullName,
    status: "succeeded",
    attemptNumber: 1,
    startedAt: "2026-11-26T12:00:00Z",
    updatedAt: "2026-11-26T12:00:10Z",
    progressMessage: "완료",
    readiness: {
      importReady: true,
      importReadyState: "ready",
      importReadySummary: "원본 스냅샷 완료",
      derivedAllReady: true,
      derivedStages: [
        {
          key: "event_normalization",
          label: "작업 이벤트 정규화",
          status: "ready",
          detail: "완료",
        },
        {
          key: "timeline_indexing",
          label: "타임라인 준비",
          status: "ready",
          detail: "완료",
        },
      ],
    },
    retryPolicy: {
      allowed: false,
      reason: "이미 성공 완료",
      nextAllowedAt: null,
    },
    summary: {
      commitsCaptured: 142,
      pullRequestsCaptured: 28,
      capturedAt: "2026-11-26T12:00:10Z",
    },
    partialDetail: null,
    limitDetail: null,
    error: null,
    ...overrides,
  };
}

describe("importJourneyReducer & domain guards (UI view-model fixtures)", () => {
  it("does NOT optimistically confirm a run when SUBMIT_IMPORT_START is dispatched", () => {
    const bootstrapped = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          latestRun: null,
          scenarioId: "success_full",
          recoveredFromReload: false,
          checkedAt: "2026-11-26T12:00:00Z",
        },
      }
    );

    expect(canSubmitInitialImport(bootstrapped)).toBe(true);

    const submitting = importJourneyReducer(bootstrapped, {
      type: "SUBMIT_IMPORT_START",
      label: "서버 응답 대기 중",
    });

    expect(submitting.phase).toBe("submitting_import");
    expect(submitting.activeRun).toBeNull();
    expect(isImportRequestLocked(submitting)).toBe(true);
    expect(canSubmitInitialImport(submitting)).toBe(false);
  });

  it("exposes retry action ONLY when server retryPolicy.allowed is true and connection is valid", () => {
    const base = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        latestRun: makeRun({
          status: "failed",
          retryPolicy: {
            allowed: true,
            reason: "일시 오류",
            nextAllowedAt: null,
          },
        }),
        scenarioId: "error_retryable",
        recoveredFromReload: false,
        checkedAt: "2026-11-26T12:00:00Z",
      },
    });

    expect(canExposeRetryAction(base)).toBe(true);

    const retryDenied = importJourneyReducer(base, {
      type: "RUN_UPDATED_FROM_SERVER",
      run: makeRun({
        status: "failed",
        retryPolicy: {
          allowed: false,
          reason: "영구 오류",
          nextAllowedAt: null,
        },
      }),
    });
    expect(canExposeRetryAction(retryDenied)).toBe(false);

    const revoked = importJourneyReducer(base, {
      type: "RUN_UPDATED_FROM_SERVER",
      run: makeRun({
        status: "permission_revoked",
        retryPolicy: {
          allowed: true, // Even if misconfigured, permission_revoked must never allow direct retry
          reason: "권한 회수",
          nextAllowedAt: null,
        },
      }),
      githubConnection: SYNTHETIC_REVOKED_GITHUB,
    });
    expect(canExposeRetryAction(revoked)).toBe(false);
  });

  it("never classifies partial, limit_reached, or failed runs as full success", () => {
    expect(isFullSuccessRun(makeRun({ status: "succeeded" }))).toBe(true);
    expect(isFullSuccessRun(makeRun({ status: "partial" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "limit_reached" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "failed" }))).toBe(false);
    expect(isFullSuccessRun(makeRun({ status: "permission_revoked" }))).toBe(
      false
    );
  });

  it("detects when import_ready is true while derived stages are incomplete", () => {
    const derivedPendingRun = makeRun({
      status: "succeeded",
      readiness: {
        importReady: true,
        importReadyState: "ready",
        importReadySummary: "원본 스냅샷 준비 완료",
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "ready",
            detail: "완료",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "failed",
            detail: "실패",
          },
        ],
      },
    });

    expect(hasDerivedStageLag(derivedPendingRun)).toBe(true);
  });

  it("creates reload recovery notice when restoring an active running job", () => {
    const restored = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        latestRun: makeRun({ status: "running" }),
        scenarioId: "running_slow_for_reload",
        recoveredFromReload: true,
        checkedAt: "2026-11-26T12:01:00Z",
      },
    });

    expect(restored.recoveryNotice?.kind).toBe("restored_running");
    expect(restored.recoveryNotice?.message).toContain("중복 요청을 다시 보내지 않고");
    expect(isImportRequestLocked(restored)).toBe(true);
  });

  it("blocks new initial import ONLY for the currently selected succeeded repository, without blocking other repositories or non-success/unconfirmed states", () => {
    const repoA = SYNTHETIC_PUBLIC_REPOSITORIES[0];
    const repoB = SYNTHETIC_PUBLIC_REPOSITORIES[1];

    const succeededSameRepo = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          selectedRepoId: repoA.repoId,
          latestRun: makeRun({
            repositoryId: repoA.repoId,
            repositoryFullName: repoA.fullName,
            status: "succeeded",
          }),
          scenarioId: "success_full",
          recoveredFromReload: false,
          checkedAt: "2026-11-26T12:01:00Z",
        },
      }
    );

    expect(isSelectedRepositoryImportSucceeded(succeededSameRepo)).toBe(true);
    expect(canSubmitInitialImport(succeededSameRepo)).toBe(false);

    // Switching to another repository clears the single-repo guard and allows submit
    const switchedToRepoB = importJourneyReducer(succeededSameRepo, {
      type: "SELECT_REPO",
      repoId: repoB.repoId,
    });
    expect(isSelectedRepositoryImportSucceeded(switchedToRepoB)).toBe(false);
    expect(canSubmitInitialImport(switchedToRepoB)).toBe(true);

    // Even if activeRun still points to repoA while selectedRepoId is repoB, repoB is NOT blocked
    const differentRepoState = {
      ...succeededSameRepo,
      selectedRepoId: repoB.repoId,
    };
    expect(isSelectedRepositoryImportSucceeded(differentRepoState)).toBe(false);
    expect(canSubmitInitialImport(differentRepoState)).toBe(true);

    // Partial, limit_reached, failed, and incomplete (importReady=false) MUST NOT be treated as succeeded
    const cleanBootstrapped = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          selectedRepoId: repoA.repoId,
          latestRun: null,
          scenarioId: "success_full",
          recoveredFromReload: false,
          checkedAt: "2026-11-26T12:00:00Z",
        },
      }
    );

    for (const nonSuccessRun of [
      makeRun({
        status: "partial",
        readiness: {
          importReady: false,
          importReadyState: "partial",
          importReadySummary: "부분 수집",
          derivedAllReady: false,
          derivedStages: [],
        },
      }),
      makeRun({
        status: "limit_reached",
        readiness: {
          importReady: false,
          importReadyState: "partial",
          importReadySummary: "한도 도달",
          derivedAllReady: false,
          derivedStages: [],
        },
      }),
      makeRun({
        status: "failed",
        readiness: {
          importReady: false,
          importReadyState: "failed",
          importReadySummary: "실패",
          derivedAllReady: false,
          derivedStages: [],
        },
      }),
      makeRun({
        status: "succeeded",
        readiness: {
          importReady: false,
          importReadyState: "pending",
          importReadySummary: "미확정",
          derivedAllReady: false,
          derivedStages: [],
        },
      }),
    ]) {
      const nonSuccessState = importJourneyReducer(cleanBootstrapped, {
        type: "RUN_UPDATED_FROM_SERVER",
        run: nonSuccessRun,
      });
      expect(isSelectedRepositoryImportSucceeded(nonSuccessState)).toBe(false);
      expect(canSubmitInitialImport(nonSuccessState)).toBe(true);
    }

    // Unconfirmed fault with no confirmed run MUST NOT be treated as succeeded
    const unconfirmedState = importJourneyReducer(switchedToRepoB, {
      type: "UNCONFIRMED_FAULT",
      fault: {
        kind: "uncertain_outcome",
        httpStatus: 502,
        title: "처리 여부 미확정",
        message: "응답을 확인할 수 없습니다.",
        serverConfirmed: false,
        actionGuidance: "서버 상태를 다시 확인하세요.",
      },
    });
    expect(isSelectedRepositoryImportSucceeded(unconfirmedState)).toBe(false);
    expect(canSubmitInitialImport(unconfirmedState)).toBe(true);
  });

  it("remembers A's confirmed success across A -> B -> A repository switching (even when activeRun is null) and clears it on auth/permission scope change", () => {
    const repoA = SYNTHETIC_PUBLIC_REPOSITORIES[0];
    const repoB = SYNTHETIC_PUBLIC_REPOSITORIES[1];

    const stateA = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        selectedRepoId: repoA.repoId,
        latestRun: makeRun({
          runId: "run_a_succeeded",
          repositoryId: repoA.repoId,
          repositoryFullName: repoA.fullName,
          status: "succeeded",
        }),
        scenarioId: "success_full",
        recoveredFromReload: false,
        checkedAt: "2026-11-26T12:01:00Z",
      },
    });

    expect(isSelectedRepositoryImportSucceeded(stateA)).toBe(true);
    expect(canSubmitInitialImport(stateA)).toBe(false);

    // Switch A -> B: B is NOT blocked and does not inherit A's run details
    const stateB = importJourneyReducer(stateA, {
      type: "SELECT_REPO",
      repoId: repoB.repoId,
    });
    expect(stateB.selectedRepoId).toBe(repoB.repoId);
    expect(stateB.activeRun).toBeNull();
    expect(isSelectedRepositoryImportSucceeded(stateB)).toBe(false);
    expect(canSubmitInitialImport(stateB)).toBe(true);

    // Switch B -> A: activeRun is null, yet A's confirmed success MUST still block re-submit
    const stateAReselected = importJourneyReducer(stateB, {
      type: "SELECT_REPO",
      repoId: repoA.repoId,
    });
    expect(stateAReselected.selectedRepoId).toBe(repoA.repoId);
    expect(stateAReselected.activeRun).toBeNull();
    expect(isSelectedRepositoryImportSucceeded(stateAReselected)).toBe(true);
    expect(canSubmitInitialImport(stateAReselected)).toBe(false);

    // Auth/session change (logout -> re-login) MUST clear prior scope's succeeded record
    const signedOut = importJourneyReducer(stateAReselected, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: {
        status: "signed_out",
        account: null,
        sessionNote: "로그아웃",
        lastCheckedAt: "2026-11-26T12:02:00Z",
      },
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: [],
      latestRun: null,
      clearFault: true,
    });
    const signedInNewScope = importJourneyReducer(signedOut, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
      latestRun: null,
      clearFault: true,
    });
    expect(isSelectedRepositoryImportSucceeded(signedInNewScope)).toBe(false);
    expect(canSubmitInitialImport(signedInNewScope)).toBe(true);

    // GitHub permission revocation MUST also clear prior scope's succeeded record
    const revoked = importJourneyReducer(stateAReselected, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
      githubConnection: SYNTHETIC_REVOKED_GITHUB,
      repositories: [],
      latestRun: null,
    });
    const reconnected = importJourneyReducer(revoked, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
      latestRun: null,
      clearFault: true,
    });
    expect(isSelectedRepositoryImportSucceeded(reconnected)).toBe(false);
    expect(canSubmitInitialImport(reconnected)).toBe(true);
  });

  it("clears previous activeRun, succeededRepoIds, and recoveryNotice on session expiry or account/connection change even when latestRun is omitted, while keeping temporary auth outage distinct from logout", () => {
    const repoA = SYNTHETIC_PUBLIC_REPOSITORIES[0];
    const account1 = {
      syntheticUserId: "user_acct_1",
      displayName: "Account One",
      email: "one@example.invalid",
    };
    const account2 = {
      syntheticUserId: "user_acct_2",
      displayName: "Account Two",
      email: "two@example.invalid",
    };

    // 1. Account 1 succeeds on repo A (with a recovery notice present)
    const acct1Succeeded = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: {
            ...SYNTHETIC_AUTHENTICATED_AUTH,
            account: account1,
          },
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          selectedRepoId: repoA.repoId,
          latestRun: makeRun({
            runId: "run_acct1_a",
            repositoryId: repoA.repoId,
            repositoryFullName: repoA.fullName,
            status: "succeeded",
          }),
          scenarioId: "success_full",
          recoveredFromReload: true,
          recoveryLookupMode: "detail_get",
          checkedAt: "2026-11-26T12:01:00Z",
        },
      }
    );

    expect(acct1Succeeded.activeRun?.runId).toBe("run_acct1_a");
    expect(acct1Succeeded.recoveryNotice).not.toBeNull();
    expect(isSelectedRepositoryImportSucceeded(acct1Succeeded)).toBe(true);
    expect(canSubmitInitialImport(acct1Succeeded)).toBe(false);

    // 2. Confirmed session expiration with `latestRun` omitted -> previous run/success/notice MUST be cleared
    const expiredState = importJourneyReducer(acct1Succeeded, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: {
        status: "session_expired",
        account: null,
        sessionNote: "세션 만료",
        lastCheckedAt: "2026-11-26T12:02:00Z",
      },
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: [],
      // latestRun intentionally omitted!
    });

    expect(expiredState.activeRun).toBeNull();
    expect(expiredState.succeededRepoIds).toEqual([]);
    expect(expiredState.recoveryNotice).toBeNull();
    expect(isSelectedRepositoryImportSucceeded(expiredState)).toBe(false);

    // 3. Account 2 signs in with `latestRun` omitted -> MUST NOT inherit Account 1's run or success!
    const acct2NoLatestRun = importJourneyReducer(expiredState, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: {
        status: "authenticated",
        account: account2,
        sessionNote: "새 계정 로그인",
        lastCheckedAt: "2026-11-26T12:03:00Z",
      },
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
      clearFault: true,
      // latestRun intentionally omitted!
    });

    expect(acct2NoLatestRun.activeRun).toBeNull();
    expect(acct2NoLatestRun.succeededRepoIds).toEqual([]);
    expect(acct2NoLatestRun.recoveryNotice).toBeNull();
    expect(isSelectedRepositoryImportSucceeded(acct2NoLatestRun)).toBe(false);
    expect(canSubmitInitialImport(acct2NoLatestRun)).toBe(true);

    // 4. Direct scope switch (Account 1 -> Account 2) with `latestRun` omitted -> MUST NOT inherit!
    const directSwitch = importJourneyReducer(acct1Succeeded, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: {
        status: "authenticated",
        account: account2,
        sessionNote: "계정 전환",
        lastCheckedAt: "2026-11-26T12:03:30Z",
      },
      githubConnection: {
        ...SYNTHETIC_CONNECTED_GITHUB,
        connectionRef: "gh_conn_acct2",
      },
      repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
      clearFault: true,
    });
    expect(directSwitch.activeRun).toBeNull();
    expect(directSwitch.succeededRepoIds).toEqual([]);
    expect(directSwitch.recoveryNotice).toBeNull();
    expect(canSubmitInitialImport(directSwitch)).toBe(true);

    // 5. New scope WITH explicit confirmed `latestRun` -> uses ONLY the new confirmed run
    const acct2WithOwnRun = importJourneyReducer(expiredState, {
      type: "AUTH_AND_CONNECTION_SYNCED",
      serviceAuth: {
        status: "authenticated",
        account: account2,
        sessionNote: "새 계정 로그인",
        lastCheckedAt: "2026-11-26T12:04:00Z",
      },
      githubConnection: SYNTHETIC_CONNECTED_GITHUB,
      repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
      latestRun: makeRun({
        runId: "run_acct2_own",
        repositoryId: SYNTHETIC_PUBLIC_REPOSITORIES[1].repoId,
        repositoryFullName: SYNTHETIC_PUBLIC_REPOSITORIES[1].fullName,
        status: "running",
        readiness: {
          importReady: false,
          importReadyState: "in_progress",
          importReadySummary: "진행 중",
          derivedAllReady: false,
          derivedStages: [],
        },
      }),
      clearFault: true,
    });
    expect(acct2WithOwnRun.activeRun?.runId).toBe("run_acct2_own");
    expect(acct2WithOwnRun.succeededRepoIds).toEqual([]);

    // 6. Confirmed 401 via UNCONFIRMED_FAULT clears activeRun, succeededRepoIds, and recoveryNotice
    const fault401 = importJourneyReducer(acct1Succeeded, {
      type: "UNCONFIRMED_FAULT",
      serviceAuth: {
        status: "session_expired",
        account: null,
        sessionNote: "401 세션 만료",
        lastCheckedAt: "2026-11-26T12:05:00Z",
      },
      fault: {
        kind: "session_expired",
        httpStatus: 401,
        title: "세션 만료",
        message: "다시 로그인하세요.",
        serverConfirmed: false,
        actionGuidance: "재로그인",
      },
    });
    expect(fault401.serviceAuth.status).toBe("session_expired");
    expect(fault401.activeRun).toBeNull();
    expect(fault401.succeededRepoIds).toEqual([]);
    expect(fault401.recoveryNotice).toBeNull();

    // 7. Temporary auth service outage (503, no confirmed session_expired) MUST NOT log the user out
    const tempAuthOutage = importJourneyReducer(acct1Succeeded, {
      type: "UNCONFIRMED_FAULT",
      fault: {
        kind: "uncertain_outcome",
        httpStatus: 503,
        title: "인증 서비스 일시 장애 (HTTP 503)",
        message: "세션 확인이 일시적으로 지연되어 요청을 중단했습니다.",
        serverConfirmed: false,
        actionGuidance: "잠시 후 상태를 다시 확인하세요.",
      },
    });
    expect(tempAuthOutage.serviceAuth.status).toBe("authenticated");
    expect(tempAuthOutage.unconfirmedFault?.httpStatus).toBe(503);
    expect(tempAuthOutage.succeededRepoIds).toEqual([repoA.repoId]);
  });

  it("handles 0.5.0-draft workspace scope violation, 404 clearActiveRun, 503 auth outage, and pendingRetry lifecycle across GET and target/scope switches", () => {
    const repoA = SYNTHETIC_PUBLIC_REPOSITORIES[0];
    const repoB = SYNTHETIC_PUBLIC_REPOSITORIES[1];

    const retryableBase = importJourneyReducer(createInitialImportJourneyState(), {
      type: "BOOTSTRAP_SUCCESS",
      payload: {
        serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
        githubConnection: SYNTHETIC_CONNECTED_GITHUB,
        repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
        selectedRepoId: repoA.repoId,
        latestRun: makeRun({
          runId: "run_v_1",
          runVersion: 3,
          repositoryId: repoA.repoId,
          status: "partial",
          readiness: {
            importReady: false,
            importReadyState: "partial",
            importReadySummary: "부분 수집",
            derivedAllReady: false,
            derivedStages: [],
          },
          retryPolicy: {
            allowed: true,
            fromSafeCheckpoint: true,
            reason: "재시도 허용",
            nextAllowedAt: null,
          },
        }),
        scenarioId: "partial_retryable",
        recoveredFromReload: false,
        checkedAt: "2026-11-27T09:00:00Z",
      },
    });

    // 1. Lost retry sets pendingRetry (v3) -> intermediate GET to Failure B (v5) MUST preserve pendingRetry (v3)
    const afterLostRetry = importJourneyReducer(retryableBase, {
      type: "UNCONFIRMED_FAULT",
      fault: {
        kind: "network_error",
        title: "재시도 응답 미수신",
        message: "응답 유실",
        serverConfirmed: false,
        actionGuidance: "GET 확인",
      },
      pendingRetry: { runId: "run_v_1", expectedRunVersion: 3 },
    });
    expect(afterLostRetry.pendingRetry).toEqual({
      runId: "run_v_1",
      expectedRunVersion: 3,
    });

    const afterGetFailureB = importJourneyReducer(afterLostRetry, {
      type: "RUN_UPDATED_FROM_SERVER",
      run: makeRun({
        runId: "run_v_1",
        runVersion: 5,
        repositoryId: repoA.repoId,
        status: "partial",
        retryPolicy: {
          allowed: true,
          fromSafeCheckpoint: true,
          reason: "재시도 허용",
          nextAllowedAt: null,
        },
      }),
    });
    expect(afterGetFailureB.activeRun?.runVersion).toBe(5);
    expect(afterGetFailureB.pendingRetry).toEqual({
      runId: "run_v_1",
      expectedRunVersion: 3,
    });

    // Switching to another repository clears pendingRetry
    const switchedTarget = importJourneyReducer(afterGetFailureB, {
      type: "SELECT_REPO",
      repoId: repoB.repoId,
    });
    expect(switchedTarget.pendingRetry).toBeNull();

    // 2. 404 RESOURCE_NOT_FOUND clears activeRun & pendingRetry while keeping login + repo list
    const after404 = importJourneyReducer(afterGetFailureB, {
      type: "UNCONFIRMED_FAULT",
      clearActiveRun: true,
      fault: {
        kind: "resource_not_found",
        httpStatus: 404,
        errorCode: "RESOURCE_NOT_FOUND",
        title: "대상 확인 불가",
        message: "중립 안내",
        serverConfirmed: false,
        actionGuidance: "확인",
      },
    });
    expect(after404.serviceAuth.status).toBe("authenticated");
    expect(after404.repositories).toHaveLength(SYNTHETIC_PUBLIC_REPOSITORIES.length);
    expect(after404.activeRun).toBeNull();
    expect(after404.pendingRetry).toBeNull();

    // 3. 503 AUTH_VERIFICATION_UNAVAILABLE keeps login active and preserves pendingRetry
    const after503 = importJourneyReducer(afterGetFailureB, {
      type: "UNCONFIRMED_FAULT",
      fault: {
        kind: "auth_verification_unavailable",
        httpStatus: 503,
        errorCode: "AUTH_VERIFICATION_UNAVAILABLE",
        title: "인증 검증 일시 장애",
        message: "로그인 유지",
        serverConfirmed: false,
        actionGuidance: "재확인",
      },
    });
    expect(after503.serviceAuth.status).toBe("authenticated");
    expect(after503.workspaceAccessRevoked).toBe(false);
    expect(after503.pendingRetry).toEqual({
      runId: "run_v_1",
      expectedRunVersion: 3,
    });

    // 4. 403 WORKSPACE_SCOPE_VIOLATION keeps login active, closes Workspace repos/run/actions/pendingRetry
    const after403Ws = importJourneyReducer(afterGetFailureB, {
      type: "UNCONFIRMED_FAULT",
      workspaceAccessRevoked: true,
      fault: {
        kind: "workspace_scope_violation",
        httpStatus: 403,
        errorCode: "WORKSPACE_SCOPE_VIOLATION",
        title: "Workspace 권한 없음",
        message: "로그인 유지",
        serverConfirmed: false,
        actionGuidance: "확인",
      },
    });
    expect(after403Ws.serviceAuth.status).toBe("authenticated");
    expect(after403Ws.workspaceAccessRevoked).toBe(true);
    expect(after403Ws.repositories).toEqual([]);
    expect(after403Ws.selectedRepoId).toBeNull();
    expect(after403Ws.activeRun).toBeNull();
    expect(after403Ws.succeededRepoIds).toEqual([]);
    expect(after403Ws.pendingRetry).toBeNull();
    expect(canSubmitInitialImport(after403Ws)).toBe(false);
    expect(canExposeRetryAction(after403Ws)).toBe(false);
  });

  it("handles T-205 official 7 contract states, CANCELED non-retryable terminal guard, structured failure reasons, and policy/source block classification", () => {
    const repoA = SYNTHETIC_PUBLIC_REPOSITORIES[0];

    const canceledState = importJourneyReducer(
      createInitialImportJourneyState(),
      {
        type: "BOOTSTRAP_SUCCESS",
        payload: {
          serviceAuth: SYNTHETIC_AUTHENTICATED_AUTH,
          githubConnection: SYNTHETIC_CONNECTED_GITHUB,
          repositories: SYNTHETIC_PUBLIC_REPOSITORIES,
          selectedRepoId: repoA.repoId,
          latestRun: makeRun({
            contractStatus: "CANCELED",
            status: "canceled",
            readiness: {
              importReady: false,
              importReadyState: "failed",
              importReadySummary: "작업 취소됨 (CANCELED · import_ready=false)",
              derivedAllReady: false,
              derivedStages: [],
            },
            retryPolicy: {
              allowed: true, // Even if misconfigured, CANCELED must never expose retry
              reason: "취소됨",
              nextAllowedAt: null,
            },
          }),
          scenarioId: "canceled_run",
          recoveredFromReload: false,
          checkedAt: "2026-11-27T10:00:00Z",
        },
      }
    );

    expect(isFullSuccessRun(canceledState.activeRun)).toBe(false);
    expect(isImportRequestLocked(canceledState)).toBe(false);
    expect(isSelectedRepositoryImportSucceeded(canceledState)).toBe(false);
    expect(canExposeRetryAction(canceledState)).toBe(false);
    expect(resolveContractStatus(canceledState.activeRun!)).toBe("CANCELED");

    // Verify all 7 contract states resolve cleanly
    expect(
      resolveContractStatus(
        makeRun({
          status: "running",
          contractStatus: "PENDING",
          readiness: {
            importReady: false,
            importReadyState: "pending",
            importReadySummary: "대기 중",
            derivedAllReady: false,
            derivedStages: [],
          },
        })
      )
    ).toBe("PENDING");
    expect(
      resolveContractStatus(
        makeRun({
          status: "running",
          contractStatus: "RUNNING",
          readiness: {
            importReady: false,
            importReadyState: "in_progress",
            importReadySummary: "진행 중",
            derivedAllReady: false,
            derivedStages: [],
          },
        })
      )
    ).toBe("RUNNING");
    expect(resolveContractStatus(makeRun({ status: "succeeded" }))).toBe("SUCCESS");
    expect(resolveContractStatus(makeRun({ status: "partial" }))).toBe("PARTIAL");
    expect(resolveContractStatus(makeRun({ status: "limit_reached" }))).toBe(
      "PARTIAL_WITH_LIMIT"
    );
    expect(resolveContractStatus(makeRun({ status: "failed" }))).toBe("FAILED");

    // Verify structured failure reason descriptions
    expect(describeFailureReason("PLAN_DRIFT", true)?.label).toContain("PLAN_DRIFT");
    expect(describeFailureReason("INCOMPLETE_PR_ASSOCIATION")?.label).toContain(
      "UNKNOWN PR"
    );
    expect(describeFailureReason("CURSOR_CYCLE_DETECTED")?.code).toBe(
      "CURSOR_CYCLE_DETECTED"
    );
    expect(describeFailureReason("MALFORMED_OR_PARTIAL_PAGE")?.code).toBe(
      "MALFORMED_OR_PARTIAL_PAGE"
    );
    expect(describeFailureReason("RATE_LIMITED")?.code).toBe("RATE_LIMITED");
    expect(
      describeFailureReason("SOURCE_POLICY_BLOCKED")?.isPolicyOrSourceBlock
    ).toBe(true);

    // Verify policy/source block detection
    expect(
      isPolicyOrSourceBlockedRun(
        makeRun({
          status: "succeeded",
          readiness: {
            importReady: true,
            importReadyState: "ready",
            importReadySummary: "완료",
            processingStatus: "BLOCKED_POLICY",
            derivedAllReady: false,
            derivedStages: [],
          },
        })
      )
    ).toBe(true);
    expect(
      isPolicyOrSourceBlockedRun(
        makeRun({
          status: "partial",
          failureReason: "SOURCE_POLICY_BLOCKED",
        })
      )
    ).toBe(true);
    expect(isPolicyOrSourceBlockedRun(makeRun({ status: "succeeded" }))).toBe(
      false
    );
  });
});
