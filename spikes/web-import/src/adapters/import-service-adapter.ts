import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";

/**
 * Transport-agnostic Service Adapter port.
 *
 * No HTTP paths, cookies, headers, or backend wire schemas are defined here.
 * Once the approved API contract is finalized, a real contract adapter will
 * implement `ImportServiceAdapter` and map contract DTOs -> UI view models.
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
}

export interface StartInitialImportInput {
  repoId: string;
  idempotencyKey: string;
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
