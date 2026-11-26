import type { BootstrapPayload } from "@/domain/state-machine";
import type {
  GitHubConnectionViewModel,
  ImportRunViewModel,
  PrototypeScenarioId,
  PublicRepositoryOption,
  ServiceAuthViewModel,
} from "@/domain/view-models";
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

/**
 * LOCAL UI FIXTURE ADAPTER ONLY.
 *
 * - Does NOT make external HTTP calls, does NOT connect to GitHub, does NOT
 *   issue cookies, and does NOT finalize transport or session storage.
 * - Uses an isolated `sessionStorage` prototype harness (with in-memory fallback)
 *   strictly to demonstrate reload state recovery in a local browser.
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

function buildRunningRun(params: {
  runId: string;
  repo: PublicRepositoryOption;
  attemptNumber: number;
  startedAt: string;
  stage: "fetching_snapshot" | "snapshot_ready_deriving";
}): ImportRunViewModel {
  const updatedAt = nowIso();
  if (params.stage === "fetching_snapshot") {
    return {
      runId: params.runId,
      repositoryId: params.repo.repoId,
      repositoryFullName: params.repo.fullName,
      status: "running",
      attemptNumber: params.attemptNumber,
      startedAt: params.startedAt,
      updatedAt,
      progressMessage:
        "서버가 최초 가져오기 작업을 수락했습니다. 공개 저장소 커밋·PR 원본 스냅샷을 수집 중입니다.",
      readiness: {
        importReady: false,
        importReadyState: "in_progress",
        importReadySummary:
          "원본 스냅샷 수집 진행 중 (import_ready 아직 미충족)",
        derivedAllReady: false,
        derivedStages: [
          {
            key: "event_normalization",
            label: "작업 이벤트 정규화",
            status: "not_started",
            detail: "원본 스냅샷 완료 후 시작됩니다.",
          },
          {
            key: "timeline_indexing",
            label: "타임라인 준비",
            status: "not_started",
            detail: "이벤트 정규화 이후 시작됩니다.",
          },
        ],
      },
      retryPolicy: {
        allowed: false,
        reason: "가져오기 작업이 실행 중이므로 재시도할 수 없습니다.",
        nextAllowedAt: null,
      },
      summary: null,
      partialDetail: null,
      limitDetail: null,
      error: null,
    };
  }

  return {
    runId: params.runId,
    repositoryId: params.repo.repoId,
    repositoryFullName: params.repo.fullName,
    status: "running",
    attemptNumber: params.attemptNumber,
    startedAt: params.startedAt,
    updatedAt,
    progressMessage:
      "원본 스냅샷 수집은 완료(import_ready=true)되었으며, 파생 단계가 계속 진행 중입니다.",
    readiness: {
      importReady: true,
      importReadyState: "ready",
      importReadySummary:
        "원본 스냅샷 저장 완료 (import_ready=true). 파생 단계와 별도로 확인 가능합니다.",
      derivedAllReady: false,
      derivedStages: [
        {
          key: "event_normalization",
          label: "작업 이벤트 정규화",
          status: "in_progress",
          detail: "수집된 커밋·PR에서 읽기 전용 작업 이벤트를 추출하고 있습니다.",
        },
        {
          key: "timeline_indexing",
          label: "타임라인 준비",
          status: "pending",
          detail: "이벤트 정규화 완료를 대기하고 있습니다.",
        },
      ],
    },
    retryPolicy: {
      allowed: false,
      reason: "파생 단계가 실행 중입니다.",
      nextAllowedAt: null,
    },
    summary: {
      commitsCaptured: params.repo.estimatedCommits,
      pullRequestsCaptured: params.repo.estimatedPullRequests,
      capturedAt: updatedAt,
    },
    partialDetail: null,
    limitDetail: null,
    error: null,
  };
}

function buildTerminalRunForScenario(params: {
  scenarioId: PrototypeScenarioId;
  runId: string;
  repo: PublicRepositoryOption;
  attemptNumber: number;
  startedAt: string;
  forceRecoverySuccess?: boolean;
}): {
  run: ImportRunViewModel;
  githubRevoked?: boolean;
} {
  const updatedAt = nowIso();
  const effectiveScenario: PrototypeScenarioId = params.forceRecoverySuccess
    ? "success_full"
    : params.scenarioId;

  switch (effectiveScenario) {
    case "success_full":
    case "running_slow_for_reload":
    case "session_expired_on_submit":
    case "network_error_on_submit":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "succeeded",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "원본 스냅샷 가져오기(import_ready)와 파생 단계가 모두 준비되었습니다.",
          readiness: {
            importReady: true,
            importReadyState: "ready",
            importReadySummary: "원본 스냅샷 저장 및 무결성 확인 완료",
            derivedAllReady: true,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "ready",
                detail: "읽기 전용 이벤트 목록 생성 완료 (편집 범위 외)",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "ready",
                detail: "기본 시간순 조회 인덱스 준비 완료",
              },
            ],
          },
          retryPolicy: {
            allowed: false,
            reason:
              "일회성 최초 가져오기가 이미 성공 완료되어 재시도 대상이 아닙니다.",
            nextAllowedAt: null,
          },
          summary: {
            commitsCaptured: params.repo.estimatedCommits,
            pullRequestsCaptured: params.repo.estimatedPullRequests,
            capturedAt: updatedAt,
          },
          partialDetail: null,
          limitDetail: null,
          error: null,
        },
      };

    case "success_import_ready_derived_pending":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "succeeded",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "원본 스냅샷(import_ready=true)은 준비되었으나, 파생 단계 중 타임라인 준비가 실패하여 후속 확인이 필요합니다.",
          readiness: {
            importReady: true,
            importReadyState: "ready",
            importReadySummary:
              "원본 스냅샷 저장 완료 (import_ready=true). 파생 단계 완료와 구분됩니다.",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "ready",
                detail: "원본 스냅샷 기준 작업 이벤트 정규화 완료",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "failed",
                detail:
                  "파생 인덱스 워커 지연으로 아직 준비되지 않았습니다. 원본 스냅샷 자체는 보존되어 있습니다.",
              },
            ],
          },
          retryPolicy: {
            allowed: false,
            reason:
              "원본 스냅샷 가져오기는 완료되었으며 파생 단계 재실행 정책은 별도 계약 확정 전까지 비노출입니다.",
            nextAllowedAt: null,
          },
          summary: {
            commitsCaptured: params.repo.estimatedCommits,
            pullRequestsCaptured: params.repo.estimatedPullRequests,
            capturedAt: updatedAt,
          },
          partialDetail: null,
          limitDetail: null,
          error: null,
        },
      };

    case "partial_retryable":
    case "partial_non_retryable": {
      const allowed = effectiveScenario === "partial_retryable";
      const importedCommits = Math.max(10, Math.floor(params.repo.estimatedCommits * 0.72));
      const skippedCommits = params.repo.estimatedCommits - importedCommits;
      const importedPrs = Math.max(2, Math.floor(params.repo.estimatedPullRequests * 0.65));
      const skippedPrs = params.repo.estimatedPullRequests - importedPrs;

      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "partial",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "일부 스냅샷만 수집된 부분 결과입니다. 누락 항목이 있어 전체 성공으로 간주하지 않습니다.",
          readiness: {
            importReady: false,
            importReadyState: "partial",
            importReadySummary:
              "부분 스냅샷만 수집됨 (PARTIAL -> import_ready=false, 전체 성공 아님)",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "ready",
                detail: "수집된 일부 항목에 대해서만 정규화가 완료되었습니다.",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "skipped",
                detail:
                  "누락 구간이 남아 있어 전체 타임라인 준비를 건너뛰었습니다.",
              },
            ],
          },
          retryPolicy: allowed
            ? {
                allowed: true,
                reason:
                  "서버가 누락 구간 재수집을 위한 재시도를 허용했습니다.",
                nextAllowedAt: null,
              }
            : {
                allowed: false,
                reason:
                  "서버 정책상 해당 저장소의 누락 항목(대용량 바이너리 diff 제외)은 재시도해도 변경되지 않아 재시도가 허용되지 않습니다.",
                nextAllowedAt: null,
              },
          summary: {
            commitsCaptured: importedCommits,
            pullRequestsCaptured: importedPrs,
            capturedAt: updatedAt,
          },
          partialDetail: {
            importedCommits,
            skippedCommits,
            importedPullRequests: importedPrs,
            skippedPullRequests: skippedPrs,
            omissionReasons: [
              "일부 PR 메타데이터 응답 시간 초과로 누락됨 (합성 사유)",
              "비정상 인코딩/대용량 diff 커밋 항목 제외됨 (합성 사유)",
            ],
          },
          limitDetail: null,
          error: null,
        },
      };
    }

    case "limit_rate_limited":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "limit_reached",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "GitHub API 요청 한도에 도달하여 가져오기가 중단되었습니다. 전체 성공이 아닙니다.",
          readiness: {
            importReady: false,
            importReadyState: "failed",
            importReadySummary:
              "한도 도달로 스냅샷 수집이 완료되지 않았습니다 (import_ready=false).",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "not_started",
                detail: "원본 스냅샷 미완료로 시작되지 않았습니다.",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "not_started",
                detail: "원본 스냅샷 미완료로 시작되지 않았습니다.",
              },
            ],
          },
          retryPolicy: {
            allowed: true,
            reason:
              "서버가 한도 해제 시점 이후 재시도를 허용했습니다 (로컬 모의에서는 즉시 재시도 테스트 가능).",
            nextAllowedAt: "2026-11-26T12:45:00+09:00",
          },
          summary: null,
          partialDetail: null,
          limitDetail: {
            kind: "rate_limited",
            limitLabel: "GitHub 읽기 수집 Rate Limit 도달",
            capturedCount: 45,
            capCount: params.repo.estimatedCommits,
            resetAt: "2026-11-26T12:45:00+09:00",
            explanation:
              "외부 GitHub 수집 호출 한도에 도달해 중단되었습니다. 성공으로 처리되지 않습니다.",
          },
          error: null,
        },
      };

    case "limit_cap_blocked":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "limit_reached",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "MVP 일회성 최초 가져오기 최대 수집 상한에 도달하여 더 이상 수집할 수 없습니다.",
          readiness: {
            importReady: false,
            importReadyState: "partial",
            importReadySummary:
              "상한선(최대 200 커밋)에 의해 절단 수집됨 (PARTIAL_WITH_LIMIT -> import_ready=false)",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "ready",
                detail: "상한선 내 수집된 범위만 정규화되었습니다.",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "skipped",
                detail: "상한 초과 저장소는 부분 절단 경고 상태로 유지됩니다.",
              },
            ],
          },
          retryPolicy: {
            allowed: false,
            reason:
              "일회성 MVP 수집 상한(200 커밋)에 의해 절단된 결과이므로 서버 정책상 재시도가 허용되지 않습니다.",
            nextAllowedAt: null,
          },
          summary: {
            commitsCaptured: 200,
            pullRequestsCaptured: 30,
            capturedAt: updatedAt,
          },
          partialDetail: null,
          limitDetail: {
            kind: "import_cap_reached",
            limitLabel: "MVP 최초 스냅샷 수집 상한 초과",
            capturedCount: 200,
            capCount: 200,
            resetAt: null,
            explanation:
              "저장소 이력이 초기 가져오기 상한을 초과하여 최근 200건까지만 기록되었습니다. 전체 성공으로 표시하지 않습니다.",
          },
          error: null,
        },
      };

    case "error_retryable":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "failed",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "스냅샷 수집 워커에서 일시적 오류가 발생하여 가져오기에 실패했습니다.",
          readiness: {
            importReady: false,
            importReadyState: "failed",
            importReadySummary: "원본 스냅샷 수집 실패 (import_ready=false)",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "not_started",
                detail: "원본 스냅샷 실패로 실행되지 않았습니다.",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "not_started",
                detail: "원본 스냅샷 실패로 실행되지 않았습니다.",
              },
            ],
          },
          retryPolicy: {
            allowed: true,
            reason:
              "일시적 백엔드/워커 장애로 판단되어 서버가 재시도를 허용했습니다.",
            nextAllowedAt: null,
          },
          summary: null,
          partialDetail: null,
          limitDetail: null,
          error: {
            code: "MOCK_WORKER_TRANSIENT_TIMEOUT",
            title: "일시적 수집 처리 시간 초과",
            message:
              "스냅샷 워커가 저장소 이력을 읽는 중 응답 시간을 초과했습니다.",
            actionGuidance:
              "서버가 재시도를 허용했으므로 아래 '가져오기 재시도' 버튼으로 다시 요청할 수 있습니다.",
          },
        },
      };

    case "error_non_retryable":
      return {
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "failed",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "저장소 메타데이터 검증 오류로 가져오기에 실패했으며 재시도가 허용되지 않습니다.",
          readiness: {
            importReady: false,
            importReadyState: "failed",
            importReadySummary: "원본 스냅샷 수집 실패 (import_ready=false)",
            derivedAllReady: false,
            derivedStages: [
              {
                key: "event_normalization",
                label: "작업 이벤트 정규화",
                status: "skipped",
                detail: "가져오기 실패로 건너뛰었습니다.",
              },
              {
                key: "timeline_indexing",
                label: "타임라인 준비",
                status: "skipped",
                detail: "가져오기 실패로 건너뛰었습니다.",
              },
            ],
          },
          retryPolicy: {
            allowed: false,
            reason:
              "지원되지 않는 저장소 구성(비어 있거나 기본 브랜치 미존재)으로 서버가 재시도를 거부했습니다.",
            nextAllowedAt: null,
          },
          summary: null,
          partialDetail: null,
          limitDetail: null,
          error: {
            code: "MOCK_REPO_UNSUPPORTED_STRUCTURE",
            title: "재시도 불가 저장소 오류",
            message:
              "선택한 저장소에서 초기 스냅샷을 생성할 수 없는 구조 오류가 반환되었습니다.",
            actionGuidance:
              "재시도할 수 없습니다. 다른 공개 저장소를 선택하거나 저장소 기본 브랜치를 확인하세요.",
          },
        },
      };

    case "permission_revoked":
      return {
        githubRevoked: true,
        run: {
          runId: params.runId,
          repositoryId: params.repo.repoId,
          repositoryFullName: params.repo.fullName,
          status: "permission_revoked",
          attemptNumber: params.attemptNumber,
          startedAt: params.startedAt,
          updatedAt,
          progressMessage:
            "가져오기 실행 중 GitHub 수집 권한이 회수되어 작업이 중단되었습니다.",
          readiness: {
            importReady: false,
            importReadyState: "failed",
            importReadySummary:
              "권한 상실로 원본 스냅샷 수집 중단 (import_ready=false)",
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
          summary: null,
          partialDetail: null,
          limitDetail: null,
          error: {
            code: "MOCK_GITHUB_PERMISSION_REVOKED",
            title: "GitHub 수집 권한 상실",
            message:
              "Career Memory 서비스 로그인은 유지되고 있으나, 별도 GitHub 수집 연결 권한이 회수되었습니다.",
            actionGuidance:
              "상단 'GitHub 수집 연결 다시 설정' 버튼으로 권한을 복구한 뒤 새 가져오기를 요청하세요.",
          },
        },
      };
  }
}

export class MockFixtureImportAdapter
  implements ImportServiceAdapter, PrototypeHarnessControls
{
  private latencyMs: number;
  private disableBrowserStorage: boolean;
  private state: PersistedPrototypeSnapshot;
  private failNextPollWithNetworkError = false;
  private seenIdempotencyKeys = new Set<string>();

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
      };
    }
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
        ? buildTerminalRunForScenario({
            scenarioId: "permission_revoked",
            runId: this.state.latestRun.runId,
            repo: findRepo(this.state.latestRun.repositoryId),
            attemptNumber: this.state.latestRun.attemptNumber,
            startedAt: this.state.latestRun.startedAt,
          }).run
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

    if (
      this.state.serviceAuth.status !== "authenticated" ||
      this.state.scenarioId === "session_expired_on_submit"
    ) {
      this.state = {
        ...this.state,
        serviceAuth: {
          ...SYNTHETIC_EXPIRED_AUTH,
          lastCheckedAt: nowIso(),
        },
      };
      this.persistSnapshot();
      return {
        ok: false,
        kind: "session_expired",
        serviceAuth: { ...this.state.serviceAuth },
        fault: {
          kind: "session_expired",
          title: "서비스 세션 만료 — 가져오기 요청 미확정",
          message:
            "최초 가져오기 요청을 처리하는 시점에 Career Memory 서비스 세션이 만료되었습니다. 작업은 시작되지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "GitHub 수집 연결을 다시 끊을 필요는 없습니다. 상단에서 '모의 서비스 다시 로그인'을 완료한 뒤 다시 요청하세요.",
        },
      };
    }

    if (this.state.scenarioId === "network_error_on_submit") {
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "네트워크 통신 실패 — 서버 응답 미수신",
          message:
            "서버로부터 요청 수락 응답을 받지 못했습니다. 최초 가져오기 시작 여부가 확정되지 않았습니다.",
          serverConfirmed: false,
          actionGuidance:
            "중복 생성을 막기 위해 먼저 '서버 상태 다시 확인'으로 기존 작업 유무를 확인한 뒤 필요 시 다시 요청하세요.",
        },
      };
    }

    if (
      this.state.latestRun?.status === "running" ||
      this.seenIdempotencyKeys.has(input.idempotencyKey)
    ) {
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          title: "중복 가져오기 요청 감지",
          message:
            "이미 동일한 저장소에 대한 최초 가져오기 작업이 서버에서 실행 중입니다.",
          serverConfirmed: false,
          actionGuidance:
            "새 요청을 보내지 않고 기존 실행 상태를 조회하세요.",
        },
      };
    }

    this.seenIdempotencyKeys.add(input.idempotencyKey);
    const repo = findRepo(input.repoId);
    const nextSeq = this.state.runSequence + 1;
    const runId = `run_demo_${nextSeq}`;
    const startedAt = nowIso();

    const run = buildRunningRun({
      runId,
      repo,
      attemptNumber: 1,
      startedAt,
      stage: "fetching_snapshot",
    });

    this.state = {
      ...this.state,
      runSequence: nextSeq,
      pollStep: 0,
      isRetryRecoveryFlow: false,
      latestRun: run,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(run),
        githubConnection: { ...this.state.githubConnection },
      },
    };
  }

  public async pollImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.pollCallCount += 1;
    await sleep(this.latencyMs);

    if (this.failNextPollWithNetworkError) {
      this.failNextPollWithNetworkError = false;
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "상태 조회 중 네트워크 실패",
          message:
            "실행 중인 가져오기 상태를 서버에서 가져오지 못했습니다. 직전 확인 상태만 화면에 유지됩니다.",
          serverConfirmed: false,
          actionGuidance:
            "새 가져오기를 다시 누르지 말고 '상태 다시 확인'을 눌러 서버 상태를 동기화하세요.",
        },
      };
    }

    if (this.state.serviceAuth.status !== "authenticated") {
      return {
        ok: false,
        kind: "session_expired",
        serviceAuth: { ...this.state.serviceAuth },
        fault: {
          kind: "session_expired",
          title: "세션 만료로 상태 조회 중단",
          message:
            "서비스 세션이 만료되어 가져오기 진행 상태를 조회할 수 없습니다.",
          serverConfirmed: false,
          actionGuidance:
            "모의 서비스에 다시 로그인하면 마지막 서버 상태를 이어서 조회합니다.",
        },
      };
    }

    const current = this.state.latestRun;
    if (!current || current.runId !== runId) {
      return {
        ok: false,
        kind: "network_error",
        fault: {
          kind: "network_error",
          title: "조회 대상 작업 없음",
          message: `요청한 작업(${runId})을 로컬 모의 상태에서 찾을 수 없습니다.`,
          serverConfirmed: false,
          actionGuidance: "'서버 상태 다시 확인'으로 화면을 동기화하세요.",
        },
      };
    }

    if (current.status !== "running") {
      return {
        ok: true,
        data: {
          run: structuredClone(current),
          githubConnection: { ...this.state.githubConnection },
        },
      };
    }

    const repo = findRepo(current.repositoryId);
    const nextStep = this.state.pollStep + 1;

    // For slow reload recovery scenario, stay running across multiple polls
    if (
      this.state.scenarioId === "running_slow_for_reload" &&
      nextStep < 4
    ) {
      const stage =
        nextStep >= 2 ? "snapshot_ready_deriving" : "fetching_snapshot";
      const updatedRun = buildRunningRun({
        runId: current.runId,
        repo,
        attemptNumber: current.attemptNumber,
        startedAt: current.startedAt,
        stage,
      });
      this.state = {
        ...this.state,
        pollStep: nextStep,
        latestRun: updatedRun,
      };
      this.persistSnapshot();
      return {
        ok: true,
        data: {
          run: structuredClone(updatedRun),
          githubConnection: { ...this.state.githubConnection },
        },
      };
    }

    // For normal full success, explicitly pass through an intermediate state
    // where `import_ready=true` while derived stages are still `in_progress`!
    if (
      (this.state.scenarioId === "success_full" ||
        this.state.isRetryRecoveryFlow) &&
      nextStep === 1
    ) {
      const midRun = buildRunningRun({
        runId: current.runId,
        repo,
        attemptNumber: current.attemptNumber,
        startedAt: current.startedAt,
        stage: "snapshot_ready_deriving",
      });
      this.state = {
        ...this.state,
        pollStep: nextStep,
        latestRun: midRun,
      };
      this.persistSnapshot();
      return {
        ok: true,
        data: {
          run: structuredClone(midRun),
          githubConnection: { ...this.state.githubConnection },
        },
      };
    }

    const terminal = buildTerminalRunForScenario({
      scenarioId: this.state.scenarioId,
      runId: current.runId,
      repo,
      attemptNumber: current.attemptNumber,
      startedAt: current.startedAt,
      forceRecoverySuccess: this.state.isRetryRecoveryFlow,
    });

    const nextGitHub = terminal.githubRevoked
      ? { ...SYNTHETIC_REVOKED_GITHUB }
      : this.state.githubConnection;

    this.state = {
      ...this.state,
      pollStep: nextStep,
      latestRun: terminal.run,
      githubConnection: nextGitHub,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(terminal.run),
        githubConnection: { ...nextGitHub },
      },
    };
  }

  public async retryImportRun(
    runId: string
  ): Promise<AdapterResult<RunSyncPayload>> {
    this.retryImportCallCount += 1;
    await sleep(this.latencyMs);

    if (this.state.serviceAuth.status !== "authenticated") {
      return {
        ok: false,
        kind: "session_expired",
        serviceAuth: { ...this.state.serviceAuth },
        fault: {
          kind: "session_expired",
          title: "세션 만료 — 재시도 요청 미확정",
          message:
            "서비스 세션이 만료되어 재시도 요청을 수락할 수 없습니다.",
          serverConfirmed: false,
          actionGuidance:
            "모의 서비스에 다시 로그인한 뒤 재시도 가능 여부를 확인하세요.",
        },
      };
    }

    const current = this.state.latestRun;
    if (!current || current.runId !== runId || !current.retryPolicy.allowed) {
      return {
        ok: false,
        kind: "duplicate_conflict",
        fault: {
          kind: "duplicate_conflict",
          title: "서버 정책상 재시도 거부",
          message:
            "현재 작업은 서버가 재시도를 허용하지 않은 상태입니다.",
          serverConfirmed: false,
          actionGuidance:
            "화면에 표시된 서버 안내 사유를 확인하고 필요한 복구 절차를 진행하세요.",
        },
      };
    }

    const repo = findRepo(current.repositoryId);
    const nextSeq = this.state.runSequence + 1;
    const retryRunId = `run_demo_${nextSeq}`;
    const startedAt = nowIso();
    const retryRun = buildRunningRun({
      runId: retryRunId,
      repo,
      attemptNumber: current.attemptNumber + 1,
      startedAt,
      stage: "fetching_snapshot",
    });

    this.state = {
      ...this.state,
      runSequence: nextSeq,
      pollStep: 0,
      isRetryRecoveryFlow: true,
      latestRun: retryRun,
    };
    this.persistSnapshot();

    return {
      ok: true,
      data: {
        run: structuredClone(retryRun),
        githubConnection: { ...this.state.githubConnection },
      },
    };
  }

  public async resetActiveRun(): Promise<AdapterResult<ConnectionSyncPayload>> {
    await sleep(Math.min(this.latencyMs, 60));
    this.state = {
      ...this.state,
      latestRun: null,
      pollStep: 0,
      isRetryRecoveryFlow: false,
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
