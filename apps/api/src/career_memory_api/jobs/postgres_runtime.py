"""PostgreSQL 18 durable Job, Lease, Poison-Isolation, Replay, Outbox, and Deletion Fence runtime."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from career_memory_api.jobs.models import (
    AuditActorType,
    BackoffPolicy,
    ClaimedJob,
    DeletionFenceActiveError,
    DeletionJobRecord,
    DeletionJobStatus,
    JobErrorClass,
    JobNotReplayableError,
    JobRecord,
    JobStatus,
    OutboxEventRecord,
    OutboxPublisherPort,
    OutboxStatus,
    PostCommitWakeNotifierPort,
    StaleJobLeaseError,
    WakeSignal,
)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def bind_worker_workspace_context(cur: psycopg.Cursor, workspace_id: str) -> str:
    """Bind transaction-local ``app.current_workspace_id`` for worker FORCE RLS queries."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    cur.execute(
        "SELECT set_config('app.current_workspace_id', %s, true)",
        (ws_uuid,),
    )
    return ws_uuid


def is_deletion_fence_active(
    cur: psycopg.Cursor, workspace_id: str, source_resource_id: str
) -> bool:
    """Return True when the repository is DELETED or has an active/completed deletion job."""
    cur.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM public.source_resources sr
                WHERE sr.workspace_id = %s::uuid
                  AND sr.id = %s::uuid
                  AND sr.status = 'DELETED'
            )
            OR EXISTS (
                SELECT 1
                FROM public.deletion_jobs dj
                WHERE dj.workspace_id = %s::uuid
                  AND dj.source_resource_id = %s::uuid
                  AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
            )
        """,
        (workspace_id, source_resource_id, workspace_id, source_resource_id),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _row_to_job(row: tuple[Any, ...]) -> JobRecord:
    return JobRecord(
        id=row[0],
        workspace_id=row[1],
        job_type=row[2],
        target_sync_run_id=row[3],
        dedupe_key=row[4],
        input_hash=row[5],
        input_manifest=dict(row[6] or {}),
        status=JobStatus(row[7]),
        attempt=int(row[8]),
        max_attempts=int(row[9]),
        lease_owner=row[10],
        lease_token=row[11],
        lease_until=row[12],
        next_run_at=row[13],
        checkpoint=dict(row[14] or {}),
        error_class=JobErrorClass(row[15]) if row[15] is not None else None,
        error_code=row[16],
        execution_parent_id=row[17],
        logical_effect_key=row[18],
        created_at=row[19],
        updated_at=row[20],
        completed_at=row[21],
    )


_JOB_SELECT_COLUMNS = """
    id::text,
    workspace_id::text,
    job_type,
    target_sync_run_id::text,
    dedupe_key,
    input_hash,
    input_manifest,
    status,
    attempt,
    max_attempts,
    lease_owner,
    lease_token::text,
    lease_until,
    next_run_at,
    checkpoint,
    error_class,
    error_code,
    execution_parent_id::text,
    logical_effect_key,
    created_at,
    updated_at,
    completed_at
"""


def _row_to_outbox(row: tuple[Any, ...]) -> OutboxEventRecord:
    return OutboxEventRecord(
        id=row[0],
        workspace_id=row[1],
        event_type=row[2],
        aggregate_type=row[3],
        aggregate_id=row[4],
        dedupe_key=row[5],
        payload=dict(row[6] or {}),
        status=OutboxStatus(row[7]),
        attempt=int(row[8]),
        max_attempts=int(row[9]),
        next_attempt_at=row[10],
        last_error_code=row[11],
        created_at=row[12],
        published_at=row[13],
    )


_OUTBOX_SELECT_COLUMNS = """
    id::text,
    workspace_id::text,
    event_type,
    aggregate_type,
    aggregate_id::text,
    dedupe_key,
    payload,
    status,
    attempt,
    max_attempts,
    next_attempt_at,
    last_error_code,
    created_at,
    published_at
"""


def _insert_audit_event(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    actor_type: AuditActorType,
    actor_id: str | None,
    action: str,
    target_type: str,
    target_id: str,
    redacted_metadata: dict[str, Any],
    created_at: datetime,
) -> None:
    cur.execute(
        """
        INSERT INTO public.audit_events (
            id, workspace_id, actor_type, actor_id,
            action, target_type, target_id, redacted_metadata, created_at
        )
        VALUES (%s::uuid, %s::uuid, %s, %s::uuid, %s, %s, %s::uuid, %s::jsonb, %s)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            actor_type.value,
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(redacted_metadata, sort_keys=True),
            created_at,
        ),
    )


def _lookup_sync_run_resource(
    cur: psycopg.Cursor, workspace_id: str, sync_run_id: str | None
) -> str | None:
    if sync_run_id is None:
        return None
    cur.execute(
        """
        SELECT source_resource_id::text
        FROM public.sync_runs
        WHERE workspace_id = %s::uuid
          AND id = %s::uuid
        """,
        (workspace_id, sync_run_id),
    )
    row = cur.fetchone()
    return str(row[0]) if row is not None else None


def _cancel_job_and_sync_run_for_fence(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    job_id: str,
    sync_run_id: str | None,
    now: datetime,
) -> None:
    cur.execute(
        """
        UPDATE public.jobs
        SET status = 'CANCELED',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            error_class = 'CANCELED_BY_FENCE',
            error_code = 'DELETION_FENCE_ACTIVE',
            updated_at = %s,
            completed_at = %s
        WHERE workspace_id = %s::uuid
          AND id = %s::uuid
          AND status IN ('PENDING', 'RUNNING', 'RETRY_WAIT')
        """,
        (now, now, workspace_id, job_id),
    )
    if sync_run_id is not None:
        cur.execute(
            """
            UPDATE public.sync_runs
            SET status = 'CANCELED',
                import_ready = false,
                processing_status = 'NOT_STARTED',
                retry_allowed = false,
                failure_reason_code = NULL,
                source_drift = false,
                run_version = run_version + 1,
                last_terminal_transition_at = %s,
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
              AND status IN ('PENDING', 'RUNNING')
            """,
            (now, now, workspace_id, sync_run_id),
        )
    _insert_audit_event(
        cur,
        workspace_id=workspace_id,
        actor_type=AuditActorType.WORKER,
        actor_id=None,
        action="JOB_CANCELED_BY_DELETION_FENCE",
        target_type="JOB",
        target_id=job_id,
        redacted_metadata={"reason": "DELETION_FENCE_ACTIVE"},
        created_at=now,
    )


class PostgresJobRuntime:
    """Workspace-scoped PostgreSQL 18 runtime implementing T-105A (REL-02)."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory,
        default_backoff: BackoffPolicy | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._default_backoff = default_backoff or BackoffPolicy()

    def claim_next_job(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=60),
        now: datetime | None = None,
    ) -> ClaimedJob | None:
        """Atomically claim the next ready job or reclaim an expired lease with SKIP LOCKED."""
        ts = now or datetime.now(UTC)
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string.")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")

        with self._connection_factory() as conn:
            while True:
                with conn.transaction():
                    with conn.cursor() as cur:
                        ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                        cur.execute(
                            f"""
                            SELECT {_JOB_SELECT_COLUMNS}
                            FROM public.jobs
                            WHERE workspace_id = %s::uuid
                              AND (
                                  (status IN ('PENDING', 'RETRY_WAIT') AND next_run_at <= %s)
                                  OR (status = 'RUNNING' AND lease_until <= %s)
                              )
                            ORDER BY
                                CASE WHEN status = 'RUNNING' THEN 0 ELSE 1 END ASC,
                                next_run_at ASC,
                                created_at ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                            """,
                            (ws_uuid, ts, ts),
                        )
                        row = cur.fetchone()
                        if row is None:
                            return None

                        candidate = _row_to_job(row)

                        # Check deletion fence before leasing
                        resource_id = _lookup_sync_run_resource(
                            cur, ws_uuid, candidate.target_sync_run_id
                        )
                        if resource_id is not None and is_deletion_fence_active(
                            cur, ws_uuid, resource_id
                        ):
                            _cancel_job_and_sync_run_for_fence(
                                cur,
                                workspace_id=ws_uuid,
                                job_id=candidate.id,
                                sync_run_id=candidate.target_sync_run_id,
                                now=ts,
                            )
                            continue

                        # If reclaiming an expired lease with no attempts remaining -> FAILED_TERMINAL
                        if candidate.attempt >= candidate.max_attempts:
                            cur.execute(
                                """
                                UPDATE public.jobs
                                SET status = 'FAILED_TERMINAL',
                                    lease_owner = NULL,
                                    lease_token = NULL,
                                    lease_until = NULL,
                                    error_class = 'POISON_BUDGET_EXHAUSTED',
                                    error_code = 'LEASE_EXPIRED_BUDGET_EXHAUSTED',
                                    updated_at = %s,
                                    completed_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                """,
                                (ts, ts, ws_uuid, candidate.id),
                            )
                            _insert_audit_event(
                                cur,
                                workspace_id=ws_uuid,
                                actor_type=AuditActorType.WORKER,
                                actor_id=None,
                                action="JOB_POISON_ISOLATED",
                                target_type="JOB",
                                target_id=candidate.id,
                                redacted_metadata={
                                    "error_class": "POISON_BUDGET_EXHAUSTED",
                                    "error_code": "LEASE_EXPIRED_BUDGET_EXHAUSTED",
                                    "attempt": candidate.attempt,
                                },
                                created_at=ts,
                            )
                            continue

                        new_token = str(uuid.uuid4())
                        lease_until = ts + lease_duration
                        cur.execute(
                            f"""
                            UPDATE public.jobs
                            SET status = 'RUNNING',
                                attempt = attempt + 1,
                                lease_owner = %s,
                                lease_token = %s::uuid,
                                lease_until = %s,
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            RETURNING {_JOB_SELECT_COLUMNS}
                            """,
                            (
                                worker_id.strip(),
                                new_token,
                                lease_until,
                                ts,
                                ws_uuid,
                                candidate.id,
                            ),
                        )
                        updated_row = cur.fetchone()
                        assert updated_row is not None
                        claimed_job = _row_to_job(updated_row)

                        if claimed_job.target_sync_run_id is not None:
                            cur.execute(
                                """
                                UPDATE public.sync_runs
                                SET status = 'RUNNING',
                                    started_at = COALESCE(started_at, %s),
                                    updated_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                  AND status = 'PENDING'
                                """,
                                (ts, ts, ws_uuid, claimed_job.target_sync_run_id),
                            )

                        return ClaimedJob(
                            job=claimed_job,
                            lease_token=new_token,
                            lease_until=lease_until,
                        )

    def reclaim_expired_leases(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
        backoff_policy: BackoffPolicy | None = None,
    ) -> list[JobRecord]:
        """Sweep expired RUNNING leases into RETRY_WAIT or FAILED_TERMINAL."""
        ts = now or datetime.now(UTC)
        policy = backoff_policy or self._default_backoff
        reclaimed: list[JobRecord] = []

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_JOB_SELECT_COLUMNS}
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND status = 'RUNNING'
                          AND lease_until <= %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        (ws_uuid, ts),
                    )
                    rows = cur.fetchall()
                    for row in rows:
                        job = _row_to_job(row)
                        if job.attempt >= job.max_attempts:
                            cur.execute(
                                f"""
                                UPDATE public.jobs
                                SET status = 'FAILED_TERMINAL',
                                    lease_owner = NULL,
                                    lease_token = NULL,
                                    lease_until = NULL,
                                    error_class = 'POISON_BUDGET_EXHAUSTED',
                                    error_code = 'LEASE_EXPIRED_BUDGET_EXHAUSTED',
                                    updated_at = %s,
                                    completed_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                RETURNING {_JOB_SELECT_COLUMNS}
                                """,
                                (ts, ts, ws_uuid, job.id),
                            )
                        else:
                            next_run_at = ts + policy.delay_for_attempt(max(job.attempt, 1))
                            cur.execute(
                                f"""
                                UPDATE public.jobs
                                SET status = 'RETRY_WAIT',
                                    lease_owner = NULL,
                                    lease_token = NULL,
                                    lease_until = NULL,
                                    next_run_at = %s,
                                    error_class = 'RETRYABLE',
                                    error_code = 'LEASE_EXPIRED',
                                    updated_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                RETURNING {_JOB_SELECT_COLUMNS}
                                """,
                                (next_run_at, ts, ws_uuid, job.id),
                            )
                        updated = cur.fetchone()
                        assert updated is not None
                        reclaimed.append(_row_to_job(updated))
        return reclaimed

    def heartbeat_and_checkpoint(
        self,
        workspace_id: str,
        *,
        job_id: str,
        lease_token: str,
        checkpoint: dict[str, Any],
        lease_extension: timedelta = timedelta(seconds=60),
        now: datetime | None = None,
    ) -> JobRecord:
        """Persist checkpoint and extend lease strictly if fencing token and expiry match."""
        ts = now or datetime.now(UTC)
        job_uuid = _normalize_uuid(job_id, "job_id")
        token_uuid = _normalize_uuid(lease_token, "lease_token")

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_JOB_SELECT_COLUMNS}
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'RUNNING'
                          AND lease_token = %s::uuid
                          AND lease_until > %s
                        FOR UPDATE
                        """,
                        (ws_uuid, job_uuid, token_uuid, ts),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise StaleJobLeaseError(
                            f"Lease token is stale or expired for job {job_uuid}."
                        )
                    job = _row_to_job(row)

                    resource_id = _lookup_sync_run_resource(
                        cur, ws_uuid, job.target_sync_run_id
                    )
                    if resource_id is not None and is_deletion_fence_active(
                        cur, ws_uuid, resource_id
                    ):
                        _cancel_job_and_sync_run_for_fence(
                            cur,
                            workspace_id=ws_uuid,
                            job_id=job.id,
                            sync_run_id=job.target_sync_run_id,
                            now=ts,
                        )
                        fenced = True
                    else:
                        fenced = False

                    if not fenced:
                        cur.execute(
                            f"""
                            UPDATE public.jobs
                            SET checkpoint = %s::jsonb,
                                lease_until = %s,
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status = 'RUNNING'
                              AND lease_token = %s::uuid
                            RETURNING {_JOB_SELECT_COLUMNS}
                            """,
                            (
                                json.dumps(checkpoint, sort_keys=True),
                                ts + lease_extension,
                                ts,
                                ws_uuid,
                                job_uuid,
                                token_uuid,
                            ),
                        )
                        updated = cur.fetchone()
                        assert updated is not None
                        return _row_to_job(updated)

        raise DeletionFenceActiveError(
            f"Deletion fence canceled job {job_uuid} during checkpoint."
        )

    def complete_job_with_effect(
        self,
        workspace_id: str,
        *,
        job_id: str,
        lease_token: str,
        effect_metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
        sync_run_success: bool = False,
        observed_at: datetime | None = None,
        processed_range: str | None = None,
    ) -> tuple[JobRecord, bool]:
        """Atomically verify lease + deletion fence, record 1-time logical effect, and mark SUCCEEDED.

        Returns ``(job_record, effect_applied)`` where ``effect_applied`` is ``True``
        iff this execution committed the logical effect in ``job_side_effect_ledger``.
        """
        ts = now or datetime.now(UTC)
        job_uuid = _normalize_uuid(job_id, "job_id")
        token_uuid = _normalize_uuid(lease_token, "lease_token")
        meta = dict(effect_metadata or {})
        fenced = False

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_JOB_SELECT_COLUMNS}
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'RUNNING'
                          AND lease_token = %s::uuid
                          AND lease_until > %s
                        FOR UPDATE
                        """,
                        (ws_uuid, job_uuid, token_uuid, ts),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise StaleJobLeaseError(
                            f"Lease token is stale or expired for job {job_uuid}."
                        )
                    job = _row_to_job(row)

                    resource_id = _lookup_sync_run_resource(
                        cur, ws_uuid, job.target_sync_run_id
                    )
                    if resource_id is not None and is_deletion_fence_active(
                        cur, ws_uuid, resource_id
                    ):
                        _cancel_job_and_sync_run_for_fence(
                            cur,
                            workspace_id=ws_uuid,
                            job_id=job.id,
                            sync_run_id=job.target_sync_run_id,
                            now=ts,
                        )
                        fenced = True

                    if not fenced:
                        cur.execute(
                            """
                            INSERT INTO public.job_side_effect_ledger (
                                workspace_id, logical_effect_key, job_id, effect_metadata, committed_at
                            )
                            VALUES (%s::uuid, %s, %s::uuid, %s::jsonb, %s)
                            ON CONFLICT (workspace_id, logical_effect_key) DO NOTHING
                            RETURNING logical_effect_key
                            """,
                            (
                                ws_uuid,
                                job.logical_effect_key,
                                job.id,
                                json.dumps(meta, sort_keys=True),
                                ts,
                            ),
                        )
                        effect_applied = cur.fetchone() is not None

                        cur.execute(
                            f"""
                            UPDATE public.jobs
                            SET status = 'SUCCEEDED',
                                lease_owner = NULL,
                                lease_token = NULL,
                                lease_until = NULL,
                                updated_at = %s,
                                completed_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                              AND status = 'RUNNING'
                              AND lease_token = %s::uuid
                            RETURNING {_JOB_SELECT_COLUMNS}
                            """,
                            (ts, ts, ws_uuid, job.id, token_uuid),
                        )
                        updated_row = cur.fetchone()
                        assert updated_row is not None
                        completed_job = _row_to_job(updated_row)

                        if sync_run_success and job.target_sync_run_id is not None:
                            cur.execute(
                                """
                                UPDATE public.sync_runs
                                SET status = 'SUCCESS',
                                    import_ready = true,
                                    processing_status = 'NOT_STARTED',
                                    retry_allowed = false,
                                    failure_reason_code = NULL,
                                    source_drift = false,
                                    observed_at = COALESCE(%s, observed_at),
                                    processed_range = COALESCE(%s, processed_range),
                                    run_version = run_version + 1,
                                    last_terminal_transition_at = %s,
                                    updated_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                """,
                                (
                                    observed_at or ts,
                                    processed_range,
                                    ts,
                                    ts,
                                    ws_uuid,
                                    job.target_sync_run_id,
                                ),
                            )
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
                                ON CONFLICT DO NOTHING
                                """,
                                (
                                    str(uuid.uuid4()),
                                    ws_uuid,
                                    job.target_sync_run_id,
                                    f"import_ready:{job.target_sync_run_id}",
                                    json.dumps(
                                        {
                                            "sync_run_id": job.target_sync_run_id,
                                            "job_id": job.id,
                                        },
                                        sort_keys=True,
                                    ),
                                    ts,
                                    ts,
                                ),
                            )

                        _insert_audit_event(
                            cur,
                            workspace_id=ws_uuid,
                            actor_type=AuditActorType.WORKER,
                            actor_id=None,
                            action="JOB_SUCCEEDED",
                            target_type="JOB",
                            target_id=job.id,
                            redacted_metadata={
                                "attempt": completed_job.attempt,
                                "effect_applied": effect_applied,
                            },
                            created_at=ts,
                        )
                        return completed_job, effect_applied

        raise DeletionFenceActiveError(
            f"Deletion fence canceled job {job_uuid} prior to commit."
        )

    def fail_job(
        self,
        workspace_id: str,
        *,
        job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
        backoff_policy: BackoffPolicy | None = None,
    ) -> JobRecord:
        """Record a retryable backoff (RETRY_WAIT) or terminal poison failure (FAILED_TERMINAL)."""
        ts = now or datetime.now(UTC)
        policy = backoff_policy or self._default_backoff
        job_uuid = _normalize_uuid(job_id, "job_id")
        token_uuid = _normalize_uuid(lease_token, "lease_token")
        clean_error_code = error_code.strip()
        if not clean_error_code:
            raise ValueError("error_code must be non-empty.")

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_JOB_SELECT_COLUMNS}
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'RUNNING'
                          AND lease_token = %s::uuid
                          AND lease_until > %s
                        FOR UPDATE
                        """,
                        (ws_uuid, job_uuid, token_uuid, ts),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise StaleJobLeaseError(
                            f"Lease token is stale or expired for job {job_uuid}."
                        )
                    job = _row_to_job(row)

                    if not retryable:
                        new_status = JobStatus.FAILED_TERMINAL
                        error_class = JobErrorClass.NON_RETRYABLE
                        next_run_at = ts
                        completed_at: datetime | None = ts
                        audit_action = "JOB_FAILED_TERMINAL"
                    elif job.attempt < job.max_attempts:
                        new_status = JobStatus.RETRY_WAIT
                        error_class = JobErrorClass.RETRYABLE
                        next_run_at = ts + policy.delay_for_attempt(max(job.attempt, 1))
                        completed_at = None
                        audit_action = "JOB_RETRY_SCHEDULED"
                    else:
                        new_status = JobStatus.FAILED_TERMINAL
                        error_class = JobErrorClass.POISON_BUDGET_EXHAUSTED
                        next_run_at = ts
                        completed_at = ts
                        audit_action = "JOB_POISON_ISOLATED"

                    cur.execute(
                        f"""
                        UPDATE public.jobs
                        SET status = %s,
                            lease_owner = NULL,
                            lease_token = NULL,
                            lease_until = NULL,
                            next_run_at = %s,
                            error_class = %s,
                            error_code = %s,
                            updated_at = %s,
                            completed_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'RUNNING'
                          AND lease_token = %s::uuid
                        RETURNING {_JOB_SELECT_COLUMNS}
                        """,
                        (
                            new_status.value,
                            next_run_at,
                            error_class.value,
                            clean_error_code,
                            ts,
                            completed_at,
                            ws_uuid,
                            job.id,
                            token_uuid,
                        ),
                    )
                    updated_row = cur.fetchone()
                    assert updated_row is not None
                    updated_job = _row_to_job(updated_row)

                    _insert_audit_event(
                        cur,
                        workspace_id=ws_uuid,
                        actor_type=AuditActorType.WORKER,
                        actor_id=None,
                        action=audit_action,
                        target_type="JOB",
                        target_id=job.id,
                        redacted_metadata={
                            "attempt": updated_job.attempt,
                            "max_attempts": updated_job.max_attempts,
                            "error_class": error_class.value,
                            "error_code": clean_error_code,
                        },
                        created_at=ts,
                    )
                    return updated_job

    def replay_terminal_job(
        self,
        workspace_id: str,
        *,
        parent_job_id: str,
        operator_actor_id: str | None = None,
        max_attempts: int | None = None,
        now: datetime | None = None,
        wake_notifier: PostCommitWakeNotifierPort | None = None,
    ) -> JobRecord:
        """Create a new PENDING child job linked via execution_parent_id while preserving the parent."""
        ts = now or datetime.now(UTC)
        parent_uuid = _normalize_uuid(parent_job_id, "parent_job_id")
        actor_uuid = (
            _normalize_uuid(operator_actor_id, "operator_actor_id")
            if operator_actor_id is not None
            else None
        )

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_JOB_SELECT_COLUMNS}
                        FROM public.jobs
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, parent_uuid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise JobNotReplayableError(f"Job {parent_uuid} was not found.")
                    parent = _row_to_job(row)
                    if parent.status != JobStatus.FAILED_TERMINAL:
                        raise JobNotReplayableError(
                            f"Only FAILED_TERMINAL jobs can be replayed (got {parent.status})."
                        )

                    resource_id = _lookup_sync_run_resource(
                        cur, ws_uuid, parent.target_sync_run_id
                    )
                    if resource_id is not None and is_deletion_fence_active(
                        cur, ws_uuid, resource_id
                    ):
                        raise DeletionFenceActiveError(
                            f"Cannot replay job {parent.id}: deletion fence is active."
                        )

                    child_id = str(uuid.uuid4())
                    outbox_id = str(uuid.uuid4())
                    child_max_attempts = (
                        max_attempts if max_attempts is not None else parent.max_attempts
                    )
                    try:
                        cur.execute(
                            f"""
                            INSERT INTO public.jobs (
                                id, workspace_id, job_type, target_sync_run_id,
                                dedupe_key, input_hash, input_manifest, status,
                                attempt, max_attempts, next_run_at, checkpoint,
                                execution_parent_id, logical_effect_key,
                                created_at, updated_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s, %s::uuid,
                                %s, %s, %s::jsonb, 'PENDING',
                                0, %s, %s, %s::jsonb,
                                %s::uuid, %s,
                                %s, %s
                            )
                            RETURNING {_JOB_SELECT_COLUMNS}
                            """,
                            (
                                child_id,
                                ws_uuid,
                                parent.job_type,
                                parent.target_sync_run_id,
                                f"replay:{parent.id}:{child_id}",
                                parent.input_hash,
                                json.dumps(parent.input_manifest, sort_keys=True),
                                child_max_attempts,
                                ts,
                                json.dumps(parent.checkpoint, sort_keys=True),
                                parent.id,
                                parent.logical_effect_key,
                                ts,
                                ts,
                            ),
                        )
                    except psycopg.errors.UniqueViolation as exc:
                        raise JobNotReplayableError(
                            f"Logical effect {parent.logical_effect_key} already has an active or succeeded execution."
                        ) from exc

                    child_row = cur.fetchone()
                    assert child_row is not None
                    child_job = _row_to_job(child_row)

                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'JOB_REPLAY_WAKE', 'JOB',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        """,
                        (
                            outbox_id,
                            ws_uuid,
                            child_job.id,
                            f"job_replay_wake:{child_job.id}",
                            json.dumps(
                                {
                                    "job_id": child_job.id,
                                    "execution_parent_id": parent.id,
                                },
                                sort_keys=True,
                            ),
                            ts,
                            ts,
                        ),
                    )
                    _insert_audit_event(
                        cur,
                        workspace_id=ws_uuid,
                        actor_type=AuditActorType.OPERATOR,
                        actor_id=actor_uuid,
                        action="JOB_REPLAYED",
                        target_type="JOB",
                        target_id=child_job.id,
                        redacted_metadata={
                            "execution_parent_id": parent.id,
                            "logical_effect_key": parent.logical_effect_key,
                        },
                        created_at=ts,
                    )

        if wake_notifier is not None:
            try:
                wake_notifier.notify_job_available(
                    WakeSignal(
                        workspace_id=child_job.workspace_id,
                        job_id=child_job.id,
                        job_type=child_job.job_type,
                        target_sync_run_id=child_job.target_sync_run_id,
                        outbox_event_id=outbox_id,
                    )
                )
            except Exception:  # noqa: BLE001
                # Wake failures never invalidate committed PENDING replay work.
                pass

        return child_job

    def request_source_deletion(
        self,
        workspace_id: str,
        *,
        source_resource_id: str,
        requested_by: str | None = None,
        now: datetime | None = None,
    ) -> DeletionJobRecord:
        """Create a deletion_jobs fence and cancel queued (PENDING/RETRY_WAIT) work for the repo."""
        ts = now or datetime.now(UTC)
        resource_uuid = _normalize_uuid(source_resource_id, "source_resource_id")
        requester_uuid = (
            _normalize_uuid(requested_by, "requested_by")
            if requested_by is not None
            else None
        )
        del_id = str(uuid.uuid4())

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        INSERT INTO public.deletion_jobs (
                            id, workspace_id, source_resource_id, status,
                            checkpoint, failure_summary, requested_by, requested_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PENDING',
                            '{}'::jsonb, '{}'::jsonb, %s::uuid, %s
                        )
                        RETURNING
                            id::text, workspace_id::text, source_resource_id::text,
                            status, checkpoint, failure_summary, requested_by::text,
                            requested_at, completed_at
                        """,
                        (del_id, ws_uuid, resource_uuid, requester_uuid, ts),
                    )
                    row = cur.fetchone()
                    assert row is not None

                    # Cancel any non-running queued jobs for this repository immediately.
                    cur.execute(
                        """
                        UPDATE public.jobs j
                        SET status = 'CANCELED',
                            error_class = 'CANCELED_BY_FENCE',
                            error_code = 'DELETION_FENCE_ACTIVE',
                            updated_at = %s,
                            completed_at = %s
                        FROM public.sync_runs sr
                        WHERE j.workspace_id = %s::uuid
                          AND sr.workspace_id = j.workspace_id
                          AND sr.id = j.target_sync_run_id
                          AND sr.source_resource_id = %s::uuid
                          AND j.status IN ('PENDING', 'RETRY_WAIT')
                        """,
                        (ts, ts, ws_uuid, resource_uuid),
                    )
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET status = 'CANCELED',
                            import_ready = false,
                            processing_status = 'NOT_STARTED',
                            retry_allowed = false,
                            failure_reason_code = NULL,
                            source_drift = false,
                            run_version = run_version + 1,
                            last_terminal_transition_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND source_resource_id = %s::uuid
                          AND status = 'PENDING'
                        """,
                        (ts, ts, ws_uuid, resource_uuid),
                    )
                    _insert_audit_event(
                        cur,
                        workspace_id=ws_uuid,
                        actor_type=AuditActorType.USER if requester_uuid else AuditActorType.SYSTEM,
                        actor_id=requester_uuid,
                        action="SOURCE_DELETION_REQUESTED",
                        target_type="SOURCE_RESOURCE",
                        target_id=resource_uuid,
                        redacted_metadata={"deletion_job_id": del_id},
                        created_at=ts,
                    )

                    return DeletionJobRecord(
                        id=row[0],
                        workspace_id=row[1],
                        source_resource_id=row[2],
                        status=DeletionJobStatus(row[3]),
                        checkpoint=dict(row[4] or {}),
                        failure_summary=dict(row[5] or {}),
                        requested_by=row[6],
                        requested_at=row[7],
                        completed_at=row[8],
                    )

    def dispatch_pending_outbox(
        self,
        workspace_id: str,
        *,
        publisher: OutboxPublisherPort,
        limit: int = 20,
        now: datetime | None = None,
        backoff_policy: BackoffPolicy | None = None,
    ) -> list[OutboxEventRecord]:
        """Deliver due PENDING outbox events with retry backoff and terminal poison isolation."""
        ts = now or datetime.now(UTC)
        policy = backoff_policy or self._default_backoff
        processed: list[OutboxEventRecord] = []

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        f"""
                        SELECT {_OUTBOX_SELECT_COLUMNS}
                        FROM public.outbox_events
                        WHERE workspace_id = %s::uuid
                          AND status = 'PENDING'
                          AND next_attempt_at <= %s
                        ORDER BY next_attempt_at ASC, created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                        """,
                        (ws_uuid, ts, limit),
                    )
                    rows = cur.fetchall()

                    for row in rows:
                        event = _row_to_outbox(row)
                        try:
                            publisher.publish(event)
                        except Exception as exc:  # noqa: BLE001
                            next_attempt = event.attempt + 1
                            error_code = type(exc).__name__
                            if next_attempt >= event.max_attempts:
                                cur.execute(
                                    f"""
                                    UPDATE public.outbox_events
                                    SET status = 'FAILED_TERMINAL',
                                        attempt = %s,
                                        last_error_code = %s
                                    WHERE workspace_id = %s::uuid
                                      AND id = %s::uuid
                                    RETURNING {_OUTBOX_SELECT_COLUMNS}
                                    """,
                                    (next_attempt, error_code, ws_uuid, event.id),
                                )
                            else:
                                cur.execute(
                                    f"""
                                    UPDATE public.outbox_events
                                    SET attempt = %s,
                                        next_attempt_at = %s,
                                        last_error_code = %s
                                    WHERE workspace_id = %s::uuid
                                      AND id = %s::uuid
                                    RETURNING {_OUTBOX_SELECT_COLUMNS}
                                    """,
                                    (
                                        next_attempt,
                                        ts + policy.delay_for_attempt(next_attempt),
                                        error_code,
                                        ws_uuid,
                                        event.id,
                                    ),
                                )
                        else:
                            cur.execute(
                                f"""
                                UPDATE public.outbox_events
                                SET status = 'PUBLISHED',
                                    attempt = attempt + 1,
                                    published_at = %s,
                                    last_error_code = NULL
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                RETURNING {_OUTBOX_SELECT_COLUMNS}
                                """,
                                (ts, ws_uuid, event.id),
                            )
                        updated = cur.fetchone()
                        assert updated is not None
                        processed.append(_row_to_outbox(updated))
        return processed

    def consume_outbox_event_once(
        self,
        workspace_id: str,
        *,
        consumer_name: str,
        event: OutboxEventRecord,
        on_effect: Callable[[OutboxEventRecord], None],
        now: datetime | None = None,
    ) -> bool:
        """Execute ``on_effect`` strictly once per ``(workspace_id, consumer_name, dedupe_key)``."""
        ts = now or datetime.now(UTC)
        clean_consumer = consumer_name.strip()
        if not clean_consumer:
            raise ValueError("consumer_name must be non-empty.")

        with self._connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    ws_uuid = bind_worker_workspace_context(cur, workspace_id)
                    cur.execute(
                        """
                        INSERT INTO public.outbox_consumed_effects (
                            workspace_id, consumer_name, dedupe_key, outbox_event_id, consumed_at
                        )
                        VALUES (%s::uuid, %s, %s, %s::uuid, %s)
                        ON CONFLICT (workspace_id, consumer_name, dedupe_key) DO NOTHING
                        RETURNING dedupe_key
                        """,
                        (ws_uuid, clean_consumer, event.dedupe_key, event.id, ts),
                    )
                    if cur.fetchone() is None:
                        return False
                    on_effect(event)
                    return True
