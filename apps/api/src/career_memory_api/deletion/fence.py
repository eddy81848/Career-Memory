"""Universal Pre-Commit Deletion Fence Recheck Helpers (DELETE-07 / EXPORT-02 / T-502A).

Every mutation, sync/import, event candidate builder, fact extractor, claim generator/approver,
export publisher, and search indexer MUST call these helpers inside its commit transaction right
before committing so in-flight workers can NEVER resurrect rows after a confirmed deletion fence.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from career_memory_api.jobs.models import DeletionFenceActiveError
from career_memory_api.jobs.postgres_runtime import is_deletion_fence_active


def assert_resources_not_deletion_fenced(
    cur: psycopg.Cursor,
    workspace_id: str,
    source_resource_ids: Sequence[str],
    *,
    context: str = "",
) -> None:
    """Raise ``DeletionFenceActiveError`` if ANY given ``source_resource_id`` is fenced or DELETED."""
    checked: set[str] = set()
    for raw_rid in source_resource_ids:
        rid = str(raw_rid).strip()
        if not rid or rid in checked:
            continue
        checked.add(rid)
        if is_deletion_fence_active(cur, workspace_id, rid):
            suffix = f" during {context}" if context else ""
            raise DeletionFenceActiveError(
                f"Deletion fence is active for source_resource {rid}{suffix}; commit aborted."
            )


def find_event_linked_resource_ids(
    cur: psycopg.Cursor,
    workspace_id: str,
    event_id: str,
) -> list[str]:
    """Return all distinct ``source_resource_id`` UUIDs linked to a WorkEvent (or its composite members)."""
    cur.execute(
        """
        WITH objs AS (
            SELECT we.primary_anchor_source_object_id AS obj_id
            FROM public.work_events we
            WHERE we.workspace_id = %s::uuid
              AND we.id = %s::uuid
              AND we.primary_anchor_source_object_id IS NOT NULL
            UNION
            SELECT em.source_object_id AS obj_id
            FROM public.event_memberships em
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.status = 'ACTIVE'
            UNION
            SELECT m_we.primary_anchor_source_object_id AS obj_id
            FROM public.event_composite_memberships ecm
            INNER JOIN public.work_events m_we
                ON m_we.workspace_id = ecm.workspace_id
               AND m_we.id = ecm.member_event_id
            WHERE ecm.workspace_id = %s::uuid
              AND ecm.composite_event_id = %s::uuid
              AND ecm.status = 'ACTIVE'
              AND m_we.primary_anchor_source_object_id IS NOT NULL
        )
        SELECT DISTINCT so.source_resource_id::text
        FROM objs o
        INNER JOIN public.source_objects so
            ON so.workspace_id = %s::uuid
           AND so.id = o.obj_id
        ORDER BY 1 ASC
        """,
        (
            workspace_id,
            event_id,
            workspace_id,
            event_id,
            workspace_id,
            event_id,
            workspace_id,
        ),
    )
    return [str(r[0]) for r in cur.fetchall()]


def assert_event_not_deletion_fenced(
    cur: psycopg.Cursor,
    workspace_id: str,
    event_id: str,
    *,
    context: str = "",
) -> None:
    """Raise ``DeletionFenceActiveError`` if ANY source resource backing ``event_id`` is fenced."""
    resource_ids = find_event_linked_resource_ids(cur, workspace_id, event_id)
    assert_resources_not_deletion_fenced(
        cur,
        workspace_id,
        resource_ids,
        context=context or f"event:{event_id}",
    )


__all__ = [
    "assert_event_not_deletion_fenced",
    "assert_resources_not_deletion_fenced",
    "find_event_linked_resource_ids",
]
