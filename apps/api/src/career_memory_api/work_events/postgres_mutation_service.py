"""PostgreSQL 18 Atomic Event Review & Curation Mutation Service (T-304 · T-304A · T-304B).

Executes every mutation strictly inside ``workspace_scoped_transaction`` under the
least-privilege ``cm_api_app`` role (NOBYPASSRLS + FORCE RLS) with:
1. Deterministic UUID-ascending row locking (``ORDER BY id ASC FOR UPDATE``) to prevent deadlocks.
2. In-transaction re-verification of live ``source_resources`` policy & ``deletion_jobs`` fences
   (``403 SOURCE_POLICY_BLOCKED`` / ``409 DELETION_PENDING`` with zero side effects).
3. Request-fingerprint idempotency via append-only ``public.review_actions`` (exact replay returns
   stored outcome without duplicate transitions; conflicting key reuse raises ``409 IDEMPOTENCY_CONFLICT``).
4. Optimistic concurrency via ``expected_version`` and optional ``If-Match`` header
   (``409 EVENT_VERSION_CONFLICT`` carrying current_version/current_status/etag/allowed_actions).
5. Append-only ``public.work_event_versions`` snapshots, ``public.event_constraints`` pairwise
   ``CANNOT_LINK`` guards, ``public.outbox_events`` downstream invalidation/regeneration intents,
   ``public.audit_events`` records, and Plan C non-raw JSONB enforcement throughout.
6. Optional ``before_commit_hook`` for deterministic concurrency barriers and rollback testing.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.schema_contracts.enums import (
    EventConstraintOrigin,
    EventConstraintStatus,
    EventConstraintType,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    RecordOrigin,
    ResourceVisibility,
    ReviewActionType,
    ReviewTargetType,
    SourceObjectLifecycleState,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.work_events.history import (
    append_event_version_snapshot,
    record_work_event_review_action,
)
from career_memory_api.work_events.models import (
    AffectedDownstreamImpact,
    ConfirmWorkEventRequest,
    DissolveWorkEventRequest,
    DissolveWorkEventResponse,
    EditWorkEventRequest,
    MergeWorkEventsRequest,
    MergeWorkEventsResponse,
    RejectWorkEventRequest,
    SplitPairwiseConstraintItem,
    SplitWorkEventRequest,
    SplitWorkEventResponse,
    StaleDownstreamImpact,
    WorkEventDetailResponse,
    WorkEventError,
    WorkEventPolicyState,
    compute_allowed_actions,
    format_work_event_etag,
    has_non_empty_anchor_scope,
    parse_if_match_version,
    resolve_idempotency_key,
)
from career_memory_api.work_events.postgres_read_service import PostgresWorkEventsReadService
from career_memory_api.work_events.relations import (
    check_merge_cannot_link_conflicts,
    record_pairwise_cannot_link,
    record_source_relation,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _LockedWorkEvent:
    event_id: str
    primary_anchor_source_object_id: str | None
    anchor_mode: WorkEventAnchorMode
    anchor_scope: dict[str, Any] | None
    version: int
    title: str
    problem: str | None
    action: str | None
    outcome: str | None
    summary_metadata: dict[str, Any]
    status: WorkEventStatus
    created_by: RecordOrigin
    occurred_start_at: datetime | None
    occurred_end_at: datetime | None


@dataclass(frozen=True, slots=True)
class _NormalizedPartition:
    title: str
    problem: str | None
    action: str | None
    outcome: str | None
    source_object_ids: list[str]
    file_paths: list[str]
    diff_hunks: list[str]
    review_thread_ids: list[str]
    content_unit_ids: list[str]


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _dedupe_clean_strings(items: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


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
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(params) >= 2:
        hook(action, cur)
    elif len(params) == 1:
        hook(action)
    else:
        hook()


class PostgresWorkEventMutationService:
    """Transactional PG18 domain service implementing all 6 WorkEvent curation commands."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        before_commit_hook: BeforeCommitHook | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._before_commit_hook = before_commit_hook
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    # =========================================================================
    # 1. CONFIRM (POST /v1/work-events/{id}/actions/confirm, UI Keep)
    # =========================================================================
    def confirm_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: ConfirmWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )
        self._assert_if_match_matches(if_match_header, request.expected_version)

        ev_uuid = _parse_uuid_or_none(event_id)
        if ev_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.CONFIRM.value,
                "event_id": ev_uuid,
                "expected_version": request.expected_version,
                "reason": request.reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid]
                )
                ev = locked.get(ev_uuid)
                if ev is None:
                    raise WorkspaceResourceNotFoundError()

                self._enforce_live_policy_and_deletion_fence(
                    cur, workspace_id=access.workspace_id, events=[ev]
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.CONFIRM,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                    expected_target_id=ev_uuid,
                )
                if replay is not None:
                    return PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                        cur, workspace_id=access.workspace_id, event_id=ev_uuid
                    )

                is_active_top = self._is_active_top_level(
                    cur, workspace_id=access.workspace_id, event=ev
                )
                self._assert_expected_version(
                    ev, expected_version=request.expected_version, is_active_top_level=is_active_top
                )

                if ev.status is WorkEventStatus.CONFIRMED and is_active_top:
                    raise WorkEventError(
                        status_code=409,
                        code="EVENT_ALREADY_CONFIRMED",
                        message="WorkEvent is already CONFIRMED.",
                    )
                if not is_active_top or ev.status not in (
                    WorkEventStatus.CANDIDATE,
                    WorkEventStatus.STALE,
                ):
                    raise WorkEventError(
                        status_code=409,
                        code="EVENT_NOT_ACTIVE",
                        message=f"WorkEvent in status {ev.status.value} cannot be confirmed.",
                    )

                now = self._now_fn()
                self._ensure_version_snapshot_exists(
                    cur, workspace_id=access.workspace_id, event=ev, created_at=now
                )

                next_version = ev.version + 1
                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "expected_version": request.expected_version,
                    "result_event_id": ev_uuid,
                    "result_version": next_version,
                }
                review_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=ev_uuid,
                    action=ReviewActionType.CONFIRM,
                    actor_user_id=access.user.user_id,
                    from_version=ev.version,
                    to_version=next_version,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'CONFIRMED',
                        version = %s,
                        reviewed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (next_version, now, now, access.workspace_id, ev_uuid),
                )

                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    version=next_version,
                    created_by_review_action_id=review_action.review_action_id,
                    created_at=now,
                )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_CONFIRMED",
                    target_id=ev_uuid,
                    metadata={
                        "from_version": ev.version,
                        "to_version": next_version,
                        "from_status": ev.status.value,
                        "to_status": WorkEventStatus.CONFIRMED.value,
                        "review_action_id": review_action.review_action_id,
                    },
                    created_at=now,
                )

                detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=ev_uuid
                )
                _invoke_before_commit_hook(self._before_commit_hook, action="CONFIRM", cur=cur)
                return detail

    # =========================================================================
    # 2. EDIT (PATCH /v1/work-events/{id})
    # =========================================================================
    def edit_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: EditWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )
        self._assert_if_match_matches(if_match_header, request.expected_version)

        clean_title = request.title.strip()
        if not clean_title:
            raise WorkEventError(
                status_code=422,
                code="INVALID_EDIT_REQUEST",
                message="title must be a non-empty string.",
            )

        ev_uuid = _parse_uuid_or_none(event_id)
        if ev_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.EDIT.value,
                "event_id": ev_uuid,
                "expected_version": request.expected_version,
                "title": clean_title,
                "problem": request.problem,
                "action_text": request.action,
                "outcome": request.outcome,
                "reason": request.reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid]
                )
                ev = locked.get(ev_uuid)
                if ev is None:
                    raise WorkspaceResourceNotFoundError()

                self._enforce_live_policy_and_deletion_fence(
                    cur, workspace_id=access.workspace_id, events=[ev]
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.EDIT,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                    expected_target_id=ev_uuid,
                )
                if replay is not None:
                    return PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                        cur, workspace_id=access.workspace_id, event_id=ev_uuid
                    )

                is_active_top = self._is_active_top_level(
                    cur, workspace_id=access.workspace_id, event=ev
                )
                self._assert_expected_version(
                    ev, expected_version=request.expected_version, is_active_top_level=is_active_top
                )

                if not is_active_top or ev.status not in (
                    WorkEventStatus.CANDIDATE,
                    WorkEventStatus.CONFIRMED,
                    WorkEventStatus.STALE,
                ):
                    raise WorkEventError(
                        status_code=409,
                        code="EVENT_NOT_ACTIVE",
                        message=f"WorkEvent in status {ev.status.value} cannot be edited.",
                    )

                now = self._now_fn()
                self._ensure_version_snapshot_exists(
                    cur, workspace_id=access.workspace_id, event=ev, created_at=now
                )

                next_version = ev.version + 1
                stale_facts, stale_claims = self._mark_downstream_stale(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid], now=now
                )

                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "expected_version": request.expected_version,
                    "result_event_id": ev_uuid,
                    "result_version": next_version,
                    "stale_fact_count": stale_facts,
                    "stale_claim_count": stale_claims,
                }
                review_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=ev_uuid,
                    action=ReviewActionType.EDIT,
                    actor_user_id=access.user.user_id,
                    from_version=ev.version,
                    to_version=next_version,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                cur.execute(
                    """
                    UPDATE public.work_events
                    SET title = %s,
                        problem = %s,
                        action = %s,
                        outcome = %s,
                        status = 'CANDIDATE',
                        version = %s,
                        created_by = 'USER',
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (
                        clean_title,
                        request.problem,
                        request.action,
                        request.outcome,
                        next_version,
                        now,
                        access.workspace_id,
                        ev_uuid,
                    ),
                )

                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    version=next_version,
                    created_by_review_action_id=review_action.review_action_id,
                    created_at=now,
                )

                if stale_facts > 0 or stale_claims > 0:
                    self._record_outbox_event(
                        cur,
                        workspace_id=access.workspace_id,
                        event_type="WORK_EVENT_EDITED",
                        aggregate_id=ev_uuid,
                        dedupe_key=f"work_event:edit:{ev_uuid}:v{next_version}",
                        payload={
                            "event_id": ev_uuid,
                            "from_version": ev.version,
                            "to_version": next_version,
                            "stale_fact_count": stale_facts,
                            "stale_claim_count": stale_claims,
                            "invalidation_required": True,
                        },
                        created_at=now,
                    )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_EDITED",
                    target_id=ev_uuid,
                    metadata={
                        "from_version": ev.version,
                        "to_version": next_version,
                        "from_status": ev.status.value,
                        "to_status": WorkEventStatus.CANDIDATE.value,
                        "review_action_id": review_action.review_action_id,
                        "stale_fact_count": stale_facts,
                        "stale_claim_count": stale_claims,
                    },
                    created_at=now,
                )

                detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=ev_uuid
                )
                _invoke_before_commit_hook(self._before_commit_hook, action="EDIT", cur=cur)
                return detail

    # =========================================================================
    # 3. REJECT (POST /v1/work-events/{id}/actions/reject, UI Exclude)
    # =========================================================================
    def reject_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: RejectWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> WorkEventDetailResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )
        self._assert_if_match_matches(if_match_header, request.expected_version)

        ev_uuid = _parse_uuid_or_none(event_id)
        if ev_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.REJECT.value,
                "event_id": ev_uuid,
                "expected_version": request.expected_version,
                "reason": request.reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid]
                )
                ev = locked.get(ev_uuid)
                if ev is None:
                    raise WorkspaceResourceNotFoundError()

                self._enforce_live_policy_and_deletion_fence(
                    cur, workspace_id=access.workspace_id, events=[ev]
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.REJECT,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                    expected_target_id=ev_uuid,
                )
                if replay is not None:
                    return PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                        cur, workspace_id=access.workspace_id, event_id=ev_uuid
                    )

                is_active_top = self._is_active_top_level(
                    cur, workspace_id=access.workspace_id, event=ev
                )
                self._assert_expected_version(
                    ev, expected_version=request.expected_version, is_active_top_level=is_active_top
                )

                if ev.anchor_mode is WorkEventAnchorMode.USER_COMPOSITE:
                    raise WorkEventError(
                        status_code=409,
                        code="USER_COMPOSITE_REJECT_REQUIRES_DISSOLVE",
                        message=(
                            "USER_COMPOSITE WorkEvents cannot be rejected directly; "
                            "use POST /v1/work-events/{id}/actions/dissolve instead."
                        ),
                    )
                if not is_active_top or ev.status not in (
                    WorkEventStatus.CANDIDATE,
                    WorkEventStatus.CONFIRMED,
                    WorkEventStatus.STALE,
                ):
                    raise WorkEventError(
                        status_code=409,
                        code="EVENT_NOT_ACTIVE",
                        message=f"WorkEvent in status {ev.status.value} cannot be rejected.",
                    )

                now = self._now_fn()
                self._ensure_version_snapshot_exists(
                    cur, workspace_id=access.workspace_id, event=ev, created_at=now
                )

                next_version = ev.version + 1
                stale_facts, stale_claims = self._mark_downstream_stale(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid], now=now
                )

                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "expected_version": request.expected_version,
                    "result_event_id": ev_uuid,
                    "result_version": next_version,
                    "stale_fact_count": stale_facts,
                    "stale_claim_count": stale_claims,
                }
                review_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=ev_uuid,
                    action=ReviewActionType.REJECT,
                    actor_user_id=access.user.user_id,
                    from_version=ev.version,
                    to_version=next_version,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                # EV-011: Never touch/delete source_objects, source_object_revisions, or content_units
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'REJECTED',
                        version = %s,
                        reviewed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (next_version, now, now, access.workspace_id, ev_uuid),
                )

                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    version=next_version,
                    created_by_review_action_id=review_action.review_action_id,
                    created_at=now,
                )

                if stale_facts > 0 or stale_claims > 0:
                    self._record_outbox_event(
                        cur,
                        workspace_id=access.workspace_id,
                        event_type="WORK_EVENT_REJECTED",
                        aggregate_id=ev_uuid,
                        dedupe_key=f"work_event:reject:{ev_uuid}:v{next_version}",
                        payload={
                            "event_id": ev_uuid,
                            "from_version": ev.version,
                            "to_version": next_version,
                            "stale_fact_count": stale_facts,
                            "stale_claim_count": stale_claims,
                        },
                        created_at=now,
                    )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_REJECTED",
                    target_id=ev_uuid,
                    metadata={
                        "from_version": ev.version,
                        "to_version": next_version,
                        "from_status": ev.status.value,
                        "to_status": WorkEventStatus.REJECTED.value,
                        "review_action_id": review_action.review_action_id,
                    },
                    created_at=now,
                )

                detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=ev_uuid
                )
                _invoke_before_commit_hook(self._before_commit_hook, action="REJECT", cur=cur)
                return detail

    # =========================================================================
    # 4. SPLIT (POST /v1/work-events/{id}/actions/split, T-304A, EV-008)
    # =========================================================================
    def split_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: SplitWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> SplitWorkEventResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )
        self._assert_if_match_matches(if_match_header, request.expected_version)

        normalized_partitions = self._validate_and_normalize_split_partitions(request.partitions)

        ev_uuid = _parse_uuid_or_none(event_id)
        if ev_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.SPLIT.value,
                "event_id": ev_uuid,
                "expected_version": request.expected_version,
                "reason": request.reason,
                "partitions": [
                    {
                        "title": p.title,
                        "problem": p.problem,
                        "action": p.action,
                        "outcome": p.outcome,
                        "source_object_ids": p.source_object_ids,
                        "file_paths": p.file_paths,
                        "diff_hunks": p.diff_hunks,
                        "review_thread_ids": p.review_thread_ids,
                        "content_unit_ids": p.content_unit_ids,
                    }
                    for p in normalized_partitions
                ],
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid]
                )
                parent = locked.get(ev_uuid)
                if parent is None:
                    raise WorkspaceResourceNotFoundError()

                self._enforce_live_policy_and_deletion_fence(
                    cur, workspace_id=access.workspace_id, events=[parent]
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.SPLIT,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                    expected_target_id=ev_uuid,
                )
                if replay is not None:
                    return self._hydrate_split_response_from_metadata(
                        cur,
                        workspace_id=access.workspace_id,
                        parent_event_id=ev_uuid,
                        action_metadata=replay,
                    )

                is_active_top = self._is_active_top_level(
                    cur, workspace_id=access.workspace_id, event=parent
                )
                self._assert_expected_version(
                    parent,
                    expected_version=request.expected_version,
                    is_active_top_level=is_active_top,
                )

                if not is_active_top or parent.status not in (
                    WorkEventStatus.CANDIDATE,
                    WorkEventStatus.CONFIRMED,
                    WorkEventStatus.STALE,
                ):
                    raise WorkEventError(
                        status_code=409,
                        code="EVENT_NOT_ACTIVE",
                        message=f"WorkEvent in status {parent.status.value} cannot be split.",
                    )

                if parent.anchor_mode is WorkEventAnchorMode.PR:
                    child_anchor_mode = WorkEventAnchorMode.PR_SEGMENT
                elif parent.anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT:
                    if has_non_empty_anchor_scope(parent.anchor_scope):
                        raise WorkEventError(
                            status_code=409,
                            code="SPLIT_NOT_ALLOWED",
                            message="Already scope-split DIRECT_COMMIT cannot be split again.",
                        )
                    child_anchor_mode = WorkEventAnchorMode.DIRECT_COMMIT
                else:
                    raise WorkEventError(
                        status_code=409,
                        code="SPLIT_NOT_ALLOWED",
                        message=(
                            f"WorkEvent with anchor_mode {parent.anchor_mode.value} "
                            "does not support SPLIT."
                        ),
                    )

                if parent.primary_anchor_source_object_id is None:
                    raise WorkEventError(
                        status_code=422,
                        code="INVALID_SPLIT_SCOPE",
                        message="Split target must have a primary anchor source object.",
                    )

                # Verify partition source_object_ids & content_unit_ids belong to parent
                parent_must_links = self._load_parent_active_memberships(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                )
                parent_context_links = self._load_parent_active_memberships(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                )

                allowed_parent_source_ids: set[str] = {parent.primary_anchor_source_object_id}
                allowed_parent_source_ids.update(parent_must_links.keys())

                all_requested_source_ids: set[str] = set()
                all_requested_cu_ids: set[str] = set()
                for part in normalized_partitions:
                    all_requested_source_ids.update(part.source_object_ids)
                    all_requested_cu_ids.update(part.content_unit_ids)

                invalid_sources = sorted(all_requested_source_ids - allowed_parent_source_ids)
                if invalid_sources:
                    raise WorkEventError(
                        status_code=422,
                        code="INVALID_SPLIT_SCOPE",
                        message="Split partition references source_object_id outside parent event scope.",
                        details={"invalid_source_object_ids": invalid_sources},
                    )

                if all_requested_cu_ids:
                    cur.execute(
                        """
                        SELECT cu.id::text, rev.source_object_id::text
                        FROM public.content_units cu
                        INNER JOIN public.source_object_revisions rev
                            ON rev.workspace_id = cu.workspace_id
                           AND rev.id = cu.source_object_revision_id
                        WHERE cu.workspace_id = %s::uuid
                          AND cu.id = ANY(%s::uuid[])
                        """,
                        (access.workspace_id, sorted(all_requested_cu_ids)),
                    )
                    cu_owner_map = {str(r[0]): str(r[1]) for r in cur.fetchall()}
                    invalid_cus = sorted(
                        cu_id
                        for cu_id in all_requested_cu_ids
                        if cu_owner_map.get(cu_id) not in allowed_parent_source_ids
                    )
                    if invalid_cus:
                        raise WorkEventError(
                            status_code=422,
                            code="INVALID_SPLIT_SCOPE",
                            message="Split partition references content_unit_id outside parent event scope.",
                            details={"invalid_content_unit_ids": invalid_cus},
                        )

                now = self._now_fn()
                self._ensure_version_snapshot_exists(
                    cur, workspace_id=access.workspace_id, event=parent, created_at=now
                )

                segment_ids = [str(uuid.uuid4()) for _ in normalized_partitions]
                next_parent_version = parent.version + 1
                stale_facts, stale_claims = self._mark_downstream_stale(
                    cur, workspace_id=access.workspace_id, event_ids=[ev_uuid], now=now
                )

                linked_res_ids = PostgresWorkEventsReadService._resolve_linked_source_resource_ids(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    primary_anchor_source_object_id=parent.primary_anchor_source_object_id,
                )
                primary_res_id = linked_res_ids[0] if linked_res_ids else None

                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "expected_version": request.expected_version,
                    "created_segment_ids": segment_ids,
                    "stale_fact_count": stale_facts,
                    "stale_claim_count": stale_claims,
                }
                split_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=ev_uuid,
                    action=ReviewActionType.SPLIT,
                    actor_user_id=access.user.user_id,
                    from_version=parent.version,
                    to_version=next_parent_version,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                # 1. Supersede parent & append parent SUPERSEDED snapshot
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'SUPERSEDED',
                        version = %s,
                        reviewed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (next_parent_version, now, now, access.workspace_id, ev_uuid),
                )
                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=ev_uuid,
                    version=next_parent_version,
                    created_by_review_action_id=split_action.review_action_id,
                    created_at=now,
                )

                # 2. Create child segments + memberships + v1 snapshots
                for idx, (seg_id, part) in enumerate(
                    zip(segment_ids, normalized_partitions, strict=True)
                ):
                    child_scope: dict[str, Any] = {"kind": child_anchor_mode.value}
                    if part.file_paths:
                        child_scope["file_paths"] = part.file_paths
                    if part.diff_hunks:
                        child_scope["diff_hunks"] = part.diff_hunks
                        child_scope["hunk_ranges"] = part.diff_hunks
                    if part.review_thread_ids:
                        child_scope["review_thread_ids"] = part.review_thread_ids
                    if part.content_unit_ids:
                        child_scope["content_unit_ids"] = part.content_unit_ids
                    assert_no_forbidden_raw_keys(child_scope, field_name="work_events.anchor_scope")

                    child_meta: dict[str, Any] = {
                        "parent_event_id": ev_uuid,
                        "parent_version": parent.version,
                        "split_ordinal": idx,
                    }
                    if primary_res_id is not None:
                        child_meta["source_resource_id"] = primary_res_id
                    if child_anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT:
                        for dc_key in (
                            "default_branch",
                            "abbreviated_sha",
                            "commit_sha",
                            "committed_at",
                            "default_branch_reachable",
                            "pr_association_state",
                            "requires_user_confirmation",
                        ):
                            if dc_key in parent.summary_metadata:
                                child_meta[dc_key] = parent.summary_metadata[dc_key]
                    assert_no_forbidden_raw_keys(
                        child_meta, field_name="work_events.summary_metadata"
                    )

                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id,
                            workspace_id,
                            primary_anchor_source_object_id,
                            anchor_mode,
                            anchor_scope,
                            version,
                            title,
                            problem,
                            action,
                            outcome,
                            summary_metadata,
                            status,
                            created_by,
                            occurred_start_at,
                            occurred_end_at,
                            created_at,
                            reviewed_at,
                            updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb,
                            1, %s, %s, %s, %s, %s::jsonb,
                            'CANDIDATE', 'USER', %s, %s, %s, NULL, %s
                        )
                        """,
                        (
                            seg_id,
                            access.workspace_id,
                            parent.primary_anchor_source_object_id,
                            child_anchor_mode.value,
                            json.dumps(child_scope, sort_keys=True),
                            part.title,
                            part.problem,
                            part.action,
                            part.outcome,
                            json.dumps(child_meta, sort_keys=True),
                            parent.occurred_start_at,
                            parent.occurred_end_at,
                            now,
                            now,
                        ),
                    )

                    # Primary anchor MUST_LINK
                    record_source_relation(
                        cur,
                        workspace_id=access.workspace_id,
                        event_id=seg_id,
                        source_object_id=parent.primary_anchor_source_object_id,
                        relation_class=EventMembershipRelationClass.MUST_LINK,
                        relation_type="PRIMARY_ANCHOR",
                        origin=EventMembershipOrigin.USER,
                        rationale_code="SPLIT_PRIMARY_ANCHOR",
                        created_at=now,
                    )

                    # Partition-scoped MUST_LINK evidence
                    for src_id in part.source_object_ids:
                        if src_id == parent.primary_anchor_source_object_id:
                            continue
                        rel_type, _orig, _rat = parent_must_links.get(
                            src_id, ("SPLIT_EVIDENCE", EventMembershipOrigin.USER, None)
                        )
                        if rel_type == "PRIMARY_ANCHOR":
                            rel_type = "SPLIT_EVIDENCE"
                        record_source_relation(
                            cur,
                            workspace_id=access.workspace_id,
                            event_id=seg_id,
                            source_object_id=src_id,
                            relation_class=EventMembershipRelationClass.MUST_LINK,
                            relation_type=rel_type,
                            origin=EventMembershipOrigin.USER,
                            rationale_code="SPLIT_PARTITION_EVIDENCE",
                            created_at=now,
                        )

                    # Inherited CONTEXT_ONLY relations (never promoted to MUST_LINK!)
                    for ctx_src_id, (ctx_rel_type, _orig, _rat) in parent_context_links.items():
                        record_source_relation(
                            cur,
                            workspace_id=access.workspace_id,
                            event_id=seg_id,
                            source_object_id=ctx_src_id,
                            relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                            relation_type=ctx_rel_type,
                            origin=EventMembershipOrigin.RULE,
                            rationale_code="INHERITED_FROM_SPLIT_PARENT",
                            created_at=now,
                        )

                    append_event_version_snapshot(
                        cur,
                        workspace_id=access.workspace_id,
                        event_id=seg_id,
                        version=1,
                        created_by_review_action_id=split_action.review_action_id,
                        created_at=now,
                    )

                # 3. Record pairwise CANNOT_LINK between all created sibling pairs (OD-007)
                pairwise_items: list[SplitPairwiseConstraintItem] = []
                for i in range(len(segment_ids)):
                    for j in range(i + 1, len(segment_ids)):
                        rec = record_pairwise_cannot_link(
                            cur,
                            workspace_id=access.workspace_id,
                            event_a_id=segment_ids[i],
                            event_b_id=segment_ids[j],
                            origin=EventConstraintOrigin.SPLIT,
                            rationale_code="SPLIT_SIBLING_CANNOT_LINK",
                            review_action_id=split_action.review_action_id,
                            metadata={
                                "parent_event_id": ev_uuid,
                                "parent_version": parent.version,
                            },
                            created_at=now,
                        )
                        pairwise_items.append(
                            SplitPairwiseConstraintItem(
                                constraint_id=rec.constraint_id,
                                left_event_id=rec.left_event_id,
                                right_event_id=rec.right_event_id,
                                constraint_type=rec.constraint_type,
                                origin=rec.origin,
                                status=rec.status,
                                rationale_code=rec.rationale_code,
                                created_by_review_action_id=rec.created_by_review_action_id,
                                created_at=rec.created_at,
                            )
                        )

                self._record_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="WORK_EVENT_SPLIT",
                    aggregate_id=ev_uuid,
                    dedupe_key=f"work_event:split:{ev_uuid}:v{next_parent_version}",
                    payload={
                        "parent_event_id": ev_uuid,
                        "parent_version": next_parent_version,
                        "created_segment_ids": segment_ids,
                        "stale_fact_count": stale_facts,
                        "stale_claim_count": stale_claims,
                    },
                    created_at=now,
                )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_SPLIT",
                    target_id=ev_uuid,
                    metadata={
                        "from_version": parent.version,
                        "to_version": next_parent_version,
                        "created_segment_ids": segment_ids,
                        "pairwise_constraint_count": len(pairwise_items),
                        "review_action_id": split_action.review_action_id,
                    },
                    created_at=now,
                )

                superseded_parent = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=ev_uuid
                )
                created_segments = [
                    PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                        cur, workspace_id=access.workspace_id, event_id=seg_id
                    )
                    for seg_id in segment_ids
                ]
                _invoke_before_commit_hook(self._before_commit_hook, action="SPLIT", cur=cur)
                return SplitWorkEventResponse(
                    superseded_parent=superseded_parent,
                    created_segments=created_segments,
                    pairwise_constraints_created=pairwise_items,
                )

    # =========================================================================
    # 5. MERGE (POST /v1/work-events/actions/merge, T-304B, EV-009..016, EV-021)
    # =========================================================================
    def merge_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        request: MergeWorkEventsRequest,
        idempotency_key_header: str | None = None,
        conceal_missing_as_not_found: bool = True,
    ) -> MergeWorkEventsResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )

        if len(request.members) < 2:
            raise WorkEventError(
                status_code=422,
                code="INVALID_MERGE_REQUEST",
                message="Manual merge requires at least 2 member events.",
            )

        resolved_title = (request.title or request.composite_title or "").strip()
        if not resolved_title:
            raise WorkEventError(
                status_code=422,
                code="INVALID_MERGE_REQUEST",
                message="title (or composite_title) must be a non-empty string.",
            )

        # 1. Duplicate member check (before DB lock)
        seen_raw: set[str] = set()
        seen_uuids: set[str] = set()
        ordered_member_refs: list[tuple[str, int]] = []
        for member in request.members:
            raw_id = member.event_id.strip()
            norm_uuid = _parse_uuid_or_none(raw_id)
            if raw_id in seen_raw or (norm_uuid is not None and norm_uuid in seen_uuids):
                raise WorkEventError(
                    status_code=409,
                    code="DUPLICATE_COMPOSITE_MEMBER",
                    message=f"Duplicate member event_id {raw_id!r} in merge request.",
                    details={"event_id": norm_uuid or raw_id},
                )
            seen_raw.add(raw_id)
            if norm_uuid is not None:
                seen_uuids.add(norm_uuid)
            ordered_member_refs.append((norm_uuid or raw_id, member.expected_version))

        # Non-UUID member IDs cannot exist in work_events or source_objects
        for ref_id, _ in ordered_member_refs:
            if _parse_uuid_or_none(ref_id) is None:
                if conceal_missing_as_not_found:
                    raise WorkspaceResourceNotFoundError()
                raise WorkEventError(
                    status_code=403,
                    code="CROSS_WORKSPACE_MEMBER",
                    message="Member event is outside the active workspace or does not exist.",
                )

        ordered_member_ids = [ref_id for ref_id, _ in ordered_member_refs]
        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.MERGE.value,
                "members": [
                    {"event_id": eid, "expected_version": ver}
                    for eid, ver in ordered_member_refs
                ],
                "title": resolved_title,
                "problem": request.problem,
                "action_text": request.action,
                "outcome": request.outcome,
                "reason": request.reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                # Lock all member work_events deterministically in UUID ascending order
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=ordered_member_ids
                )

                # 2. Check missing member IDs (EV-013 non-event / UNKNOWN commit vs EV-016 missing/cross-workspace)
                missing_ids = [eid for eid in ordered_member_ids if eid not in locked]
                if missing_ids:
                    cur.execute(
                        """
                        SELECT id::text, object_type, pr_association_state
                        FROM public.source_objects
                        WHERE workspace_id = %s::uuid
                          AND id = ANY(%s::uuid[])
                        """,
                        (access.workspace_id, missing_ids),
                    )
                    src_rows = {str(r[0]): (str(r[1]), r[2]) for r in cur.fetchall()}
                    for mid in missing_ids:
                        if mid in src_rows:
                            obj_type, assoc_state = src_rows[mid]
                            if (
                                obj_type == SourceObjectType.COMMIT.value
                                and assoc_state == PrAssociationState.UNKNOWN.value
                            ):
                                raise WorkEventError(
                                    status_code=409,
                                    code="PR_ASSOCIATION_INCOMPLETE",
                                    message=(
                                        "Unpromoted commit with UNKNOWN PR association cannot be merged "
                                        "until association verification completes."
                                    ),
                                    details={"source_object_id": mid},
                                )
                            raise WorkEventError(
                                status_code=422,
                                code="NON_EVENT_MEMBER_NOT_ALLOWED",
                                message=(
                                    f"Source object {mid} (type={obj_type}) is not a WorkEvent and "
                                    "cannot be merged as a composite member."
                                ),
                                details={"source_object_id": mid, "object_type": obj_type},
                            )
                    if conceal_missing_as_not_found:
                        raise WorkspaceResourceNotFoundError()
                    raise WorkEventError(
                        status_code=403,
                        code="CROSS_WORKSPACE_MEMBER",
                        message="One or more member events do not belong to the active workspace.",
                    )

                ordered_events = [locked[eid] for eid in ordered_member_ids]

                # 3. Re-verify live policy & deletion fence across all members
                self._enforce_live_policy_and_deletion_fence(
                    cur, workspace_id=access.workspace_id, events=ordered_events
                )

                # 4. Idempotency replay check (before status/version checks!)
                replay_row = self._check_idempotency_replay_with_target(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.MERGE,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                )
                if replay_row is not None:
                    composite_id, replay_meta = replay_row
                    return self._hydrate_merge_response_from_metadata(
                        cur,
                        workspace_id=access.workspace_id,
                        composite_event_id=composite_id,
                        action_metadata=replay_meta,
                    )

                # 5. Same repository (source_resource_id) check (EV-016)
                distinct_resource_ids: set[str] = set()
                for ev in ordered_events:
                    res_ids = PostgresWorkEventsReadService._resolve_linked_source_resource_ids(
                        cur,
                        workspace_id=access.workspace_id,
                        event_id=ev.event_id,
                        primary_anchor_source_object_id=ev.primary_anchor_source_object_id,
                    )
                    if not res_ids and isinstance(ev.summary_metadata.get("source_resource_id"), str):
                        res_ids = [str(ev.summary_metadata["source_resource_id"])]
                    distinct_resource_ids.update(res_ids)

                if len(distinct_resource_ids) > 1:
                    raise WorkEventError(
                        status_code=409,
                        code="CROSS_REPOSITORY_MEMBER",
                        message="All composite members must belong to the same source repository.",
                        details={"source_resource_ids": sorted(distinct_resource_ids)},
                    )
                shared_resource_id = next(iter(distinct_resource_ids), None)

                # 6. Nested USER_COMPOSITE prohibition (EV-014)
                for ev in ordered_events:
                    if ev.anchor_mode is WorkEventAnchorMode.USER_COMPOSITE:
                        raise WorkEventError(
                            status_code=409,
                            code="NESTED_COMPOSITE_NOT_ALLOWED",
                            message="Nested USER_COMPOSITE merge is strictly prohibited.",
                            details={"event_id": ev.event_id},
                        )

                # 7. Already composed member prohibition (EV-014)
                cur.execute(
                    """
                    SELECT member_event_id::text, composite_event_id::text
                    FROM public.event_composite_memberships
                    WHERE workspace_id = %s::uuid
                      AND member_event_id = ANY(%s::uuid[])
                      AND status = 'ACTIVE'
                    ORDER BY member_event_id ASC
                    """,
                    (access.workspace_id, ordered_member_ids),
                )
                composed_row = cur.fetchone()
                if composed_row is not None:
                    raise WorkEventError(
                        status_code=409,
                        code="MEMBER_ALREADY_COMPOSED",
                        message="Member event already belongs to an active USER_COMPOSITE.",
                        details={
                            "member_event_id": str(composed_row[0]),
                            "composite_event_id": str(composed_row[1]),
                        },
                    )

                # 8. Active status check: strictly CANDIDATE or CONFIRMED (EV-014)
                for ev in ordered_events:
                    if ev.status not in (WorkEventStatus.CANDIDATE, WorkEventStatus.CONFIRMED):
                        raise WorkEventError(
                            status_code=409,
                            code="MEMBER_NOT_ACTIVE",
                            message=(
                                f"Member event {ev.event_id} in status {ev.status.value} "
                                "is not eligible for merge."
                            ),
                            details={
                                "event_id": ev.event_id,
                                "current_status": ev.status.value,
                            },
                        )

                # 9. Optimistic concurrency expected_version check per member (ER-004 / EVENT-09)
                for ev, (_, expected_ver) in zip(ordered_events, ordered_member_refs, strict=True):
                    self._assert_expected_version(
                        ev, expected_version=expected_ver, is_active_top_level=True
                    )

                # 10. Pairwise & source-level CANNOT_LINK conflict check (CAND-EV-021 / SPLIT siblings)
                conflicts = check_merge_cannot_link_conflicts(
                    cur,
                    workspace_id=access.workspace_id,
                    member_event_ids=ordered_member_ids,
                )
                if conflicts:
                    raise WorkEventError(
                        status_code=409,
                        code="CANNOT_LINK_CONFLICT",
                        message=(
                            "Selected events have active CANNOT_LINK constraints and cannot be merged."
                        ),
                        details={
                            "conflicts": [c.model_dump(mode="json") for c in conflicts],
                        },
                    )

                # =============================================================
                # Atomic 7-Step Merge Execution (T-304B)
                # =============================================================
                now = self._now_fn()

                # Step 1: Ensure pre-merge version snapshot exists for every member
                for ev in ordered_events:
                    self._ensure_version_snapshot_exists(
                        cur, workspace_id=access.workspace_id, event=ev, created_at=now
                    )

                # Step 2: Insert new USER_COMPOSITE work_events row (NO MUST_LINK rows!)
                composite_id = str(uuid.uuid4())
                starts = [ev.occurred_start_at for ev in ordered_events if ev.occurred_start_at is not None]
                ends = [ev.occurred_end_at for ev in ordered_events if ev.occurred_end_at is not None]
                comp_start = min(starts) if starts else None
                comp_end = max(ends) if ends else None
                if comp_start is not None and comp_end is not None and comp_end < comp_start:
                    comp_end = comp_start

                comp_meta: dict[str, Any] = {
                    "merge_origin": "USER_MANUAL_MERGE",
                    "regeneration_status": "PENDING",
                }
                if shared_resource_id is not None:
                    comp_meta["source_resource_id"] = shared_resource_id
                if request.reason is not None:
                    comp_meta["merge_reason"] = request.reason
                assert_no_forbidden_raw_keys(comp_meta, field_name="work_events.summary_metadata")

                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id,
                        workspace_id,
                        primary_anchor_source_object_id,
                        anchor_mode,
                        anchor_scope,
                        version,
                        title,
                        problem,
                        action,
                        outcome,
                        summary_metadata,
                        status,
                        created_by,
                        occurred_start_at,
                        occurred_end_at,
                        created_at,
                        reviewed_at,
                        updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, NULL, 'USER_COMPOSITE', NULL,
                        1, %s, %s, %s, %s, %s::jsonb,
                        'CANDIDATE', 'USER', %s, %s, %s, NULL, %s
                    )
                    """,
                    (
                        composite_id,
                        access.workspace_id,
                        resolved_title,
                        request.problem,
                        request.action,
                        request.outcome,
                        json.dumps(comp_meta, sort_keys=True),
                        comp_start,
                        comp_end,
                        now,
                        now,
                    ),
                )

                # Step 6 (pre-count & invalidate member downstream facts/claims; NEVER copy to composite!)
                affected_facts, affected_claims = self._mark_downstream_stale(
                    cur,
                    workspace_id=access.workspace_id,
                    event_ids=ordered_member_ids,
                    now=now,
                )
                affected_exports = 0
                invalidated_total = affected_facts + affected_claims

                # Step 3: Record MERGE review_action & audit_event
                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "superseded_member_ids": ordered_member_ids,
                    "invalidated_downstream_count": invalidated_total,
                    "affected_downstream_impact": {
                        "fact_count": affected_facts,
                        "claim_count": affected_claims,
                        "export_count": affected_exports,
                    },
                    "regeneration_job_status": "PENDING",
                }
                merge_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=composite_id,
                    action=ReviewActionType.MERGE,
                    actor_user_id=access.user.user_id,
                    from_version=None,
                    to_version=1,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_COMPOSITE_MERGED",
                    target_id=composite_id,
                    metadata={
                        "member_event_ids": ordered_member_ids,
                        "source_resource_id": shared_resource_id,
                        "invalidated_downstream_count": invalidated_total,
                        "review_action_id": merge_action.review_action_id,
                    },
                    created_at=now,
                )

                # Step 4 & Step 5: Insert event_composite_memberships & supersede each member
                for ordinal, ev in enumerate(ordered_events):
                    cur.execute(
                        """
                        INSERT INTO public.event_composite_memberships (
                            id,
                            workspace_id,
                            composite_event_id,
                            composite_event_version,
                            member_event_id,
                            member_event_version,
                            ordinal,
                            status,
                            added_by_review_action_id,
                            created_at,
                            removed_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 1,
                            %s::uuid, %s, %s, 'ACTIVE', %s::uuid, %s, NULL
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            access.workspace_id,
                            composite_id,
                            ev.event_id,
                            ev.version,
                            ordinal,
                            merge_action.review_action_id,
                            now,
                        ),
                    )
                    next_member_ver = ev.version + 1
                    cur.execute(
                        """
                        UPDATE public.work_events
                        SET status = 'SUPERSEDED',
                            version = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (next_member_ver, now, access.workspace_id, ev.event_id),
                    )
                    append_event_version_snapshot(
                        cur,
                        workspace_id=access.workspace_id,
                        event_id=ev.event_id,
                        version=next_member_ver,
                        created_by_review_action_id=merge_action.review_action_id,
                        created_at=now,
                    )

                # Step 6b: Record outbox regeneration intent in same transaction
                self._record_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="WORK_EVENT_COMPOSITE_MERGED",
                    aggregate_id=composite_id,
                    dedupe_key=f"work_event:merge:{composite_id}:v1",
                    payload={
                        "composite_event_id": composite_id,
                        "composite_version": 1,
                        "member_event_ids": ordered_member_ids,
                        "source_resource_id": shared_resource_id,
                        "invalidated_downstream_count": invalidated_total,
                        "regeneration_required": True,
                    },
                    created_at=now,
                )

                # Step 7: Append composite v1 snapshot (after composite memberships exist!)
                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=composite_id,
                    version=1,
                    created_by_review_action_id=merge_action.review_action_id,
                    created_at=now,
                )

                composite_detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=composite_id
                )
                _invoke_before_commit_hook(self._before_commit_hook, action="MERGE", cur=cur)
                return MergeWorkEventsResponse(
                    composite_event=composite_detail,
                    superseded_member_ids=ordered_member_ids,
                    invalidated_downstream_count=invalidated_total,
                    affected_downstream_impact=AffectedDownstreamImpact(
                        fact_count=affected_facts,
                        claim_count=affected_claims,
                        export_count=affected_exports,
                    ),
                    regeneration_job_status="PENDING",
                )

    # =========================================================================
    # 6. DISSOLVE (POST /v1/work-events/{id}/actions/dissolve, T-304B, EV-012)
    # =========================================================================
    def dissolve_work_event(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
        request: DissolveWorkEventRequest,
        idempotency_key_header: str | None = None,
        if_match_header: str | None = None,
    ) -> DissolveWorkEventResponse:
        idem_key = resolve_idempotency_key(
            header_key=idempotency_key_header,
            body_key=request.idempotency_key,
        )
        self._assert_if_match_matches(if_match_header, request.expected_version)

        comp_uuid = _parse_uuid_or_none(event_id)
        if comp_uuid is None:
            raise WorkspaceResourceNotFoundError()

        fingerprint = _compute_request_fingerprint(
            {
                "action": ReviewActionType.DISSOLVE.value,
                "event_id": comp_uuid,
                "expected_version": request.expected_version,
                "reason": request.reason,
            }
        )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                # Read active member IDs first so we lock composite + all members in UUID ASC order
                cur.execute(
                    """
                    SELECT member_event_id::text, member_event_version, ordinal
                    FROM public.event_composite_memberships
                    WHERE workspace_id = %s::uuid
                      AND composite_event_id = %s::uuid
                      AND status = 'ACTIVE'
                    ORDER BY ordinal ASC, member_event_id ASC
                    """,
                    (access.workspace_id, comp_uuid),
                )
                active_member_rows = [(str(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]
                lock_ids = [comp_uuid, *[m_id for m_id, _, _ in active_member_rows]]
                locked = self._lock_events_ordered(
                    cur, workspace_id=access.workspace_id, event_ids=lock_ids
                )

                comp = locked.get(comp_uuid)
                if comp is None:
                    raise WorkspaceResourceNotFoundError()

                # Re-query active members after locking to guard against concurrent transitions
                cur.execute(
                    """
                    SELECT member_event_id::text, member_event_version, ordinal
                    FROM public.event_composite_memberships
                    WHERE workspace_id = %s::uuid
                      AND composite_event_id = %s::uuid
                      AND status = 'ACTIVE'
                    ORDER BY ordinal ASC, member_event_id ASC
                    """,
                    (access.workspace_id, comp_uuid),
                )
                active_member_rows = [(str(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]
                missing_lock_members = [
                    m_id for m_id, _, _ in active_member_rows if m_id not in locked
                ]
                if missing_lock_members:
                    extra_locked = self._lock_events_ordered(
                        cur, workspace_id=access.workspace_id, event_ids=missing_lock_members
                    )
                    locked.update(extra_locked)

                member_events_to_check = [
                    locked[m_id] for m_id, _, _ in active_member_rows if m_id in locked
                ]
                self._enforce_live_policy_and_deletion_fence(
                    cur,
                    workspace_id=access.workspace_id,
                    events=[comp, *member_events_to_check],
                )

                replay = self._check_idempotency_replay(
                    cur,
                    workspace_id=access.workspace_id,
                    action=ReviewActionType.DISSOLVE,
                    idempotency_key=idem_key,
                    fingerprint=fingerprint,
                    expected_target_id=comp_uuid,
                )
                if replay is not None:
                    return self._hydrate_dissolve_response_from_metadata(
                        cur,
                        workspace_id=access.workspace_id,
                        composite_event_id=comp_uuid,
                        action_metadata=replay,
                    )

                is_active_top = self._is_active_top_level(
                    cur, workspace_id=access.workspace_id, event=comp
                )
                self._assert_expected_version(
                    comp,
                    expected_version=request.expected_version,
                    is_active_top_level=is_active_top,
                )

                if (
                    comp.anchor_mode is not WorkEventAnchorMode.USER_COMPOSITE
                    or not is_active_top
                    or comp.status
                    not in (
                        WorkEventStatus.CANDIDATE,
                        WorkEventStatus.CONFIRMED,
                        WorkEventStatus.STALE,
                    )
                ):
                    raise WorkEventError(
                        status_code=409,
                        code="NOT_ACTIVE_USER_COMPOSITE",
                        message="Only an active top-level USER_COMPOSITE WorkEvent can be dissolved.",
                    )

                now = self._now_fn()
                self._ensure_version_snapshot_exists(
                    cur, workspace_id=access.workspace_id, event=comp, created_at=now
                )

                next_comp_version = comp.version + 1
                stale_facts, stale_claims = self._mark_downstream_stale(
                    cur, workspace_id=access.workspace_id, event_ids=[comp_uuid], now=now
                )
                stale_exports = 0
                invalidated_total = stale_facts + stale_claims
                restored_member_ids = [m_id for m_id, _, _ in active_member_rows if m_id in locked]

                action_meta = {
                    "origin": "USER",
                    "request_fingerprint": fingerprint,
                    "expected_version": request.expected_version,
                    "restored_member_ids": restored_member_ids,
                    "invalidated_downstream_count": invalidated_total,
                    "stale_downstream_impact": {
                        "stale_fact_count": stale_facts,
                        "stale_claim_count": stale_claims,
                        "stale_export_count": stale_exports,
                    },
                    "cleanup_job_status": "PENDING",
                }
                dissolve_action = record_work_event_review_action(
                    cur,
                    workspace_id=access.workspace_id,
                    target_event_id=comp_uuid,
                    action=ReviewActionType.DISSOLVE,
                    actor_user_id=access.user.user_id,
                    from_version=comp.version,
                    to_version=next_comp_version,
                    idempotency_key=idem_key,
                    reason=request.reason,
                    origin="USER",
                    action_metadata=action_meta,
                    created_at=now,
                )

                # 1. Mark composite SUPERSEDED & remove active composite memberships
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET status = 'SUPERSEDED',
                        version = %s,
                        reviewed_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (next_comp_version, now, now, access.workspace_id, comp_uuid),
                )
                cur.execute(
                    """
                    UPDATE public.event_composite_memberships
                    SET status = 'REMOVED',
                        removed_at = %s
                    WHERE workspace_id = %s::uuid
                      AND composite_event_id = %s::uuid
                      AND status = 'ACTIVE'
                    """,
                    (now, access.workspace_id, comp_uuid),
                )

                # 2. Re-evaluate each member's restoration status (CANDIDATE / CONFIRMED / STALE)
                restored_statuses: dict[str, str] = {}
                for m_id, pre_merge_ver, _ordinal in active_member_rows:
                    member_ev = locked.get(m_id)
                    if member_ev is None:
                        continue
                    restored_status = self._evaluate_dissolve_member_restoration_status(
                        cur,
                        workspace_id=access.workspace_id,
                        member=member_ev,
                        pre_merge_version=pre_merge_ver,
                    )
                    restored_statuses[m_id] = restored_status.value
                    next_member_ver = member_ev.version + 1
                    cur.execute(
                        """
                        UPDATE public.work_events
                        SET status = %s,
                            version = %s,
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (restored_status.value, next_member_ver, now, access.workspace_id, m_id),
                    )
                    append_event_version_snapshot(
                        cur,
                        workspace_id=access.workspace_id,
                        event_id=m_id,
                        version=next_member_ver,
                        created_by_review_action_id=dissolve_action.review_action_id,
                        created_at=now,
                    )

                # 3. Append composite SUPERSEDED snapshot + outbox cleanup intent + audit_event
                append_event_version_snapshot(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=comp_uuid,
                    version=next_comp_version,
                    created_by_review_action_id=dissolve_action.review_action_id,
                    created_at=now,
                )

                self._record_outbox_event(
                    cur,
                    workspace_id=access.workspace_id,
                    event_type="WORK_EVENT_COMPOSITE_DISSOLVED",
                    aggregate_id=comp_uuid,
                    dedupe_key=f"work_event:dissolve:{comp_uuid}:v{next_comp_version}",
                    payload={
                        "composite_event_id": comp_uuid,
                        "composite_version": next_comp_version,
                        "restored_member_ids": restored_member_ids,
                        "restored_statuses": restored_statuses,
                        "invalidated_downstream_count": invalidated_total,
                        "cleanup_required": True,
                    },
                    created_at=now,
                )

                self._record_audit_event(
                    cur,
                    workspace_id=access.workspace_id,
                    actor_user_id=access.user.user_id,
                    action="WORK_EVENT_COMPOSITE_DISSOLVED",
                    target_id=comp_uuid,
                    metadata={
                        "from_version": comp.version,
                        "to_version": next_comp_version,
                        "restored_member_ids": restored_member_ids,
                        "restored_statuses": restored_statuses,
                        "invalidated_downstream_count": invalidated_total,
                        "review_action_id": dissolve_action.review_action_id,
                    },
                    created_at=now,
                )

                comp_detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                    cur, workspace_id=access.workspace_id, event_id=comp_uuid
                )
                restored_details = [
                    PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                        cur, workspace_id=access.workspace_id, event_id=m_id
                    )
                    for m_id in restored_member_ids
                ]
                _invoke_before_commit_hook(self._before_commit_hook, action="DISSOLVE", cur=cur)
                return DissolveWorkEventResponse(
                    dissolved_composite=comp_detail,
                    superseded_composite=comp_detail,
                    restored_members=restored_details,
                    invalidated_downstream_count=invalidated_total,
                    stale_downstream_impact=StaleDownstreamImpact(
                        stale_fact_count=stale_facts,
                        stale_claim_count=stale_claims,
                        stale_export_count=stale_exports,
                    ),
                    cleanup_job_status="PENDING",
                )

    # =========================================================================
    # Shared Internal Helpers (Locking, Policy, Idempotency, Concurrency, Split)
    # =========================================================================
    @staticmethod
    def _assert_if_match_matches(if_match_header: str | None, expected_version: int) -> None:
        if_match_ver = parse_if_match_version(if_match_header)
        if if_match_ver is not None and if_match_ver != expected_version:
            raise WorkEventError(
                status_code=422,
                code="VERSION_EXPECTATION_MISMATCH",
                message=(
                    f"If-Match version v{if_match_ver} does not match body "
                    f"expected_version {expected_version}."
                ),
            )

    @staticmethod
    def _lock_events_ordered(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_ids: Sequence[str],
    ) -> dict[str, _LockedWorkEvent]:
        ordered_ids = sorted(set(event_ids))
        if not ordered_ids:
            return {}
        cur.execute(
            """
            SELECT
                id::text,
                primary_anchor_source_object_id::text,
                anchor_mode,
                anchor_scope,
                version,
                title,
                problem,
                action,
                outcome,
                summary_metadata,
                status,
                created_by,
                occurred_start_at,
                occurred_end_at
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND id = ANY(%s::uuid[])
            ORDER BY id ASC
            FOR UPDATE
            """,
            (workspace_id, ordered_ids),
        )
        locked: dict[str, _LockedWorkEvent] = {}
        for row in cur.fetchall():
            ev_id = str(row[0])
            locked[ev_id] = _LockedWorkEvent(
                event_id=ev_id,
                primary_anchor_source_object_id=str(row[1]) if row[1] is not None else None,
                anchor_mode=WorkEventAnchorMode(str(row[2])),
                anchor_scope=dict(row[3]) if row[3] is not None else None,
                version=int(row[4]),
                title=str(row[5]),
                problem=str(row[6]) if row[6] is not None else None,
                action=str(row[7]) if row[7] is not None else None,
                outcome=str(row[8]) if row[8] is not None else None,
                summary_metadata=dict(row[9] or {}),
                status=WorkEventStatus(str(row[10])),
                created_by=RecordOrigin(str(row[11])),
                occurred_start_at=row[12],
                occurred_end_at=row[13],
            )
        return locked

    @staticmethod
    def _enforce_live_policy_and_deletion_fence(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        events: Sequence[_LockedWorkEvent],
    ) -> None:
        resource_ids: list[str] = []
        seen: set[str] = set()
        for ev in events:
            linked = PostgresWorkEventsReadService._resolve_linked_source_resource_ids(
                cur,
                workspace_id=workspace_id,
                event_id=ev.event_id,
                primary_anchor_source_object_id=ev.primary_anchor_source_object_id,
            )
            if not linked and isinstance(ev.summary_metadata.get("source_resource_id"), str):
                maybe_rid = _parse_uuid_or_none(str(ev.summary_metadata["source_resource_id"]))
                if maybe_rid is not None:
                    linked = [maybe_rid]
            for rid in linked:
                if rid not in seen:
                    seen.add(rid)
                    resource_ids.append(rid)

        policy_state = PostgresWorkEventsReadService._evaluate_live_policy_state(
            cur,
            workspace_id=workspace_id,
            resource_ids=resource_ids,
        )
        if policy_state is WorkEventPolicyState.DELETION_PENDING:
            raise WorkEventError(
                status_code=409,
                code="DELETION_PENDING",
                message="Mutation blocked because an active deletion fence exists for the source resource.",
            )
        if policy_state is WorkEventPolicyState.POLICY_BLOCKED:
            raise WorkEventError(
                status_code=403,
                code="SOURCE_POLICY_BLOCKED",
                message="Mutation blocked because the source resource is not PUBLIC/ACTIVE.",
            )

    @staticmethod
    def _is_active_top_level(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event: _LockedWorkEvent,
    ) -> bool:
        if event.status not in (
            WorkEventStatus.CANDIDATE,
            WorkEventStatus.CONFIRMED,
            WorkEventStatus.STALE,
        ):
            return False
        cur.execute(
            """
            SELECT 1
            FROM public.event_composite_memberships
            WHERE workspace_id = %s::uuid
              AND member_event_id = %s::uuid
              AND status = 'ACTIVE'
            LIMIT 1
            """,
            (workspace_id, event.event_id),
        )
        return cur.fetchone() is None

    @staticmethod
    def _assert_expected_version(
        event: _LockedWorkEvent,
        *,
        expected_version: int,
        is_active_top_level: bool,
    ) -> None:
        if event.version != expected_version:
            allowed = compute_allowed_actions(
                anchor_mode=event.anchor_mode,
                status=event.status,
                policy_state=WorkEventPolicyState.ALLOWED,
                is_active_top_level=is_active_top_level,
                anchor_scope=event.anchor_scope,
            )
            raise WorkEventError(
                status_code=409,
                code="EVENT_VERSION_CONFLICT",
                message=(
                    f"Version conflict on WorkEvent {event.event_id}: "
                    f"expected v{expected_version}, current is v{event.version}."
                ),
                retryable=False,
                details={
                    "event_id": event.event_id,
                    "expected_version": expected_version,
                    "current_version": event.version,
                    "current_status": event.status.value,
                    "etag": format_work_event_etag(event.version),
                    "allowed_actions": allowed,
                },
            )

    @staticmethod
    def _check_idempotency_replay(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        action: ReviewActionType,
        idempotency_key: str,
        fingerprint: str,
        expected_target_id: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT action, target_id::text, action_metadata
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND target_type = %s
              AND idempotency_key = %s
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (workspace_id, ReviewTargetType.WORK_EVENT.value, idempotency_key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        existing_action = str(row[0])
        existing_target = str(row[1])
        existing_meta = dict(row[2] or {})
        existing_fp = existing_meta.get("request_fingerprint")
        if (
            existing_action == action.value
            and existing_target == expected_target_id
            and existing_fp == fingerprint
        ):
            return existing_meta
        raise WorkEventError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message=(
                "Idempotency-Key was already used with a different action, target, "
                "expected_version, or parameters."
            ),
        )

    @staticmethod
    def _check_idempotency_replay_with_target(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        action: ReviewActionType,
        idempotency_key: str,
        fingerprint: str,
    ) -> tuple[str, dict[str, Any]] | None:
        cur.execute(
            """
            SELECT action, target_id::text, action_metadata
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND target_type = %s
              AND idempotency_key = %s
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (workspace_id, ReviewTargetType.WORK_EVENT.value, idempotency_key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        existing_action = str(row[0])
        existing_target = str(row[1])
        existing_meta = dict(row[2] or {})
        existing_fp = existing_meta.get("request_fingerprint")
        if existing_action == action.value and existing_fp == fingerprint:
            return existing_target, existing_meta
        raise WorkEventError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message=(
                "Idempotency-Key was already used with a different action, target, "
                "expected_version, or parameters."
            ),
        )

    @staticmethod
    def _ensure_version_snapshot_exists(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event: _LockedWorkEvent,
        created_at: datetime,
    ) -> None:
        append_event_version_snapshot(
            cur,
            workspace_id=workspace_id,
            event_id=event.event_id,
            version=event.version,
            created_by_review_action_id=None,
            created_at=created_at,
        )

    @staticmethod
    def _mark_downstream_stale(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_ids: Sequence[str],
        now: datetime,
    ) -> tuple[int, int]:
        ids = sorted(set(event_ids))
        if not ids:
            return 0, 0

        cur.execute(
            """
            SELECT COUNT(*)::integer
            FROM public.facts
            WHERE workspace_id = %s::uuid
              AND event_id = ANY(%s::uuid[])
              AND status <> 'REJECTED'
            """,
            (workspace_id, ids),
        )
        f_row = cur.fetchone()
        fact_count = int(f_row[0]) if f_row is not None else 0

        cur.execute(
            """
            UPDATE public.facts
            SET status = 'STALE',
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND event_id = ANY(%s::uuid[])
              AND status NOT IN ('REJECTED', 'STALE')
            """,
            (now, workspace_id, ids),
        )

        cur.execute(
            """
            SELECT COUNT(*)::integer
            FROM public.claims
            WHERE workspace_id = %s::uuid
              AND event_id = ANY(%s::uuid[])
              AND status NOT IN ('REJECTED', 'SUPERSEDED')
            """,
            (workspace_id, ids),
        )
        c_row = cur.fetchone()
        claim_count = int(c_row[0]) if c_row is not None else 0

        cur.execute(
            """
            UPDATE public.claims
            SET status = 'STALE',
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND event_id = ANY(%s::uuid[])
              AND status NOT IN ('REJECTED', 'SUPERSEDED', 'STALE')
            """,
            (now, workspace_id, ids),
        )

        cur.execute(
            """
            UPDATE public.claim_atoms ca
            SET status = 'STALE'
            FROM public.claims c
            WHERE ca.workspace_id = %s::uuid
              AND c.workspace_id = ca.workspace_id
              AND c.id = ca.claim_id
              AND c.event_id = ANY(%s::uuid[])
              AND c.status = 'STALE'
              AND ca.status <> 'STALE'
            """,
            (workspace_id, ids),
        )

        return fact_count, claim_count

    @staticmethod
    def _record_outbox_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_type: str,
        aggregate_id: str,
        dedupe_key: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(payload, field_name="outbox_events.payload")
        cur.execute(
            """
            INSERT INTO public.outbox_events (
                id,
                workspace_id,
                event_type,
                aggregate_type,
                aggregate_id,
                dedupe_key,
                payload,
                status,
                attempt,
                max_attempts,
                next_attempt_at,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s, 'WORK_EVENT', %s::uuid,
                %s, %s::jsonb, 'PENDING', 0, 5, %s, %s
            )
            ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                event_type,
                aggregate_id,
                dedupe_key,
                json.dumps(payload, sort_keys=True),
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _record_audit_event(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        actor_user_id: str | None,
        action: str,
        target_id: str,
        metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(metadata, field_name="audit_events.redacted_metadata")
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id,
                workspace_id,
                actor_type,
                actor_id,
                action,
                target_type,
                target_id,
                redacted_metadata,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, 'WORK_EVENT', %s::uuid, %s::jsonb, %s
            )
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                AuditActorType.USER.value,
                actor_user_id,
                action,
                target_id,
                json.dumps(metadata, sort_keys=True),
                created_at,
            ),
        )

    @staticmethod
    def _validate_and_normalize_split_partitions(
        partitions: Sequence[Any],
    ) -> list[_NormalizedPartition]:
        if len(partitions) < 2:
            raise WorkEventError(
                status_code=422,
                code="INVALID_SPLIT_SCOPE",
                message="SPLIT requires at least 2 partitions.",
            )

        normalized: list[_NormalizedPartition] = []
        seen_file_paths: set[str] = set()
        seen_diff_hunks: set[str] = set()
        seen_review_threads: set[str] = set()
        seen_content_units: set[str] = set()

        for idx, part in enumerate(partitions):
            clean_title = (part.title or "").strip()
            if not clean_title:
                raise WorkEventError(
                    status_code=422,
                    code="INVALID_SPLIT_SCOPE",
                    message=f"Partition {idx} title must be non-empty.",
                )

            norm_source_ids: list[str] = []
            seen_part_src: set[str] = set()
            for raw_sid in part.source_object_ids:
                sid = _parse_uuid_or_none(raw_sid)
                if sid is None:
                    raise WorkEventError(
                        status_code=422,
                        code="INVALID_SPLIT_SCOPE",
                        message=f"Partition {idx} contains invalid source_object_id {raw_sid!r}.",
                    )
                if sid not in seen_part_src:
                    seen_part_src.add(sid)
                    norm_source_ids.append(sid)

            scope = part.anchor_scope
            file_paths = _dedupe_clean_strings(scope.file_paths) if scope else []
            diff_hunks = (
                _dedupe_clean_strings([*scope.diff_hunks, *scope.hunk_ranges]) if scope else []
            )
            review_thread_ids = _dedupe_clean_strings(scope.review_thread_ids) if scope else []
            raw_cu_ids = _dedupe_clean_strings(scope.content_unit_ids) if scope else []
            content_unit_ids: list[str] = []
            for raw_cu in raw_cu_ids:
                cu_uuid = _parse_uuid_or_none(raw_cu)
                if cu_uuid is None:
                    raise WorkEventError(
                        status_code=422,
                        code="INVALID_SPLIT_SCOPE",
                        message=f"Partition {idx} contains invalid content_unit_id {raw_cu!r}.",
                    )
                if cu_uuid not in content_unit_ids:
                    content_unit_ids.append(cu_uuid)

            if not (file_paths or diff_hunks or review_thread_ids or content_unit_ids):
                raise WorkEventError(
                    status_code=422,
                    code="INVALID_SPLIT_SCOPE",
                    message=(
                        f"Partition {idx} must declare a non-empty anchor_scope "
                        "(file_paths, diff_hunks/hunk_ranges, review_thread_ids, or content_unit_ids)."
                    ),
                )

            # Mutual exclusivity (non-overlap) checks across partitions
            overlap_files = sorted(set(file_paths) & seen_file_paths)
            overlap_hunks = sorted(set(diff_hunks) & seen_diff_hunks)
            overlap_threads = sorted(set(review_thread_ids) & seen_review_threads)
            overlap_cus = sorted(set(content_unit_ids) & seen_content_units)
            if overlap_files or overlap_hunks or overlap_threads or overlap_cus:
                raise WorkEventError(
                    status_code=422,
                    code="SPLIT_SCOPE_OVERLAP",
                    message="Split partitions must have mutually disjoint anchor scopes.",
                    details={
                        "overlapping_file_paths": overlap_files,
                        "overlapping_diff_hunks": overlap_hunks,
                        "overlapping_review_thread_ids": overlap_threads,
                        "overlapping_content_unit_ids": overlap_cus,
                    },
                )

            seen_file_paths.update(file_paths)
            seen_diff_hunks.update(diff_hunks)
            seen_review_threads.update(review_thread_ids)
            seen_content_units.update(content_unit_ids)

            normalized.append(
                _NormalizedPartition(
                    title=clean_title,
                    problem=part.problem,
                    action=part.action,
                    outcome=part.outcome,
                    source_object_ids=norm_source_ids,
                    file_paths=file_paths,
                    diff_hunks=diff_hunks,
                    review_thread_ids=review_thread_ids,
                    content_unit_ids=content_unit_ids,
                )
            )

        return normalized

    @staticmethod
    def _load_parent_active_memberships(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        relation_class: EventMembershipRelationClass,
    ) -> dict[str, tuple[str, EventMembershipOrigin, str | None]]:
        cur.execute(
            """
            SELECT source_object_id::text, relation_type, origin, rationale_code
            FROM public.event_memberships
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND relation_class = %s
              AND status = 'ACTIVE'
            ORDER BY id ASC
            """,
            (workspace_id, event_id, relation_class.value),
        )
        result: dict[str, tuple[str, EventMembershipOrigin, str | None]] = {}
        for sid, rtype, orig, rat in cur.fetchall():
            sid_s = str(sid)
            if sid_s not in result:
                result[sid_s] = (
                    str(rtype),
                    EventMembershipOrigin(str(orig)),
                    str(rat) if rat is not None else None,
                )
        return result

    @staticmethod
    def _evaluate_dissolve_member_restoration_status(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        member: _LockedWorkEvent,
        pre_merge_version: int,
    ) -> WorkEventStatus:
        """Re-evaluate member status on DISSOLVE: STALE if source invalid, else CONFIRMED/CANDIDATE."""
        source_obj_ids: list[str] = []
        if member.primary_anchor_source_object_id is not None:
            source_obj_ids.append(member.primary_anchor_source_object_id)

        cur.execute(
            """
            SELECT DISTINCT source_object_id::text
            FROM public.event_memberships
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND relation_class = 'MUST_LINK'
              AND status = 'ACTIVE'
            ORDER BY source_object_id::text ASC
            """,
            (workspace_id, member.event_id),
        )
        for (sid,) in cur.fetchall():
            sid_s = str(sid)
            if sid_s not in source_obj_ids:
                source_obj_ids.append(sid_s)

        if not source_obj_ids:
            return WorkEventStatus.STALE

        cur.execute(
            """
            SELECT
                so.id::text,
                so.lifecycle_state,
                sr.visibility,
                sr.status AS resource_status,
                sa.status AS account_status,
                COALESCE(latest_rev.is_tombstone, false) AS latest_is_tombstone,
                EXISTS (
                    SELECT 1
                    FROM public.deletion_jobs dj
                    WHERE dj.workspace_id = sr.workspace_id
                      AND dj.source_resource_id = sr.id
                      AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
                ) AS has_deletion_fence,
                EXISTS (
                    SELECT 1
                    FROM public.content_units cu
                    WHERE cu.workspace_id = so.workspace_id
                      AND cu.source_object_revision_id = latest_rev.id
                      AND cu.policy_state IN ('PURGED', 'BLOCKED')
                ) AS has_invalid_unit
            FROM public.source_objects so
            INNER JOIN public.source_resources sr
                ON sr.workspace_id = so.workspace_id
               AND sr.id = so.source_resource_id
            INNER JOIN public.source_accounts sa
                ON sa.workspace_id = sr.workspace_id
               AND sa.id = sr.source_account_id
            LEFT JOIN LATERAL (
                SELECT r.id, r.is_tombstone
                FROM public.source_object_revisions r
                WHERE r.workspace_id = so.workspace_id
                  AND r.source_object_id = so.id
                ORDER BY r.observed_at DESC, r.id DESC
                LIMIT 1
            ) latest_rev ON true
            WHERE so.workspace_id = %s::uuid
              AND so.id = ANY(%s::uuid[])
            """,
            (workspace_id, source_obj_ids),
        )
        rows = cur.fetchall()
        if len(rows) < len(source_obj_ids):
            return WorkEventStatus.STALE

        for (
            _so_id,
            lifecycle,
            visibility,
            res_status,
            acct_status,
            is_tombstone,
            has_fence,
            has_invalid_unit,
        ) in rows:
            if (
                str(lifecycle) != SourceObjectLifecycleState.ACTIVE.value
                or str(visibility) != ResourceVisibility.PUBLIC.value
                or str(res_status) != "ACTIVE"
                or str(acct_status) != "ACTIVE"
                or bool(is_tombstone)
                or bool(has_fence)
                or bool(has_invalid_unit)
            ):
                return WorkEventStatus.STALE

        # Source is valid -> check pre-merge version snapshot in work_event_versions
        cur.execute(
            """
            SELECT status
            FROM public.work_event_versions
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND version = %s
            LIMIT 1
            """,
            (workspace_id, member.event_id, pre_merge_version),
        )
        snap_row = cur.fetchone()
        if snap_row is not None and str(snap_row[0]) == WorkEventStatus.CONFIRMED.value:
            return WorkEventStatus.CONFIRMED
        return WorkEventStatus.CANDIDATE

    @staticmethod
    def _hydrate_split_response_from_metadata(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        parent_event_id: str,
        action_metadata: dict[str, Any],
    ) -> SplitWorkEventResponse:
        segment_ids = [str(s) for s in (action_metadata.get("created_segment_ids") or [])]
        superseded_parent = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
            cur, workspace_id=workspace_id, event_id=parent_event_id
        )
        created_segments = [
            PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                cur, workspace_id=workspace_id, event_id=seg_id
            )
            for seg_id in segment_ids
        ]
        pairwise_items: list[SplitPairwiseConstraintItem] = []
        if len(segment_ids) >= 2:
            cur.execute(
                """
                SELECT
                    id::text,
                    left_event_id::text,
                    right_event_id::text,
                    constraint_type,
                    origin,
                    status,
                    rationale_code,
                    created_by_review_action_id::text,
                    created_at
                FROM public.event_constraints
                WHERE workspace_id = %s::uuid
                  AND left_event_id = ANY(%s::uuid[])
                  AND right_event_id = ANY(%s::uuid[])
                  AND status = 'ACTIVE'
                ORDER BY left_event_id ASC, right_event_id ASC, id ASC
                """,
                (workspace_id, segment_ids, segment_ids),
            )
            for r in cur.fetchall():
                pairwise_items.append(
                    SplitPairwiseConstraintItem(
                        constraint_id=str(r[0]),
                        left_event_id=str(r[1]),
                        right_event_id=str(r[2]),
                        constraint_type=EventConstraintType(str(r[3])),
                        origin=EventConstraintOrigin(str(r[4])),
                        status=EventConstraintStatus(str(r[5])),
                        rationale_code=str(r[6]) if r[6] is not None else None,
                        created_by_review_action_id=str(r[7]) if r[7] is not None else None,
                        created_at=r[8],
                    )
                )
        return SplitWorkEventResponse(
            superseded_parent=superseded_parent,
            created_segments=created_segments,
            pairwise_constraints_created=pairwise_items,
        )

    @staticmethod
    def _hydrate_merge_response_from_metadata(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        composite_event_id: str,
        action_metadata: dict[str, Any],
    ) -> MergeWorkEventsResponse:
        composite_detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
            cur, workspace_id=workspace_id, event_id=composite_event_id
        )
        superseded_member_ids = [
            str(m) for m in (action_metadata.get("superseded_member_ids") or [])
        ]
        raw_impact = dict(action_metadata.get("affected_downstream_impact") or {})
        impact = AffectedDownstreamImpact(
            fact_count=int(raw_impact.get("fact_count", 0)),
            claim_count=int(raw_impact.get("claim_count", 0)),
            export_count=int(raw_impact.get("export_count", 0)),
        )
        invalidated_total = int(
            action_metadata.get(
                "invalidated_downstream_count", impact.fact_count + impact.claim_count
            )
        )
        regen_status = str(action_metadata.get("regeneration_job_status") or "PENDING")
        return MergeWorkEventsResponse(
            composite_event=composite_detail,
            superseded_member_ids=superseded_member_ids,
            invalidated_downstream_count=invalidated_total,
            affected_downstream_impact=impact,
            regeneration_job_status=regen_status,
        )

    @staticmethod
    def _hydrate_dissolve_response_from_metadata(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        composite_event_id: str,
        action_metadata: dict[str, Any],
    ) -> DissolveWorkEventResponse:
        comp_detail = PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
            cur, workspace_id=workspace_id, event_id=composite_event_id
        )
        restored_ids = [str(m) for m in (action_metadata.get("restored_member_ids") or [])]
        restored_details = [
            PostgresWorkEventsReadService.hydrate_work_event_detail_in_tx(
                cur, workspace_id=workspace_id, event_id=m_id
            )
            for m_id in restored_ids
        ]
        raw_impact = dict(action_metadata.get("stale_downstream_impact") or {})
        impact = StaleDownstreamImpact(
            stale_fact_count=int(raw_impact.get("stale_fact_count", 0)),
            stale_claim_count=int(raw_impact.get("stale_claim_count", 0)),
            stale_export_count=int(raw_impact.get("stale_export_count", 0)),
        )
        invalidated_total = int(
            action_metadata.get(
                "invalidated_downstream_count",
                impact.stale_fact_count + impact.stale_claim_count,
            )
        )
        cleanup_status = str(action_metadata.get("cleanup_job_status") or "PENDING")
        return DissolveWorkEventResponse(
            dissolved_composite=comp_detail,
            superseded_composite=comp_detail,
            restored_members=restored_details,
            invalidated_downstream_count=invalidated_total,
            stale_downstream_impact=impact,
            cleanup_job_status=cleanup_status,
        )


__all__ = [
    "BeforeCommitHook",
    "ConnectionFactory",
    "PostgresWorkEventMutationService",
]
