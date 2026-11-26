import type {
  GitHubConnectionViewModel,
  ImportJourneyState,
  ImportRunViewModel,
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
  latestRun: ImportRunViewModel | null;
  scenarioId: PrototypeScenarioId;
  recoveredFromReload: boolean;
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
    }
  | { type: "DUPLICATE_ATTEMPT_BLOCKED"; message: string }
  | { type: "CLEAR_DUPLICATE_NOTICE" }
  | {
      type: "UNCONFIRMED_FAULT";
      fault: UnconfirmedFaultViewModel;
      serviceAuth?: ServiceAuthViewModel;
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
      scopeSummary: "공개 저장소 읽기 전용 수집 (합성 예시)",
      revocationReason: null,
      connectedAt: null,
    },
    repositories: [],
    selectedRepoId: null,
    activeRun: null,
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

/**
 * Pure predicate: can the user initiate a new initial import?
 */
export function canSubmitInitialImport(state: ImportJourneyState): boolean {
  if (isImportRequestLocked(state)) {
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
 * GitHub collection connection is active (not revoked/disconnected), and no
 * request is currently in flight.
 */
export function canExposeRetryAction(state: ImportJourneyState): boolean {
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
  if (run.status === "running" || run.status === "permission_revoked") {
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
        latestRun,
        scenarioId,
        recoveredFromReload,
        checkedAt,
      } = action.payload;

      const selectedRepoId =
        latestRun?.repositoryId ??
        (state.selectedRepoId &&
        repositories.some((repo) => repo.repoId === state.selectedRepoId)
          ? state.selectedRepoId
          : repositories[0]?.repoId ?? null);

      let recoveryNotice: RecoveryNoticeViewModel | null = null;
      if (recoveredFromReload && latestRun) {
        recoveryNotice =
          latestRun.status === "running"
            ? {
                kind: "restored_running",
                message: `새로고침 후 진행 중인 가져오기 작업(${latestRun.runId})을 서버 상태에서 복구했습니다. 중복 요청을 다시 보내지 않고 진행 상태만 이어서 조회합니다.`,
                restoredAt: checkedAt,
              }
            : {
                kind: "restored_terminal",
                message: `새로고침 후 최근 가져오기 결과(${latestRun.runId})를 서버 상태에서 복구했습니다.`,
                restoredAt: checkedAt,
              };
      } else if (recoveredFromReload) {
        recoveryNotice = {
          kind: "checked_clean",
          message:
            "새로고침 후 서버 상태를 확인했으며 진행 중인 가져오기 작업은 없습니다.",
          restoredAt: checkedAt,
        };
      }

      return {
        ...state,
        phase: "ready",
        serviceAuth,
        githubConnection,
        repositories,
        selectedRepoId,
        activeRun: latestRun,
        pendingActionLabel: null,
        duplicateGuardNotice: null,
        unconfirmedFault: null,
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

    case "SELECT_REPO":
      if (isImportRequestLocked(state)) {
        return {
          ...state,
          duplicateGuardNotice:
            "가져오기 요청이 처리 중일 때는 대상 저장소를 바꿀 수 없습니다.",
        };
      }
      return {
        ...state,
        selectedRepoId: action.repoId,
        duplicateGuardNotice: null,
      };

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

      return {
        ...state,
        phase: "ready",
        serviceAuth: action.serviceAuth,
        githubConnection: action.githubConnection,
        repositories: action.repositories,
        selectedRepoId: nextSelected,
        activeRun:
          action.latestRun !== undefined ? action.latestRun : state.activeRun,
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

    case "RUN_UPDATED_FROM_SERVER":
      return {
        ...state,
        phase: "ready",
        activeRun: action.run,
        selectedRepoId: action.run.repositoryId,
        githubConnection: action.githubConnection ?? state.githubConnection,
        pendingActionLabel: null,
        unconfirmedFault: null,
        recoveryNotice:
          action.recoveryNotice !== undefined
            ? action.recoveryNotice
            : state.recoveryNotice,
      };

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

    case "UNCONFIRMED_FAULT":
      return {
        ...state,
        phase: "ready",
        pendingActionLabel: null,
        serviceAuth: action.serviceAuth ?? state.serviceAuth,
        unconfirmedFault: action.fault,
      };

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
        unconfirmedFault: null,
        duplicateGuardNotice: null,
        recoveryNotice: null,
      };

    default:
      return state;
  }
}
