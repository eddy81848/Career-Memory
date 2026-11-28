import { FetchClaimReviewHttpTransport } from "@/adapters/claim-review-http-transport";
import { ContractSyncRunClient } from "@/adapters/contract-sync-run-client";
import { FetchEventReviewHttpTransport } from "@/adapters/event-review-http-transport";
import type {
  AdapterResult,
  ConnectionSyncPayload,
  ImportServiceAdapter,
  RecoverRepositoryRunInput,
  RecoverRepositoryRunPayload,
  RetryImportRunOptions,
  RunSyncPayload,
  StartInitialImportInput,
} from "@/adapters/import-service-adapter";
import { FetchMemoryOpsHttpTransport } from "@/adapters/memory-ops-http-transport";
import type {
  SyntheticHttpRequest,
  SyntheticHttpResponse,
  SyntheticHttpTransport,
} from "@/adapters/synthetic-http-transport";
import type { WorkspaceSourceResourceSummary } from "@/domain/memory-ops-models";
import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PublicRepositoryOption,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";

/**
 * Live PG18 Fullstack client adapters for `NEXT_PUBLIC_API_MODE=live`.
 *
 * Security invariants:
 * - Strictly Same-Origin (`baseUrl: ""`) through Next.js rewrites -> FastAPI.
 * - Zero `localStorage`, `sessionStorage`, or `document.cookie` token/content persistence.
 *   Access tokens live strictly in React/JS memory for the active page lifecycle.
 */

export interface BootstrapResourceItem {
  source_resource_id: string;
  source_account_id: string;
  provider_resource_id: string;
  repository_full_name: string;
  canonical_url: string;
  visibility: string;
  status: string;
  default_branch: string;
  description: string;
  primary_language: string;
  estimated_commits: number;
  estimated_pull_requests: number;
  latest_sync_run_id: string | null;
  latest_sync_run_status: string | null;
  latest_processing_status: string | null;
  latest_import_ready: boolean;
}

export interface SampleRepositoryItem {
  provider_resource_id: string;
  owner: string;
  name: string;
  full_name: string;
  canonical_url: string;
  default_branch: string;
  primary_language: string;
  description: string;
  estimated_commits: number;
  estimated_pull_requests: number;
}

export interface LocalDevBootstrapResponse {
  mode: "local_sample" | "live_public_github" | string;
  workspace_id: string;
  workspace_name: string;
  user_id: string;
  user_display_name: string;
  user_email: string;
  github_login: string;
  access_token: string;
  connected_account_id: string | null;
  resources: BootstrapResourceItem[];
  default_source_resource_id: string | null;
  default_confirmed_event_id: string | null;
  sample_repositories: SampleRepositoryItem[];
}

export function isLiveApiMode(): boolean {
  return process.env.NEXT_PUBLIC_API_MODE === "live";
}

export async function fetchLocalDevBootstrap(
  fetchImpl: typeof fetch = fetch
): Promise<LocalDevBootstrapResponse> {
  const res = await fetchImpl("/local-dev/bootstrap", {
    method: "GET",
    cache: "no-store",
    headers: {
      Accept: "application/json",
    },
  });
  if (!res.ok) {
    throw new Error(`Failed to load /local-dev/bootstrap (HTTP ${res.status})`);
  }
  return (await res.json()) as LocalDevBootstrapResponse;
}

export function buildLiveAuthHeaders(
  bootstrap: Pick<LocalDevBootstrapResponse, "access_token" | "workspace_id">
): Record<string, string> {
  return {
    Authorization: `Bearer ${bootstrap.access_token}`,
    "X-Workspace-ID": bootstrap.workspace_id,
  };
}

export function mapBootstrapResourcesToMemoryOpsRepos(
  resources: BootstrapResourceItem[]
): WorkspaceSourceResourceSummary[] {
  return resources.map((res) => ({
    source_resource_id: res.source_resource_id,
    repository_full_name: res.repository_full_name,
    provider: "GITHUB",
    visibility: "PUBLIC",
    fence_active: res.status === "DELETED",
    deleted: res.status === "DELETED",
    active_deletion_job_id: null,
  }));
}

export function mapBootstrapResourceToPublicOption(
  res: BootstrapResourceItem
): PublicRepositoryOption {
  return {
    repoId: res.source_resource_id,
    fullName: res.repository_full_name,
    visibility: "public",
    defaultBranch: res.default_branch || "main",
    primaryLanguage: res.primary_language || "Code",
    description:
      res.description || `Allowlisted public repository (${res.repository_full_name})`,
    lastPushedAt: new Date().toISOString(),
    estimatedCommits: res.estimated_commits ?? 0,
    estimatedPullRequests: res.estimated_pull_requests ?? 0,
  };
}

export function createLiveEventReviewTransport(
  bootstrap: LocalDevBootstrapResponse,
  fetchImpl: typeof fetch = fetch
): FetchEventReviewHttpTransport {
  return new FetchEventReviewHttpTransport({
    baseUrl: "",
    fetchImpl,
    defaultHeaders: buildLiveAuthHeaders(bootstrap),
  });
}

export function createLiveClaimReviewTransport(
  bootstrap: LocalDevBootstrapResponse,
  fetchImpl: typeof fetch = fetch
): FetchClaimReviewHttpTransport {
  return new FetchClaimReviewHttpTransport({
    baseUrl: "",
    workspaceId: bootstrap.workspace_id,
    authorization: `Bearer ${bootstrap.access_token}`,
    fetchImpl,
  });
}

export function createLiveMemoryOpsTransport(
  bootstrap: LocalDevBootstrapResponse,
  fetchImpl: typeof fetch = fetch
): FetchMemoryOpsHttpTransport {
  return new FetchMemoryOpsHttpTransport({
    baseUrl: "",
    workspaceId: bootstrap.workspace_id,
    authorization: `Bearer ${bootstrap.access_token}`,
    fetchImpl,
  });
}

export class SameOriginFetchSyncRunTransport implements SyntheticHttpTransport {
  private readonly defaultHeaders: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: {
    defaultHeaders: Record<string, string>;
    fetchImpl?: typeof fetch;
  }) {
    this.defaultHeaders = { ...options.defaultHeaders };
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  public async send(
    request: SyntheticHttpRequest
  ): Promise<SyntheticHttpResponse> {
    const res = await this.fetchImpl(request.path, {
      method: request.method,
      cache: "no-store",
      headers: {
        ...this.defaultHeaders,
        ...request.headers,
      },
      body: request.body,
    });
    const rawBody = await res.text();
    const headers: Record<string, string> = {};
    res.headers.forEach((value, key) => {
      headers[key] = value;
    });
    return {
      status: res.status,
      headers,
      rawBody,
    };
  }
}

function buildLiveNetworkFault(message: string): UnconfirmedFaultViewModel {
  return {
    kind: "network_error",
    title: "로컬 PG18 백엔드 통신 오류",
    message,
    serverConfirmed: false,
    actionGuidance:
      "FastAPI 서버와 PostgreSQL 18 연결 상태를 확인한 뒤 상태 새로고침을 시도하세요.",
  };
}

export class LiveImportServiceAdapter implements ImportServiceAdapter {
  private bootstrap: LocalDevBootstrapResponse;
  private readonly fetchImpl: typeof fetch;
  private readonly syncClient: ContractSyncRunClient;
  private repositories: PublicRepositoryOption[];
  private selectedRepoId: string | null;
  private connected: boolean;
  private signedIn: boolean;
  private readonly runRepoByRunId = new Map<string, PublicRepositoryOption>();

  constructor(
    bootstrap: LocalDevBootstrapResponse,
    options?: { fetchImpl?: typeof fetch }
  ) {
    this.bootstrap = bootstrap;
    this.fetchImpl = options?.fetchImpl ?? fetch;
    this.syncClient = new ContractSyncRunClient({
      transport: new SameOriginFetchSyncRunTransport({
        defaultHeaders: buildLiveAuthHeaders(bootstrap),
        fetchImpl: this.fetchImpl,
      }),
      initialAuthScope: `${bootstrap.user_id}:${bootstrap.workspace_id}`,
    });
    this.repositories = bootstrap.resources.map(mapBootstrapResourceToPublicOption);
    this.selectedRepoId =
      bootstrap.default_source_resource_id ??
      this.repositories[0]?.repoId ??
      null;
    this.connected = Boolean(bootstrap.connected_account_id);
    this.signedIn = true;
  }

  private buildServiceAuth(): ServiceAuthViewModel {
    const now = new Date().toISOString();
    if (!this.signedIn) {
      return {
        status: "signed_out",
        account: null,
        sessionNote: "로컬 PG18 세션이 로그아웃되었습니다.",
        lastCheckedAt: now,
      };
    }
    return {
      status: "authenticated",
      account: {
        syntheticUserId: this.bootstrap.user_id,
        displayName: this.bootstrap.user_display_name,
        email: this.bootstrap.user_email,
      },
      sessionNote: `ES256 JWT + PG18 FORCE RLS 인증 완료 (Workspace: ${this.bootstrap.workspace_name})`,
      lastCheckedAt: now,
    };
  }

  private buildGitHubConnection(): GitHubConnectionViewModel {
    if (!this.connected) {
      return {
        status: "disconnected",
        accountHandle: null,
        connectionRef: null,
        scopeSummary:
          'GitHub OAuth App 최소 읽기 연결 (scope="read:user" 단독 · 저장소/쓰기 스코프 0개)',
        revocationReason: null,
        connectedAt: null,
      };
    }
    return {
      status: "connected",
      accountHandle: `@${this.bootstrap.github_login}`,
      connectionRef: this.bootstrap.connected_account_id,
      scopeSummary:
        this.bootstrap.mode === "live_public_github"
          ? 'Live Public GitHub 읽기 전용 연결 (scope="read:user" · 공개 저장소 Allowlist 전용)'
          : 'Local Sample PG18 읽기 전용 연결 (scope="read:user" · 0 외부 네트워크 · ₩0)',
      revocationReason: null,
      connectedAt: new Date().toISOString(),
    };
  }

  private findRepo(repoId: string | null): PublicRepositoryOption | null {
    if (!repoId) return this.repositories[0] ?? null;
    return (
      this.repositories.find((r) => r.repoId === repoId) ??
      this.repositories[0] ??
      null
    );
  }

  public selectRepository(repoId: string): void {
    this.selectedRepoId = repoId;
    this.syncClient.clearPendingKeysForTargetChange(repoId);
  }

  private async refreshSourceResourcesFromApi(): Promise<void> {
    const res = await this.fetchImpl("/v1/source-resources", {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...buildLiveAuthHeaders(this.bootstrap),
      },
    });
    if (!res.ok) return;
    const data = (await res.json()) as {
      items?: Array<{
        id: string;
        name: string;
        visibility: string;
        status: string;
      }>;
    };
    if (!Array.isArray(data.items)) return;

    const existingMeta = new Map(
      this.bootstrap.resources.map((r) => [r.source_resource_id, r])
    );
    this.repositories = data.items
      .filter((item) => item.status !== "DELETED")
      .map((item) => {
        const meta = existingMeta.get(item.id);
        return {
          repoId: item.id,
          fullName: item.name,
          visibility: "public" as const,
          defaultBranch: meta?.default_branch ?? "main",
          primaryLanguage: meta?.primary_language ?? "Code",
          description:
            meta?.description ?? `Allowlisted public repository (${item.name})`,
          lastPushedAt: new Date().toISOString(),
          estimatedCommits: meta?.estimated_commits ?? 0,
          estimatedPullRequests: meta?.estimated_pull_requests ?? 0,
        };
      });
  }

  public async bootstrapSession(options?: {
    simulateReload?: boolean;
    selectedRepoId?: string | null;
  }): Promise<AdapterResult<BootstrapPayload>> {
    try {
      await this.refreshSourceResourcesFromApi();
    } catch {
      // Fall back to initial bootstrap resources if list refresh temporarily fails
    }

    const targetRepo = this.findRepo(
      options?.selectedRepoId ?? this.selectedRepoId
    );
    this.selectedRepoId = targetRepo?.repoId ?? null;

    let latestRun: ImportRunViewModel | null = null;
    if (this.signedIn && this.connected && targetRepo) {
      const recovered = await this.syncClient.recoverLatestRunForRepository({
        repo: targetRepo,
      });
      if (recovered.ok && recovered.data.run) {
        latestRun = recovered.data.run;
        this.runRepoByRunId.set(latestRun.runId, targetRepo);
      }
    }

    return {
      ok: true,
      data: {
        serviceAuth: this.buildServiceAuth(),
        githubConnection: this.buildGitHubConnection(),
        repositories: [...this.repositories],
        selectedRepoId: this.selectedRepoId,
        latestRun,
        scenarioId: "success_full",
        recoveredFromReload: Boolean(options?.simulateReload && latestRun),
        recoveryLookupMode: latestRun ? "recovery_get" : null,
        recoveryFault: null,
        workspaceAccessRevoked: false,
        checkedAt: new Date().toISOString(),
      },
    };
  }

  public async signInService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    this.signedIn = true;
    return {
      ok: true,
      data: {
        serviceAuth: this.buildServiceAuth(),
        githubConnection: this.buildGitHubConnection(),
        repositories: [...this.repositories],
      },
    };
  }

  public async signOutService(): Promise<AdapterResult<ConnectionSyncPayload>> {
    this.signedIn = false;
    this.syncClient.clearAllCachesAndPendingKeys();
    return {
      ok: true,
      data: {
        serviceAuth: this.buildServiceAuth(),
        githubConnection: this.buildGitHubConnection(),
        repositories: [],
        latestRun: null,
      },
    };
  }

  public async connectGitHub(): Promise<AdapterResult<ConnectionSyncPayload>> {
    try {
      this.bootstrap = await fetchLocalDevBootstrap(this.fetchImpl);
      this.connected = Boolean(this.bootstrap.connected_account_id);
      await this.refreshSourceResourcesFromApi();
      return {
        ok: true,
        data: {
          serviceAuth: this.buildServiceAuth(),
          githubConnection: this.buildGitHubConnection(),
          repositories: [...this.repositories],
        },
      };
    } catch (err) {
      const fault = buildLiveNetworkFault(
        err instanceof Error ? err.message : "GitHub 연결 상태 조회에 실패했습니다."
      );
      return { ok: false, kind: fault.kind, fault };
    }
  }

  public async disconnectGitHub(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    if (this.bootstrap.connected_account_id) {
      try {
        await this.fetchImpl(
          `/v1/source-accounts/${encodeURIComponent(this.bootstrap.connected_account_id)}/disconnect`,
          {
            method: "POST",
            cache: "no-store",
            headers: buildLiveAuthHeaders(this.bootstrap),
          }
        );
      } catch {
        // Proceed with local state disconnect notice if network fails
      }
    }
    this.connected = false;
    this.syncClient.clearAllCachesAndPendingKeys();
    return {
      ok: true,
      data: {
        serviceAuth: this.buildServiceAuth(),
        githubConnection: this.buildGitHubConnection(),
        repositories: [],
        latestRun: null,
      },
    };
  }

  public async startInitialImport(
    input: StartInitialImportInput
  ): Promise<AdapterResult<RunSyncPayload>> {
    const repo = this.findRepo(input.repoId);
    if (!repo) {
      const fault: UnconfirmedFaultViewModel = {
        kind: "resource_not_found",
        title: "선택된 저장소를 찾을 수 없습니다",
        message: `source_resource_id=${input.repoId}가 Allowlist에 없습니다.`,
        serverConfirmed: false,
        actionGuidance: "저장소 목록을 새로고침한 뒤 다시 선택하세요.",
      };
      return { ok: false, kind: fault.kind, fault };
    }
    this.selectedRepoId = repo.repoId;
    const res = await this.syncClient.createInitialImport({ repo });
    if (!res.ok) return res;
    this.runRepoByRunId.set(res.data.run.runId, repo);
    return {
      ok: true,
      data: {
        run: res.data.run,
        githubConnection: this.buildGitHubConnection(),
        recoveryNotice: res.data.recoveryNotice,
        pendingRetry: res.data.pendingRetry,
      },
    };
  }

  public async pollImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    const repo =
      this.runRepoByRunId.get(runId) ?? this.findRepo(this.selectedRepoId);
    if (!repo) {
      const fault = buildLiveNetworkFault("활성 저장소 컨텍스트가 없습니다.");
      return { ok: false, kind: fault.kind, fault };
    }
    const res = await this.syncClient.getImportStatus({ runId, repo });
    if (!res.ok) return res;
    this.runRepoByRunId.set(res.data.run.runId, repo);
    return {
      ok: true,
      data: {
        run: res.data.run,
        githubConnection: this.buildGitHubConnection(),
        recoveryNotice: res.data.recoveryNotice,
        pendingRetry: res.data.pendingRetry,
      },
    };
  }

  public async recoverRepositoryRun(
    input: RecoverRepositoryRunInput
  ): Promise<AdapterResult<RecoverRepositoryRunPayload>> {
    const repo = this.findRepo(input.repoId);
    if (!repo) {
      const fault = buildLiveNetworkFault("선택된 저장소를 찾을 수 없습니다.");
      return { ok: false, kind: fault.kind, fault };
    }
    this.selectedRepoId = repo.repoId;
    const res = await this.syncClient.recoverLatestRunForRepository({ repo });
    if (!res.ok) return res;
    if (res.data.run) {
      this.runRepoByRunId.set(res.data.run.runId, repo);
    }
    const now = new Date().toISOString();
    return {
      ok: true,
      data: {
        run: res.data.run,
        lookupMode: "recovery_get",
        githubConnection: this.buildGitHubConnection(),
        recoveryNotice: {
          kind: res.data.run
            ? res.data.run.status === "running"
              ? "restored_running"
              : "restored_terminal"
            : "checked_clean",
          message: res.data.run
            ? `PostgreSQL 18에서 저장소(${repo.fullName})의 최신 SyncRun 상태를 복구했습니다.`
            : `저장소(${repo.fullName})에는 아직 실행된 최초 가져오기가 없습니다.`,
          restoredAt: now,
        },
        pendingRetry: res.data.pendingRetry,
        checkedAt: now,
      },
    };
  }

  public async retryImportRun(
    runId: string,
    options?: RetryImportRunOptions
  ): Promise<AdapterResult<RunSyncPayload>> {
    const repo =
      this.runRepoByRunId.get(runId) ?? this.findRepo(this.selectedRepoId);
    if (!repo) {
      const fault = buildLiveNetworkFault("재시도할 저장소 컨텍스트가 없습니다.");
      return { ok: false, kind: fault.kind, fault };
    }
    const res = await this.syncClient.retryInitialImport({
      runId,
      repo,
      expectedRunVersion: options?.expectedRunVersion,
      mode: options?.mode,
    });
    if (!res.ok) return res;
    this.runRepoByRunId.set(res.data.run.runId, repo);
    return {
      ok: true,
      data: {
        run: res.data.run,
        githubConnection: this.buildGitHubConnection(),
        recoveryNotice: res.data.recoveryNotice,
        pendingRetry: res.data.pendingRetry,
      },
    };
  }

  public async resetActiveRun(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    this.syncClient.clearPendingKeys();
    return {
      ok: true,
      data: {
        serviceAuth: this.buildServiceAuth(),
        githubConnection: this.buildGitHubConnection(),
        repositories: [...this.repositories],
        latestRun: null,
      },
    };
  }
}
