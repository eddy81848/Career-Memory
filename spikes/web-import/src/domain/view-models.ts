/**
 * UI View Models for the Career Memory one-time initial import journey.
 *
 * IMPORTANT BOUNDARY:
 * These types represent local UI presentation models only — NOT finalized HTTP
 * request/response schemas, NOT shared backend contracts, and NOT cookie/session
 * storage formats. When the approved API contract arrives, only the adapter layer
 * translates contract payloads into these view models.
 */

export type ServiceAuthStatus =
  | "signed_out"
  | "authenticating"
  | "authenticated"
  | "session_expired";

export interface SyntheticServiceAccount {
  syntheticUserId: string;
  displayName: string;
  email: string;
}

export interface ServiceAuthViewModel {
  status: ServiceAuthStatus;
  account: SyntheticServiceAccount | null;
  sessionNote: string;
  lastCheckedAt: string | null;
}

export type GitHubConnectionStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "permission_revoked";

export interface GitHubConnectionViewModel {
  status: GitHubConnectionStatus;
  accountHandle: string | null;
  connectionRef: string | null;
  scopeSummary: string;
  revocationReason: string | null;
  connectedAt: string | null;
}

export interface PublicRepositoryOption {
  repoId: string;
  fullName: string;
  visibility: "public";
  defaultBranch: string;
  primaryLanguage: string;
  description: string;
  lastPushedAt: string;
  estimatedCommits: number;
  estimatedPullRequests: number;
  advisoryNote?: string;
}

export type ImportRunStatus =
  | "running"
  | "succeeded"
  | "partial"
  | "limit_reached"
  | "failed"
  | "permission_revoked";

export type ImportReadyState =
  | "pending"
  | "in_progress"
  | "ready"
  | "partial"
  | "failed";

export type DerivedStageKey = "event_normalization" | "timeline_indexing";

export type DerivedStageStatus =
  | "not_started"
  | "pending"
  | "in_progress"
  | "ready"
  | "skipped"
  | "failed";

export interface DerivedStageViewModel {
  key: DerivedStageKey;
  label: string;
  status: DerivedStageStatus;
  detail: string;
}

export interface ReadinessViewModel {
  /**
   * Explicit separation: `importReady` reflects only whether the raw initial
   * snapshot import succeeded enough for snapshot availability (`import_ready`),
   * independent of downstream derived processing stages.
   */
  importReady: boolean;
  importReadyState: ImportReadyState;
  importReadySummary: string;
  derivedAllReady: boolean;
  derivedStages: DerivedStageViewModel[];
}

export interface RetryPolicyViewModel {
  /**
   * Retry CTA is ONLY exposed when the server explicitly sets `allowed: true`.
   */
  allowed: boolean;
  reason: string;
  nextAllowedAt: string | null;
}

export interface SnapshotSummaryViewModel {
  commitsCaptured: number;
  pullRequestsCaptured: number;
  capturedAt: string;
}

export interface PartialDetailViewModel {
  importedCommits: number;
  skippedCommits: number;
  importedPullRequests: number;
  skippedPullRequests: number;
  omissionReasons: string[];
}

export interface LimitDetailViewModel {
  kind: "rate_limited" | "import_cap_reached";
  limitLabel: string;
  capturedCount: number;
  capCount: number;
  resetAt: string | null;
  explanation: string;
}

export interface ImportErrorViewModel {
  code: string;
  title: string;
  message: string;
  actionGuidance: string;
}

export interface ImportRunViewModel {
  runId: string;
  repositoryId: string;
  repositoryFullName: string;
  status: ImportRunStatus;
  attemptNumber: number;
  startedAt: string;
  updatedAt: string;
  progressMessage: string;
  readiness: ReadinessViewModel;
  retryPolicy: RetryPolicyViewModel;
  summary: SnapshotSummaryViewModel | null;
  partialDetail: PartialDetailViewModel | null;
  limitDetail: LimitDetailViewModel | null;
  error: ImportErrorViewModel | null;
}

export interface UnconfirmedFaultViewModel {
  kind: "session_expired" | "network_error" | "duplicate_conflict";
  title: string;
  message: string;
  serverConfirmed: false;
  actionGuidance: string;
}

export interface RecoveryNoticeViewModel {
  kind: "restored_running" | "restored_terminal" | "checked_clean";
  message: string;
  restoredAt: string;
}

export type PrototypeScenarioId =
  | "success_full"
  | "success_import_ready_derived_pending"
  | "partial_retryable"
  | "partial_non_retryable"
  | "limit_rate_limited"
  | "limit_cap_blocked"
  | "error_retryable"
  | "error_non_retryable"
  | "permission_revoked"
  | "session_expired_on_submit"
  | "network_error_on_submit"
  | "running_slow_for_reload";

export interface PrototypeScenarioMeta {
  id: PrototypeScenarioId;
  label: string;
  shortDescription: string;
}

export type UiInteractionPhase =
  | "bootstrapping"
  | "ready"
  | "submitting_import"
  | "retrying_import"
  | "refreshing_status"
  | "mutating_connection";

export interface ImportJourneyState {
  phase: UiInteractionPhase;
  serviceAuth: ServiceAuthViewModel;
  githubConnection: GitHubConnectionViewModel;
  repositories: PublicRepositoryOption[];
  selectedRepoId: string | null;
  activeRun: ImportRunViewModel | null;
  pendingActionLabel: string | null;
  duplicateGuardNotice: string | null;
  unconfirmedFault: UnconfirmedFaultViewModel | null;
  recoveryNotice: RecoveryNoticeViewModel | null;
  scenarioId: PrototypeScenarioId;
}
