"""T-302 Relation Semantics & OD-007 Pairwise Event Constraints for WorkEvents.

Enforces the owner-approved OD-007 Option B split and ER-002 / EVENT-04 / EVENT-05 invariants:
- ``list_active_evidence_memberships`` returns strictly ``relation_class = 'MUST_LINK' AND status = 'ACTIVE'``.
  ``CONTEXT_ONLY`` and ``CANNOT_LINK`` rows are NEVER surfaced as evidence.
- ``list_active_context_relations`` returns strictly ``relation_class = 'CONTEXT_ONLY' AND status = 'ACTIVE'``.
- ``list_cannot_link_summary`` surfaces source-level ``CANNOT_LINK`` memberships and pairwise
  ``public.event_constraints`` separately from evidence.
- ``USER_COMPOSITE`` events NEVER store ``MUST_LINK`` source relations in ``event_memberships``;
  composite member relationships live exclusively in ``event_composite_memberships``.
- Issue/ticket/file/branch/time/similarity clue relation types are strictly rejected as ``MUST_LINK`` (EVENT-04).
- Active ``CANNOT_LINK`` rows block automatic (``SOURCE`` / ``RULE``) ``MUST_LINK`` attachment (EVENT-05).
- ``check_merge_cannot_link_conflicts`` detects both pairwise ``event_constraints`` conflicts
  (e.g., ``CAND-EV-021``, ``SPLIT`` siblings) and cross-member ``MUST_LINK`` vs ``CANNOT_LINK``
  source-object conflicts for ``T-304B`` merge validation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.schema_contracts.enums import (
    EventConstraintOrigin,
    EventConstraintStatus,
    EventConstraintType,
    EventMembershipOrigin,
    EventMembershipRelationClass,
    SourceObjectType,
    WorkEventAnchorMode,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.work_events.models import (
    CannotLinkConflictDetail,
    CannotLinkSummary,
    EventSourceRelationItem,
    PairwiseEventConstraintItem,
    PairwiseEventConstraintRecord,
    RelationSemanticsError,
)

ConnOrCursor = psycopg.Connection | psycopg.Cursor

FORBIDDEN_MUST_LINK_RELATION_TYPES: frozenset[str] = frozenset(
    {
        "ISSUE_REFERENCE",
        "ISSUE_LINK",
        "TICKET_REFERENCE",
        "TICKET_LINK",
        "SHARED_TICKET",
        "SHARED_LABEL",
        "FILE_OVERLAP",
        "SHARED_FILE",
        "BRANCH_HEURISTIC",
        "SAME_BRANCH",
        "TIME_PROXIMITY",
        "TEXT_SIMILARITY",
        "SEMANTIC_SIMILARITY",
        "SIMILARITY_CLUE",
    }
)

_FORBIDDEN_MUST_LINK_KEYWORDS: tuple[str, ...] = (
    "ISSUE",
    "TICKET",
    "FILE_OVERLAP",
    "SHARED_FILE",
    "BRANCH",
    "TIME_PROX",
    "SIMILAR",
    "HEURISTIC",
)


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


def canonicalize_event_pair(event_a: str, event_b: str) -> tuple[str, str]:
    """Return canonical ``(left_event_id, right_event_id)`` where ``left < right``.

    Raises ``ValueError`` if either value is not a valid UUID or if both refer to
    the same event (preventing self-referential constraints).
    """
    norm_a = _normalize_uuid(event_a, "event_a")
    norm_b = _normalize_uuid(event_b, "event_b")
    if norm_a == norm_b:
        raise ValueError("Pairwise event constraint requires two distinct event IDs.")
    return (norm_a, norm_b) if norm_a < norm_b else (norm_b, norm_a)


def is_forbidden_must_link_relation_type(relation_type: str) -> bool:
    """Return True if ``relation_type`` is a context/heuristic clue forbidden as MUST_LINK (EVENT-04)."""
    normalized = relation_type.strip().upper()
    if not normalized:
        return True
    if normalized in FORBIDDEN_MUST_LINK_RELATION_TYPES:
        return True
    return any(keyword in normalized for keyword in _FORBIDDEN_MUST_LINK_KEYWORDS)


def list_active_evidence_memberships(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
) -> list[EventSourceRelationItem]:
    """Return strictly ``relation_class = 'MUST_LINK' AND status = 'ACTIVE'`` memberships (ER-002)."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                em.id::text,
                em.source_object_id::text,
                so.object_type,
                so.provider_object_id,
                em.relation_type,
                em.origin,
                em.rationale_code,
                em.created_at
            FROM public.event_memberships em
            INNER JOIN public.source_objects so
                ON so.workspace_id = em.workspace_id
               AND so.id = em.source_object_id
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.relation_class = 'MUST_LINK'
              AND em.status = 'ACTIVE'
            ORDER BY
                CASE WHEN em.relation_type = 'PRIMARY_ANCHOR' THEN 0 ELSE 1 END ASC,
                em.relation_type ASC,
                so.provider_object_id ASC,
                em.id ASC
            """,
            (ws_uuid, ev_uuid),
        )
        rows = cur.fetchall()

    return [
        EventSourceRelationItem(
            relation_id=str(row[0]),
            source_object_id=str(row[1]),
            object_type=SourceObjectType(str(row[2])),
            provider_object_id=str(row[3]),
            relation_type=str(row[4]),
            origin=EventMembershipOrigin(str(row[5])),
            rationale_code=str(row[6]) if row[6] is not None else None,
            created_at=row[7],
        )
        for row in rows
    ]


def list_active_context_relations(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
) -> list[EventSourceRelationItem]:
    """Return strictly ``relation_class = 'CONTEXT_ONLY' AND status = 'ACTIVE'`` relations."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                em.id::text,
                em.source_object_id::text,
                so.object_type,
                so.provider_object_id,
                em.relation_type,
                em.origin,
                em.rationale_code,
                em.created_at
            FROM public.event_memberships em
            INNER JOIN public.source_objects so
                ON so.workspace_id = em.workspace_id
               AND so.id = em.source_object_id
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.relation_class = 'CONTEXT_ONLY'
              AND em.status = 'ACTIVE'
            ORDER BY em.relation_type ASC, so.provider_object_id ASC, em.id ASC
            """,
            (ws_uuid, ev_uuid),
        )
        rows = cur.fetchall()

    return [
        EventSourceRelationItem(
            relation_id=str(row[0]),
            source_object_id=str(row[1]),
            object_type=SourceObjectType(str(row[2])),
            provider_object_id=str(row[3]),
            relation_type=str(row[4]),
            origin=EventMembershipOrigin(str(row[5])),
            rationale_code=str(row[6]) if row[6] is not None else None,
            created_at=row[7],
        )
        for row in rows
    ]


def list_cannot_link_summary(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
) -> CannotLinkSummary:
    """Return active source-level and pairwise event-level ``CANNOT_LINK`` constraints."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT DISTINCT em.source_object_id::text
            FROM public.event_memberships em
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.relation_class = 'CANNOT_LINK'
              AND em.status = 'ACTIVE'
            ORDER BY em.source_object_id::text ASC
            """,
            (ws_uuid, ev_uuid),
        )
        source_object_ids = [str(row[0]) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT
                ec.id::text,
                CASE
                    WHEN ec.left_event_id = %s::uuid THEN ec.right_event_id::text
                    ELSE ec.left_event_id::text
                END AS peer_event_id,
                ec.constraint_type,
                ec.origin,
                ec.status,
                ec.rationale_code,
                ec.created_by_review_action_id::text,
                ec.created_at
            FROM public.event_constraints ec
            WHERE ec.workspace_id = %s::uuid
              AND ec.status = 'ACTIVE'
              AND ec.constraint_type = 'CANNOT_LINK'
              AND (ec.left_event_id = %s::uuid OR ec.right_event_id = %s::uuid)
            ORDER BY peer_event_id ASC, ec.created_at ASC, ec.id ASC
            """,
            (ev_uuid, ws_uuid, ev_uuid, ev_uuid),
        )
        constraint_rows = cur.fetchall()

    constraints = [
        PairwiseEventConstraintItem(
            constraint_id=str(row[0]),
            peer_event_id=str(row[1]),
            constraint_type=EventConstraintType(str(row[2])),
            origin=EventConstraintOrigin(str(row[3])),
            status=EventConstraintStatus(str(row[4])),
            rationale_code=str(row[5]) if row[5] is not None else None,
            created_by_review_action_id=str(row[6]) if row[6] is not None else None,
            created_at=row[7],
        )
        for row in constraint_rows
    ]
    peer_event_ids = sorted({item.peer_event_id for item in constraints})
    return CannotLinkSummary(
        source_object_ids=source_object_ids,
        peer_event_ids=peer_event_ids,
        constraints=constraints,
    )


def record_source_relation(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_id: str,
    source_object_id: str,
    relation_class: EventMembershipRelationClass | str,
    relation_type: str,
    origin: EventMembershipOrigin | str,
    rationale_code: str | None = None,
    invalidate_conflicting_links: bool = True,
    created_at: datetime | None = None,
) -> EventSourceRelationItem:
    """Persist a WorkEvent <-> SourceObject relation enforcing T-302 / EVENT-04 / EVENT-05.

    Rules enforced:
    1. ``USER_COMPOSITE`` events CANNOT store ``MUST_LINK`` source relations in
       ``event_memberships`` (composite members live exclusively in ``event_composite_memberships``).
    2. Issue/ticket/file/branch/time/similarity clue relation types AND ``ISSUE`` source objects
       CANNOT be recorded as ``MUST_LINK`` (EVENT-04).
    3. Active ``CANNOT_LINK`` on ``(event_id, source_object_id)`` blocks automatic
       (``SOURCE`` / ``RULE``) ``MUST_LINK`` attachment (EVENT-05).
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ev_uuid = _normalize_uuid(event_id, "event_id")
    obj_uuid = _normalize_uuid(source_object_id, "source_object_id")
    rel_class = EventMembershipRelationClass(relation_class)
    rel_origin = EventMembershipOrigin(origin)
    clean_rel_type = relation_type.strip()
    if not clean_rel_type:
        raise RelationSemanticsError(
            "relation_type must be a non-empty string.",
            code="INVALID_RELATION_TYPE",
        )
    ts = created_at or datetime.now(UTC)

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT anchor_mode
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (ws_uuid, ev_uuid),
        )
        ev_row = cur.fetchone()
        if ev_row is None:
            raise RelationSemanticsError(
                f"WorkEvent {ev_uuid} not found in workspace {ws_uuid}.",
                code="WORK_EVENT_NOT_FOUND",
            )
        anchor_mode = WorkEventAnchorMode(str(ev_row[0]))

        if (
            anchor_mode is WorkEventAnchorMode.USER_COMPOSITE
            and rel_class is EventMembershipRelationClass.MUST_LINK
        ):
            raise RelationSemanticsError(
                "USER_COMPOSITE events must never store MUST_LINK source relations in "
                "event_memberships; use event_composite_memberships instead.",
                code="USER_COMPOSITE_MUST_LINK_FORBIDDEN",
            )

        cur.execute(
            """
            SELECT object_type, provider_object_id
            FROM public.source_objects
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (ws_uuid, obj_uuid),
        )
        obj_row = cur.fetchone()
        if obj_row is None:
            raise RelationSemanticsError(
                f"SourceObject {obj_uuid} not found in workspace {ws_uuid}.",
                code="SOURCE_OBJECT_NOT_FOUND",
            )
        object_type = SourceObjectType(str(obj_row[0]))
        provider_object_id = str(obj_row[1])

        if rel_class is EventMembershipRelationClass.MUST_LINK:
            if object_type is SourceObjectType.ISSUE or is_forbidden_must_link_relation_type(
                clean_rel_type
            ):
                raise RelationSemanticsError(
                    f"Relation type {clean_rel_type!r} (object_type={object_type.value}) is a "
                    "context/heuristic clue and cannot be stored as MUST_LINK (EVENT-04).",
                    code="CONTEXT_ONLY_RELATION_FORBIDDEN_AS_MUST_LINK",
                )

            if rel_origin in (EventMembershipOrigin.SOURCE, EventMembershipOrigin.RULE):
                cur.execute(
                    """
                    SELECT 1
                    FROM public.event_memberships
                    WHERE workspace_id = %s::uuid
                      AND event_id = %s::uuid
                      AND source_object_id = %s::uuid
                      AND relation_class = 'CANNOT_LINK'
                      AND status = 'ACTIVE'
                    LIMIT 1
                    """,
                    (ws_uuid, ev_uuid, obj_uuid),
                )
                if cur.fetchone() is not None:
                    raise RelationSemanticsError(
                        f"Active CANNOT_LINK blocks automatic {rel_origin.value} MUST_LINK "
                        f"attachment for source_object {obj_uuid} on event {ev_uuid} (EVENT-05).",
                        code="CANNOT_LINK_BLOCKS_AUTO_ATTACHMENT",
                    )

        if (
            rel_class is EventMembershipRelationClass.CANNOT_LINK
            and invalidate_conflicting_links
        ):
            cur.execute(
                """
                UPDATE public.event_memberships
                SET status = 'INVALIDATED'
                WHERE workspace_id = %s::uuid
                  AND event_id = %s::uuid
                  AND source_object_id = %s::uuid
                  AND relation_class IN ('MUST_LINK', 'CONTEXT_ONLY')
                  AND status = 'ACTIVE'
                """,
                (ws_uuid, ev_uuid, obj_uuid),
            )

        cur.execute(
            """
            INSERT INTO public.event_memberships (
                id,
                workspace_id,
                event_id,
                source_object_id,
                relation_class,
                relation_type,
                origin,
                rationale_code,
                status,
                created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s, 'ACTIVE', %s
            )
            ON CONFLICT (event_id, source_object_id, relation_type)
            DO UPDATE SET
                relation_class = EXCLUDED.relation_class,
                origin = EXCLUDED.origin,
                rationale_code = COALESCE(EXCLUDED.rationale_code, public.event_memberships.rationale_code),
                status = 'ACTIVE'
            RETURNING id::text, created_at, rationale_code
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                ev_uuid,
                obj_uuid,
                rel_class.value,
                clean_rel_type,
                rel_origin.value,
                rationale_code,
                ts,
            ),
        )
        saved = cur.fetchone()
        assert saved is not None

    return EventSourceRelationItem(
        relation_id=str(saved[0]),
        source_object_id=obj_uuid,
        object_type=object_type,
        provider_object_id=provider_object_id,
        relation_type=clean_rel_type,
        origin=rel_origin,
        rationale_code=str(saved[2]) if saved[2] is not None else None,
        created_at=saved[1],
    )


def record_pairwise_cannot_link(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    event_a_id: str,
    event_b_id: str,
    origin: EventConstraintOrigin | str,
    rationale_code: str | None = None,
    review_action_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> PairwiseEventConstraintRecord:
    """Idempotently record a canonical pairwise ``CANNOT_LINK`` constraint in ``public.event_constraints`` (OD-007)."""
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    left_id, right_id = canonicalize_event_pair(event_a_id, event_b_id)
    constraint_origin = EventConstraintOrigin(origin)
    ra_uuid = (
        _normalize_uuid(review_action_id, "review_action_id")
        if review_action_id is not None
        else None
    )
    meta_dict: dict[str, Any] = dict(metadata or {})
    assert_no_forbidden_raw_keys(meta_dict, field_name="event_constraints.constraint_metadata")
    ts = created_at or datetime.now(UTC)

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            INSERT INTO public.event_constraints (
                id,
                workspace_id,
                left_event_id,
                right_event_id,
                constraint_type,
                origin,
                status,
                rationale_code,
                constraint_metadata,
                created_by_review_action_id,
                created_at,
                revoked_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                'CANNOT_LINK', %s, 'ACTIVE', %s, %s::jsonb, %s::uuid, %s, NULL
            )
            ON CONFLICT (workspace_id, left_event_id, right_event_id, constraint_type)
            WHERE status = 'ACTIVE'
            DO NOTHING
            RETURNING
                id::text,
                workspace_id::text,
                left_event_id::text,
                right_event_id::text,
                constraint_type,
                origin,
                status,
                rationale_code,
                created_by_review_action_id::text,
                constraint_metadata,
                created_at,
                revoked_at
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                left_id,
                right_id,
                constraint_origin.value,
                rationale_code,
                json.dumps(meta_dict, sort_keys=True),
                ra_uuid,
                ts,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT
                    id::text,
                    workspace_id::text,
                    left_event_id::text,
                    right_event_id::text,
                    constraint_type,
                    origin,
                    status,
                    rationale_code,
                    created_by_review_action_id::text,
                    constraint_metadata,
                    created_at,
                    revoked_at
                FROM public.event_constraints
                WHERE workspace_id = %s::uuid
                  AND left_event_id = %s::uuid
                  AND right_event_id = %s::uuid
                  AND constraint_type = 'CANNOT_LINK'
                  AND status = 'ACTIVE'
                """,
                (ws_uuid, left_id, right_id),
            )
            row = cur.fetchone()
            assert row is not None

    return PairwiseEventConstraintRecord(
        constraint_id=str(row[0]),
        workspace_id=str(row[1]),
        left_event_id=str(row[2]),
        right_event_id=str(row[3]),
        constraint_type=EventConstraintType(str(row[4])),
        origin=EventConstraintOrigin(str(row[5])),
        status=EventConstraintStatus(str(row[6])),
        rationale_code=str(row[7]) if row[7] is not None else None,
        created_by_review_action_id=str(row[8]) if row[8] is not None else None,
        constraint_metadata=dict(row[9] or {}),
        created_at=row[10],
        revoked_at=row[11],
    )


def check_merge_cannot_link_conflicts(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    member_event_ids: Sequence[str],
) -> list[CannotLinkConflictDetail]:
    """Detect all active pairwise and source-level ``CANNOT_LINK`` merge conflicts (T-302 / T-304B).

    Checks:
    1. Any pair ``(A, B)`` in ``member_event_ids`` with an ``ACTIVE`` ``CANNOT_LINK`` row
       in ``public.event_constraints`` (e.g. ``SPLIT`` siblings, ``CAND-EV-021``).
    2. Any ``source_object_id`` that is an active ``MUST_LINK`` evidence item of one member
       ``A`` while being an active ``CANNOT_LINK`` exclusion of another member ``B``.
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    normalized_ids = sorted({_normalize_uuid(eid, "member_event_id") for eid in member_event_ids})
    if len(normalized_ids) < 2:
        return []

    conflicts: list[CannotLinkConflictDetail] = []

    with _cursor_context(conn) as cur:
        # 1. Pairwise WorkEvent <-> WorkEvent CANNOT_LINK constraints (OD-007)
        cur.execute(
            """
            SELECT
                id::text,
                left_event_id::text,
                right_event_id::text,
                origin,
                rationale_code
            FROM public.event_constraints
            WHERE workspace_id = %s::uuid
              AND status = 'ACTIVE'
              AND constraint_type = 'CANNOT_LINK'
              AND left_event_id = ANY(%s::uuid[])
              AND right_event_id = ANY(%s::uuid[])
            ORDER BY left_event_id ASC, right_event_id ASC, id ASC
            """,
            (ws_uuid, normalized_ids, normalized_ids),
        )
        for row in cur.fetchall():
            conflicts.append(
                CannotLinkConflictDetail(
                    conflict_kind="PAIRWISE_EVENT_CANNOT_LINK",
                    left_event_id=str(row[1]),
                    right_event_id=str(row[2]),
                    source_object_id=None,
                    constraint_id=str(row[0]),
                    origin=str(row[3]),
                    rationale_code=str(row[4]) if row[4] is not None else None,
                )
            )

        # 2. Cross-member MUST_LINK vs CANNOT_LINK on the same SourceObject
        cur.execute(
            """
            SELECT
                em.event_id::text,
                em.source_object_id::text,
                em.relation_class,
                em.origin,
                em.rationale_code
            FROM public.event_memberships em
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = ANY(%s::uuid[])
              AND em.status = 'ACTIVE'
              AND em.relation_class IN ('MUST_LINK', 'CANNOT_LINK')
            ORDER BY em.source_object_id ASC, em.event_id ASC
            """,
            (ws_uuid, normalized_ids),
        )
        must_by_obj: dict[str, list[str]] = {}
        cannot_by_obj: dict[str, list[tuple[str, str, str | None]]] = {}
        for ev_id, obj_id, rel_class, rel_origin, rationale in cur.fetchall():
            ev_s = str(ev_id)
            obj_s = str(obj_id)
            if str(rel_class) == EventMembershipRelationClass.MUST_LINK.value:
                must_by_obj.setdefault(obj_s, []).append(ev_s)
            else:
                cannot_by_obj.setdefault(obj_s, []).append(
                    (ev_s, str(rel_origin), str(rationale) if rationale is not None else None)
                )

    seen_source_conflicts: set[tuple[str, str, str]] = set()
    for obj_id in sorted(set(must_by_obj.keys()) & set(cannot_by_obj.keys())):
        for must_ev in sorted(set(must_by_obj[obj_id])):
            for cannot_ev, cannot_origin, cannot_rationale in cannot_by_obj[obj_id]:
                if must_ev == cannot_ev:
                    continue
                left_id, right_id = canonicalize_event_pair(must_ev, cannot_ev)
                dedupe_key = (left_id, right_id, obj_id)
                if dedupe_key in seen_source_conflicts:
                    continue
                seen_source_conflicts.add(dedupe_key)
                conflicts.append(
                    CannotLinkConflictDetail(
                        conflict_kind="SOURCE_OBJECT_CANNOT_LINK",
                        left_event_id=left_id,
                        right_event_id=right_id,
                        source_object_id=obj_id,
                        constraint_id=None,
                        origin=cannot_origin,
                        rationale_code=cannot_rationale,
                    )
                )

    conflicts.sort(
        key=lambda c: (
            c.conflict_kind,
            c.left_event_id,
            c.right_event_id,
            c.source_object_id or "",
            c.constraint_id or "",
        )
    )
    return conflicts


__all__ = [
    "FORBIDDEN_MUST_LINK_RELATION_TYPES",
    "canonicalize_event_pair",
    "check_merge_cannot_link_conflicts",
    "is_forbidden_must_link_relation_type",
    "list_active_context_relations",
    "list_active_evidence_memberships",
    "list_cannot_link_summary",
    "record_pairwise_cannot_link",
    "record_source_relation",
]
