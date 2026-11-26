import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  ServiceAuthViewModel,
} from "@/domain/view-models";
import { ContractSyncRunClient } from "./contract-sync-run-client";
import {
  SYNTHETIC_AUTHENTICATED_AUTH,
  SYNTHETIC_CONNECTED_GITHUB,
  SYNTHETIC_DISCONNECTED_GITHUB,
  SYNTHETIC_EXPIRED_AUTH,
  SYNTHETIC_PUBLIC_REPOSITORIES,
  SYNTHETIC_REVOKED_GITHUB,
  SYNTHETIC_SIGNED_OUT_AUTH,
} from "./fixtures";
import type {
  AdapterResult,
  ConnectionSyncPayload,
  ImportServiceAdapter,
  PrototypeHarnessControls,
  RunSyncPayload,
  StartInitialImportInput,
} from "./import-service-adapter";
import type { SyntheticHttpTransport } from "./synthetic-http-transport";
import {
  SyntheticOpenApiTransport,
  type SyntheticOpenApiState,
} from "./synthetic-openapi-transport";

/**
 * LOCAL UI FIXTURE ADAPTER.
 *
 * - Login, GitHub collection connection, and repository list remain local mock
 *   UI fixtures (untouched, no real network/OAuth/cookies).
 * - ONLY the 3 sync-run paths (`POST /v1/sync-runs`, `GET <status_url>`,
 *   `POST /v1/sync-runs/{id}/retry`) execute through `ContractSyncRunClient`
 *   over an in-memory `SyntheticHttpTransport` matching OpenAPI 0.2.0-draft
 *   (CONTRACT commit a0314f2cbcb33f1d34ed486db7b1009dcb4adc59).
 */

const PROTOTYPE_STORAGE_KEY = "career_memory_spike_web_import_v1";

interface PersistedPrototypeSnapshot {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  latestRun: ImportRunViewModel | null;
  scenarioId: PrototypeScenarioId;
  pollStep: number;
  runSequence: number;
  isRetryRecoveryFlow: boolean;
  activePlanId: string | null;
}

export interface MockFixtureAdapterOptions {
  /** Simulated response latency in milliseconds. */
  latencyMs?: number;
  /** Disable browser sessionStorage (useful in isolated unit tests). */
  disableBrowserStorage?: boolean;
  /** Initial scenario for tests or stories. */
  initialScenario?: PrototypeScenarioId;
  /** Initial GitHub connection status. */
  initialGitHubStatus?: "connected" | "disconnected" | "permission_revoked";
  /** Initial service auth status. */
  initialAuthStatus?: "authenticated" | "signed_out" | "session_expired";
  /** Optional custom synthetic HTTP transport for the 3 sync-run paths. */
  syncRunTransport?: SyntheticHttpTransport;
  /** Optional deterministic UUID generator for contract adapter tests. */
  idempotencyKeyGenerator?: () => string;
}

function nowIso(): string {
  return new Date().toISOString();
}

function sleep(ms: number): Promise<void> {
  if (ms <= 0) {
    return Promise.resolve();
  }
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function findRepo(repoId: string): PublicRepositoryOption {
  return (
    SYNTHETIC_PUBLIC_REPOSITORIES.find((repo) => repo.repoId === repoId) ??
    SYNTHETIC_PUBLIC_REPOSITORIES[0]
  );
}

function buildPermissionRevokedRun(run: ImportRunViewModel): ImportRunViewModel {
  return {
    ...run,
    status: "permission_revoked",
    updatedAt: nowIso(),
    progressMessage:
      "가져오기 실행 중 GitHub 수집 권한이 회수되어 작업이 중단되었습니다.",
    readiness: {
      importReady: false,
      importReadyState: "failed",
      importReadySummary:
        "권한 상실로 원본 스냅샷 수집 중단 (import_ready=false)",
      processingStatus: "NOT_STARTED",
      processingBlockedMessage: null,
      derivedAllReady: false,
      derivedStages: [
        {
          key: "event_normalization",
          label: "작업 이벤트 정규화",
          status: "not_started",
          detail: "권한 상실로 실행되지 않았습니다.",
        },
        {
          key: "timeline_indexing",
          label: "타임라인 준비",
          status: "not_started",
          detail: "권한 상실로 실행되지 않았습니다.",
        },
      ],
    },
    retryPolicy: {
      allowed: false,
      reason:
        "GitHub 수집 권한이 상실된 상태에서는 재시도할 수 없습니다. 먼저 GitHub 수집 연결을 다시 설정하세요.",
      nextAllowedAt: null,
    },
    error: {
      code: "MOCK_GITHUB_PERMISSION_REVOKED",
      title: "GitHub 수집 권한 상실",
      message:
        "Career Memory 서비스 로그인은 유지되고 있으나, 별도 GitHub 수집 연결 권한이 회수되었습니다.",
      actionGuidance:
        "상단 'GitHub 수집 연결 다시 설정' 버튼으로 권한을 복구한 뒤 새 가져오기를 요청하세요.",
    },
  };
}

export class MockFixtureImportAdapter
  implements ImportServiceAdapter, PrototypeHarnessControls
{
  private latencyMs: number;
  private disableBrowserStorage: boolean;
  private state: PersistedPrototypeSnapshot;
  private failNextPollWithNetworkError = false;
  private revokedDuringLastPoll = false;

  private readonly defaultOpenApiTransport: SyntheticOpenApiTransport;
  public readonly syncRunClient: ContractSyncRunClient;

  /** Diagnostic counters exposed for unit/integration assertions */
  public startImportCallCount = 0;
  public retryImportCallCount = 0;
  public pollCallCount = 0;

  constructor(options: MockFixtureAdapterOptions = {}) {
    this.latencyMs = options.latencyMs ?? 180;
    this.disableBrowserStorage = options.disableBrowserStorage ?? false;

    const loaded = this.readPersistedSnapshot();
    if (loaded) {
      this.state = loaded;
    } else {
      const initialAuth =
        options.initialAuthStatus === "signed_out"
          ? SYNTHETIC_SIGNED_OUT_AUTH
          : options.initialAuthStatus === "session_expired"
            ? SYNTHETIC_EXPIRED_AUTH
            : SYNTHETIC_AUTHENTICATED_AUTH;

      const initialGitHub =
        options.initialGitHubStatus === "disconnected"
          ? SYNTHETIC_DISCONNECTED_GITHUB
          : options.initialGitHubStatus === "permission_revoked"
            ? SYNTHETIC_REVOKED_GITHUB
            : SYNTHETIC_CONNECTED_GITHUB;

      this.state = {
        serviceAuth: { ...initialAuth },
        githubConnection: { ...initialGitHub },
        latestRun: null,
        scenarioId: options.initialScenario ?? "success_full",
        pollStep: 0,
        runSequence: 100,
        isRetryRecoveryFlow: false,
        activePlanId: null,
      };
    }

    this.defaultOpenApiTransport = new SyntheticOpenApiTransport(() => ({
      isAuthenticated: this.state.serviceAuth.status === "authenticated",
      state: {
        scenarioId: this.state.scenarioId,
        pollStep: this.state.pollStep,
        runSequence: this.state.runSequence,
        isRetryRecoveryFlow: this.state.isRetryRecoveryFlow,
        activeRunId: this.state.latestRun?.runId ?? null,
        activePlanId: this.state.activePlanId,
        failNextPollWithNetworkError: this.failNextPollWithNetworkError,
      },
      onStateChange: (next: SyntheticOpenApiState) => {
        this.failNextPollWithNetworkError = next.failNextPollWithNetworkError;
        this.state = {
          ...this.state,
          pollStep: next.pollStep,
          runSequence: next.runSequence,
          isRetryRecoveryFlow: next.isRetryRecoveryFlow,
          activePlanId: next.activePlanId,
        };
      },
      onGitHubPermissionRevoked: () => {
        this.revokedDuringLastPoll = true;
      },
    }));

    this.syncRunClient = new ContractSyncRunClient({
      transport: options.syncRunTransport ?? this.defaultOpenApiTransport,
      keyGenerator: options.idempotencyKeyGenerator,
    });

    if (this.state.latestRun) {
      this.syncRunClient.registerKnownRun(
        this.state.latestRun,
        findRepo(this.state.latestRun.repositoryId)
      );
    }
  }

  public getRecordedHttpRequests() {
    return this.defaultOpenApiTransport.requestLog;
  }

  private getStorage(): Storage | null {
    if (this.disableBrowserStorage) {
      return null;
    }
    if (typeof window === "undefined") {
      return null;
    }
    try {
      return window.sessionStorage;
    } catch {
      return null;
    }
  }

  private readPersistedSnapshot(): PersistedPrototypeSnapshot | null {
    const storage = this.getStorage();
    if (!storage) {
      return null;
    }
    try {
      const raw = storage.getItem(PROTOTYPE_STORAGE_KEY);
      if (!raw) {
        return null;
      }
      return JSON.parse(raw) as PersistedPrototypeSnapshot;
    } catch {
      return null;
    }
  }

  private persistSnapshot(): void {
    const storage = this.getStorage();
    if (!storage) {
      return;
    }
    try {
      storage.setItem(PROTOTYPE_STORAGE_KEY, JSON.stringify(this.state));
    } catch {
      // Ignore quota errors in prototype harness
    }
  }

  private getAvailableRepositories(): PublicRepositoryOption[] {
    if (
      this.state.serviceAuth.status === "authenticated" &&
      this.state.githubConnection.status === "connected"
    ) {
      return SYNTHETIC_PUBLIC_REPOSITORIES;
    }
    return [];
  }

  public getScenario(): PrototypeScenarioId {
    return this.state.scenarioId;
  }

  public setScenario(scenarioId: PrototypeScenarioId): void {
    this.state = {
      ...this.state,
      scenarioId,
    };
    this.persistSnapshot();
  }

  public simulateNextPollNetworkFailure(): void {
    this.failNextPollWithNetworkError = true;
  }

  public clearPrototypeStorage(): void {
    const storage = this.getStorage();
    if (storage) {
      storage.removeItem(PROTOTYPE_STORAGE_KEY);
    }
  }

  public async bootstrapSession(options?: {
    simulateReload?: boolean;
  }): Promise<AdapterResult<BootstrapPayload>> {
    await sleep(this.latencyMs);
    const reloaded = this.readPersistedSnapshot();
    const hadPersistedRun = Boolean(reloaded?.latestRun || this.state.latestRun);
    if (reloaded) {
      this.state = reloaded;
      if (reloaded.latestRun) {
        this.syncRunClient.registerKnownRun(
          reloaded.latestRun,
          findRepo(reloaded.latestRun.repositoryId)
        );
      }
    }
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth, lastCheckedAt: nowIso() },
        githubConnection: { ...this.state.githubConnection },
        repositories: this.getAvailableRepositories(),
        latestRun: this.state.latestRun
          ? structuredClone(this.state.latestRun)
          : null,
        scenarioId: this.state.scenarioId,
        recoveredFromReload: Boolean(options?.simulateReload || hadPersistedRun),
        checkedAt: nowIso(),
      },
    };
  }

  public async signInService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      serviceAuth: {
        ...SYNTHETIC_AUTHENTICATED_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: this.getAvailableRepositories(),
      },
    };
  }

  public async signOutService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      serviceAuth: {
        ...SYNTHETIC_SIGNED_OUT_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
      },
    };
  }

  public async simulateSessionExpired(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      serviceAuth: {
        ...SYNTHETIC_EXPIRED_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
      },
    };
  }

  public async connectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(this.latencyMs);
    if (this.state.serviceAuth.status !== "authenticated") {
      return {
        ok: false,
        kind: "session_expired",
        serviceAuth: { ...this.state.serviceAuth },
        fault: {
          kind: "session_expired",
          title: "서비스 로그인 세션 확인 필요",
          message:
            "Career Memory 서비스 로그인이 유효하지 않아 GitHub 수집 연결을 진행할 수 없습니다.",
          serverConfirmed: false,
          actionGuidance:
            "먼저 Career Memory 모의 서비스에 다시 로그인한 뒤 GitHub 수집 연결을 진행하세요.",
        },
      };
    }

    const nextRun =
      this.state.latestRun?.status === "permission_revoked"
        ? null
        : this.state.latestRun;

    this.state = {
      ...this.state,
      githubConnection: {
        ...SYNTHETIC_CONNECTED_GITHUB,
        connectedAt: nowIso(),
      },
      latestRun: nextRun,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: this.getAvailableRepositories(),
        latestRun: nextRun ? structuredClone(nextRun) : null,
      },
    };
  }

  public async disconnectGitHub(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      githubConnection: { ...SYNTHETIC_DISCONNECTED_GITHUB },
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
      },
    };
  }

  public async simulateGitHubPermissionRevoked(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    await sleep(this.latencyMs);
    const updatedRun =
      this.state.latestRun && this.state.latestRun.status === "running"
        ? buildPermissionRevokedRun(this.state.latestRun)
        : this.state.latestRun;

    this.state = {
      ...this.state,
      githubConnection: { ...SYNTHETIC_REVOKED_GITHUB },
      latestRun: updatedRun,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
        latestRun: updatedRun ? structuredClone(updatedRun) : null,
      },
    };
  }

  public async startInitialImport(
    input: StartInitialImportInput
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.startImportCallCount += 1;
    await sleep(this.latencyMs);

    const repo = findRepo(input.repoId);
    const result = await this.syncRunClient.createInitialImport({ repo });

    if (!result.ok) {
      if (result.kind === "session_expired") {
        this.state = {
          ...this.state,
          serviceAuth: {
            ...SYNTHETIC_EXPIRED_AUTH,
            lastCheckedAt: nowIso(),
          },
        };
        this.persistSnapshot();
        return {
          ...result,
          serviceAuth: { ...this.state.serviceAuth },
        };
      }
      return result;
    }

    this.state = {
      ...this.state,
      latestRun: result.data.run,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(result.data.run),
        githubConnection: { ...this.state.githubConnection },
        recoveryNotice: result.data.recoveryNotice,
      },
    };
  }

  public async pollImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.pollCallCount += 1;
    await sleep(this.latencyMs);

    const current = this.state.latestRun;
    const repo = findRepo(current?.repositoryId ?? SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId);
    this.revokedDuringLastPoll = false;

    const result = await this.syncRunClient.getImportStatus({ runId, repo });
    if (!result.ok) {
      if (result.kind === "session_expired") {
        this.state = {
          ...this.state,
          serviceAuth: {
            ...SYNTHETIC_EXPIRED_AUTH,
            lastCheckedAt: nowIso(),
          },
        };
        this.persistSnapshot();
        return {
          ...result,
          serviceAuth: { ...this.state.serviceAuth },
        };
      }
      return result;
    }

    let nextRun = result.data.run;
    let nextGitHub = this.state.githubConnection;

    if (this.revokedDuringLastPoll) {
      nextGitHub = { ...SYNTHETIC_REVOKED_GITHUB };
      nextRun = buildPermissionRevokedRun(nextRun);
    }

    this.state = {
      ...this.state,
      latestRun: nextRun,
      githubConnection: nextGitHub,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(nextRun),
        githubConnection: { ...nextGitHub },
      },
    };
  }

  public async retryImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.retryImportCallCount += 1;
    await sleep(this.latencyMs);

    const current = this.state.latestRun;
    const repo = findRepo(current?.repositoryId ?? SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId);

    const result = await this.syncRunClient.retryInitialImport({
      runId,
      repo,
      previousRun: current,
    });

    if (!result.ok) {
      if (result.kind === "session_expired") {
        this.state = {
          ...this.state,
          serviceAuth: {
            ...SYNTHETIC_EXPIRED_AUTH,
            lastCheckedAt: nowIso(),
          },
        };
        this.persistSnapshot();
        return {
          ...result,
          serviceAuth: { ...this.state.serviceAuth },
        };
      }
      return result;
    }

    this.state = {
      ...this.state,
      latestRun: result.data.run,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(result.data.run),
        githubConnection: { ...this.state.githubConnection },
        recoveryNotice: result.data.recoveryNotice,
      },
    };
  }

  public async resetActiveRun(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(Math.min(this.latencyMs, 60));
    this.syncRunClient.clearPendingKeys();
    this.defaultOpenApiTransport.clearIdempotencyLog();
    this.state = {
      ...this.state,
      latestRun: null,
      pollStep: 0,
      isRetryRecoveryFlow: false,
      activePlanId: null,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: this.getAvailableRepositories(),
        latestRun: null,
      },
    };
  }
}
