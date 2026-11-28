"""Transactional OD-003 Source Deletion Cleanup Cascade for M4 Tables (Facts, Claims, Exports).

Enforces OD-003 / SCN-13 invariants inside a single ``workspace_scoped_transaction``:
- Requires an active ``public.deletion_jobs`` fence for the target ``source_resource_id``.
- Immediately invalidates and purges all dependent ``exports``, ``claims`` (including ALL
  versions in a supersede chain and mixed-source claims — ``PARTIALLY_GROUNDED`` is forbidden,
  ``retained_versions = 0``), ``claim_atoms``, ``claim_atom_support``, ``contributions`` backed
  exclusively by affected facts, ``facts`` & ``fact_support`` backed by ``source_resource_id``,
  and ``content_units`` / ``source_object_revisions`` / ``source_objects`` for that resource.
- Transitions ``source_resources.status = 'DELETED'`` and ``deletion_jobs.status = 'SUCCEEDED'``.
- Preserves independent ``source_resources`` and their independent ``facts`` untouched.
- Records non-raw redacted ``audit_events`` and ``outbox_events`` (Plan C compliant).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.claims.models import ClaimError
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.llm_gate import assert_no_raw_text_or_secret_keys

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


@dataclass(frozen=True, slots=True)
class SourceDeletionCascadeCounts:
    """Deterministic counts of M4 entities purged during OD-003 cleanup."""

    source_resource_id: str
    deletion_job_id: str
    purged_exports: int
    purged_claims: int
    purged_claim_atoms: int
    purged_facts: int
    purged_content_units: int
    purged_source_revisions: int
    purged_source_objects: int


def execute_source_deletion_m4_cascade(
    api_connection_factory: ConnectionFactory,
    access: WorkspaceAccessContext,
    *,
    source_resource_id: str,
    deletion_job_id: str | None = None,
    now: datetime | None = None,
) -> SourceDeletionCascadeCounts:
    """Execute transactional OD-003 M4 cleanup for a deletion-fenced source_resource_id."""
    ts = now or datetime.now(UTC)
    ws_id = access.workspace_id

    with api_connection_factory() as conn:
        with workspace_scoped_transaction(conn, access) as cur:
            # 1. Verify an active deletion_jobs fence exists for this source_resource_id
            if deletion_job_id is not None:
                cur.execute(
                    """
                    SELECT id::text, status
                    FROM public.deletion_jobs
                    WHERE workspace_id = %s::uuid
                      AND source_resource_id = %s::uuid
                      AND id = %s::uuid
                    FOR UPDATE
                    """,
                    (ws_id, source_resource_id, deletion_job_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id::text, status
                    FROM public.deletion_jobs
                    WHERE workspace_id = %s::uuid
                      AND source_resource_id = %s::uuid
                      AND status IN ('PENDING', 'RUNNING', 'PARTIAL')
                    ORDER BY requested_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (ws_id, source_resource_id),
                )
            dj_row = cur.fetchone()
            if dj_row is None:
                raise ClaimError(
                    status_code=409,
                    code="DELETION_FENCE_NOT_ACTIVE",
                    message=(
                        "OD-003 cleanup requires a confirmed active deletion_jobs fence "
                        "for the target source_resource_id."
                    ),
                )
            resolved_job_id = str(dj_row[0])

            # 2. Identify all source_objects, revisions, and content_units under source_resource_id
            cur.execute(
                """
                SELECT id::text
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                  AND source_resource_id = %s::uuid
                """,
                (ws_id, source_resource_id),
            )
            obj_ids = [str(r[0]) for r in cur.fetchall()]

            rev_ids: list[str] = []
            if obj_ids:
                cur.execute(
                    """
                    SELECT id::text
                    FROM public.source_object_revisions
                    WHERE workspace_id = %s::uuid
                      AND source_object_id = ANY(%s::uuid[])
                    """,
                    (ws_id, obj_ids),
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
                    """,
                    (ws_id, rev_ids),
                )
                cu_ids = [str(r[0]) for r in cur.fetchall()]

            # 3. Identify ALL facts referencing any content_unit of the deleted resource
            affected_fact_ids: list[str] = []
            if cu_ids:
                cur.execute(
                    """
                    SELECT DISTINCT fact_id::text
                    FROM public.fact_support
                    WHERE workspace_id = %s::uuid
                      AND content_unit_id = ANY(%s::uuid[])
                    """,
                    (ws_id, cu_ids),
                )
                affected_fact_ids = [str(r[0]) for r in cur.fetchall()]

            # Expand to any supersedes chain of affected facts (both directions)
            if affected_fact_ids:
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
                    SELECT DISTINCT id::text FROM fchain
                    """,
                    (ws_id, affected_fact_ids, ws_id),
                )
                affected_fact_ids = [str(r[0]) for r in cur.fetchall()]

            # 4. Identify ALL claims depending on any affected fact (including mixed-source claims
            #    and every version in their supersedes_claim_id chain — 0 retained versions!)
            affected_claim_ids: list[str] = []
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
                    (ws_id, affected_fact_ids),
                )
                seed_claim_ids = [str(r[0]) for r in cur.fetchall()]
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
                        SELECT DISTINCT id::text FROM cchain
                        """,
                        (ws_id, seed_claim_ids, ws_id),
                    )
                    affected_claim_ids = [str(r[0]) for r in cur.fetchall()]

            # 5. Purge dependent exports (by source_resource_id OR referencing any affected claim)
            cur.execute(
                """
                SELECT DISTINCT e.id::text
                FROM public.exports e
                LEFT JOIN public.export_claims ec
                    ON ec.workspace_id = e.workspace_id
                   AND ec.export_id = e.id
                WHERE e.workspace_id = %s::uuid
                  AND (
                      e.source_resource_id = %s::uuid
                      OR (cardinality(%s::uuid[]) > 0 AND ec.claim_id = ANY(%s::uuid[]))
                  )
                """,
                (ws_id, source_resource_id, affected_claim_ids, affected_claim_ids),
            )
            affected_export_ids = [str(r[0]) for r in cur.fetchall()]
            purged_exports = 0
            if affected_export_ids:
                cur.execute(
                    """
                    DELETE FROM public.exports
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, affected_export_ids),
                )
                purged_exports = int(cur.rowcount or 0)

            # 6. Purge dependent claims (cascades claim_atoms & claim_atom_support)
            purged_atoms = 0
            purged_claims = 0
            if affected_claim_ids:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.claim_atoms
                    WHERE workspace_id = %s::uuid
                      AND claim_id = ANY(%s::uuid[])
                    """,
                    (ws_id, affected_claim_ids),
                )
                purged_atoms = int(cur.fetchone()[0])  # type: ignore[index]
                cur.execute(
                    """
                    DELETE FROM public.claims
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, affected_claim_ids),
                )
                purged_claims = int(cur.rowcount or 0)

            # 7. Purge dependent facts (cascades fact_support & contribution_support)
            purged_facts = 0
            if affected_fact_ids:
                cur.execute(
                    """
                    DELETE FROM public.facts
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, affected_fact_ids),
                )
                purged_facts = int(cur.rowcount or 0)

            # 8. Purge deleted resource's content_units, revisions, and non-primary-anchor objects
            purged_cus = 0
            if cu_ids:
                cur.execute(
                    """
                    DELETE FROM public.content_units
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, cu_ids),
                )
                purged_cus = int(cur.rowcount or 0)

            purged_revs = 0
            if rev_ids:
                cur.execute("SELECT set_config('app.allow_revision_purge', 'on', true)")
                cur.execute(
                    """
                    DELETE FROM public.source_object_revisions
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, rev_ids),
                )
                purged_revs = int(cur.rowcount or 0)

            purged_objs = 0
            if obj_ids:
                # Remove event_memberships pointing to deleted source_objects
                cur.execute(
                    """
                    DELETE FROM public.event_memberships
                    WHERE workspace_id = %s::uuid
                      AND source_object_id = ANY(%s::uuid[])
                    """,
                    (ws_id, obj_ids),
                )
                # Re-anchor work_events whose primary_anchor_source_object_id is being purged:
                # prefer a surviving event_memberships source_object, otherwise convert anchor_mode to USER_COMPOSITE.
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
                    (ts, ws_id, obj_ids),
                )
                cur.execute(
                    """
                    DELETE FROM public.source_objects
                    WHERE workspace_id = %s::uuid
                      AND id = ANY(%s::uuid[])
                    """,
                    (ws_id, obj_ids),
                )
                purged_objs = int(cur.rowcount or 0)

            # 9. Purge search_entries if 0080 table exists, mark source_resources DELETED & deletion_jobs SUCCEEDED
            cur.execute("SELECT to_regclass('public.search_entries') IS NOT NULL")
            if bool((cur.fetchone() or (False,))[0]):
                cur.execute(
                    """
                    DELETE FROM public.search_entries
                    WHERE workspace_id = %s::uuid
                      AND (
                          source_resource_id = %s::uuid
                          OR (
                              projection_metadata ? 'linked_resource_ids'
                              AND projection_metadata -> 'linked_resource_ids' ? %s
                          )
                      )
                    """,
                    (ws_id, source_resource_id, source_resource_id),
                )
            cur.execute(
                """
                UPDATE public.source_resources
                SET status = 'DELETED'
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (ws_id, source_resource_id),
            )
            checkpoint_meta = {
                "stage": "M4_CASCADE_CLEANUP_COMPLETED",
                "purged_exports": purged_exports,
                "purged_claims": purged_claims,
                "purged_claim_atoms": purged_atoms,
                "purged_facts": purged_facts,
                "purged_content_units": purged_cus,
                "purged_source_revisions": purged_revs,
                "purged_source_objects": purged_objs,
            }
            assert_no_raw_text_or_secret_keys(checkpoint_meta, context="deletion_jobs.checkpoint")
            cur.execute(
                """
                UPDATE public.deletion_jobs
                SET status = 'SUCCEEDED',
                    checkpoint = %s,
                    completed_at = %s
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (Jsonb(checkpoint_meta), ts, ws_id, resolved_job_id),
            )

            # 10. Non-raw audit & outbox record
            cur.execute(
                """
                INSERT INTO public.audit_events (
                    id, workspace_id, actor_type, actor_id, action,
                    target_type, target_id, redacted_metadata, created_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s, 'SOURCE_RESOURCE_DELETION_CASCADE_COMPLETED',
                    'SOURCE_RESOURCE', %s::uuid, %s, %s
                )
                """,
                (
                    str(uuid.uuid4()),
                    ws_id,
                    AuditActorType.USER.value,
                    access.user_id,
                    source_resource_id,
                    Jsonb(checkpoint_meta),
                    ts,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.outbox_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    dedupe_key, payload, status, created_at
                ) VALUES (
                    %s::uuid, %s::uuid, 'SOURCE_RESOURCE_PURGED', 'SOURCE_RESOURCE', %s::uuid,
                    %s, %s, 'PENDING', %s
                )
                ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    ws_id,
                    source_resource_id,
                    f"source_resource_purged:{ws_id}:{source_resource_id}:{resolved_job_id}",
                    Jsonb(checkpoint_meta),
                    ts,
                ),
            )

    return SourceDeletionCascadeCounts(
        source_resource_id=source_resource_id,
        deletion_job_id=resolved_job_id,
        purged_exports=purged_exports,
        purged_claims=purged_claims,
        purged_claim_atoms=purged_atoms,
        purged_facts=purged_facts,
        purged_content_units=purged_cus,
        purged_source_revisions=purged_revs,
        purged_source_objects=purged_objs,
    )


__all__ = [
    "SourceDeletionCascadeCounts",
    "execute_source_deletion_m4_cascade",
]
