"""PostgreSQL 18 single-transaction SyncRun coordinator and read-only recovery service."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from career_memory_api.auth.postgres_guard import bind_postgres_workspace_context
from career_memory_api.authz import (
    WorkspaceAccessContext,
    WorkspaceMembershipDeniedError,
)
from career_memory_api.jobs.models import (
    AuditActorType,
    NoopWakeNotifier,
    PostCommitWakeNotifierPort,
    WakeSignal,
)
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.sync_runs.models import (
    ACTIVE_SYNC_RUN_STATUSES,
    AcceptedSyncRunReceipt,
    ProcessingStatus,
    StreamProgress,
    SyncRunApiError,
    SyncRunObservation,
    SyncRunStatus,
    SyncRunStatusSnapshot,
    compute_create_request_fingerprint,
    compute_retry_request_fingerprint,
    parse_idempotency_key,
    parse_initial_import_request,
)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
IDEMPOTENCY_WINDOW = timedelta(hours=24)

_SYNC_RUN_COLUMNS = """
    id::text,
    workspace_id::text,
    source_resource_id::text,
    plan_id::text,
    mode,
    run_version,
    status,
    processing_status,
    import_ready,
    retry_allowed,
    failure_reason_code,
    has_safe_checkpoint,
    source_drift,
    observed_at,
    processed_range,
    remaining_range,
    is_abandoned,
    last_terminal_transition_at,
    created_at
"""


def _try_normalize_uuid(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _require_live_membership(
    cur: psycopg.Cursor, access: WorkspaceAccessContext
) -> None:
    """Verify current unrevoked Workspace membership inside the active transaction."""
    cur.execute(
        """
        SELECT 1
        FROM public.workspace_memberships
        WHERE workspace_id = %s::uuid
          AND user_id = %s::uuid
          AND revoked_at IS NULL
        """,
        (access.workspace_id, access.user_id),
    )
    if cur.fetchone() is None:
        raise WorkspaceMembershipDeniedError(
            "Access to the selected workspace has been revoked."
        )


def _load_streams_for_run(
    cur: psycopg.Cursor, workspace_id: str, sync_run_id: str
) -> tuple[StreamProgress, ...]:
    cur.execute(
        """
        SELECT stream_name, stream_status, last_committed_page, unresolved_failures
        FROM public.sync_checkpoints
        WHERE workspace_id = %s::uuid
          AND sync_run_id = %s::uuid
        ORDER BY stream_name ASC
        """,
        (workspace_id, sync_run_id),
    )
    return tuple(
        StreamProgress(
            type=str(r[0]),
            status=str(r[1]),
            last_committed_page=int(r[2]) if r[2] is not None else None,
            unresolved_failures=int(r[3]) if r[3] is not None else None,
        )
        for r in cur.fetchall()
    )


def _row_to_snapshot(
    row: tuple[Any, ...], streams: tuple[StreamProgress, ...]
) -> SyncRunStatusSnapshot:
    return SyncRunStatusSnapshot(
        id=row[0],
        source_resource_id=row[2],
        plan_id=row[3],
        run_version=int(row[5]),
        status=SyncRunStatus(row[6]),
        processing_status=ProcessingStatus(row[7]),
        import_ready=bool(row[8]),
        retry_allowed=bool(row[9]),
        failure_reason=row[10],
        source_drift=bool(row[12]),
        observed_at=row[13],
        processed_range=row[14],
        remaining_range=row[15],
        streams=streams,
    )


def _verify_run_readable_for_replay(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    run_id: str,
    unreadable_run_ids: frozenset[str],
) -> None:
    if run_id in unreadable_run_ids:
        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")
    cur.execute(
        """
        SELECT sr.source_resource_id::text
        FROM public.sync_runs sr
        JOIN public.source_resources res
          ON res.workspace_id = sr.workspace_id
         AND res.id = sr.source_resource_id
        WHERE sr.workspace_id = %s::uuid
          AND sr.id = %s::uuid
          AND res.status <> 'DELETED'
        """,
        (workspace_id, run_id),
    )
    row = cur.fetchone()
    if row is None or is_deletion_fence_active(cur, workspace_id, str(row[0])):
        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")


def _check_idempotency_replay(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    operation_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
    now: datetime,
    unreadable_run_ids: frozenset[str],
) -> AcceptedSyncRunReceipt | None:
    cur.execute(
        """
        SELECT request_fingerprint, target_sync_run_id::text, response_body, expires_at
        FROM public.api_idempotency_records
        WHERE workspace_id = %s::uuid
          AND operation_scope = %s
          AND idempotency_key = %s
        """,
        (workspace_id, operation_scope, idempotency_key),
    )
    row = cur.fetchone()
    if row is None:
        return None

    stored_fingerprint, target_run_id, response_body, expires_at = (
        str(row[0]),
        str(row[1]),
        dict(row[2]),
        row[3],
    )
    _verify_run_readable_for_replay(
        cur,
        workspace_id=workspace_id,
        run_id=target_run_id,
        unreadable_run_ids=unreadable_run_ids,
    )
    if now >= expires_at:
        raise SyncRunApiError(status_code=409, code="IDEMPOTENCY_KEY_EXPIRED")
    if stored_fingerprint != request_fingerprint:
        raise SyncRunApiError(status_code=409, code="IDEMPOTENCY_CONFLICT")
    return AcceptedSyncRunReceipt.from_dict(response_body)


class PostgresSyncRunService:
    """Product PostgreSQL 18 implementation of the 4 initial-import routes (T-105)."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory,
        worker_connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        wake_notifier: PostCommitWakeNotifierPort | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._worker_connection_factory = worker_connection_factory or connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wake_notifier: PostCommitWakeNotifierPort = (
            wake_notifier if wake_notifier is not None else NoopWakeNotifier()
        )
        self._fault_injector = fault_injector

    def _maybe_inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _notify_post_commit(self, signal: WakeSignal) -> None:
        try:
            self._wake_notifier.notify_job_available(signal)
        except Exception:  # noqa: BLE001
            # Post-commit wake failures must never fail the already-committed 202 response;
            # PENDING job + outbox rows remain durable for recovery runners.
            return None

    def recover_latest(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        unreadable_run_ids: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Read-only recovery lookup: GET /v1/sync-runs?source_resource_id=...&limit=1."""
        resource_uuid = _try_normalize_uuid(source_resource_id)

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    bind_postgres_workspace_context(cur, access)
                    _require_live_membership(cur, access)

                    if resource_uuid is None:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    cur.execute(
                        """
                        SELECT status
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (access.workspace_id, resource_uuid),
                    )
                    res_row = cur.fetchone()
                    if res_row is None or res_row[0] == "DELETED" or is_deletion_fence_active(
                        cur, access.workspace_id, resource_uuid
                    ):
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    cur.execute(
                        f"""
                        SELECT {_SYNC_RUN_COLUMNS}
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND source_resource_id = %s::uuid
                        ORDER BY
                            CASE WHEN status IN ('PENDING', 'RUNNING') THEN 0 ELSE 1 END ASC,
                            last_terminal_transition_at DESC NULLS LAST,
                            created_at DESC
                        """,
                        (access.workspace_id, resource_uuid),
                    )
                    selected = next(
                        (r for r in cur.fetchall() if str(r[0]) not in unreadable_run_ids),
                        None,
                    )
                    if selected is None:
                        return {"items": []}

                    streams = _load_streams_for_run(
                        cur, access.workspace_id, str(selected[0])
                    )
                    snapshot = _row_to_snapshot(selected, streams)
                    return {
                        "items": [
                            {
                                "target_id": snapshot.id,
                                "status_url": f"/v1/sync-runs/{snapshot.id}",
                                "import_status": snapshot.to_contract_dict(),
                            }
                        ]
                    }

    def get_status(
        self,
        access: WorkspaceAccessContext,
        *,
        run_id: str,
        unreadable_run_ids: frozenset[str] = frozenset(),
    ) -> SyncRunStatusSnapshot:
        """Read-only run detail lookup: GET /v1/sync-runs/{id}."""
        run_uuid = _try_normalize_uuid(run_id)

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    bind_postgres_workspace_context(cur, access)
                    _require_live_membership(cur, access)

                    if run_uuid is None or run_uuid in unreadable_run_ids:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    cur.execute(
                        f"""
                        SELECT {_SYNC_RUN_COLUMNS}
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (access.workspace_id, run_uuid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    resource_id = str(row[2])
                    cur.execute(
                        """
                        SELECT status
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (access.workspace_id, resource_id),
                    )
                    res_row = cur.fetchone()
                    if res_row is None or res_row[0] == "DELETED" or is_deletion_fence_active(
                        cur, access.workspace_id, resource_id
                    ):
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    streams = _load_streams_for_run(cur, access.workspace_id, run_uuid)
                    return _row_to_snapshot(row, streams)

    def create_initial_import(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        request_id: str | None = None,
        unreadable_run_ids: frozenset[str] = frozenset(),
    ) -> AcceptedSyncRunReceipt:
        """Atomically persist SyncRun + Job + Outbox + Idempotency + Audit, then wake Worker."""
        normalized_key = parse_idempotency_key(idempotency_key)
        parsed = parse_initial_import_request(request_payload)
        fingerprint = compute_create_request_fingerprint(parsed)
        resource_uuid = _try_normalize_uuid(parsed.source_resource_id)
        now = self._clock()
        wake_signal: WakeSignal | None = None

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_postgres_workspace_context(cur, access)
                    _require_live_membership(cur, access)

                    if resource_uuid is None:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    # Lock parent source_resource row to serialize concurrent creates cleanly.
                    cur.execute(
                        """
                        SELECT status
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (access.workspace_id, resource_uuid),
                    )
                    res_row = cur.fetchone()
                    if res_row is None or res_row[0] == "DELETED" or is_deletion_fence_active(
                        cur, access.workspace_id, resource_uuid
                    ):
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    # Check existing 24h idempotency record AFTER access & deletion fence checks.
                    replayed = _check_idempotency_replay(
                        cur,
                        workspace_id=access.workspace_id,
                        operation_scope="SYNC_RUN_CREATE",
                        idempotency_key=normalized_key,
                        request_fingerprint=fingerprint,
                        now=now,
                        unreadable_run_ids=unreadable_run_ids,
                    )
                    if replayed is not None:
                        return replayed

                    # Enforce single active run and no replacement of non-abandoned runs.
                    cur.execute(
                        """
                        SELECT id::text, status, is_abandoned
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND source_resource_id = %s::uuid
                          AND status <> 'CANCELED'
                          AND NOT is_abandoned
                        ORDER BY created_at DESC
                        """,
                        (access.workspace_id, resource_uuid),
                    )
                    existing_runs = cur.fetchall()
                    for existing_id, existing_status, _abandoned in existing_runs:
                        if SyncRunStatus(existing_status) in ACTIVE_SYNC_RUN_STATUSES:
                            readable_id = (
                                None
                                if str(existing_id) in unreadable_run_ids
                                else str(existing_id)
                            )
                            raise SyncRunApiError(
                                status_code=409,
                                code="SYNC_ALREADY_RUNNING",
                                existing_run_id=readable_id,
                            )
                    if existing_runs:
                        raise SyncRunApiError(status_code=409)

                    run_id = str(uuid.uuid4())
                    plan_id = str(uuid.uuid4())
                    job_id = str(uuid.uuid4())
                    outbox_id = str(uuid.uuid4())

                    plan_spec: dict[str, Any] = {
                        "mode": parsed.mode.value,
                    }
                    if parsed.backfill_from is not None:
                        plan_spec["backfill_from"] = parsed.backfill_from
                    if parsed.limits is not None:
                        plan_spec["limits"] = dict(parsed.limits)

                    cur.execute(
                        """
                        INSERT INTO public.sync_runs (
                            id, workspace_id, source_resource_id, plan_id,
                            mode, run_version, status, processing_status,
                            import_ready, plan_spec, retry_allowed,
                            has_safe_checkpoint, source_drift,
                            created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            'INITIAL_BACKFILL', 1, 'PENDING', 'NOT_STARTED',
                            false, %s::jsonb, false,
                            false, false,
                            %s, %s
                        )
                        """,
                        (
                            run_id,
                            access.workspace_id,
                            resource_uuid,
                            plan_id,
                            json.dumps(plan_spec, sort_keys=True),
                            now,
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_sync_run")

                    input_manifest = {
                        "sync_run_id": run_id,
                        "source_resource_id": resource_uuid,
                        "plan_id": plan_id,
                        "run_version": 1,
                        "mode": parsed.mode.value,
                    }
                    input_hash = hashlib.sha256(
                        json.dumps(input_manifest, sort_keys=True, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    ).hexdigest()

                    cur.execute(
                        """
                        INSERT INTO public.jobs (
                            id, workspace_id, job_type, target_sync_run_id,
                            dedupe_key, input_hash, input_manifest, status,
                            attempt, max_attempts, next_run_at, checkpoint,
                            logical_effect_key, created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'INITIAL_BACKFILL_SYNC', %s::uuid,
                            %s, %s, %s::jsonb, 'PENDING',
                            0, 3, %s, '{}'::jsonb,
                            %s, %s, %s
                        )
                        """,
                        (
                            job_id,
                            access.workspace_id,
                            run_id,
                            f"sync_run:{run_id}:v1",
                            input_hash,
                            json.dumps(input_manifest, sort_keys=True),
                            now,
                            f"sync_run:{run_id}:v1",
                            now,
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_job")

                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'SYNC_RUN_WAKE', 'SYNC_RUN',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        """,
                        (
                            outbox_id,
                            access.workspace_id,
                            run_id,
                            f"sync_run_wake:{run_id}:v1",
                            json.dumps(
                                {
                                    "sync_run_id": run_id,
                                    "job_id": job_id,
                                    "run_version": 1,
                                },
                                sort_keys=True,
                            ),
                            now,
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_outbox")

                    receipt = AcceptedSyncRunReceipt(
                        target_id=run_id,
                        status_url=f"/v1/sync-runs/{run_id}",
                        job_id=job_id,
                    )
                    expires_at = now + IDEMPOTENCY_WINDOW
                    cur.execute(
                        """
                        INSERT INTO public.api_idempotency_records (
                            workspace_id, operation_scope, idempotency_key,
                            request_fingerprint, target_sync_run_id,
                            response_status, response_body, created_at, expires_at
                        )
                        VALUES (
                            %s::uuid, 'SYNC_RUN_CREATE', %s,
                            %s, %s::uuid,
                            202, %s::jsonb, %s, %s
                        )
                        """,
                        (
                            access.workspace_id,
                            normalized_key,
                            fingerprint,
                            run_id,
                            json.dumps(receipt.to_contract_dict(), sort_keys=True),
                            now,
                            expires_at,
                        ),
                    )
                    self._maybe_inject_fault("after_idempotency")

                    audit_meta: dict[str, Any] = {
                        "run_version": 1,
                        "job_id": job_id,
                        "source_resource_id": resource_uuid,
                    }
                    if request_id:
                        audit_meta["request_id"] = request_id
                    cur.execute(
                        """
                        INSERT INTO public.audit_events (
                            id, workspace_id, actor_type, actor_id,
                            action, target_type, target_id, redacted_metadata, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, %s::uuid,
                            'SYNC_RUN_CREATED', 'SYNC_RUN', %s::uuid, %s::jsonb, %s
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            access.workspace_id,
                            AuditActorType.USER.value,
                            access.user_id,
                            run_id,
                            json.dumps(audit_meta, sort_keys=True),
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_audit")

                    wake_signal = WakeSignal(
                        workspace_id=access.workspace_id,
                        job_id=job_id,
                        job_type="INITIAL_BACKFILL_SYNC",
                        target_sync_run_id=run_id,
                        outbox_event_id=outbox_id,
                    )

        # Strictly post-commit wake invocation
        if wake_signal is not None:
            self._notify_post_commit(wake_signal)
        return receipt

    def retry_sync_run(
        self,
        access: WorkspaceAccessContext,
        *,
        run_id: str,
        idempotency_key: str,
        expected_run_version: int,
        request_id: str | None = None,
        unreadable_run_ids: frozenset[str] = frozenset(),
    ) -> AcceptedSyncRunReceipt:
        """Atomically replay-or-transition a retryable SyncRun with optimistic run_version check."""
        normalized_key = parse_idempotency_key(idempotency_key)
        run_uuid = _try_normalize_uuid(run_id)
        now = self._clock()
        wake_signal: WakeSignal | None = None

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_postgres_workspace_context(cur, access)
                    _require_live_membership(cur, access)

                    if run_uuid is None or run_uuid in unreadable_run_ids:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    # Lock the SyncRun row FIRST to serialize concurrent retries cleanly.
                    cur.execute(
                        f"""
                        SELECT {_SYNC_RUN_COLUMNS}
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (access.workspace_id, run_uuid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    resource_id = str(row[2])
                    cur.execute(
                        """
                        SELECT status
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR SHARE
                        """,
                        (access.workspace_id, resource_id),
                    )
                    res_row = cur.fetchone()
                    if res_row is None or res_row[0] == "DELETED" or is_deletion_fence_active(
                        cur, access.workspace_id, resource_id
                    ):
                        raise SyncRunApiError(status_code=404, code="RESOURCE_NOT_FOUND")

                    fingerprint = compute_retry_request_fingerprint(
                        run_uuid, expected_run_version
                    )

                    # In-window replay of accepted key restores original 202 BEFORE version/eligibility checks!
                    replayed = _check_idempotency_replay(
                        cur,
                        workspace_id=access.workspace_id,
                        operation_scope="SYNC_RUN_RETRY",
                        idempotency_key=normalized_key,
                        request_fingerprint=fingerprint,
                        now=now,
                        unreadable_run_ids=unreadable_run_ids,
                    )
                    if replayed is not None:
                        return replayed

                    current_version = int(row[5])
                    current_status = SyncRunStatus(row[6])
                    retry_allowed = bool(row[9])
                    failure_reason_code = row[10]
                    has_safe_checkpoint = bool(row[11])
                    source_drift = bool(row[12])
                    is_abandoned = bool(row[16])
                    plan_id = str(row[3])

                    if current_version != expected_run_version:
                        raise SyncRunApiError(
                            status_code=409, code="RESOURCE_VERSION_CONFLICT"
                        )
                    if current_status in ACTIVE_SYNC_RUN_STATUSES:
                        raise SyncRunApiError(
                            status_code=409,
                            code="SYNC_ALREADY_RUNNING",
                            existing_run_id=run_uuid,
                        )
                    if (
                        is_abandoned
                        or current_status != SyncRunStatus.PARTIAL
                        or failure_reason_code != "TRANSIENT_PROVIDER_ERROR"
                        or source_drift
                        or not retry_allowed
                        or not has_safe_checkpoint
                    ):
                        raise SyncRunApiError(status_code=409)

                    next_version = current_version + 1
                    job_id = str(uuid.uuid4())
                    outbox_id = str(uuid.uuid4())

                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'PENDING',
                            retry_allowed = false,
                            failure_reason_code = NULL,
                            last_terminal_transition_at = NULL,
                            run_version = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND run_version = %s
                        """,
                        (
                            next_version,
                            now,
                            access.workspace_id,
                            run_uuid,
                            current_version,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise SyncRunApiError(
                            status_code=409, code="RESOURCE_VERSION_CONFLICT"
                        )
                    self._maybe_inject_fault("after_sync_run")

                    input_manifest = {
                        "sync_run_id": run_uuid,
                        "source_resource_id": resource_id,
                        "plan_id": plan_id,
                        "run_version": next_version,
                        "mode": "INITIAL_BACKFILL",
                        "resume_from_safe_checkpoint": True,
                    }
                    input_hash = hashlib.sha256(
                        json.dumps(input_manifest, sort_keys=True, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    ).hexdigest()

                    cur.execute(
                        """
                        INSERT INTO public.jobs (
                            id, workspace_id, job_type, target_sync_run_id,
                            dedupe_key, input_hash, input_manifest, status,
                            attempt, max_attempts, next_run_at, checkpoint,
                            logical_effect_key, created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'INITIAL_BACKFILL_SYNC', %s::uuid,
                            %s, %s, %s::jsonb, 'PENDING',
                            0, 3, %s, '{}'::jsonb,
                            %s, %s, %s
                        )
                        """,
                        (
                            job_id,
                            access.workspace_id,
                            run_uuid,
                            f"sync_run:{run_uuid}:v{next_version}",
                            input_hash,
                            json.dumps(input_manifest, sort_keys=True),
                            now,
                            f"sync_run:{run_uuid}:v{next_version}",
                            now,
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_job")

                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'SYNC_RUN_WAKE', 'SYNC_RUN',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        """,
                        (
                            outbox_id,
                            access.workspace_id,
                            run_uuid,
                            f"sync_run_wake:{run_uuid}:v{next_version}",
                            json.dumps(
                                {
                                    "sync_run_id": run_uuid,
                                    "job_id": job_id,
                                    "run_version": next_version,
                                },
                                sort_keys=True,
                            ),
                            now,
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_outbox")

                    receipt = AcceptedSyncRunReceipt(
                        target_id=run_uuid,
                        status_url=f"/v1/sync-runs/{run_uuid}",
                        job_id=job_id,
                    )
                    expires_at = now + IDEMPOTENCY_WINDOW
                    try:
                        with conn.transaction():
                            cur.execute(
                                """
                                INSERT INTO public.api_idempotency_records (
                                    workspace_id, operation_scope, idempotency_key,
                                    request_fingerprint, target_sync_run_id,
                                    response_status, response_body, created_at, expires_at
                                )
                                VALUES (
                                    %s::uuid, 'SYNC_RUN_RETRY', %s,
                                    %s, %s::uuid,
                                    202, %s::jsonb, %s, %s
                                )
                                """,
                                (
                                    access.workspace_id,
                                    normalized_key,
                                    fingerprint,
                                    run_uuid,
                                    json.dumps(receipt.to_contract_dict(), sort_keys=True),
                                    now,
                                    expires_at,
                                ),
                            )
                    except psycopg.errors.UniqueViolation as exc:
                        raise SyncRunApiError(
                            status_code=409, code="IDEMPOTENCY_CONFLICT"
                        ) from exc
                    self._maybe_inject_fault("after_idempotency")

                    audit_meta: dict[str, Any] = {
                        "expected_run_version": expected_run_version,
                        "new_run_version": next_version,
                        "job_id": job_id,
                    }
                    if request_id:
                        audit_meta["request_id"] = request_id
                    cur.execute(
                        """
                        INSERT INTO public.audit_events (
                            id, workspace_id, actor_type, actor_id,
                            action, target_type, target_id, redacted_metadata, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, %s::uuid,
                            'SYNC_RUN_RETRIED', 'SYNC_RUN', %s::uuid, %s::jsonb, %s
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            access.workspace_id,
                            AuditActorType.USER.value,
                            access.user_id,
                            run_uuid,
                            json.dumps(audit_meta, sort_keys=True),
                            now,
                        ),
                    )
                    self._maybe_inject_fault("after_audit")

                    wake_signal = WakeSignal(
                        workspace_id=access.workspace_id,
                        job_id=job_id,
                        job_type="INITIAL_BACKFILL_SYNC",
                        target_sync_run_id=run_uuid,
                        outbox_event_id=outbox_id,
                    )

        if wake_signal is not None:
            self._notify_post_commit(wake_signal)
        return receipt

    def record_worker_observation(
        self,
        workspace_id: str,
        run_id: str,
        observation: SyncRunObservation,
        *,
        now: datetime | None = None,
    ) -> SyncRunStatusSnapshot:
        """Apply a worker/provider state transition and checkpoints (runs under worker role)."""
        ts = now or self._clock()
        run_uuid = _try_normalize_uuid(run_id)
        if run_uuid is None:
            raise ValueError("run_id must be a valid UUID.")

        import_ready = (
            observation.import_ready
            if observation.import_ready is not None
            else observation.status == SyncRunStatus.SUCCESS
        )
        processing_status = (
            observation.processing_status
            if observation.status == SyncRunStatus.SUCCESS
            else ProcessingStatus.NOT_STARTED
        )

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_SYNC_RUN_COLUMNS}
                        FROM public.sync_runs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise ValueError(f"SyncRun {run_uuid} not found.")
                    if bool(existing[16]):
                        raise ValueError("Cannot update an abandoned SyncRun.")

                    existing_terminal_at = existing[17]
                    if observation.status in ACTIVE_SYNC_RUN_STATUSES:
                        new_terminal_at = None
                    elif existing_terminal_at is None:
                        new_terminal_at = ts
                    else:
                        new_terminal_at = existing_terminal_at

                    cur.execute(
                        f"""
                        UPDATE public.sync_runs
                        SET status = %s,
                            processing_status = %s,
                            import_ready = %s,
                            retry_allowed = %s,
                            failure_reason_code = %s,
                            has_safe_checkpoint = %s,
                            source_drift = %s,
                            observed_at = COALESCE(%s, observed_at),
                            processed_range = COALESCE(%s, processed_range),
                            remaining_range = COALESCE(%s, remaining_range),
                            run_version = run_version + 1,
                            last_terminal_transition_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        RETURNING {_SYNC_RUN_COLUMNS}
                        """,
                        (
                            observation.status.value,
                            processing_status.value,
                            import_ready,
                            observation.retry_allowed,
                            observation.failure_reason,
                            observation.has_safe_checkpoint,
                            observation.source_drift,
                            observation.observed_at,
                            observation.processed_range,
                            observation.remaining_range,
                            new_terminal_at,
                            ts,
                            ws_uuid,
                            run_uuid,
                        ),
                    )
                    updated = cur.fetchone()
                    assert updated is not None

                    if observation.streams is not None:
                        for stream in observation.streams:
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
                                    1, '{}'::jsonb, %s
                                )
                                ON CONFLICT (sync_run_id, stream_name)
                                DO UPDATE SET
                                    stream_status = EXCLUDED.stream_status,
                                    last_committed_page = EXCLUDED.last_committed_page,
                                    unresolved_failures = EXCLUDED.unresolved_failures,
                                    checkpoint_version = public.sync_checkpoints.checkpoint_version + 1,
                                    updated_at = EXCLUDED.updated_at
                                """,
                                (
                                    str(uuid.uuid4()),
                                    ws_uuid,
                                    run_uuid,
                                    stream.type,
                                    stream.status,
                                    stream.last_committed_page,
                                    stream.unresolved_failures,
                                    ts,
                                ),
                            )

                    streams = _load_streams_for_run(cur, ws_uuid, run_uuid)
                    return _row_to_snapshot(updated, streams)
