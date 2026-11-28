"""T-305 Immutable EventVersion Snapshots & ReviewAction Audit History Layer.

Provides append-only snapshot persistence into ``public.work_event_versions`` and
review action auditing into ``public.review_actions``, while strictly separating
user ``REJECT`` (Exclude) actions from system/source invalidation ``STALE``
transitions (recorded via ``work_events`` + ``work_event_versions`` + ``audit_events``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.jobs.models import AuditActorType
from career_memory_api.schema_contracts.enums import (
    EventMembershipRelationClass,
    RecordOrigin,
    ReviewActionType,
    ReviewTargetType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.work_events.models import (
    EventHistoryError,
    EventVersionSnapshotItem,
    ReviewActionHistoryItem,
)

ConnOrCursor = psycopg.Connection | psycopg.Cursor


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


@contextmanager
def _cursor_context(conn_or_cur: ConnOrCursor) -> Iterator[psycopg.Cursor]:
    if isinstance(conn_or_cur, psycopg.Cursor):
        yield conn_or_cur
    else:
        with conn_or_cur.cursor() as cur:
            yield cur


def _row_to_version_snapshot(row: tuple[Any, ...]) -> EventVersionSnapshotItem:
    return EventVersionSnapshotItem(
        version_id=str(row[0]),
        event_id=str(row[1]),
        version=int(row[2]),
        anchor_mode=WorkEventAnchorMode(str(row[3])),
        primary_anchor_source_object_id=str(row[4]) if row[4] is not None else None,
        anchor_scope=dict(row[5]) if row[5] is not None else None,
        title=str(row[6]),
        problem=str(row[7]) if row[7] is not None else None,
        action=str(row[8]) if row[8] is not None else None,
        outcome=str(row[9]) if row[9] is not None else None,
        summary_metadata=dict(row[10] or {}),
        status=WorkEventStatus(str(row[11])),
        created_by=RecordOrigin(str(row[12])),
        created_by_review_action_id=str(row[13]) if row[13] is not None else None,
        membership_snapshot=dict(row[14] or {}),
        occurred_start_at=row[15],
        occurred_end_at=row[16],
        created_at=row[17],
    )


def _row_to_review_action(row: tuple[Any, ...]) -> ReviewActionHistoryItem:
    action_meta = dict(row[7] or {})
    origin_val = str(action_meta.get("origin") or "USER").strip() or "USER"
    return ReviewActionHistoryItem(
        review_action_id=str(row[0]),
        action=ReviewActionType(str(row[1])),
        actor_user_id=str(row[2]) if row[2] is not None else None,
        origin=origin_val,
        from_version=int(row[3]) if row[3] is not None else None,
        to_version=int(row[4]) if row[4] is not None else None,
        reason=str(row[5]) if row[5] is not None else None,
        idempotency_key=str(row[6]),
        action_metadata=action_meta,
        created_at=row[8],
    )


def build_membership_snapshot(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
    composite_version: int | None = None,
) -> dict[str, Any]:
    """Build a Plan C compliant non-raw ID/version membership snapshot for an event."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    must_link_ids: list[str] = []
    context_only_ids: list[str] = []
    cannot_link_ids: list[str] = []
    composite_members: list[dict[str, Any]] = []

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT DISTINCT relation_class, source_object_id::text
            FROM public.event_memberships
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND status = 'ACTIVE'
            ORDER BY relation_class ASC, source_object_id::text ASC
            """,
            (ws_uuid, ev_uuid),
        )
        for rel_class, source_object_id in cur.fetchall():
            cls_str = str(rel_class)
            obj_str = str(source_object_id)
            if cls_str == EventMembershipRelationClass.MUST_LINK.value:
                must_link_ids.append(obj_str)
            elif cls_str == EventMembershipRelationClass.CONTEXT_ONLY.value:
                context_only_ids.append(obj_str)
            elif cls_str == EventMembershipRelationClass.CANNOT_LINK.value:
                cannot_link_ids.append(obj_str)

        if composite_version is not None:
            cur.execute(
                """
                SELECT
                    member_event_id::text,
                    member_event_version,
                    ordinal,
                    status
                FROM public.event_composite_memberships
                WHERE workspace_id = %s::uuid
                  AND composite_event_id = %s::uuid
                  AND composite_event_version = %s
                ORDER BY ordinal ASC, member_event_id ASC
                """,
                (ws_uuid, ev_uuid, composite_version),
            )
        else:
            cur.execute(
                """
                SELECT
                    member_event_id::text,
                    member_event_version,
                    ordinal,
                    status
                FROM public.event_composite_memberships
                WHERE workspace_id = %s::uuid
                  AND composite_event_id = %s::uuid
                  AND status = 'ACTIVE'
                ORDER BY ordinal ASC, member_event_id ASC
                """,
                (ws_uuid, ev_uuid),
            )
        for member_id, member_ver, ordinal, status in cur.fetchall():
            composite_members.append(
                {
                    "member_event_id": str(member_id),
                    "member_event_version": int(member_ver),
                    "ordinal": int(ordinal),
                    "status": str(status),
                }
            )

    snapshot: dict[str, Any] = {
        "must_link_source_object_ids": must_link_ids,
        "context_only_source_object_ids": context_only_ids,
        "cannot_link_source_object_ids": cannot_link_ids,
        "composite_members": composite_members,
    }
    assert_no_forbidden_raw_keys(snapshot, field_name="work_event_versions.membership_snapshot")
    return snapshot


def append_event_version_snapshot(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
    version: int | None = None,
    created_by_review_action_id: str | None = None,
    membership_snapshot: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> EventVersionSnapshotItem:
    """Append an immutable version snapshot to ``public.work_event_versions`` (idempotent).

    Reads the authoritative current projection from ``public.work_events`` and records it
    into ``public.work_event_versions`` with ``ON CONFLICT (workspace_id, event_id, version) DO NOTHING``.
    Existing version rows are NEVER updated.
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")
    ra_uuid = (
        _normalize_uuid(created_by_review_action_id, "created_by_review_action_id")
        if created_by_review_action_id is not None
        else None
    )
    ts = created_at or datetime.now(UTC)

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                anchor_mode,
                primary_anchor_source_object_id::text,
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
              AND id = %s::uuid
            """,
            (ws_uuid, ev_uuid),
        )
        ev_row = cur.fetchone()
        if ev_row is None:
            raise EventHistoryError(
                f"WorkEvent {ev_uuid} not found in workspace {ws_uuid}.",
                code="WORK_EVENT_NOT_FOUND",
            )

        anchor_mode = str(ev_row[0])
        primary_anchor_id = str(ev_row[1]) if ev_row[1] is not None else None
        anchor_scope = dict(ev_row[2]) if ev_row[2] is not None else None
        effective_version = int(version) if version is not None else int(ev_row[3])
        if effective_version < 1:
            raise EventHistoryError(
                f"Event version must be >= 1, got {effective_version}.",
                code="INVALID_EVENT_VERSION",
            )
        title = str(ev_row[4])
        problem = str(ev_row[5]) if ev_row[5] is not None else None
        action_text = str(ev_row[6]) if ev_row[6] is not None else None
        outcome = str(ev_row[7]) if ev_row[7] is not None else None
        summary_metadata = dict(ev_row[8] or {})
        status = str(ev_row[9])
        created_by = str(ev_row[10])
        occurred_start_at = ev_row[11]
        occurred_end_at = ev_row[12]

        if anchor_scope is not None:
            assert_no_forbidden_raw_keys(anchor_scope, field_name="work_event_versions.anchor_scope")
        assert_no_forbidden_raw_keys(
            summary_metadata, field_name="work_event_versions.summary_metadata"
        )

        resolved_membership_snapshot = (
            dict(membership_snapshot)
            if membership_snapshot is not None
            else build_membership_snapshot(
                cur,
                workspace_id=ws_uuid,
                event_id=ev_uuid,
                composite_version=(
                    effective_version
                    if anchor_mode == WorkEventAnchorMode.USER_COMPOSITE.value
                    else None
                ),
            )
        )
        assert_no_forbidden_raw_keys(
            resolved_membership_snapshot,
            field_name="work_event_versions.membership_snapshot",
        )

        cur.execute(
            """
            INSERT INTO public.work_event_versions (
                id,
                workspace_id,
                event_id,
                version,
                anchor_mode,
                primary_anchor_source_object_id,
                anchor_scope,
                title,
                problem,
                action,
                outcome,
                summary_metadata,
                status,
                created_by,
                created_by_review_action_id,
                membership_snapshot,
                occurred_start_at,
                occurred_end_at,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s,
                %s, %s::uuid, %s::jsonb,
                %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s::uuid, %s::jsonb,
                %s, %s, %s
            )
            ON CONFLICT (workspace_id, event_id, version) DO NOTHING
            RETURNING
                id::text,
                event_id::text,
                version,
                anchor_mode,
                primary_anchor_source_object_id::text,
                anchor_scope,
                title,
                problem,
                action,
                outcome,
                summary_metadata,
                status,
                created_by,
                created_by_review_action_id::text,
                membership_snapshot,
                occurred_start_at,
                occurred_end_at,
                created_at
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                ev_uuid,
                effective_version,
                anchor_mode,
                primary_anchor_id,
                json.dumps(anchor_scope, sort_keys=True) if anchor_scope is not None else None,
                title,
                problem,
                action_text,
                outcome,
                json.dumps(summary_metadata, sort_keys=True),
                status,
                created_by,
                ra_uuid,
                json.dumps(resolved_membership_snapshot, sort_keys=True),
                occurred_start_at,
                occurred_end_at,
                ts,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT
                    id::text,
                    event_id::text,
                    version,
                    anchor_mode,
                    primary_anchor_source_object_id::text,
                    anchor_scope,
                    title,
                    problem,
                    action,
                    outcome,
                    summary_metadata,
                    status,
                    created_by,
                    created_by_review_action_id::text,
                    membership_snapshot,
                    occurred_start_at,
                    occurred_end_at,
                    created_at
                FROM public.work_event_versions
                WHERE workspace_id = %s::uuid
                  AND event_id = %s::uuid
                  AND version = %s
                """,
                (ws_uuid, ev_uuid, effective_version),
            )
            row = cur.fetchone()
            assert row is not None

    return _row_to_version_snapshot(row)


def record_work_event_review_action(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    target_event_id: str,
    action: ReviewActionType | str,
    actor_user_id: str | None,
    from_version: int | None,
    to_version: int | None,
    idempotency_key: str,
    reason: str | None,
    origin: str = "USER",
    action_metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> ReviewActionHistoryItem:
    """Append an idempotent ``WORK_EVENT`` review action to ``public.review_actions`` (T-305)."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(target_event_id, "target_event_id")
    actor_uuid = (
        _normalize_uuid(actor_user_id, "actor_user_id") if actor_user_id is not None else None
    )
    action_enum = ReviewActionType(action)
    clean_idem = idempotency_key.strip()
    if not clean_idem:
        raise EventHistoryError(
            "idempotency_key must be non-empty.",
            code="INVALID_IDEMPOTENCY_KEY",
        )
    clean_origin = origin.strip().upper()
    if not clean_origin:
        raise EventHistoryError("origin must be non-empty.", code="INVALID_REVIEW_ORIGIN")

    if from_version is not None and from_version < 1:
        raise EventHistoryError("from_version must be >= 1.", code="INVALID_EVENT_VERSION")
    if to_version is not None and to_version < 1:
        raise EventHistoryError("to_version must be >= 1.", code="INVALID_EVENT_VERSION")

    meta_payload: dict[str, Any] = dict(action_metadata or {})
    meta_payload["origin"] = clean_origin
    assert_no_forbidden_raw_keys(meta_payload, field_name="review_actions.action_metadata")
    ts = created_at or datetime.now(UTC)

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            INSERT INTO public.review_actions (
                id,
                workspace_id,
                target_type,
                target_id,
                action,
                actor_user_id,
                from_version,
                to_version,
                idempotency_key,
                reason,
                action_metadata,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s::uuid, %s, %s,
                %s, %s, %s::jsonb, %s
            )
            ON CONFLICT (workspace_id, action, target_type, target_id, idempotency_key)
            DO NOTHING
            RETURNING
                id::text,
                action,
                actor_user_id::text,
                from_version,
                to_version,
                reason,
                idempotency_key,
                action_metadata,
                created_at
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                ReviewTargetType.WORK_EVENT.value,
                ev_uuid,
                action_enum.value,
                actor_uuid,
                from_version,
                to_version,
                clean_idem,
                reason,
                json.dumps(meta_payload, sort_keys=True),
                ts,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT
                    id::text,
                    action,
                    actor_user_id::text,
                    from_version,
                    to_version,
                    reason,
                    idempotency_key,
                    action_metadata,
                    created_at
                FROM public.review_actions
                WHERE workspace_id = %s::uuid
                  AND action = %s
                  AND target_type = %s
                  AND target_id = %s::uuid
                  AND idempotency_key = %s
                """,
                (
                    ws_uuid,
                    action_enum.value,
                    ReviewTargetType.WORK_EVENT.value,
                    ev_uuid,
                    clean_idem,
                ),
            )
            row = cur.fetchone()
            assert row is not None

    return _row_to_review_action(row)


def mark_work_event_stale_from_source(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
    reason_code: str,
    source_object_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.SYSTEM,
    created_at: datetime | None = None,
) -> EventVersionSnapshotItem:
    """Transition a WorkEvent to ``STALE`` due to source invalidation without creating a user ``REJECT`` review action.

    Records:
    1. Incremented ``work_events.version`` + ``status = 'STALE'`` (preserving user decisions
       if already ``REJECTED`` or ``SUPERSEDED`` by raising ``EventHistoryError``).
    2. New immutable ``work_event_versions`` snapshot (`created_by_review_action_id = NULL`).
    3. Append-only ``audit_events`` entry (`action = 'WORK_EVENT_MARKED_STALE'`).
    Never inserts a ``REJECT`` row into ``public.review_actions`` (T-305 separation invariant).
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")
    src_obj_uuid = (
        _normalize_uuid(source_object_id, "source_object_id")
        if source_object_id is not None
        else None
    )
    clean_reason = reason_code.strip()
    if not clean_reason:
        raise EventHistoryError("reason_code must be non-empty.", code="INVALID_STALE_REASON")
    ts = created_at or datetime.now(UTC)

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT version, status, summary_metadata
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            FOR UPDATE
            """,
            (ws_uuid, ev_uuid),
        )
        row = cur.fetchone()
        if row is None:
            raise EventHistoryError(
                f"WorkEvent {ev_uuid} not found in workspace {ws_uuid}.",
                code="WORK_EVENT_NOT_FOUND",
            )
        current_version = int(row[0])
        current_status = WorkEventStatus(str(row[1]))
        if current_status in (WorkEventStatus.REJECTED, WorkEventStatus.SUPERSEDED):
            raise EventHistoryError(
                f"Cannot mark {current_status.value} WorkEvent {ev_uuid} as STALE.",
                code="INVALID_STALE_TRANSITION",
            )

        next_version = current_version + 1
        updated_meta = dict(row[2] or {})
        updated_meta["stale_reason_code"] = clean_reason
        if src_obj_uuid is not None:
            updated_meta["stale_source_object_id"] = src_obj_uuid
        assert_no_forbidden_raw_keys(updated_meta, field_name="work_events.summary_metadata")

        cur.execute(
            """
            UPDATE public.work_events
            SET status = 'STALE',
                version = %s,
                summary_metadata = %s::jsonb,
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (next_version, json.dumps(updated_meta, sort_keys=True), ts, ws_uuid, ev_uuid),
        )

        snapshot = append_event_version_snapshot(
            cur,
            workspace_id=ws_uuid,
            event_id=ev_uuid,
            version=next_version,
            created_by_review_action_id=None,
            created_at=ts,
        )

        audit_meta: dict[str, Any] = {
            "from_version": current_version,
            "to_version": next_version,
            "from_status": current_status.value,
            "to_status": WorkEventStatus.STALE.value,
            "reason_code": clean_reason,
        }
        if src_obj_uuid is not None:
            audit_meta["source_object_id"] = src_obj_uuid
        assert_no_forbidden_raw_keys(audit_meta, field_name="audit_events.redacted_metadata")

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
                %s::uuid, %s::uuid, %s, NULL,
                'WORK_EVENT_MARKED_STALE', 'WORK_EVENT', %s::uuid, %s::jsonb, %s
            )
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                actor_type.value,
                ev_uuid,
                json.dumps(audit_meta, sort_keys=True),
                ts,
            ),
        )

    return snapshot


def list_event_version_history(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
) -> list[EventVersionSnapshotItem]:
    """Return immutable version snapshots ``v1..vN`` in ascending version order."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                id::text,
                event_id::text,
                version,
                anchor_mode,
                primary_anchor_source_object_id::text,
                anchor_scope,
                title,
                problem,
                action,
                outcome,
                summary_metadata,
                status,
                created_by,
                created_by_review_action_id::text,
                membership_snapshot,
                occurred_start_at,
                occurred_end_at,
                created_at
            FROM public.work_event_versions
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
            ORDER BY version ASC, created_at ASC, id ASC
            """,
            (ws_uuid, ev_uuid),
        )
        rows = cur.fetchall()

    return [_row_to_version_snapshot(row) for row in rows]


def list_work_event_review_actions(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
) -> list[ReviewActionHistoryItem]:
    """Return chronological append-only ``review_actions`` for a WorkEvent."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                id::text,
                action,
                actor_user_id::text,
                from_version,
                to_version,
                reason,
                idempotency_key,
                action_metadata,
                created_at
            FROM public.review_actions
            WHERE workspace_id = %s::uuid
              AND target_type = %s
              AND target_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (ws_uuid, ReviewTargetType.WORK_EVENT.value, ev_uuid),
        )
        rows = cur.fetchall()

    return [_row_to_review_action(row) for row in rows]


__all__ = [
    "append_event_version_snapshot",
    "build_membership_snapshot",
    "list_event_version_history",
    "list_work_event_review_actions",
    "mark_work_event_stale_from_source",
    "record_work_event_review_action",
]
