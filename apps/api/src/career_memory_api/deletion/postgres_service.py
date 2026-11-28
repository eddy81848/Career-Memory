"""PostgreSQL 18 Source Deletion Workflow, Pre-Commit Fence & 5-Component Receipt Worker
(OD-003 / T-502 / T-502A / DELETE-01..07).

Guarantees:
1. Live `preview_source_deletion`: computes exact counts across all 10 categories plus
   deterministic `resource_version` and `impact_scope_version` signed into `preview_token`.
2. Atomic `confirm_source_deletion`: locks `source_resources FOR NO KEY UPDATE`, enforces 24h immutable
   `api_idempotency_records` (`CONFIRM_SOURCE_DELETION`), guarantees single active `deletion_jobs`
   per resource (`DELETE-01`), rejects expired/drifted tokens with `409` and ZERO mutations
   (`DELETE-02`), activates fence + 5 `PENDING` component receipts + cancels queued jobs/runs +
   immediately marks affected `exports` as `DELETED` (`artifact_payload = NULL`) + evicts
   ephemeral cache entries (`DELETE-03`, `DELETE-04`).
3. 5-Component `execute_deletion_job`: executes `JOBS_AND_OUTBOX`, `EXPORTS`, `FTS_INDEX`,
   `EPHEMERAL_CACHE`, and `AUTHORITATIVE_DOMAIN` in deterministic order with per-component
   receipts. Partial failure transitions `deletion_jobs` to `PARTIAL` (`DELETION_PENDING`, fence
   stays active -> `DELETE-05`); retry executes ONLY non-`SUCCEEDED` components and converges
   idempotently to `SUCCEEDED` with 0 resurrected rows (`DELETE-06`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.contracts_api.models import (
    ContractBoundaryError,
    DeletionConfirmAcceptedResponse,
    DeletionConfirmRequest,
    DeletionImpactCounts,
    DeletionPreviewResponse,
    DeletionPreviewTokenPayload,
    NonRecoverableScopes,
    SearchEntityType,
    decode_and_verify_deletion_preview_token,
    encode_deletion_preview_token,
)
from career_memory_api.deletion.models import (
    DELETION_COMPONENT_ORDER,
    DeletionComponentReceiptItem,
    DeletionError,
    DeletionJobStatusResponse,
    EphemeralCacheRegistry,
    project_user_visible_deletion_state,
)
from career_memory_api.jobs.models import AuditActorType, DeletionJobStatus
from career_memory_api.jobs.postgres_runtime import bind_worker_workspace_context
from career_memory_api.schema_contracts.enums import (
    DeletionComponent,
    DeletionComponentStatus,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)
from career_memory_api.search.indexer import PostgresSearchIndexer

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
ComponentFaultInjector = Callable[[str], None]


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _sha256_prefixed(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _sha256_hex(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sanitize_error_code(exc: Exception) -> str:
    raw = getattr(exc, "code", None) or type(exc).__name__
    cleaned = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in str(raw))
    return (cleaned[:120] or "COMPONENT_ERROR").upper()


@dataclass(frozen=True, slots=True)
class _DeletionImpactPlan:
    source_resource_id: str
    resource_version: str
    impact_scope_version: str
    counts: DeletionImpactCounts
    obj_ids: tuple[str, ...]
    rev_ids: tuple[str, ...]
    cu_ids: tuple[str, ...]
    affected_event_ids: tuple[str, ...]
    exclusive_event_ids: tuple[str, ...]
    mixed_event_ids: tuple[str, ...]
    affected_fact_ids: tuple[str, ...]
    affected_contribution_ids: tuple[str, ...]
    affected_claim_ids: tuple[str, ...]
    affected_export_ids: tuple[str, ...]


class PostgresDeletionService:
    """PostgreSQL 18 OD-003 Source Deletion Preview, Confirm, Status & 5-Component Worker."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        worker_connection_factory: ConnectionFactory | None = None,
        ephemeral_cache_registry: EphemeralCacheRegistry | None = None,
        preview_ttl_seconds: int = 300,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._worker_connection_factory = worker_connection_factory
        self.ephemeral_cache = ephemeral_cache_registry or EphemeralCacheRegistry()
        self._preview_ttl_seconds = preview_ttl_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    # =========================================================================
    # 1. POST /v1/source-resources/{source_resource_id}/deletion-preview
    # =========================================================================
    def preview_source_deletion(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
    ) -> DeletionPreviewResponse:
        res_uuid = _parse_uuid_or_none(source_resource_id)
        if res_uuid is None:
            raise ContractBoundaryError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )
        ts = self._now_fn()
        expires_at = ts + timedelta(seconds=self._preview_ttl_seconds)

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                plan = self._collect_deletion_impact(
                    cur,
                    workspace_id=access.workspace_id,
                    source_resource_id=res_uuid,
                    lock_resource=False,
                )

        token = encode_deletion_preview_token(
            DeletionPreviewTokenPayload(
                workspace_id=access.workspace_id,
                source_resource_id=res_uuid,
                resource_version=plan.resource_version,
                impact_scope_version=plan.impact_scope_version,
                expires_at=expires_at,
            )
        )
        return DeletionPreviewResponse(
            source_resource_id=res_uuid,
            resource_version=plan.resource_version,
            impact_scope_version=plan.impact_scope_version,
            impact_counts=plan.counts,
            non_recoverable_scopes=NonRecoverableScopes(),
            preview_token=token,
            expires_at=expires_at,
        )

    # =========================================================================
    # 2. POST /v1/source-resources/{source_resource_id}/deletion-confirm
    # =========================================================================
    def confirm_source_deletion(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        idempotency_key: str,
        request: DeletionConfirmRequest,
    ) -> DeletionConfirmAcceptedResponse:
        res_uuid = _parse_uuid_or_none(source_resource_id)
        if res_uuid is None:
            raise ContractBoundaryError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )
        ts = self._now_fn()
        req_fingerprint = _sha256_hex(
            {
                "acknowledge_irreversible_purge": request.acknowledge_irreversible_purge,
                "expected_impact_scope_version": request.expected_impact_scope_version,
                "expected_resource_version": request.expected_resource_version,
                "preview_token": request.preview_token,
                "source_resource_id": res_uuid,
            }
        )

        accepted: DeletionConfirmAcceptedResponse
        should_evict_cache = False

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # Lock source_resources row FOR NO KEY UPDATE first (serializes concurrent confirms
                # without blocking on FOR KEY SHARE FK locks held by concurrent workers!)
                plan = self._collect_deletion_impact(
                    cur,
                    workspace_id=access.workspace_id,
                    source_resource_id=res_uuid,
                    lock_resource=True,
                )

                # 1. Check 24h immutable api_idempotency_records
                cur.execute(
                    """
                    SELECT request_fingerprint, response_body, expires_at
                    FROM public.api_idempotency_records
                    WHERE workspace_id = %s::uuid
                      AND operation_scope = 'CONFIRM_SOURCE_DELETION'
                      AND idempotency_key = %s
                    """,
                    (access.workspace_id, idempotency_key),
                )
                idem_row = cur.fetchone()
                if idem_row is not None:
                    stored_fp = str(idem_row[0])
                    stored_body = dict(idem_row[1] or {})
                    expires_at = idem_row[2]
                    if ts >= expires_at:
                        raise ContractBoundaryError(
                            status_code=409,
                            code="IDEMPOTENCY_KEY_EXPIRED",
                            message="Idempotency-Key has expired after 24 hours.",
                        )
                    if stored_fp != req_fingerprint:
                        raise ContractBoundaryError(
                            status_code=409,
                            code="IDEMPOTENCY_CONFLICT",
                            message=(
                                "Idempotency-Key was already used with a different "
                                "deletion confirmation payload."
                            ),
                        )
                    return DeletionConfirmAcceptedResponse.model_validate(stored_body)

                # 2. Deduplicate against any existing active deletion_jobs for this resource (DELETE-01)
                cur.execute(
                    """
                    SELECT id::text
                    FROM public.deletion_jobs
                    WHERE workspace_id = %s::uuid
                      AND source_resource_id = %s::uuid
                      AND status IN ('PENDING', 'RUNNING', 'PARTIAL')
                    ORDER BY requested_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (access.workspace_id, res_uuid),
                )
                existing_dj = cur.fetchone()
                if existing_dj is not None:
                    existing_job_id = str(existing_dj[0])
                    accepted = DeletionConfirmAcceptedResponse(
                        deletion_job_id=existing_job_id,
                        source_resource_id=res_uuid,
                    )
                    self._insert_idempotency_receipt(
                        cur,
                        workspace_id=access.workspace_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=req_fingerprint,
                        deletion_job_id=existing_job_id,
                        response=accepted,
                        now=ts,
                    )
                    should_evict_cache = True
                else:
                    # 3. Verify token signature, expiry (409 DELETION_PREVIEW_EXPIRED), and
                    #    live resource/impact scope versions (409 DELETION_PREVIEW_SCOPE_CHANGED)
                    #    with ZERO rows mutated on failure (DELETE-02)!
                    decode_and_verify_deletion_preview_token(
                        request.preview_token,
                        expected_workspace_id=access.workspace_id,
                        expected_source_resource_id=res_uuid,
                        current_resource_version=plan.resource_version,
                        current_impact_scope_version=plan.impact_scope_version,
                        now=ts,
                    )
                    if (
                        request.expected_resource_version != plan.resource_version
                        or request.expected_impact_scope_version != plan.impact_scope_version
                    ):
                        raise ContractBoundaryError(
                            status_code=409,
                            code="DELETION_PREVIEW_SCOPE_CHANGED",
                            message=(
                                "Expected resource_version or impact_scope_version does not match "
                                "current state."
                            ),
                        )

                    # 4. Atomic fence activation + 5 component receipts + immediate zero-window blocks
                    del_job_id = str(uuid.uuid4())
                    checkpoint_init = {
                        "stage": "FENCE_ACTIVATED",
                        "resource_version": plan.resource_version,
                        "impact_scope_version": plan.impact_scope_version,
                        "preview_counts": plan.counts.model_dump(mode="json"),
                    }
                    assert_no_forbidden_raw_keys(checkpoint_init, field_name="deletion_jobs.checkpoint")
                    assert_no_forbidden_secret_keys(
                        checkpoint_init, field_name="deletion_jobs.checkpoint"
                    )
                    cur.execute(
                        """
                        INSERT INTO public.deletion_jobs (
                            id, workspace_id, source_resource_id, status,
                            checkpoint, failure_summary, requested_by, requested_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PENDING',
                            %s, '{}'::jsonb, %s::uuid, %s
                        )
                        """,
                        (
                            del_job_id,
                            access.workspace_id,
                            res_uuid,
                            Jsonb(checkpoint_init),
                            _parse_uuid_or_none(access.user_id),
                            ts,
                        ),
                    )

                    for component in DELETION_COMPONENT_ORDER:
                        cur.execute(
                            """
                            INSERT INTO public.deletion_component_receipts (
                                id, workspace_id, deletion_job_id, component,
                                status, attempt, purged_count, receipt_metadata, updated_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                'PENDING', 0, 0, '{}'::jsonb, %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                access.workspace_id,
                                del_job_id,
                                component.value,
                                ts,
                            ),
                        )

                    # Cancel queued jobs & pending sync_runs targeting this source_resource_id
                    canceled_jobs, canceled_runs = self._cancel_queued_work_for_resource(
                        cur,
                        workspace_id=access.workspace_id,
                        source_resource_id=res_uuid,
                        include_running=False,
                        now=ts,
                    )

                    # Zero-window export download block (DELETE-04): mark affected exports DELETED now!
                    invalidated_exports = 0
                    if plan.affected_export_ids:
                        cur.execute(
                            """
                            UPDATE public.exports
                            SET status = 'DELETED',
                                artifact_payload = NULL,
                                invalidation_reason = 'SOURCE_RESOURCE_DELETED',
                                deleted_at = %s,
                                updated_at = %s
                            WHERE workspace_id = %s::uuid
                              AND id = ANY(%s::uuid[])
                              AND status <> 'DELETED'
                            """,
                            (ts, ts, access.workspace_id, list(plan.affected_export_ids)),
                        )
                        invalidated_exports = int(cur.rowcount or 0)

                    accepted = DeletionConfirmAcceptedResponse(
                        deletion_job_id=del_job_id,
                        source_resource_id=res_uuid,
                    )
                    self._insert_idempotency_receipt(
                        cur,
                        workspace_id=access.workspace_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=req_fingerprint,
                        deletion_job_id=del_job_id,
                        response=accepted,
                        now=ts,
                    )
                    audit_meta = {
                        "deletion_job_id": del_job_id,
                        "resource_version": plan.resource_version,
                        "impact_scope_version": plan.impact_scope_version,
                        "canceled_queued_jobs": canceled_jobs,
                        "canceled_pending_sync_runs": canceled_runs,
                        "immediately_deleted_exports": invalidated_exports,
                    }
                    self._insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.USER,
                        actor_id=access.user_id,
                        action="SOURCE_DELETION_CONFIRMED",
                        target_type="SOURCE_RESOURCE",
                        target_id=res_uuid,
                        redacted_metadata=audit_meta,
                        created_at=ts,
                    )
                    self._insert_outbox_event(
                        cur,
                        workspace_id=access.workspace_id,
                        event_type="SOURCE_DELETION_REQUESTED",
                        aggregate_type="DELETION_JOB",
                        aggregate_id=del_job_id,
                        dedupe_key=f"source_deletion_requested:{access.workspace_id}:{del_job_id}",
                        payload={
                            "deletion_job_id": del_job_id,
                            "source_resource_id": res_uuid,
                        },
                        created_at=ts,
                    )
                    should_evict_cache = True

        if should_evict_cache:
            # Zero-window in-process ephemeral cache eviction
            self.ephemeral_cache.purge_for_resource(access.workspace_id, res_uuid)

        return accepted

    # =========================================================================
    # 3. GET /v1/deletion-jobs/{deletion_job_id}
    # =========================================================================
    def get_deletion_job(
        self,
        access: WorkspaceAccessContext,
        *,
        deletion_job_id: str,
    ) -> DeletionJobStatusResponse:
        job_uuid = _parse_uuid_or_none(deletion_job_id)
        if job_uuid is None:
            raise DeletionError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                return self._load_deletion_job_status(
                    cur, workspace_id=access.workspace_id, deletion_job_id=job_uuid
                )

    # =========================================================================
    # 4. 5-Component Deletion Worker (DELETE-03..06, T-502A)
    # =========================================================================
    def execute_deletion_job(
        self,
        workspace_id: str,
        deletion_job_id: str,
        *,
        access: WorkspaceAccessContext | None = None,
        actor_user_id: str | None = None,
        fault_injector: ComponentFaultInjector | None = None,
        now: datetime | None = None,
    ) -> DeletionJobStatusResponse:
        """Execute all non-SUCCEEDED components for a deletion job with idempotent convergence."""
        ws_uuid = _parse_uuid_or_none(workspace_id)
        job_uuid = _parse_uuid_or_none(deletion_job_id)
        if ws_uuid is None or job_uuid is None:
            raise DeletionError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )
        ts = now or self._now_fn()

        resolved_actor_id = (
            access.user_id
            if access is not None
            else (_parse_uuid_or_none(actor_user_id) if actor_user_id is not None else None)
        )
        if resolved_actor_id is None and self._worker_connection_factory is not None:
            with self._worker_connection_factory() as wconn:
                with wconn.transaction():
                    with wconn.cursor() as wcur:
                        bind_worker_workspace_context(wcur, ws_uuid)
                        wcur.execute(
                            """
                            SELECT requested_by::text
                            FROM public.deletion_jobs
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            """,
                            (ws_uuid, job_uuid),
                        )
                        r = wcur.fetchone()
                        if r is not None and r[0] is not None:
                            resolved_actor_id = str(r[0])

        # Step 1: Load job, ensure 5 receipts exist, short-circuit if already SUCCEEDED (DELETE-06)
        with self._api_scoped_tx(
            workspace_id=ws_uuid, access=access, actor_user_id=resolved_actor_id
        ) as cur:
            status_before = self._load_deletion_job_status(
                cur, workspace_id=ws_uuid, deletion_job_id=job_uuid, for_update=True
            )
            res_uuid = status_before.source_resource_id
            if status_before.status is DeletionJobStatus.SUCCEEDED:
                return status_before

            # Ensure all 5 component receipt rows exist (e.g. if job was created by legacy path)
            existing_components = {item.component for item in status_before.components}
            for comp in DELETION_COMPONENT_ORDER:
                if comp not in existing_components:
                    cur.execute(
                        """
                        INSERT INTO public.deletion_component_receipts (
                            id, workspace_id, deletion_job_id, component,
                            status, attempt, purged_count, receipt_metadata, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s,
                            'PENDING', 0, 0, '{}'::jsonb, %s
                        )
                        ON CONFLICT (workspace_id, deletion_job_id, component) DO NOTHING
                        """,
                        (str(uuid.uuid4()), ws_uuid, job_uuid, comp.value, ts),
                    )

            cur.execute(
                """
                UPDATE public.deletion_jobs
                SET status = 'RUNNING',
                    completed_at = NULL
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (ws_uuid, job_uuid),
            )
            receipt_status_map = {
                item.component: item.status for item in status_before.components
            }

        # Step 2: Execute each incomplete component in deterministic order
        failed_component_errors: dict[str, str] = {}
        for component in DELETION_COMPONENT_ORDER:
            if receipt_status_map.get(component) is DeletionComponentStatus.SUCCEEDED:
                continue

            # Mark component RUNNING & increment attempt in its own transaction
            with self._api_scoped_tx(
                workspace_id=ws_uuid, access=access, actor_user_id=resolved_actor_id
            ) as cur:
                cur.execute(
                    """
                    UPDATE public.deletion_component_receipts
                    SET status = 'RUNNING',
                        attempt = attempt + 1,
                        last_error_code = NULL,
                        completed_at = NULL,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND deletion_job_id = %s::uuid
                      AND component = %s
                    """,
                    (ts, ws_uuid, job_uuid, component.value),
                )

            try:
                use_worker_tx = (
                    component is DeletionComponent.JOBS_AND_OUTBOX
                    and self._worker_connection_factory is not None
                )
                tx_cm = (
                    self._worker_scoped_tx(workspace_id=ws_uuid)
                    if use_worker_tx
                    else self._api_scoped_tx(
                        workspace_id=ws_uuid,
                        access=access,
                        actor_user_id=resolved_actor_id,
                    )
                )
                with tx_cm as cur:
                    if fault_injector is not None:
                        fault_injector(component.value)
                    purged_count, comp_meta = self._run_single_component_in_tx(
                        cur,
                        workspace_id=ws_uuid,
                        source_resource_id=res_uuid,
                        component=component,
                        can_update_outbox=use_worker_tx,
                        now=ts,
                    )
                    assert_no_forbidden_raw_keys(
                        comp_meta, field_name="deletion_component_receipts.receipt_metadata"
                    )
                    assert_no_forbidden_secret_keys(
                        comp_meta, field_name="deletion_component_receipts.receipt_metadata"
                    )
                    cur.execute(
                        """
                        UPDATE public.deletion_component_receipts
                        SET status = 'SUCCEEDED',
                            purged_count = %s,
                            last_error_code = NULL,
                            receipt_metadata = %s,
                            updated_at = %s,
                            completed_at = %s
                        WHERE workspace_id = %s::uuid
                          AND deletion_job_id = %s::uuid
                          AND component = %s
                        """,
                        (
                            purged_count,
                            Jsonb(comp_meta),
                            ts,
                            ts,
                            ws_uuid,
                            job_uuid,
                            component.value,
                        ),
                    )
                    receipt_status_map[component] = DeletionComponentStatus.SUCCEEDED
            except Exception as exc:  # noqa: BLE001
                err_code = _sanitize_error_code(exc)
                failed_component_errors[component.value] = err_code
                receipt_status_map[component] = DeletionComponentStatus.FAILED
                with self._api_scoped_tx(
                    workspace_id=ws_uuid, access=access, actor_user_id=resolved_actor_id
                ) as cur:
                    cur.execute(
                        """
                        UPDATE public.deletion_component_receipts
                        SET status = 'FAILED',
                            last_error_code = %s,
                            completed_at = NULL,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND deletion_job_id = %s::uuid
                          AND component = %s
                        """,
                        (err_code, ts, ws_uuid, job_uuid, component.value),
                    )
                    self._insert_audit_event(
                        cur,
                        workspace_id=ws_uuid,
                        actor_type=AuditActorType.WORKER,
                        actor_id=resolved_actor_id,
                        action="SOURCE_DELETION_COMPONENT_FAILED",
                        target_type="DELETION_JOB",
                        target_id=job_uuid,
                        redacted_metadata={
                            "component": component.value,
                            "error_code": err_code,
                            "source_resource_id": res_uuid,
                        },
                        created_at=ts,
                    )
                # Stop at the first failing component so downstream dependencies remain intact
                # until retry succeeds (DELETE-05 / DELETE-06)
                break

        # Step 3: Finalize deletion_jobs state (SUCCEEDED iff all 5 receipts SUCCEEDED, else PARTIAL)
        with self._api_scoped_tx(
            workspace_id=ws_uuid, access=access, actor_user_id=resolved_actor_id
        ) as cur:
            cur.execute(
                """
                SELECT component, status, purged_count, last_error_code
                FROM public.deletion_component_receipts
                WHERE workspace_id = %s::uuid
                  AND deletion_job_id = %s::uuid
                ORDER BY component ASC
                """,
                (ws_uuid, job_uuid),
            )
            receipt_rows = cur.fetchall()
            succeeded_components: list[str] = []
            failed_components: list[str] = []
            pending_components: list[str] = []
            purged_by_component: dict[str, int] = {}
            error_codes: dict[str, str] = {}
            for comp_name, comp_status, pcount, err_c in receipt_rows:
                c_str = str(comp_name)
                st_str = str(comp_status)
                purged_by_component[c_str] = int(pcount or 0)
                if st_str == DeletionComponentStatus.SUCCEEDED.value:
                    succeeded_components.append(c_str)
                elif st_str == DeletionComponentStatus.FAILED.value:
                    failed_components.append(c_str)
                    if err_c is not None:
                        error_codes[c_str] = str(err_c)
                else:
                    pending_components.append(c_str)

            all_succeeded = len(succeeded_components) == len(DELETION_COMPONENT_ORDER)
            checkpoint_meta = {
                "stage": "COMPLETED" if all_succeeded else "PARTIAL",
                "succeeded_components": sorted(succeeded_components),
                "purged_by_component": purged_by_component,
            }
            assert_no_forbidden_raw_keys(checkpoint_meta, field_name="deletion_jobs.checkpoint")
            assert_no_forbidden_secret_keys(checkpoint_meta, field_name="deletion_jobs.checkpoint")

            if all_succeeded:
                cur.execute(
                    """
                    UPDATE public.deletion_jobs
                    SET status = 'SUCCEEDED',
                        checkpoint = %s,
                        failure_summary = '{}'::jsonb,
                        completed_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (Jsonb(checkpoint_meta), ts, ws_uuid, job_uuid),
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=ws_uuid,
                    actor_type=AuditActorType.WORKER,
                    actor_id=resolved_actor_id,
                    action="SOURCE_DELETION_COMPLETED",
                    target_type="SOURCE_RESOURCE",
                    target_id=res_uuid,
                    redacted_metadata={
                        "deletion_job_id": job_uuid,
                        "purged_by_component": purged_by_component,
                    },
                    created_at=ts,
                )
                self._insert_outbox_event(
                    cur,
                    workspace_id=ws_uuid,
                    event_type="SOURCE_RESOURCE_PURGED",
                    aggregate_type="SOURCE_RESOURCE",
                    aggregate_id=res_uuid,
                    dedupe_key=f"source_resource_purged:{ws_uuid}:{res_uuid}:{job_uuid}",
                    payload={
                        "deletion_job_id": job_uuid,
                        "source_resource_id": res_uuid,
                        "purged_by_component": purged_by_component,
                    },
                    created_at=ts,
                )
            else:
                failure_summary = {
                    "failed_components": sorted(failed_components),
                    "pending_components": sorted(pending_components),
                    "component_errors": error_codes,
                    "retryable": True,
                }
                assert_no_forbidden_raw_keys(
                    failure_summary, field_name="deletion_jobs.failure_summary"
                )
                assert_no_forbidden_secret_keys(
                    failure_summary, field_name="deletion_jobs.failure_summary"
                )
                cur.execute(
                    """
                    UPDATE public.deletion_jobs
                    SET status = 'PARTIAL',
                        checkpoint = %s,
                        failure_summary = %s,
                        completed_at = NULL
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (Jsonb(checkpoint_meta), Jsonb(failure_summary), ws_uuid, job_uuid),
                )

            return self._load_deletion_job_status(
                cur, workspace_id=ws_uuid, deletion_job_id=job_uuid
            )

    # =========================================================================
    # Per-Component Cleanup Execution
    # =========================================================================
    def _run_single_component_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str,
        component: DeletionComponent,
        can_update_outbox: bool = False,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        if component is DeletionComponent.JOBS_AND_OUTBOX:
            c_jobs, c_runs = self._cancel_queued_work_for_resource(
                cur,
                workspace_id=workspace_id,
                source_resource_id=source_resource_id,
                include_running=True,
                now=now,
            )
            c_outbox = 0
            if can_update_outbox:
                cur.execute(
                    """
                    UPDATE public.outbox_events oe
                    SET status = 'FAILED_TERMINAL',
                        last_error_code = 'DELETION_FENCE_ACTIVE'
                    FROM public.sync_runs sr
                    WHERE oe.workspace_id = %s::uuid
                      AND sr.workspace_id = oe.workspace_id
                      AND oe.aggregate_type = 'SYNC_RUN'
                      AND oe.aggregate_id = sr.id
                      AND sr.source_resource_id = %s::uuid
                      AND oe.status = 'PENDING'
                    """,
                    (workspace_id, source_resource_id),
                )
                c_outbox = int(cur.rowcount or 0)
            return c_jobs + c_runs + c_outbox, {
                "canceled_jobs": c_jobs,
                "canceled_sync_runs": c_runs,
                "suppressed_outbox_events": c_outbox,
            }

        if component is DeletionComponent.EXPORTS:
            plan = self._collect_deletion_impact(
                cur,
                workspace_id=workspace_id,
                source_resource_id=source_resource_id,
                lock_resource=False,
                include_deleted_exports=True,
            )
            purged = 0
            if plan.affected_export_ids:
                cur.execute(
                    """
                    DELETE FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (workspace_id, list(plan.affected_export_ids)),
                )
                purged = int(cur.rowcount or 0)
            return purged, {"purged_exports": purged}

        if component is DeletionComponent.FTS_INDEX:
            plan = self._collect_deletion_impact(
                cur,
                workspace_id=workspace_id,
                source_resource_id=source_resource_id,
                lock_resource=False,
            )
            indexer = PostgresSearchIndexer()
            purged = indexer.purge_resource_entries_in_tx(
                cur,
                workspace_id=workspace_id,
                source_resource_id=source_resource_id,
                affected_entity_ids={
                    SearchEntityType.WORK_EVENT: list(plan.exclusive_event_ids),
                    SearchEntityType.FACT: list(plan.affected_fact_ids),
                    SearchEntityType.CLAIM: list(plan.affected_claim_ids),
                },
            )
            return purged, {"purged_search_entries": purged}

        if component is DeletionComponent.EPHEMERAL_CACHE:
            purged = self.ephemeral_cache.purge_for_resource(workspace_id, source_resource_id)
            return purged, {"purged_ephemeral_cache_entries": purged}

        if component is DeletionComponent.AUTHORITATIVE_DOMAIN:
            return self._purge_authoritative_domain_in_tx(
                cur,
                workspace_id=workspace_id,
                source_resource_id=source_resource_id,
                now=now,
            )

        raise ValueError(f"Unsupported deletion component: {component!r}")

    def _purge_authoritative_domain_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        plan = self._collect_deletion_impact(
            cur,
            workspace_id=workspace_id,
            source_resource_id=source_resource_id,
            lock_resource=True,
            include_deleted_exports=True,
        )

        # Enable approved OD-003 purge session flags for immutable version/revision triggers
        cur.execute("SELECT set_config('app.allow_revision_purge', 'on', true)")
        cur.execute("SELECT set_config('app.allow_work_event_version_purge', 'on', true)")

        # 1. Purge any remaining dependent exports
        purged_exports = 0
        if plan.affected_export_ids:
            cur.execute(
                """
                DELETE FROM public.exports
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.affected_export_ids)),
            )
            purged_exports = int(cur.rowcount or 0)

        # 2. Purge all affected claims (every version in supersede chain + mixed-source claims -> 0 retained!)
        purged_claims = 0
        if plan.affected_claim_ids:
            cur.execute(
                """
                DELETE FROM public.claims
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.affected_claim_ids)),
            )
            purged_claims = int(cur.rowcount or 0)

        # 3. Purge affected contributions (exclusively backed by affected facts or deleted-only events)
        purged_contributions = 0
        if plan.affected_contribution_ids:
            cur.execute(
                """
                DELETE FROM public.contributions
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.affected_contribution_ids)),
            )
            purged_contributions = int(cur.rowcount or 0)

        # 4. Purge all affected facts (every version in supersede chain; cascades fact_support & contribution_support)
        purged_facts = 0
        if plan.affected_fact_ids:
            cur.execute(
                """
                DELETE FROM public.facts
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.affected_fact_ids)),
            )
            purged_facts = int(cur.rowcount or 0)

        # 5. Purge exclusively-backed work_events (cascades work_event_versions, event_memberships,
        #    event_composite_memberships, event_constraints, sync_run_event_promotions)
        purged_events = 0
        if plan.exclusive_event_ids:
            cur.execute(
                """
                DELETE FROM public.work_events
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.exclusive_event_ids)),
            )
            purged_events = int(cur.rowcount or 0)

        # 6. Purge deleted resource's content_units, revisions, and source_objects
        #    (re-anchoring any surviving mixed-resource work_events onto their other-resource objects)
        purged_cus = 0
        if plan.cu_ids:
            cur.execute(
                """
                DELETE FROM public.content_units
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.cu_ids)),
            )
            purged_cus = int(cur.rowcount or 0)

        purged_revs = 0
        if plan.rev_ids:
            cur.execute(
                """
                DELETE FROM public.source_object_revisions
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.rev_ids)),
            )
            purged_revs = int(cur.rowcount or 0)

        purged_objs = 0
        if plan.obj_ids:
            cur.execute(
                """
                DELETE FROM public.event_memberships
                WHERE workspace_id = %s::uuid
                  AND source_object_id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.obj_ids)),
            )
            cur.execute(
                """
                UPDATE public.work_events AS we
                SET primary_anchor_source_object_id = (
                        SELECT em.source_object_id
                        FROM public.event_memberships AS em
                        WHERE em.workspace_id = we.workspace_id
                          AND em.event_id = we.id
                          AND em.status = 'ACTIVE'
                        ORDER BY em.created_at ASC, em.id ASC
                        LIMIT 1
                    ),
                    anchor_mode = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM public.event_memberships AS em
                            WHERE em.workspace_id = we.workspace_id
                              AND em.event_id = we.id
                              AND em.status = 'ACTIVE'
                        )
                        THEN we.anchor_mode
                        ELSE 'USER_COMPOSITE'
                    END,
                    updated_at = %s
                WHERE we.workspace_id = %s::uuid
                  AND we.primary_anchor_source_object_id = ANY(%s::uuid[])
                """,
                (now, workspace_id, list(plan.obj_ids)),
            )
            cur.execute(
                """
                DELETE FROM public.source_objects
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                """,
                (workspace_id, list(plan.obj_ids)),
            )
            purged_objs = int(cur.rowcount or 0)

        # 7. Transition source_resources.status = 'DELETED' (source_accounts & other repos preserved!)
        cur.execute(
            """
            UPDATE public.source_resources
            SET status = 'DELETED'
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (workspace_id, source_resource_id),
        )

        # 8. Safety purge of any leftover search_entries referencing purged entities
        PostgresSearchIndexer().purge_resource_entries_in_tx(
            cur,
            workspace_id=workspace_id,
            source_resource_id=source_resource_id,
            affected_entity_ids={
                SearchEntityType.WORK_EVENT: list(plan.exclusive_event_ids),
                SearchEntityType.FACT: list(plan.affected_fact_ids),
                SearchEntityType.CLAIM: list(plan.affected_claim_ids),
            },
        )

        meta = {
            "purged_exports": purged_exports,
            "purged_claims": purged_claims,
            "purged_contributions": purged_contributions,
            "purged_facts": purged_facts,
            "purged_work_events": purged_events,
            "purged_content_units": purged_cus,
            "purged_source_revisions": purged_revs,
            "purged_source_objects": purged_objs,
        }
        total = sum(meta.values())
        return total, meta

    # =========================================================================
    # Live Impact Scope Calculation & State Loaders
    # =========================================================================
    def _collect_deletion_impact(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str,
        lock_resource: bool,
        include_deleted_exports: bool = False,
    ) -> _DeletionImpactPlan:
        lock_sql = "FOR NO KEY UPDATE" if lock_resource else ""
        cur.execute(
            f"""
            SELECT
                id::text,
                provider_resource_id,
                name,
                visibility,
                processing_mode,
                status,
                policy_version,
                consent_version
            FROM public.source_resources
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            {lock_sql}
            """,
            (workspace_id, source_resource_id),
        )
        res_row = cur.fetchone()
        if res_row is None:
            raise ContractBoundaryError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )
        resource_version = _sha256_prefixed(
            {
                "consent_version": res_row[7],
                "id": str(res_row[0]),
                "name": str(res_row[2]),
                "policy_version": str(res_row[6]),
                "processing_mode": str(res_row[4]),
                "provider_resource_id": str(res_row[1]),
                "status": str(res_row[5]),
                "visibility": str(res_row[3]),
            }
        )

        # 1. source_objects, revisions, content_units
        cur.execute(
            """
            SELECT id::text, lifecycle_state
            FROM public.source_objects
            WHERE workspace_id = %s::uuid
              AND source_resource_id = %s::uuid
            ORDER BY id ASC
            """,
            (workspace_id, source_resource_id),
        )
        obj_rows = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        obj_ids = [r[0] for r in obj_rows]

        rev_ids: list[str] = []
        if obj_ids:
            cur.execute(
                """
                SELECT id::text
                FROM public.source_object_revisions
                WHERE workspace_id = %s::uuid
                  AND source_object_id = ANY(%s::uuid[])
                ORDER BY id ASC
                """,
                (workspace_id, obj_ids),
            )
            rev_ids = [str(r[0]) for r in cur.fetchall()]

        cu_ids: list[str] = []
        if rev_ids:
            cur.execute(
                """
                SELECT id::text
                FROM public.content_units
                WHERE workspace_id = %s::uuid
                  AND source_object_revision_id = ANY(%s::uuid[])
                ORDER BY id ASC
                """,
                (workspace_id, rev_ids),
            )
            cu_ids = [str(r[0]) for r in cur.fetchall()]

        # 2. Affected work_events (direct anchor, active memberships, and composite parents)
        affected_event_ids: list[str] = []
        exclusive_event_ids: list[str] = []
        mixed_event_ids: list[str] = []
        event_fingerprints: list[tuple[str, int, str]] = []

        if obj_ids:
            cur.execute(
                """
                WITH seed_events AS (
                    SELECT we.id
                    FROM public.work_events we
                    WHERE we.workspace_id = %s::uuid
                      AND we.primary_anchor_source_object_id = ANY(%s::uuid[])
                    UNION
                    SELECT em.event_id AS id
                    FROM public.event_memberships em
                    WHERE em.workspace_id = %s::uuid
                      AND em.source_object_id = ANY(%s::uuid[])
                      AND em.status = 'ACTIVE'
                ),
                all_affected AS (
                    SELECT id FROM seed_events
                    UNION
                    SELECT ecm.composite_event_id AS id
                    FROM public.event_composite_memberships ecm
                    INNER JOIN seed_events se ON se.id = ecm.member_event_id
                    WHERE ecm.workspace_id = %s::uuid
                      AND ecm.status = 'ACTIVE'
                )
                SELECT
                    we.id::text,
                    we.version,
                    we.status,
                    (
                        EXISTS (
                            SELECT 1
                            FROM public.source_objects so_pa
                            WHERE so_pa.workspace_id = we.workspace_id
                              AND so_pa.id = we.primary_anchor_source_object_id
                              AND so_pa.source_resource_id <> %s::uuid
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM public.event_memberships em_other
                            INNER JOIN public.source_objects so_other
                                ON so_other.workspace_id = em_other.workspace_id
                               AND so_other.id = em_other.source_object_id
                            WHERE em_other.workspace_id = we.workspace_id
                              AND em_other.event_id = we.id
                              AND em_other.status = 'ACTIVE'
                              AND so_other.source_resource_id <> %s::uuid
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM public.event_composite_memberships ecm_o
                            INNER JOIN public.work_events m_we
                                ON m_we.workspace_id = ecm_o.workspace_id
                               AND m_we.id = ecm_o.member_event_id
                            INNER JOIN public.source_objects so_mo
                                ON so_mo.workspace_id = m_we.workspace_id
                               AND so_mo.id = m_we.primary_anchor_source_object_id
                            WHERE ecm_o.workspace_id = we.workspace_id
                              AND ecm_o.composite_event_id = we.id
                              AND ecm_o.status = 'ACTIVE'
                              AND so_mo.source_resource_id <> %s::uuid
                        )
                    ) AS has_other_resource
                FROM public.work_events we
                INNER JOIN all_affected aa ON aa.id = we.id
                WHERE we.workspace_id = %s::uuid
                ORDER BY we.id ASC
                """,
                (
                    workspace_id,
                    obj_ids,
                    workspace_id,
                    obj_ids,
                    workspace_id,
                    source_resource_id,
                    source_resource_id,
                    source_resource_id,
                    workspace_id,
                ),
            )
            for eid, ever, est, has_other in cur.fetchall():
                eid_s = str(eid)
                affected_event_ids.append(eid_s)
                event_fingerprints.append((eid_s, int(ever), str(est)))
                if bool(has_other):
                    mixed_event_ids.append(eid_s)
                else:
                    exclusive_event_ids.append(eid_s)

        # 3. Affected facts (from content_units OR exclusive_event_ids, expanded across supersedes chain)
        seed_fact_ids: set[str] = set()
        if cu_ids:
            cur.execute(
                """
                SELECT DISTINCT fact_id::text
                FROM public.fact_support
                WHERE workspace_id = %s::uuid
                  AND content_unit_id = ANY(%s::uuid[])
                """,
                (workspace_id, cu_ids),
            )
            seed_fact_ids.update(str(r[0]) for r in cur.fetchall())
        if exclusive_event_ids:
            cur.execute(
                """
                SELECT id::text
                FROM public.facts
                WHERE workspace_id = %s::uuid
                  AND event_id = ANY(%s::uuid[])
                """,
                (workspace_id, exclusive_event_ids),
            )
            seed_fact_ids.update(str(r[0]) for r in cur.fetchall())

        affected_fact_ids: list[str] = []
        fact_fingerprints: list[tuple[str, int, str]] = []
        if seed_fact_ids:
            cur.execute(
                """
                WITH RECURSIVE fchain(id, supersedes_fact_id) AS (
                    SELECT id, supersedes_fact_id
                    FROM public.facts
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    UNION
                    SELECT f.id, f.supersedes_fact_id
                    FROM public.facts f
                    INNER JOIN fchain fc
                        ON f.supersedes_fact_id = fc.id
                        OR f.id = fc.supersedes_fact_id
                    WHERE f.workspace_id = %s::uuid
                )
                SELECT DISTINCT f.id::text, f.version, f.status
                FROM public.facts f
                INNER JOIN fchain fc ON fc.id = f.id
                WHERE f.workspace_id = %s::uuid
                ORDER BY 1 ASC
                """,
                (workspace_id, sorted(seed_fact_ids), workspace_id, workspace_id),
            )
            for fid, fver, fst in cur.fetchall():
                affected_fact_ids.append(str(fid))
                fact_fingerprints.append((str(fid), int(fver), str(fst)))

        # 4. Affected contributions (backed exclusively by affected_fact_ids or on exclusive_event_ids)
        affected_contribution_ids: list[str] = []
        contrib_fingerprints: list[tuple[str, str]] = []
        if affected_fact_ids or exclusive_event_ids:
            cur.execute(
                """
                SELECT DISTINCT c.id::text, c.status
                FROM public.contributions c
                WHERE c.workspace_id = %s::uuid
                  AND (
                      (cardinality(%s::uuid[]) > 0 AND c.event_id = ANY(%s::uuid[]))
                      OR (
                          cardinality(%s::uuid[]) > 0
                          AND EXISTS (
                              SELECT 1
                              FROM public.contribution_support cs
                              WHERE cs.workspace_id = c.workspace_id
                                AND cs.contribution_id = c.id
                                AND cs.fact_id = ANY(%s::uuid[])
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM public.contribution_support cs_keep
                              WHERE cs_keep.workspace_id = c.workspace_id
                                AND cs_keep.contribution_id = c.id
                                AND cs_keep.status = 'ACTIVE'
                                AND NOT (cs_keep.fact_id = ANY(%s::uuid[]))
                          )
                      )
                  )
                ORDER BY 1 ASC
                """,
                (
                    workspace_id,
                    exclusive_event_ids,
                    exclusive_event_ids,
                    affected_fact_ids,
                    affected_fact_ids,
                    affected_fact_ids,
                ),
            )
            for cid, cst in cur.fetchall():
                affected_contribution_ids.append(str(cid))
                contrib_fingerprints.append((str(cid), str(cst)))

        # 5. Affected claims (via claim_atom_support->affected_fact_ids OR exclusive_event_ids,
        #    expanded recursively across supersedes_claim_id chain -> 0 retained versions!)
        seed_claim_ids: set[str] = set()
        if affected_fact_ids:
            cur.execute(
                """
                SELECT DISTINCT ca.claim_id::text
                FROM public.claim_atom_support cas
                INNER JOIN public.claim_atoms ca
                    ON ca.workspace_id = cas.workspace_id
                   AND ca.id = cas.claim_atom_id
                WHERE cas.workspace_id = %s::uuid
                  AND cas.fact_id = ANY(%s::uuid[])
                """,
                (workspace_id, affected_fact_ids),
            )
            seed_claim_ids.update(str(r[0]) for r in cur.fetchall())
        if exclusive_event_ids:
            cur.execute(
                """
                SELECT id::text
                FROM public.claims
                WHERE workspace_id = %s::uuid
                  AND event_id = ANY(%s::uuid[])
                """,
                (workspace_id, exclusive_event_ids),
            )
            seed_claim_ids.update(str(r[0]) for r in cur.fetchall())

        affected_claim_ids: list[str] = []
        claim_fingerprints: list[tuple[str, int, str]] = []
        claims_draft = 0
        claims_edited = 0
        claims_approved = 0
        claims_historical = 0

        if seed_claim_ids:
            cur.execute(
                """
                WITH RECURSIVE cchain(id, supersedes_claim_id) AS (
                    SELECT id, supersedes_claim_id
                    FROM public.claims
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    UNION
                    SELECT c.id, c.supersedes_claim_id
                    FROM public.claims c
                    INNER JOIN cchain cc
                        ON c.supersedes_claim_id = cc.id
                        OR c.id = cc.supersedes_claim_id
                    WHERE c.workspace_id = %s::uuid
                )
                SELECT DISTINCT c.id::text, c.version, c.status, c.supersedes_claim_id::text
                FROM public.claims c
                INNER JOIN cchain cc ON cc.id = c.id
                WHERE c.workspace_id = %s::uuid
                ORDER BY 1 ASC
                """,
                (workspace_id, sorted(seed_claim_ids), workspace_id, workspace_id),
            )
            for cid, cver, cst, sup_id in cur.fetchall():
                cid_s = str(cid)
                ver_i = int(cver)
                st_s = str(cst)
                affected_claim_ids.append(cid_s)
                claim_fingerprints.append((cid_s, ver_i, st_s))
                if st_s == "DRAFT":
                    claims_draft += 1
                if ver_i > 1 or sup_id is not None:
                    claims_edited += 1
                if st_s == "APPROVED":
                    claims_approved += 1
                if st_s == "SUPERSEDED":
                    claims_historical += 1

        # 6. Affected exports
        export_status_filter = "" if include_deleted_exports else "AND e.status <> 'DELETED'"
        cur.execute(
            f"""
            SELECT DISTINCT e.id::text, e.status
            FROM public.exports e
            LEFT JOIN public.export_claims ec
                ON ec.workspace_id = e.workspace_id
               AND ec.export_id = e.id
            WHERE e.workspace_id = %s::uuid
              {export_status_filter}
              AND (
                  e.source_resource_id = %s::uuid
                  OR (cardinality(%s::uuid[]) > 0 AND ec.claim_id = ANY(%s::uuid[]))
              )
            ORDER BY 1 ASC
            """,
            (workspace_id, source_resource_id, affected_claim_ids, affected_claim_ids),
        )
        export_rows = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        affected_export_ids = [r[0] for r in export_rows]

        # 7. Search index entries count
        search_entries_count = 0
        cur.execute("SELECT to_regclass('public.search_entries') IS NOT NULL")
        has_search_table = bool((cur.fetchone() or (False,))[0])
        if has_search_table:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.search_entries se
                WHERE se.workspace_id = %s::uuid
                  AND (
                      se.source_resource_id = %s::uuid
                      OR (
                          se.projection_metadata ? 'linked_resource_ids'
                          AND se.projection_metadata -> 'linked_resource_ids' ? %s
                      )
                      OR (
                          cardinality(%s::uuid[]) > 0
                          AND se.entity_type = 'WORK_EVENT'
                          AND se.entity_id = ANY(%s::uuid[])
                      )
                      OR (
                          cardinality(%s::uuid[]) > 0
                          AND se.entity_type = 'FACT'
                          AND se.entity_id = ANY(%s::uuid[])
                      )
                      OR (
                          cardinality(%s::uuid[]) > 0
                          AND se.entity_type = 'CLAIM'
                          AND se.entity_id = ANY(%s::uuid[])
                      )
                  )
                """,
                (
                    workspace_id,
                    source_resource_id,
                    source_resource_id,
                    affected_event_ids,
                    affected_event_ids,
                    affected_fact_ids,
                    affected_fact_ids,
                    affected_claim_ids,
                    affected_claim_ids,
                ),
            )
            search_entries_count = int((cur.fetchone() or (0,))[0])

        # 8. Ephemeral cache count
        ephemeral_count = self.ephemeral_cache.count_for_resource(
            workspace_id, source_resource_id
        )

        counts = DeletionImpactCounts(
            work_events=len(affected_event_ids),
            facts=len(affected_fact_ids),
            contributions=len(affected_contribution_ids),
            claims_draft=claims_draft,
            claims_edited=claims_edited,
            claims_approved=claims_approved,
            claims_historical_versions=claims_historical,
            search_index_entries=search_entries_count,
            ephemeral_cache_entries=ephemeral_count,
            server_exports=len(affected_export_ids),
        )
        impact_scope_version = _sha256_prefixed(
            {
                "claims": claim_fingerprints,
                "contributions": contrib_fingerprints,
                "ephemeral_cache_entries": ephemeral_count,
                "exports": export_rows,
                "facts": fact_fingerprints,
                "search_index_entries": search_entries_count,
                "source_objects": obj_rows,
                "work_events": event_fingerprints,
            }
        )
        return _DeletionImpactPlan(
            source_resource_id=source_resource_id,
            resource_version=resource_version,
            impact_scope_version=impact_scope_version,
            counts=counts,
            obj_ids=tuple(obj_ids),
            rev_ids=tuple(rev_ids),
            cu_ids=tuple(cu_ids),
            affected_event_ids=tuple(affected_event_ids),
            exclusive_event_ids=tuple(exclusive_event_ids),
            mixed_event_ids=tuple(mixed_event_ids),
            affected_fact_ids=tuple(affected_fact_ids),
            affected_contribution_ids=tuple(affected_contribution_ids),
            affected_claim_ids=tuple(affected_claim_ids),
            affected_export_ids=tuple(affected_export_ids),
        )

    @contextmanager
    def _worker_scoped_tx(
        self,
        *,
        workspace_id: str,
    ) -> Iterator[psycopg.Cursor]:
        assert self._worker_connection_factory is not None
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, workspace_id)
                    yield cur

    @contextmanager
    def _api_scoped_tx(
        self,
        *,
        workspace_id: str,
        access: WorkspaceAccessContext | None,
        actor_user_id: str | None,
    ) -> Iterator[psycopg.Cursor]:
        with self._api_connection_factory() as conn:
            if access is not None:
                with workspace_scoped_transaction(conn, access) as cur:
                    yield cur
                return
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.current_workspace_id', %s, true)",
                        (workspace_id,),
                    )
                    if actor_user_id is not None:
                        cur.execute(
                            "SELECT set_config('app.current_user_id', %s, true)",
                            (actor_user_id,),
                        )
                    yield cur

    @staticmethod
    def _load_deletion_job_status(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        deletion_job_id: str,
        for_update: bool = False,
    ) -> DeletionJobStatusResponse:
        lock_sql = "FOR UPDATE" if for_update else ""
        cur.execute(
            f"""
            SELECT
                id::text,
                workspace_id::text,
                source_resource_id::text,
                status,
                checkpoint,
                failure_summary,
                requested_by::text,
                requested_at,
                completed_at
            FROM public.deletion_jobs
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            {lock_sql}
            """,
            (workspace_id, deletion_job_id),
        )
        row = cur.fetchone()
        if row is None:
            raise DeletionError(
                status_code=404,
                code="RESOURCE_NOT_FOUND",
                message="Requested resource was not found.",
            )

        cur.execute(
            """
            SELECT
                component,
                status,
                attempt,
                purged_count,
                last_error_code,
                receipt_metadata,
                updated_at,
                completed_at
            FROM public.deletion_component_receipts
            WHERE workspace_id = %s::uuid
              AND deletion_job_id = %s::uuid
            ORDER BY component ASC
            """,
            (workspace_id, deletion_job_id),
        )
        receipts_by_comp = {
            DeletionComponent(str(r[0])): DeletionComponentReceiptItem(
                component=DeletionComponent(str(r[0])),
                status=DeletionComponentStatus(str(r[1])),
                attempt=int(r[2]),
                purged_count=int(r[3]),
                last_error_code=str(r[4]) if r[4] is not None else None,
                receipt_metadata=dict(r[5] or {}),
                updated_at=r[6],
                completed_at=r[7],
            )
            for r in cur.fetchall()
        }
        ordered_receipts = [
            receipts_by_comp[c]
            for c in DELETION_COMPONENT_ORDER
            if c in receipts_by_comp
        ]
        job_status = DeletionJobStatus(str(row[3]))
        return DeletionJobStatusResponse(
            deletion_job_id=str(row[0]),
            workspace_id=str(row[1]),
            source_resource_id=str(row[2]),
            status=job_status,
            user_visible_state=project_user_visible_deletion_state(job_status),
            fence_active=job_status
            in (
                DeletionJobStatus.PENDING,
                DeletionJobStatus.RUNNING,
                DeletionJobStatus.PARTIAL,
                DeletionJobStatus.SUCCEEDED,
            ),
            checkpoint=dict(row[4] or {}),
            failure_summary=dict(row[5] or {}),
            components=ordered_receipts,
            requested_by=str(row[6]) if row[6] is not None else None,
            requested_at=row[7],
            completed_at=row[8],
        )

    @staticmethod
    def _cancel_queued_work_for_resource(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str,
        include_running: bool,
        now: datetime,
    ) -> tuple[int, int]:
        job_statuses = (
            ("PENDING", "RETRY_WAIT", "RUNNING")
            if include_running
            else ("PENDING", "RETRY_WAIT")
        )
        run_statuses = ("PENDING", "RUNNING") if include_running else ("PENDING",)

        cur.execute(
            """
            UPDATE public.jobs j
            SET status = 'CANCELED',
                lease_owner = NULL,
                lease_token = NULL,
                lease_until = NULL,
                error_class = 'CANCELED_BY_FENCE',
                error_code = 'DELETION_FENCE_ACTIVE',
                updated_at = %s,
                completed_at = %s
            WHERE j.workspace_id = %s::uuid
              AND j.status = ANY(%s::text[])
              AND (
                  EXISTS (
                      SELECT 1
                      FROM public.sync_runs sr
                      WHERE sr.workspace_id = j.workspace_id
                        AND sr.id = j.target_sync_run_id
                        AND sr.source_resource_id = %s::uuid
                  )
                  OR (j.input_manifest ->> 'source_resource_id') = %s
              )
            """,
            (now, now, workspace_id, list(job_statuses), source_resource_id, source_resource_id),
        )
        canceled_jobs = int(cur.rowcount or 0)

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
              AND status = ANY(%s::text[])
            """,
            (now, now, workspace_id, source_resource_id, list(run_statuses)),
        )
        canceled_runs = int(cur.rowcount or 0)
        return canceled_jobs, canceled_runs

    @staticmethod
    def _insert_idempotency_receipt(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        deletion_job_id: str,
        response: DeletionConfirmAcceptedResponse,
        now: datetime,
    ) -> None:
        resp_body = response.model_dump(mode="json")
        assert_no_forbidden_raw_keys(resp_body, field_name="api_idempotency_records.response_body")
        cur.execute(
            """
            INSERT INTO public.api_idempotency_records (
                workspace_id, operation_scope, idempotency_key,
                request_fingerprint, target_sync_run_id, target_deletion_job_id,
                response_status, response_body, created_at, expires_at
            ) VALUES (
                %s::uuid, 'CONFIRM_SOURCE_DELETION', %s,
                %s, NULL, %s::uuid,
                202, %s, %s, %s
            )
            ON CONFLICT (workspace_id, operation_scope, idempotency_key) DO NOTHING
            """,
            (
                workspace_id,
                idempotency_key,
                request_fingerprint,
                deletion_job_id,
                Jsonb(resp_body),
                now,
                now + timedelta(hours=24),
            ),
        )

    @staticmethod
    def _assert_live_workspace_membership(
        cur: psycopg.Cursor, access: WorkspaceAccessContext
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.workspace_memberships
            WHERE workspace_id = %s::uuid
              AND user_id = %s::uuid
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (access.workspace_id, access.user_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceMembershipDeniedError("Workspace membership is no longer active.")

    @staticmethod
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
        assert_no_forbidden_raw_keys(redacted_metadata, field_name="redacted_metadata")
        assert_no_forbidden_secret_keys(redacted_metadata, field_name="audit_events.redacted_metadata")
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id, workspace_id, actor_type, actor_id,
                action, target_type, target_id, redacted_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s, %s::uuid, %s, %s
            )
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                actor_type.value,
                _parse_uuid_or_none(actor_id),
                action,
                target_type,
                target_id,
                Jsonb(redacted_metadata),
                created_at,
            ),
        )

    @staticmethod
    def _insert_outbox_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        dedupe_key: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(payload, field_name="outbox_events.payload")
        assert_no_forbidden_secret_keys(payload, field_name="outbox_events.payload")
        cur.execute(
            """
            INSERT INTO public.outbox_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                dedupe_key, payload, status, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s, %s::uuid,
                %s, %s, 'PENDING', %s
            )
            ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                event_type,
                aggregate_type,
                aggregate_id,
                dedupe_key,
                Jsonb(payload),
                created_at,
            ),
        )


__all__ = [
    "ComponentFaultInjector",
    "ConnectionFactory",
    "PostgresDeletionService",
]
