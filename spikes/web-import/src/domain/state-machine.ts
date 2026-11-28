import { resolveContractStatus } from "./import-status-presenter";
import type {
  GitHubConnectionViewModel,
  ImportJourneyState,
  ImportRunViewModel,
  PendingUnconfirmedRetryViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "./view-models";

export interface BootstrapPayload {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  repositories: PublicRepositoryOption[];
  selectedRepoId?: string | null;
  latestRun: ImportRunViewModel | null;
  scenarioId: PrototypeScenarioId;
  recoveredFromReload: boolean;
  recoveryLookupMode?: "detail_get" | "recovery_get" | null;
  recoveryFault?: UnconfirmedFaultViewModel | null;
  workspaceAccessRevoked?: boolean;
  checkedAt: string;
}

export type ImportJourneyAction =
  | { type: "BOOTSTRAP_START" }
  | { type: "BOOTSTRAP_SUCCESS"; payload: BootstrapPayload }
  | { type: "BOOTSTRAP_NETWORK_ERROR"; fault: UnconfirmedFaultViewModel }
  | { type: "SELECT_REPO"; repoId: string }
  | { type: "SET_SCENARIO"; scenarioId: PrototypeScenarioId }
  | { type: "MUTATION_START"; label: string }
  | {
      type: "AUTH_AND_CONNECTION_SYNCED";
      serviceAuth: ServiceAuthViewModel;
      githubConnection: GitHubConnectionViewModel;
      repositories: PublicRepositoryOption[];
      latestRun?: ImportRunViewModel | null;
      clearFault?: boolean;
    }
  | { type: "SUBMIT_IMPORT_START"; label: string }
  | { type: "RETRY_IMPORT_START"; label: string }
  | { type: "REFRESH_STATUS_START"; label: string }
  | {
      type: "RUN_UPDATED_FROM_SERVER";
      run: ImportRunViewModel;
      githubConnection?: GitHubConnectionViewModel;
      recoveryNotice?: RecoveryNoticeViewModel | null;
      pendingRetry?: PendingUnconfirmedRetryViewModel | null;
    }
  | {
      type: "RECOVERY_LOOKUP_SYNCED";
      run: ImportRunViewModel | null;
      githubConnection?: GitHubConnectionViewModel;
      recoveryNotice: RecoveryNoticeViewModel;
      pendingRetry?: PendingUnconfirmedRetryViewModel | null;
    }
  | { type: "DUPLICATE_ATTEMPT_BLOCKED"; message: string }
  | { type: "CLEAR_DUPLICATE_NOTICE" }
  | {
      type: "UNCONFIRMED_FAULT";
      fault: UnconfirmedFaultViewModel;
      serviceAuth?: ServiceAuthViewModel;
      workspaceAccessRevoked?: boolean;
      clearActiveRun?: boolean;
      pendingRetry?: PendingUnconfirmedRetryViewModel | null;
    }
  | { type: "RESET_TO_IDLE_SELECTION" };

export function createInitialImportJourneyState(): ImportJourneyState {
  return {
    phase: "bootstrapping",
    serviceAuth: {
      status: "authenticating",
      account: null,
      sessionNote: "모의 서비스 로그인 상태를 확인하는 중입니다.",
      lastCheckedAt: null,
    },
    githubConnection: {
      status: "disconnected",
      accountHandle: null,
      connectionRef: null,
      scopeSummary:
        'GitHub OAuth App 최소 읽기 연결 (scope="read:user" 단독 · 저장소/쓰기 스코프 0개 · 단일 공개 저장소 Allowlist 전용)',
      revocationReason: null,
      connectedAt: null,
    },
    repositories: [],
    selectedRepoId: null,
    activeRun: null,
    succeededRepoIds: [],
    workspaceAccessRevoked: false,
    pendingRetry: null,
    pendingActionLabel: "저장된 연결 및 최근 가져오기 상태를 확인하고 있습니다.",
    duplicateGuardNotice: null,
    unconfirmedFault: null,
    recoveryNotice: null,
    scenarioId: "success_full",
  };
}

/**
 * Pure predicate: determines whether an import request is currently in-flight
 * or actively running on the server, so duplicate clicks/triggers are blocked.
 */
export function isImportRequestLocked(state: ImportJourneyState): boolean {
  if (
    state.phase === "bootstrapping" ||
    state.phase === "submitting_import" ||
    state.phase === "retrying_import" ||
    state.phase === "mutating_connection"
  ) {
    return true;
  }
  return state.activeRun?.status === "running";
}

function recordSucceededRepo(
  existing: string[],
  run: ImportRunViewModel | null | undefined
): string[] {
  if (!run || !isFullSuccessRun(run)) {
    return existing;
  }
  if (existing.includes(run.repositoryId)) {
    return existing;
  }
  return [...existing, run.repositoryId];
}

/**
 * Pure predicate: is the currently selected repository already confirmed as
 * succeeded (`status === "succeeded"` and `importReady === true`) in the
 * current auth/connection scope?
 *
 * Repository-scoped UI guard (MVP API §4 / Snapshot Import §2-3):
 * checks both the currently displayed `activeRun` and the session-scoped
 * `succeededRepoIds` set so switching A -> B -> A keeps A blocked without
 * globally blocking B, leaking across auth/permission changes, or blocking
 * manual status checks. Partial results (`partial`), limit outcomes
 * (`limit_reached`), failures, permission revocations, unconfirmed faults,
 * and incomplete runs MUST NOT be treated as succeeded. Final duplicate and
 * authorization enforcement always belongs to the backend server.
 */
export function isSelectedRepositoryImportSucceeded(
  state: ImportJourneyState
): boolean {
  if (
    state.workspaceAccessRevoked ||
    state.serviceAuth.status !== "authenticated" ||
    state.githubConnection.status !== "connected" ||
    !state.selectedRepoId
  ) {
    return false;
  }
  if (
    state.activeRun &&
    state.activeRun.repositoryId === state.selectedRepoId &&
    isFullSuccessRun(state.activeRun)
  ) {
    return true;
  }
  return (state.succeededRepoIds ?? []).includes(state.selectedRepoId);
}

/**
 * Pure predicate: can the user initiate a new initial import?
 */
export function canSubmitInitialImport(state: ImportJourneyState): boolean {
  if (state.workspaceAccessRevoked) {
    return false;
  }
  if (isImportRequestLocked(state)) {
    return false;
  }
  if (isSelectedRepositoryImportSucceeded(state)) {
    return false;
  }
  if (state.serviceAuth.status !== "authenticated") {
    return false;
  }
  if (state.githubConnection.status !== "connected") {
    return false;
  }
  if (!state.selectedRepoId) {
    return false;
  }
  return true;
}

/**
 * Pure predicate: retry action is ONLY exposed when the server explicitly
 * grants `retryPolicy.allowed === true`, the service session is authenticated,
 * Workspace access has not been revoked, GitHub collection connection is active
 * (not revoked/disconnected), and no request is currently in flight.
 */
export function canExposeRetryAction(state: ImportJourneyState): boolean {
  if (state.workspaceAccessRevoked) {
    return false;
  }
  const run = state.activeRun;
  if (!run) {
    return false;
  }
  if (state.serviceAuth.status !== "authenticated") {
    return false;
  }
  if (state.githubConnection.status !== "connected") {
    return false;
  }
  if (
    run.status === "running" ||
    run.status === "canceled" ||
    run.status === "permission_revoked"
  ) {
    return false;
  }
  return run.retryPolicy.allowed === true;
}

/**
 * Pure predicate: is this run strictly a full, unqualified success?
 * Partial results, limits, failures, and permission revocations MUST NEVER
 * evaluate as full success.
 */
export function isFullSuccessRun(run: ImportRunViewModel | null): boolean {
  if (!run) {
    return false;
  }
  return run.status === "succeeded" && run.readiness.importReady === true;
}

/**
 * Pure predicate: is `import_ready` true while derived stages are still
 * incomplete or failed? Used to display explicit distinction messaging.
 */
export function hasDerivedStageLag(run: ImportRunViewModel | null): boolean {
  if (!run) {
    return false;
  }
  return run.readiness.importReady && !run.readiness.derivedAllReady;
}

/**
 * Pure predicate (Product & UX Spec §5 entry gate):
 * Event Inbox CTA / view MUST ONLY be enabled when:
 *   - authenticated service session & connected GitHub scope (not revoked)
 *   - activeRun status === "SUCCESS" (succeeded)
 *   - activeRun.readiness.importReady === true
 *   - activeRun.readiness.processingStatus !== "BLOCKED_POLICY" (and not "BLOCKED_SOURCE")
 *
 * PENDING, RUNNING, PARTIAL, PARTIAL_WITH_LIMIT, FAILED, CANCELED,
 * import_ready=false, and BLOCKED_POLICY MUST NEVER expose Event Inbox results.
 */
export function canEnterEventInbox(state: ImportJourneyState): boolean {
  if (
    state.workspaceAccessRevoked ||
    state.serviceAuth.status !== "authenticated" ||
    state.githubConnection.status !== "connected" ||
    !state.activeRun
  ) {
    return false;
  }

  const contractStatus = resolveContractStatus(state.activeRun);
  if (
    contractStatus !== "SUCCESS" ||
    state.activeRun.status !== "succeeded" ||
    state.activeRun.readiness.importReady !== true
  ) {
    return false;
  }

  const processingStatus = state.activeRun.readiness.processingStatus;
  if (
    processingStatus === "BLOCKED_POLICY" ||
    processingStatus === "BLOCKED_SOURCE"
  ) {
    return false;
  }

  return true;
}

export function buildRecoveryNotice(params: {
  run: ImportRunViewModel | null;
  lookupMode: "detail_get" | "recovery_get";
  checkedAt: string;
}): RecoveryNoticeViewModel {
  const { run, lookupMode, checkedAt } = params;

  if (lookupMode === "recovery_get") {
    if (!run) {
      return {
        kind: "checked_clean",
        message:
          "서버 복구 조회에서 표시할 실행 이력이 없었습니다. 응답이 유실된 이전 요청이 있었다면 이 빈 조회만으로 실패를 확정하지 않으며, 자동으로 새 가져오기를 전송하지 않았습니다.",
        restoredAt: checkedAt,
      };
    }
    if (run.status === "running") {
      return {
        kind: "restored_running",
        message: `저장소 복구 조회로 진행 중인 실행(${run.runId})을 확인했습니다. 중복 요청을 다시 보내지 않고 진행 상태만 이어서 조회합니다. 이 실행은 이전 이력일 수 있으므로 조회 결과만으로 응답이 유실된 마지막 요청의 성공을 단정하지 않습니다.`,
        restoredAt: checkedAt,
      };
    }
    return {
      kind: "restored_terminal",
      message: `저장소 복구 조회로 최근 종료 실행(${run.runId})의 최신 상태를 불러왔습니다. 이 실행은 이전 이력일 수 있으므로 조회 결과만으로 응답이 유실된 마지막 요청의 성공을 단정하지 않습니다. 종료 상태이므로 자동 폴링은 종료했습니다.`,
      restoredAt: checkedAt,
    };
  }

  if (!run) {
    return {
      kind: "checked_clean",
      message:
        "새로고침 후 서버 상태를 확인했으며 진행 중인 가져오기 작업은 없습니다.",
      restoredAt: checkedAt,
    };
  }

  if (run.status === "running") {
    return {
      kind: "restored_running",
      message: `새로고침 후 진행 중인 가져오기 작업(${run.runId})을 상세 조회로 복구했습니다. 중복 요청을 다시 보내지 않고 진행 상태만 이어서 조회합니다.`,
      restoredAt: checkedAt,
    };
  }

  return {
    kind: "restored_terminal",
    message: `기존 실행(${run.runId})의 최신 상태를 상세 조회로 복구했습니다. 종료 상태이므로 자동 폴링은 종료했습니다.`,
    restoredAt: checkedAt,
  };
}

function buildBootstrapRecoveryNotice(params: {
  latestRun: ImportRunViewModel | null;
  recoveredFromReload: boolean;
  lookupMode: "detail_get" | "recovery_get" | null;
  checkedAt: string;
}): RecoveryNoticeViewModel | null {
  const { latestRun, recoveredFromReload, lookupMode, checkedAt } = params;

  if (lookupMode === "recovery_get") {
    if (latestRun) {
      return buildRecoveryNotice({
        run: latestRun,
        lookupMode: "recovery_get",
        checkedAt,
      });
    }
    if (recoveredFromReload) {
      return buildRecoveryNotice({
        run: null,
        lookupMode: "recovery_get",
        checkedAt,
      });
    }
    return null;
  }

  if (recoveredFromReload) {
    return buildRecoveryNotice({
      run: latestRun,
      lookupMode: lookupMode ?? "detail_get",
      checkedAt,
    });
  }

  return null;
}

export function importJourneyReducer(
  state: ImportJourneyState,
  action: ImportJourneyAction
): ImportJourneyState {
  switch (action.type) {
    case "BOOTSTRAP_START":
      return {
        ...state,
        phase: "bootstrapping",
        pendingActionLabel:
          "새로고침 이후 서버의 연결 및 최근 가져오기 상태를 확인하고 있습니다.",
        unconfirmedFault: null,
        duplicateGuardNotice: null,
      };

    case "BOOTSTRAP_SUCCESS": {
      const {
        serviceAuth,
        githubConnection,
        repositories,
        selectedRepoId: payloadSelectedRepoId,
        latestRun,
        scenarioId,
        recoveredFromReload,
        recoveryLookupMode,
        recoveryFault,
        workspaceAccessRevoked: payloadWorkspaceRevoked,
        checkedAt,
      } = action.payload;

      const workspaceAccessRevoked = Boolean(payloadWorkspaceRevoked);
      const effectiveRepos = workspaceAccessRevoked ? [] : repositories;
      const effectiveLatestRun = workspaceAccessRevoked ? null : latestRun;

      const selectedRepoId = workspaceAccessRevoked
        ? null
        : effectiveLatestRun?.repositoryId ??
          (payloadSelectedRepoId &&
          effectiveRepos.some((repo) => repo.repoId === payloadSelectedRepoId)
            ? payloadSelectedRepoId
            : state.selectedRepoId &&
                effectiveRepos.some(
                  (repo) => repo.repoId === state.selectedRepoId
                )
              ? state.selectedRepoId
              : effectiveRepos[0]?.repoId ?? null);

      const recoveryNotice =
        workspaceAccessRevoked ||
        (recoveryFault !== undefined && recoveryFault !== null)
          ? null
          : buildBootstrapRecoveryNotice({
              latestRun: effectiveLatestRun,
              recoveredFromReload,
              lookupMode: recoveryLookupMode ?? null,
              checkedAt,
            });

      const isScopeActive =
        !workspaceAccessRevoked &&
        serviceAuth.status === "authenticated" &&
        githubConnection.status === "connected";

      return {
        ...state,
        phase: "ready",
        serviceAuth,
        githubConnection,
        repositories: effectiveRepos,
        selectedRepoId,
        activeRun: effectiveLatestRun,
        succeededRepoIds: isScopeActive
          ? recordSucceededRepo([], effectiveLatestRun)
          : [],
        workspaceAccessRevoked,
        // Pending retry keys/bodies are NEVER persisted across reloads.
        pendingRetry: null,
        pendingActionLabel: null,
        duplicateGuardNotice: null,
        unconfirmedFault: recoveryFault ?? null,
        recoveryNotice,
        scenarioId,
      };
    }

    case "BOOTSTRAP_NETWORK_ERROR":
      return {
        ...state,
        phase: "ready",
        pendingActionLabel: null,
        unconfirmedFault: action.fault,
      };

    case "SELECT_REPO": {
      if (isImportRequestLocked(state)) {
        return {
          ...state,
          duplicateGuardNotice:
            "가져오기 요청이 처리 중일 때는 대상 저장소를 바꿀 수 없습니다.",
        };
      }
      const switchedDifferentRepo = state.selectedRepoId !== action.repoId;
      const keepActiveRun =
        state.activeRun && state.activeRun.repositoryId === action.repoId
          ? state.activeRun
          : null;
      return {
        ...state,
        phase: state.phase === "refreshing_status" ? "ready" : state.phase,
        pendingActionLabel:
          state.phase === "refreshing_status" ? null : state.pendingActionLabel,
        selectedRepoId: action.repoId,
        activeRun: switchedDifferentRepo ? keepActiveRun : state.activeRun,
        pendingRetry: switchedDifferentRepo ? null : state.pendingRetry,
        unconfirmedFault: switchedDifferentRepo ? null : state.unconfirmedFault,
        recoveryNotice: switchedDifferentRepo ? null : state.recoveryNotice,
        duplicateGuardNotice: null,
      };
    }

    case "SET_SCENARIO":
      return {
        ...state,
        scenarioId: action.scenarioId,
        duplicateGuardNotice: null,
      };

    case "MUTATION_START":
      return {
        ...state,
        phase: "mutating_connection",
        pendingActionLabel: action.label,
        duplicateGuardNotice: null,
        unconfirmedFault: null,
      };

    case "AUTH_AND_CONNECTION_SYNCED": {
      const nextSelected =
        state.selectedRepoId &&
        action.repositories.some((repo) => repo.repoId === state.selectedRepoId)
          ? state.selectedRepoId
          : action.repositories[0]?.repoId ?? null;

      const isAuthValid = action.serviceAuth.status === "authenticated";
      const isScopeActive =
        isAuthValid && action.githubConnection.status === "connected";

      // Omitting `latestRun` NEVER inherits the previous scope's `activeRun` or
      // confirmed success list. Only use an explicit `action.latestRun` confirmed
      // for the new auth/connection scope.
      let nextRun: ImportRunViewModel | null = null;
      if (isScopeActive && action.latestRun) {
        nextRun = action.latestRun;
      } else if (
        isAuthValid &&
        action.githubConnection.status === "permission_revoked" &&
        action.latestRun?.status === "permission_revoked"
      ) {
        nextRun = action.latestRun;
      }

      const nextSucceededRepoIds = isScopeActive
        ? recordSucceededRepo([], nextRun)
        : [];

      return {
        ...state,
        phase: "ready",
        serviceAuth: action.serviceAuth,
        githubConnection: action.githubConnection,
        repositories: action.repositories,
        selectedRepoId: nextRun?.repositoryId ?? nextSelected,
        activeRun: nextRun,
        succeededRepoIds: nextSucceededRepoIds,
        workspaceAccessRevoked: false,
        pendingRetry: null,
        recoveryNotice: null,
        duplicateGuardNotice: null,
        pendingActionLabel: null,
        unconfirmedFault: action.clearFault ? null : state.unconfirmedFault,
      };
    }

    case "SUBMIT_IMPORT_START":
      // Never optimistically set activeRun to "running" or "succeeded" before
      // receiving server acknowledgement!
      return {
        ...state,
        phase: "submitting_import",
        pendingActionLabel: action.label,
        duplicateGuardNotice: null,
        unconfirmedFault: null,
        recoveryNotice: null,
      };

    case "RETRY_IMPORT_START":
      return {
        ...state,
        phase: "retrying_import",
        pendingActionLabel: action.label,
        duplicateGuardNotice: null,
        unconfirmedFault: null,
        recoveryNotice: null,
      };

    case "REFRESH_STATUS_START":
      return {
        ...state,
        phase: "refreshing_status",
        pendingActionLabel: action.label,
        unconfirmedFault: null,
      };

    case "RUN_UPDATED_FROM_SERVER": {
      const nextGitHub = action.githubConnection ?? state.githubConnection;
      const isScopeActive =
        !state.workspaceAccessRevoked &&
        state.serviceAuth.status === "authenticated" &&
        nextGitHub.status === "connected";
      return {
        ...state,
        phase: "ready",
        activeRun: action.run,
        selectedRepoId: action.run.repositoryId,
        githubConnection: nextGitHub,
        succeededRepoIds: isScopeActive
          ? recordSucceededRepo(state.succeededRepoIds, action.run)
          : [],
        pendingRetry:
          action.pendingRetry !== undefined
            ? action.pendingRetry
            : state.pendingRetry,
        pendingActionLabel: null,
        unconfirmedFault: null,
        recoveryNotice:
          action.recoveryNotice !== undefined
            ? action.recoveryNotice
            : state.recoveryNotice,
      };
    }

    case "RECOVERY_LOOKUP_SYNCED": {
      const nextGitHub = action.githubConnection ?? state.githubConnection;
      const isScopeActive =
        !state.workspaceAccessRevoked &&
        state.serviceAuth.status === "authenticated" &&
        nextGitHub.status === "connected";
      return {
        ...state,
        phase: "ready",
        activeRun: action.run,
        selectedRepoId: action.run?.repositoryId ?? state.selectedRepoId,
        githubConnection: nextGitHub,
        succeededRepoIds: isScopeActive
          ? recordSucceededRepo(state.succeededRepoIds, action.run)
          : [],
        pendingRetry:
          action.pendingRetry !== undefined
            ? action.pendingRetry
            : state.pendingRetry,
        pendingActionLabel: null,
        unconfirmedFault: null,
        recoveryNotice: action.recoveryNotice,
      };
    }

    case "DUPLICATE_ATTEMPT_BLOCKED":
      return {
        ...state,
        duplicateGuardNotice: action.message,
      };

    case "CLEAR_DUPLICATE_NOTICE":
      return {
        ...state,
        duplicateGuardNotice: null,
      };

    case "UNCONFIRMED_FAULT": {
      const nextServiceAuth = action.serviceAuth ?? state.serviceAuth;
      const authStillValid = nextServiceAuth.status === "authenticated";
      const workspaceRevoked =
        action.workspaceAccessRevoked === true ||
        action.fault.kind === "workspace_scope_violation";

      if (!authStillValid) {
        return {
          ...state,
          phase: "ready",
          pendingActionLabel: null,
          serviceAuth: nextServiceAuth,
          repositories: [],
          selectedRepoId: null,
          activeRun: null,
          succeededRepoIds: [],
          workspaceAccessRevoked: false,
          pendingRetry: null,
          recoveryNotice: null,
          duplicateGuardNotice: null,
          unconfirmedFault: action.fault,
        };
      }

      if (workspaceRevoked) {
        // 403 WORKSPACE_SCOPE_VIOLATION: keep login active (`serviceAuth` stays authenticated),
        // close Workspace repos, run details, and polling.
        return {
          ...state,
          phase: "ready",
          pendingActionLabel: null,
          serviceAuth: nextServiceAuth,
          repositories: [],
          selectedRepoId: null,
          activeRun: null,
          succeededRepoIds: [],
          workspaceAccessRevoked: true,
          pendingRetry: null,
          recoveryNotice: null,
          duplicateGuardNotice: null,
          unconfirmedFault: action.fault,
        };
      }

      const clearActiveRun =
        action.clearActiveRun === true ||
        action.fault.kind === "resource_not_found";

      return {
        ...state,
        phase: "ready",
        pendingActionLabel: null,
        serviceAuth: nextServiceAuth,
        activeRun: clearActiveRun ? null : state.activeRun,
        recoveryNotice: clearActiveRun ? null : state.recoveryNotice,
        pendingRetry:
          action.pendingRetry !== undefined
            ? action.pendingRetry
            : clearActiveRun
              ? null
              : state.pendingRetry,
        unconfirmedFault: action.fault,
      };
    }

    case "RESET_TO_IDLE_SELECTION":
      if (isImportRequestLocked(state)) {
        return {
          ...state,
          duplicateGuardNotice:
            "진행 중인 가져오기 작업이 완료될 때까지 초기화할 수 없습니다.",
        };
      }
      return {
        ...state,
        activeRun: null,
        succeededRepoIds: [],
        workspaceAccessRevoked: false,
        pendingRetry: null,
        unconfirmedFault: null,
        duplicateGuardNotice: null,
        recoveryNotice: null,
      };

    default:
      return state;
  }
}
