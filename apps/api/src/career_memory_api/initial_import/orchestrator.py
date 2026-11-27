"""Production initial backfill orchestrator integrating T-201, T-202, T-203, and T-206.

Unifies:
- `career_memory_api.github_connect` (policy & credential gate, visibility/auth transitions)
- `career_memory_api.github_provider` (`FrozenImportPlan`, `CompletionGateEvaluator`, `GitHubImportClientPort`)
- `career_memory_api.backfill_engine` (`PaginationSafeguard`, `CommitPrAssociationClassifier`, `PostgresBackfillPageCommitter`)
- `career_memory_api.initial_import.hooks` (`BackfillLifecycleHookPort` for B-004 fault harnesses)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.backfill_engine.association_classifier import (
    CommitPrAssociationClassifier,
)
from career_memory_api.backfill_engine.models import (
    REQUIRED_BACKFILL_STREAMS,
    BackfillFailureReasonCode,
    BackfillLimits,
    BackfillStreamName,
    CommitPrLookupObservation,
    DefaultBranchCommitRecord,
    FinalizationDirectCommitCandidate,
    FinalizationPrCandidate,
    FinalizeOutcomeStatus,
    FinalizeRunRequest,
    FinalizeRunResult,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    PageCommitRequest,
    PageCommitStatus,
    ProviderPageEnvelope,
    PullRequestRecord,
    RepositoryHeadSnapshot,
    SkipReasonCode,
    StreamCheckpointSnapshot,
)
from career_memory_api.backfill_engine.pagination_guard import (
    PaginationSafeguard,
    compute_page_fingerprint,
)
from career_memory_api.backfill_engine.postgres_page_committer import (
    PostgresBackfillPageCommitter,
)
from career_memory_api.github_connect.models import (
    GitHubConnectError,
    PolicyGuardedOperation,
    parse_repository_full_name,
)
from career_memory_api.github_connect.postgres_service import (
    PostgresGitHubConnectService,
)
from career_memory_api.github_provider.import_client import GitHubImportClientPort
from career_memory_api.github_provider.models import (
    GITHUB_CONNECTOR_VERSION,
    OPTIONAL_CONTEXT_STREAMS,
    REQUIRED_IMPORT_STREAMS,
    GitHubErrorCode,
    GitHubProviderError,
    GitHubRepositorySnapshot,
    GitHubStreamKind,
    MergedPullRequestCue,
    ObservedGitHubRevision,
    RepositoryPolicyViolationError,
    format_utc_iso,
    parse_github_iso_timestamp,
    safe_json_dumps,
)
from career_memory_api.github_provider.plan import (
    CompletionGateEvaluator,
    FrozenImportPlan,
    ImportPlanLimits,
    build_frozen_import_plan,
)
from career_memory_api.initial_import.hooks import (
    BackfillLifecycleHookPort,
    FinalizeHookContext,
    NoopBackfillLifecycleHook,
    PageHookContext,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.schema_contracts.enums import (
    PrAssociationState,
    ResourceVisibility,
    SourceObjectType,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunMode, SyncRunStatus
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

_PR_REVIEW_SUBSTREAMS: tuple[str, ...] = ("reviews", "review_comments", "issue_comments")
_POLICY_AUTH_ERROR_CODES: frozenset[GitHubErrorCode] = frozenset(
    {
        GitHubErrorCode.AUTH_REVOKED,
        GitHubErrorCode.FORBIDDEN_POLICY,
        GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE,
        GitHubErrorCode.MISSING_CREDENTIALS,
        GitHubErrorCode.ENDPOINT_BLOCKED,
        GitHubErrorCode.REDIRECT_BLOCKED,
    }
)
_TRANSIENT_ERROR_CODES: frozenset[GitHubErrorCode] = frozenset(
    {
        GitHubErrorCode.RATE_LIMITED,
        GitHubErrorCode.SERVER_ERROR,
        GitHubErrorCode.TIMEOUT,
        GitHubErrorCode.CONNECTION_ERROR,
    }
)


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def _compute_pr_cues_digest(cues: Sequence[MergedPullRequestCue]) -> str:
    canonical = sorted(cue.comparison_tuple() for cue in cues)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _serialize_cue(cue: MergedPullRequestCue) -> dict[str, Any]:
    return {
        "pr_number": cue.pr_number,
        "head_sha": cue.head_sha,
        "merged_at": format_utc_iso(cue.merged_at),
        "updated_at": format_utc_iso(cue.updated_at),
        "merge_commit_sha": cue.merge_commit_sha,
        "state": cue.state,
    }


def _deserialize_cue(raw: dict[str, Any]) -> MergedPullRequestCue:
    merged_at = parse_github_iso_timestamp(raw.get("merged_at"))
    updated_at = parse_github_iso_timestamp(raw.get("updated_at")) or merged_at
    if merged_at is None or updated_at is None:
        raise ValueError("Serialized MergedPullRequestCue missing merged_at/updated_at.")
    return MergedPullRequestCue(
        pr_number=int(raw["pr_number"]),
        head_sha=str(raw["head_sha"]),
        merged_at=merged_at,
        updated_at=updated_at,
        merge_commit_sha=(
            str(raw["merge_commit_sha"]) if raw.get("merge_commit_sha") is not None else None
        ),
        state=str(raw.get("state") or "closed"),
    )


def frozen_import_plan_from_plan_spec(plan_spec: dict[str, Any]) -> FrozenImportPlan:
    """Reconstruct an immutable `FrozenImportPlan` previously persisted in `sync_runs.plan_spec`."""
    backfill_from = parse_github_iso_timestamp(plan_spec.get("backfill_from"))
    cutoff_at = parse_github_iso_timestamp(plan_spec.get("cutoff_at"))
    started_observed_at = parse_github_iso_timestamp(plan_spec.get("started_observed_at"))
    if backfill_from is None or cutoff_at is None or started_observed_at is None:
        raise ValueError("Frozen plan_spec missing required timestamps.")

    limits_raw = plan_spec.get("limits")
    limits = (
        ImportPlanLimits.from_request_limits(limits_raw)
        if isinstance(limits_raw, dict)
        else ImportPlanLimits()
    )
    req_raw = plan_spec.get("required_streams")
    required_streams = (
        tuple(GitHubStreamKind(str(s)) for s in req_raw)
        if isinstance(req_raw, list) and req_raw
        else REQUIRED_IMPORT_STREAMS
    )
    opt_raw = plan_spec.get("optional_streams")
    optional_streams = (
        tuple(GitHubStreamKind(str(s)) for s in opt_raw)
        if isinstance(opt_raw, list)
        else OPTIONAL_CONTEXT_STREAMS
    )

    return FrozenImportPlan(
        plan_id=str(plan_spec["plan_id"]),
        mode=SyncRunMode(str(plan_spec.get("mode", SyncRunMode.INITIAL_BACKFILL.value))),
        source_resource_id=str(plan_spec["source_resource_id"]),
        provider_resource_id=str(plan_spec["provider_resource_id"]),
        repository_full_name=str(plan_spec["repository_full_name"]),
        canonical_url=str(plan_spec["canonical_url"]),
        visibility=ResourceVisibility(str(plan_spec["visibility"])),
        default_branch=str(plan_spec["default_branch"]),
        start_head_sha=str(plan_spec["start_head_sha"]),
        backfill_from=backfill_from,
        cutoff_at=cutoff_at,
        started_observed_at=started_observed_at,
        limits=limits,
        required_streams=required_streams,
        optional_streams=optional_streams,
        connector_version=str(plan_spec.get("connector_version") or GITHUB_CONNECTOR_VERSION),
        hash_policy_version=str(plan_spec.get("hash_policy_version") or "cm-canon-nfc-lf-v1"),
    )


def _revision_to_envelope_item(rev: ObservedGitHubRevision) -> dict[str, Any]:
    item: dict[str, Any] = {
        "object_type": rev.source_object_type.value,
        "provider_object_id": rev.provider_object_id,
        "content_hash": rev.content_hash,
        "normalized_metadata": dict(rev.normalized_metadata),
        "hash_policy_version": rev.hash_policy_version,
        "connector_version": rev.connector_version,
        "schema_version": rev.schema_version,
        "provider_revision_id": rev.provider_revision_id,
        "provider_updated_at": rev.provider_updated_at,
        "observed_at": rev.observed_at,
        "is_tombstone": False,
        "pr_association_state": (
            rev.pr_association_state.value if rev.pr_association_state is not None else None
        ),
    }
    if rev.source_object_type is SourceObjectType.COMMIT:
        item["commit_sha"] = rev.provider_object_id
    return item


def _skip_outcomes_for_revisions(
    stream_name: BackfillStreamName,
    revisions: Sequence[ObservedGitHubRevision],
) -> tuple[ItemOutcomeRecord, ...]:
    outcomes: list[ItemOutcomeRecord] = []
    for rev in revisions:
        if rev.canonicalization_status == "SKIPPED":
            outcomes.append(
                ItemOutcomeRecord(
                    stream_name=stream_name,
                    object_type=rev.source_object_type.value,
                    provider_object_id=rev.provider_object_id,
                    outcome_kind=ItemOutcomeKind.SKIPPED,
                    reason_code=rev.skip_reason_code or SkipReasonCode.OVERSIZED_FILE.value,
                    retryable=False,
                    outcome_metadata={"canonicalization_status": rev.canonicalization_status},
                )
            )
    return tuple(outcomes)


@dataclass(frozen=True, slots=True)
class InitialBackfillExecutionResult:
    """Structured outcome returned by `InitialBackfillOrchestrator.execute_sync_run`."""

    sync_run_id: str
    source_resource_id: str
    plan_id: str
    run_version: int
    status: SyncRunStatus
    processing_status: ProcessingStatus
    import_ready: bool
    retry_allowed: bool
    has_safe_checkpoint: bool
    source_drift: bool
    resumed_from_checkpoint: bool
    failure_reason_code: str | None = None
    processed_range: str | None = None
    remaining_range: str | None = None
    import_ready_outbox_id: str | None = None
    promoted_pr_event_ids: tuple[str, ...] = ()
    promoted_direct_commit_event_ids: tuple[str, ...] = ()
    pages_committed_this_attempt: int = 0
    frozen_plan_fingerprint: str | None = None
    diagnostic_message: str | None = None


@dataclass(frozen=True, slots=True)
class _LoadedRunContext:
    sync_run_id: str
    workspace_id: str
    source_resource_id: str
    plan_id: str
    run_version: int
    status: SyncRunStatus
    processing_status: ProcessingStatus
    import_ready: bool
    retry_allowed: bool
    has_safe_checkpoint: bool
    source_drift: bool
    is_abandoned: bool
    plan_spec: dict[str, Any]
    failure_reason_code: str | None
    processed_range: str | None
    remaining_range: str | None
    provider_resource_id: str
    resource_name: str
    resource_canonical_url: str
    resource_visibility: str
    resource_processing_mode: str
    resource_raw_storage_mode: str
    resource_status: str
    source_account_id: str
    account_status: str
    token_secret_ref: str
    deletion_fence_active: bool


class InitialBackfillOrchestrator:
    """Product M2 single-repository initial backfill orchestrator with T-206 checkpoint resume.

    Enforces:
    - Actor membership / resource policy / active credential / deletion fence / live PUBLIC
      visibility checks at start and before completion.
    - Immutable `FrozenImportPlan` persisted in `sync_runs.plan_spec` on first run and loaded
      verbatim on every `retry` / resume (never re-anchoring HEAD or time window).
    - Atomic page commits (`checkpoint <= committed page`) with zero duplicate re-fetching of
      already-committed pages on resume.
    - Strict non-promotion (`work_events = 0`, `IMPORT_READY = 0`) on `PARTIAL`,
      `PARTIAL_WITH_LIMIT`, `PLAN_DRIFT`, `CURSOR_CYCLE_DETECTED`, `MALFORMED_OR_PARTIAL_PAGE`,
      `INCOMPLETE_PR_ASSOCIATION`, policy/visibility transitions, and deletion fences.
    - Single-transaction `SUCCESS` + single `IMPORT_READY` outbox + PR/1:1 `DIRECT_COMMIT`
      `work_events` promotion via `PostgresBackfillPageCommitter.finalize_import_run`.
    - Production `BackfillLifecycleHookPort` (default no-op) around page commits and finalization.
    """

    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        github_client: GitHubImportClientPort,
        page_committer: PostgresBackfillPageCommitter | None = None,
        github_connect_service: PostgresGitHubConnectService | None = None,
        lifecycle_hook: BackfillLifecycleHookPort | None = None,
        completion_gate: CompletionGateEvaluator | None = None,
        association_classifier: CommitPrAssociationClassifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._worker_connection_factory = worker_connection_factory
        self._github_client = github_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._page_committer = page_committer or PostgresBackfillPageCommitter(
            worker_connection_factory=worker_connection_factory,
            clock=self._clock,
        )
        self._github_connect_service = github_connect_service
        self._lifecycle_hook: BackfillLifecycleHookPort = (
            lifecycle_hook if lifecycle_hook is not None else NoopBackfillLifecycleHook()
        )
        self._completion_gate = completion_gate or CompletionGateEvaluator()
        self._association_classifier = association_classifier or CommitPrAssociationClassifier()

    @property
    def page_committer(self) -> PostgresBackfillPageCommitter:
        return self._page_committer

    @property
    def lifecycle_hook(self) -> BackfillLifecycleHookPort:
        return self._lifecycle_hook

    def execute_sync_run(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        access: WorkspaceAccessContext | None = None,
        attempt: int = 1,
        on_page_committed: Callable[[PageHookContext], None] | None = None,
        now: datetime | None = None,
    ) -> InitialBackfillExecutionResult:
        """Execute or safely resume an initial backfill SyncRun from its last committed checkpoint."""
        ts = now or self._clock()
        ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")

        run_ctx = self._load_run_context(ws_uuid, run_uuid)
        if run_ctx is None:
            raise ValueError(f"SyncRun {run_uuid} not found in workspace {ws_uuid}.")

        # Idempotent short-circuit if already SUCCESS
        if run_ctx.status is SyncRunStatus.SUCCESS:
            outbox_id, pr_ids, dc_ids = self._load_existing_success_promotions(ws_uuid, run_uuid)
            plan_fp = (
                frozen_import_plan_from_plan_spec(run_ctx.plan_spec).plan_fingerprint()
                if "start_head_sha" in run_ctx.plan_spec
                else None
            )
            return InitialBackfillExecutionResult(
                sync_run_id=run_uuid,
                source_resource_id=run_ctx.source_resource_id,
                plan_id=run_ctx.plan_id,
                run_version=run_ctx.run_version,
                status=SyncRunStatus.SUCCESS,
                processing_status=run_ctx.processing_status,
                import_ready=True,
                retry_allowed=False,
                has_safe_checkpoint=run_ctx.has_safe_checkpoint,
                source_drift=False,
                resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                processed_range=run_ctx.processed_range,
                import_ready_outbox_id=outbox_id,
                promoted_pr_event_ids=pr_ids,
                promoted_direct_commit_event_ids=dc_ids,
                frozen_plan_fingerprint=plan_fp,
                diagnostic_message="SyncRun already reached SUCCESS.",
            )

        # Reject non-active runs (e.g. PARTIAL awaiting explicit retry, FAILED, CANCELED)
        if run_ctx.is_abandoned or run_ctx.status not in (
            SyncRunStatus.PENDING,
            SyncRunStatus.RUNNING,
        ):
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                pages_committed=0,
                diagnostic_message=f"SyncRun is not executable in status {run_ctx.status.value}.",
            )

        # 1. Pre-flight deletion fence check
        if run_ctx.deletion_fence_active or run_ctx.resource_status == "DELETED":
            self._cancel_run_for_deletion_fence(ws_uuid, run_uuid, ts)
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                pages_committed=0,
                diagnostic_message="Repository deletion fence active before backfill start.",
            )

        # 2. Pre-flight policy & credential gate (github_connect + DB defense-in-depth)
        if self._github_connect_service is not None and access is not None:
            try:
                self._github_connect_service.assert_resource_processing_allowed(
                    access,
                    source_resource_id=run_ctx.source_resource_id,
                    operation=PolicyGuardedOperation.INGESTION,
                )
                self._github_connect_service.resolve_active_credential_for_resource(
                    access,
                    source_resource_id=run_ctx.source_resource_id,
                    operation=PolicyGuardedOperation.INGESTION,
                )
            except WorkspaceResourceNotFoundError:
                self._cancel_run_for_deletion_fence(ws_uuid, run_uuid, ts)
                return self._build_result_from_db(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                    pages_committed=0,
                    diagnostic_message="Source resource unavailable or fenced during pre-flight.",
                )
            except GitHubConnectError as exc:
                self._transition_run_non_retryable_failure(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    failure_reason_code=exc.code,
                    ts=ts,
                )
                return self._build_result_from_db(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                    pages_committed=0,
                    diagnostic_message=exc.message,
                )

        if (
            run_ctx.account_status != "ACTIVE"
            or not run_ctx.token_secret_ref
            or run_ctx.resource_status != "ACTIVE"
            or run_ctx.resource_visibility != ResourceVisibility.PUBLIC.value
            or run_ctx.resource_processing_mode != "PUBLIC_CLOUD"
            or run_ctx.resource_raw_storage_mode != "NONE"
        ):
            reason = (
                "REAUTH_REQUIRED"
                if run_ctx.account_status != "ACTIVE" or not run_ctx.token_secret_ref
                else "POLICY_BLOCKED"
            )
            self._transition_run_non_retryable_failure(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                failure_reason_code=reason,
                ts=ts,
            )
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                resumed_from_checkpoint=run_ctx.has_safe_checkpoint,
                pages_committed=0,
                diagnostic_message=f"Pre-flight policy check failed ({reason}).",
            )

        owner, repo_name = parse_repository_full_name(run_ctx.resource_name)
        token_secret_ref = run_ctx.token_secret_ref
        resumed = "start_head_sha" in run_ctx.plan_spec or run_ctx.has_safe_checkpoint

        # 3. Live repository snapshot check (visibility PUBLIC + identity + start anchor)
        try:
            live_start_snapshot = self._github_client.fetch_repository_snapshot(
                owner=owner,
                repo=repo_name,
                token_secret_ref=token_secret_ref,
                provider_resource_id=run_ctx.provider_resource_id,
            )
        except GitHubProviderError as exc:
            return self._handle_provider_error(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                stream_name=BackfillStreamName.PRS,
                exc=exc,
                access=access,
                resumed=resumed,
                pages_committed=0,
                safeguard=None,
                current_page=1,
                current_cursor=None,
                ts=ts,
            )

        if live_start_snapshot.provider_resource_id != run_ctx.provider_resource_id:
            self._transition_run_non_retryable_failure(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                failure_reason_code="REPOSITORY_IDENTITY_MISMATCH",
                ts=ts,
            )
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                resumed_from_checkpoint=resumed,
                pages_committed=0,
                diagnostic_message="Provider repository ID mismatch at backfill start.",
            )

        if live_start_snapshot.visibility is not ResourceVisibility.PUBLIC:
            return self._handle_policy_or_auth_block(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                access=access,
                observed_visibility=live_start_snapshot.visibility.value,
                failure_reason_code="REPOSITORY_VISIBILITY_CHANGED",
                resumed=resumed,
                pages_committed=0,
                ts=ts,
            )

        # 4. Freeze plan on first run OR load verbatim frozen plan on resume (T-206 invariant!)
        if "start_head_sha" in run_ctx.plan_spec:
            plan = frozen_import_plan_from_plan_spec(run_ctx.plan_spec)
            self._mark_run_running(ws_uuid, run_uuid, plan_spec=None, ts=ts)
        else:
            try:
                plan = build_frozen_import_plan(
                    source_resource_id=run_ctx.source_resource_id,
                    repo_snapshot=live_start_snapshot,
                    cutoff_at=ts,
                    backfill_from=run_ctx.plan_spec.get("backfill_from"),
                    limits=run_ctx.plan_spec.get("limits"),
                    plan_id=run_ctx.plan_id,
                )
            except (RepositoryPolicyViolationError, ValueError) as exc:
                self._transition_run_non_retryable_failure(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    failure_reason_code="INVALID_IMPORT_PLAN",
                    ts=ts,
                )
                return self._build_result_from_db(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resumed_from_checkpoint=resumed,
                    pages_committed=0,
                    diagnostic_message=str(exc),
                )
            self._mark_run_running(ws_uuid, run_uuid, plan_spec=plan.to_plan_spec_dict(), ts=ts)

        plan_fp = plan.plan_fingerprint()

        # 5. Restore PaginationSafeguard state across all previously committed pages/checkpoints
        safeguard = self._build_safeguard_from_db(ws_uuid, run_uuid, run_ctx.source_resource_id, plan)
        pages_committed = 0

        def _commit_and_hook(
            *,
            stream_name: BackfillStreamName,
            page_number: int,
            expected_version: int,
            expected_cursor: str | None,
            next_cursor: str | None,
            page_fingerprint: str,
            items: tuple[Any, ...],
            item_outcomes: tuple[ItemOutcomeRecord, ...],
            commit_associations: tuple[Any, ...],
            stream_completed: bool,
            cursor_metadata_extra: dict[str, Any] | None,
        ) -> tuple[PageCommitStatus, int, str | None, str | None]:
            nonlocal pages_committed
            meta_extra: dict[str, Any] = dict(cursor_metadata_extra or {})
            meta_extra["safeguard_requests_total"] = safeguard.requests_total
            meta_extra["safeguard_bytes_total"] = safeguard.bytes_total
            assert_no_forbidden_raw_keys(meta_extra, field_name="cursor_metadata_extra")

            before_ctx = PageHookContext(
                workspace_id=ws_uuid,
                sync_run_id=run_uuid,
                source_resource_id=run_ctx.source_resource_id,
                run_version=run_ctx.run_version,
                stream_name=stream_name,
                page_number=page_number,
                request_cursor=expected_cursor,
                next_cursor=next_cursor,
                checkpoint_version=expected_version,
                attempt=attempt,
                item_count=len(items) if items else len(commit_associations),
                page_fingerprint=page_fingerprint,
                commit_result=None,
            )
            self._lifecycle_hook.before_page_commit(before_ctx)

            commit_res = self._page_committer.commit_page_batch(
                PageCommitRequest(
                    workspace_id=ws_uuid,
                    sync_run_id=run_uuid,
                    source_resource_id=run_ctx.source_resource_id,
                    stream_name=stream_name,
                    page_number=page_number,
                    expected_checkpoint_version=expected_version,
                    expected_cursor=expected_cursor,
                    next_cursor=next_cursor,
                    page_fingerprint=page_fingerprint,
                    items=items,
                    item_outcomes=item_outcomes,
                    commit_associations=commit_associations,
                    stream_completed=stream_completed,
                    unresolved_failures=0,
                    cursor_metadata_extra=meta_extra,
                ),
                now=self._clock(),
            )
            if commit_res.status is PageCommitStatus.COMMITTED:
                pages_committed += 1

            after_ctx = PageHookContext(
                workspace_id=ws_uuid,
                sync_run_id=run_uuid,
                source_resource_id=run_ctx.source_resource_id,
                run_version=run_ctx.run_version,
                stream_name=stream_name,
                page_number=page_number,
                request_cursor=expected_cursor,
                next_cursor=next_cursor,
                checkpoint_version=commit_res.checkpoint_version,
                attempt=attempt,
                item_count=len(items) if items else len(commit_associations),
                page_fingerprint=page_fingerprint,
                commit_result=commit_res,
            )
            self._lifecycle_hook.after_page_commit(after_ctx)
            if on_page_committed is not None and commit_res.status is PageCommitStatus.COMMITTED:
                on_page_committed(after_ctx)

            return (
                commit_res.status,
                commit_res.checkpoint_version,
                commit_res.current_cursor,
                commit_res.reason,
            )

        # =====================================================================
        # STREAM 1: PRS (Merged Pull Requests targeting plan.default_branch)
        # =====================================================================
        prs_cp = self._page_committer.get_stream_checkpoint(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            stream_name=BackfillStreamName.PRS,
        )
        cues_by_pr: dict[int, MergedPullRequestCue] = {}
        referenced_issues_set: set[int] = set()
        if prs_cp is not None:
            for raw_cue in prs_cp.cursor_metadata.get("merged_pr_cues", []):
                cue_obj = _deserialize_cue(raw_cue)
                cues_by_pr[cue_obj.pr_number] = cue_obj
            for issue_num in prs_cp.cursor_metadata.get("referenced_issue_numbers", []):
                referenced_issues_set.add(int(issue_num))

        if not (
            prs_cp is not None
            and prs_cp.stream_status is SyncRunStatus.SUCCESS
            and prs_cp.unresolved_failures == 0
        ):
            page = (
                prs_cp.last_committed_page + 1
                if prs_cp is not None and prs_cp.last_committed_page is not None
                else 1
            )
            expected_version = prs_cp.checkpoint_version if prs_cp is not None else 0
            expected_cursor = prs_cp.current_cursor if prs_cp is not None else None

            while True:
                try:
                    pr_page = self._github_client.fetch_merged_pull_requests_page(
                        plan,
                        page=page,
                        token_secret_ref=token_secret_ref,
                    )
                except GitHubProviderError as exc:
                    return self._handle_provider_error(
                        ws_uuid=ws_uuid,
                        run_ctx=run_ctx,
                        stream_name=BackfillStreamName.PRS,
                        exc=exc,
                        access=access,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        safeguard=safeguard,
                        current_page=page,
                        current_cursor=expected_cursor,
                        ts=self._clock(),
                        plan_fp=plan_fp,
                    )

                next_cursor = (
                    (pr_page.next_url or f"page:{pr_page.next_page}")
                    if pr_page.has_next
                    else None
                )
                envelope = ProviderPageEnvelope(
                    stream_name=BackfillStreamName.PRS,
                    page_number=page,
                    http_status=200,
                    request_cursor=expected_cursor,
                    next_cursor=next_cursor,
                    items=tuple(_revision_to_envelope_item(r) for r in pr_page.items),
                    has_next_page=pr_page.has_next,
                    payload_bytes=pr_page.bytes_read,
                    skipped_items=_skip_outcomes_for_revisions(
                        BackfillStreamName.PRS, pr_page.items
                    ),
                )
                decision = safeguard.evaluate_page(envelope)
                if not decision.accepted:
                    return self._handle_safeguard_rejection(
                        ws_uuid=ws_uuid,
                        run_ctx=run_ctx,
                        stream_name=BackfillStreamName.PRS,
                        decision=decision,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )

                for cue in pr_page.cues:
                    cues_by_pr[cue.pr_number] = cue
                for rev in pr_page.items:
                    for issue_num in rev.referenced_issue_numbers:
                        referenced_issues_set.add(issue_num)

                extra_meta = {
                    "merged_pr_cues": [
                        _serialize_cue(cues_by_pr[n]) for n in sorted(cues_by_pr.keys())
                    ],
                    "referenced_issue_numbers": sorted(referenced_issues_set),
                }
                status, new_ver, new_cur, reason = _commit_and_hook(
                    stream_name=BackfillStreamName.PRS,
                    page_number=page,
                    expected_version=expected_version,
                    expected_cursor=expected_cursor,
                    next_cursor=next_cursor,
                    page_fingerprint=decision.page_fingerprint,
                    items=decision.deduplicated_items,
                    item_outcomes=decision.item_outcomes,
                    commit_associations=(),
                    stream_completed=not pr_page.has_next,
                    cursor_metadata_extra=extra_meta,
                )
                if status is not PageCommitStatus.COMMITTED:
                    return self._handle_commit_block(
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        status=status,
                        reason=reason,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )

                expected_version = new_ver
                expected_cursor = new_cur
                if not pr_page.has_next:
                    break
                page = pr_page.next_page if pr_page.next_page is not None else page + 1

        sorted_pr_numbers = sorted(cues_by_pr.keys())
        first_pass_cues = tuple(cues_by_pr[n] for n in sorted_pr_numbers)

        # =====================================================================
        # STREAM 2: PR_REVIEWS (PR reviews + review comments + issue comments)
        # =====================================================================
        reviews_cp = self._page_committer.get_stream_checkpoint(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            stream_name=BackfillStreamName.PR_REVIEWS,
        )
        if not (
            reviews_cp is not None
            and reviews_cp.stream_status is SyncRunStatus.SUCCESS
            and reviews_cp.unresolved_failures == 0
        ):
            page_num = (
                reviews_cp.last_committed_page + 1
                if reviews_cp is not None and reviews_cp.last_committed_page is not None
                else 1
            )
            expected_version = reviews_cp.checkpoint_version if reviews_cp is not None else 0
            expected_cursor = reviews_cp.current_cursor if reviews_cp is not None else None

            if not sorted_pr_numbers:
                empty_fp = compute_page_fingerprint(BackfillStreamName.PR_REVIEWS, (), None)
                status, _, _, reason = _commit_and_hook(
                    stream_name=BackfillStreamName.PR_REVIEWS,
                    page_number=page_num,
                    expected_version=expected_version,
                    expected_cursor=expected_cursor,
                    next_cursor=None,
                    page_fingerprint=empty_fp,
                    items=(),
                    item_outcomes=(),
                    commit_associations=(),
                    stream_completed=True,
                    cursor_metadata_extra={"pr_review_next": None},
                )
                if status is not PageCommitStatus.COMMITTED:
                    return self._handle_commit_block(
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        status=status,
                        reason=reason,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )
            else:
                next_state = (
                    reviews_cp.cursor_metadata.get("pr_review_next")
                    if reviews_cp is not None
                    else None
                )
                if isinstance(next_state, dict):
                    pr_idx = int(next_state["pr_idx"])
                    kind_idx = int(next_state["kind_idx"])
                    sub_page = int(next_state["sub_page"])
                else:
                    pr_idx, kind_idx, sub_page = 0, 0, 1

                while pr_idx < len(sorted_pr_numbers):
                    pr_num = sorted_pr_numbers[pr_idx]
                    kind = _PR_REVIEW_SUBSTREAMS[kind_idx]
                    try:
                        if kind == "reviews":
                            rev_page = self._github_client.fetch_pull_request_reviews_page(
                                plan,
                                pr_number=pr_num,
                                page=sub_page,
                                token_secret_ref=token_secret_ref,
                            )
                        elif kind == "review_comments":
                            rev_page = self._github_client.fetch_pull_request_review_comments_page(
                                plan,
                                pr_number=pr_num,
                                page=sub_page,
                                token_secret_ref=token_secret_ref,
                            )
                        else:
                            rev_page = self._github_client.fetch_pull_request_issue_comments_page(
                                plan,
                                pr_number=pr_num,
                                page=sub_page,
                                token_secret_ref=token_secret_ref,
                            )
                    except GitHubProviderError as exc:
                        return self._handle_provider_error(
                            ws_uuid=ws_uuid,
                            run_ctx=run_ctx,
                            stream_name=BackfillStreamName.PR_REVIEWS,
                            exc=exc,
                            access=access,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            safeguard=safeguard,
                            current_page=page_num,
                            current_cursor=expected_cursor,
                            ts=self._clock(),
                            plan_fp=plan_fp,
                        )

                    if rev_page.has_next:
                        next_pr_idx, next_kind_idx = pr_idx, kind_idx
                        next_sub_page = (
                            rev_page.next_page if rev_page.next_page is not None else sub_page + 1
                        )
                        has_more = True
                        next_cursor = (
                            rev_page.next_url
                            or f"pr:{pr_num}:kind:{kind}:page:{next_sub_page}"
                        )
                    elif kind_idx + 1 < len(_PR_REVIEW_SUBSTREAMS):
                        next_pr_idx, next_kind_idx, next_sub_page = pr_idx, kind_idx + 1, 1
                        has_more = True
                        next_cursor = (
                            f"pr:{sorted_pr_numbers[next_pr_idx]}"
                            f":kind:{_PR_REVIEW_SUBSTREAMS[next_kind_idx]}:page:1"
                        )
                    elif pr_idx + 1 < len(sorted_pr_numbers):
                        next_pr_idx, next_kind_idx, next_sub_page = pr_idx + 1, 0, 1
                        has_more = True
                        next_cursor = (
                            f"pr:{sorted_pr_numbers[next_pr_idx]}"
                            f":kind:{_PR_REVIEW_SUBSTREAMS[0]}:page:1"
                        )
                    else:
                        next_pr_idx, next_kind_idx, next_sub_page = len(sorted_pr_numbers), 0, 1
                        has_more = False
                        next_cursor = None

                    envelope = ProviderPageEnvelope(
                        stream_name=BackfillStreamName.PR_REVIEWS,
                        page_number=page_num,
                        http_status=200,
                        request_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        items=tuple(_revision_to_envelope_item(r) for r in rev_page.items),
                        has_next_page=has_more,
                        payload_bytes=rev_page.bytes_read,
                        skipped_items=_skip_outcomes_for_revisions(
                            BackfillStreamName.PR_REVIEWS, rev_page.items
                        ),
                    )
                    decision = safeguard.evaluate_page(envelope)
                    if not decision.accepted:
                        return self._handle_safeguard_rejection(
                            ws_uuid=ws_uuid,
                            run_ctx=run_ctx,
                            stream_name=BackfillStreamName.PR_REVIEWS,
                            decision=decision,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )

                    pr_review_next = (
                        {
                            "pr_idx": next_pr_idx,
                            "kind_idx": next_kind_idx,
                            "sub_page": next_sub_page,
                        }
                        if has_more
                        else None
                    )
                    status, new_ver, new_cur, reason = _commit_and_hook(
                        stream_name=BackfillStreamName.PR_REVIEWS,
                        page_number=page_num,
                        expected_version=expected_version,
                        expected_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        page_fingerprint=decision.page_fingerprint,
                        items=decision.deduplicated_items,
                        item_outcomes=decision.item_outcomes,
                        commit_associations=(),
                        stream_completed=not has_more,
                        cursor_metadata_extra={"pr_review_next": pr_review_next},
                    )
                    if status is not PageCommitStatus.COMMITTED:
                        return self._handle_commit_block(
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            status=status,
                            reason=reason,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )

                    expected_version = new_ver
                    expected_cursor = new_cur
                    page_num += 1
                    pr_idx, kind_idx, sub_page = next_pr_idx, next_kind_idx, next_sub_page
                    if not has_more:
                        break

        # =====================================================================
        # STREAM 3: DEFAULT_BRANCH_COMMITS (anchored strictly to plan.start_head_sha)
        # =====================================================================
        commits_cp = self._page_committer.get_stream_checkpoint(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
        )
        observed_commit_shas: list[str] = (
            list(commits_cp.cursor_metadata.get("observed_commit_shas", []))
            if commits_cp is not None
            else []
        )
        seen_commit_shas: set[str] = set(observed_commit_shas)

        if not (
            commits_cp is not None
            and commits_cp.stream_status is SyncRunStatus.SUCCESS
            and commits_cp.unresolved_failures == 0
        ):
            page = (
                commits_cp.last_committed_page + 1
                if commits_cp is not None and commits_cp.last_committed_page is not None
                else 1
            )
            expected_version = commits_cp.checkpoint_version if commits_cp is not None else 0
            expected_cursor = commits_cp.current_cursor if commits_cp is not None else None

            while True:
                try:
                    commit_page = self._github_client.fetch_default_branch_commits_page(
                        plan,
                        page=page,
                        token_secret_ref=token_secret_ref,
                    )
                except GitHubProviderError as exc:
                    return self._handle_provider_error(
                        ws_uuid=ws_uuid,
                        run_ctx=run_ctx,
                        stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
                        exc=exc,
                        access=access,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        safeguard=safeguard,
                        current_page=page,
                        current_cursor=expected_cursor,
                        ts=self._clock(),
                        plan_fp=plan_fp,
                    )

                next_cursor = (
                    (commit_page.next_url or f"page:{commit_page.next_page}")
                    if commit_page.has_next
                    else None
                )
                envelope = ProviderPageEnvelope(
                    stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
                    page_number=page,
                    http_status=200,
                    request_cursor=expected_cursor,
                    next_cursor=next_cursor,
                    items=tuple(_revision_to_envelope_item(r) for r in commit_page.items),
                    has_next_page=commit_page.has_next,
                    payload_bytes=commit_page.bytes_read,
                    skipped_items=_skip_outcomes_for_revisions(
                        BackfillStreamName.DEFAULT_BRANCH_COMMITS, commit_page.items
                    ),
                )
                decision = safeguard.evaluate_page(envelope)
                if not decision.accepted:
                    return self._handle_safeguard_rejection(
                        ws_uuid=ws_uuid,
                        run_ctx=run_ctx,
                        stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
                        decision=decision,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )

                for rev in commit_page.items:
                    sha = rev.provider_object_id.strip().lower()
                    if sha not in seen_commit_shas:
                        seen_commit_shas.add(sha)
                        observed_commit_shas.append(sha)

                status, new_ver, new_cur, reason = _commit_and_hook(
                    stream_name=BackfillStreamName.DEFAULT_BRANCH_COMMITS,
                    page_number=page,
                    expected_version=expected_version,
                    expected_cursor=expected_cursor,
                    next_cursor=next_cursor,
                    page_fingerprint=decision.page_fingerprint,
                    items=decision.deduplicated_items,
                    item_outcomes=decision.item_outcomes,
                    commit_associations=(),
                    stream_completed=not commit_page.has_next,
                    cursor_metadata_extra={"observed_commit_shas": observed_commit_shas},
                )
                if status is not PageCommitStatus.COMMITTED:
                    return self._handle_commit_block(
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        status=status,
                        reason=reason,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )

                expected_version = new_ver
                expected_cursor = new_cur
                if not commit_page.has_next:
                    break
                page = commit_page.next_page if commit_page.next_page is not None else page + 1

        # =====================================================================
        # STREAM 4: COMMIT_PR_ASSOCIATIONS (3-state MATCHED_PR / NO_MATCH_OBSERVED / UNKNOWN)
        # =====================================================================
        assoc_cp = self._page_committer.get_stream_checkpoint(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
        )
        if not (
            assoc_cp is not None
            and assoc_cp.stream_status is SyncRunStatus.SUCCESS
            and assoc_cp.unresolved_failures == 0
        ):
            pr_records = self._load_pr_records_for_classification(
                ws_uuid=ws_uuid,
                source_resource_id=run_ctx.source_resource_id,
                plan=plan,
                pr_numbers=sorted_pr_numbers,
            )
            in_window_pr_id_set = {pr.provider_object_id for pr in pr_records if pr.in_window}
            known_pr_shas: set[str] = set()
            for pr_rec in pr_records:
                for sha_opt in (
                    pr_rec.head_sha,
                    pr_rec.merge_commit_sha,
                    pr_rec.squash_commit_sha,
                    *pr_rec.commit_shas,
                    *pr_rec.rebase_commit_shas,
                ):
                    if sha_opt:
                        known_pr_shas.add(sha_opt.strip().lower())

            page_num = (
                assoc_cp.last_committed_page + 1
                if assoc_cp is not None and assoc_cp.last_committed_page is not None
                else 1
            )
            expected_version = assoc_cp.checkpoint_version if assoc_cp is not None else 0
            expected_cursor = assoc_cp.current_cursor if assoc_cp is not None else None
            batch_size = max(1, plan.limits.per_page)

            if not observed_commit_shas:
                empty_fp = compute_page_fingerprint(
                    BackfillStreamName.COMMIT_PR_ASSOCIATIONS, (), None
                )
                status, _, _, reason = _commit_and_hook(
                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                    page_number=page_num,
                    expected_version=expected_version,
                    expected_cursor=expected_cursor,
                    next_cursor=None,
                    page_fingerprint=empty_fp,
                    items=(),
                    item_outcomes=(),
                    commit_associations=(),
                    stream_completed=True,
                    cursor_metadata_extra=None,
                )
                if status is not PageCommitStatus.COMMITTED:
                    return self._handle_commit_block(
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        status=status,
                        reason=reason,
                        resumed=resumed,
                        pages_committed=pages_committed,
                        plan_fp=plan_fp,
                    )
            else:
                # Partition observed_commit_shas deterministically into pages of batch_size
                total_batches = (len(observed_commit_shas) + batch_size - 1) // batch_size
                start_batch_idx = page_num - 1
                start_head_snap = RepositoryHeadSnapshot(
                    default_branch=plan.default_branch,
                    head_sha=plan.start_head_sha,
                    start_head_reachable=True,
                    pr_enumeration_digest=_compute_pr_cues_digest(first_pass_cues),
                    visibility=plan.visibility.value,
                )

                for batch_idx in range(start_batch_idx, total_batches):
                    batch_shas = observed_commit_shas[
                        batch_idx * batch_size : (batch_idx + 1) * batch_size
                    ]
                    commit_records = [
                        DefaultBranchCommitRecord(
                            provider_object_id=sha,
                            commit_sha=sha,
                            title=f"Commit {sha[:12]}",
                            reachable_at_start=True,
                            reachable_at_end=True,
                        )
                        for sha in batch_shas
                    ]
                    lookups: list[CommitPrLookupObservation] = []
                    batch_requests = 0
                    batch_bytes = 0

                    for sha in batch_shas:
                        if sha in known_pr_shas:
                            continue
                        if safeguard.requests_total + batch_requests + 1 > plan.limits.max_requests:
                            return self._record_limit_exceeded(
                                ws_uuid=ws_uuid,
                                run_ctx=run_ctx,
                                stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                processed_range=(
                                    f"stream=COMMIT_PR_ASSOCIATIONS,committed_pages=0..{page_num - 1},"
                                    f"unique_objects={safeguard.unique_objects_total},"
                                    f"requests={safeguard.requests_total + batch_requests}"
                                ),
                                remaining_range=(
                                    f"stream=COMMIT_PR_ASSOCIATIONS,from_page={page_num},"
                                    f"reason=max_requests({plan.limits.max_requests})"
                                ),
                                resumed=resumed,
                                pages_committed=pages_committed,
                                plan_fp=plan_fp,
                            )
                        try:
                            assoc_res = self._github_client.fetch_commit_pull_request_associations(
                                plan,
                                commit_sha=sha,
                                token_secret_ref=token_secret_ref,
                            )
                            batch_requests += assoc_res.requests_used
                            batch_bytes += assoc_res.bytes_read
                            if safeguard.bytes_total + batch_bytes > plan.limits.max_total_bytes:
                                return self._record_limit_exceeded(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                    processed_range=(
                                        f"stream=COMMIT_PR_ASSOCIATIONS,committed_pages=0..{page_num - 1},"
                                        f"bytes={safeguard.bytes_total}"
                                    ),
                                    remaining_range=(
                                        f"stream=COMMIT_PR_ASSOCIATIONS,from_page={page_num},"
                                        f"reason=max_bytes({plan.limits.max_total_bytes})"
                                    ),
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    plan_fp=plan_fp,
                                )
                            assoc_pr_ids = tuple(str(n) for n in assoc_res.associated_pr_numbers)
                            lookups.append(
                                CommitPrLookupObservation(
                                    commit_sha=sha,
                                    lookup_succeeded=True,
                                    associated_pr_ids=assoc_pr_ids,
                                    associated_pr_in_window={
                                        pid: (pid in in_window_pr_id_set) for pid in assoc_pr_ids
                                    },
                                )
                            )
                        except GitHubProviderError as exc:
                            if exc.code in _TRANSIENT_ERROR_CODES or exc.retryable:
                                return self._handle_provider_error(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                    exc=exc,
                                    access=access,
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    safeguard=safeguard,
                                    current_page=page_num,
                                    current_cursor=expected_cursor,
                                    ts=self._clock(),
                                    plan_fp=plan_fp,
                                )
                            if exc.code in (
                                GitHubErrorCode.AUTH_REVOKED,
                                GitHubErrorCode.FORBIDDEN_POLICY,
                                GitHubErrorCode.MISSING_CREDENTIALS,
                            ):
                                return self._handle_provider_error(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                    exc=exc,
                                    access=access,
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    safeguard=safeguard,
                                    current_page=page_num,
                                    current_cursor=expected_cursor,
                                    ts=self._clock(),
                                    plan_fp=plan_fp,
                                )
                            if exc.code is GitHubErrorCode.BYTE_LIMIT_EXCEEDED:
                                return self._handle_provider_error(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                    exc=exc,
                                    access=access,
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    safeguard=safeguard,
                                    current_page=page_num,
                                    current_cursor=expected_cursor,
                                    ts=self._clock(),
                                    plan_fp=plan_fp,
                                )
                            # Non-retryable lookup failure -> explicit UNKNOWN association!
                            lookups.append(
                                CommitPrLookupObservation(
                                    commit_sha=sha,
                                    lookup_succeeded=False,
                                    error_code=exc.code.value,
                                )
                            )

                    batch_result = self._association_classifier.classify_batch(
                        start_snapshot=start_head_snap,
                        end_snapshot=start_head_snap,
                        pull_requests=pr_records,
                        default_branch_commits=commit_records,
                        lookup_observations=lookups,
                    )

                    has_more = (batch_idx + 1) < total_batches
                    next_cursor = f"assoc_batch:{batch_idx + 2}" if has_more else None
                    # Account for request/byte usage through PaginationSafeguard
                    assoc_envelope = ProviderPageEnvelope(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        page_number=page_num,
                        http_status=200,
                        request_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        items=(),
                        has_next_page=has_more,
                        payload_bytes=batch_bytes,
                    )
                    decision = safeguard.evaluate_page(assoc_envelope)
                    if not decision.accepted:
                        return self._handle_safeguard_rejection(
                            ws_uuid=ws_uuid,
                            run_ctx=run_ctx,
                            stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                            decision=decision,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )
                    # Add any additional requests beyond the 1 already counted by evaluate_page
                    if batch_requests > 1:
                        safeguard._requests_total += batch_requests - 1  # noqa: SLF001

                    assoc_fp = hashlib.sha256(
                        safe_json_dumps(
                            {
                                "stream": BackfillStreamName.COMMIT_PR_ASSOCIATIONS.value,
                                "batch": [
                                    (d.commit_sha, d.state.value, d.matched_pr_provider_id)
                                    for d in batch_result.decisions
                                ],
                                "next_cursor": next_cursor,
                            }
                        ).encode("utf-8")
                    ).hexdigest()

                    status, new_ver, new_cur, reason = _commit_and_hook(
                        stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                        page_number=page_num,
                        expected_version=expected_version,
                        expected_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        page_fingerprint=assoc_fp,
                        items=(),
                        item_outcomes=(),
                        commit_associations=batch_result.decisions,
                        stream_completed=not has_more,
                        cursor_metadata_extra=None,
                    )
                    if status is not PageCommitStatus.COMMITTED:
                        return self._handle_commit_block(
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            status=status,
                            reason=reason,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )

                    expected_version = new_ver
                    expected_cursor = new_cur
                    page_num += 1

        # =====================================================================
        # STREAM 5: CONTEXT_ISSUES (Optional PR-referenced issues)
        # =====================================================================
        issue_numbers = sorted(n for n in referenced_issues_set if n not in cues_by_pr)
        if issue_numbers:
            issues_cp = self._page_committer.get_stream_checkpoint(
                workspace_id=ws_uuid,
                sync_run_id=run_uuid,
                stream_name=BackfillStreamName.CONTEXT_ISSUES,
            )
            if not (
                issues_cp is not None
                and issues_cp.stream_status is SyncRunStatus.SUCCESS
                and issues_cp.unresolved_failures == 0
            ):
                page_num = (
                    issues_cp.last_committed_page + 1
                    if issues_cp is not None and issues_cp.last_committed_page is not None
                    else 1
                )
                expected_version = issues_cp.checkpoint_version if issues_cp is not None else 0
                expected_cursor = issues_cp.current_cursor if issues_cp is not None else None
                batch_size = max(1, plan.limits.per_page)
                total_batches = (len(issue_numbers) + batch_size - 1) // batch_size

                for batch_idx in range(page_num - 1, total_batches):
                    batch_issues = issue_numbers[
                        batch_idx * batch_size : (batch_idx + 1) * batch_size
                    ]
                    fetched_revs: list[ObservedGitHubRevision] = []
                    opt_exclusions: list[ItemOutcomeRecord] = []

                    for issue_num in batch_issues:
                        try:
                            rev = self._github_client.fetch_referenced_issue(
                                plan,
                                issue_number=issue_num,
                                token_secret_ref=token_secret_ref,
                            )
                            fetched_revs.append(rev)
                        except GitHubProviderError as exc:
                            if exc.code in (
                                GitHubErrorCode.NOT_FOUND_OR_INACCESSIBLE,
                                GitHubErrorCode.FORBIDDEN_POLICY,
                            ):
                                opt_exclusions.append(
                                    ItemOutcomeRecord(
                                        stream_name=BackfillStreamName.CONTEXT_ISSUES,
                                        object_type="ISSUE",
                                        provider_object_id=str(issue_num),
                                        outcome_kind=ItemOutcomeKind.EXCLUDED,
                                        reason_code=SkipReasonCode.OPTIONAL_CONTEXT_UNAVAILABLE.value,
                                        retryable=False,
                                        outcome_metadata={
                                            "http_status": exc.http_status,
                                            "error_code": exc.code.value,
                                        },
                                    )
                                )
                            elif exc.code in _TRANSIENT_ERROR_CODES or exc.retryable:
                                return self._handle_provider_error(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.CONTEXT_ISSUES,
                                    exc=exc,
                                    access=access,
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    safeguard=safeguard,
                                    current_page=page_num,
                                    current_cursor=expected_cursor,
                                    ts=self._clock(),
                                    plan_fp=plan_fp,
                                )
                            elif exc.code in (
                                GitHubErrorCode.AUTH_REVOKED,
                                GitHubErrorCode.MISSING_CREDENTIALS,
                            ):
                                return self._handle_provider_error(
                                    ws_uuid=ws_uuid,
                                    run_ctx=run_ctx,
                                    stream_name=BackfillStreamName.CONTEXT_ISSUES,
                                    exc=exc,
                                    access=access,
                                    resumed=resumed,
                                    pages_committed=pages_committed,
                                    safeguard=safeguard,
                                    current_page=page_num,
                                    current_cursor=expected_cursor,
                                    ts=self._clock(),
                                    plan_fp=plan_fp,
                                )
                            else:
                                opt_exclusions.append(
                                    ItemOutcomeRecord(
                                        stream_name=BackfillStreamName.CONTEXT_ISSUES,
                                        object_type="ISSUE",
                                        provider_object_id=str(issue_num),
                                        outcome_kind=ItemOutcomeKind.EXCLUDED,
                                        reason_code=SkipReasonCode.OPTIONAL_CONTEXT_UNAVAILABLE.value,
                                        retryable=False,
                                        outcome_metadata={"error_code": exc.code.value},
                                    )
                                )

                    has_more = (batch_idx + 1) < total_batches
                    next_cursor = f"issues_batch:{batch_idx + 2}" if has_more else None
                    envelope = ProviderPageEnvelope(
                        stream_name=BackfillStreamName.CONTEXT_ISSUES,
                        page_number=page_num,
                        http_status=200,
                        request_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        items=tuple(_revision_to_envelope_item(r) for r in fetched_revs),
                        has_next_page=has_more,
                        payload_bytes=sum(
                            int(r.normalized_metadata.get("byte_length") or 0) for r in fetched_revs
                        ),
                        skipped_items=(
                            *_skip_outcomes_for_revisions(
                                BackfillStreamName.CONTEXT_ISSUES, fetched_revs
                            ),
                            *opt_exclusions,
                        ),
                    )
                    decision = safeguard.evaluate_page(envelope)
                    if not decision.accepted:
                        return self._handle_safeguard_rejection(
                            ws_uuid=ws_uuid,
                            run_ctx=run_ctx,
                            stream_name=BackfillStreamName.CONTEXT_ISSUES,
                            decision=decision,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )

                    status, new_ver, new_cur, reason = _commit_and_hook(
                        stream_name=BackfillStreamName.CONTEXT_ISSUES,
                        page_number=page_num,
                        expected_version=expected_version,
                        expected_cursor=expected_cursor,
                        next_cursor=next_cursor,
                        page_fingerprint=decision.page_fingerprint,
                        items=decision.deduplicated_items,
                        item_outcomes=decision.item_outcomes,
                        commit_associations=(),
                        stream_completed=not has_more,
                        cursor_metadata_extra=None,
                    )
                    if status is not PageCommitStatus.COMMITTED:
                        return self._handle_commit_block(
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            status=status,
                            reason=reason,
                            resumed=resumed,
                            pages_committed=pages_committed,
                            plan_fp=plan_fp,
                        )

                    expected_version = new_ver
                    expected_cursor = new_cur
                    page_num += 1

        # =====================================================================
        # COMPLETION GATE: End Snapshot + 2nd-Pass PR Re-Enumeration + Atomic Finalize
        # =====================================================================
        try:
            end_repo_snapshot = self._github_client.fetch_repository_snapshot(
                owner=owner,
                repo=repo_name,
                token_secret_ref=token_secret_ref,
                provider_resource_id=plan.provider_resource_id,
            )
        except GitHubProviderError as exc:
            return self._handle_provider_error(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                exc=exc,
                access=access,
                resumed=resumed,
                pages_committed=pages_committed,
                safeguard=safeguard,
                current_page=1,
                current_cursor=None,
                ts=self._clock(),
                plan_fp=plan_fp,
            )

        if end_repo_snapshot.provider_resource_id != plan.provider_resource_id:
            self._transition_run_non_retryable_failure(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                failure_reason_code="REPOSITORY_IDENTITY_MISMATCH",
                ts=self._clock(),
            )
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_uuid,
                resumed_from_checkpoint=resumed,
                pages_committed=pages_committed,
                plan_fp=plan_fp,
                diagnostic_message="Provider repository ID mismatch at completion gate.",
            )

        if end_repo_snapshot.visibility is not ResourceVisibility.PUBLIC:
            return self._handle_policy_or_auth_block(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                access=access,
                observed_visibility=end_repo_snapshot.visibility.value,
                failure_reason_code="REPOSITORY_VISIBILITY_CHANGED",
                resumed=resumed,
                pages_committed=pages_committed,
                ts=self._clock(),
                plan_fp=plan_fp,
            )

        try:
            second_pass_cues = self._github_client.enumerate_merged_pr_cues(
                plan,
                token_secret_ref=token_secret_ref,
            )
        except GitHubProviderError as exc:
            return self._handle_provider_error(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                stream_name=BackfillStreamName.PRS,
                exc=exc,
                access=access,
                resumed=resumed,
                pages_committed=pages_committed,
                safeguard=safeguard,
                current_page=1,
                current_cursor=None,
                ts=self._clock(),
                plan_fp=plan_fp,
            )

        gate_result = self._completion_gate.verify_completion_snapshot(
            plan=plan,
            end_repo_snapshot=end_repo_snapshot,
            first_pass_merged_prs=first_pass_cues,
            second_pass_merged_prs=second_pass_cues,
            completed_required_streams=REQUIRED_IMPORT_STREAMS,
            has_safe_checkpoint=True,
        )

        first_digest = _compute_pr_cues_digest(first_pass_cues)
        second_digest = _compute_pr_cues_digest(second_pass_cues)

        start_head_snapshot = RepositoryHeadSnapshot(
            default_branch=plan.default_branch,
            head_sha=plan.start_head_sha,
            start_head_reachable=True,
            pr_enumeration_digest=first_digest,
            visibility=plan.visibility.value,
        )
        end_head_snapshot = RepositoryHeadSnapshot(
            default_branch=end_repo_snapshot.default_branch,
            head_sha=end_repo_snapshot.default_branch_head_sha,
            start_head_reachable=True,
            pr_enumeration_digest=second_digest,
            visibility=end_repo_snapshot.visibility.value,
        )

        pr_candidates, direct_commit_candidates = self._build_finalization_candidates_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_uuid,
            source_resource_id=run_ctx.source_resource_id,
            plan=plan,
            pr_numbers=sorted_pr_numbers,
        )

        processed_range = (
            f"window={format_utc_iso(plan.backfill_from)}..{format_utc_iso(plan.cutoff_at)},"
            f"default_branch={plan.default_branch},prs={len(pr_candidates)},"
            f"direct_commits={len(direct_commit_candidates)}"
        )

        before_fin_ctx = FinalizeHookContext(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            source_resource_id=run_ctx.source_resource_id,
            run_version=run_ctx.run_version,
            plan_id=plan.plan_id,
            attempt=attempt,
            pr_candidate_count=len(pr_candidates),
            direct_commit_candidate_count=len(direct_commit_candidates),
            finalize_result=None,
        )
        self._lifecycle_hook.before_finalize(before_fin_ctx)

        finalize_res: FinalizeRunResult = self._page_committer.finalize_import_run(
            FinalizeRunRequest(
                workspace_id=ws_uuid,
                sync_run_id=run_uuid,
                source_resource_id=run_ctx.source_resource_id,
                start_snapshot=start_head_snapshot,
                end_snapshot=end_head_snapshot,
                required_streams=REQUIRED_BACKFILL_STREAMS,
                pr_candidates=pr_candidates,
                direct_commit_candidates=direct_commit_candidates,
                expected_visibility=ResourceVisibility.PUBLIC.value,
                processed_range=processed_range,
            ),
            now=self._clock(),
        )

        after_fin_ctx = FinalizeHookContext(
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            source_resource_id=run_ctx.source_resource_id,
            run_version=run_ctx.run_version,
            plan_id=plan.plan_id,
            attempt=attempt,
            pr_candidate_count=len(pr_candidates),
            direct_commit_candidate_count=len(direct_commit_candidates),
            finalize_result=finalize_res,
        )
        self._lifecycle_hook.after_finalize(after_fin_ctx)

        refreshed = self._load_run_context(ws_uuid, run_uuid)
        assert refreshed is not None
        return InitialBackfillExecutionResult(
            sync_run_id=run_uuid,
            source_resource_id=run_ctx.source_resource_id,
            plan_id=plan.plan_id,
            run_version=refreshed.run_version,
            status=finalize_res.sync_run_status,
            processing_status=refreshed.processing_status,
            import_ready=finalize_res.import_ready,
            retry_allowed=refreshed.retry_allowed,
            has_safe_checkpoint=refreshed.has_safe_checkpoint,
            source_drift=finalize_res.source_drift or gate_result.source_drift,
            resumed_from_checkpoint=resumed,
            failure_reason_code=finalize_res.failure_reason_code,
            processed_range=refreshed.processed_range,
            remaining_range=refreshed.remaining_range,
            import_ready_outbox_id=finalize_res.import_ready_outbox_id,
            promoted_pr_event_ids=finalize_res.promoted_pr_event_ids,
            promoted_direct_commit_event_ids=finalize_res.promoted_direct_commit_event_ids,
            pages_committed_this_attempt=pages_committed,
            frozen_plan_fingerprint=plan_fp,
            diagnostic_message=finalize_res.diagnostic_message,
        )

    # =========================================================================
    # Internal DB & State Helpers
    # =========================================================================
    def _load_run_context(self, ws_uuid: str, run_uuid: str) -> _LoadedRunContext | None:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        SELECT
                            sr.id::text,
                            sr.workspace_id::text,
                            sr.source_resource_id::text,
                            sr.plan_id::text,
                            sr.run_version,
                            sr.status,
                            sr.processing_status,
                            sr.import_ready,
                            sr.retry_allowed,
                            sr.has_safe_checkpoint,
                            sr.source_drift,
                            sr.is_abandoned,
                            sr.plan_spec,
                            sr.failure_reason_code,
                            sr.processed_range,
                            sr.remaining_range,
                            res.provider_resource_id,
                            res.name,
                            res.canonical_url,
                            res.visibility,
                            res.processing_mode,
                            res.raw_storage_mode,
                            res.status,
                            res.source_account_id::text,
                            sa.status,
                            sa.token_secret_ref
                        FROM public.sync_runs sr
                        JOIN public.source_resources res
                          ON res.workspace_id = sr.workspace_id
                         AND res.id = sr.source_resource_id
                        JOIN public.source_accounts sa
                          ON sa.workspace_id = res.workspace_id
                         AND sa.id = res.source_account_id
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                        """,
                        (ws_uuid, run_uuid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    res_id = str(row[2])
                    fence_active = is_deletion_fence_active(cur, ws_uuid, res_id)
                    return _LoadedRunContext(
                        sync_run_id=str(row[0]),
                        workspace_id=str(row[1]),
                        source_resource_id=res_id,
                        plan_id=str(row[3]),
                        run_version=int(row[4]),
                        status=SyncRunStatus(str(row[5])),
                        processing_status=ProcessingStatus(str(row[6])),
                        import_ready=bool(row[7]),
                        retry_allowed=bool(row[8]),
                        has_safe_checkpoint=bool(row[9]),
                        source_drift=bool(row[10]),
                        is_abandoned=bool(row[11]),
                        plan_spec=dict(row[12] or {}),
                        failure_reason_code=row[13],
                        processed_range=row[14],
                        remaining_range=row[15],
                        provider_resource_id=str(row[16]),
                        resource_name=str(row[17]),
                        resource_canonical_url=str(row[18]),
                        resource_visibility=str(row[19]),
                        resource_processing_mode=str(row[20]),
                        resource_raw_storage_mode=str(row[21]),
                        resource_status=str(row[22]),
                        source_account_id=str(row[23]),
                        account_status=str(row[24]),
                        token_secret_ref=str(row[25]),
                        deletion_fence_active=fence_active,
                    )

    def _load_existing_success_promotions(
        self, ws_uuid: str, run_uuid: str
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.outbox_events
                        WHERE workspace_id = %s::uuid
                          AND aggregate_type = 'SYNC_RUN'
                          AND aggregate_id = %s::uuid
                          AND event_type = 'IMPORT_READY'
                        """,
                        (ws_uuid, run_uuid),
                    )
                    ob = cur.fetchone()
                    outbox_id = str(ob[0]) if ob is not None else None
                    cur.execute(
                        """
                        SELECT anchor_mode, work_event_id::text
                        FROM public.sync_run_event_promotions
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                        ORDER BY work_event_id ASC
                        """,
                        (ws_uuid, run_uuid),
                    )
                    pr_ids: list[str] = []
                    dc_ids: list[str] = []
                    for mode, eid in cur.fetchall():
                        if mode == "PR":
                            pr_ids.append(str(eid))
                        elif mode == "DIRECT_COMMIT":
                            dc_ids.append(str(eid))
                    return outbox_id, tuple(sorted(pr_ids)), tuple(sorted(dc_ids))

    def _mark_run_running(
        self,
        ws_uuid: str,
        run_uuid: str,
        *,
        plan_spec: dict[str, Any] | None,
        ts: datetime,
    ) -> None:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    if plan_spec is not None:
                        assert_no_forbidden_raw_keys(plan_spec, field_name="plan_spec")
                        cur.execute(
                            """
                            UPDATE public.sync_runs
                            SET status = 'RUNNING',
                                plan_spec = %s::jsonb,
                                started_at = COALESCE(started_at, %s),
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status IN ('PENDING', 'RUNNING')
                            """,
                            (json.dumps(plan_spec, sort_keys=True), ts, ts, ws_uuid, run_uuid),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE public.sync_runs
                            SET status = 'RUNNING',
                                remaining_range = NULL,
                                started_at = COALESCE(started_at, %s),
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status IN ('PENDING', 'RUNNING')
                            """,
                            (ts, ts, ws_uuid, run_uuid),
                        )

    def _cancel_run_for_deletion_fence(self, ws_uuid: str, run_uuid: str, ts: datetime) -> None:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'CANCELED',
                            processing_status = 'NOT_STARTED',
                            import_ready = false,
                            retry_allowed = false,
                            failure_reason_code = NULL,
                            source_drift = false,
                            run_version = run_version + 1,
                            last_terminal_transition_at = COALESCE(last_terminal_transition_at, %s),
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status IN ('PENDING', 'RUNNING')
                        """,
                        (ts, ts, ws_uuid, run_uuid),
                    )

    def _transition_run_non_retryable_failure(
        self,
        *,
        ws_uuid: str,
        run_uuid: str,
        failure_reason_code: str,
        ts: datetime,
    ) -> None:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'FAILED',
                            processing_status = 'NOT_STARTED',
                            import_ready = false,
                            retry_allowed = false,
                            failure_reason_code = %s,
                            source_drift = false,
                            run_version = run_version + 1,
                            last_terminal_transition_at = COALESCE(last_terminal_transition_at, %s),
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status IN ('PENDING', 'RUNNING')
                        """,
                        (failure_reason_code, ts, ts, ws_uuid, run_uuid),
                    )

    def _handle_policy_or_auth_block(
        self,
        *,
        ws_uuid: str,
        run_ctx: _LoadedRunContext,
        access: WorkspaceAccessContext | None,
        observed_visibility: str | None,
        failure_reason_code: str,
        resumed: bool,
        pages_committed: int,
        ts: datetime,
        plan_fp: str | None = None,
    ) -> InitialBackfillExecutionResult:
        # 1. Delegate to github_connect policy verifier when available (updates source_accounts / revokes secret on 401)
        if self._github_connect_service is not None and access is not None:
            try:
                self._github_connect_service.verify_resource_policy(
                    access,
                    source_resource_id=run_ctx.source_resource_id,
                )
            except (GitHubConnectError, WorkspaceResourceNotFoundError):
                pass

        # 2. Defense-in-depth under worker role: ensure resource & provenance are POLICY_REVIEW_REQUIRED/BLOCKED,
        #    0 tombstones created, and sync_run is non-retryable terminal (FAILED or CANCELED).
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    if observed_visibility in (
                        ResourceVisibility.PRIVATE.value,
                        ResourceVisibility.INTERNAL.value,
                    ):
                        cur.execute(
                            """
                            UPDATE public.source_resources
                            SET visibility = %s,
                                status = 'POLICY_REVIEW_REQUIRED',
                                last_visibility_checked_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status <> 'DELETED'
                            """,
                            (observed_visibility, ts, ws_uuid, run_ctx.source_resource_id),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE public.source_resources
                            SET status = 'POLICY_REVIEW_REQUIRED',
                                last_visibility_checked_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status <> 'DELETED'
                            """,
                            (ts, ws_uuid, run_ctx.source_resource_id),
                        )

                    cur.execute(
                        """
                        UPDATE public.source_objects
                        SET lifecycle_state = 'POLICY_REVIEW_REQUIRED'
                        WHERE workspace_id = %s::uuid
                          AND source_resource_id = %s::uuid
                          AND lifecycle_state <> 'DELETED'
                        """,
                        (ws_uuid, run_ctx.source_resource_id),
                    )
                    cur.execute(
                        """
                        UPDATE public.content_units cu
                        SET policy_state = 'BLOCKED'
                        FROM public.source_object_revisions rev
                        JOIN public.source_objects so
                          ON so.workspace_id = rev.workspace_id
                         AND so.id = rev.source_object_id
                        WHERE cu.workspace_id = %s::uuid
                          AND rev.workspace_id = cu.workspace_id
                          AND rev.id = cu.source_object_revision_id
                          AND so.source_resource_id = %s::uuid
                          AND cu.policy_state = 'ACTIVE'
                        """,
                        (ws_uuid, run_ctx.source_resource_id),
                    )
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'FAILED',
                            processing_status = 'NOT_STARTED',
                            import_ready = false,
                            retry_allowed = false,
                            failure_reason_code = %s,
                            source_drift = false,
                            run_version = run_version + 1,
                            last_terminal_transition_at = COALESCE(last_terminal_transition_at, %s),
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status IN ('PENDING', 'RUNNING')
                        """,
                        (failure_reason_code, ts, ts, ws_uuid, run_ctx.sync_run_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.audit_events (
                            id, workspace_id, actor_type, actor_id,
                            action, target_type, target_id, redacted_metadata, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, NULL,
                            'INITIAL_BACKFILL_POLICY_BLOCKED', 'SYNC_RUN', %s::uuid, %s::jsonb, %s
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            ws_uuid,
                            AuditActorType.WORKER.value,
                            run_ctx.sync_run_id,
                            json.dumps(
                                {
                                    "source_resource_id": run_ctx.source_resource_id,
                                    "failure_reason_code": failure_reason_code,
                                    "observed_visibility": observed_visibility,
                                    "tombstone_created": False,
                                },
                                sort_keys=True,
                            ),
                            ts,
                        ),
                    )

        return self._build_result_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_ctx.sync_run_id,
            resumed_from_checkpoint=resumed,
            pages_committed=pages_committed,
            plan_fp=plan_fp,
            diagnostic_message=f"Import halted due to policy/auth transition ({failure_reason_code}).",
        )

    def _handle_provider_error(
        self,
        *,
        ws_uuid: str,
        run_ctx: _LoadedRunContext,
        stream_name: BackfillStreamName,
        exc: GitHubProviderError,
        access: WorkspaceAccessContext | None,
        resumed: bool,
        pages_committed: int,
        safeguard: PaginationSafeguard | None,
        current_page: int,
        current_cursor: str | None,
        ts: datetime,
        plan_fp: str | None = None,
    ) -> InitialBackfillExecutionResult:
        if exc.code in _POLICY_AUTH_ERROR_CODES:
            return self._handle_policy_or_auth_block(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                access=access,
                observed_visibility=None,
                failure_reason_code=exc.code.value,
                resumed=resumed,
                pages_committed=pages_committed,
                ts=ts,
                plan_fp=plan_fp,
            )

        if exc.code is GitHubErrorCode.BYTE_LIMIT_EXCEEDED:
            return self._record_limit_exceeded(
                ws_uuid=ws_uuid,
                run_ctx=run_ctx,
                stream_name=stream_name,
                processed_range=(
                    f"stream={stream_name.value},committed_pages=0..{max(0, current_page - 1)}"
                ),
                remaining_range=(
                    f"stream={stream_name.value},from_page={current_page},"
                    f"cursor={current_cursor or 'START'},reason=BYTE_LIMIT_EXCEEDED"
                ),
                resumed=resumed,
                pages_committed=pages_committed,
                plan_fp=plan_fp,
            )

        if exc.code in _TRANSIENT_ERROR_CODES or exc.retryable:
            processed_range = (
                f"stream={stream_name.value},committed_pages=0..{max(0, current_page - 1)},"
                f"unique_objects={safeguard.unique_objects_total if safeguard else 0}"
            )
            remaining_range = (
                f"stream={stream_name.value},from_page={current_page},"
                f"cursor={current_cursor or 'START'},reason={exc.code.value}"
            )
            self._page_committer.record_non_success_terminal_state(
                workspace_id=ws_uuid,
                sync_run_id=run_ctx.sync_run_id,
                source_resource_id=run_ctx.source_resource_id,
                stream_name=stream_name,
                target_status=SyncRunStatus.PARTIAL,
                failure_reason_code=BackfillFailureReasonCode.TRANSIENT_PROVIDER_ERROR,
                processed_range=processed_range,
                remaining_range=remaining_range,
                unresolved_failures=1,
                now=ts,
            )
            return self._build_result_from_db(
                ws_uuid=ws_uuid,
                run_uuid=run_ctx.sync_run_id,
                resumed_from_checkpoint=resumed,
                pages_committed=pages_committed,
                plan_fp=plan_fp,
                diagnostic_message=str(exc),
            )

        # PARTIAL_RESPONSE / INVALID_RESPONSE -> non-retryable MALFORMED_OR_PARTIAL_PAGE
        self._page_committer.record_non_success_terminal_state(
            workspace_id=ws_uuid,
            sync_run_id=run_ctx.sync_run_id,
            source_resource_id=run_ctx.source_resource_id,
            stream_name=stream_name,
            target_status=SyncRunStatus.PARTIAL,
            failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
            processed_range=f"stream={stream_name.value},committed_pages=0..{max(0, current_page - 1)}",
            remaining_range=f"stream={stream_name.value},from_page={current_page},reason={exc.code.value}",
            unresolved_failures=1,
            now=ts,
        )
        return self._build_result_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_ctx.sync_run_id,
            resumed_from_checkpoint=resumed,
            pages_committed=pages_committed,
            plan_fp=plan_fp,
            diagnostic_message=str(exc),
        )

    def _handle_safeguard_rejection(
        self,
        *,
        ws_uuid: str,
        run_ctx: _LoadedRunContext,
        stream_name: BackfillStreamName,
        decision: Any,
        resumed: bool,
        pages_committed: int,
        plan_fp: str,
    ) -> InitialBackfillExecutionResult:
        target_status = decision.terminal_status or SyncRunStatus.PARTIAL
        self._page_committer.record_non_success_terminal_state(
            workspace_id=ws_uuid,
            sync_run_id=run_ctx.sync_run_id,
            source_resource_id=run_ctx.source_resource_id,
            stream_name=stream_name,
            target_status=target_status,
            failure_reason_code=decision.failure_reason_code,
            processed_range=decision.processed_range,
            remaining_range=decision.remaining_range,
            item_outcomes=decision.item_outcomes,
            unresolved_failures=1,
            now=self._clock(),
        )
        return self._build_result_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_ctx.sync_run_id,
            resumed_from_checkpoint=resumed,
            pages_committed=pages_committed,
            plan_fp=plan_fp,
            diagnostic_message=decision.diagnostic_message,
        )

    def _record_limit_exceeded(
        self,
        *,
        ws_uuid: str,
        run_ctx: _LoadedRunContext,
        stream_name: BackfillStreamName,
        processed_range: str,
        remaining_range: str,
        resumed: bool,
        pages_committed: int,
        plan_fp: str | None,
    ) -> InitialBackfillExecutionResult:
        self._page_committer.record_non_success_terminal_state(
            workspace_id=ws_uuid,
            sync_run_id=run_ctx.sync_run_id,
            source_resource_id=run_ctx.source_resource_id,
            stream_name=stream_name,
            target_status=SyncRunStatus.PARTIAL_WITH_LIMIT,
            failure_reason_code=BackfillFailureReasonCode.LIMIT_EXCEEDED,
            processed_range=processed_range,
            remaining_range=remaining_range,
            unresolved_failures=1,
            now=self._clock(),
        )
        return self._build_result_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_ctx.sync_run_id,
            resumed_from_checkpoint=resumed,
            pages_committed=pages_committed,
            plan_fp=plan_fp,
            diagnostic_message=f"Import budget exceeded: {remaining_range}",
        )

    def _handle_commit_block(
        self,
        *,
        ws_uuid: str,
        run_uuid: str,
        status: PageCommitStatus,
        reason: str | None,
        resumed: bool,
        pages_committed: int,
        plan_fp: str,
    ) -> InitialBackfillExecutionResult:
        if status is PageCommitStatus.BLOCKED_BY_DELETION_FENCE:
            self._cancel_run_for_deletion_fence(ws_uuid, run_uuid, self._clock())
        return self._build_result_from_db(
            ws_uuid=ws_uuid,
            run_uuid=run_uuid,
            resumed_from_checkpoint=resumed,
            pages_committed=pages_committed,
            plan_fp=plan_fp,
            diagnostic_message=reason or f"Page commit blocked: {status.value}",
        )

    def _build_result_from_db(
        self,
        *,
        ws_uuid: str,
        run_uuid: str,
        resumed_from_checkpoint: bool,
        pages_committed: int,
        plan_fp: str | None = None,
        diagnostic_message: str | None = None,
    ) -> InitialBackfillExecutionResult:
        refreshed = self._load_run_context(ws_uuid, run_uuid)
        assert refreshed is not None
        return InitialBackfillExecutionResult(
            sync_run_id=run_uuid,
            source_resource_id=refreshed.source_resource_id,
            plan_id=refreshed.plan_id,
            run_version=refreshed.run_version,
            status=refreshed.status,
            processing_status=refreshed.processing_status,
            import_ready=refreshed.import_ready,
            retry_allowed=refreshed.retry_allowed,
            has_safe_checkpoint=refreshed.has_safe_checkpoint,
            source_drift=refreshed.source_drift,
            resumed_from_checkpoint=resumed_from_checkpoint,
            failure_reason_code=refreshed.failure_reason_code,
            processed_range=refreshed.processed_range,
            remaining_range=refreshed.remaining_range,
            pages_committed_this_attempt=pages_committed,
            frozen_plan_fingerprint=plan_fp,
            diagnostic_message=diagnostic_message,
        )

    def _build_safeguard_from_db(
        self,
        ws_uuid: str,
        run_uuid: str,
        source_resource_id: str,
        plan: FrozenImportPlan,
    ) -> PaginationSafeguard:
        visited_cursors: dict[BackfillStreamName, set[str]] = {
            s: set() for s in BackfillStreamName
        }
        unique_objects: set[tuple[str, str]] = set()
        initial_requests = 0
        initial_bytes = 0

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        SELECT stream_name, request_cursor
                        FROM public.sync_run_page_observations
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                        """,
                        (ws_uuid, run_uuid),
                    )
                    for stream_raw, req_cursor in cur.fetchall():
                        stream = BackfillStreamName(str(stream_raw))
                        visited_cursors[stream].add(
                            str(req_cursor) if req_cursor is not None else "__START__"
                        )

                    cur.execute(
                        """
                        SELECT cursor_metadata
                        FROM public.sync_checkpoints
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                        """,
                        (ws_uuid, run_uuid),
                    )
                    for (meta_raw,) in cur.fetchall():
                        meta = dict(meta_raw or {})
                        req_val = meta.get("safeguard_requests_total")
                        byte_val = meta.get("safeguard_bytes_total")
                        if isinstance(req_val, int):
                            initial_requests = max(initial_requests, req_val)
                        if isinstance(byte_val, int):
                            initial_bytes = max(initial_bytes, byte_val)

                    cur.execute(
                        """
                        SELECT object_type, provider_object_id
                        FROM public.source_objects
                        WHERE workspace_id = %s::uuid
                          AND source_resource_id = %s::uuid
                        """,
                        (ws_uuid, source_resource_id),
                    )
                    for obj_type, pid in cur.fetchall():
                        unique_objects.add((str(obj_type), str(pid)))

        return PaginationSafeguard(
            limits=BackfillLimits(
                max_objects=plan.limits.max_objects,
                max_requests=plan.limits.max_requests,
                max_bytes=plan.limits.max_total_bytes,
            ),
            initial_visited_cursors=visited_cursors,
            initial_unique_objects=unique_objects,
            initial_requests=initial_requests,
            initial_bytes=initial_bytes,
        )

    def _load_pr_records_for_classification(
        self,
        *,
        ws_uuid: str,
        source_resource_id: str,
        plan: FrozenImportPlan,
        pr_numbers: Sequence[int],
    ) -> tuple[PullRequestRecord, ...]:
        if not pr_numbers:
            return ()
        pr_id_strs = [str(n) for n in pr_numbers]
        records: list[PullRequestRecord] = []
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        SELECT DISTINCT ON (so.provider_object_id)
                            so.provider_object_id,
                            rev.normalized_metadata
                        FROM public.source_objects so
                        JOIN public.source_object_revisions rev
                          ON rev.workspace_id = so.workspace_id
                         AND rev.source_object_id = so.id
                        WHERE so.workspace_id = %s::uuid
                          AND so.source_resource_id = %s::uuid
                          AND so.object_type = 'PR'
                          AND so.provider_object_id = ANY(%s)
                        ORDER BY so.provider_object_id, rev.observed_at DESC
                        """,
                        (ws_uuid, source_resource_id, pr_id_strs),
                    )
                    rows_by_id = {str(r[0]): dict(r[1] or {}) for r in cur.fetchall()}

        for pr_num in sorted(pr_numbers):
            pid = str(pr_num)
            meta = rows_by_id.get(pid, {})
            records.append(
                PullRequestRecord(
                    provider_object_id=pid,
                    pr_number=pr_num,
                    title=f"PR #{pr_num}",
                    merged=True,
                    in_window=True,
                    base_branch=str(meta.get("base_ref") or plan.default_branch),
                    head_sha=meta.get("head_sha"),
                    merge_commit_sha=meta.get("merge_commit_sha"),
                    merged_at=parse_github_iso_timestamp(meta.get("merged_at")),
                    created_at=parse_github_iso_timestamp(meta.get("created_at")),
                )
            )
        return tuple(records)

    def _build_finalization_candidates_from_db(
        self,
        *,
        ws_uuid: str,
        run_uuid: str,
        source_resource_id: str,
        plan: FrozenImportPlan,
        pr_numbers: Sequence[int],
    ) -> tuple[
        tuple[FinalizationPrCandidate, ...],
        tuple[FinalizationDirectCommitCandidate, ...],
    ]:
        pr_candidates: list[FinalizationPrCandidate] = []
        direct_candidates: list[FinalizationDirectCommitCandidate] = []

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)

                    # Load matched commits grouped by PR provider_object_id
                    cur.execute(
                        """
                        SELECT matched_pr_provider_id, commit_sha
                        FROM public.sync_commit_pr_associations
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                          AND pr_association_state = 'MATCHED_PR'
                          AND matched_pr_provider_id IS NOT NULL
                        ORDER BY commit_sha ASC
                        """,
                        (ws_uuid, run_uuid),
                    )
                    commits_by_pr: dict[str, list[str]] = {}
                    for pr_pid, sha in cur.fetchall():
                        commits_by_pr.setdefault(str(pr_pid), []).append(str(sha))

                    # Load reviews grouped by PR number from normalized_metadata->>'provider_number'
                    cur.execute(
                        """
                        SELECT DISTINCT
                            so.provider_object_id,
                            rev.normalized_metadata ->> 'provider_number'
                        FROM public.source_objects so
                        JOIN public.source_object_revisions rev
                          ON rev.workspace_id = so.workspace_id
                         AND rev.source_object_id = so.id
                        WHERE so.workspace_id = %s::uuid
                          AND so.source_resource_id = %s::uuid
                          AND so.object_type = 'REVIEW'
                        ORDER BY so.provider_object_id ASC
                        """,
                        (ws_uuid, source_resource_id),
                    )
                    reviews_by_pr: dict[str, list[str]] = {}
                    for rev_pid, pr_num_str in cur.fetchall():
                        if pr_num_str:
                            reviews_by_pr.setdefault(str(pr_num_str), []).append(str(rev_pid))

                    # Load PR metadata
                    if pr_numbers:
                        pr_id_strs = [str(n) for n in pr_numbers]
                        cur.execute(
                            """
                            SELECT DISTINCT ON (so.provider_object_id)
                                so.provider_object_id,
                                rev.normalized_metadata
                            FROM public.source_objects so
                            JOIN public.source_object_revisions rev
                              ON rev.workspace_id = so.workspace_id
                             AND rev.source_object_id = so.id
                            WHERE so.workspace_id = %s::uuid
                              AND so.source_resource_id = %s::uuid
                              AND so.object_type = 'PR'
                              AND so.provider_object_id = ANY(%s)
                            ORDER BY so.provider_object_id, rev.observed_at DESC
                            """,
                            (ws_uuid, source_resource_id, pr_id_strs),
                        )
                        pr_meta_by_id = {str(r[0]): dict(r[1] or {}) for r in cur.fetchall()}
                        for pr_num in sorted(pr_numbers):
                            pid = str(pr_num)
                            meta = pr_meta_by_id.get(pid, {})
                            summary_meta: dict[str, Any] = {
                                "pr_number": pr_num,
                                "default_branch": plan.default_branch,
                            }
                            if meta.get("head_sha"):
                                summary_meta["head_sha"] = meta["head_sha"]
                            if meta.get("merge_commit_sha"):
                                summary_meta["merge_commit_sha"] = meta["merge_commit_sha"]
                            pr_candidates.append(
                                FinalizationPrCandidate(
                                    provider_object_id=pid,
                                    title=f"PR #{pr_num}",
                                    occurred_start_at=parse_github_iso_timestamp(
                                        meta.get("created_at")
                                    ),
                                    occurred_end_at=parse_github_iso_timestamp(
                                        meta.get("merged_at")
                                    ),
                                    member_commit_provider_ids=tuple(
                                        sorted(set(commits_by_pr.get(pid, ())))
                                    ),
                                    member_review_provider_ids=tuple(
                                        sorted(set(reviews_by_pr.get(pid, ())))
                                    ),
                                    summary_metadata=summary_meta,
                                )
                            )

                    # Load verified NO_MATCH_OBSERVED direct commit candidates
                    cur.execute(
                        """
                        SELECT DISTINCT ON (assoc.commit_sha)
                            so.provider_object_id,
                            assoc.commit_sha,
                            assoc.default_branch_name,
                            rev.provider_updated_at
                        FROM public.sync_commit_pr_associations assoc
                        JOIN public.source_objects so
                          ON so.workspace_id = assoc.workspace_id
                         AND so.id = assoc.commit_source_object_id
                        LEFT JOIN public.source_object_revisions rev
                          ON rev.workspace_id = so.workspace_id
                         AND rev.source_object_id = so.id
                        WHERE assoc.workspace_id = %s::uuid
                          AND assoc.sync_run_id = %s::uuid
                          AND assoc.pr_association_state = 'NO_MATCH_OBSERVED'
                        ORDER BY assoc.commit_sha ASC, rev.observed_at DESC
                        """,
                        (ws_uuid, run_uuid),
                    )
                    for pid, sha, branch, updated_at in cur.fetchall():
                        direct_candidates.append(
                            FinalizationDirectCommitCandidate(
                                provider_object_id=str(pid),
                                commit_sha=str(sha),
                                default_branch=str(branch),
                                title=f"Direct commit {str(sha)[:12]} on {branch}",
                                occurred_at=updated_at,
                                summary_metadata={
                                    "commit_sha": str(sha),
                                    "default_branch": str(branch),
                                },
                            )
                        )

        return tuple(pr_candidates), tuple(direct_candidates)


__all__ = [
    "InitialBackfillExecutionResult",
    "InitialBackfillOrchestrator",
    "frozen_import_plan_from_plan_spec",
]
