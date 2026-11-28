"""Single Logical Event Collapse & Search Helpers for M3 WorkEvents (EV-009 ~ EV-012).

Ensures search and export queries return at most one logical top-level WorkEvent:
- When a ``SUPERSEDED`` member of an active ``USER_COMPOSITE`` matches by title, summary,
  or source object, it collapses strictly to the active ``USER_COMPOSITE`` event ID with
  zero duplicate returns (``EV-009``, ``EV-010``).
- ``REJECTED`` events and dissolved (``SUPERSEDED``) historical ``USER_COMPOSITE`` events
  are completely excluded; after dissolve, only restored active member events are returned
  (``EV-011``, ``EV-012``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import psycopg

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


def collapse_event_ids_for_search_and_export(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    candidate_event_ids: Sequence[str],
) -> list[str]:
    """Collapse candidate WorkEvent IDs into unique active top-level logical event IDs.

    Rules enforced in order:
    1. ``REJECTED`` events are excluded (``EV-011``).
    2. Any event that is an ``ACTIVE`` member of an active ``USER_COMPOSITE``
       (``status IN ('CANDIDATE', 'CONFIRMED', 'STALE')``) maps to that composite's ID
       (``EV-009``, ``EV-010``).
    3. Any remaining ``SUPERSEDED`` event (e.g., dissolved ``USER_COMPOSITE`` or split parent)
       is excluded (``EV-012``).
    4. Active top-level events (``CANDIDATE``, ``CONFIRMED``, ``STALE``) are preserved.
    5. Ordering from ``candidate_event_ids`` is preserved while guaranteeing zero duplicates.
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    ordered_candidates: list[str] = []
    for raw_id in candidate_event_ids:
        try:
            ordered_candidates.append(_normalize_uuid(raw_id, "candidate_event_id"))
        except ValueError:
            continue

    if not ordered_candidates:
        return []

    unique_candidates = sorted(set(ordered_candidates))
    resolved_map: dict[str, str | None] = {}

    with _cursor_context(conn) as cur:
        cur.execute(
            """
            SELECT
                we.id::text AS event_id,
                we.status AS event_status,
                active_comp.composite_event_id::text AS active_composite_id
            FROM public.work_events we
            LEFT JOIN LATERAL (
                SELECT ecm.composite_event_id
                FROM public.event_composite_memberships ecm
                INNER JOIN public.work_events comp
                    ON comp.workspace_id = ecm.workspace_id
                   AND comp.id = ecm.composite_event_id
                WHERE ecm.workspace_id = we.workspace_id
                  AND ecm.member_event_id = we.id
                  AND ecm.status = 'ACTIVE'
                  AND comp.status IN ('CANDIDATE', 'CONFIRMED', 'STALE')
                ORDER BY ecm.created_at DESC, ecm.id DESC
                LIMIT 1
            ) active_comp ON true
            WHERE we.workspace_id = %s::uuid
              AND we.id = ANY(%s::uuid[])
            """,
            (ws_uuid, unique_candidates),
        )
        for ev_id, ev_status, active_comp_id in cur.fetchall():
            ev_s = str(ev_id)
            st_s = str(ev_status)
            if st_s == "REJECTED":
                resolved_map[ev_s] = None
            elif active_comp_id is not None:
                resolved_map[ev_s] = str(active_comp_id)
            elif st_s in ("CANDIDATE", "CONFIRMED", "STALE"):
                resolved_map[ev_s] = ev_s
            else:
                # SUPERSEDED (e.g., split parent or dissolved USER_COMPOSITE)
                resolved_map[ev_s] = None

    collapsed: list[str] = []
    seen: set[str] = set()
    for candidate_id in ordered_candidates:
        logical_id = resolved_map.get(candidate_id)
        if logical_id is not None and logical_id not in seen:
            seen.add(logical_id)
            collapsed.append(logical_id)
    return collapsed


def _escape_like_token(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_searchable_top_level_work_events(
    conn: ConnOrCursor,
    *,
    workspace_id: str,
    query_text: str,
    source_resource_id: str | None = None,
    limit: int = 50,
) -> list[str]:
    """Search non-raw WorkEvent fields & source identifiers, collapsing to active top-level IDs.

    Splits ``query_text`` into whitespace-delimited search tokens and requires EVERY token
    (case-insensitive AND semantics) to match across title/problem/action/outcome or linked
    ``source_objects.provider_object_id`` on either active top-level events or ``SUPERSEDED``
    composite members, then collapses every hit through ``collapse_event_ids_for_search_and_export``
    so active ``USER_COMPOSITE`` events appear at most once and ``REJECTED`` / dissolved
    composites never leak.
    """
    ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
    res_uuid = (
        _normalize_uuid(source_resource_id, "source_resource_id")
        if source_resource_id is not None
        else None
    )
    tokens = list(dict.fromkeys(tok for tok in query_text.strip().split() if tok))
    if not tokens or limit < 1:
        return []

    where_clauses: list[str] = [
        "we.workspace_id = %s::uuid",
        "we.status <> 'REJECTED'",
    ]
    params: list[object] = [ws_uuid]

    for tok in tokens:
        like_pattern = f"%{_escape_like_token(tok)}%"
        where_clauses.append(
            """
            (
                we.title ILIKE %s ESCAPE '\\'
                OR COALESCE(we.problem, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(we.action, '') ILIKE %s ESCAPE '\\'
                OR COALESCE(we.outcome, '') ILIKE %s ESCAPE '\\'
                OR EXISTS (
                    SELECT 1
                    FROM public.source_objects so_pa
                    WHERE so_pa.workspace_id = we.workspace_id
                      AND so_pa.id = we.primary_anchor_source_object_id
                      AND so_pa.provider_object_id ILIKE %s ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1
                    FROM public.event_memberships em
                    INNER JOIN public.source_objects so
                        ON so.workspace_id = em.workspace_id
                       AND so.id = em.source_object_id
                    WHERE em.workspace_id = we.workspace_id
                      AND em.event_id = we.id
                      AND em.status = 'ACTIVE'
                      AND em.relation_class = 'MUST_LINK'
                      AND so.provider_object_id ILIKE %s ESCAPE '\\'
                )
            )
            """
        )
        params.extend(
            [
                like_pattern,
                like_pattern,
                like_pattern,
                like_pattern,
                like_pattern,
                like_pattern,
            ]
        )

    if res_uuid is not None:
        where_clauses.append(
            """
            (
                EXISTS (
                    SELECT 1
                    FROM public.source_objects so_pa_r
                    WHERE so_pa_r.workspace_id = we.workspace_id
                      AND so_pa_r.id = we.primary_anchor_source_object_id
                      AND so_pa_r.source_resource_id = %s::uuid
                )
                OR EXISTS (
                    SELECT 1
                    FROM public.event_memberships em_r
                    INNER JOIN public.source_objects so_r
                        ON so_r.workspace_id = em_r.workspace_id
                       AND so_r.id = em_r.source_object_id
                    WHERE em_r.workspace_id = we.workspace_id
                      AND em_r.event_id = we.id
                      AND em_r.status = 'ACTIVE'
                      AND em_r.relation_class = 'MUST_LINK'
                      AND so_r.source_resource_id = %s::uuid
                )
                OR EXISTS (
                    SELECT 1
                    FROM public.event_composite_memberships ecm_r
                    INNER JOIN public.work_events m_we_r
                        ON m_we_r.workspace_id = ecm_r.workspace_id
                       AND m_we_r.id = ecm_r.member_event_id
                    INNER JOIN public.source_objects so_mr
                        ON so_mr.workspace_id = m_we_r.workspace_id
                       AND so_mr.id = m_we_r.primary_anchor_source_object_id
                    WHERE ecm_r.workspace_id = we.workspace_id
                      AND ecm_r.composite_event_id = we.id
                      AND ecm_r.status = 'ACTIVE'
                      AND so_mr.source_resource_id = %s::uuid
                )
            )
            """
        )
        params.extend([res_uuid, res_uuid, res_uuid])

    with _cursor_context(conn) as cur:
        cur.execute(
            f"""
            SELECT we.id::text
            FROM public.work_events we
            WHERE {' AND '.join(where_clauses)}
            ORDER BY COALESCE(we.occurred_end_at, we.occurred_start_at, we.created_at) DESC, we.id DESC
            """,
            tuple(params),
        )
        candidate_ids = [str(row[0]) for row in cur.fetchall()]
        collapsed = collapse_event_ids_for_search_and_export(
            cur,
            workspace_id=ws_uuid,
            candidate_event_ids=candidate_ids,
        )

    return collapsed[:limit]


__all__ = [
    "collapse_event_ids_for_search_and_export",
    "query_searchable_top_level_work_events",
]
