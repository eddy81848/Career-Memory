"""PostgreSQL 18 Fact Extraction, Validation, Normalization, Contributions & /v1/facts Service.

Implements M4 Wave 2 (T-402, T-403, T-403A, ER-012, MVP API §6):
- Stage 2A FactCandidateExtractor persistence (`generation_runs.run_type = 'FACT_CANDIDATE_EXTRACTION'`)
  with idempotency by `(workspace_id, event_id, content_unit_id, extractor_version, input_hash)`.
- Stage 2B FactSupportValidator persistence (`generation_runs.run_type = 'FACT_SUPPORT_VALIDATION'`,
  strictly distinct `validator_run_id != extractor_run_id`) and `fact_support` (`extracted_span = NULL`).
- CanonicalFactNormalizer promotion (`SUPPORTED` ONLY with >=1 ACTIVE DIRECT, 0 ACTIVE CONTRADICTS,
  and verified identity gate) + deterministic canonical-key deduplication + MVP RULE Contributions.
- `/v1/facts*` HTTP read/mutation operations with `FOR UPDATE` row locks, `ER-012` non-overwrite
  versioned edit (`SUPERSEDED` + new `CANDIDATE` row with zero support inheritance + downstream
  `STALE`/`INVALIDATED` cascade), deletion fence checks, live membership re-verification before
  commit, and `before_commit_hook` for deterministic concurrency tests.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    ContentUnitType,
    SpanLocator,
)
from career_memory_api.conformance.ai_schemas import (
    SCHEMA_PACKAGE_VERSION,
    FactPromotionDecision,
)
from career_memory_api.facts.extractor import (
    FACT_CANDIDATE_SCHEMA_ID,
    FactCandidateExtractor,
    FactExtractionRequest,
    compute_extraction_input_hash,
)
from career_memory_api.facts.models import (
    DEFAULT_EXTRACTOR_MODEL,
    DEFAULT_EXTRACTOR_PROMPT_VERSION,
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_VALIDATOR_MODEL,
    DEFAULT_VALIDATOR_PROMPT_VERSION,
    DEFAULT_VALIDATOR_VERSION,
    ApproveFactRequest,
    ApproveFactResponse,
    ContributionRecord,
    EditFactRequest,
    EditFactResponse,
    EventFactPipelineResult,
    FactCandidateDraft,
    FactCursorPayload,
    FactError,
    FactExtractionUnitResult,
    FactInvalidationCounts,
    FactListResponse,
    FactPageInfo,
    FactSummaryItem,
    FactSupportItem,
    FactSupportsResponse,
    FactValidationOutcome,
    MetricDetailsModel,
    RejectFactRequest,
    RejectFactResponse,
    compute_facts_filter_hash,
    decode_and_verify_fact_cursor,
    encode_fact_cursor,
    extract_semantic_attributes,
    format_fact_etag,
    resolve_expected_version,
    utc_now,
)
from career_memory_api.facts.normalizer import (
    CanonicalFactNormalizer,
    ContributionCandidateInput,
    build_canonical_fact_key,
)
from career_memory_api.facts.validator import (
    FACT_SUPPORT_VALIDATION_SCHEMA_ID,
    FactSupportValidationRequest,
    FactSupportValidator,
    compute_validation_input_hash,
)
from career_memory_api.identities.models import IdentityAttributionBlockReason
from career_memory_api.identities.postgres_service import evaluate_identity_attribution
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.llm_gate import (
    BoundedStageRunner,
    ConnectionJobReference,
    HostedLlmApprovalConfig,
    LlmPolicyGate,
    LlmProviderMode,
    StageBudgetPolicy,
    StageExecutionAdapterPort,
    assert_no_raw_text_or_secret_keys,
)
from career_memory_api.resolver.models import RefetchRequestInput, ResolutionStatus
from career_memory_api.resolver.service import OperationRefetchSession, RefetchResolverService
from career_memory_api.schema_contracts.enums import (
    ContributionRole,
    ContributionStatus,
    FactStatus,
    FactType,
    GenerationRunStatus,
    GenerationRunType,
    IdentityType,
    LinkStatus,
    RecordOrigin,
    ResourceVisibility,
    ReviewActionType,
    ReviewTargetType,
    SupportType,
    SupportValidator,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[..., None]

_FACT_SELECT_COLUMNS = """
    f.id::text,
    f.workspace_id::text,
    f.event_id::text,
    f.version,
    f.supersedes_fact_id::text,
    f.fact_type,
    f.subject_identity_id::text,
    f.predicate,
    f.object_text,
    f.normalized_value,
    f.fact_metadata,
    f.status,
    f.extractor_version,
    f.created_at,
    f.updated_at
"""


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _compute_request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _invoke_before_commit_hook(
    hook: BeforeCommitHook | None,
    *,
    action: str,
    cur: psycopg.Cursor,
) -> None:
    if hook is None:
        return
    sig = inspect.signature(hook)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
    )
    if has_varargs or len(params) >= 2:
        hook(action, cur)
    elif len(params) == 1:
        hook(action)
    else:
        hook()


class PostgresFactService:
    """PostgreSQL 18 transactional service for M4 Wave 2 Facts pipeline and /v1/facts* API."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        resolver: RefetchResolverService | None = None,
        extractor: FactCandidateExtractor | None = None,
        validator: FactSupportValidator | None = None,
        normalizer: CanonicalFactNormalizer | None = None,
        before_commit_hook: BeforeCommitHook | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._resolver = resolver
        default_runner = BoundedStageRunner(
            policy_gate=LlmPolicyGate(approval_config=HostedLlmApprovalConfig())
        )
        self._extractor = extractor or FactCandidateExtractor(stage_runner=default_runner)
        self._validator = validator or FactSupportValidator(stage_runner=default_runner)
        self._normalizer = normalizer or CanonicalFactNormalizer()
        self._before_commit_hook = before_commit_hook
        self._now_fn = now_fn or utc_now

    # =========================================================================
    # 1. HTTP Read API: GET /v1/facts & GET /v1/facts/{fact_id}/supports
    # =========================================================================
    def list_facts(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str | None = None,
        status: FactStatus | None = None,
        fact_type: FactType | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> FactListResponse:
        if limit < 1 or limit > 100:
            raise FactError(
                status_code=422,
                code="INVALID_PAGINATION_LIMIT",
                message="Query parameter 'limit' must be between 1 and 100.",
            )
        parsed_event_id: str | None = None
        if event_id is not None:
            parsed_event_id = _parse_uuid_or_none(event_id)
            if parsed_event_id is None:
                raise FactError(
                    status_code=422,
                    code="INVALID_EVENT_ID",
                    message="Query parameter 'event_id' must be a valid UUID.",
                )

        filter_hash = compute_facts_filter_hash(
            event_id=parsed_event_id,
            status=status,
            fact_type=fact_type,
            include_superseded=include_superseded,
        )
        decoded_cursor: FactCursorPayload | None = None
        if cursor is not None:
            decoded_cursor = decode_and_verify_fact_cursor(
                cursor,
                expected_workspace_id=access.workspace_id,
                expected_filter_hash=filter_hash,
            )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                where_clauses = ["f.workspace_id = %s::uuid"]
                params: list[Any] = [access.workspace_id]

                if parsed_event_id is not None:
                    where_clauses.append("f.event_id = %s::uuid")
                    params.append(parsed_event_id)
                if status is not None:
                    where_clauses.append("f.status = %s")
                    params.append(status.value)
                elif not include_superseded:
                    where_clauses.append("f.status <> 'SUPERSEDED'")
                if fact_type is not None:
                    where_clauses.append("f.fact_type = %s")
                    params.append(fact_type.value)
                if decoded_cursor is not None:
                    where_clauses.append("(f.created_at, f.id) < (%s, %s::uuid)")
                    params.extend([decoded_cursor.sort_ts, decoded_cursor.last_fact_id])

                params.append(limit + 1)
                cur.execute(
                    f"""
                    SELECT {_FACT_SELECT_COLUMNS}
                    FROM public.facts f
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
                has_more = len(rows) > limit
                page_rows = rows[:limit]
                items = self._hydrate_fact_summaries(
                    cur, workspace_id=access.workspace_id, rows=page_rows
                )
                next_cursor: str | None = None
                if has_more and items:
                    last = items[-1]
                    next_cursor = encode_fact_cursor(
                        FactCursorPayload(
                            workspace_id=access.workspace_id,
                            filter_hash=filter_hash,
                            sort_ts=last.created_at,
                            last_fact_id=last.fact_id,
                        )
                    )
                return FactListResponse(
                    items=items,
                    page=FactPageInfo(next_cursor=next_cursor, has_more=has_more),
                )

    def get_fact_supports(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
    ) -> FactSupportsResponse:
        fact_uuid = _parse_uuid_or_none(fact_id)
        if fact_uuid is None:
            raise WorkspaceResourceNotFoundError()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    f"""
                    SELECT {_FACT_SELECT_COLUMNS}
                    FROM public.facts f
                    WHERE f.workspace_id = %s::uuid
                      AND f.id = %s::uuid
                    """,
                    (access.workspace_id, fact_uuid),
                )
                fact_row = cur.fetchone()
                if fact_row is None:
                    raise WorkspaceResourceNotFoundError()
                fact_summary = self._hydrate_fact_summaries(
                    cur, workspace_id=access.workspace_id, rows=[fact_row]
                )[0]

                # Strictly never select or expose extracted_span (Plan C / REF-04)
                cur.execute(
                    """
                    SELECT
                        fs.id::text,
                        fs.content_unit_id::text,
                        cu.source_object_revision_id::text,
                        cu.unit_type,
                        fs.support_type,
                        fs.status,
                        fs.validator,
                        fs.validator_version,
                        fs.support_metadata,
                        fs.created_at
                    FROM public.fact_support fs
                    INNER JOIN public.content_units cu
                        ON cu.workspace_id = fs.workspace_id
                       AND cu.id = fs.content_unit_id
                    WHERE fs.workspace_id = %s::uuid
                      AND fs.fact_id = %s::uuid
                    ORDER BY fs.created_at ASC, fs.id ASC
                    """,
                    (access.workspace_id, fact_uuid),
                )
                supports: list[FactSupportItem] = []
                for srow in cur.fetchall():
                    sm = dict(srow[8] or {})
                    assert_no_forbidden_raw_keys(sm, field_name="support_metadata")
                    supports.append(
                        FactSupportItem(
                            support_id=str(srow[0]),
                            content_unit_id=str(srow[1]),
                            source_object_revision_id=(
                                str(srow[2]) if srow[2] is not None else None
                            ),
                            unit_type=ContentUnitType(str(srow[3])),
                            support_type=SupportType(str(srow[4])),
                            status=LinkStatus(str(srow[5])),
                            validator=SupportValidator(str(srow[6])),
                            validator_version=str(srow[7]),
                            support_metadata=sm,
                            created_at=srow[9],
                        )
                    )
                return FactSupportsResponse(fact=fact_summary, supports=supports)

    # =========================================================================
    # 2. HTTP Mutations: Approve, Reject, ER-012 Versioned Edit
    # =========================================================================
    def approve_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: ApproveFactRequest,
        if_match_header: str | None = None,
    ) -> ApproveFactResponse:
        fact_uuid = _parse_uuid_or_none(fact_id)
        if fact_uuid is None:
            raise WorkspaceResourceNotFoundError()
        expected_version = resolve_expected_version(
            if_match_header=if_match_header,
            body_expected_version=request.expected_version,
        )
        fingerprint = _compute_request_fingerprint(
            {
                "action": "APPROVE_FACT",
                "expected_version": expected_version,
                "fact_id": fact_uuid,
                "reason": request.reason,
            }
        )
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_fact_row(
                    cur, workspace_id=access.workspace_id, fact_id=fact_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.CONFIRM,
                    fact_id=fact_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    summary = self._hydrate_fact_summaries(
                        cur, workspace_id=access.workspace_id, rows=[locked]
                    )[0]
                    return ApproveFactResponse(fact=summary, idempotent_replay=True)

                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                current_version = int(locked[3])
                current_status = FactStatus(str(locked[11]))
                if current_version != expected_version:
                    raise FactError(
                        status_code=409,
                        code="FACT_VERSION_CONFLICT",
                        message=(
                            f"Expected fact version {expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status not in (FactStatus.CANDIDATE, FactStatus.USER_CONFIRMED):
                    raise FactError(
                        status_code=409,
                        code="FACT_NOT_APPROVABLE",
                        message=(
                            f"Fact in status {current_status.value!r} cannot be approved as "
                            "USER_CONFIRMED."
                        ),
                    )

                subject_ident_id = str(locked[6]) if locked[6] is not None else None
                if subject_ident_id is not None:
                    elig = evaluate_identity_attribution(
                        cur, access.workspace_id, subject_ident_id
                    )
                    if elig.identity_type is IdentityType.BOT or elig.block_reason_code in (
                        IdentityAttributionBlockReason.IDENTITY_IS_BOT,
                        IdentityAttributionBlockReason.IDENTITY_CONFLICTED,
                        IdentityAttributionBlockReason.IDENTITY_REVOKED,
                    ):
                        raise FactError(
                            status_code=409,
                            code="FACT_NOT_APPROVABLE",
                            message=(
                                "Fact subject identity is ineligible for user confirmation "
                                f"({elig.block_reason_code})."
                            ),
                        )

                # NEVER sets SUPPORTED without Stage 2B DIRECT source validation!
                cur.execute(
                    f"""
                    UPDATE public.facts f
                    SET status = 'USER_CONFIRMED',
                        updated_at = %s
                    WHERE f.workspace_id = %s::uuid
                      AND f.id = %s::uuid
                    RETURNING {_FACT_SELECT_COLUMNS}
                    """,
                    (ts, access.workspace_id, fact_uuid),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None

                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=fact_uuid,
                    action=ReviewActionType.CONFIRM,
                    actor_user_id=access.user_id,
                    from_version=current_version,
                    to_version=current_version,
                    idempotency_key=idempotency_key,
                    reason=request.reason,
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "previous_status": current_status.value,
                        "new_status": FactStatus.USER_CONFIRMED.value,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="FACT_USER_CONFIRMED",
                    target_type="FACT",
                    target_id=fact_uuid,
                    redacted_metadata={
                        "event_id": str(locked[2]),
                        "version": current_version,
                        "previous_status": current_status.value,
                        "new_status": FactStatus.USER_CONFIRMED.value,
                    },
                    created_at=ts,
                )

                _invoke_before_commit_hook(
                    self._before_commit_hook, action="approve_fact", cur=cur
                )
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                summary = self._hydrate_fact_summaries(
                    cur, workspace_id=access.workspace_id, rows=[updated_row]
                )[0]
                return ApproveFactResponse(fact=summary, idempotent_replay=False)

    def reject_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: RejectFactRequest,
        if_match_header: str | None = None,
    ) -> RejectFactResponse:
        fact_uuid = _parse_uuid_or_none(fact_id)
        if fact_uuid is None:
            raise WorkspaceResourceNotFoundError()
        expected_version = resolve_expected_version(
            if_match_header=if_match_header,
            body_expected_version=request.expected_version,
        )
        fingerprint = _compute_request_fingerprint(
            {
                "action": "REJECT_FACT",
                "expected_version": expected_version,
                "fact_id": fact_uuid,
                "reason": request.reason,
            }
        )
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_fact_row(
                    cur, workspace_id=access.workspace_id, fact_id=fact_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.REJECT,
                    fact_id=fact_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    summary = self._hydrate_fact_summaries(
                        cur, workspace_id=access.workspace_id, rows=[locked]
                    )[0]
                    counts = FactInvalidationCounts.model_validate(
                        replay.get("invalidated_counts", {})
                    )
                    return RejectFactResponse(
                        fact=summary,
                        invalidated_counts=counts,
                        idempotent_replay=True,
                    )

                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                current_version = int(locked[3])
                current_status = FactStatus(str(locked[11]))
                if current_version != expected_version:
                    raise FactError(
                        status_code=409,
                        code="FACT_VERSION_CONFLICT",
                        message=(
                            f"Expected fact version {expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status in (FactStatus.SUPERSEDED, FactStatus.REJECTED):
                    raise FactError(
                        status_code=409,
                        code="FACT_NOT_REJECTABLE",
                        message=f"Fact in status {current_status.value!r} cannot be rejected.",
                    )

                cur.execute(
                    f"""
                    UPDATE public.facts f
                    SET status = 'REJECTED',
                        updated_at = %s
                    WHERE f.workspace_id = %s::uuid
                      AND f.id = %s::uuid
                    RETURNING {_FACT_SELECT_COLUMNS}
                    """,
                    (ts, access.workspace_id, fact_uuid),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None

                counts = self._cascade_downstream_for_facts(
                    cur,
                    workspace_id=access.workspace_id,
                    fact_ids=[fact_uuid],
                    export_reason="FACT_REJECTED",
                    now=ts,
                )
                counts_dict = counts.model_dump(mode="json")

                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=fact_uuid,
                    action=ReviewActionType.REJECT,
                    actor_user_id=access.user_id,
                    from_version=current_version,
                    to_version=current_version,
                    idempotency_key=idempotency_key,
                    reason=request.reason,
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "previous_status": current_status.value,
                        "new_status": FactStatus.REJECTED.value,
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="FACT_REJECTED",
                    target_type="FACT",
                    target_id=fact_uuid,
                    redacted_metadata={
                        "event_id": str(locked[2]),
                        "version": current_version,
                        "reason": request.reason,
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )
                self._insert_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="FACT_INVALIDATED",
                    aggregate_type="FACT",
                    aggregate_id=fact_uuid,
                    dedupe_key=f"fact_rejected:{access.workspace_id}:{fact_uuid}:{idempotency_key}",
                    payload={
                        "fact_id": fact_uuid,
                        "event_id": str(locked[2]),
                        "trigger": "REJECTED",
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )

                _invoke_before_commit_hook(
                    self._before_commit_hook, action="reject_fact", cur=cur
                )
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                summary = self._hydrate_fact_summaries(
                    cur, workspace_id=access.workspace_id, rows=[updated_row]
                )[0]
                return RejectFactResponse(
                    fact=summary,
                    invalidated_counts=counts,
                    idempotent_replay=False,
                )

    def edit_fact(
        self,
        access: WorkspaceAccessContext,
        *,
        fact_id: str,
        idempotency_key: str,
        request: EditFactRequest,
        if_match_header: str | None = None,
    ) -> EditFactResponse:
        """ER-012 Non-Overwrite Versioned Edit: supersede old row & insert fresh CANDIDATE v+1."""
        fact_uuid = _parse_uuid_or_none(fact_id)
        if fact_uuid is None:
            raise WorkspaceResourceNotFoundError()
        expected_version = resolve_expected_version(
            if_match_header=if_match_header,
            body_expected_version=request.expected_version,
        )
        if (
            request.predicate is None
            and request.object_text is None
            and request.normalized_value is None
        ):
            raise FactError(
                status_code=422,
                code="EMPTY_FACT_EDIT",
                message="At least one of predicate, object_text, or normalized_value must be provided.",
            )
        if request.normalized_value is not None:
            assert_no_forbidden_raw_keys(request.normalized_value, field_name="normalized_value")
            assert_no_raw_text_or_secret_keys(
                request.normalized_value, context="EditFactRequest.normalized_value"
            )

        fingerprint = _compute_request_fingerprint(
            {
                "action": "EDIT_FACT",
                "expected_version": expected_version,
                "fact_id": fact_uuid,
                "normalized_value": request.normalized_value,
                "object_text": request.object_text,
                "predicate": request.predicate,
                "reason": request.reason,
            }
        )
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                locked = self._lock_fact_row(
                    cur, workspace_id=access.workspace_id, fact_id=fact_uuid
                )
                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.EDIT,
                    fact_id=fact_uuid,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    new_fact_id = str(replay["new_fact_id"])
                    cur.execute(
                        f"""
                        SELECT {_FACT_SELECT_COLUMNS}
                        FROM public.facts f
                        WHERE f.workspace_id = %s::uuid
                          AND f.id = %s::uuid
                        """,
                        (access.workspace_id, new_fact_id),
                    )
                    new_row = cur.fetchone()
                    assert new_row is not None
                    new_summary = self._hydrate_fact_summaries(
                        cur, workspace_id=access.workspace_id, rows=[new_row]
                    )[0]
                    counts = FactInvalidationCounts.model_validate(
                        replay.get("invalidated_counts", {})
                    )
                    return EditFactResponse(
                        fact=new_summary,
                        superseded_fact_id=fact_uuid,
                        superseded_version=expected_version,
                        invalidated_counts=counts,
                        idempotent_replay=True,
                    )

                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                current_version = int(locked[3])
                current_status = FactStatus(str(locked[11]))
                if current_status is FactStatus.SUPERSEDED:
                    raise FactError(
                        status_code=409,
                        code="FACT_VERSION_CONFLICT",
                        message="Fact version has already been superseded by a newer version.",
                    )
                if current_version != expected_version:
                    raise FactError(
                        status_code=409,
                        code="FACT_VERSION_CONFLICT",
                        message=(
                            f"Expected fact version {expected_version}, "
                            f"but current version is {current_version}."
                        ),
                    )
                if current_status is FactStatus.REJECTED:
                    raise FactError(
                        status_code=409,
                        code="FACT_NOT_EDITABLE",
                        message="Rejected fact cannot be edited.",
                    )

                # 1. Mark prior row as SUPERSEDED without overwriting predicate/object_text!
                cur.execute(
                    """
                    UPDATE public.facts
                    SET status = 'SUPERSEDED',
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (ts, access.workspace_id, fact_uuid),
                )

                # 2. Insert NEW versioned row (status='CANDIDATE', zero copied fact_support!)
                new_fact_id = str(uuid.uuid4())
                new_version = current_version + 1
                new_predicate = (
                    request.predicate if request.predicate is not None else str(locked[7])
                )
                new_object_text = (
                    request.object_text if request.object_text is not None else str(locked[8])
                )
                new_normalized_value = (
                    dict(request.normalized_value)
                    if request.normalized_value is not None
                    else (dict(locked[9]) if locked[9] is not None else None)
                )
                old_meta = dict(locked[10] or {})
                new_meta: dict[str, Any] = {
                    "edited_from_fact_id": fact_uuid,
                    "edited_from_version": current_version,
                    "revalidation_required": True,
                }
                if "extractor_run_id" in old_meta:
                    new_meta["origin_extractor_run_id"] = old_meta["extractor_run_id"]
                assert_no_raw_text_or_secret_keys(new_meta, context="EditFact.fact_metadata")

                cur.execute(
                    f"""
                    INSERT INTO public.facts AS f (
                        id, workspace_id, event_id, version, supersedes_fact_id,
                        fact_type, subject_identity_id, predicate, object_text,
                        normalized_value, fact_metadata, status, extractor_version,
                        created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s, %s::uuid,
                        %s, %s::uuid, %s, %s,
                        %s, %s, 'CANDIDATE', %s,
                        %s, %s
                    )
                    RETURNING {_FACT_SELECT_COLUMNS}
                    """,
                    (
                        new_fact_id,
                        access.workspace_id,
                        str(locked[2]),
                        new_version,
                        fact_uuid,
                        str(locked[5]),
                        str(locked[6]) if locked[6] is not None else None,
                        new_predicate,
                        new_object_text,
                        Jsonb(new_normalized_value) if new_normalized_value is not None else None,
                        Jsonb(new_meta),
                        str(locked[12]),
                        ts,
                        ts,
                    ),
                )
                inserted_row = cur.fetchone()
                assert inserted_row is not None

                # 3. Cascade STALE / INVALIDATED to downstream dependents of superseded fact_id
                counts = self._cascade_downstream_for_facts(
                    cur,
                    workspace_id=access.workspace_id,
                    fact_ids=[fact_uuid],
                    export_reason="FACT_VERSION_SUPERSEDED",
                    now=ts,
                )
                counts_dict = counts.model_dump(mode="json")

                # 4. Record review_actions, audit_events, outbox_events
                self._insert_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_id=fact_uuid,
                    action=ReviewActionType.EDIT,
                    actor_user_id=access.user_id,
                    from_version=current_version,
                    to_version=new_version,
                    idempotency_key=idempotency_key,
                    reason=request.reason,
                    action_metadata={
                        "request_fingerprint": fingerprint,
                        "new_fact_id": new_fact_id,
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )
                self._insert_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_type=AuditActorType.USER,
                    actor_id=access.user_id,
                    action="FACT_VERSION_EDITED",
                    target_type="FACT",
                    target_id=new_fact_id,
                    redacted_metadata={
                        "event_id": str(locked[2]),
                        "superseded_fact_id": fact_uuid,
                        "from_version": current_version,
                        "to_version": new_version,
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )
                self._insert_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="FACT_SUPERSEDED",
                    aggregate_type="FACT",
                    aggregate_id=fact_uuid,
                    dedupe_key=f"fact_edited:{access.workspace_id}:{fact_uuid}:{new_fact_id}",
                    payload={
                        "superseded_fact_id": fact_uuid,
                        "new_fact_id": new_fact_id,
                        "from_version": current_version,
                        "to_version": new_version,
                        "invalidated_counts": counts_dict,
                    },
                    created_at=ts,
                )

                _invoke_before_commit_hook(
                    self._before_commit_hook, action="edit_fact", cur=cur
                )
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=str(locked[2])
                )

                new_summary = self._hydrate_fact_summaries(
                    cur, workspace_id=access.workspace_id, rows=[inserted_row]
                )[0]
                return EditFactResponse(
                    fact=new_summary,
                    superseded_fact_id=fact_uuid,
                    superseded_version=current_version,
                    invalidated_counts=counts,
                    idempotent_replay=False,
                )

    # =========================================================================
    # 3. Programmatic Pipeline Entrypoints (Stage 2A -> Stage 2B -> Normalizer)
    # =========================================================================
    def extract_candidates_for_unit(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        content_unit_id: str,
        budget: StageBudgetPolicy,
        adapter: StageExecutionAdapterPort,
        provider_mode: LlmProviderMode,
        allow_synthetic_fixture_mode: bool = False,
        extractor_version: str = DEFAULT_EXTRACTOR_VERSION,
        prompt_version: str = DEFAULT_EXTRACTOR_PROMPT_VERSION,
        model: str = DEFAULT_EXTRACTOR_MODEL,
        job_reference: ConnectionJobReference | None = None,
        operation_id: str | None = None,
        refetch_session: OperationRefetchSession | None = None,
    ) -> FactExtractionUnitResult:
        """Stage 2A entrypoint: refetch ephemeral text -> extract 0..N candidates -> persist CANDIDATE rows."""
        if self._resolver is None:
            raise FactError(
                status_code=500,
                code="RESOLVER_NOT_CONFIGURED",
                message="RefetchResolverService is required for Stage 2A candidate extraction.",
            )
        evt_uuid = _parse_uuid_or_none(event_id)
        unit_uuid = _parse_uuid_or_none(content_unit_id)
        if evt_uuid is None or unit_uuid is None:
            raise WorkspaceResourceNotFoundError()

        # 1. Load non-raw provenance target metadata & check Stage 2A idempotency
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                unit_info = self._load_unit_and_event_info(
                    cur, workspace_id=access.workspace_id, event_id=evt_uuid, unit_id=unit_uuid
                )
                input_hash = compute_extraction_input_hash(
                    event_id=evt_uuid,
                    content_unit_id=unit_uuid,
                    expected_revision_hash=unit_info["revision_hash"],
                    expected_unit_hash=unit_info["unit_hash"],
                    extractor_version=extractor_version,
                    prompt_version=prompt_version,
                )
                # Idempotent lookup: existing SUCCEEDED extraction run for identical inputs
                cur.execute(
                    """
                    SELECT id::text, input_artifact_versions, run_metadata
                    FROM public.generation_runs
                    WHERE workspace_id = %s::uuid
                      AND event_id = %s::uuid
                      AND run_type = 'FACT_CANDIDATE_EXTRACTION'
                      AND algorithm_version = %s
                      AND input_hash = %s
                      AND status = 'SUCCEEDED'
                    ORDER BY started_at ASC
                    LIMIT 1
                    """,
                    (access.workspace_id, evt_uuid, extractor_version, input_hash),
                )
                existing_run = cur.fetchone()
                if existing_run is not None:
                    prev_run_id = str(existing_run[0])
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.facts
                        WHERE workspace_id = %s::uuid
                          AND event_id = %s::uuid
                          AND fact_metadata ->> 'extractor_run_id' = %s
                          AND status <> 'SUPERSEDED'
                        ORDER BY created_at ASC, id ASC
                        """,
                        (access.workspace_id, evt_uuid, prev_run_id),
                    )
                    existing_fact_ids = tuple(str(r[0]) for r in cur.fetchall())
                    return FactExtractionUnitResult(
                        extractor_run_id=prev_run_id,
                        event_id=evt_uuid,
                        content_unit_id=unit_uuid,
                        source_object_revision_id=unit_info["source_object_revision_id"],
                        status=GenerationRunStatus.SUCCEEDED,
                        error_code=None,
                        candidates=(),
                        input_hash=input_hash,
                        extractor_version=extractor_version,
                        prompt_version=prompt_version,
                        model=model,
                        input_artifact_versions=dict(existing_run[1] or {}),
                        run_metadata=dict(existing_run[2] or {}),
                        persisted_fact_ids=existing_fact_ids,
                        idempotent_reuse=True,
                    )

        # 2. Ephemerally refetch ContentUnit via Plan C RefetchResolverService (NO DB raw text!)
        op_id = operation_id or f"stage2a-{uuid.uuid4()}"
        own_session = refetch_session is None
        session = (
            self._resolver.open_operation_session(access, operation_id=op_id)
            if own_session
            else refetch_session
        )
        assert session is not None
        try:
            resolution = self._resolver.resolve_unit(
                session,
                RefetchRequestInput(
                    access=access,
                    content_unit_id=unit_uuid,
                    operation_id=session.operation_id,
                    expected_revision_hash=unit_info["revision_hash"],
                    expected_unit_hash=unit_info["unit_hash"],
                    include_context=False,
                ),
            )
            extractor_run_id = str(uuid.uuid4())
            result = self._extractor.extract_candidates(
                FactExtractionRequest(
                    access=access,
                    event_id=evt_uuid,
                    source_resource_id=unit_info["source_resource_id"],
                    visibility=ResourceVisibility(unit_info["visibility"]),
                    resolution=resolution,
                    expected_revision_hash=unit_info["revision_hash"],
                    expected_unit_hash=unit_info["unit_hash"],
                    budget=budget,
                    adapter=adapter,
                    provider_mode=provider_mode,
                    allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
                    extractor_run_id=extractor_run_id,
                    extractor_version=extractor_version,
                    prompt_version=prompt_version,
                    model=model,
                    job_reference=job_reference,
                    close_resolution_on_exit=True,
                )
            )
        finally:
            if own_session:
                session.close()

        # 3. Persist generation_runs + (ONLY on SUCCEEDED) 0..N CANDIDATE facts atomically
        ts = self._now_fn()
        persisted_ids: list[str] = []
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=evt_uuid
                )
                assert_no_raw_text_or_secret_keys(
                    result.input_artifact_versions, context="generation_runs.input_artifact_versions"
                )
                assert_no_raw_text_or_secret_keys(
                    result.run_metadata, context="generation_runs.run_metadata"
                )
                cur.execute(
                    """
                    INSERT INTO public.generation_runs (
                        id, workspace_id, event_id, run_type,
                        input_artifact_versions, model, prompt_version, algorithm_version,
                        input_hash, status, error_code, run_metadata,
                        started_at, completed_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        result.extractor_run_id,
                        access.workspace_id,
                        evt_uuid,
                        GenerationRunType.FACT_CANDIDATE_EXTRACTION.value,
                        Jsonb(result.input_artifact_versions),
                        result.model,
                        result.prompt_version,
                        result.extractor_version,
                        result.input_hash,
                        result.status.value,
                        result.error_code,
                        Jsonb(result.run_metadata),
                        ts,
                        ts,
                    ),
                )

                if result.status is GenerationRunStatus.SUCCEEDED:
                    for cand in result.candidates:
                        fact_id = self._insert_or_reuse_candidate_fact_in_tx(
                            cur,
                            workspace_id=access.workspace_id,
                            event_id=evt_uuid,
                            candidate=cand,
                            now=ts,
                        )
                        persisted_ids.append(fact_id)

                _invoke_before_commit_hook(
                    self._before_commit_hook, action="extract_candidates_for_unit", cur=cur
                )
                self._assert_live_workspace_membership(cur, access)

        return FactExtractionUnitResult(
            extractor_run_id=result.extractor_run_id,
            event_id=result.event_id,
            content_unit_id=result.content_unit_id,
            source_object_revision_id=result.source_object_revision_id,
            status=result.status,
            error_code=result.error_code,
            candidates=result.candidates,
            input_hash=result.input_hash,
            extractor_version=result.extractor_version,
            prompt_version=result.prompt_version,
            model=result.model,
            input_artifact_versions=result.input_artifact_versions,
            run_metadata=result.run_metadata,
            persisted_fact_ids=tuple(persisted_ids),
            idempotent_reuse=False,
        )

    def validate_and_normalize_candidates(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        fact_ids: Sequence[str],
        content_unit_id: str,
        budget: StageBudgetPolicy,
        adapter: StageExecutionAdapterPort,
        provider_mode: LlmProviderMode,
        allow_synthetic_fixture_mode: bool = False,
        candidate_locator_by_fact_id: Mapping[str, Mapping[str, Any]] | None = None,
        validator_version: str = DEFAULT_VALIDATOR_VERSION,
        prompt_version: str = DEFAULT_VALIDATOR_PROMPT_VERSION,
        model: str = DEFAULT_VALIDATOR_MODEL,
        job_reference: ConnectionJobReference | None = None,
        operation_id: str | None = None,
        refetch_session: OperationRefetchSession | None = None,
    ) -> EventFactPipelineResult:
        """Stage 2B + Normalizer entrypoint: re-verify unit -> validate each candidate -> update facts."""
        if self._resolver is None:
            raise FactError(
                status_code=500,
                code="RESOLVER_NOT_CONFIGURED",
                message="RefetchResolverService is required for Stage 2B validation.",
            )
        evt_uuid = _parse_uuid_or_none(event_id)
        unit_uuid = _parse_uuid_or_none(content_unit_id)
        if evt_uuid is None or unit_uuid is None:
            raise WorkspaceResourceNotFoundError()

        parsed_fact_ids: list[str] = []
        for fid in fact_ids:
            f_uuid = _parse_uuid_or_none(fid)
            if f_uuid is None:
                raise WorkspaceResourceNotFoundError()
            parsed_fact_ids.append(f_uuid)

        if not parsed_fact_ids:
            return EventFactPipelineResult(
                event_id=evt_uuid,
                extractor_run_ids=(),
                validator_run_ids=(),
                extraction_results=(),
                validation_outcomes=(),
                facts=(),
            )

        # 1. Load unit metadata, candidate facts, and identity attribution eligibilities
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                unit_info = self._load_unit_and_event_info(
                    cur, workspace_id=access.workspace_id, event_id=evt_uuid, unit_id=unit_uuid
                )
                candidates_meta: list[dict[str, Any]] = []
                for fid in parsed_fact_ids:
                    cur.execute(
                        f"""
                        SELECT {_FACT_SELECT_COLUMNS}
                        FROM public.facts f
                        WHERE f.workspace_id = %s::uuid
                          AND f.event_id = %s::uuid
                          AND f.id = %s::uuid
                        """,
                        (access.workspace_id, evt_uuid, fid),
                    )
                    frow = cur.fetchone()
                    if frow is None:
                        raise WorkspaceResourceNotFoundError()
                    fmeta = dict(frow[10] or {})
                    nv = dict(frow[9]) if frow[9] is not None else None
                    metric_model: MetricDetailsModel | None = None
                    if isinstance(fmeta.get("metric_details"), Mapping):
                        metric_model = MetricDetailsModel.model_validate(fmeta["metric_details"])
                    elif isinstance(nv, Mapping) and isinstance(nv.get("metric_details"), Mapping):
                        metric_model = MetricDetailsModel.model_validate(nv["metric_details"])
                    semantic = extract_semantic_attributes(nv, metric_details=metric_model)
                    subj_id = str(frow[6]) if frow[6] is not None else None
                    elig = (
                        evaluate_identity_attribution(cur, access.workspace_id, subj_id)
                        if subj_id is not None
                        else None
                    )
                    ext_run_id = str(
                        fmeta.get("extractor_run_id")
                        or fmeta.get("origin_extractor_run_id")
                        or f"legacy-ext-{fid}"
                    )
                    loc_override = (
                        candidate_locator_by_fact_id.get(fid)
                        if candidate_locator_by_fact_id is not None
                        else None
                    )
                    cand_locator = (
                        dict(loc_override)
                        if loc_override is not None
                        else dict(fmeta.get("locator") or {})
                    )
                    candidates_meta.append(
                        {
                            "fact_id": fid,
                            "fact_type": FactType(str(frow[5])),
                            "subject_identity_id": subj_id,
                            "identity_eligibility": elig,
                            "predicate": str(frow[7]),
                            "object_text": str(frow[8]),
                            "semantic": semantic,
                            "metric_details": metric_model,
                            "extractor_run_id": ext_run_id,
                            "locator": cand_locator,
                        }
                    )

        # 2. Re-verify ContentUnit via RefetchResolverService in the current operation
        op_id = operation_id or f"stage2b-{uuid.uuid4()}"
        own_session = refetch_session is None
        session = (
            self._resolver.open_operation_session(access, operation_id=op_id)
            if own_session
            else refetch_session
        )
        assert session is not None
        outcomes: list[FactValidationOutcome] = []
        validator_run_ids: list[str] = []
        extractor_run_ids: list[str] = []
        try:
            for item in candidates_meta:
                resolution = self._resolver.resolve_unit(
                    session,
                    RefetchRequestInput(
                        access=access,
                        content_unit_id=unit_uuid,
                        operation_id=session.operation_id,
                        expected_revision_hash=unit_info["revision_hash"],
                        expected_unit_hash=unit_info["unit_hash"],
                        include_context=False,
                    ),
                )
                val_run_id = str(uuid.uuid4())
                # Guarantee distinct validator_run_id != extractor_run_id
                while val_run_id == item["extractor_run_id"]:
                    val_run_id = str(uuid.uuid4())

                outcome = self._validator.validate_candidate(
                    FactSupportValidationRequest(
                        access=access,
                        event_id=evt_uuid,
                        source_resource_id=unit_info["source_resource_id"],
                        visibility=ResourceVisibility(unit_info["visibility"]),
                        fact_candidate_id=item["fact_id"],
                        extractor_run_id=item["extractor_run_id"],
                        validator_run_id=val_run_id,
                        fact_type=item["fact_type"],
                        predicate=item["predicate"],
                        object_text=item["object_text"],
                        candidate_locator=item["locator"],
                        semantic=item["semantic"],
                        subject_identity_id=item["subject_identity_id"],
                        identity_eligibility=item["identity_eligibility"],
                        metric_details=item["metric_details"],
                        resolution=resolution,
                        expected_revision_hash=unit_info["revision_hash"],
                        expected_unit_hash=unit_info["unit_hash"],
                        unit_type=ContentUnitType(unit_info["unit_type"]),
                        budget=budget,
                        adapter=adapter,
                        provider_mode=provider_mode,
                        allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
                        validator_version=validator_version,
                        prompt_version=prompt_version,
                        model=model,
                        job_reference=job_reference,
                        close_resolution_on_exit=True,
                    )
                )
                outcomes.append(outcome)
                validator_run_ids.append(outcome.validator_run_id)
                if item["extractor_run_id"] not in extractor_run_ids:
                    extractor_run_ids.append(item["extractor_run_id"])
        finally:
            if own_session:
                session.close()

        # 3. Persist Stage 2B generation_runs + fact_support (extracted_span=NULL) + recompute facts.status
        ts = self._now_fn()
        hydrated_facts: list[FactSummaryItem] = []
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                self._assert_no_active_deletion_fence_for_event(
                    cur, workspace_id=access.workspace_id, event_id=evt_uuid
                )
                for item, outcome in zip(candidates_meta, outcomes, strict=True):
                    val_input_hash = compute_validation_input_hash(
                        event_id=evt_uuid,
                        fact_candidate_id=item["fact_id"],
                        extractor_run_id=outcome.extractor_run_id,
                        content_unit_id=unit_uuid,
                        expected_revision_hash=unit_info["revision_hash"],
                        expected_unit_hash=unit_info["unit_hash"],
                        locator_span_sha256=(
                            str(outcome.verified_locator.get("span_sha256"))
                            if outcome.verified_locator.get("span_sha256")
                            else unit_info["unit_hash"]
                        ),
                        validator_version=validator_version,
                        prompt_version=prompt_version,
                    )
                    val_input_versions = {
                        "source_revision_ids": [unit_info["source_object_revision_id"]],
                        "source_revision_hashes": [unit_info["revision_hash"]],
                        "content_unit_ids": [unit_uuid],
                        "content_unit_hashes": [unit_info["unit_hash"]],
                        "fact_ids": [item["fact_id"]],
                        "canonicalization_version": CANONICALIZATION_VERSION,
                        "schema_version": SCHEMA_PACKAGE_VERSION,
                    }
                    val_run_meta = {
                        "stage_code": "STAGE_2B_FACT_SUPPORT_VALIDATION",
                        "output_schema_id": FACT_SUPPORT_VALIDATION_SCHEMA_ID,
                        "output_schema_version": SCHEMA_PACKAGE_VERSION,
                        "extractor_run_id": outcome.extractor_run_id,
                        "promotion_decision": outcome.promotion_decision.value,
                        "support_type": outcome.support_type.value,
                        "revision_and_locator_verified": outcome.revision_and_locator_verified,
                        "guard_override_applied": outcome.guard_override_applied,
                    }
                    assert_no_raw_text_or_secret_keys(
                        val_run_meta, context="validation.generation_runs.run_metadata"
                    )
                    cur.execute(
                        """
                        INSERT INTO public.generation_runs (
                            id, workspace_id, event_id, run_type,
                            input_artifact_versions, model, prompt_version, algorithm_version,
                            input_hash, status, error_code, run_metadata,
                            started_at, completed_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s
                        )
                        """,
                        (
                            outcome.validator_run_id,
                            access.workspace_id,
                            evt_uuid,
                            GenerationRunType.FACT_SUPPORT_VALIDATION.value,
                            Jsonb(val_input_versions),
                            model,
                            prompt_version,
                            validator_version,
                            val_input_hash,
                            outcome.run_status.value,
                            outcome.run_error_code,
                            Jsonb(val_run_meta),
                            ts,
                            ts,
                        ),
                    )

                    # Record fact_support ONLY when revision & locator were verified (extracted_span ALWAYS NULL!)
                    should_persist_support = outcome.revision_and_locator_verified and (
                        (
                            outcome.support_type is SupportType.DIRECT
                            and outcome.promotion_decision
                            is FactPromotionDecision.PROMOTE_SUPPORTED
                        )
                        or outcome.support_type
                        in (SupportType.CONTEXT, SupportType.CONTRADICTS)
                    )
                    if should_persist_support:
                        assert_no_raw_text_or_secret_keys(
                            outcome.support_metadata, context="fact_support.support_metadata"
                        )
                        cur.execute(
                            """
                            INSERT INTO public.fact_support (
                                id, workspace_id, fact_id, content_unit_id,
                                support_type, extracted_span, support_metadata,
                                validator, validator_version, status, created_at
                            ) VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s, NULL, %s,
                                %s, %s, 'ACTIVE', %s
                            )
                            ON CONFLICT (fact_id, content_unit_id, support_type)
                            DO UPDATE SET
                                support_metadata = EXCLUDED.support_metadata,
                                validator = EXCLUDED.validator,
                                validator_version = EXCLUDED.validator_version,
                                status = 'ACTIVE'
                            """,
                            (
                                str(uuid.uuid4()),
                                access.workspace_id,
                                item["fact_id"],
                                unit_uuid,
                                outcome.support_type.value,
                                Jsonb(outcome.support_metadata),
                                outcome.validator.value,
                                outcome.validator_version,
                                ts,
                            ),
                        )

                    # Recompute Fact status from all ACTIVE fact_support rows + identity gate
                    updated_fact = self._recompute_single_fact_status_in_tx(
                        cur,
                        workspace_id=access.workspace_id,
                        fact_id=item["fact_id"],
                        now=ts,
                    )
                    hydrated_facts.append(updated_fact)

                _invoke_before_commit_hook(
                    self._before_commit_hook,
                    action="validate_and_normalize_candidates",
                    cur=cur,
                )
                self._assert_live_workspace_membership(cur, access)

        return EventFactPipelineResult(
            event_id=evt_uuid,
            extractor_run_ids=tuple(extractor_run_ids),
            validator_run_ids=tuple(validator_run_ids),
            extraction_results=(),
            validation_outcomes=tuple(outcomes),
            facts=tuple(hydrated_facts),
        )

    def extract_and_validate_event_facts(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        content_unit_ids: Sequence[str],
        budget: StageBudgetPolicy,
        adapter: StageExecutionAdapterPort,
        provider_mode: LlmProviderMode,
        allow_synthetic_fixture_mode: bool = False,
        extractor_version: str = DEFAULT_EXTRACTOR_VERSION,
        validator_version: str = DEFAULT_VALIDATOR_VERSION,
        operation_id: str | None = None,
    ) -> EventFactPipelineResult:
        """Orchestrate Stage 2A -> Stage 2B -> CanonicalFactNormalizer across ContentUnits."""
        if self._resolver is None:
            raise FactError(
                status_code=500,
                code="RESOLVER_NOT_CONFIGURED",
                message="RefetchResolverService is required for event fact extraction pipeline.",
            )
        op_id = operation_id or f"event-facts-{uuid.uuid4()}"
        all_ext_results: list[FactExtractionUnitResult] = []
        all_val_outcomes: list[FactValidationOutcome] = []
        all_ext_ids: list[str] = []
        all_val_ids: list[str] = []
        facts_by_id: dict[str, FactSummaryItem] = {}

        with self._resolver.open_operation_session(access, operation_id=op_id) as session:
            for unit_id in content_unit_ids:
                ext_res = self.extract_candidates_for_unit(
                    access,
                    event_id=event_id,
                    content_unit_id=unit_id,
                    budget=budget,
                    adapter=adapter,
                    provider_mode=provider_mode,
                    allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
                    extractor_version=extractor_version,
                    operation_id=op_id,
                    refetch_session=session,
                )
                all_ext_results.append(ext_res)
                all_ext_ids.append(ext_res.extractor_run_id)
                if (
                    ext_res.status is GenerationRunStatus.SUCCEEDED
                    and ext_res.persisted_fact_ids
                ):
                    val_res = self.validate_and_normalize_candidates(
                        access,
                        event_id=event_id,
                        fact_ids=ext_res.persisted_fact_ids,
                        content_unit_id=unit_id,
                        budget=budget,
                        adapter=adapter,
                        provider_mode=provider_mode,
                        allow_synthetic_fixture_mode=allow_synthetic_fixture_mode,
                        validator_version=validator_version,
                        operation_id=op_id,
                        refetch_session=session,
                    )
                    all_val_outcomes.extend(val_res.validation_outcomes)
                    all_val_ids.extend(val_res.validator_run_ids)
                    for fact_item in val_res.facts:
                        facts_by_id[fact_item.fact_id] = fact_item

        return EventFactPipelineResult(
            event_id=event_id,
            extractor_run_ids=tuple(all_ext_ids),
            validator_run_ids=tuple(all_val_ids),
            extraction_results=tuple(all_ext_results),
            validation_outcomes=tuple(all_val_outcomes),
            facts=tuple(facts_by_id.values()),
        )

    # =========================================================================
    # 4. MVP Contributions & ContributionSupport (Data Model §3.8 / CLAIM-05)
    # =========================================================================
    def record_contribution(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        identity_id: str,
        role: ContributionRole,
        origin: RecordOrigin = RecordOrigin.RULE,
        supporting_fact_ids: Sequence[str] = (),
    ) -> ContributionRecord:
        """Persist a Contribution & ContributionSupport respecting MVP RULE roles and CLAIM-05."""
        evt_uuid = _parse_uuid_or_none(event_id)
        ident_uuid = _parse_uuid_or_none(identity_id)
        if evt_uuid is None or ident_uuid is None:
            raise WorkspaceResourceNotFoundError()
        parsed_fact_ids = [
            fid for fid in (_parse_uuid_or_none(x) for x in supporting_fact_ids) if fid is not None
        ]
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                elig = evaluate_identity_attribution(cur, access.workspace_id, ident_uuid)

                supported_direct_ids: set[str] = set()
                if parsed_fact_ids:
                    cur.execute(
                        """
                        SELECT DISTINCT f.id::text
                        FROM public.facts f
                        INNER JOIN public.fact_support fs
                            ON fs.workspace_id = f.workspace_id
                           AND fs.fact_id = f.id
                        WHERE f.workspace_id = %s::uuid
                          AND f.event_id = %s::uuid
                          AND f.id = ANY(%s::uuid[])
                          AND f.status = 'SUPPORTED'
                          AND fs.status = 'ACTIVE'
                          AND fs.support_type = 'DIRECT'
                        """,
                        (access.workspace_id, evt_uuid, parsed_fact_ids),
                    )
                    supported_direct_ids = {str(r[0]) for r in cur.fetchall()}

                evaluated = self._normalizer.evaluate_contribution(
                    ContributionCandidateInput(
                        workspace_id=access.workspace_id,
                        event_id=evt_uuid,
                        identity_id=ident_uuid,
                        role=role,
                        origin=origin,
                        supporting_fact_ids=tuple(parsed_fact_ids),
                    ),
                    identity_eligibility=elig,
                    supported_direct_fact_ids=supported_direct_ids,
                )

                contrib_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO public.contributions (
                        id, workspace_id, event_id, identity_id, role, origin, status, created_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s
                    )
                    ON CONFLICT (event_id, identity_id, role)
                    DO UPDATE SET
                        status = EXCLUDED.status
                    RETURNING id::text, created_at
                    """,
                    (
                        contrib_id,
                        access.workspace_id,
                        evt_uuid,
                        ident_uuid,
                        role.value,
                        origin.value,
                        evaluated.status.value,
                        ts,
                    ),
                )
                row = cur.fetchone()
                assert row is not None
                actual_contrib_id = str(row[0])
                actual_created_at = row[1]

                for fid in evaluated.supporting_fact_ids:
                    cur.execute(
                        """
                        INSERT INTO public.contribution_support (
                            workspace_id, contribution_id, fact_id, status, created_at
                        ) VALUES (%s::uuid, %s::uuid, %s::uuid, 'ACTIVE', %s)
                        ON CONFLICT (contribution_id, fact_id)
                        DO UPDATE SET status = 'ACTIVE'
                        """,
                        (access.workspace_id, actual_contrib_id, fid, ts),
                    )

                _invoke_before_commit_hook(
                    self._before_commit_hook, action="record_contribution", cur=cur
                )
                self._assert_live_workspace_membership(cur, access)

                return ContributionRecord(
                    contribution_id=actual_contrib_id,
                    workspace_id=access.workspace_id,
                    event_id=evt_uuid,
                    identity_id=ident_uuid,
                    role=role,
                    origin=origin,
                    status=evaluated.status,
                    supporting_fact_ids=evaluated.supporting_fact_ids,
                    created_at=actual_created_at,
                )

    # =========================================================================
    # 5. Support / Validator-Version Invalidation Propagation
    # =========================================================================
    def recalculate_facts_after_support_or_validator_change(
        self,
        access: WorkspaceAccessContext,
        *,
        content_unit_ids: Sequence[str] | None = None,
        source_object_revision_ids: Sequence[str] | None = None,
        invalidated_support_ids: Sequence[str] | None = None,
        superseded_validator_versions: Sequence[str] | None = None,
        reason: str = "SUPPORT_OR_VALIDATOR_CHANGED",
    ) -> dict[str, Any]:
        """Invalidate affected fact_support rows, recalculate facts, and cascade STALE/INVALIDATED."""
        unit_uuids = [
            u for u in (_parse_uuid_or_none(x) for x in (content_unit_ids or ())) if u is not None
        ]
        rev_uuids = [
            r
            for r in (_parse_uuid_or_none(x) for x in (source_object_revision_ids or ()))
            if r is not None
        ]
        sup_uuids = [
            s
            for s in (_parse_uuid_or_none(x) for x in (invalidated_support_ids or ()))
            if s is not None
        ]
        val_versions = [v.strip() for v in (superseded_validator_versions or ()) if v.strip()]
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # 1. Invalidate matching ACTIVE fact_support rows
                cur.execute(
                    """
                    UPDATE public.fact_support fs
                    SET status = 'INVALIDATED'
                    FROM public.content_units cu
                    INNER JOIN public.source_object_revisions rev
                        ON rev.workspace_id = cu.workspace_id
                       AND rev.id = cu.source_object_revision_id
                    WHERE fs.workspace_id = %s::uuid
                      AND fs.status = 'ACTIVE'
                      AND cu.workspace_id = fs.workspace_id
                      AND cu.id = fs.content_unit_id
                      AND (
                          cu.policy_state IN ('PURGED', 'BLOCKED')
                          OR rev.is_tombstone = TRUE
                          OR (cardinality(%s::uuid[]) > 0 AND fs.content_unit_id = ANY(%s::uuid[]))
                          OR (cardinality(%s::uuid[]) > 0 AND cu.source_object_revision_id = ANY(%s::uuid[]))
                          OR (cardinality(%s::uuid[]) > 0 AND fs.id = ANY(%s::uuid[]))
                          OR (cardinality(%s::text[]) > 0 AND fs.validator_version = ANY(%s::text[]))
                      )
                    RETURNING fs.id::text, fs.fact_id::text
                    """,
                    (
                        access.workspace_id,
                        unit_uuids,
                        unit_uuids,
                        rev_uuids,
                        rev_uuids,
                        sup_uuids,
                        sup_uuids,
                        val_versions,
                        val_versions,
                    ),
                )
                inv_rows = cur.fetchall()
                invalidated_support_count = len(inv_rows)
                affected_fact_ids = sorted({str(r[1]) for r in inv_rows})

                stale_fact_ids: list[str] = []
                conflicted_fact_ids: list[str] = []
                for fid in affected_fact_ids:
                    updated = self._recompute_single_fact_status_in_tx(
                        cur, workspace_id=access.workspace_id, fact_id=fid, now=ts
                    )
                    if updated.status is FactStatus.STALE:
                        stale_fact_ids.append(fid)
                    elif updated.status is FactStatus.CONFLICTED:
                        conflicted_fact_ids.append(fid)

                degraded_fact_ids = sorted(set(stale_fact_ids) | set(conflicted_fact_ids))
                counts = (
                    self._cascade_downstream_for_facts(
                        cur,
                        workspace_id=access.workspace_id,
                        fact_ids=degraded_fact_ids,
                        export_reason=reason,
                        now=ts,
                    )
                    if degraded_fact_ids
                    else FactInvalidationCounts()
                )

                if degraded_fact_ids:
                    self._insert_audit_event(
                        cur,
                        workspace_id=access.workspace_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=access.user_id,
                        action="FACT_SUPPORT_INVALIDATION_RECALCULATED",
                        target_type="WORKSPACE",
                        target_id=access.workspace_id,
                        redacted_metadata={
                            "reason": reason,
                            "invalidated_support_count": invalidated_support_count,
                            "stale_fact_ids": stale_fact_ids,
                            "conflicted_fact_ids": conflicted_fact_ids,
                            "invalidated_counts": counts.model_dump(mode="json"),
                        },
                        created_at=ts,
                    )

                _invoke_before_commit_hook(
                    self._before_commit_hook,
                    action="recalculate_facts_after_support_or_validator_change",
                    cur=cur,
                )
                self._assert_live_workspace_membership(cur, access)

                return {
                    "invalidated_support_count": invalidated_support_count,
                    "stale_fact_ids": stale_fact_ids,
                    "conflicted_fact_ids": conflicted_fact_ids,
                    "invalidated_counts": counts,
                }

    # =========================================================================
    # Internal Transactional Helpers
    # =========================================================================
    def _insert_or_reuse_candidate_fact_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        candidate: FactCandidateDraft,
        now: datetime,
    ) -> str:
        canonical_key = build_canonical_fact_key(
            workspace_id=workspace_id,
            event_id=event_id,
            fact_type=candidate.fact_type,
            subject_identity_id=candidate.subject_identity_id,
            predicate=candidate.predicate,
            object_text=candidate.object_text,
            semantic=candidate.semantic,
        )
        # Deterministic canonical-key deduplication within the same WorkEvent (zero fuzzy merging!)
        cur.execute(
            """
            SELECT id::text
            FROM public.facts
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND status NOT IN ('REJECTED', 'SUPERSEDED')
              AND fact_metadata ->> 'canonical_key' = %s
            ORDER BY version DESC, created_at ASC
            LIMIT 1
            """,
            (workspace_id, event_id, canonical_key),
        )
        existing = cur.fetchone()
        if existing is not None:
            return str(existing[0])

        fact_id = str(uuid.uuid4())
        fact_meta: dict[str, Any] = {
            "canonical_key": canonical_key,
            "extractor_run_id": candidate.extractor_run_id,
            "source_object_revision_id": candidate.source_object_revision_id,
            "content_unit_id": candidate.content_unit_id,
            "locator": dict(candidate.locator),
            "completion_state": candidate.semantic.completion_state.value,
            "scope": candidate.semantic.scope.value,
            "environment_scope": candidate.semantic.environment_scope.value,
            "causal_basis": candidate.semantic.causal_basis.value,
        }
        if candidate.metric_details is not None:
            fact_meta["metric_details"] = candidate.metric_details.model_dump(mode="json")
        if candidate.uncertainty_notes is not None:
            fact_meta["uncertainty_notes"] = candidate.uncertainty_notes
        assert_no_raw_text_or_secret_keys(fact_meta, context="facts.fact_metadata")

        nv = dict(candidate.normalized_value) if candidate.normalized_value is not None else {}
        nv.setdefault("completion_state", candidate.semantic.completion_state.value)
        nv.setdefault("scope", candidate.semantic.scope.value)
        nv.setdefault("environment_scope", candidate.semantic.environment_scope.value)
        nv.setdefault("causal_basis", candidate.semantic.causal_basis.value)
        assert_no_raw_text_or_secret_keys(nv, context="facts.normalized_value")

        cur.execute(
            """
            INSERT INTO public.facts (
                id, workspace_id, event_id, version, supersedes_fact_id,
                fact_type, subject_identity_id, predicate, object_text,
                normalized_value, fact_metadata, status, extractor_version,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, 1, NULL,
                %s, %s::uuid, %s, %s,
                %s, %s, 'CANDIDATE', %s,
                %s, %s
            )
            """,
            (
                fact_id,
                workspace_id,
                event_id,
                candidate.fact_type.value,
                _parse_uuid_or_none(candidate.subject_identity_id),
                candidate.predicate,
                candidate.object_text,
                Jsonb(nv),
                Jsonb(fact_meta),
                candidate.extractor_version,
                now,
                now,
            ),
        )
        return fact_id

    def _recompute_single_fact_status_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        fact_id: str,
        now: datetime,
    ) -> FactSummaryItem:
        locked = self._lock_fact_row(cur, workspace_id=workspace_id, fact_id=fact_id)
        current_status = FactStatus(str(locked[11]))
        fact_type = FactType(str(locked[5]))
        subj_id = str(locked[6]) if locked[6] is not None else None
        nv = dict(locked[9]) if locked[9] is not None else None
        semantic = extract_semantic_attributes(nv)

        cur.execute(
            """
            SELECT fs.support_type
            FROM public.fact_support fs
            INNER JOIN public.content_units cu
                ON cu.workspace_id = fs.workspace_id
               AND cu.id = fs.content_unit_id
            INNER JOIN public.source_object_revisions rev
                ON rev.workspace_id = cu.workspace_id
               AND rev.id = cu.source_object_revision_id
            WHERE fs.workspace_id = %s::uuid
              AND fs.fact_id = %s::uuid
              AND fs.status = 'ACTIVE'
              AND cu.policy_state = 'ACTIVE'
              AND rev.is_tombstone = FALSE
            """,
            (workspace_id, fact_id),
        )
        active_types = [SupportType(str(r[0])) for r in cur.fetchall()]
        identity_required = self._normalizer.requires_verified_identity(
            fact_type=fact_type,
            subject_identity_id=subj_id,
            semantic=semantic,
        )
        elig = (
            evaluate_identity_attribution(cur, workspace_id, subj_id)
            if subj_id is not None
            else None
        )
        next_status = self._normalizer.compute_fact_status(
            active_support_types=active_types,
            identity_required=identity_required,
            identity_eligibility=elig,
            current_status=current_status,
        )
        if next_status is not current_status:
            cur.execute(
                f"""
                UPDATE public.facts f
                SET status = %s,
                    updated_at = %s
                WHERE f.workspace_id = %s::uuid
                  AND f.id = %s::uuid
                RETURNING {_FACT_SELECT_COLUMNS}
                """,
                (next_status.value, now, workspace_id, fact_id),
            )
            locked = cur.fetchone()
            assert locked is not None
        return self._hydrate_fact_summaries(cur, workspace_id=workspace_id, rows=[locked])[0]

    @staticmethod
    def _load_unit_and_event_info(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        unit_id: str,
    ) -> dict[str, str]:
        cur.execute(
            """
            SELECT 1
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceResourceNotFoundError()

        cur.execute(
            """
            SELECT
                cu.id::text,
                cu.source_object_revision_id::text,
                cu.unit_type,
                cu.content_hash,
                rev.content_hash,
                sr.id::text,
                sr.visibility
            FROM public.content_units cu
            INNER JOIN public.source_object_revisions rev
                ON rev.workspace_id = cu.workspace_id
               AND rev.id = cu.source_object_revision_id
            INNER JOIN public.source_objects so
                ON so.workspace_id = rev.workspace_id
               AND so.id = rev.source_object_id
            INNER JOIN public.source_resources sr
                ON sr.workspace_id = so.workspace_id
               AND sr.id = so.source_resource_id
            WHERE cu.workspace_id = %s::uuid
              AND cu.id = %s::uuid
            """,
            (workspace_id, unit_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceResourceNotFoundError()
        return {
            "content_unit_id": str(row[0]),
            "source_object_revision_id": str(row[1]),
            "unit_type": str(row[2]),
            "unit_hash": str(row[3]),
            "revision_hash": str(row[4]),
            "source_resource_id": str(row[5]),
            "visibility": str(row[6]),
        }

    @staticmethod
    def _hydrate_fact_summaries(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        rows: list[tuple[Any, ...]],
    ) -> list[FactSummaryItem]:
        if not rows:
            return []
        fact_ids = [str(r[0]) for r in rows]
        cur.execute(
            """
            SELECT fact_id::text, support_type, COUNT(*)
            FROM public.fact_support
            WHERE workspace_id = %s::uuid
              AND fact_id = ANY(%s::uuid[])
              AND status = 'ACTIVE'
            GROUP BY fact_id, support_type
            """,
            (workspace_id, fact_ids),
        )
        counts: dict[str, dict[str, int]] = {}
        for fid, stype, cnt in cur.fetchall():
            counts.setdefault(str(fid), {})[str(stype)] = int(cnt)

        items: list[FactSummaryItem] = []
        for row in rows:
            fid = str(row[0])
            ver = int(row[3])
            c_map = counts.get(fid, {})
            items.append(
                FactSummaryItem(
                    fact_id=fid,
                    workspace_id=str(row[1]),
                    event_id=str(row[2]),
                    version=ver,
                    supersedes_fact_id=str(row[4]) if row[4] is not None else None,
                    fact_type=FactType(str(row[5])),
                    subject_identity_id=str(row[6]) if row[6] is not None else None,
                    predicate=str(row[7]),
                    object_text=str(row[8]),
                    normalized_value=dict(row[9]) if row[9] is not None else None,
                    fact_metadata=dict(row[10] or {}),
                    status=FactStatus(str(row[11])),
                    extractor_version=str(row[12]),
                    direct_support_count=c_map.get(SupportType.DIRECT.value, 0),
                    context_support_count=c_map.get(SupportType.CONTEXT.value, 0),
                    contradicts_support_count=c_map.get(SupportType.CONTRADICTS.value, 0),
                    etag=format_fact_etag(ver),
                    created_at=row[13],
                    updated_at=row[14],
                )
            )
        return items

    @staticmethod
    def _lock_fact_row(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        fact_id: str,
    ) -> tuple[Any, ...]:
        cur.execute(
            f"""
            SELECT {_FACT_SELECT_COLUMNS}
            FROM public.facts f
            WHERE f.workspace_id = %s::uuid
              AND f.id = %s::uuid
            FOR UPDATE OF f
            """,
            (workspace_id, fact_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceResourceNotFoundError()
        return row

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
            raise WorkspaceMembershipDeniedError(
                "Workspace membership is no longer active."
            )

    @staticmethod
    def _assert_no_active_deletion_fence_for_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.deletion_jobs dj
            INNER JOIN public.source_objects so
                ON so.workspace_id = dj.workspace_id
               AND so.source_resource_id = dj.source_resource_id
            LEFT JOIN public.work_events we
                ON we.workspace_id = so.workspace_id
               AND we.primary_anchor_source_object_id = so.id
               AND we.id = %s::uuid
            LEFT JOIN public.event_memberships em
                ON em.workspace_id = so.workspace_id
               AND em.source_object_id = so.id
               AND em.event_id = %s::uuid
               AND em.status = 'ACTIVE'
            WHERE dj.workspace_id = %s::uuid
              AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL')
              AND (we.id IS NOT NULL OR em.id IS NOT NULL)
            LIMIT 1
            """,
            (event_id, event_id, workspace_id),
        )
        if cur.fetchone() is not None:
            raise FactError(
                status_code=409,
                code="DELETION_PENDING",
                message="Fact operation blocked while a linked source resource deletion is active.",
            )

    @staticmethod
    def _check_idempotency_replay(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        action: ReviewActionType,
        fact_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT action_metadata
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND action = %s
              AND target_type = %s
              AND target_id = %s::uuid
              AND idempotency_key = %s
            LIMIT 1
            """,
            (
                workspace_id,
                action.value,
                ReviewTargetType.FACT.value,
                fact_id,
                idempotency_key,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        meta = dict(row[0] or {})
        if meta.get("request_fingerprint") != fingerprint:
            raise FactError(
                status_code=409,
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key was already used with a different request payload.",
            )
        return meta

    @staticmethod
    def _cascade_downstream_for_facts(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        fact_ids: list[str],
        export_reason: str,
        now: datetime,
    ) -> FactInvalidationCounts:
        if not fact_ids:
            return FactInvalidationCounts()

        # 1. contribution_support -> INVALIDATED; dependent contributions -> STALE
        cur.execute(
            """
            UPDATE public.contribution_support
            SET status = 'INVALIDATED'
            WHERE workspace_id = %s::uuid
              AND fact_id = ANY(%s::uuid[])
              AND status = 'ACTIVE'
            RETURNING id::text, contribution_id::text
            """,
            (workspace_id, fact_ids),
        )
        cs_rows = cur.fetchall()
        affected_contrib_ids = sorted({str(r[1]) for r in cs_rows})
        stale_contrib_count = 0
        if affected_contrib_ids:
            cur.execute(
                """
                UPDATE public.contributions
                SET status = 'STALE'
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND status IN ('SOURCE_SUPPORTED', 'USER_CONFIRMED')
                RETURNING id::text
                """,
                (workspace_id, affected_contrib_ids),
            )
            stale_contrib_count = len(cur.fetchall())

        # 2. claim_atom_support -> INVALIDATED; dependent claim_atoms -> STALE
        cur.execute(
            """
            UPDATE public.claim_atom_support
            SET status = 'INVALIDATED'
            WHERE workspace_id = %s::uuid
              AND fact_id = ANY(%s::uuid[])
              AND status = 'ACTIVE'
            RETURNING id::text, claim_atom_id::text
            """,
            (workspace_id, fact_ids),
        )
        cas_rows = cur.fetchall()
        affected_atom_ids = sorted({str(r[1]) for r in cas_rows})
        stale_atom_count = 0
        affected_claim_ids: set[str] = set()
        if affected_atom_ids:
            cur.execute(
                """
                UPDATE public.claim_atoms
                SET status = 'STALE'
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND status IN ('SUPPORTED', 'USER_CONFIRMED')
                RETURNING id::text, claim_id::text
                """,
                (workspace_id, affected_atom_ids),
            )
            for aid, cid in cur.fetchall():
                stale_atom_count += 1
                affected_claim_ids.add(str(cid))

        # 3. Parent claims in (APPROVED, SUPPORT_VALIDATED) -> STALE
        stale_claim_ids: list[str] = []
        if affected_claim_ids:
            cur.execute(
                """
                UPDATE public.claims
                SET status = 'STALE',
                    updated_at = %s
                WHERE workspace_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND status IN ('APPROVED', 'SUPPORT_VALIDATED')
                RETURNING id::text
                """,
                (now, workspace_id, sorted(affected_claim_ids)),
            )
            stale_claim_ids = [str(r[0]) for r in cur.fetchall()]

        # 4. Dependent exports in (PENDING, READY) -> INVALIDATED
        invalidated_export_count = 0
        if stale_claim_ids:
            cur.execute("SELECT to_regclass('public.exports') IS NOT NULL")
            has_exports = cur.fetchone()
            if has_exports and bool(has_exports[0]):
                cur.execute(
                    """
                    UPDATE public.exports
                    SET status = 'INVALIDATED',
                        invalidation_reason = %s,
                        invalidated_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND status IN ('PENDING', 'READY')
                      AND id IN (
                          SELECT ec.export_id
                          FROM public.export_claims ec
                          WHERE ec.workspace_id = %s::uuid
                            AND ec.claim_id = ANY(%s::uuid[])
                      )
                    RETURNING id::text
                    """,
                    (
                        export_reason,
                        now,
                        now,
                        workspace_id,
                        workspace_id,
                        stale_claim_ids,
                    ),
                )
                invalidated_export_count = len(cur.fetchall())

        return FactInvalidationCounts(
            contributions=stale_contrib_count,
            contribution_supports=len(cs_rows),
            claim_atoms=stale_atom_count,
            claim_atom_supports=len(cas_rows),
            claims=len(stale_claim_ids),
            exports=invalidated_export_count,
        )

    @staticmethod
    def _insert_review_action(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        target_id: str,
        action: ReviewActionType,
        actor_user_id: str | None,
        from_version: int | None,
        to_version: int | None,
        idempotency_key: str,
        reason: str | None,
        action_metadata: dict[str, Any],
        created_at: datetime,
    ) -> str:
        assert_no_forbidden_raw_keys(action_metadata, field_name="action_metadata")
        ra_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO public.review_actions (
                id, workspace_id, target_type, target_id, action,
                actor_user_id, from_version, to_version, idempotency_key,
                reason, action_metadata, created_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid, %s,
                %s::uuid, %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                ra_id,
                workspace_id,
                ReviewTargetType.FACT.value,
                target_id,
                action.value,
                _parse_uuid_or_none(actor_user_id),
                from_version,
                to_version,
                idempotency_key,
                reason,
                Jsonb(action_metadata),
                created_at,
            ),
        )
        return ra_id

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
    ) -> str:
        assert_no_forbidden_raw_keys(redacted_metadata, field_name="redacted_metadata")
        audit_id = str(uuid.uuid4())
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
                audit_id,
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
        return audit_id

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
    "ConnectionFactory",
    "PostgresFactService",
]
