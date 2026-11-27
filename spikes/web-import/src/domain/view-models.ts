/**
 * UI View Models for the Career Memory one-time initial import journey.
 *
 * IMPORTANT BOUNDARY:
 * These types represent local UI presentation models only — NOT shared backend
 * contracts and NOT cookie/session storage formats. The 3 sync-run operations
 * map OpenAPI 0.2.0-draft synthetic HTTP responses into these view models in
 * the adapter layer.
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

export type ContractImportState =
  | "PENDING"
  | "RUNNING"
  | "SUCCESS"
  | "PARTIAL"
  | "PARTIAL_WITH_LIMIT"
  | "FAILED"
  | "CANCELED";

export type ImportRunStatus =
  | "running"
  | "succeeded"
  | "partial"
  | "limit_reached"
  | "failed"
  | "canceled"
  | "permission_revoked";

export interface ImportPlanScopeViewModel {
  backfillFrom: string;
  cutoffAt: string;
  windowLabel: string;
  maxObjects: number;
  storageClass: "PUBLIC_CLOUD";
  rawStorageMode: "NONE";
}

export type ImportReadyState =
  | "pending"
  | "in_progress"
  | "ready"
  | "partial"
  | "failed";

export type ProcessingStatusViewModel =
  | "NOT_STARTED"
  | "RUNNING"
  | "BLOCKED_POLICY"
  | "BLOCKED_SOURCE"
  | "FAILED"
  | "COMPLETE";

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

export interface StreamProgressViewModel {
  type: string;
  status: string;
  lastCommittedPage?: number;
  unresolvedFailures?: number;
}

export interface ReadinessViewModel {
  /**
   * Explicit separation: `importReady` reflects only whether the historical
   * initial import succeeded and committed its single IMPORT_READY intent,
   * independent of downstream processing status or policy blocks.
   */
  importReady: boolean;
  importReadyState: ImportReadyState;
  importReadySummary: string;
  processingStatus?: ProcessingStatusViewModel;
  processingBlockedMessage?: string | null;
  derivedAllReady: boolean;
  derivedStages: DerivedStageViewModel[];
}

export interface RetryPolicyViewModel {
  /**
   * Retry CTA is ONLY exposed when the server explicitly sets `allowed: true`.
   */
  allowed: boolean;
  fromSafeCheckpoint?: boolean;
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
  /** Server-reported optimistic concurrency version attributed strictly to `runId` (OpenAPI 0.5.0-draft). */
  runVersion?: number;
  statusUrl?: string;
  planId?: string;
  repositoryId: string;
  repositoryFullName: string;
  /** Repository visibility/policy verification status (`PUBLIC` or `PRIVATE_OR_RESTRICTED`). */
  repositoryVisibility?: "PUBLIC" | "PRIVATE_OR_RESTRICTED";
  /** Fixed one-time backfill plan window, item cap, and non-storage policy metadata. */
  planScope?: ImportPlanScopeViewModel;
  /** Exact server contract state enum (`PENDING | RUNNING | SUCCESS | PARTIAL | PARTIAL_WITH_LIMIT | FAILED | CANCELED`). */
  contractStatus?: ContractImportState;
  status: ImportRunStatus;
  attemptNumber: number;
  startedAt: string;
  /** Server-reported observation timestamp (`observed_at`), when available. */
  observedAt?: string | null;
  /** Terminal completion/end observation timestamp (null while PENDING or RUNNING). */
  completedAt?: string | null;
  updatedAt: string;
  progressMessage: string;
  /** Structured server failure reason code (`PLAN_DRIFT`, `TRANSIENT_PROVIDER_ERROR`, `INCOMPLETE_PR_ASSOCIATION`, etc.). */
  failureReason?: string | null;
  sourceDrift?: boolean;
  processedRange?: string | null;
  remainingRange?: string | null;
  streams?: StreamProgressViewModel[];
  readiness: ReadinessViewModel;
  retryPolicy: RetryPolicyViewModel;
  summary: SnapshotSummaryViewModel | null;
  partialDetail: PartialDetailViewModel | null;
  limitDetail: LimitDetailViewModel | null;
  error: ImportErrorViewModel | null;
}

/**
 * In-memory summary of a previous retry request whose server acceptance is
 * unconfirmed (e.g. lost 202 response or unclassified 5xx).
 * Kept strictly in memory (never persisted across reloads) so the UI can
 * present an explicit "resend exact previous retry" action distinct from
 * starting a new retry against a newer run version.
 */
export interface PendingUnconfirmedRetryViewModel {
  runId: string;
  expectedRunVersion: number;
}

export interface UnconfirmedFaultViewModel {
  kind:
    | "session_expired"
    | "workspace_scope_violation"
    | "resource_not_found"
    | "auth_verification_unavailable"
    | "version_conflict"
    | "network_error"
    | "uncertain_outcome"
    | "duplicate_conflict"
    | "request_rejected";
  httpStatus?: number;
  errorCode?: string;
  requestId?: string;
  title: string;
  message: string;
  serverConfirmed: false;
  actionGuidance: string;
}

export interface RecoveryNoticeViewModel {
  kind:
    | "restored_running"
    | "restored_terminal"
    | "checked_clean"
    | "attached_existing_run";
  message: string;
  restoredAt: string;
}

export type PrototypeScenarioId =
  | "success_full"
  | "success_import_ready_derived_pending"
  | "success_blocked_policy"
  | "success_blocked_source"
  | "partial_retryable"
  | "partial_non_retryable"
  | "partial_plan_drift"
  | "limit_rate_limited"
  | "limit_cap_blocked"
  | "error_retryable"
  | "error_non_retryable"
  | "canceled_run"
  | "permission_revoked"
  | "session_expired_on_submit"
  | "network_error_on_submit"
  | "conflict_existing_run"
  | "empty_403"
  | "empty_409"
  | "empty_422"
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
  /**
   * In-memory list of repository IDs whose initial import was confirmed as
   * succeeded (`status === "succeeded"` and `importReady === true`) in the
   * current authenticated/connected scope. Never persisted; cleared on
   * logout, session expiration, permission/connection change, or reset.
   */
  succeededRepoIds: string[];
  /**
   * True when the server returned `403 WORKSPACE_SCOPE_VIOLATION`.
   * Closes this Workspace's repositories/runs/polling while keeping the user
   * logged in (`serviceAuth.status === "authenticated"`).
   */
  workspaceAccessRevoked: boolean;
  /**
   * In-memory summary of an unconfirmed previous retry for explicit resend
   * (same runId, same Idempotency-Key, same original expected_run_version).
   */
  pendingRetry: PendingUnconfirmedRetryViewModel | null;
  pendingActionLabel: string | null;
  duplicateGuardNotice: string | null;
  unconfirmedFault: UnconfirmedFaultViewModel | null;
  recoveryNotice: RecoveryNoticeViewModel | null;
  scenarioId: PrototypeScenarioId;
}
