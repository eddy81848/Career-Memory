import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PendingUnconfirmedRetryViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";

/**
 * Transport-agnostic Service Adapter port.
 *
 * The 4 sync-run operations map OpenAPI 0.5.0-draft HTTP responses inside the
 * adapter layer; auth/GitHub connection/repo list remain mock UI fixtures until
 * their product contracts are approved.
 */

export type AdapterResult<T> =
  | { ok: true; data: T }
  | {
      ok: false;
      kind: UnconfirmedFaultViewModel["kind"];
      fault: UnconfirmedFaultViewModel;
      serviceAuth?: ServiceAuthViewModel;
      workspaceAccessRevoked?: boolean;
      clearActiveRun?: boolean;
      pendingRetry?: PendingUnconfirmedRetryViewModel | null;
    };

export interface ConnectionSyncPayload {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  repositories: PublicRepositoryOption[];
  latestRun?: ImportRunViewModel | null;
}

export interface RunSyncPayload {
  run: ImportRunViewModel;
  githubConnection?: GitHubConnectionViewModel;
  recoveryNotice?: RecoveryNoticeViewModel | null;
  pendingRetry?: PendingUnconfirmedRetryViewModel | null;
}

export interface StartInitialImportInput {
  repoId: string;
  idempotencyKey?: string;
}

export interface RetryImportRunOptions {
  /**
   * - `"new_retry"` (default): starts a brand-new retry against the latest
   *   observed run state using a fresh UUID Idempotency-Key and `expectedRunVersion`.
   * - `"resend_pending"`: explicitly resends the exact previously unconfirmed
   *   retry (same runId, same Idempotency-Key, same original expected_run_version).
   */
  mode?: "new_retry" | "resend_pending";
  /** Run version attributed to `runId` (required for `"new_retry"`). */
  expectedRunVersion?: number;
}

export interface RecoverRepositoryRunInput {
  repoId: string;
  knownRunId?: string | null;
}

export interface RecoverRepositoryRunPayload {
  run: ImportRunViewModel | null;
  lookupMode: "detail_get" | "recovery_get";
  githubConnection?: GitHubConnectionViewModel;
  recoveryNotice: RecoveryNoticeViewModel;
  pendingRetry?: PendingUnconfirmedRetryViewModel | null;
  checkedAt: string;
}

export interface ImportServiceAdapter {
  bootstrapSession(options?: {
    simulateReload?: boolean;
    selectedRepoId?: string | null;
  }): Promise<AdapterResult<BootstrapPayload>>;

  selectRepository?(repoId: string): void;

  signInService(): Promise<AdapterResult<ConnectionSyncPayload>>;
  signOutService(): Promise<AdapterResult<ConnectionSyncPayload>>;

  connectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>>;
  disconnectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>>;

  startInitialImport(
    input: StartInitialImportInput
  ): Promise<AdapterResult<RunSyncPayload>>;

  pollImportRun(runId: string): Promise<AdapterResult<RunSyncPayload>>;

  recoverRepositoryRun?(
    input: RecoverRepositoryRunInput
  ): Promise<AdapterResult<RecoverRepositoryRunPayload>>;

  retryImportRun(
    runId: string,
    options?: RetryImportRunOptions
  ): Promise<AdapterResult<RunSyncPayload>>;

  resetActiveRun(): Promise<AdapterResult<ConnectionSyncPayload>>;
}

/**
 * Separate prototype-only simulation hooks used strictly by the local
 * scenario bar and automated UI tests. Production adapters do NOT implement
 * this interface.
 */
export interface PrototypeHarnessControls {
  getScenario(): PrototypeScenarioId;
  setScenario(scenarioId: PrototypeScenarioId): void;
  simulateSessionExpired(): Promise<AdapterResult<ConnectionSyncPayload>>;
  simulateGitHubPermissionRevoked(): Promise<
    AdapterResult<ConnectionSyncPayload>
  >;
  simulateNextPollNetworkFailure(): void;
  clearPrototypeStorage(): void;
}
