"""Non-raw PostgreSQL FTS search projection indexer (`public.search_entries`, T-501 / T-501A).

Guarantees:
- Indexes strictly non-raw domain projections (`WorkEvent`, `Fact`, `Claim`). Never reads or
  persists raw source bodies, diffs, quotes, or credentials (Plan C).
- Checks `is_deletion_fence_active` and resource/account policy status BEFORE upserting AND
  again right before commit (`before_commit_hook` supported for `DELETE-07` concurrency barriers).
- Exposes `skip_index_update=True` toggle so tests can deterministically inject index lag
  (`SEARCH-05`) and verify that authoritative pre-response rechecks never leak stale/deleted rows.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.contracts_api.models import (
    DEFAULT_SEARCH_INDEX_VERSION,
    SearchEntityType,
)
from career_memory_api.jobs.models import DeletionFenceActiveError
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)
from career_memory_api.search.models import (
    MAX_SEARCH_TOKENS_LEN,
    MAX_TITLE_OR_SUMMARY_LEN,
    SearchIndexerStats,
)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[..., None]


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


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


def _truncate_clean(text: str, max_len: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len].rstrip()


def _search_entries_table_exists(cur: psycopg.Cursor) -> bool:
    cur.execute("SELECT to_regclass('public.search_entries') IS NOT NULL")
    row = cur.fetchone()
    return bool(row and row[0])


def _get_work_event_linked_resources_and_tokens(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    event_id: str,
) -> tuple[list[str], list[str]]:
    """Return sorted linked source_resource_ids and non-raw identifier/member tokens for an event."""
    cur.execute(
        """
        WITH direct_objs AS (
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
              AND em.relation_class = 'MUST_LINK'
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
            UNION
            SELECT m_em.source_object_id AS obj_id
            FROM public.event_composite_memberships ecm
            INNER JOIN public.event_memberships m_em
                ON m_em.workspace_id = ecm.workspace_id
               AND m_em.event_id = ecm.member_event_id
            WHERE ecm.workspace_id = %s::uuid
              AND ecm.composite_event_id = %s::uuid
              AND ecm.status = 'ACTIVE'
              AND m_em.status = 'ACTIVE'
              AND m_em.relation_class = 'MUST_LINK'
        )
        SELECT DISTINCT
            so.source_resource_id::text,
            so.provider_object_id
        FROM direct_objs d
        INNER JOIN public.source_objects so
            ON so.workspace_id = %s::uuid
           AND so.id = d.obj_id
        ORDER BY 1 ASC, 2 ASC
        """,
        (
            workspace_id,
            event_id,
            workspace_id,
            event_id,
            workspace_id,
            event_id,
            workspace_id,
            event_id,
            workspace_id,
        ),
    )
    res_ids: set[str] = set()
    extra_tokens: list[str] = []
    for res_id, prov_obj_id in cur.fetchall():
        res_ids.add(str(res_id))
        if prov_obj_id:
            extra_tokens.append(str(prov_obj_id))

    # Include member WorkEvent non-raw summaries for active USER_COMPOSITE events
    cur.execute(
        """
        SELECT m_we.title, m_we.problem, m_we.action, m_we.outcome
        FROM public.event_composite_memberships ecm
        INNER JOIN public.work_events m_we
            ON m_we.workspace_id = ecm.workspace_id
           AND m_we.id = ecm.member_event_id
        WHERE ecm.workspace_id = %s::uuid
          AND ecm.composite_event_id = %s::uuid
          AND ecm.status = 'ACTIVE'
        ORDER BY m_we.id ASC
        """,
        (workspace_id, event_id),
    )
    for row in cur.fetchall():
        for part in row:
            if part:
                extra_tokens.append(str(part))

    return sorted(res_ids), extra_tokens


def _get_fact_linked_resources(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    fact_id: str,
    event_id: str,
) -> list[str]:
    """Resolve source_resource_ids backing a Fact via fact_support (with event fallback)."""
    cur.execute(
        """
        SELECT DISTINCT so.source_resource_id::text
        FROM public.fact_support fs
        INNER JOIN public.content_units cu
            ON cu.workspace_id = fs.workspace_id
           AND cu.id = fs.content_unit_id
        INNER JOIN public.source_object_revisions rev
            ON rev.workspace_id = cu.workspace_id
           AND rev.id = cu.source_object_revision_id
        INNER JOIN public.source_objects so
            ON so.workspace_id = rev.workspace_id
           AND so.id = rev.source_object_id
        WHERE fs.workspace_id = %s::uuid
          AND fs.fact_id = %s::uuid
          AND fs.status = 'ACTIVE'
        ORDER BY 1 ASC
        """,
        (workspace_id, fact_id),
    )
    res_ids = [str(r[0]) for r in cur.fetchall()]
    if res_ids:
        return res_ids
    ev_res, _ = _get_work_event_linked_resources_and_tokens(
        cur, workspace_id=workspace_id, event_id=event_id
    )
    return ev_res


def _get_claim_linked_resources(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    claim_id: str,
    event_id: str,
) -> list[str]:
    """Resolve all source_resource_ids backing a Claim via atoms->facts->resources and event_id."""
    cur.execute(
        """
        SELECT DISTINCT so.source_resource_id::text
        FROM public.claim_atoms ca
        INNER JOIN public.claim_atom_support cas
            ON cas.workspace_id = ca.workspace_id
           AND cas.claim_atom_id = ca.id
        INNER JOIN public.fact_support fs
            ON fs.workspace_id = cas.workspace_id
           AND fs.fact_id = cas.fact_id
        INNER JOIN public.content_units cu
            ON cu.workspace_id = fs.workspace_id
           AND cu.id = fs.content_unit_id
        INNER JOIN public.source_object_revisions rev
            ON rev.workspace_id = cu.workspace_id
           AND rev.id = cu.source_object_revision_id
        INNER JOIN public.source_objects so
            ON so.workspace_id = rev.workspace_id
           AND so.id = rev.source_object_id
        WHERE ca.workspace_id = %s::uuid
          AND ca.claim_id = %s::uuid
          AND cas.status = 'ACTIVE'
          AND fs.status = 'ACTIVE'
        ORDER BY 1 ASC
        """,
        (workspace_id, claim_id),
    )
    res_ids = {str(r[0]) for r in cur.fetchall()}
    ev_res, _ = _get_work_event_linked_resources_and_tokens(
        cur, workspace_id=workspace_id, event_id=event_id
    )
    res_ids.update(ev_res)
    return sorted(res_ids)


def _resources_all_indexable(
    cur: psycopg.Cursor,
    *,
    workspace_id: str,
    resource_ids: list[str],
) -> tuple[bool, bool]:
    """Return ``(all_active_and_public, any_deletion_fenced)`` for the given resources."""
    if not resource_ids:
        return False, False
    any_fenced = False
    for rid in resource_ids:
        if is_deletion_fence_active(cur, workspace_id, rid):
            any_fenced = True
    if any_fenced:
        return False, True

    cur.execute(
        """
        SELECT COUNT(*)
        FROM public.source_resources sr
        INNER JOIN public.source_accounts sa
            ON sa.workspace_id = sr.workspace_id
           AND sa.id = sr.source_account_id
        WHERE sr.workspace_id = %s::uuid
          AND sr.id = ANY(%s::uuid[])
          AND sr.status = 'ACTIVE'
          AND sr.visibility = 'PUBLIC'
          AND sa.status = 'ACTIVE'
        """,
        (workspace_id, resource_ids),
    )
    row = cur.fetchone()
    ok_count = int(row[0]) if row is not None else 0
    return ok_count == len(resource_ids), False


class PostgresSearchIndexer:
    """Maintains non-raw ``public.search_entries`` projections with deletion fence safety."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        index_version: str = DEFAULT_SEARCH_INDEX_VERSION,
        before_commit_hook: BeforeCommitHook | None = None,
        skip_index_update: bool = False,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self.index_version = index_version
        self._before_commit_hook = before_commit_hook
        self.skip_index_update = skip_index_update
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    # =========================================================================
    # In-Transaction Helpers (Safe to call inside domain mutation transactions)
    # =========================================================================
    def delete_entity_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        entity_type: SearchEntityType,
        entity_id: str,
    ) -> int:
        if self.skip_index_update or not _search_entries_table_exists(cur):
            return 0
        cur.execute(
            """
            DELETE FROM public.search_entries
            WHERE workspace_id = %s::uuid
              AND entity_type = %s
              AND entity_id = %s::uuid
            """,
            (workspace_id, entity_type.value, entity_id),
        )
        return int(cur.rowcount or 0)

    def purge_resource_entries_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str,
        affected_entity_ids: dict[SearchEntityType, list[str]] | None = None,
    ) -> int:
        """Delete all ``public.search_entries`` belonging to ``source_resource_id`` or affected entities."""
        if not _search_entries_table_exists(cur):
            return 0
        total = 0
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
            (workspace_id, source_resource_id, source_resource_id),
        )
        total += int(cur.rowcount or 0)
        if affected_entity_ids:
            for etype, eids in affected_entity_ids.items():
                if not eids:
                    continue
                cur.execute(
                    """
                    DELETE FROM public.search_entries
                    WHERE workspace_id = %s::uuid
                      AND entity_type = %s
                      AND entity_id = ANY(%s::uuid[])
                    """,
                    (workspace_id, etype.value, eids),
                )
                total += int(cur.rowcount or 0)
        return total

    def sync_work_event_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        raise_on_fence: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Upsert or remove a single WorkEvent projection in ``public.search_entries``."""
        if self.skip_index_update or not _search_entries_table_exists(cur):
            return False
        ts = now or self._now_fn()

        cur.execute(
            """
            SELECT
                we.id::text,
                we.version,
                we.status,
                we.anchor_mode,
                we.title,
                we.problem,
                we.action,
                we.outcome,
                EXISTS (
                    SELECT 1
                    FROM public.event_composite_memberships ecm
                    INNER JOIN public.work_events comp
                        ON comp.workspace_id = ecm.workspace_id
                       AND comp.id = ecm.composite_event_id
                    WHERE ecm.workspace_id = we.workspace_id
                      AND ecm.member_event_id = we.id
                      AND ecm.status = 'ACTIVE'
                      AND comp.status IN ('CANDIDATE', 'CONFIRMED', 'STALE')
                ) AS is_active_composite_member
            FROM public.work_events we
            WHERE we.workspace_id = %s::uuid
              AND we.id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        row = cur.fetchone()
        if row is None:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.WORK_EVENT,
                entity_id=event_id,
            )
            return False

        ev_version = int(row[1])
        ev_status = str(row[2])
        anchor_mode = str(row[3])
        title = str(row[4])
        problem = str(row[5]) if row[5] is not None else ""
        action = str(row[6]) if row[6] is not None else ""
        outcome = str(row[7]) if row[7] is not None else ""
        is_member = bool(row[8])

        linked_resources, extra_tokens = _get_work_event_linked_resources_and_tokens(
            cur, workspace_id=workspace_id, event_id=event_id
        )
        indexable, fenced = _resources_all_indexable(
            cur, workspace_id=workspace_id, resource_ids=linked_resources
        )
        if fenced:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.WORK_EVENT,
                entity_id=event_id,
            )
            if raise_on_fence:
                raise DeletionFenceActiveError(
                    f"Deletion fence blocks FTS indexing for WorkEvent {event_id}."
                )
            return False

        if ev_status != "CONFIRMED" or is_member or not indexable:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.WORK_EVENT,
                entity_id=event_id,
            )
            return False

        title_or_summary = _truncate_clean(title, MAX_TITLE_OR_SUMMARY_LEN)
        token_parts = [title, problem, action, outcome, *extra_tokens]
        search_tokens_text = _truncate_clean(
            " ".join(p for p in token_parts if p and p.strip()),
            MAX_SEARCH_TOKENS_LEN,
        )
        single_resource_id = linked_resources[0] if len(linked_resources) == 1 else None
        meta = {
            "anchor_mode": anchor_mode,
            "linked_resource_ids": linked_resources,
        }
        self._upsert_entry_in_tx(
            cur,
            workspace_id=workspace_id,
            source_resource_id=single_resource_id,
            entity_type=SearchEntityType.WORK_EVENT,
            entity_id=event_id,
            entity_version=ev_version,
            status_projection=ev_status,
            title_or_summary=title_or_summary,
            search_tokens_text=search_tokens_text,
            projection_metadata=meta,
            now=ts,
        )
        return True

    def sync_fact_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        fact_id: str,
        raise_on_fence: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Upsert or remove a single Fact projection in ``public.search_entries``."""
        if self.skip_index_update or not _search_entries_table_exists(cur):
            return False
        ts = now or self._now_fn()

        cur.execute(
            """
            SELECT
                f.id::text,
                f.event_id::text,
                f.version,
                f.fact_type,
                f.predicate,
                f.object_text,
                f.status,
                i.display_name
            FROM public.facts f
            LEFT JOIN public.identities i
                ON i.workspace_id = f.workspace_id
               AND i.id = f.subject_identity_id
            WHERE f.workspace_id = %s::uuid
              AND f.id = %s::uuid
            """,
            (workspace_id, fact_id),
        )
        row = cur.fetchone()
        if row is None:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.FACT,
                entity_id=fact_id,
            )
            return False

        event_id = str(row[1])
        fact_version = int(row[2])
        fact_type = str(row[3])
        predicate = str(row[4])
        object_text = str(row[5])
        fact_status = str(row[6])
        subject_name = str(row[7]) if row[7] is not None else ""

        linked_resources = _get_fact_linked_resources(
            cur, workspace_id=workspace_id, fact_id=fact_id, event_id=event_id
        )
        indexable, fenced = _resources_all_indexable(
            cur, workspace_id=workspace_id, resource_ids=linked_resources
        )
        if fenced:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.FACT,
                entity_id=fact_id,
            )
            if raise_on_fence:
                raise DeletionFenceActiveError(
                    f"Deletion fence blocks FTS indexing for Fact {fact_id}."
                )
            return False

        if fact_status not in ("SUPPORTED", "USER_CONFIRMED") or not indexable:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.FACT,
                entity_id=fact_id,
            )
            return False

        summary_raw = (
            f"{subject_name} {predicate}: {object_text}"
            if subject_name.strip()
            else f"{predicate}: {object_text}"
        )
        title_or_summary = _truncate_clean(summary_raw, MAX_TITLE_OR_SUMMARY_LEN)
        search_tokens_text = _truncate_clean(
            " ".join(p for p in (fact_type, subject_name, predicate, object_text) if p and p.strip()),
            MAX_SEARCH_TOKENS_LEN,
        )
        single_resource_id = linked_resources[0] if len(linked_resources) == 1 else None
        meta = {
            "event_id": event_id,
            "fact_type": fact_type,
            "linked_resource_ids": linked_resources,
        }
        self._upsert_entry_in_tx(
            cur,
            workspace_id=workspace_id,
            source_resource_id=single_resource_id,
            entity_type=SearchEntityType.FACT,
            entity_id=fact_id,
            entity_version=fact_version,
            status_projection=fact_status,
            title_or_summary=title_or_summary,
            search_tokens_text=search_tokens_text,
            projection_metadata=meta,
            now=ts,
        )
        return True

    def sync_claim_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        claim_id: str,
        raise_on_fence: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Upsert or remove a single Claim projection in ``public.search_entries``."""
        if self.skip_index_update or not _search_entries_table_exists(cur):
            return False
        ts = now or self._now_fn()

        cur.execute(
            """
            SELECT id::text, event_id::text, version, claim_text, status
            FROM public.claims
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
            """,
            (workspace_id, claim_id),
        )
        row = cur.fetchone()
        if row is None:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.CLAIM,
                entity_id=claim_id,
            )
            return False

        event_id = str(row[1])
        claim_version = int(row[2])
        claim_text = str(row[3])
        claim_status = str(row[4])

        linked_resources = _get_claim_linked_resources(
            cur, workspace_id=workspace_id, claim_id=claim_id, event_id=event_id
        )
        indexable, fenced = _resources_all_indexable(
            cur, workspace_id=workspace_id, resource_ids=linked_resources
        )
        if fenced:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.CLAIM,
                entity_id=claim_id,
            )
            if raise_on_fence:
                raise DeletionFenceActiveError(
                    f"Deletion fence blocks FTS indexing for Claim {claim_id}."
                )
            return False

        if claim_status != "APPROVED" or not indexable:
            self.delete_entity_in_tx(
                cur,
                workspace_id=workspace_id,
                entity_type=SearchEntityType.CLAIM,
                entity_id=claim_id,
            )
            return False

        cur.execute(
            """
            SELECT atom_text
            FROM public.claim_atoms
            WHERE workspace_id = %s::uuid
              AND claim_id = %s::uuid
            ORDER BY ordinal ASC, id ASC
            """,
            (workspace_id, claim_id),
        )
        atom_texts = [str(r[0]) for r in cur.fetchall() if r[0]]
        title_or_summary = _truncate_clean(claim_text, MAX_TITLE_OR_SUMMARY_LEN)
        search_tokens_text = _truncate_clean(
            " ".join([claim_text, *atom_texts]),
            MAX_SEARCH_TOKENS_LEN,
        )
        single_resource_id = linked_resources[0] if len(linked_resources) == 1 else None
        meta = {
            "event_id": event_id,
            "linked_resource_ids": linked_resources,
        }
        self._upsert_entry_in_tx(
            cur,
            workspace_id=workspace_id,
            source_resource_id=single_resource_id,
            entity_type=SearchEntityType.CLAIM,
            entity_id=claim_id,
            entity_version=claim_version,
            status_projection=claim_status,
            title_or_summary=title_or_summary,
            search_tokens_text=search_tokens_text,
            projection_metadata=meta,
            now=ts,
        )
        return True

    def _upsert_entry_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str | None,
        entity_type: SearchEntityType,
        entity_id: str,
        entity_version: int,
        status_projection: str,
        title_or_summary: str,
        search_tokens_text: str,
        projection_metadata: dict[str, Any],
        now: datetime,
    ) -> None:
        assert_no_forbidden_raw_keys(
            projection_metadata, field_name="search_entries.projection_metadata"
        )
        assert_no_forbidden_secret_keys(
            projection_metadata, field_name="search_entries.projection_metadata"
        )
        cur.execute(
            """
            INSERT INTO public.search_entries (
                id, workspace_id, source_resource_id, entity_type, entity_id,
                entity_version, status_projection, title_or_summary,
                search_tokens_text, index_version, projection_metadata, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s, %s::uuid,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (workspace_id, entity_type, entity_id)
            DO UPDATE SET
                source_resource_id = EXCLUDED.source_resource_id,
                entity_version = EXCLUDED.entity_version,
                status_projection = EXCLUDED.status_projection,
                title_or_summary = EXCLUDED.title_or_summary,
                search_tokens_text = EXCLUDED.search_tokens_text,
                index_version = EXCLUDED.index_version,
                projection_metadata = EXCLUDED.projection_metadata,
                updated_at = EXCLUDED.updated_at
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                source_resource_id,
                entity_type.value,
                entity_id,
                entity_version,
                status_projection,
                title_or_summary,
                search_tokens_text,
                self.index_version,
                Jsonb(projection_metadata),
                now,
            ),
        )

    # =========================================================================
    # Standalone Transactional Index Operations (with Pre-Commit Fence & Hook)
    # =========================================================================
    def sync_entity(
        self,
        *,
        workspace_id: str,
        entity_type: SearchEntityType,
        entity_id: str,
        access: WorkspaceAccessContext | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Synchronize one entity in its own transaction with pre-commit deletion fence check."""
        if self._connection_factory is None:
            raise RuntimeError("connection_factory is required for standalone indexer methods.")
        ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
        ent_uuid = _normalize_uuid(entity_id, "entity_id")
        ts = now or self._now_fn()

        with self._connection_factory() as conn:
            if access is not None:
                with workspace_scoped_transaction(conn, access) as cur:
                    return self._sync_entity_with_fence_in_tx(
                        cur,
                        workspace_id=ws_uuid,
                        entity_type=entity_type,
                        entity_id=ent_uuid,
                        now=ts,
                    )
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    return self._sync_entity_with_fence_in_tx(
                        cur,
                        workspace_id=ws_uuid,
                        entity_type=entity_type,
                        entity_id=ent_uuid,
                        now=ts,
                    )

    def _sync_entity_with_fence_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        entity_type: SearchEntityType,
        entity_id: str,
        now: datetime,
    ) -> bool:
        if entity_type is SearchEntityType.WORK_EVENT:
            linked_res, _ = _get_work_event_linked_resources_and_tokens(
                cur, workspace_id=workspace_id, event_id=entity_id
            )
            indexed = self.sync_work_event_in_tx(
                cur,
                workspace_id=workspace_id,
                event_id=entity_id,
                raise_on_fence=True,
                now=now,
            )
        elif entity_type is SearchEntityType.FACT:
            cur.execute(
                "SELECT event_id::text FROM public.facts WHERE workspace_id = %s::uuid AND id = %s::uuid",
                (workspace_id, entity_id),
            )
            frow = cur.fetchone()
            linked_res = (
                _get_fact_linked_resources(
                    cur, workspace_id=workspace_id, fact_id=entity_id, event_id=str(frow[0])
                )
                if frow is not None
                else []
            )
            indexed = self.sync_fact_in_tx(
                cur,
                workspace_id=workspace_id,
                fact_id=entity_id,
                raise_on_fence=True,
                now=now,
            )
        else:
            cur.execute(
                "SELECT event_id::text FROM public.claims WHERE workspace_id = %s::uuid AND id = %s::uuid",
                (workspace_id, entity_id),
            )
            crow = cur.fetchone()
            linked_res = (
                _get_claim_linked_resources(
                    cur, workspace_id=workspace_id, claim_id=entity_id, event_id=str(crow[0])
                )
                if crow is not None
                else []
            )
            indexed = self.sync_claim_in_tx(
                cur,
                workspace_id=workspace_id,
                claim_id=entity_id,
                raise_on_fence=True,
                now=now,
            )

        _invoke_before_commit_hook(
            self._before_commit_hook, action=f"search_index_{entity_type.value.lower()}", cur=cur
        )
        # Pre-commit fence re-verification (DELETE-07: zero resurrected FTS entries!)
        for rid in linked_res:
            if is_deletion_fence_active(cur, workspace_id, rid):
                raise DeletionFenceActiveError(
                    f"Pre-commit deletion fence blocked search index upsert for resource {rid}."
                )
        return indexed

    def rebuild_workspace(
        self,
        workspace_id: str,
        *,
        access: WorkspaceAccessContext | None = None,
        source_resource_id: str | None = None,
        now: datetime | None = None,
    ) -> SearchIndexerStats:
        """Full deterministic rebuild of ``public.search_entries`` for a workspace (or resource)."""
        if self._connection_factory is None:
            raise RuntimeError("connection_factory is required for rebuild_workspace.")
        ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
        res_uuid = (
            _normalize_uuid(source_resource_id, "source_resource_id")
            if source_resource_id is not None
            else None
        )
        ts = now or self._now_fn()

        with self._connection_factory() as conn:
            if access is not None:
                with workspace_scoped_transaction(conn, access) as cur:
                    return self._rebuild_in_tx(
                        cur, workspace_id=ws_uuid, source_resource_id=res_uuid, now=ts
                    )
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    return self._rebuild_in_tx(
                        cur, workspace_id=ws_uuid, source_resource_id=res_uuid, now=ts
                    )

    def _rebuild_in_tx(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        source_resource_id: str | None,
        now: datetime,
    ) -> SearchIndexerStats:
        if source_resource_id is not None:
            if is_deletion_fence_active(cur, workspace_id, source_resource_id):
                deleted = self.purge_resource_entries_in_tx(
                    cur, workspace_id=workspace_id, source_resource_id=source_resource_id
                )
                return SearchIndexerStats(
                    workspace_id=workspace_id,
                    index_version=self.index_version,
                    work_events_indexed=0,
                    facts_indexed=0,
                    claims_indexed=0,
                    deleted_entries=deleted,
                )
            deleted = self.purge_resource_entries_in_tx(
                cur, workspace_id=workspace_id, source_resource_id=source_resource_id
            )
        else:
            cur.execute(
                "DELETE FROM public.search_entries WHERE workspace_id = %s::uuid",
                (workspace_id,),
            )
            deleted = int(cur.rowcount or 0)

        touched_resources: set[str] = set()
        if source_resource_id is not None:
            touched_resources.add(source_resource_id)

        # 1. Index CONFIRMED WorkEvents
        cur.execute(
            """
            SELECT id::text
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND status = 'CONFIRMED'
            ORDER BY id ASC
            """,
            (workspace_id,),
        )
        event_ids = [str(r[0]) for r in cur.fetchall()]
        ev_count = 0
        for eid in event_ids:
            linked_res, _ = _get_work_event_linked_resources_and_tokens(
                cur, workspace_id=workspace_id, event_id=eid
            )
            if source_resource_id is not None and source_resource_id not in linked_res:
                continue
            if self.sync_work_event_in_tx(cur, workspace_id=workspace_id, event_id=eid, now=now):
                ev_count += 1
                touched_resources.update(linked_res)

        # 2. Index SUPPORTED / USER_CONFIRMED Facts
        cur.execute(
            """
            SELECT id::text, event_id::text
            FROM public.facts
            WHERE workspace_id = %s::uuid
              AND status IN ('SUPPORTED', 'USER_CONFIRMED')
            ORDER BY id ASC
            """,
            (workspace_id,),
        )
        fact_rows = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        fact_count = 0
        for fid, feid in fact_rows:
            linked_res = _get_fact_linked_resources(
                cur, workspace_id=workspace_id, fact_id=fid, event_id=feid
            )
            if source_resource_id is not None and source_resource_id not in linked_res:
                continue
            if self.sync_fact_in_tx(cur, workspace_id=workspace_id, fact_id=fid, now=now):
                fact_count += 1
                touched_resources.update(linked_res)

        # 3. Index APPROVED Claims
        cur.execute(
            """
            SELECT id::text, event_id::text
            FROM public.claims
            WHERE workspace_id = %s::uuid
              AND status = 'APPROVED'
            ORDER BY id ASC
            """,
            (workspace_id,),
        )
        claim_rows = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        claim_count = 0
        for cid, ceid in claim_rows:
            linked_res = _get_claim_linked_resources(
                cur, workspace_id=workspace_id, claim_id=cid, event_id=ceid
            )
            if source_resource_id is not None and source_resource_id not in linked_res:
                continue
            if self.sync_claim_in_tx(cur, workspace_id=workspace_id, claim_id=cid, now=now):
                claim_count += 1
                touched_resources.update(linked_res)

        _invoke_before_commit_hook(
            self._before_commit_hook, action="search_rebuild_workspace", cur=cur
        )
        for rid in sorted(touched_resources):
            if is_deletion_fence_active(cur, workspace_id, rid):
                raise DeletionFenceActiveError(
                    f"Pre-commit deletion fence blocked FTS index rebuild for resource {rid}."
                )

        return SearchIndexerStats(
            workspace_id=workspace_id,
            index_version=self.index_version,
            work_events_indexed=ev_count,
            facts_indexed=fact_count,
            claims_indexed=claim_count,
            deleted_entries=deleted,
        )


__all__ = [
    "BeforeCommitHook",
    "ConnectionFactory",
    "PostgresSearchIndexer",
]
