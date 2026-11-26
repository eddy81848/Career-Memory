import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  RecoveryNoticeViewModel,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";

/**
 * Transport-agnostic Service Adapter port.
 *
 * The 3 sync-run operations map OpenAPI 0.2.0-draft synthetic HTTP responses
 * inside the adapter layer; auth/GitHub connection/repo list remain mock UI
 * fixtures until their contracts are approved.
 */

export type AdapterResult<T> =
  | { ok: true; data: T }
  | {
      ok: false;
      kind: UnconfirmedFaultViewModel["kind"];
      fault: UnconfirmedFaultViewModel;
      serviceAuth?: ServiceAuthViewModel;
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
}

export interface StartInitialImportInput {
  repoId: string;
  idempotencyKey?: string;
}

export interface ImportServiceAdapter {
  bootstrapSession(options?: {
    simulateReload?: boolean;
  }): Promise<AdapterResult<BootstrapPayload>>;

  signInService(): Promise<AdapterResult<ConnectionSyncPayload>>;
  signOutService(): Promise<AdapterResult<ConnectionSyncPayload>>;

  connectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>>;
  disconnectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>>;

  startInitialImport(
    input: StartInitialImportInput
  ): Promise<AdapterResult<RunSyncPayload>>;

  pollImportRun(runId: string): Promise<AdapterResult<RunSyncPayload>>;

  retryImportRun(runId: string): Promise<AdapterResult<RunSyncPayload>>;

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
