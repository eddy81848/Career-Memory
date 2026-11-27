import {
  buildRecoveryNotice,
  type BootstrapPayload,
} from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
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
  RecoverRepositoryRunInput,
  RecoverRepositoryRunPayload,
  RetryImportRunOptions,
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
 * - ONLY the 4 sync-run paths (`POST /v1/sync-runs`,
 *   `GET /v1/sync-runs?source_resource_id=...&limit=1`, `GET <status_url>`,
 *   `POST /v1/sync-runs/{id}/retry`) execute through `ContractSyncRunClient`
 *   matching OpenAPI 0.5.0-draft (CONTRACT commit
 *   87f1515cbe74bf02b0aece68904bf9a641c74c60).
 * - Pending Idempotency-Keys and retry bodies are NEVER persisted in browser storage.
 */

const PROTOTYPE_STORAGE_KEY = "career_memory_spike_web_import_v1";

interface PersistedPrototypeSnapshot {
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  sessionId: string;
  selectedRepoId: string | null;
  latestRun: ImportRunViewModel | null;
  latestRunSessionId: string | null;
  scenarioId: PrototypeScenarioId;
  pollStep: number;
  runSequence: number;
  runVersion: number;
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
  /** Optional custom synthetic HTTP transport for the 4 sync-run paths. */
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
  private authScopeVersion = 1;
  private sessionSequence = 1;
  private selectionGeneration = 0;

  private readonly defaultOpenApiTransport: SyntheticOpenApiTransport;
  public readonly syncRunClient: ContractSyncRunClient;

  /** Diagnostic counters exposed for unit/integration assertions */
  public startImportCallCount = 0;
  public retryImportCallCount = 0;
  public pollCallCount = 0;
  public recoveryLookupCallCount = 0;

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
        sessionId: "session_1",
        selectedRepoId: SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId,
        latestRun: null,
        latestRunSessionId: null,
        scenarioId: options.initialScenario ?? "success_full",
        pollStep: 0,
        runSequence: 100,
        runVersion: 1,
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
        runVersion: this.state.runVersion,
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
          runVersion: next.runVersion,
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
      initialAuthScope: this.getCurrentAuthScope(),
    });

    if (this.state.latestRun) {
      this.syncRunClient.registerKnownRun(
        this.state.latestRun,
        findRepo(this.state.latestRun.repositoryId)
      );
    }
  }

  private getCurrentAuthScope(): string {
    const userId = this.state.serviceAuth.account?.syntheticUserId ?? "anonymous";
    const ghRef = this.state.githubConnection.connectionRef ?? "none";
    return `${this.state.sessionId}:${this.state.serviceAuth.status}:${userId}:${this.state.githubConnection.status}:${ghRef}:epoch-${this.authScopeVersion}`;
  }

  private advanceAuthScope(): void {
    this.authScopeVersion += 1;
    this.syncRunClient.setAuthScope(this.getCurrentAuthScope());
  }

  private nextSessionId(): string {
    this.sessionSequence += 1;
    return `session_${this.sessionSequence}_${Date.now()}`;
  }

  public selectRepository(repoId: string): void {
    if (this.state.selectedRepoId === repoId) {
      return;
    }
    this.selectionGeneration += 1;
    this.syncRunClient.clearPendingKeysForTargetChange(repoId);
    const keepRun =
      this.state.latestRun && this.state.latestRun.repositoryId === repoId
        ? this.state.latestRun
        : null;
    this.state = {
      ...this.state,
      selectedRepoId: repoId,
      latestRun: keepRun,
      latestRunSessionId: keepRun ? this.state.latestRunSessionId : null,
    };
    this.persistSnapshot();
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
      const parsed = JSON.parse(raw) as Partial<PersistedPrototypeSnapshot>;
      if (!parsed.serviceAuth || !parsed.githubConnection || !parsed.scenarioId) {
        return null;
      }
      const sessionId = parsed.sessionId ?? "session_1";
      const latestRun = parsed.latestRun ?? null;
      return {
        serviceAuth: parsed.serviceAuth,
        githubConnection: parsed.githubConnection,
        sessionId,
        selectedRepoId:
          parsed.selectedRepoId ??
          latestRun?.repositoryId ??
          SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId,
        latestRun,
        latestRunSessionId:
          parsed.latestRunSessionId !== undefined
            ? parsed.latestRunSessionId
            : latestRun
              ? sessionId
              : null,
        scenarioId: parsed.scenarioId,
        pollStep: parsed.pollStep ?? 0,
        runSequence: parsed.runSequence ?? 100,
        runVersion: parsed.runVersion ?? 1,
        isRetryRecoveryFlow: parsed.isRetryRecoveryFlow ?? false,
        activePlanId: parsed.activePlanId ?? null,
      };
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

  private findMatchingKnownRun(
    repoId: string,
    requestedRunId?: string | null
  ): ImportRunViewModel | null {
    const candidate = this.state.latestRun;
    if (!candidate) {
      return null;
    }
    if (
      candidate.repositoryId !== repoId ||
      this.state.latestRunSessionId !== this.state.sessionId
    ) {
      return null;
    }
    if (requestedRunId && candidate.runId !== requestedRunId) {
      return null;
    }
    return candidate;
  }

  private isStaleLookup(params: {
    authScope: string;
    selectionGen: number;
    repoId: string;
  }): boolean {
    return (
      params.authScope !== this.getCurrentAuthScope() ||
      params.selectionGen !== this.selectionGeneration ||
      this.state.selectedRepoId !== params.repoId
    );
  }

  private buildStaleLookupFault(): UnconfirmedFaultViewModel {
    return {
      kind: "uncertain_outcome",
      title: "이전 세션 또는 저장소의 늦은 조회 응답 무시됨",
      message:
        "조회 중 저장소 또는 세션이 변경되어 이전 조회 응답을 현재 화면에 반영하지 않았습니다.",
      serverConfirmed: false,
      actionGuidance: "현재 선택한 저장소와 세션에서 필요한 작업을 진행해 주세요.",
    };
  }

  private applyClientFailureToAdapterState<T>(
    result: Extract<AdapterResult<T>, { ok: false }>
  ): Extract<AdapterResult<T>, { ok: false }> {
    if (result.kind === "session_expired") {
      this.state = {
        ...this.state,
        latestRun: null,
        latestRunSessionId: null,
        serviceAuth: {
          ...SYNTHETIC_EXPIRED_AUTH,
          lastCheckedAt: nowIso(),
        },
      };
      this.advanceAuthScope();
      this.persistSnapshot();
      return {
        ...result,
        serviceAuth: { ...this.state.serviceAuth },
        pendingRetry: null,
      };
    }

    if (
      result.kind === "workspace_scope_violation" ||
      result.workspaceAccessRevoked
    ) {
      this.state = {
        ...this.state,
        latestRun: null,
        latestRunSessionId: null,
      };
      this.advanceAuthScope();
      this.persistSnapshot();
      return {
        ...result,
        workspaceAccessRevoked: true,
        pendingRetry: null,
      };
    }

    if (result.kind === "resource_not_found" || result.clearActiveRun) {
      this.state = {
        ...this.state,
        latestRun: null,
        latestRunSessionId: null,
      };
      this.persistSnapshot();
      return {
        ...result,
        clearActiveRun: true,
        pendingRetry: this.syncRunClient.getPendingRetrySummary(),
      };
    }

    return {
      ...result,
      pendingRetry:
        result.pendingRetry !== undefined
          ? result.pendingRetry
          : this.syncRunClient.getPendingRetrySummary(),
    };
  }

  public async bootstrapSession(options?: {
    simulateReload?: boolean;
    selectedRepoId?: string | null;
  }): Promise<AdapterResult<BootstrapPayload>> {
    const startAuthScope = this.getCurrentAuthScope();
    const startSelectionGen = this.selectionGeneration;
    await sleep(this.latencyMs);

    if (
      startAuthScope !== this.getCurrentAuthScope() ||
      startSelectionGen !== this.selectionGeneration
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }

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
      this.syncRunClient.setAuthScope(this.getCurrentAuthScope());
    }

    const repositories = this.getAvailableRepositories();
    const targetRepoId =
      options?.selectedRepoId &&
      repositories.some((repo) => repo.repoId === options.selectedRepoId)
        ? options.selectedRepoId
        : this.state.selectedRepoId &&
            repositories.some((repo) => repo.repoId === this.state.selectedRepoId)
          ? this.state.selectedRepoId
          : repositories[0]?.repoId ?? null;

    if (targetRepoId && this.state.selectedRepoId !== targetRepoId) {
      this.state = {
        ...this.state,
        selectedRepoId: targetRepoId,
      };
    }

    let recoveryLookupMode: "detail_get" | "recovery_get" | null = null;
    let recoveryFault: UnconfirmedFaultViewModel | null = null;
    let workspaceAccessRevoked = false;

    if (
      this.state.serviceAuth.status === "authenticated" &&
      this.state.githubConnection.status === "connected" &&
      targetRepoId
    ) {
      const lookupAuthScope = this.getCurrentAuthScope();
      const lookupSelectionGen = this.selectionGeneration;
      const repo = findRepo(targetRepoId);
      const matchingRun = this.findMatchingKnownRun(targetRepoId);

      this.revokedDuringLastPoll = false;
      if (matchingRun) {
        recoveryLookupMode = "detail_get";
        this.syncRunClient.registerKnownRun(matchingRun, repo);
        const detailResult = await this.syncRunClient.getImportStatus({
          runId: matchingRun.runId,
          repo,
        });

        if (
          this.isStaleLookup({
            authScope: lookupAuthScope,
            selectionGen: lookupSelectionGen,
            repoId: targetRepoId,
          })
        ) {
          return {
            ok: false,
            kind: "uncertain_outcome",
            fault: this.buildStaleLookupFault(),
          };
        }

        if (detailResult.ok) {
          let nextRun = detailResult.data.run;
          let nextGitHub = this.state.githubConnection;
          if (this.revokedDuringLastPoll) {
            nextGitHub = { ...SYNTHETIC_REVOKED_GITHUB };
            nextRun = buildPermissionRevokedRun(nextRun);
          }
          this.state = {
            ...this.state,
            latestRun: nextRun,
            latestRunSessionId: this.state.sessionId,
            githubConnection: nextGitHub,
          };
          if (this.revokedDuringLastPoll) {
            this.advanceAuthScope();
          }
        } else {
          const handled = this.applyClientFailureToAdapterState(detailResult);
          if (handled.workspaceAccessRevoked) {
            workspaceAccessRevoked = true;
          }
          recoveryFault = handled.fault;
        }
      } else {
        recoveryLookupMode = "recovery_get";
        this.recoveryLookupCallCount += 1;
        const recoveryResult =
          await this.syncRunClient.recoverLatestRunForRepository({ repo });

        if (
          this.isStaleLookup({
            authScope: lookupAuthScope,
            selectionGen: lookupSelectionGen,
            repoId: targetRepoId,
          })
        ) {
          return {
            ok: false,
            kind: "uncertain_outcome",
            fault: this.buildStaleLookupFault(),
          };
        }

        if (recoveryResult.ok) {
          const recoveredRun = recoveryResult.data.run;
          this.state = {
            ...this.state,
            latestRun: recoveredRun,
            latestRunSessionId: recoveredRun ? this.state.sessionId : null,
          };
        } else {
          const handled = this.applyClientFailureToAdapterState(recoveryResult);
          if (handled.workspaceAccessRevoked) {
            workspaceAccessRevoked = true;
          }
          recoveryFault = handled.fault;
        }
      }
    }

    this.persistSnapshot();

    const checkedAt = nowIso();
    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth, lastCheckedAt: checkedAt },
        githubConnection: { ...this.state.githubConnection },
        repositories: workspaceAccessRevoked
          ? []
          : this.getAvailableRepositories(),
        selectedRepoId: workspaceAccessRevoked ? null : this.state.selectedRepoId,
        latestRun:
          workspaceAccessRevoked || !this.state.latestRun
            ? null
            : structuredClone(this.state.latestRun),
        scenarioId: this.state.scenarioId,
        recoveredFromReload: Boolean(options?.simulateReload || hadPersistedRun),
        recoveryLookupMode,
        recoveryFault,
        workspaceAccessRevoked,
        checkedAt,
      },
    };
  }

  public async recoverRepositoryRun(
    input: RecoverRepositoryRunInput
  ): Promise<AdapterResult<RecoverRepositoryRunPayload>> {
    const requestAuthScope = this.getCurrentAuthScope();
    const requestSelectionGen = this.selectionGeneration;
    const targetRepoId = input.repoId;
    await sleep(this.latencyMs);

    if (
      this.isStaleLookup({
        authScope: requestAuthScope,
        selectionGen: requestSelectionGen,
        repoId: targetRepoId,
      })
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }

    const repo = findRepo(targetRepoId);
    // When caller explicitly passes `knownRunId: null`, use repository recovery GET
    // rather than inheriting any adapter-side cached runId.
    const matchingRun =
      input.knownRunId === null
        ? null
        : this.findMatchingKnownRun(targetRepoId, input.knownRunId);
    this.revokedDuringLastPoll = false;

    if (matchingRun) {
      this.syncRunClient.registerKnownRun(matchingRun, repo);
      const detailResult = await this.syncRunClient.getImportStatus({
        runId: matchingRun.runId,
        repo,
      });

      if (
        this.isStaleLookup({
          authScope: requestAuthScope,
          selectionGen: requestSelectionGen,
          repoId: targetRepoId,
        })
      ) {
        return {
          ok: false,
          kind: "uncertain_outcome",
          fault: this.buildStaleLookupFault(),
        };
      }

      if (!detailResult.ok) {
        return this.applyClientFailureToAdapterState(detailResult);
      }

      let nextRun = detailResult.data.run;
      let nextGitHub = this.state.githubConnection;
      if (this.revokedDuringLastPoll) {
        nextGitHub = { ...SYNTHETIC_REVOKED_GITHUB };
        nextRun = buildPermissionRevokedRun(nextRun);
      }

      this.state = {
        ...this.state,
        latestRun: nextRun,
        latestRunSessionId: this.state.sessionId,
        githubConnection: nextGitHub,
      };
      if (this.revokedDuringLastPoll) {
        this.advanceAuthScope();
      }
      this.persistSnapshot();

      const checkedAt = nowIso();
      return {
        ok: true,
        data: {
          run: structuredClone(nextRun),
          lookupMode: "detail_get",
          githubConnection: { ...nextGitHub },
          recoveryNotice: buildRecoveryNotice({
            run: nextRun,
            lookupMode: "detail_get",
            checkedAt,
          }),
          pendingRetry:
            detailResult.data.pendingRetry ??
            this.syncRunClient.getPendingRetrySummary(),
          checkedAt,
        },
      };
    }

    this.recoveryLookupCallCount += 1;
    const recoveryResult =
      await this.syncRunClient.recoverLatestRunForRepository({ repo });

    if (
      this.isStaleLookup({
        authScope: requestAuthScope,
        selectionGen: requestSelectionGen,
        repoId: targetRepoId,
      })
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }

    if (!recoveryResult.ok) {
      return this.applyClientFailureToAdapterState(recoveryResult);
    }

    const recoveredRun = recoveryResult.data.run;
    this.state = {
      ...this.state,
      latestRun: recoveredRun,
      latestRunSessionId: recoveredRun ? this.state.sessionId : null,
    };
    this.persistSnapshot();

    const checkedAt = nowIso();
    return {
      ok: true,
      data: {
        run: recoveredRun ? structuredClone(recoveredRun) : null,
        lookupMode: "recovery_get",
        githubConnection: { ...this.state.githubConnection },
        recoveryNotice: buildRecoveryNotice({
          run: recoveredRun,
          lookupMode: "recovery_get",
          checkedAt,
        }),
        pendingRetry:
          recoveryResult.data.pendingRetry ??
          this.syncRunClient.getPendingRetrySummary(),
        checkedAt,
      },
    };
  }

  public async signInService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(this.latencyMs);
    const nextSession = this.nextSessionId();
    this.state = {
      ...this.state,
      sessionId: nextSession,
      latestRun: null,
      latestRunSessionId: null,
      serviceAuth: {
        ...SYNTHETIC_AUTHENTICATED_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.advanceAuthScope();
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

  public async signOutService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(this.latencyMs);
    const nextSession = this.nextSessionId();
    this.state = {
      ...this.state,
      sessionId: nextSession,
      latestRun: null,
      latestRunSessionId: null,
      serviceAuth: {
        ...SYNTHETIC_SIGNED_OUT_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.advanceAuthScope();
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
        latestRun: null,
      },
    };
  }

  public async simulateSessionExpired(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      latestRun: null,
      latestRunSessionId: null,
      serviceAuth: {
        ...SYNTHETIC_EXPIRED_AUTH,
        lastCheckedAt: nowIso(),
      },
    };
    this.advanceAuthScope();
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
        latestRun: null,
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

    this.state = {
      ...this.state,
      githubConnection: {
        ...SYNTHETIC_CONNECTED_GITHUB,
        connectedAt: nowIso(),
      },
      latestRun: null,
      latestRunSessionId: null,
    };
    this.advanceAuthScope();
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

  public async disconnectGitHub(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    await sleep(this.latencyMs);
    this.state = {
      ...this.state,
      latestRun: null,
      latestRunSessionId: null,
      githubConnection: { ...SYNTHETIC_DISCONNECTED_GITHUB },
    };
    this.advanceAuthScope();
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        serviceAuth: { ...this.state.serviceAuth },
        githubConnection: { ...this.state.githubConnection },
        repositories: [],
        latestRun: null,
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
        : null;

    this.state = {
      ...this.state,
      githubConnection: { ...SYNTHETIC_REVOKED_GITHUB },
      latestRun: updatedRun,
      latestRunSessionId: updatedRun ? this.state.sessionId : null,
    };
    this.advanceAuthScope();
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
    const requestAuthScope = this.getCurrentAuthScope();
    const requestSelectionGen = this.selectionGeneration;
    await sleep(this.latencyMs);

    if (
      requestAuthScope !== this.getCurrentAuthScope() ||
      requestSelectionGen !== this.selectionGeneration
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          title: "이전 세션의 늦은 응답 무시됨",
          message:
            "요청 전송 중 로그아웃 또는 세션 변경이 발생하여 이전 요청의 응답을 반영하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance: "현재 세션 상태에서 필요한 작업을 진행해 주세요.",
        },
      };
    }

    const repo = findRepo(input.repoId);
    // Persist only the selected repository (NEVER any Idempotency-Key) so that
    // a reload after a dropped 202 response knows which repository to recover.
    if (this.state.selectedRepoId !== repo.repoId) {
      this.state = {
        ...this.state,
        selectedRepoId: repo.repoId,
      };
    }
    this.persistSnapshot();

    const result = await this.syncRunClient.createInitialImport({
      repo,
      authScope: requestAuthScope,
    });

    if (requestAuthScope !== this.getCurrentAuthScope()) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          title: "이전 세션의 늦은 응답 무시됨",
          message:
            "요청 전송 중 로그아웃 또는 세션 변경이 발생하여 이전 요청의 응답을 반영하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance: "현재 세션 상태에서 필요한 작업을 진행해 주세요.",
        },
      };
    }

    if (!result.ok) {
      return this.applyClientFailureToAdapterState(result);
    }

    this.state = {
      ...this.state,
      selectedRepoId: result.data.run.repositoryId,
      latestRun: result.data.run,
      latestRunSessionId: this.state.sessionId,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(result.data.run),
        githubConnection: { ...this.state.githubConnection },
        recoveryNotice: result.data.recoveryNotice,
        pendingRetry:
          result.data.pendingRetry ??
          this.syncRunClient.getPendingRetrySummary(),
      },
    };
  }

  public async pollImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.pollCallCount += 1;
    const requestAuthScope = this.getCurrentAuthScope();
    const requestSelectionGen = this.selectionGeneration;
    const current = this.state.latestRun;
    if (
      this.state.serviceAuth.status !== "authenticated" ||
      this.state.githubConnection.status !== "connected" ||
      !current ||
      current.runId !== runId ||
      this.state.latestRunSessionId !== this.state.sessionId
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }
    const targetRepoId = current.repositoryId;
    await sleep(this.latencyMs);

    if (
      this.isStaleLookup({
        authScope: requestAuthScope,
        selectionGen: requestSelectionGen,
        repoId: targetRepoId,
      })
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }

    const repo = findRepo(targetRepoId);
    this.revokedDuringLastPoll = false;

    const result = await this.syncRunClient.getImportStatus({ runId, repo });

    if (
      this.isStaleLookup({
        authScope: requestAuthScope,
        selectionGen: requestSelectionGen,
        repoId: targetRepoId,
      })
    ) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: this.buildStaleLookupFault(),
      };
    }

    if (!result.ok) {
      return this.applyClientFailureToAdapterState(result);
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
      latestRunSessionId: this.state.sessionId,
      githubConnection: nextGitHub,
    };
    if (this.revokedDuringLastPoll) {
      this.advanceAuthScope();
    }
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(nextRun),
        githubConnection: { ...nextGitHub },
        pendingRetry:
          result.data.pendingRetry ??
          this.syncRunClient.getPendingRetrySummary(),
      },
    };
  }

  public async retryImportRun(
    runId: string,
    options?: RetryImportRunOptions
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.retryImportCallCount += 1;
    const requestAuthScope = this.getCurrentAuthScope();
    await sleep(this.latencyMs);

    if (requestAuthScope !== this.getCurrentAuthScope()) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          title: "이전 세션의 늦은 응답 무시됨",
          message:
            "재시도 요청 중 로그아웃 또는 세션 변경이 발생하여 이전 요청의 응답을 반영하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance: "현재 세션 상태에서 필요한 작업을 진행해 주세요.",
        },
      };
    }

    const current = this.state.latestRun;
    const repo = findRepo(
      current?.repositoryId ??
        this.state.selectedRepoId ??
        SYNTHETIC_PUBLIC_REPOSITORIES[0].repoId
    );

    const result = await this.syncRunClient.retryInitialImport({
      runId,
      repo,
      mode: options?.mode,
      expectedRunVersion: options?.expectedRunVersion,
      previousRun: current,
      authScope: requestAuthScope,
    });

    if (requestAuthScope !== this.getCurrentAuthScope()) {
      return {
        ok: false,
        kind: "uncertain_outcome",
        fault: {
          kind: "uncertain_outcome",
          title: "이전 세션의 늦은 응답 무시됨",
          message:
            "재시도 요청 중 로그아웃 또는 세션 변경이 발생하여 이전 요청의 응답을 반영하지 않았습니다.",
          serverConfirmed: false,
          actionGuidance: "현재 세션 상태에서 필요한 작업을 진행해 주세요.",
        },
      };
    }

    if (!result.ok) {
      return this.applyClientFailureToAdapterState(result);
    }

    this.state = {
      ...this.state,
      selectedRepoId: result.data.run.repositoryId,
      latestRun: result.data.run,
      latestRunSessionId: this.state.sessionId,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(result.data.run),
        githubConnection: { ...this.state.githubConnection },
        recoveryNotice: result.data.recoveryNotice,
        pendingRetry:
          result.data.pendingRetry ??
          this.syncRunClient.getPendingRetrySummary(),
      },
    };
  }

  public async resetActiveRun(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(Math.min(this.latencyMs, 60));
    this.syncRunClient.clearAllCachesAndPendingKeys();
    this.defaultOpenApiTransport.clearIdempotencyLog();
    this.state = {
      ...this.state,
      latestRun: null,
      latestRunSessionId: null,
      pollStep: 0,
      runVersion: 1,
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
