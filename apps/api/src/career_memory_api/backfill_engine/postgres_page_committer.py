"""PostgreSQL 18 atomic page committer, 2-worker checkpoint fencing & SUCCESS promotion gate."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from career_memory_api.backfill_engine.association_classifier import (
    CommitPrAssociationClassifier,
)
from career_memory_api.backfill_engine.models import (
    DIRECT_COMMIT_ASSOCIATION_NOTE,
    BackfillFailureReasonCode,
    BackfillStreamName,
    FinalizeOutcomeStatus,
    FinalizeRunRequest,
    FinalizeRunResult,
    ItemOutcomeKind,
    ItemOutcomeRecord,
    PageCommitRequest,
    PageCommitResult,
    PageCommitStatus,
    StreamCheckpointSnapshot,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.schema_contracts.enums import (
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    RecordOrigin,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    validate_sha256_hash,
)
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunStatus

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

MIGRATION_0050_FILENAME = "0050_m2_backfill_observations.sql"


def default_backfill_migration_path() -> Path:
    """Resolve ``<repo_root>/db/migrations/0050_m2_backfill_observations.sql``."""
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "db" / "migrations" / MIGRATION_0050_FILENAME


def apply_backfill_engine_migration(
    conn: Any, *, migration_path: Path | None = None
) -> str:
    """Apply M2 backfill observations migration ``0050`` idempotently onto ``conn``."""
    path = migration_path if migration_path is not None else default_backfill_migration_path()
    if not path.is_file():
        raise FileNotFoundError(f"M2 backfill migration not found: {path}")
    sql_text = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
    if not getattr(conn, "autocommit", False):
        conn.commit()
    return path.name


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


class PostgresBackfillPageCommitter:
    """PostgreSQL 18 transactional engine for initial-backfill page commits & promotion.

    Guarantees:
    - Every ``commit_page_batch`` locks ``sync_runs`` and ``sync_checkpoints`` with
      ``SELECT ... FOR UPDATE`` and verifies ``expected_checkpoint_version`` (and cursor).
      Stale concurrent workers immediately discard their writes (0 DB mutations) and
      receive the latest checkpoint state.
    - ``source_objects`` upsert + ``source_object_revisions`` immutable append
      (``ON CONFLICT DO NOTHING``) + ``sync_item_outcomes`` + ``sync_commit_pr_associations`` +
      ``sync_checkpoints`` advancement + ``sync_run_page_observations`` execute inside one
      atomic transaction (``checkpoint <= committed page``).
    - Intermediate page transactions NEVER emit ``IMPORT_READY`` outbox rows or create
      ``work_events`` / ``facts`` / ``claims``.
    - ``finalize_import_run`` verifies required streams, zero ``UNKNOWN`` PR associations,
      no plan drift, no deletion/policy fence, and in ONE final transaction sets
      ``sync_runs.status = 'SUCCESS'``, emits exactly ONE ``IMPORT_READY`` outbox event,
      and promotes eligible merged PRs + 1:1 ``NO_MATCH_OBSERVED`` ``DIRECT_COMMIT``
      ``work_events`` idempotently.
    """

    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        clock: Callable[[], datetime] | None = None,
        fault_injector: Callable[[str], None] | None = None,
        after_lock_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._worker_connection_factory = worker_connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_injector = fault_injector
        self._after_lock_hook = after_lock_hook

    def _maybe_inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _maybe_run_lock_hook(self, stage: str) -> None:
        if self._after_lock_hook is not None:
            self._after_lock_hook(stage)

    def get_stream_checkpoint(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        stream_name: BackfillStreamName,
    ) -> StreamCheckpointSnapshot | None:
        """Read current persisted checkpoint for ``(sync_run_id, stream_name)``."""
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        SELECT stream_status, last_committed_page, unresolved_failures,
                               checkpoint_version, cursor_metadata
                        FROM public.sync_checkpoints
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                          AND stream_name = %s
                        """,
                        (ws_uuid, run_uuid, stream_name.value),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    meta = dict(row[4] or {})
                    return StreamCheckpointSnapshot(
                        stream_name=stream_name,
                        stream_status=SyncRunStatus(str(row[0])),
                        last_committed_page=int(row[1]) if row[1] is not None else None,
                        unresolved_failures=int(row[2]) if row[2] is not None else 0,
                        checkpoint_version=int(row[3]),
                        current_cursor=meta.get("current_cursor"),
                        cursor_metadata=meta,
                    )

    def _upsert_item_outcome(
        self,
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
        resource_uuid: str,
        outcome: ItemOutcomeRecord,
        ts: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(outcome.outcome_metadata, field_name="outcome_metadata")
        cur.execute(
            """
            INSERT INTO public.sync_item_outcomes (
                id, workspace_id, sync_run_id, source_resource_id, source_object_id,
                stream_name, object_type, provider_object_id, outcome_kind,
                reason_code, affected_range, retryable, outcome_metadata,
                observed_at, created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb,
                %s, %s
            )
            ON CONFLICT (
                sync_run_id, stream_name, object_type,
                provider_object_id, outcome_kind, reason_code
            )
            DO UPDATE SET
                source_object_id = COALESCE(EXCLUDED.source_object_id, public.sync_item_outcomes.source_object_id),
                affected_range = COALESCE(EXCLUDED.affected_range, public.sync_item_outcomes.affected_range),
                retryable = EXCLUDED.retryable,
                outcome_metadata = EXCLUDED.outcome_metadata,
                observed_at = EXCLUDED.observed_at
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                run_uuid,
                resource_uuid,
                outcome.source_object_id,
                outcome.stream_name.value,
                outcome.object_type,
                outcome.provider_object_id,
                outcome.outcome_kind.value,
                outcome.reason_code,
                outcome.affected_range,
                outcome.retryable,
                json.dumps(outcome.outcome_metadata, sort_keys=True),
                ts,
                ts,
            ),
        )

    def commit_page_batch(
        self,
        request: PageCommitRequest,
        *,
        now: datetime | None = None,
    ) -> PageCommitResult:
        """Atomically persist one validated page batch and advance stream checkpoint."""
        ts = now or self._clock()
        run_uuid = _normalize_uuid(request.sync_run_id, "sync_run_id")
        resource_uuid = _normalize_uuid(request.source_resource_id, "source_resource_id")
        validate_sha256_hash(request.page_fingerprint)

        # Upfront Plan C validation before touching PostgreSQL
        for item in request.items:
            validate_sha256_hash(item.content_hash)
            assert_no_forbidden_raw_keys(item.normalized_metadata, field_name="normalized_metadata")
        for outcome in request.item_outcomes:
            assert_no_forbidden_raw_keys(outcome.outcome_metadata, field_name="outcome_metadata")
        if request.cursor_metadata_extra is not None:
            assert_no_forbidden_raw_keys(
                request.cursor_metadata_extra, field_name="cursor_metadata_extra"
            )

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, request.workspace_id)

                    # 1. Lock sync_runs row FOR UPDATE (serializes concurrent workers & first checkpoint)
                    cur.execute(
                        """
                        SELECT source_resource_id::text, status, is_abandoned
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid),
                    )
                    run_row = cur.fetchone()
                    if run_row is None:
                        raise ValueError(f"SyncRun {run_uuid} not found in workspace {ws_uuid}.")
                    db_resource_id, run_status_raw, is_abandoned = (
                        str(run_row[0]),
                        str(run_row[1]),
                        bool(run_row[2]),
                    )
                    if db_resource_id != resource_uuid:
                        raise ValueError("source_resource_id does not match SyncRun.")

                    # 2. Check deletion fence before any write
                    if is_deletion_fence_active(cur, ws_uuid, resource_uuid):
                        return PageCommitResult(
                            status=PageCommitStatus.BLOCKED_BY_DELETION_FENCE,
                            stream_name=request.stream_name,
                            checkpoint_version=request.expected_checkpoint_version,
                            last_committed_page=None,
                            current_cursor=request.expected_cursor,
                            reason="Active deletion fence on source_resource.",
                        )

                    # 3. Lock sync_checkpoints row FOR UPDATE and verify version/cursor fencing
                    cur.execute(
                        """
                        SELECT id::text, stream_status, last_committed_page,
                               checkpoint_version, cursor_metadata
                        FROM public.sync_checkpoints
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                          AND stream_name = %s
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid, request.stream_name.value),
                    )
                    cp_row = cur.fetchone()
                    if cp_row is None:
                        current_version = 0
                        current_last_page: int | None = None
                        current_cursor: str | None = None
                        existing_cursor_meta: dict[str, Any] = {}
                    else:
                        current_version = int(cp_row[3])
                        current_last_page = int(cp_row[2]) if cp_row[2] is not None else None
                        existing_cursor_meta = dict(cp_row[4] or {})
                        current_cursor = existing_cursor_meta.get("current_cursor")

                    if is_abandoned or run_status_raw not in (
                        SyncRunStatus.PENDING.value,
                        SyncRunStatus.RUNNING.value,
                    ):
                        return PageCommitResult(
                            status=PageCommitStatus.BLOCKED_BY_RUN_STATE,
                            stream_name=request.stream_name,
                            checkpoint_version=current_version,
                            last_committed_page=current_last_page,
                            current_cursor=current_cursor,
                            reason=f"SyncRun is not active (status={run_status_raw}, abandoned={is_abandoned}).",
                        )

                    # Stale worker check: discard write immediately with ZERO DB writes!
                    if current_version != request.expected_checkpoint_version or (
                        current_version > 0 and current_cursor != request.expected_cursor
                    ):
                        return PageCommitResult(
                            status=PageCommitStatus.STALE_CHECKPOINT_VERSION,
                            stream_name=request.stream_name,
                            checkpoint_version=current_version,
                            last_committed_page=current_last_page,
                            current_cursor=current_cursor,
                            reason=(
                                f"Expected checkpoint_version={request.expected_checkpoint_version} "
                                f"cursor={request.expected_cursor!r}, "
                                f"found version={current_version} cursor={current_cursor!r}."
                            ),
                        )

                    self._maybe_run_lock_hook("checkpoint_locked")

                    # 4. Transition PENDING -> RUNNING inside the same transaction
                    if run_status_raw == SyncRunStatus.PENDING.value:
                        cur.execute(
                            """
                            UPDATE public.sync_runs
                            SET status = 'RUNNING',
                                started_at = COALESCE(started_at, %s),
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            """,
                            (ts, ts, ws_uuid, run_uuid),
                        )

                    # 5. Upsert source_objects + append immutable source_object_revisions
                    objects_upserted = 0
                    revisions_inserted = 0
                    object_id_by_key: dict[tuple[str, str], str] = {}

                    for item in request.items:
                        seen_ts = item.observed_at or ts
                        cur.execute(
                            """
                            INSERT INTO public.source_objects (
                                id, workspace_id, source_resource_id, object_type,
                                provider_object_id, lifecycle_state, pr_association_state,
                                first_seen_at, last_seen_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                %s, 'ACTIVE', %s,
                                %s, %s
                            )
                            ON CONFLICT (source_resource_id, object_type, provider_object_id)
                            DO UPDATE SET
                                pr_association_state = COALESCE(
                                    EXCLUDED.pr_association_state,
                                    public.source_objects.pr_association_state
                                ),
                                last_seen_at = GREATEST(
                                    public.source_objects.last_seen_at,
                                    EXCLUDED.last_seen_at
                                )
                            RETURNING id::text
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_uuid,
                                resource_uuid,
                                item.object_type.value,
                                item.provider_object_id,
                                item.pr_association_state.value
                                if item.pr_association_state is not None
                                else None,
                                seen_ts,
                                seen_ts,
                            ),
                        )
                        obj_row = cur.fetchone()
                        assert obj_row is not None
                        source_object_id = str(obj_row[0])
                        object_id_by_key[(item.object_type.value, item.provider_object_id)] = (
                            source_object_id
                        )
                        objects_upserted += 1

                    self._maybe_inject_fault("after_source_objects")

                    for item in request.items:
                        source_object_id = object_id_by_key[
                            (item.object_type.value, item.provider_object_id)
                        ]
                        cur.execute(
                            """
                            INSERT INTO public.source_object_revisions (
                                id, workspace_id, source_object_id, provider_revision_id,
                                content_hash, hash_policy_version, observed_at,
                                provider_updated_at, is_tombstone, normalized_metadata,
                                connector_version, schema_version
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                %s, %s, %s,
                                %s, %s, %s::jsonb,
                                %s, %s
                            )
                            ON CONFLICT (source_object_id, content_hash) DO NOTHING
                            RETURNING id::text
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_uuid,
                                source_object_id,
                                item.provider_revision_id,
                                item.content_hash,
                                item.hash_policy_version,
                                item.observed_at or ts,
                                item.provider_updated_at,
                                item.is_tombstone,
                                json.dumps(item.normalized_metadata, sort_keys=True),
                                item.connector_version,
                                item.schema_version,
                            ),
                        )
                        if cur.fetchone() is not None:
                            revisions_inserted += 1

                    self._maybe_inject_fault("after_revisions")

                    # 6. Persist item outcomes (skips, exclusions, failures)
                    outcomes_recorded = 0
                    for outcome in request.item_outcomes:
                        self._upsert_item_outcome(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            resource_uuid=resource_uuid,
                            outcome=outcome,
                            ts=ts,
                        )
                        outcomes_recorded += 1

                    self._maybe_inject_fault("after_item_outcomes")

                    # 7. Persist commit-to-PR association decisions & update source_objects
                    associations_recorded = 0
                    for decision in request.commit_associations:
                        commit_obj_id = object_id_by_key.get(
                            (SourceObjectType.COMMIT.value, decision.commit_provider_object_id)
                        )
                        if commit_obj_id is None:
                            cur.execute(
                                """
                                SELECT id::text
                                FROM public.source_objects
                                WHERE workspace_id = %s::uuid
                                  AND source_resource_id = %s::uuid
                                  AND object_type = 'COMMIT'
                                  AND provider_object_id = %s
                                """,
                                (ws_uuid, resource_uuid, decision.commit_provider_object_id),
                            )
                            found = cur.fetchone()
                            if found is None:
                                raise ValueError(
                                    f"Commit source_object not found for {decision.commit_provider_object_id!r}."
                                )
                            commit_obj_id = str(found[0])

                        cur.execute(
                            """
                            UPDATE public.source_objects
                            SET pr_association_state = %s,
                                last_seen_at = GREATEST(last_seen_at, %s)
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            """,
                            (decision.state.value, ts, ws_uuid, commit_obj_id),
                        )

                        obs_meta: dict[str, Any] = {}
                        if decision.direct_commit_badge_metadata is not None:
                            assert_no_forbidden_raw_keys(
                                decision.direct_commit_badge_metadata,
                                field_name="direct_commit_badge_metadata",
                            )
                            obs_meta.update(decision.direct_commit_badge_metadata)

                        cur.execute(
                            """
                            INSERT INTO public.sync_commit_pr_associations (
                                id, workspace_id, sync_run_id, source_resource_id,
                                commit_source_object_id, commit_sha, default_branch_name,
                                pr_association_state, matched_pr_provider_id,
                                matched_pr_in_window, match_mechanism, unknown_reason_code,
                                reachable_at_start, reachable_at_end, lookup_completed,
                                observation_metadata, observed_at, updated_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s::uuid, %s, %s,
                                %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                %s::jsonb, %s, %s
                            )
                            ON CONFLICT (sync_run_id, commit_source_object_id)
                            DO UPDATE SET
                                pr_association_state = EXCLUDED.pr_association_state,
                                matched_pr_provider_id = EXCLUDED.matched_pr_provider_id,
                                matched_pr_in_window = EXCLUDED.matched_pr_in_window,
                                match_mechanism = EXCLUDED.match_mechanism,
                                unknown_reason_code = EXCLUDED.unknown_reason_code,
                                reachable_at_start = EXCLUDED.reachable_at_start,
                                reachable_at_end = EXCLUDED.reachable_at_end,
                                lookup_completed = EXCLUDED.lookup_completed,
                                observation_metadata = EXCLUDED.observation_metadata,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_uuid,
                                run_uuid,
                                resource_uuid,
                                commit_obj_id,
                                decision.commit_sha.strip().lower(),
                                decision.default_branch,
                                decision.state.value,
                                decision.matched_pr_provider_id,
                                decision.matched_pr_in_window,
                                decision.match_mechanism.value
                                if decision.match_mechanism is not None
                                else None,
                                decision.unknown_reason_code,
                                decision.reachable_at_start,
                                decision.reachable_at_end,
                                decision.lookup_completed,
                                json.dumps(obs_meta, sort_keys=True),
                                ts,
                                ts,
                            ),
                        )
                        associations_recorded += 1

                        if decision.exclusion_outcome is not None:
                            self._upsert_item_outcome(
                                cur,
                                ws_uuid=ws_uuid,
                                run_uuid=run_uuid,
                                resource_uuid=resource_uuid,
                                outcome=ItemOutcomeRecord(
                                    stream_name=decision.exclusion_outcome.stream_name,
                                    object_type=decision.exclusion_outcome.object_type,
                                    provider_object_id=decision.exclusion_outcome.provider_object_id,
                                    outcome_kind=decision.exclusion_outcome.outcome_kind,
                                    reason_code=decision.exclusion_outcome.reason_code,
                                    affected_range=decision.exclusion_outcome.affected_range,
                                    retryable=decision.exclusion_outcome.retryable,
                                    outcome_metadata=decision.exclusion_outcome.outcome_metadata,
                                    source_object_id=commit_obj_id,
                                ),
                                ts=ts,
                            )
                            outcomes_recorded += 1

                        if decision.state == PrAssociationState.UNKNOWN:
                            self._upsert_item_outcome(
                                cur,
                                ws_uuid=ws_uuid,
                                run_uuid=run_uuid,
                                resource_uuid=resource_uuid,
                                outcome=ItemOutcomeRecord(
                                    stream_name=BackfillStreamName.COMMIT_PR_ASSOCIATIONS,
                                    object_type="COMMIT",
                                    provider_object_id=decision.commit_provider_object_id,
                                    outcome_kind=ItemOutcomeKind.ASSOCIATION_UNKNOWN,
                                    reason_code=decision.unknown_reason_code
                                    or "ASSOCIATION_UNKNOWN",
                                    outcome_metadata={
                                        "commit_sha": decision.commit_sha,
                                        "default_branch": decision.default_branch,
                                    },
                                    source_object_id=commit_obj_id,
                                ),
                                ts=ts,
                            )
                            outcomes_recorded += 1

                    self._maybe_inject_fault("after_associations")
                    self._maybe_inject_fault("before_checkpoint_update")

                    # 8. Advance sync_checkpoints & record page observation atomically
                    next_version = current_version + 1
                    next_stream_status = (
                        SyncRunStatus.SUCCESS.value
                        if request.stream_completed and request.unresolved_failures == 0
                        else SyncRunStatus.RUNNING.value
                    )
                    new_cursor_meta: dict[str, Any] = dict(existing_cursor_meta)
                    new_cursor_meta["current_cursor"] = request.next_cursor
                    new_cursor_meta["last_page_fingerprint"] = request.page_fingerprint
                    new_cursor_meta["last_page_number"] = request.page_number
                    if request.cursor_metadata_extra:
                        new_cursor_meta.update(request.cursor_metadata_extra)
                    assert_no_forbidden_raw_keys(new_cursor_meta, field_name="cursor_metadata")

                    if cp_row is None:
                        cur.execute(
                            """
                            INSERT INTO public.sync_checkpoints (
                                id, workspace_id, sync_run_id, stream_name,
                                stream_status, last_committed_page, unresolved_failures,
                                checkpoint_version, cursor_metadata, updated_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                %s, %s, %s,
                                %s, %s::jsonb, %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_uuid,
                                run_uuid,
                                request.stream_name.value,
                                next_stream_status,
                                request.page_number,
                                request.unresolved_failures,
                                next_version,
                                json.dumps(new_cursor_meta, sort_keys=True),
                                ts,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE public.sync_checkpoints
                            SET stream_status = %s,
                                last_committed_page = %s,
                                unresolved_failures = %s,
                                checkpoint_version = %s,
                                cursor_metadata = %s::jsonb,
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            """,
                            (
                                next_stream_status,
                                request.page_number,
                                request.unresolved_failures,
                                next_version,
                                json.dumps(new_cursor_meta, sort_keys=True),
                                ts,
                                ws_uuid,
                                str(cp_row[0]),
                            ),
                        )

                    cur.execute(
                        """
                        INSERT INTO public.sync_run_page_observations (
                            id, workspace_id, sync_run_id, stream_name,
                            page_number, request_cursor, next_cursor,
                            page_fingerprint, checkpoint_version,
                            item_count, unique_object_count, committed_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (sync_run_id, stream_name, page_number) DO NOTHING
                        """,
                        (
                            str(uuid.uuid4()),
                            ws_uuid,
                            run_uuid,
                            request.stream_name.value,
                            request.page_number,
                            request.expected_cursor,
                            request.next_cursor,
                            request.page_fingerprint,
                            next_version,
                            len(request.items),
                            len(object_id_by_key),
                            ts,
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET has_safe_checkpoint = true,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (ts, ws_uuid, run_uuid),
                    )

                    self._maybe_inject_fault("after_checkpoint_update")

        # Post-commit hook (allows tests to simulate worker crash immediately after DB COMMIT)
        self._maybe_inject_fault("after_commit_before_return")

        return PageCommitResult(
            status=PageCommitStatus.COMMITTED,
            stream_name=request.stream_name,
            checkpoint_version=next_version,
            last_committed_page=request.page_number,
            current_cursor=request.next_cursor,
            objects_upserted=objects_upserted,
            revisions_inserted=revisions_inserted,
            outcomes_recorded=outcomes_recorded,
            associations_recorded=associations_recorded,
        )

    def record_non_success_terminal_state(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        source_resource_id: str,
        stream_name: BackfillStreamName,
        target_status: SyncRunStatus,
        failure_reason_code: BackfillFailureReasonCode | None,
        processed_range: str | None = None,
        remaining_range: str | None = None,
        item_outcomes: tuple[ItemOutcomeRecord, ...] = (),
        unresolved_failures: int = 1,
        now: datetime | None = None,
    ) -> SyncRunStatus:
        """Persist PARTIAL / PARTIAL_WITH_LIMIT / FAILED / CANCELED without advancing checkpoint.

        Never creates IMPORT_READY outbox rows or work_events/facts/claims.
        """
        if target_status not in (
            SyncRunStatus.PARTIAL,
            SyncRunStatus.PARTIAL_WITH_LIMIT,
            SyncRunStatus.FAILED,
            SyncRunStatus.CANCELED,
        ):
            raise ValueError(f"Unsupported non-success terminal status: {target_status}")

        ts = now or self._clock()
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")
        resource_uuid = _normalize_uuid(source_resource_id, "source_resource_id")

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        SELECT status, has_safe_checkpoint, is_abandoned, last_terminal_transition_at
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid),
                    )
                    run_row = cur.fetchone()
                    if run_row is None:
                        raise ValueError(f"SyncRun {run_uuid} not found.")
                    has_safe_checkpoint = bool(run_row[1])
                    is_abandoned = bool(run_row[2])
                    existing_terminal_at = run_row[3]

                    for outcome in item_outcomes:
                        self._upsert_item_outcome(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            resource_uuid=resource_uuid,
                            outcome=outcome,
                            ts=ts,
                        )

                    # Mark stream status WITHOUT advancing last_committed_page!
                    cur.execute(
                        """
                        INSERT INTO public.sync_checkpoints (
                            id, workspace_id, sync_run_id, stream_name,
                            stream_status, last_committed_page, unresolved_failures,
                            checkpoint_version, cursor_metadata, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s,
                            %s, NULL, %s,
                            1, '{}'::jsonb, %s
                        )
                        ON CONFLICT (sync_run_id, stream_name)
                        DO UPDATE SET
                            stream_status = EXCLUDED.stream_status,
                            unresolved_failures = COALESCE(public.sync_checkpoints.unresolved_failures, 0)
                                + EXCLUDED.unresolved_failures,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            str(uuid.uuid4()),
                            ws_uuid,
                            run_uuid,
                            stream_name.value,
                            target_status.value,
                            max(0, unresolved_failures),
                            ts,
                        ),
                    )

                    reason_str = (
                        failure_reason_code.value
                        if target_status != SyncRunStatus.CANCELED and failure_reason_code is not None
                        else None
                    )
                    source_drift = (
                        target_status == SyncRunStatus.PARTIAL
                        and reason_str == BackfillFailureReasonCode.PLAN_DRIFT.value
                    )
                    retry_allowed = (
                        target_status == SyncRunStatus.PARTIAL
                        and reason_str == BackfillFailureReasonCode.TRANSIENT_PROVIDER_ERROR.value
                        and not source_drift
                        and has_safe_checkpoint
                        and not is_abandoned
                    )
                    effective_remaining = remaining_range
                    if target_status == SyncRunStatus.PARTIAL_WITH_LIMIT and not (
                        effective_remaining and effective_remaining.strip()
                    ):
                        effective_remaining = f"stream={stream_name.value},limit_exceeded"

                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = %s,
                            processing_status = 'NOT_STARTED',
                            import_ready = false,
                            retry_allowed = %s,
                            failure_reason_code = %s,
                            source_drift = %s,
                            processed_range = COALESCE(%s, processed_range),
                            remaining_range = COALESCE(%s, remaining_range),
                            run_version = run_version + 1,
                            last_terminal_transition_at = COALESCE(%s, %s),
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (
                            target_status.value,
                            retry_allowed,
                            reason_str,
                            source_drift,
                            processed_range,
                            effective_remaining,
                            existing_terminal_at,
                            ts,
                            ts,
                            ws_uuid,
                            run_uuid,
                        ),
                    )
        return target_status

    def _transition_run_blocked(
        self,
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
        target_status: SyncRunStatus,
        failure_reason_code: BackfillFailureReasonCode | None,
        source_drift: bool,
        remaining_range: str | None,
        ts: datetime,
    ) -> None:
        reason_str = (
            failure_reason_code.value
            if target_status != SyncRunStatus.CANCELED and failure_reason_code is not None
            else None
        )
        effective_remaining = remaining_range
        if target_status == SyncRunStatus.PARTIAL_WITH_LIMIT and not (
            effective_remaining and effective_remaining.strip()
        ):
            effective_remaining = "limit_exceeded"

        cur.execute(
            """
            UPDATE public.sync_runs
            SET status = %s,
                processing_status = 'NOT_STARTED',
                import_ready = false,
                retry_allowed = false,
                failure_reason_code = %s,
                source_drift = %s,
                remaining_range = COALESCE(%s, remaining_range),
                run_version = run_version + 1,
                last_terminal_transition_at = COALESCE(last_terminal_transition_at, %s),
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (
                target_status.value,
                reason_str,
                source_drift,
                effective_remaining,
                ts,
                ts,
                ws_uuid,
                run_uuid,
            ),
        )

    def _load_existing_promotions(
        self, cur: psycopg.Cursor, ws_uuid: str, run_uuid: str
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
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
        outbox_row = cur.fetchone()
        outbox_id = str(outbox_row[0]) if outbox_row is not None else None

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
        direct_ids: list[str] = []
        for mode, event_id in cur.fetchall():
            if mode == WorkEventAnchorMode.PR.value:
                pr_ids.append(str(event_id))
            elif mode == WorkEventAnchorMode.DIRECT_COMMIT.value:
                direct_ids.append(str(event_id))
        return outbox_id, tuple(sorted(pr_ids)), tuple(sorted(direct_ids))

    def finalize_import_run(
        self,
        request: FinalizeRunRequest,
        *,
        now: datetime | None = None,
    ) -> FinalizeRunResult:
        """Single-transaction SUCCESS gate: emits 1 IMPORT_READY + promotes PR/DIRECT_COMMIT events."""
        ts = now or self._clock()
        run_uuid = _normalize_uuid(request.sync_run_id, "sync_run_id")
        resource_uuid = _normalize_uuid(request.source_resource_id, "source_resource_id")

        for pr_cand in request.pr_candidates:
            assert_no_forbidden_raw_keys(pr_cand.summary_metadata, field_name="summary_metadata")
        for dc_cand in request.direct_commit_candidates:
            assert_no_forbidden_raw_keys(dc_cand.summary_metadata, field_name="summary_metadata")

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, request.workspace_id)

                    # 1. Lock sync_runs FOR UPDATE
                    cur.execute(
                        """
                        SELECT source_resource_id::text, plan_id::text, status, is_abandoned,
                               failure_reason_code, source_drift, remaining_range
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid),
                    )
                    run_row = cur.fetchone()
                    if run_row is None:
                        raise ValueError(f"SyncRun {run_uuid} not found in workspace {ws_uuid}.")
                    (
                        db_resource_id,
                        plan_id,
                        run_status_raw,
                        is_abandoned,
                        existing_reason,
                        existing_drift,
                        existing_remaining,
                    ) = (
                        str(run_row[0]),
                        str(run_row[1]),
                        str(run_row[2]),
                        bool(run_row[3]),
                        run_row[4],
                        bool(run_row[5]),
                        run_row[6],
                    )
                    if db_resource_id != resource_uuid:
                        raise ValueError("source_resource_id does not match SyncRun.")

                    # Idempotent replay if already SUCCESS
                    if run_status_raw == SyncRunStatus.SUCCESS.value:
                        outbox_id, pr_ids, direct_ids = self._load_existing_promotions(
                            cur, ws_uuid, run_uuid
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.ALREADY_SUCCEEDED,
                            sync_run_status=SyncRunStatus.SUCCESS,
                            import_ready=True,
                            import_ready_outbox_id=outbox_id,
                            promoted_pr_event_ids=pr_ids,
                            promoted_direct_commit_event_ids=direct_ids,
                        )

                    # Already non-success terminal or abandoned -> zero promotion
                    if is_abandoned or run_status_raw in (
                        SyncRunStatus.PARTIAL.value,
                        SyncRunStatus.PARTIAL_WITH_LIMIT.value,
                        SyncRunStatus.FAILED.value,
                        SyncRunStatus.CANCELED.value,
                    ):
                        outcome = (
                            FinalizeOutcomeStatus.BLOCKED_PARTIAL_WITH_LIMIT
                            if run_status_raw == SyncRunStatus.PARTIAL_WITH_LIMIT.value
                            else FinalizeOutcomeStatus.BLOCKED_PARTIAL
                        )
                        return FinalizeRunResult(
                            outcome=outcome,
                            sync_run_status=SyncRunStatus(run_status_raw),
                            import_ready=False,
                            failure_reason_code=existing_reason,
                            source_drift=existing_drift,
                            diagnostic_message=f"Run already terminal ({run_status_raw}).",
                        )

                    # 2. Check deletion fence & source_resources status/visibility fence
                    cur.execute(
                        """
                        SELECT status, visibility
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, resource_uuid),
                    )
                    res_row = cur.fetchone()
                    if (
                        res_row is None
                        or str(res_row[0]) == "DELETED"
                        or request.end_snapshot.is_deleted
                        or is_deletion_fence_active(cur, ws_uuid, resource_uuid)
                    ):
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.CANCELED,
                            failure_reason_code=None,
                            source_drift=False,
                            remaining_range=None,
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_BY_DELETION_FENCE,
                            sync_run_status=SyncRunStatus.CANCELED,
                            import_ready=False,
                            diagnostic_message="Repository deletion fence active.",
                        )

                    res_status, res_visibility = str(res_row[0]), str(res_row[1])
                    visibility_drifted = (
                        res_status != "ACTIVE"
                        or request.start_snapshot.visibility != request.end_snapshot.visibility
                        or (
                            request.expected_visibility is not None
                            and request.end_snapshot.visibility != request.expected_visibility
                        )
                        or request.end_snapshot.visibility != res_visibility
                    )
                    if visibility_drifted:
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.PARTIAL,
                            failure_reason_code=BackfillFailureReasonCode.POLICY_VISIBILITY_CHANGED,
                            source_drift=False,
                            remaining_range=None,
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_BY_POLICY_FENCE,
                            sync_run_status=SyncRunStatus.PARTIAL,
                            import_ready=False,
                            failure_reason_code=BackfillFailureReasonCode.POLICY_VISIBILITY_CHANGED.value,
                            diagnostic_message="Repository visibility or policy status changed during import.",
                        )

                    # 3. Verify plan integrity (branch / HEAD / force-push / secondary PR enumeration)
                    plan_eval = CommitPrAssociationClassifier.evaluate_plan_integrity(
                        request.start_snapshot, request.end_snapshot
                    )
                    if plan_eval.drift_detected:
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.PARTIAL,
                            failure_reason_code=BackfillFailureReasonCode.PLAN_DRIFT,
                            source_drift=True,
                            remaining_range=None,
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_PARTIAL,
                            sync_run_status=SyncRunStatus.PARTIAL,
                            import_ready=False,
                            failure_reason_code=BackfillFailureReasonCode.PLAN_DRIFT.value,
                            source_drift=True,
                            retry_allowed=False,
                            diagnostic_message=", ".join(plan_eval.drift_reasons),
                        )

                    # 4. Verify all required streams completed cleanly
                    cur.execute(
                        """
                        SELECT stream_name, stream_status, COALESCE(unresolved_failures, 0)
                        FROM public.sync_checkpoints
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                        """,
                        (ws_uuid, run_uuid),
                    )
                    stream_rows = {
                        str(r[0]): (str(r[1]), int(r[2])) for r in cur.fetchall()
                    }
                    if any(
                        st == SyncRunStatus.PARTIAL_WITH_LIMIT.value
                        for st, _ in stream_rows.values()
                    ):
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.PARTIAL_WITH_LIMIT,
                            failure_reason_code=BackfillFailureReasonCode.LIMIT_EXCEEDED,
                            source_drift=False,
                            remaining_range=existing_remaining or "limit_exceeded",
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_PARTIAL_WITH_LIMIT,
                            sync_run_status=SyncRunStatus.PARTIAL_WITH_LIMIT,
                            import_ready=False,
                            failure_reason_code=BackfillFailureReasonCode.LIMIT_EXCEEDED.value,
                            diagnostic_message="At least one stream terminated with PARTIAL_WITH_LIMIT.",
                        )

                    incomplete_streams: list[str] = []
                    for req_stream in sorted(request.required_streams, key=lambda s: s.value):
                        info = stream_rows.get(req_stream.value)
                        if info is None or info[0] != SyncRunStatus.SUCCESS.value or info[1] > 0:
                            incomplete_streams.append(req_stream.value)

                    if incomplete_streams:
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.PARTIAL,
                            failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE,
                            source_drift=False,
                            remaining_range=None,
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_PARTIAL,
                            sync_run_status=SyncRunStatus.PARTIAL,
                            import_ready=False,
                            failure_reason_code=BackfillFailureReasonCode.MALFORMED_OR_PARTIAL_PAGE.value,
                            diagnostic_message=f"Incomplete required stream(s): {', '.join(incomplete_streams)}",
                        )

                    # 5. Verify ZERO UNKNOWN PR associations in DB
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM public.sync_commit_pr_associations
                        WHERE workspace_id = %s::uuid
                          AND sync_run_id = %s::uuid
                          AND pr_association_state = 'UNKNOWN'
                        """,
                        (ws_uuid, run_uuid),
                    )
                    unknown_count = int(cur.fetchone()[0])
                    if unknown_count > 0:
                        self._transition_run_blocked(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            target_status=SyncRunStatus.PARTIAL,
                            failure_reason_code=BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION,
                            source_drift=False,
                            remaining_range=None,
                            ts=ts,
                        )
                        return FinalizeRunResult(
                            outcome=FinalizeOutcomeStatus.BLOCKED_PARTIAL,
                            sync_run_status=SyncRunStatus.PARTIAL,
                            import_ready=False,
                            failure_reason_code=BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION.value,
                            diagnostic_message=f"{unknown_count} commit(s) remain in UNKNOWN PR association state.",
                        )

                    # 6. Defense-in-depth: verify every DIRECT_COMMIT candidate is strictly NO_MATCH_OBSERVED
                    direct_commit_obj_ids: dict[str, str] = {}
                    for dc in request.direct_commit_candidates:
                        cur.execute(
                            """
                            SELECT so.id::text, assoc.pr_association_state
                            FROM public.source_objects AS so
                            INNER JOIN public.sync_commit_pr_associations AS assoc
                                ON assoc.workspace_id = so.workspace_id
                               AND assoc.commit_source_object_id = so.id
                               AND assoc.sync_run_id = %s::uuid
                            WHERE so.workspace_id = %s::uuid
                              AND so.source_resource_id = %s::uuid
                              AND so.object_type = 'COMMIT'
                              AND so.provider_object_id = %s
                            """,
                            (run_uuid, ws_uuid, resource_uuid, dc.provider_object_id),
                        )
                        dc_row = cur.fetchone()
                        if (
                            dc_row is None
                            or str(dc_row[1]) != PrAssociationState.NO_MATCH_OBSERVED.value
                        ):
                            self._transition_run_blocked(
                                cur,
                                ws_uuid=ws_uuid,
                                run_uuid=run_uuid,
                                target_status=SyncRunStatus.PARTIAL,
                                failure_reason_code=BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION,
                                source_drift=False,
                                remaining_range=None,
                                ts=ts,
                            )
                            return FinalizeRunResult(
                                outcome=FinalizeOutcomeStatus.BLOCKED_PARTIAL,
                                sync_run_status=SyncRunStatus.PARTIAL,
                                import_ready=False,
                                failure_reason_code=BackfillFailureReasonCode.INCOMPLETE_PR_ASSOCIATION.value,
                                diagnostic_message=(
                                    f"Direct commit candidate {dc.provider_object_id!r} is not verified "
                                    "NO_MATCH_OBSERVED."
                                ),
                            )
                        direct_commit_obj_ids[dc.provider_object_id] = str(dc_row[0])

                    # 7. ALL GATES PASSED -> Single-transaction SUCCESS + Promotion + Single IMPORT_READY
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'SUCCESS',
                            processing_status = %s,
                            import_ready = true,
                            retry_allowed = false,
                            failure_reason_code = NULL,
                            source_drift = false,
                            observed_at = %s,
                            processed_range = COALESCE(%s, processed_range),
                            run_version = run_version + 1,
                            last_terminal_transition_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (
                            ProcessingStatus.NOT_STARTED.value,
                            ts,
                            request.processed_range,
                            ts,
                            ts,
                            ws_uuid,
                            run_uuid,
                        ),
                    )

                    promoted_pr_event_ids: list[str] = []
                    for pr_cand in request.pr_candidates:
                        cur.execute(
                            """
                            SELECT id::text
                            FROM public.source_objects
                            WHERE workspace_id = %s::uuid
                              AND source_resource_id = %s::uuid
                              AND object_type = 'PR'
                              AND provider_object_id = %s
                            """,
                            (ws_uuid, resource_uuid, pr_cand.provider_object_id),
                        )
                        pr_obj_row = cur.fetchone()
                        if pr_obj_row is None:
                            raise ValueError(
                                f"PR source_object not found for {pr_cand.provider_object_id!r}."
                            )
                        pr_obj_id = str(pr_obj_row[0])

                        event_id = self._promote_event_idempotently(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            primary_anchor_source_object_id=pr_obj_id,
                            anchor_mode=WorkEventAnchorMode.PR,
                            title=pr_cand.title,
                            summary_metadata=dict(pr_cand.summary_metadata),
                            occurred_start_at=pr_cand.occurred_start_at,
                            occurred_end_at=pr_cand.occurred_end_at,
                            primary_rationale="PR_ANCHOR",
                            ts=ts,
                        )
                        promoted_pr_event_ids.append(event_id)

                        # MUST_LINK memberships for PR commits
                        for commit_pid in pr_cand.member_commit_provider_ids:
                            cur.execute(
                                """
                                SELECT id::text
                                FROM public.source_objects
                                WHERE workspace_id = %s::uuid
                                  AND source_resource_id = %s::uuid
                                  AND object_type = 'COMMIT'
                                  AND provider_object_id = %s
                                """,
                                (ws_uuid, resource_uuid, commit_pid),
                            )
                            c_row = cur.fetchone()
                            if c_row is not None:
                                self._insert_event_membership(
                                    cur,
                                    ws_uuid=ws_uuid,
                                    event_id=event_id,
                                    source_object_id=str(c_row[0]),
                                    relation_type="PR_COMMIT",
                                    rationale_code="MATCHED_PR_COMMIT",
                                    ts=ts,
                                )

                        # MUST_LINK memberships for PR reviews
                        for review_pid in pr_cand.member_review_provider_ids:
                            cur.execute(
                                """
                                SELECT id::text
                                FROM public.source_objects
                                WHERE workspace_id = %s::uuid
                                  AND source_resource_id = %s::uuid
                                  AND object_type = 'REVIEW'
                                  AND provider_object_id = %s
                                """,
                                (ws_uuid, resource_uuid, review_pid),
                            )
                            r_row = cur.fetchone()
                            if r_row is not None:
                                self._insert_event_membership(
                                    cur,
                                    ws_uuid=ws_uuid,
                                    event_id=event_id,
                                    source_object_id=str(r_row[0]),
                                    relation_type="PR_REVIEW",
                                    rationale_code="MATCHED_PR_REVIEW",
                                    ts=ts,
                                )

                    promoted_direct_event_ids: list[str] = []
                    for dc_cand in request.direct_commit_candidates:
                        dc_obj_id = direct_commit_obj_ids[dc_cand.provider_object_id]
                        dc_meta = dict(dc_cand.summary_metadata)
                        dc_meta["user_review_required"] = True
                        dc_meta["association_note"] = DIRECT_COMMIT_ASSOCIATION_NOTE
                        dc_meta["commit_sha"] = dc_cand.commit_sha.strip().lower()
                        dc_meta["default_branch"] = dc_cand.default_branch
                        assert_no_forbidden_raw_keys(dc_meta, field_name="summary_metadata")

                        event_id = self._promote_event_idempotently(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            primary_anchor_source_object_id=dc_obj_id,
                            anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
                            title=dc_cand.title,
                            summary_metadata=dc_meta,
                            occurred_start_at=dc_cand.occurred_at,
                            occurred_end_at=dc_cand.occurred_at,
                            primary_rationale="DIRECT_COMMIT_ANCHOR",
                            ts=ts,
                        )
                        promoted_direct_event_ids.append(event_id)

                    self._maybe_inject_fault("after_finalize_events_before_outbox")

                    # Single IMPORT_READY outbox event per SyncRun (0040 partial unique index enforced)
                    outbox_id = str(uuid.uuid4())
                    outbox_dedupe_key = f"import_ready:{run_uuid}"
                    outbox_payload = {
                        "sync_run_id": run_uuid,
                        "source_resource_id": resource_uuid,
                        "plan_id": plan_id,
                        "default_branch": request.end_snapshot.default_branch,
                        "promoted_pr_event_count": len(promoted_pr_event_ids),
                        "promoted_direct_commit_event_count": len(promoted_direct_event_ids),
                    }
                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'IMPORT_READY', 'SYNC_RUN',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
                        RETURNING id::text
                        """,
                        (
                            outbox_id,
                            ws_uuid,
                            run_uuid,
                            outbox_dedupe_key,
                            json.dumps(outbox_payload, sort_keys=True),
                            ts,
                            ts,
                        ),
                    )
                    outbox_row = cur.fetchone()
                    if outbox_row is not None:
                        final_outbox_id = str(outbox_row[0])
                    else:
                        cur.execute(
                            """
                            SELECT id::text
                            FROM public.outbox_events
                            WHERE workspace_id = %s::uuid
                              AND dedupe_key = %s
                            """,
                            (ws_uuid, outbox_dedupe_key),
                        )
                        existing_ob = cur.fetchone()
                        assert existing_ob is not None
                        final_outbox_id = str(existing_ob[0])

                    cur.execute(
                        """
                        INSERT INTO public.audit_events (
                            id, workspace_id, actor_type, actor_id,
                            action, target_type, target_id, redacted_metadata, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, NULL,
                            'SYNC_RUN_IMPORT_READY', 'SYNC_RUN', %s::uuid, %s::jsonb, %s
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            ws_uuid,
                            AuditActorType.WORKER.value,
                            run_uuid,
                            json.dumps(outbox_payload, sort_keys=True),
                            ts,
                        ),
                    )

                    self._maybe_inject_fault("before_finalize_commit")

        return FinalizeRunResult(
            outcome=FinalizeOutcomeStatus.SUCCESS,
            sync_run_status=SyncRunStatus.SUCCESS,
            import_ready=True,
            import_ready_outbox_id=final_outbox_id,
            promoted_pr_event_ids=tuple(sorted(promoted_pr_event_ids)),
            promoted_direct_commit_event_ids=tuple(sorted(promoted_direct_event_ids)),
        )

    def _promote_event_idempotently(
        self,
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
        primary_anchor_source_object_id: str,
        anchor_mode: WorkEventAnchorMode,
        title: str,
        summary_metadata: dict[str, Any],
        occurred_start_at: datetime | None,
        occurred_end_at: datetime | None,
        primary_rationale: str,
        ts: datetime,
    ) -> str:
        cur.execute(
            """
            SELECT work_event_id::text
            FROM public.sync_run_event_promotions
            WHERE workspace_id = %s::uuid
              AND source_object_id = %s::uuid
              AND anchor_mode = %s
            """,
            (ws_uuid, primary_anchor_source_object_id, anchor_mode.value),
        )
        existing_promo = cur.fetchone()
        if existing_promo is not None:
            event_id = str(existing_promo[0])
        else:
            event_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                    anchor_scope, version, title, summary_metadata,
                    status, created_by, occurred_start_at, occurred_end_at,
                    created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s,
                    NULL, 1, %s, %s::jsonb,
                    %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    event_id,
                    ws_uuid,
                    primary_anchor_source_object_id,
                    anchor_mode.value,
                    title.strip() or f"{anchor_mode.value} candidate",
                    json.dumps(summary_metadata, sort_keys=True),
                    WorkEventStatus.CANDIDATE.value,
                    RecordOrigin.RULE.value,
                    occurred_start_at,
                    occurred_end_at,
                    ts,
                    ts,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.sync_run_event_promotions (
                    id, workspace_id, sync_run_id, source_object_id,
                    anchor_mode, work_event_id, promoted_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s, %s::uuid, %s
                )
                ON CONFLICT (workspace_id, source_object_id, anchor_mode) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    ws_uuid,
                    run_uuid,
                    primary_anchor_source_object_id,
                    anchor_mode.value,
                    event_id,
                    ts,
                ),
            )

        self._insert_event_membership(
            cur,
            ws_uuid=ws_uuid,
            event_id=event_id,
            source_object_id=primary_anchor_source_object_id,
            relation_type="PRIMARY_ANCHOR",
            rationale_code=primary_rationale,
            ts=ts,
        )
        return event_id

    @staticmethod
    def _insert_event_membership(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        event_id: str,
        source_object_id: str,
        relation_type: str,
        rationale_code: str,
        ts: datetime,
    ) -> None:
        cur.execute(
            """
            INSERT INTO public.event_memberships (
                id, workspace_id, event_id, source_object_id,
                relation_class, relation_type, origin, rationale_code,
                status, created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s,
                'ACTIVE', %s
            )
            ON CONFLICT (event_id, source_object_id, relation_type) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                event_id,
                source_object_id,
                EventMembershipRelationClass.MUST_LINK.value,
                relation_type,
                EventMembershipOrigin.RULE.value,
                rationale_code,
                ts,
            ),
        )
