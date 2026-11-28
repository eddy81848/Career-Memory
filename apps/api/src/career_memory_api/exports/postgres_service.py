"""PostgreSQL 18 Export Service with Same-Operation Plan C Refetch & EXPORT-02 Race Fence.

Implements M4 Wave 3A (T-407, EXPORT-02, REF-07b/d/e/f/g):
- Strict eligibility enforcement (APPROVED claims only, exact version match, policy gate).
- Live current-operation Plan C ``EXPORT_DOWNLOAD`` refetch on BOTH ``POST /v1/exports``
  and ``GET /v1/exports/{export_id}``.
- 3-phase ``PENDING -> live refetch & render -> FOR UPDATE publish fence -> READY / INVALIDATED``
  with injectable ``before_publish_hook`` & ``before_commit_hook`` so build-time invalidation
  (``EXPORT-02`` / ``REF-07e``) marks the export ``INVALIDATED`` with ``artifact_payload = NULL``
  and NEVER publishes or leaks stale artifacts.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.claims.verification import (
    ClaimVerificationEvaluation,
    VerificationCalculator,
)
from career_memory_api.exports.models import (
    EXPORT_POLICY_VERSION,
    CreateExportRequest,
    DeleteExportResponse,
    ExportDetailResponse,
    ExportError,
    ExportListResponse,
    ExportSummaryItem,
    utc_now,
)
from career_memory_api.exports.renderer import (
    ExportAtomCitation,
    ExportClaimRenderItem,
    ExportRenderInput,
    render_export_artifact,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.llm_gate import assert_no_raw_text_or_secret_keys
from career_memory_api.resolver.models import (
    ExportGatePrecondition,
    GateOperationKind,
    ResolutionStatus,
    format_utc_timestamp,
)
from career_memory_api.resolver.policy import evaluate_target_policy
from career_memory_api.resolver.service import (
    RefetchResolverService,
    verify_required_supports_for_gate,
)
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ExportFormat,
    ExportPolicy,
    ExportStatus,
    ExportVerificationClass,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforePublishHook = Callable[..., None]
BeforeCommitHook = Callable[..., None]


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _invoke_hook(hook: Callable[..., None] | None, *, action: str, arg2: Any = None) -> None:
    if hook is None:
        return
    sig = inspect.signature(hook)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(params) >= 2:
        hook(action, arg2)
    elif len(params) == 1:
        hook(action)
    else:
        hook()


def compute_export_input_hash(
    *,
    claim_versions: dict[str, int],
    export_format: ExportFormat,
    export_policy: ExportPolicy,
    policy_version: str,
) -> str:
    """Compute deterministic SHA-256 over sorted claim_id:version pairs and export policy."""
    canonical = json.dumps(
        {
            "claims": [f"{cid}:{claim_versions[cid]}" for cid in sorted(claim_versions.keys())],
            "export_policy": export_policy.value,
            "format": export_format.value,
            "policy_version": policy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class PostgresExportService:
    """PostgreSQL 18 transactional service for Plan C zero-quote Markdown/JSON exports."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        resolver: RefetchResolverService | None = None,
        verification_calculator: VerificationCalculator | None = None,
        before_publish_hook: BeforePublishHook | None = None,
        before_commit_hook: BeforeCommitHook | None = None,
        now_fn: Callable[[], datetime] | None = None,
        policy_version: str = EXPORT_POLICY_VERSION,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._resolver = resolver
        self._verification = verification_calculator or VerificationCalculator()
        self._before_publish_hook = before_publish_hook
        self._before_commit_hook = before_commit_hook
        self._now_fn = now_fn or utc_now
        self._policy_version = policy_version

    # =========================================================================
    # 1. GET /v1/exports
    # =========================================================================
    def list_exports(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str | None = None,
        include_deleted: bool = False,
    ) -> ExportListResponse:
        parsed_res_id: str | None = None
        if source_resource_id is not None:
            parsed_res_id = _parse_uuid_or_none(source_resource_id)
            if parsed_res_id is None:
                raise ExportError(
                    status_code=422,
                    code="INVALID_SOURCE_RESOURCE_ID",
                    message="Query parameter 'source_resource_id' must be a valid UUID.",
                )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                where = ["e.workspace_id = %s::uuid"]
                params: list[Any] = [access.workspace_id]
                if parsed_res_id is not None:
                    where.append("e.source_resource_id = %s::uuid")
                    params.append(parsed_res_id)
                if not include_deleted:
                    where.append("e.status <> 'DELETED'")

                cur.execute(
                    f"""
                    SELECT
                        e.id::text,
                        e.workspace_id::text,
                        e.source_resource_id::text,
                        e.format,
                        e.export_policy,
                        e.status,
                        e.policy_version,
                        e.input_hash,
                        e.invalidation_reason,
                        e.created_at,
                        e.completed_at,
                        e.invalidated_at,
                        e.deleted_at,
                        (
                            SELECT COUNT(*)
                            FROM public.export_claims ec
                            WHERE ec.workspace_id = e.workspace_id
                              AND ec.export_id = e.id
                        )::integer
                    FROM public.exports e
                    WHERE {' AND '.join(where)}
                    ORDER BY e.created_at DESC, e.id DESC
                    """,
                    params,
                )
                items = [
                    ExportSummaryItem(
                        export_id=str(r[0]),
                        workspace_id=str(r[1]),
                        source_resource_id=str(r[2]) if r[2] is not None else None,
                        format=ExportFormat(str(r[3])),
                        export_policy=ExportPolicy(str(r[4])),
                        status=ExportStatus(str(r[5])),
                        policy_version=str(r[6]),
                        input_hash=str(r[7]),
                        invalidation_reason=str(r[8]) if r[8] is not None else None,
                        created_at=r[9],
                        completed_at=r[10],
                        invalidated_at=r[11],
                        deleted_at=r[12],
                        claim_count=int(r[13] or 0),
                    )
                    for r in cur.fetchall()
                ]
                return ExportListResponse(items=items)

    # =========================================================================
    # 2. POST /v1/exports (3-Phase Race-Fenced Export Build & Publish)
    # =========================================================================
    def create_export(
        self,
        access: WorkspaceAccessContext,
        *,
        idempotency_key: str,
        request: CreateExportRequest,
    ) -> ExportDetailResponse:
        claim_uuids: list[str] = []
        for raw_cid in request.claim_ids:
            parsed = _parse_uuid_or_none(raw_cid)
            if parsed is None:
                raise WorkspaceResourceNotFoundError()
            claim_uuids.append(parsed)

        parsed_res_id: str | None = None
        if request.source_resource_id is not None:
            parsed_res_id = _parse_uuid_or_none(request.source_resource_id)
            if parsed_res_id is None:
                raise WorkspaceResourceNotFoundError()

        expected_versions_norm: dict[str, int] = {}
        if request.expected_claim_versions:
            for k, v in request.expected_claim_versions.items():
                pk = _parse_uuid_or_none(k)
                if pk is not None:
                    expected_versions_norm[pk] = int(v)

        ts = self._now_fn()

        # Phase 1 (Tx 1): Check idempotency, validate eligibility, compute pre-build epochs, insert PENDING
        existing_replay_id: str | None = None
        export_id = str(uuid.uuid4())
        pre_evals: dict[str, ClaimVerificationEvaluation] = {}
        claim_versions: dict[str, int] = {}
        required_unit_ids: set[str] = set()
        input_hash = ""

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # Validate every requested claim exists in workspace and passes strict eligibility
                for cid in claim_uuids:
                    cur.execute(
                        """
                        SELECT id::text, version, status
                        FROM public.claims
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (access.workspace_id, cid),
                    )
                    crow = cur.fetchone()
                    if crow is None:
                        raise WorkspaceResourceNotFoundError()
                    ev = self._verification.evaluate_claim(
                        cur, workspace_id=access.workspace_id, claim_id=cid
                    )
                    self._assert_claim_eligible_for_export(
                        evaluation=ev,
                        expected_version=expected_versions_norm.get(cid),
                        export_policy=request.export_policy,
                    )
                    pre_evals[cid] = ev
                    claim_versions[cid] = ev.claim_version
                    required_unit_ids.update(ev.required_source_unit_ids)

                input_hash = compute_export_input_hash(
                    claim_versions=claim_versions,
                    export_format=request.format,
                    export_policy=request.export_policy,
                    policy_version=self._policy_version,
                )

                # Idempotency check by (workspace_id, idempotency_key)
                cur.execute(
                    """
                    SELECT id::text, input_hash
                    FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND idempotency_key = %s
                    LIMIT 1
                    """,
                    (access.workspace_id, idempotency_key),
                )
                idem_row = cur.fetchone()
                if idem_row is not None:
                    if str(idem_row[1]) != input_hash:
                        raise ExportError(
                            status_code=409,
                            code="IDEMPOTENCY_CONFLICT",
                            message="Idempotency-Key was already used with a different export payload.",
                        )
                    existing_replay_id = str(idem_row[0])
                else:
                    if parsed_res_id is not None:
                        cur.execute(
                            """
                            SELECT 1
                            FROM public.source_resources
                            WHERE workspace_id = %s::uuid
                              AND id = %s::uuid
                            """,
                            (access.workspace_id, parsed_res_id),
                        )
                        if cur.fetchone() is None:
                            raise WorkspaceResourceNotFoundError()

                    initial_manifest = {
                        "export_id": export_id,
                        "format": request.format.value,
                        "export_policy": request.export_policy.value,
                        "policy_version": self._policy_version,
                        "claim_versions": claim_versions,
                        "excludes_raw_source_quotes": True,
                    }
                    cur.execute(
                        """
                        INSERT INTO public.exports (
                            id, workspace_id, source_resource_id, requested_by_user_id,
                            format, export_policy, status, idempotency_key,
                            input_hash, policy_version, manifest, artifact_payload,
                            created_at, updated_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, 'PENDING', %s,
                            %s, %s, %s, NULL,
                            %s, %s
                        )
                        """,
                        (
                            export_id,
                            access.workspace_id,
                            parsed_res_id,
                            _parse_uuid_or_none(access.user_id),
                            request.format.value,
                            request.export_policy.value,
                            idempotency_key,
                            input_hash,
                            self._policy_version,
                            Jsonb(initial_manifest),
                            ts,
                            ts,
                        ),
                    )
                    for idx, cid in enumerate(claim_uuids):
                        ev = pre_evals[cid]
                        snapshot = {
                            "support_graph_epoch": ev.support_graph_epoch,
                            "required_content_unit_ids": list(ev.required_source_unit_ids),
                        }
                        assert_no_forbidden_raw_keys(snapshot, field_name="support_manifest_snapshot")
                        cur.execute(
                            """
                            INSERT INTO public.export_claims (
                                id, workspace_id, export_id, claim_id, claim_version,
                                computed_verification_at_export, support_manifest_snapshot,
                                ordinal, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s,
                                %s, %s, %s, %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                access.workspace_id,
                                export_id,
                                cid,
                                ev.claim_version,
                                ev.computed_verification.value,
                                Jsonb(snapshot),
                                idx,
                                ts,
                            ),
                        )

        if existing_replay_id is not None:
            return self.get_export(access, export_id=existing_replay_id)

        # Phase 2: Current-operation live Plan C refetch (REF-07b / REF-07d) + Zero-Quote Render
        sorted_units = sorted(required_unit_ids)
        checked_at_by_unit: dict[str, datetime] = {}
        matched_unit_ids: frozenset[str] = frozenset()
        verif_calc_at = self._now_fn()

        if sorted_units:
            if self._resolver is None:
                self._mark_export_invalidated(
                    access,
                    export_id=export_id,
                    reason="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                )
                raise ExportError(
                    status_code=409,
                    code="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                    message=(
                        "Live Plan C refetch in the current operation is required for export; "
                        "historical verification is insufficient."
                    ),
                    details={"export_id": export_id},
                )
            op_id = f"export-build-{export_id}-{uuid.uuid4()}"
            first_ver = claim_versions[claim_uuids[0]]
            with self._resolver.open_operation_session(access, operation_id=op_id) as session:
                gate_res = verify_required_supports_for_gate(
                    session,
                    sorted_units,
                    operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
                    resolver=self._resolver,
                    export_precondition=ExportGatePrecondition(
                        claim_version_approved=True,
                        requested_claim_version=first_ver,
                        approved_claim_version=first_ver,
                    ),
                    clock=self._now_fn,
                )
            if not gate_res.allowed:
                err = gate_res.error
                inv_reason = gate_res.reason_code or (
                    err.code if err is not None else "REFETCH_FAILED"
                )
                self._mark_export_invalidated(
                    access,
                    export_id=export_id,
                    reason=inv_reason,
                )
                raise ExportError(
                    status_code=err.http_status if err is not None else 409,
                    code=err.code if err is not None else "EXPORT_INVALIDATED",
                    message=(
                        err.message if err is not None else "Live Plan C refetch blocked export."
                    ),
                    retryable=err.retryable if err is not None else False,
                    details={
                        "export_id": export_id,
                        "reason_code": inv_reason,
                        "blocked_unit_id": gate_res.blocked_unit_id,
                    },
                )
            verif_calc_at = gate_res.checked_at
            matched_unit_ids = frozenset(
                m.content_unit_id
                for m in gate_res.resolutions
                if m.status is ResolutionStatus.MATCHED
            )
            for meta in gate_res.resolutions:
                checked_at_by_unit[meta.content_unit_id] = meta.checked_at

        # Load non-raw render inputs and render deterministically in memory
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                render_items = self._build_render_items(
                    cur,
                    workspace_id=access.workspace_id,
                    claim_ids=claim_uuids,
                    evals=pre_evals,
                    checked_at_by_unit=checked_at_by_unit,
                    default_checked_at=verif_calc_at,
                )

        rendered = render_export_artifact(
            ExportRenderInput(
                export_id=export_id,
                workspace_id=access.workspace_id,
                format=request.format,
                export_policy=request.export_policy,
                policy_version=self._policy_version,
                generated_at=self._now_fn(),
                verification_calculated_at=verif_calc_at,
                claims=render_items,
            )
        )

        # Injectable TOCTOU window between build/refetch and final publish commit (EXPORT-02 / REF-07e)
        _invoke_hook(
            self._before_publish_hook, action="before_export_publish", arg2=export_id
        )

        # Phase 3 (Tx 2): Atomic FOR UPDATE publish fence (EXPORT-02 / REF-07e)
        invalidated_reason: str | None = None
        publish_ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # Lock export row + all exported claims & support graphs FOR UPDATE
                cur.execute(
                    """
                    SELECT status, invalidation_reason
                    FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    FOR UPDATE
                    """,
                    (access.workspace_id, export_id),
                )
                exp_row = cur.fetchone()
                if exp_row is None:
                    raise WorkspaceResourceNotFoundError()
                current_exp_status = ExportStatus(str(exp_row[0]))

                self._lock_claims_and_supports_for_export(
                    cur, workspace_id=access.workspace_id, claim_ids=claim_uuids
                )

                if current_exp_status is not ExportStatus.PENDING:
                    invalidated_reason = (
                        str(exp_row[1]) if exp_row[1] is not None else "SUPPORT_INVALIDATED_DURING_BUILD"
                    )
                else:
                    # Re-check every claim's APPROVED status, exact version, support_graph_epoch, policy, and deletion fence
                    for cid in claim_uuids:
                        try:
                            live_ev = self._verification.evaluate_claim(
                                cur,
                                workspace_id=access.workspace_id,
                                claim_id=cid,
                                current_op_matched_unit_ids=matched_unit_ids,
                            )
                        except LookupError:
                            invalidated_reason = "SUPPORT_INVALIDATED_DURING_BUILD"
                            break

                        pre_ev = pre_evals[cid]
                        if (
                            live_ev.claim_status is not ClaimStatus.APPROVED
                            or live_ev.claim_version != pre_ev.claim_version
                            or live_ev.support_graph_epoch != pre_ev.support_graph_epoch
                            or live_ev.deletion_fenced
                            or live_ev.policy_blocked
                            or live_ev.computed_verification is ClaimVerification.UNVERIFIED
                            or (
                                request.export_policy is ExportPolicy.SOURCE_VERIFIED_ONLY
                                and live_ev.computed_verification
                                is not ClaimVerification.SOURCE_VERIFIED
                            )
                        ):
                            invalidated_reason = "SUPPORT_INVALIDATED_DURING_BUILD"
                            break

                    # Re-check live Plan C policy fence on all required content_units (REF-07e)
                    if invalidated_reason is None and self._resolver is not None:
                        for uid in sorted_units:
                            live_target = self._resolver.metadata_store.check_policy_fence_current(
                                access, uid
                            )
                            if live_target is None or not evaluate_target_policy(live_target)[0]:
                                invalidated_reason = "SUPPORT_INVALIDATED_DURING_BUILD"
                                break

                if invalidated_reason is not None:
                    # EXPORT-02 / REF-07e: Mark INVALIDATED, emit ZERO artifact_payload, fail closed!
                    cur.execute(
                        """
                        UPDATE public.exports
                        SET status = 'INVALIDATED',
                            invalidation_reason = %s,
                            invalidated_at = %s,
                            completed_at = NULL,
                            artifact_payload = NULL,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (invalidated_reason, publish_ts, publish_ts, access.workspace_id, export_id),
                    )
                    self._insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=access.user_id,
                        action="EXPORT_INVALIDATED",
                        target_type="EXPORT",
                        target_id=export_id,
                        redacted_metadata={
                            "invalidation_reason": invalidated_reason,
                            "claim_versions": claim_versions,
                        },
                        created_at=publish_ts,
                    )
                else:
                    assert_no_forbidden_raw_keys(rendered.manifest, field_name="exports.manifest")
                    assert_no_forbidden_raw_keys(
                        rendered.artifact_payload, field_name="exports.artifact_payload"
                    )
                    assert_no_raw_text_or_secret_keys(
                        rendered.artifact_payload, context="exports.artifact_payload"
                    )
                    cur.execute(
                        """
                        UPDATE public.exports
                        SET status = 'READY',
                            manifest = %s,
                            artifact_payload = %s,
                            completed_at = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        RETURNING created_at
                        """,
                        (
                            Jsonb(rendered.manifest),
                            Jsonb(rendered.artifact_payload),
                            publish_ts,
                            publish_ts,
                            access.workspace_id,
                            export_id,
                        ),
                    )
                    created_row = cur.fetchone()
                    assert created_row is not None
                    created_at_db = created_row[0]

                    self._insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.USER,
                        actor_id=access.user_id,
                        action="EXPORT_CREATED",
                        target_type="EXPORT",
                        target_id=export_id,
                        redacted_metadata={
                            "format": request.format.value,
                            "export_policy": request.export_policy.value,
                            "policy_version": self._policy_version,
                            "claim_versions": claim_versions,
                            "excludes_raw_source_quotes": True,
                        },
                        created_at=publish_ts,
                    )
                    self._insert_outbox_event(
                        cur,
                        workspace_id=access.workspace_id,
                        event_type="EXPORT_READY",
                        aggregate_type="EXPORT",
                        aggregate_id=export_id,
                        dedupe_key=f"export_ready:{access.workspace_id}:{export_id}",
                        payload={
                            "export_id": export_id,
                            "format": request.format.value,
                            "export_policy": request.export_policy.value,
                            "claim_versions": claim_versions,
                        },
                        created_at=publish_ts,
                    )
                    _invoke_hook(self._before_commit_hook, action="publish_export", arg2=cur)
                    self._assert_live_workspace_membership(cur, access)

        if invalidated_reason is not None:
            raise ExportError(
                status_code=409,
                code="EXPORT_INVALIDATED",
                message="Export was invalidated because claim or support provenance changed during build.",
                details={
                    "export_id": export_id,
                    "invalidation_reason": invalidated_reason,
                },
            )

        return ExportDetailResponse(
            export_id=export_id,
            workspace_id=access.workspace_id,
            source_resource_id=parsed_res_id,
            format=request.format,
            export_policy=request.export_policy,
            status=ExportStatus.READY,
            policy_version=self._policy_version,
            input_hash=input_hash,
            manifest=rendered.manifest,
            rendered_markdown=rendered.rendered_markdown,
            rendered_json=rendered.rendered_json,
            invalidation_reason=None,
            created_at=created_at_db,
            completed_at=publish_ts,
            invalidated_at=None,
            deleted_at=None,
        )

    # =========================================================================
    # 3. GET /v1/exports/{export_id} (Live EXPORT_DOWNLOAD Refetch & Recheck)
    # =========================================================================
    def get_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
    ) -> ExportDetailResponse:
        exp_uuid = _parse_uuid_or_none(export_id)
        if exp_uuid is None:
            raise WorkspaceResourceNotFoundError()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT
                        id::text,
                        workspace_id::text,
                        source_resource_id::text,
                        format,
                        export_policy,
                        status,
                        policy_version,
                        input_hash,
                        manifest,
                        invalidation_reason,
                        created_at,
                        completed_at,
                        invalidated_at,
                        deleted_at
                    FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (access.workspace_id, exp_uuid),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceResourceNotFoundError()

                status = ExportStatus(str(row[5]))
                if status is ExportStatus.DELETED:
                    raise WorkspaceResourceNotFoundError()
                if status is ExportStatus.INVALIDATED:
                    raise ExportError(
                        status_code=409,
                        code="EXPORT_INVALIDATED",
                        message="Export has been invalidated and can no longer be downloaded.",
                        details={
                            "export_id": exp_uuid,
                            "invalidation_reason": str(row[9]) if row[9] is not None else "EXPORT_INVALIDATED",
                        },
                    )
                if status is ExportStatus.FAILED:
                    raise ExportError(
                        status_code=409,
                        code="EXPORT_FAILED",
                        message="Export failed and cannot be downloaded.",
                        details={"export_id": exp_uuid},
                    )

                export_format = ExportFormat(str(row[3]))
                export_policy = ExportPolicy(str(row[4]))
                policy_version = str(row[6])
                input_hash = str(row[7])
                source_resource_id = str(row[2]) if row[2] is not None else None
                created_at = row[10]
                completed_at = row[11]

                if status is ExportStatus.PENDING:
                    return ExportDetailResponse(
                        export_id=exp_uuid,
                        workspace_id=access.workspace_id,
                        source_resource_id=source_resource_id,
                        format=export_format,
                        export_policy=export_policy,
                        status=status,
                        policy_version=policy_version,
                        input_hash=input_hash,
                        manifest=dict(row[8] or {}),
                        rendered_markdown=None,
                        rendered_json=None,
                        invalidation_reason=None,
                        created_at=created_at,
                        completed_at=completed_at,
                        invalidated_at=row[12],
                        deleted_at=row[13],
                    )

                # READY: re-verify every exported claim & support_graph_epoch!
                cur.execute(
                    """
                    SELECT claim_id::text, claim_version, support_manifest_snapshot
                    FROM public.export_claims
                    WHERE workspace_id = %s::uuid
                      AND export_id = %s::uuid
                    ORDER BY ordinal ASC, id ASC
                    """,
                    (access.workspace_id, exp_uuid),
                )
                ec_rows = cur.fetchall()
                claim_ids = [str(r[0]) for r in ec_rows]
                live_evals: dict[str, ClaimVerificationEvaluation] = {}
                required_unit_ids: set[str] = set()
                stale_reason: str | None = None

                for ec_cid, ec_ver, ec_snap in ec_rows:
                    cid = str(ec_cid)
                    snap = dict(ec_snap or {})
                    expected_epoch = snap.get("support_graph_epoch")
                    try:
                        ev = self._verification.evaluate_claim(
                            cur, workspace_id=access.workspace_id, claim_id=cid
                        )
                    except LookupError:
                        stale_reason = "CLAIM_MISSING_OR_DELETED"
                        break
                    if (
                        ev.claim_status is not ClaimStatus.APPROVED
                        or ev.claim_version != int(ec_ver)
                        or (expected_epoch is not None and ev.support_graph_epoch != expected_epoch)
                        or ev.deletion_fenced
                        or ev.policy_blocked
                        or ev.computed_verification is ClaimVerification.UNVERIFIED
                        or (
                            export_policy is ExportPolicy.SOURCE_VERIFIED_ONLY
                            and ev.computed_verification is not ClaimVerification.SOURCE_VERIFIED
                        )
                    ):
                        stale_reason = "SUPPORT_OR_CLAIM_INVALIDATED_BEFORE_DOWNLOAD"
                        break
                    live_evals[cid] = ev
                    required_unit_ids.update(ev.required_source_unit_ids)

        if stale_reason is not None:
            self._mark_export_invalidated(access, export_id=exp_uuid, reason=stale_reason)
            raise ExportError(
                status_code=409,
                code="EXPORT_INVALIDATED",
                message="Export invalidated because underlying claim or support state changed.",
                details={"export_id": exp_uuid, "invalidation_reason": stale_reason},
            )

        # Live EXPORT_DOWNLOAD Plan C refetch across all source-backed units
        sorted_units = sorted(required_unit_ids)
        checked_at_by_unit: dict[str, datetime] = {}
        verif_calc_at = self._now_fn()

        if sorted_units:
            if self._resolver is None:
                self._mark_export_invalidated(
                    access,
                    export_id=exp_uuid,
                    reason="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                )
                raise ExportError(
                    status_code=409,
                    code="REFETCH_REQUIRED_IN_CURRENT_OPERATION",
                    message="Live Plan C refetch is required on export download.",
                    details={"export_id": exp_uuid},
                )
            op_id = f"export-download-{exp_uuid}-{uuid.uuid4()}"
            first_ver = live_evals[claim_ids[0]].claim_version
            with self._resolver.open_operation_session(access, operation_id=op_id) as session:
                gate_res = verify_required_supports_for_gate(
                    session,
                    sorted_units,
                    operation_kind=GateOperationKind.EXPORT_DOWNLOAD,
                    resolver=self._resolver,
                    export_precondition=ExportGatePrecondition(
                        claim_version_approved=True,
                        requested_claim_version=first_ver,
                        approved_claim_version=first_ver,
                    ),
                    clock=self._now_fn,
                )
            if not gate_res.allowed:
                err = gate_res.error
                inv_reason = gate_res.reason_code or (
                    err.code if err is not None else "EXPORT_DOWNLOAD_REFETCH_FAILED"
                )
                self._mark_export_invalidated(access, export_id=exp_uuid, reason=inv_reason)
                raise ExportError(
                    status_code=err.http_status if err is not None else 409,
                    code=err.code if err is not None else "EXPORT_INVALIDATED",
                    message=(
                        err.message
                        if err is not None
                        else "Live Plan C refetch blocked export download."
                    ),
                    retryable=err.retryable if err is not None else False,
                    details={
                        "export_id": exp_uuid,
                        "reason_code": inv_reason,
                        "blocked_unit_id": gate_res.blocked_unit_id,
                    },
                )
            verif_calc_at = gate_res.checked_at
            for meta in gate_res.resolutions:
                checked_at_by_unit[meta.content_unit_id] = meta.checked_at

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                render_items = self._build_render_items(
                    cur,
                    workspace_id=access.workspace_id,
                    claim_ids=claim_ids,
                    evals=live_evals,
                    checked_at_by_unit=checked_at_by_unit,
                    default_checked_at=verif_calc_at,
                )

        rendered = render_export_artifact(
            ExportRenderInput(
                export_id=exp_uuid,
                workspace_id=access.workspace_id,
                format=export_format,
                export_policy=export_policy,
                policy_version=policy_version,
                generated_at=completed_at or created_at,
                verification_calculated_at=verif_calc_at,
                claims=render_items,
            )
        )
        return ExportDetailResponse(
            export_id=exp_uuid,
            workspace_id=access.workspace_id,
            source_resource_id=source_resource_id,
            format=export_format,
            export_policy=export_policy,
            status=ExportStatus.READY,
            policy_version=policy_version,
            input_hash=input_hash,
            manifest=rendered.manifest,
            rendered_markdown=rendered.rendered_markdown,
            rendered_json=rendered.rendered_json,
            invalidation_reason=None,
            created_at=created_at,
            completed_at=completed_at,
            invalidated_at=None,
            deleted_at=None,
        )

    # =========================================================================
    # 4. DELETE /v1/exports/{export_id}
    # =========================================================================
    def delete_export(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
        idempotency_key: str,
    ) -> DeleteExportResponse:
        exp_uuid = _parse_uuid_or_none(export_id)
        if exp_uuid is None:
            raise WorkspaceResourceNotFoundError()
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT status, deleted_at
                    FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    FOR UPDATE
                    """,
                    (access.workspace_id, exp_uuid),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceResourceNotFoundError()
                if ExportStatus(str(row[0])) is ExportStatus.DELETED:
                    return DeleteExportResponse(
                        export_id=exp_uuid,
                        status="DELETED",
                        deleted_at=row[1] or ts,
                    )

                cur.execute(
                    """
                    UPDATE public.exports
                    SET status = 'DELETED',
                        deleted_at = %s,
                        artifact_payload = NULL,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (ts, ts, access.workspace_id, exp_uuid),
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="EXPORT_DELETED",
                    target_type="EXPORT",
                    target_id=exp_uuid,
                    redacted_metadata={"idempotency_key": idempotency_key},
                    created_at=ts,
                )
                _invoke_hook(self._before_commit_hook, action="delete_export", arg2=cur)
                self._assert_live_workspace_membership(cur, access)
                return DeleteExportResponse(
                    export_id=exp_uuid,
                    status="DELETED",
                    deleted_at=ts,
                )

    # =========================================================================
    # Internal Helpers
    # =========================================================================
    @staticmethod
    def _assert_claim_eligible_for_export(
        *,
        evaluation: ClaimVerificationEvaluation,
        expected_version: int | None,
        export_policy: ExportPolicy,
    ) -> None:
        if evaluation.deletion_fenced:
            raise ExportError(
                status_code=409,
                code="DELETION_PENDING",
                message="Export blocked while a linked source resource deletion is active.",
                details={"claim_id": evaluation.claim_id},
            )
        if evaluation.policy_blocked:
            raise ExportError(
                status_code=403,
                code="SOURCE_POLICY_BLOCKED",
                message="Export blocked because linked source provenance is policy-blocked.",
                details={"claim_id": evaluation.claim_id},
            )
        if expected_version is not None and evaluation.claim_version != expected_version:
            raise ExportError(
                status_code=409,
                code="EXPORT_CLAIM_VERSION_MISMATCH",
                message=(
                    f"Requested claim {evaluation.claim_id} version {expected_version} does not "
                    f"match current version {evaluation.claim_version}."
                ),
                details={
                    "claim_id": evaluation.claim_id,
                    "expected_version": expected_version,
                    "actual_version": evaluation.claim_version,
                },
            )
        if evaluation.claim_status is not ClaimStatus.APPROVED:
            raise ExportError(
                status_code=409,
                code="EXPORT_CLAIM_NOT_APPROVED",
                message=(
                    f"Only APPROVED claims may be exported (claim {evaluation.claim_id} is "
                    f"{evaluation.claim_status.value})."
                ),
                details={
                    "claim_id": evaluation.claim_id,
                    "claim_status": evaluation.claim_status.value,
                },
            )
        if (
            evaluation.computed_verification is ClaimVerification.UNVERIFIED
            or evaluation.unsupported_atom_count > 0
            or evaluation.stale_atom_count > 0
            or evaluation.atom_count == 0
        ):
            raise ExportError(
                status_code=409,
                code="SUPPORT_INCOMPLETE",
                message=(
                    f"Claim {evaluation.claim_id} is UNVERIFIED or has incomplete/stale support "
                    "and cannot be exported."
                ),
                details={"claim_id": evaluation.claim_id},
            )
        if (
            export_policy is ExportPolicy.SOURCE_VERIFIED_ONLY
            and evaluation.computed_verification is not ClaimVerification.SOURCE_VERIFIED
        ):
            raise ExportError(
                status_code=409,
                code="EXPORT_POLICY_DISALLOWS_VERIFICATION_LEVEL",
                message=(
                    f"Export policy SOURCE_VERIFIED_ONLY disallows claim {evaluation.claim_id} "
                    f"with computed_verification={evaluation.computed_verification.value}."
                ),
                details={
                    "claim_id": evaluation.claim_id,
                    "computed_verification": evaluation.computed_verification.value,
                    "export_policy": export_policy.value,
                },
            )

    def _mark_export_invalidated(
        self,
        access: WorkspaceAccessContext,
        *,
        export_id: str,
        reason: str,
    ) -> None:
        ts = self._now_fn()
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                cur.execute(
                    """
                    UPDATE public.exports
                    SET status = 'INVALIDATED',
                        invalidation_reason = %s,
                        invalidated_at = %s,
                        completed_at = NULL,
                        artifact_payload = NULL,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                      AND status IN ('PENDING', 'READY')
                    """,
                    (reason, ts, ts, access.workspace_id, export_id),
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=access.user_id,
                    action="EXPORT_INVALIDATED",
                    target_type="EXPORT",
                    target_id=export_id,
                    redacted_metadata={"invalidation_reason": reason},
                    created_at=ts,
                )

    def _build_render_items(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_ids: list[str],
        evals: dict[str, ClaimVerificationEvaluation],
        checked_at_by_unit: dict[str, datetime],
        default_checked_at: datetime,
    ) -> tuple[ExportClaimRenderItem, ...]:
        items: list[ExportClaimRenderItem] = []
        for cid in claim_ids:
            cur.execute(
                """
                SELECT claim_text, version
                FROM public.claims
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (workspace_id, cid),
            )
            crow = cur.fetchone()
            assert crow is not None
            claim_text = str(crow[0])
            claim_ver = int(crow[1])
            ev = evals[cid]

            cur.execute(
                """
                SELECT id::text, ordinal, atom_type, atom_text, status
                FROM public.claim_atoms
                WHERE workspace_id = %s::uuid
                  AND claim_id = %s::uuid
                ORDER BY ordinal ASC, id ASC
                """,
                (workspace_id, cid),
            )
            arows = cur.fetchall()
            aids = [str(r[0]) for r in arows]

            cits_by_atom: dict[str, list[dict[str, Any]]] = {aid: [] for aid in aids}
            if aids:
                cur.execute(
                    """
                    SELECT
                        cas.claim_atom_id::text,
                        sr.canonical_url,
                        rev.id::text,
                        rev.content_hash,
                        rev.observed_at,
                        cu.id::text,
                        cu.unit_type,
                        cu.anchor,
                        fs.support_metadata
                    FROM public.claim_atom_support cas
                    INNER JOIN public.facts f
                        ON f.workspace_id = cas.workspace_id
                       AND f.id = cas.fact_id
                    INNER JOIN public.fact_support fs
                        ON fs.workspace_id = f.workspace_id
                       AND fs.fact_id = f.id
                    INNER JOIN public.content_units cu
                        ON cu.workspace_id = fs.workspace_id
                       AND cu.id = fs.content_unit_id
                    INNER JOIN public.source_object_revisions rev
                        ON rev.workspace_id = cu.workspace_id
                       AND rev.id = cu.source_object_revision_id
                    INNER JOIN public.source_objects so
                        ON so.workspace_id = rev.workspace_id
                       AND so.id = rev.source_object_id
                    INNER JOIN public.source_resources sr
                        ON sr.workspace_id = so.workspace_id
                       AND sr.id = so.source_resource_id
                    WHERE cas.workspace_id = %s::uuid
                      AND cas.claim_atom_id = ANY(%s::uuid[])
                      AND cas.status = 'ACTIVE'
                      AND cas.support_type = 'DIRECT'
                      AND f.status = 'SUPPORTED'
                      AND fs.status = 'ACTIVE'
                      AND fs.support_type = 'DIRECT'
                      AND cu.policy_state = 'ACTIVE'
                      AND NOT rev.is_tombstone
                    ORDER BY rev.id ASC, cu.id ASC
                    """,
                    (workspace_id, aids),
                )
                seen_per_atom: dict[str, set[tuple[str, str]]] = {}
                for r in cur.fetchall():
                    aid = str(r[0])
                    rev_id = str(r[2])
                    cu_id = str(r[5])
                    dedupe = (rev_id, cu_id)
                    if dedupe in seen_per_atom.setdefault(aid, set()):
                        continue
                    seen_per_atom[aid].add(dedupe)
                    anchor = dict(r[7] or {})
                    sm = dict(r[8] or {})
                    locator = (
                        dict(sm["verified_locator"])
                        if isinstance(sm.get("verified_locator"), dict)
                        else anchor
                    )
                    checked_dt = checked_at_by_unit.get(cu_id, default_checked_at)
                    cits_by_atom.setdefault(aid, []).append(
                        {
                            "canonical_url": str(r[1]),
                            "source_object_revision_id": rev_id,
                            "revision_content_hash": str(r[3]),
                            "content_unit_id": cu_id,
                            "unit_type": str(r[6]),
                            "locator": locator,
                            "observed_at": format_utc_timestamp(r[4]),
                            "checked_at": format_utc_timestamp(checked_dt),
                        }
                    )

            atoms_out = tuple(
                ExportAtomCitation(
                    atom_id=str(ar[0]),
                    ordinal=int(ar[1]),
                    atom_type=ClaimAtomType(str(ar[2])),
                    atom_text=str(ar[3]),
                    status=ClaimAtomStatus(str(ar[4])),
                    citations=tuple(cits_by_atom.get(str(ar[0]), [])),
                )
                for ar in arows
            )
            items.append(
                ExportClaimRenderItem(
                    claim_id=cid,
                    version=claim_ver,
                    claim_text=claim_text,
                    computed_verification=ExportVerificationClass(ev.computed_verification.value),
                    atoms=atoms_out,
                )
            )
        return tuple(items)

    @staticmethod
    def _lock_claims_and_supports_for_export(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_ids: list[str],
    ) -> None:
        sorted_cids = sorted(set(claim_ids))
        cur.execute(
            """
            SELECT id
            FROM public.claims
            WHERE workspace_id = %s::uuid
              AND id = ANY(%s::uuid[])
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, sorted_cids),
        )
        cur.execute(
            """
            SELECT id
            FROM public.claim_atoms
            WHERE workspace_id = %s::uuid
              AND claim_id = ANY(%s::uuid[])
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, sorted_cids),
        )
        atom_ids = [str(r[0]) for r in cur.fetchall()]
        if atom_ids:
            cur.execute(
                """
                SELECT id, fact_id
                FROM public.claim_atom_support
                WHERE workspace_id = %s::uuid
                  AND claim_atom_id = ANY(%s::uuid[])
                ORDER BY id ASC
                FOR UPDATE
                """,
                (workspace_id, atom_ids),
            )
            fact_ids = sorted({str(r[1]) for r in cur.fetchall()})
            if fact_ids:
                cur.execute(
                    """
                    SELECT id
                    FROM public.facts
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (workspace_id, fact_ids),
                )
                cur.execute(
                    """
                    SELECT id
                    FROM public.fact_support
                    WHERE workspace_id = %s::uuid
                      AND fact_id = ANY(%s::uuid[])
                    ORDER BY id ASC
                    FOR UPDATE
                    """,
                    (workspace_id, fact_ids),
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
        assert_no_forbidden_raw_keys(payload, field_name="outbox_payload")
        cur.execute(
            """
            INSERT INTO public.outbox_events (
                workspace_id, event_type, aggregate_type, aggregate_id,
                dedupe_key, payload, status, created_at
            ) VALUES (
                %s::uuid, %s, %s, %s::uuid,
                %s, %s, 'PENDING', %s
            )
            """,
            (
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
    "BeforeCommitHook",
    "BeforePublishHook",
    "ConnectionFactory",
    "PostgresExportService",
    "compute_export_input_hash",
]
