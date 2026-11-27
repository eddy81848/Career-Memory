"""Workspace-scoped PostgreSQL and in-memory metadata-only provenance stores (Plan C)."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import ContentUnitType
from career_memory_api.resolver.models import (
    PersistedUnitResolutionTarget,
    ResolutionMetadata,
    ResolutionStatus,
    assert_no_forbidden_raw_keys,
)


def _try_normalize_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _row_to_target(
    row: tuple[Any, ...],
    *,
    deletion_fence_active: bool = False,
) -> PersistedUnitResolutionTarget:
    anchor = dict(row[25]) if isinstance(row[25], dict) else {}
    parser_version = str(anchor.get("parser_version", ""))
    norm_meta = dict(row[22]) if isinstance(row[22], dict) else {}
    return PersistedUnitResolutionTarget(
        workspace_id=row[0],
        source_account_id=row[1],
        source_account_provider=row[2],
        source_account_status=row[3],
        source_account_token_secret_ref=row[4],
        source_resource_id=row[5],
        provider_resource_id=row[6],
        resource_type=row[7],
        resource_name=row[8],
        canonical_url=row[9],
        visibility=row[10],
        processing_mode=row[11],
        raw_storage_mode=row[12],
        resource_status=row[13],
        source_object_id=row[14],
        object_type=row[15],
        provider_object_id=row[16],
        object_lifecycle_state=row[17],
        source_object_revision_id=row[18],
        provider_revision_id=row[19],
        expected_revision_hash=row[20],
        hash_policy_version=row[21],
        revision_normalized_metadata=norm_meta,
        revision_is_tombstone=bool(row[23]),
        content_unit_id=row[24],
        anchor=anchor,
        unit_type=ContentUnitType(row[26]),
        expected_unit_hash=row[27],
        canonicalization_version=row[28],
        parser_version=parser_version,
        unit_ordinal=int(row[29]),
        unit_policy_state=row[30],
        text_content=row[31],
        content_ref=row[32],
        retention_class=row[33],
        last_verified_at=row[34],
        deletion_fence_active=deletion_fence_active,
    )


def _query_deletion_fence_active(
    cur: psycopg.Cursor,
    workspace_id: str,
    source_resource_id: str,
) -> bool:
    """Check public.deletion_jobs fence when 0040+ schema is present (safe on 0010+0020 clusters)."""
    cur.execute("SELECT to_regclass('public.deletion_jobs') IS NOT NULL")
    reg_row = cur.fetchone()
    if not reg_row or not bool(reg_row[0]):
        return False
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM public.deletion_jobs AS dj
            WHERE dj.workspace_id = %s::uuid
              AND dj.source_resource_id = %s::uuid
              AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
        )
        """,
        (workspace_id, source_resource_id),
    )
    fence_row = cur.fetchone()
    return bool(fence_row and fence_row[0])


_SELECT_TARGET_COLUMNS = """
    SELECT
        cu.workspace_id::text,
        acc.id::text,
        acc.provider,
        acc.status,
        acc.token_secret_ref,
        res.id::text,
        res.provider_resource_id,
        res.resource_type,
        res.name,
        res.canonical_url,
        res.visibility,
        res.processing_mode,
        res.raw_storage_mode,
        res.status,
        obj.id::text,
        obj.object_type,
        obj.provider_object_id,
        obj.lifecycle_state,
        rev.id::text,
        rev.provider_revision_id,
        rev.content_hash,
        rev.hash_policy_version,
        rev.normalized_metadata,
        rev.is_tombstone,
        cu.id::text,
        cu.anchor,
        cu.unit_type,
        cu.content_hash,
        cu.canonicalization_version,
        cu.ordinal,
        cu.policy_state,
        cu.text_content,
        cu.content_ref,
        cu.retention_class,
        res.last_visibility_checked_at
    FROM public.content_units AS cu
    JOIN public.source_object_revisions AS rev
      ON rev.workspace_id = cu.workspace_id
     AND rev.id = cu.source_object_revision_id
    JOIN public.source_objects AS obj
      ON obj.workspace_id = rev.workspace_id
     AND obj.id = rev.source_object_id
    JOIN public.source_resources AS res
      ON res.workspace_id = obj.workspace_id
     AND res.id = obj.source_resource_id
    JOIN public.source_accounts AS acc
      ON acc.workspace_id = res.workspace_id
     AND acc.id = res.source_account_id
"""


class PostgresProvenanceMetadataStore:
    """PostgreSQL 18 RLS-enforced metadata-only provenance store for Plan C resolver.

    Exposes NO bare ``get_by_id`` method. Every operation requires a server-verified
    ``WorkspaceAccessContext`` and executes inside ``workspace_scoped_transaction``.
    Never writes raw source text and never updates immutable ``source_object_revisions``.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_unit_resolution_target(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        unit_uuid = _try_normalize_uuid(content_unit_id)
        if unit_uuid is None:
            return None

        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                _SELECT_TARGET_COLUMNS
                + """
                WHERE cu.workspace_id = %s::uuid
                  AND cu.id = %s::uuid
                """,
                (access.workspace_id, unit_uuid),
            )
            row = cur.fetchone()
            if row is None:
                return None
            fence_active = _query_deletion_fence_active(cur, access.workspace_id, str(row[5]))

        target = _row_to_target(row, deletion_fence_active=fence_active)
        if target.workspace_id != access.workspace_id:
            return None
        return target

    def list_sibling_unit_anchors(
        self,
        access: WorkspaceAccessContext,
        source_object_revision_id: str,
    ) -> tuple[PersistedUnitResolutionTarget, ...]:
        rev_uuid = _try_normalize_uuid(source_object_revision_id)
        if rev_uuid is None:
            return ()

        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                _SELECT_TARGET_COLUMNS
                + """
                WHERE cu.workspace_id = %s::uuid
                  AND cu.source_object_revision_id = %s::uuid
                ORDER BY cu.ordinal ASC
                """,
                (access.workspace_id, rev_uuid),
            )
            rows = cur.fetchall()
            fence_by_resource: dict[str, bool] = {}
            for row in rows:
                res_id = str(row[5])
                if res_id not in fence_by_resource:
                    fence_by_resource[res_id] = _query_deletion_fence_active(
                        cur, access.workspace_id, res_id
                    )

        return tuple(
            target
            for row in rows
            if (
                target := _row_to_target(
                    row, deletion_fence_active=fence_by_resource.get(str(row[5]), False)
                )
            ).workspace_id
            == access.workspace_id
        )

    def check_policy_fence_current(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        return self.get_unit_resolution_target(access, content_unit_id)

    def record_resolution_metadata(
        self,
        access: WorkspaceAccessContext,
        metadata: ResolutionMetadata,
    ) -> None:
        # Assert strictly zero raw-text keys before any DB operation
        assert_no_forbidden_raw_keys(metadata.to_dict(), context="PostgresProvenanceMetadataStore")

        # Only update non-raw last_visibility_checked_at on verified public MATCHED;
        # NEVER mutate source_object_revisions (DATA-01) or store text in content_units (REF-04).
        if metadata.status is not ResolutionStatus.MATCHED:
            return

        rev_uuid = _try_normalize_uuid(metadata.source_object_revision_id)
        if rev_uuid is None:
            return

        with workspace_scoped_transaction(self._conn, access) as cur:
            cur.execute(
                """
                UPDATE public.source_resources AS sr
                SET last_visibility_checked_at = %s
                FROM public.source_objects AS so
                JOIN public.source_object_revisions AS rev
                  ON rev.workspace_id = so.workspace_id
                 AND rev.source_object_id = so.id
                WHERE sr.workspace_id = %s::uuid
                  AND so.workspace_id = sr.workspace_id
                  AND so.source_resource_id = sr.id
                  AND rev.id = %s::uuid
                """,
                (metadata.checked_at, access.workspace_id, rev_uuid),
            )


class InMemoryProvenanceMetadataStore:
    """Workspace-scoped in-memory metadata store for offline unit & HTTP contract tests."""

    def __init__(self, targets: Sequence[PersistedUnitResolutionTarget] = ()) -> None:
        self._targets: dict[tuple[str, str], PersistedUnitResolutionTarget] = {}
        self.recorded_metadata: list[ResolutionMetadata] = []
        for target in targets:
            self.put_target(target)

    def put_target(self, target: PersistedUnitResolutionTarget) -> None:
        assert_no_forbidden_raw_keys(target.anchor, context="InMemoryProvenanceMetadataStore.anchor")
        assert_no_forbidden_raw_keys(
            target.revision_normalized_metadata,
            context="InMemoryProvenanceMetadataStore.revision_normalized_metadata",
        )
        self._targets[(target.workspace_id, target.content_unit_id)] = target

    def get_unit_resolution_target(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        target = self._targets.get((access.workspace_id, content_unit_id))
        if target is None or target.workspace_id != access.workspace_id:
            return None
        return target

    def list_sibling_unit_anchors(
        self,
        access: WorkspaceAccessContext,
        source_object_revision_id: str,
    ) -> tuple[PersistedUnitResolutionTarget, ...]:
        matched = [
            target
            for (ws_id, _cu_id), target in self._targets.items()
            if ws_id == access.workspace_id
            and target.source_object_revision_id == source_object_revision_id
        ]
        return tuple(sorted(matched, key=lambda item: item.unit_ordinal))

    def check_policy_fence_current(
        self,
        access: WorkspaceAccessContext,
        content_unit_id: str,
    ) -> PersistedUnitResolutionTarget | None:
        return self.get_unit_resolution_target(access, content_unit_id)

    def record_resolution_metadata(
        self,
        access: WorkspaceAccessContext,
        metadata: ResolutionMetadata,
    ) -> None:
        assert_no_forbidden_raw_keys(metadata.to_dict(), context="InMemoryProvenanceMetadataStore")
        self.recorded_metadata.append(metadata)

    def dump_all_persisted_records(self) -> list[dict[str, Any]]:
        """Return a deep-copied snapshot of all stored metadata records for REF-04 canary audits."""
        dumped: list[dict[str, Any]] = []
        for target in self._targets.values():
            dumped.append(
                {
                    "workspace_id": target.workspace_id,
                    "source_resource_id": target.source_resource_id,
                    "source_object_id": target.source_object_id,
                    "source_object_revision_id": target.source_object_revision_id,
                    "content_unit_id": target.content_unit_id,
                    "expected_revision_hash": target.expected_revision_hash,
                    "expected_unit_hash": target.expected_unit_hash,
                    "anchor": copy.deepcopy(target.anchor),
                    "text_content": target.text_content,
                    "content_ref": target.content_ref,
                    "retention_class": target.retention_class,
                }
            )
        return dumped
