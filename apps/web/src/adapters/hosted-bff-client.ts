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
import {
  mapBootstrapResourcesToMemoryOpsRepos,
  mapBootstrapResourceToPublicOption,
  type BootstrapResourceItem,
  type SampleRepositoryItem,
} from "@/adapters/live-runtime-client";
import { FetchMemoryOpsHttpTransport } from "@/adapters/memory-ops-http-transport";
import type {
  SyntheticHttpRequest,
  SyntheticHttpResponse,
  SyntheticHttpTransport,
} from "@/adapters/synthetic-http-transport";
import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PublicRepositoryOption,
  ServiceAuthViewModel,
  UnconfirmedFaultViewModel,
} from "@/domain/view-models";

/**
 * Hosted OD-010 Next.js BFF client adapters for `NEXT_PUBLIC_API_MODE=hosted`.
 *
 * Security invariants:
 * - Strictly Same-Origin through `/auth/*` and `/bff/v1/*` Next.js Route Handlers.
 * - ZERO `Authorization: Bearer` header in browser JS (raw Supabase JWTs live
 *   exclusively inside encrypted `HttpOnly; Secure; SameSite=Lax` cookies).
 * - Automatically attaches `X-Workspace-ID` on all requests and session-bound
 *   `X-CSRF-Token` on state-changing methods (`POST`, `PATCH`, `PUT`, `DELETE`).
 * - ZERO `localStorage`, `sessionStorage`, or `document.cookie` usage.
 */

const STATE_CHANGING_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

export interface HostedBackendSession {
  mode: string;
  workspace_id: string;
  workspace_name: string;
  user_id: string;
  user_display_name: string;
  user_email: string;
  github_login: string;
  connected_account_id: string | null;
  resources: BootstrapResourceItem[];
  default_source_resource_id: string | null;
  default_confirmed_event_id: string | null;
  sample_repositories: SampleRepositoryItem[];
}

export interface HostedAuthSessionResponse {
  authenticated: boolean;
  csrf_token: string | null;
  session: HostedBackendSession | null;
  reason?: string;
}

export class HostedAuthUnavailableError extends Error {
  public readonly status: number;
  public readonly code: string;

  constructor(message: string, status = 503, code = "AUTH_VERIFICATION_UNAVAILABLE") {
    super(message);
    this.name = "HostedAuthUnavailableError";
    this.status = status;
    this.code = code;
  }
}

export function isHostedApiMode(): boolean {
  return process.env.NEXT_PUBLIC_API_MODE === "hosted";
}

export async function fetchHostedAuthSession(
  fetchImpl: typeof fetch = fetch
): Promise<HostedAuthSessionResponse> {
  const res = await fetchImpl("/auth/session", {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      Accept: "application/json",
    },
  });

  if (res.status === 401) {
    return {
      authenticated: false,
      csrf_token: null,
      session: null,
      reason: "SESSION_TERMINATED",
    };
  }

  if (res.status === 503) {
    throw new HostedAuthUnavailableError(
      "인증 확인 서버가 일시적으로 응답하지 않습니다(503). 로그인 세션은 유지되며, 잠시 후 다시 확인할 수 있습니다."
    );
  }

  if (!res.ok) {
    throw new Error(`Failed to load /auth/session (HTTP ${res.status})`);
  }

  return (await res.json()) as HostedAuthSessionResponse;
}

export async function logoutHostedSession(
  csrfToken: string,
  fetchImpl: typeof fetch = fetch,
  scope: "local" | "global" = "local"
): Promise<void> {
  const res = await fetchImpl("/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify({ scope }),
  });
  if (!res.ok) {
    throw new Error(`Failed to logout via /auth/logout (HTTP ${res.status})`);
  }
}

/**
 * Wraps `fetch` for all `/bff/v1/*` requests:
 * - Always sends `credentials: "same-origin"` and `cache: "no-store"`.
 * - Always strips any `Authorization` header defensively.
 * - Sets `X-Workspace-ID`.
 * - Attaches `X-CSRF-Token` on state-changing HTTP methods.
 */
export function createHostedBffFetch(options: {
  workspaceId: string;
  csrfToken: string;
  fetchImpl?: typeof fetch;
}): typeof fetch {
  const baseFetch = options.fetchImpl ?? fetch;

  return ((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method || "GET").trim().toUpperCase();
    const headers = new Headers(init?.headers);
    headers.delete("authorization");
    headers.set("X-Workspace-ID", options.workspaceId);
    if (STATE_CHANGING_METHODS.has(method)) {
      headers.set("X-CSRF-Token", options.csrfToken);
    }

    return baseFetch(input, {
      ...init,
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers,
    });
  }) as typeof fetch;
}

export function createHostedEventReviewTransport(
  session: HostedBackendSession,
  csrfToken: string,
  fetchImpl: typeof fetch = fetch
): FetchEventReviewHttpTransport {
  const bffFetch = createHostedBffFetch({
    workspaceId: session.workspace_id,
    csrfToken,
    fetchImpl,
  });
  return new FetchEventReviewHttpTransport({
    baseUrl: "/bff",
    fetchImpl: bffFetch,
    defaultHeaders: {
      "X-Workspace-ID": session.workspace_id,
    },
  });
}

export function createHostedClaimReviewTransport(
  session: HostedBackendSession,
  csrfToken: string,
  fetchImpl: typeof fetch = fetch
): FetchClaimReviewHttpTransport {
  const bffFetch = createHostedBffFetch({
    workspaceId: session.workspace_id,
    csrfToken,
    fetchImpl,
  });
  return new FetchClaimReviewHttpTransport({
    baseUrl: "/bff",
    workspaceId: session.workspace_id,
    fetchImpl: bffFetch,
  });
}

export function createHostedMemoryOpsTransport(
  session: HostedBackendSession,
  csrfToken: string,
  fetchImpl: typeof fetch = fetch
): FetchMemoryOpsHttpTransport {
  const bffFetch = createHostedBffFetch({
    workspaceId: session.workspace_id,
    csrfToken,
    fetchImpl,
  });
  return new FetchMemoryOpsHttpTransport({
    baseUrl: "/bff",
    workspaceId: session.workspace_id,
    fetchImpl: bffFetch,
  });
}

export class SameOriginBffSyncRunTransport implements SyntheticHttpTransport {
  private readonly bffFetch: typeof fetch;
  private readonly basePathPrefix: string;

  constructor(options: {
    workspaceId: string;
    csrfToken: string;
    fetchImpl?: typeof fetch;
    basePathPrefix?: string;
  }) {
    this.bffFetch = createHostedBffFetch({
      workspaceId: options.workspaceId,
      csrfToken: options.csrfToken,
      fetchImpl: options.fetchImpl,
    });
    this.basePathPrefix = (options.basePathPrefix ?? "/bff").replace(/\/+$/, "");
  }

  public async send(
    request: SyntheticHttpRequest
  ): Promise<SyntheticHttpResponse> {
    const targetPath = request.path.startsWith(this.basePathPrefix)
      ? request.path
      : `${this.basePathPrefix}${request.path}`;
    const res = await this.bffFetch(targetPath, {
      method: request.method,
      headers: request.headers,
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

function buildHostedNetworkFault(message: string): UnconfirmedFaultViewModel {
  return {
    kind: "network_error",
    title: "Hosted BFF 백엔드 통신 오류",
    message,
    serverConfirmed: false,
    actionGuidance:
      "세션은 유지됩니다. 잠시 후 읽기 전용 상태 새로고침으로 확인하세요.",
  };
}

export class HostedImportServiceAdapter implements ImportServiceAdapter {
  private session: HostedBackendSession;
  private readonly csrfToken: string;
  private readonly rawFetch: typeof fetch;
  private readonly bffFetch: typeof fetch;
  private readonly syncClient: ContractSyncRunClient;
  private readonly onSignedOut?: () => void;
  private repositories: PublicRepositoryOption[];
  private selectedRepoId: string | null;
  private connected: boolean;
  private signedIn: boolean;
  private readonly runRepoByRunId = new Map<string, PublicRepositoryOption>();

  constructor(
    session: HostedBackendSession,
    csrfToken: string,
    options?: {
      fetchImpl?: typeof fetch;
      onSignedOut?: () => void;
    }
  ) {
    this.session = session;
    this.csrfToken = csrfToken;
    this.rawFetch = options?.fetchImpl ?? fetch;
    this.onSignedOut = options?.onSignedOut;
    this.bffFetch = createHostedBffFetch({
      workspaceId: session.workspace_id,
      csrfToken,
      fetchImpl: this.rawFetch,
    });
    this.syncClient = new ContractSyncRunClient({
      transport: new SameOriginBffSyncRunTransport({
        workspaceId: session.workspace_id,
        csrfToken,
        fetchImpl: this.rawFetch,
      }),
      initialAuthScope: `${session.user_id}:${session.workspace_id}`,
    });
    this.repositories = session.resources.map(mapBootstrapResourceToPublicOption);
    this.selectedRepoId =
      session.default_source_resource_id ??
      this.repositories[0]?.repoId ??
      null;
    this.connected = Boolean(session.connected_account_id);
    this.signedIn = true;
  }

  private buildServiceAuth(): ServiceAuthViewModel {
    const now = new Date().toISOString();
    if (!this.signedIn) {
      return {
        status: "signed_out",
        account: null,
        sessionNote: "Hosted Supabase Auth 세션이 로그아웃되었습니다.",
        lastCheckedAt: now,
      };
    }
    return {
      status: "authenticated",
      account: {
        syntheticUserId: this.session.user_id,
        displayName: this.session.user_display_name,
        email: this.session.user_email,
      },
      sessionNote: `OD-010 HttpOnly Cookie BFF 인증 완료 (Workspace: ${this.session.workspace_name})`,
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
          'GitHub 저장소 읽기 전용 연결 (OD-011 · 서비스 로그인과 분리된 수집 경계)',
        revocationReason: null,
        connectedAt: null,
      };
    }
    return {
      status: "connected",
      accountHandle: `@${this.session.github_login}`,
      connectionRef: this.session.connected_account_id,
      scopeSummary:
        'Hosted BFF 공개 저장소 읽기 전용 연결 (scope="read:user" · Allowlist 전용)',
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
    const res = await this.bffFetch("/bff/v1/source-resources", {
      method: "GET",
      headers: {
        Accept: "application/json",
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
      this.session.resources.map((r) => [r.source_resource_id, r])
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
      // Keep initial session resources if list refresh temporarily fails
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
    try {
      await logoutHostedSession(this.csrfToken, this.rawFetch);
    } catch {
      // Always transition local UI state to signed-out
    }
    this.signedIn = false;
    this.syncClient.clearAllCachesAndPendingKeys();
    this.onSignedOut?.();
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
      const auth = await fetchHostedAuthSession(this.rawFetch);
      if (auth.authenticated && auth.session) {
        this.session = auth.session;
        this.connected = Boolean(auth.session.connected_account_id);
        await this.refreshSourceResourcesFromApi();
      }
      return {
        ok: true,
        data: {
          serviceAuth: this.buildServiceAuth(),
          githubConnection: this.buildGitHubConnection(),
          repositories: [...this.repositories],
        },
      };
    } catch (err) {
      const fault = buildHostedNetworkFault(
        err instanceof Error ? err.message : "GitHub 연결 상태 조회에 실패했습니다."
      );
      return { ok: false, kind: fault.kind, fault };
    }
  }

  public async disconnectGitHub(): Promise<
    AdapterResult<ConnectionSyncPayload>
  > {
    if (this.session.connected_account_id) {
      try {
        await this.bffFetch(
          `/bff/v1/source-accounts/${encodeURIComponent(this.session.connected_account_id)}/disconnect`,
          {
            method: "POST",
          }
        );
      } catch {
        // Proceed with local disconnect notice
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
      const fault = buildHostedNetworkFault("활성 저장소 컨텍스트가 없습니다.");
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
      const fault = buildHostedNetworkFault("선택된 저장소를 찾을 수 없습니다.");
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
            ? `Hosted BFF를 통해 저장소(${repo.fullName})의 최신 SyncRun 상태를 복구했습니다.`
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
      const fault = buildHostedNetworkFault("재시도할 저장소 컨텍스트가 없습니다.");
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

export { mapBootstrapResourcesToMemoryOpsRepos };
