"""Post-IMPORT_READY Plan C derived Content Unit processor (`DerivedUnitIngestionProcessor`).

Enforces 방안 C §3.4:
- Executes STRICTLY for SyncRuns that have atomically reached `status='SUCCESS'`,
  `import_ready=true`, and have a persisted `IMPORT_READY` outbox event.
  NEVER runs for `PARTIAL`, `PARTIAL_WITH_LIMIT`, `FAILED`, or `CANCELED` runs.
- Ephemerally refetches raw source content into memory via `GitHubResolverContentFetcher`
  (with `EndpointSecurityPolicy` SSRF/redirect validation), verifies revision SHA-256 hash
  and `PUBLIC` visibility, parses units via `parse_content_units`, and persists ONLY
  Plan C metadata into `public.content_units` (`text_content=NULL`, `content_ref=NULL`,
  `retention_class='NONE'`, `expires_at=NULL`).
- If refetch encounters a revision hash mismatch or unavailable source -> preserves historical
  `sync_runs.status='SUCCESS'` & `import_ready=true` untouched and projects ONLY
  `sync_runs.processing_status='BLOCKED_SOURCE'`.
- If refetch encounters a non-PUBLIC visibility flip, policy denial, or deletion fence ->
  preserves `sync_runs.status='SUCCESS'` & `import_ready=true` untouched and projects ONLY
  `sync_runs.processing_status='BLOCKED_POLICY'` (0 tombstones).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    SourceObjectKind,
    canonicalize_source_bytes,
    parse_content_units,
)
from career_memory_api.jobs.models import AuditActorType
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.resolver.models import (
    ProviderFetchOutcomeKind,
    ProviderFetchRequest,
    RefetchRequestInput,
    ResolutionStatus,
    normalize_sha256_hash,
)
from career_memory_api.resolver.policy import EndpointSecurityPolicy, OperationBudgetConfig
from career_memory_api.resolver.ports import ProviderContentFetcherPort
from career_memory_api.resolver.service import RefetchResolverService
from career_memory_api.schema_contracts.enums import ResourceVisibility, SourceObjectType
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunStatus

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

_OBJECT_TYPE_TO_SOURCE_KIND: dict[SourceObjectType, SourceObjectKind] = {
    SourceObjectType.PR: SourceObjectKind.PR_BODY,
    SourceObjectType.COMMIT: SourceObjectKind.COMMIT_MESSAGE,
    SourceObjectType.ISSUE: SourceObjectKind.ISSUE_BODY,
    SourceObjectType.COMMENT: SourceObjectKind.COMMENT,
    SourceObjectType.REVIEW: SourceObjectKind.REVIEW_COMMENT,
}


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def _build_revision_endpoint_url(
    *,
    provider_resource_id: str,
    object_type: SourceObjectType,
    provider_object_id: str,
    normalized_metadata: dict[str, Any],
) -> str:
    if object_type is SourceObjectType.PR:
        return f"https://api.github.com/repositories/{provider_resource_id}/pulls/{provider_object_id}"
    if object_type is SourceObjectType.ISSUE:
        return f"https://api.github.com/repositories/{provider_resource_id}/issues/{provider_object_id}"
    if object_type is SourceObjectType.COMMIT:
        return f"https://api.github.com/repositories/{provider_resource_id}/commits/{provider_object_id}"
    if object_type is SourceObjectType.COMMENT:
        return f"https://api.github.com/repositories/{provider_resource_id}/issues/comments/{provider_object_id}"
    if object_type is SourceObjectType.REVIEW:
        # PR reviews (with review state) live under /pulls/{pr}/reviews/{id};
        # inline review comments live under /pulls/comments/{id}.
        pr_number = normalized_metadata.get("provider_number")
        if normalized_metadata.get("state") is not None and pr_number is not None:
            return (
                f"https://api.github.com/repositories/{provider_resource_id}"
                f"/pulls/{pr_number}/reviews/{provider_object_id}"
            )
        return f"https://api.github.com/repositories/{provider_resource_id}/pulls/comments/{provider_object_id}"
    raise ValueError(f"Unsupported object_type for derived unit refetch: {object_type!r}")


@dataclass(frozen=True, slots=True)
class DerivedUnitIngestionResult:
    """Result of running `DerivedUnitIngestionProcessor.process_import_ready_run`."""

    executed: bool
    sync_run_id: str
    sync_run_status: SyncRunStatus
    processing_status: ProcessingStatus
    revisions_processed: int = 0
    content_units_inserted: int = 0
    reason_code: str | None = None
    blocked_revision_id: str | None = None


class DerivedUnitIngestionProcessor:
    """Consumes confirmed `IMPORT_READY` runs and writes Plan C `content_units` metadata."""

    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        content_fetcher: ProviderContentFetcherPort,
        resolver_service: RefetchResolverService | None = None,
        security_policy: EndpointSecurityPolicy | None = None,
        budget: OperationBudgetConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._worker_connection_factory = worker_connection_factory
        self._content_fetcher = content_fetcher
        self._resolver_service = resolver_service
        self._security_policy = security_policy or EndpointSecurityPolicy()
        self._budget = budget or OperationBudgetConfig(
            max_requests=200,
            max_total_bytes=64 * 1024 * 1024,
            max_unit_bytes=5 * 1024 * 1024,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def process_import_ready_run(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        now: datetime | None = None,
    ) -> DerivedUnitIngestionResult:
        """Refetch ephemeral source bytes for an `IMPORT_READY` run and persist unit metadata."""
        ts = now or self._clock()
        ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    cur.execute(
                        """
                        SELECT
                            sr.source_resource_id::text,
                            sr.status,
                            sr.import_ready,
                            sr.processing_status,
                            res.provider_resource_id,
                            res.name,
                            res.visibility,
                            res.raw_storage_mode,
                            res.status,
                            sa.provider,
                            sa.status,
                            sa.token_secret_ref
                        FROM public.sync_runs sr
                        JOIN public.source_resources res
                          ON res.workspace_id = sr.workspace_id
                         AND res.id = sr.source_resource_id
                        JOIN public.source_accounts sa
                          ON sa.workspace_id = res.workspace_id
                         AND sa.id = res.source_account_id
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                        """,
                        (ws_uuid, run_uuid),
                    )
                    run_row = cur.fetchone()
                    if run_row is None:
                        raise ValueError(f"SyncRun {run_uuid} not found in workspace {ws_uuid}.")

                    (
                        resource_id,
                        run_status_raw,
                        import_ready,
                        proc_status_raw,
                        provider_resource_id,
                        resource_name,
                        res_visibility,
                        raw_storage_mode,
                        res_status,
                        provider_name,
                        acct_status,
                        token_secret_ref,
                    ) = (
                        str(run_row[0]),
                        str(run_row[1]),
                        bool(run_row[2]),
                        str(run_row[3]),
                        str(run_row[4]),
                        str(run_row[5]),
                        str(run_row[6]),
                        str(run_row[7]),
                        str(run_row[8]),
                        str(run_row[9]),
                        str(run_row[10]),
                        str(run_row[11]),
                    )
                    run_status = SyncRunStatus(run_status_raw)
                    proc_status = ProcessingStatus(proc_status_raw)

                    # Gate 1: Must be SUCCESS + import_ready=True + persisted IMPORT_READY outbox
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.outbox_events
                        WHERE workspace_id = %s::uuid
                          AND aggregate_type = 'SYNC_RUN'
                          AND aggregate_id = %s::uuid
                          AND event_type = 'IMPORT_READY'
                        """,
                        (ws_uuid, run_uuid),
                    )
                    has_import_ready_outbox = cur.fetchone() is not None

                    if run_status is not SyncRunStatus.SUCCESS or not import_ready or not has_import_ready_outbox:
                        return DerivedUnitIngestionResult(
                            executed=False,
                            sync_run_id=run_uuid,
                            sync_run_status=run_status,
                            processing_status=proc_status,
                            reason_code="RUN_NOT_IMPORT_READY",
                        )

                    # Gate 2: Check deletion & policy fences (project ONLY processing_status, never mutate status='SUCCESS'!)
                    if res_status == "DELETED" or is_deletion_fence_active(cur, ws_uuid, resource_id):
                        cur.execute(
                            """
                            UPDATE public.content_units cu
                            SET policy_state = 'BLOCKED'
                            FROM public.source_object_revisions rev
                            JOIN public.source_objects so
                              ON so.workspace_id = rev.workspace_id
                             AND so.id = rev.source_object_id
                            WHERE cu.workspace_id = %s::uuid
                              AND rev.workspace_id = cu.workspace_id
                              AND rev.id = cu.source_object_revision_id
                              AND so.source_resource_id = %s::uuid
                              AND cu.policy_state = 'ACTIVE'
                            """,
                            (ws_uuid, resource_id),
                        )
                        self._project_processing_status_in_tx(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="DELETION_FENCE_ACTIVE",
                            ts=ts,
                        )
                        return DerivedUnitIngestionResult(
                            executed=True,
                            sync_run_id=run_uuid,
                            sync_run_status=SyncRunStatus.SUCCESS,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="DELETION_FENCE_ACTIVE",
                        )

                    if acct_status != "ACTIVE":
                        self._project_processing_status_in_tx(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            processing_status=ProcessingStatus.BLOCKED_SOURCE,
                            reason_code="SOURCE_ACCOUNT_REVOKED",
                            ts=ts,
                        )
                        return DerivedUnitIngestionResult(
                            executed=True,
                            sync_run_id=run_uuid,
                            sync_run_status=SyncRunStatus.SUCCESS,
                            processing_status=ProcessingStatus.BLOCKED_SOURCE,
                            reason_code="SOURCE_ACCOUNT_REVOKED",
                        )

                    if (
                        res_status != "ACTIVE"
                        or res_visibility != ResourceVisibility.PUBLIC.value
                        or raw_storage_mode != "NONE"
                    ):
                        self._project_processing_status_in_tx(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="RESOURCE_POLICY_BLOCKED",
                            ts=ts,
                        )
                        return DerivedUnitIngestionResult(
                            executed=True,
                            sync_run_id=run_uuid,
                            sync_run_status=SyncRunStatus.SUCCESS,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="RESOURCE_POLICY_BLOCKED",
                        )

                    # Mark processing_status = RUNNING (status remains SUCCESS!)
                    cur.execute(
                        """
                        UPDATE public.sync_runs
                        SET processing_status = 'RUNNING',
                            updated_at = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                          AND status = 'SUCCESS'
                        """,
                        (ts, ws_uuid, run_uuid),
                    )

                    # Load non-tombstone revisions belonging to this source_resource
                    cur.execute(
                        """
                        SELECT
                            rev.id::text,
                            so.object_type,
                            so.provider_object_id,
                            rev.provider_revision_id,
                            rev.content_hash,
                            rev.normalized_metadata
                        FROM public.source_object_revisions rev
                        JOIN public.source_objects so
                          ON so.workspace_id = rev.workspace_id
                         AND so.id = rev.source_object_id
                        WHERE rev.workspace_id = %s::uuid
                          AND so.source_resource_id = %s::uuid
                          AND rev.is_tombstone = false
                          AND so.lifecycle_state = 'ACTIVE'
                        ORDER BY so.object_type ASC, so.provider_object_id ASC, rev.observed_at ASC
                        """,
                        (ws_uuid, resource_id),
                    )
                    revisions = [
                        (
                            str(r[0]),
                            SourceObjectType(str(r[1])),
                            str(r[2]),
                            str(r[3]) if r[3] is not None else None,
                            str(r[4]),
                            dict(r[5] or {}),
                        )
                        for r in cur.fetchall()
                    ]

        revisions_processed = 0
        units_inserted = 0
        operation_id = f"derived-units:{run_uuid}"

        for rev_id, obj_type, provider_object_id, provider_revision_id, expected_hash, norm_meta in revisions:
            source_kind = _OBJECT_TYPE_TO_SOURCE_KIND.get(obj_type)
            if source_kind is None:
                continue

            endpoint_url = _build_revision_endpoint_url(
                provider_resource_id=provider_resource_id,
                object_type=obj_type,
                provider_object_id=provider_object_id,
                normalized_metadata=norm_meta,
            )
            endpoint_check = self._security_policy.validate_endpoint_url(endpoint_url)
            if not endpoint_check.allowed or endpoint_check.normalized_url is None:
                return self._block_run_processing(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resource_id=resource_id,
                    processing_status=ProcessingStatus.BLOCKED_POLICY,
                    reason_code=endpoint_check.reason_code or "ENDPOINT_POLICY_BLOCKED",
                    blocked_revision_id=rev_id,
                    revisions_processed=revisions_processed,
                    units_inserted=units_inserted,
                    observed_visibility=None,
                    ts=self._clock(),
                )

            current_url = endpoint_check.normalized_url
            fetch_resp = None
            redirects = 0
            while True:
                fetch_resp = self._content_fetcher.fetch(
                    ProviderFetchRequest(
                        provider=provider_name,
                        endpoint_url=current_url,
                        provider_resource_id=provider_resource_id,
                        resource_name=resource_name,
                        object_type=obj_type.value,
                        provider_object_id=provider_object_id,
                        provider_revision_id=provider_revision_id,
                        token_secret_ref=token_secret_ref,
                        operation_id=operation_id,
                        max_bytes=self._budget.max_unit_bytes,
                        timeout_seconds=self._budget.timeout_seconds,
                        source_object_revision_id=rev_id,
                        expected_revision_hash=expected_hash,
                        is_diff=False,
                    )
                )
                if fetch_resp.kind is ProviderFetchOutcomeKind.REDIRECT:
                    redirects += 1
                    if redirects > self._budget.max_redirects:
                        return self._block_run_processing(
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            resource_id=resource_id,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="REDIRECT_LIMIT_EXCEEDED",
                            blocked_revision_id=rev_id,
                            revisions_processed=revisions_processed,
                            units_inserted=units_inserted,
                            observed_visibility=None,
                            ts=self._clock(),
                        )
                    redir_check = self._security_policy.validate_redirect_target(
                        current_url, fetch_resp.redirect_location
                    )
                    if not redir_check.allowed or redir_check.normalized_url is None:
                        return self._block_run_processing(
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            resource_id=resource_id,
                            processing_status=ProcessingStatus.BLOCKED_POLICY,
                            reason_code="REDIRECT_BLOCKED",
                            blocked_revision_id=rev_id,
                            revisions_processed=revisions_processed,
                            units_inserted=units_inserted,
                            observed_visibility=None,
                            ts=self._clock(),
                        )
                    current_url = redir_check.normalized_url
                    continue
                break

            assert fetch_resp is not None

            # Check provider visibility flip or policy denial -> BLOCKED_POLICY
            if (
                fetch_resp.provider_visibility is not None
                and fetch_resp.provider_visibility.strip().upper() != ResourceVisibility.PUBLIC.value
            ):
                return self._block_run_processing(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resource_id=resource_id,
                    processing_status=ProcessingStatus.BLOCKED_POLICY,
                    reason_code="REPOSITORY_VISIBILITY_CHANGED",
                    blocked_revision_id=rev_id,
                    revisions_processed=revisions_processed,
                    units_inserted=units_inserted,
                    observed_visibility=fetch_resp.provider_visibility.strip().upper(),
                    ts=self._clock(),
                )

            if fetch_resp.kind is ProviderFetchOutcomeKind.POLICY_DENIED:
                return self._block_run_processing(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resource_id=resource_id,
                    processing_status=ProcessingStatus.BLOCKED_POLICY,
                    reason_code=fetch_resp.reason_code or "SOURCE_POLICY_BLOCKED",
                    blocked_revision_id=rev_id,
                    revisions_processed=revisions_processed,
                    units_inserted=units_inserted,
                    observed_visibility=None,
                    ts=self._clock(),
                )

            if fetch_resp.kind is not ProviderFetchOutcomeKind.OK or fetch_resp.raw_bytes is None:
                return self._block_run_processing(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resource_id=resource_id,
                    processing_status=ProcessingStatus.BLOCKED_SOURCE,
                    reason_code=fetch_resp.reason_code or f"SOURCE_{fetch_resp.kind.value}",
                    blocked_revision_id=rev_id,
                    revisions_processed=revisions_processed,
                    units_inserted=units_inserted,
                    observed_visibility=None,
                    ts=self._clock(),
                )

            canon = canonicalize_source_bytes(
                fetch_resp.raw_bytes,
                mime_type=fetch_resp.mime_type or "text/markdown",
                is_diff=False,
            )
            if canon.status != "CANONICALIZED" or canon.document is None:
                revisions_processed += 1
                continue

            observed_hash = normalize_sha256_hash(
                canon.document.metadata.content_sha256,
                field_name="observed_revision_hash",
            )
            normalized_expected_hash = normalize_sha256_hash(
                expected_hash,
                field_name="expected_revision_hash",
            )
            if observed_hash != normalized_expected_hash:
                return self._block_run_processing(
                    ws_uuid=ws_uuid,
                    run_uuid=run_uuid,
                    resource_id=resource_id,
                    processing_status=ProcessingStatus.BLOCKED_SOURCE,
                    reason_code="SOURCE_REVISION_MISMATCH",
                    blocked_revision_id=rev_id,
                    revisions_processed=revisions_processed,
                    units_inserted=units_inserted,
                    observed_visibility=None,
                    ts=self._clock(),
                )

            parsed = parse_content_units(canon.document, source_kind)
            # Ephemeral raw text is dropped right after parsing; persist ONLY metadata (Plan C!)
            inserted_for_rev = self._persist_content_units_metadata(
                ws_uuid=ws_uuid,
                revision_id=rev_id,
                persisted_metadata=parsed.persisted_metadata,
                ts=self._clock(),
            )
            units_inserted += inserted_for_rev
            revisions_processed += 1

        # All revisions verified & parsed -> project processing_status = COMPLETE (status stays SUCCESS)
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    self._project_processing_status_in_tx(
                        cur,
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        processing_status=ProcessingStatus.COMPLETE,
                        reason_code="DERIVED_UNITS_COMPLETE",
                        ts=self._clock(),
                        extra_meta={
                            "revisions_processed": revisions_processed,
                            "content_units_inserted": units_inserted,
                        },
                    )

        return DerivedUnitIngestionResult(
            executed=True,
            sync_run_id=run_uuid,
            sync_run_status=SyncRunStatus.SUCCESS,
            processing_status=ProcessingStatus.COMPLETE,
            revisions_processed=revisions_processed,
            content_units_inserted=units_inserted,
        )

    def verify_unit_and_project_status(
        self,
        *,
        access: WorkspaceAccessContext,
        sync_run_id: str,
        content_unit_id: str,
        now: datetime | None = None,
    ) -> tuple[ResolutionStatus, ProcessingStatus | None]:
        """Resolve a persisted Content Unit via `RefetchResolverService` and project blocked status.

        Never mutates historical `sync_runs.status = 'SUCCESS'`.
        """
        if self._resolver_service is None:
            raise RuntimeError("resolver_service must be configured for verify_unit_and_project_status.")

        ts = now or self._clock()
        ws_uuid = _normalize_uuid(access.workspace_id, "workspace_id")
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")

        with self._resolver_service.open_operation_session(
            access, operation_id=f"verify-unit:{content_unit_id}"
        ) as session:
            handle = self._resolver_service.resolve_unit(
                session,
                RefetchRequestInput(
                    access=access,
                    content_unit_id=content_unit_id,
                    operation_id=f"verify-unit:{content_unit_id}",
                ),
            )
            res_status = handle.metadata.status
            reason_code = handle.metadata.reason_code

        projected: ProcessingStatus | None = None
        if res_status is ResolutionStatus.POLICY_BLOCKED:
            projected = ProcessingStatus.BLOCKED_POLICY
        elif res_status in (
            ResolutionStatus.REVISION_MISMATCH,
            ResolutionStatus.INVALID_LOCATOR,
            ResolutionStatus.UNAVAILABLE,
            ResolutionStatus.PARTIAL_WITH_LIMIT,
        ):
            projected = ProcessingStatus.BLOCKED_SOURCE

        if projected is not None:
            with self._worker_connection_factory() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        bind_worker_workspace_context(cur, ws_uuid)
                        self._project_processing_status_in_tx(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            processing_status=projected,
                            reason_code=reason_code or res_status.value,
                            ts=ts,
                        )
        return res_status, projected

    def _persist_content_units_metadata(
        self,
        *,
        ws_uuid: str,
        revision_id: str,
        persisted_metadata: tuple[Any, ...],
        ts: datetime,
    ) -> int:
        if not persisted_metadata:
            return 0
        inserted = 0
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    for idx, meta in enumerate(persisted_metadata):
                        anchor = meta.to_persisted_anchor_dict()
                        # Strip Plan C forbidden keys (text_content/content_ref are columns, not anchor keys)
                        anchor.pop("text_content", None)
                        anchor.pop("content_ref", None)
                        anchor.pop("retention_class", None)
                        assert_no_forbidden_raw_keys(anchor, field_name="content_units.anchor")
                        cur.execute(
                            """
                            INSERT INTO public.content_units (
                                id, workspace_id, source_object_revision_id, unit_type,
                                anchor, text_content, content_ref, content_hash,
                                canonicalization_version, retention_class, expires_at,
                                ordinal, policy_state, created_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                %s::jsonb, NULL, NULL, %s,
                                %s, 'NONE', NULL,
                                %s, 'ACTIVE', %s
                            )
                            ON CONFLICT (source_object_revision_id, ordinal) DO NOTHING
                            RETURNING id::text
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_uuid,
                                revision_id,
                                meta.unit_type.value,
                                json.dumps(anchor, sort_keys=True),
                                meta.locator.span_sha256,
                                CANONICALIZATION_VERSION,
                                idx,
                                ts,
                            ),
                        )
                        if cur.fetchone() is not None:
                            inserted += 1
        return inserted

    def _block_run_processing(
        self,
        *,
        ws_uuid: str,
        run_uuid: str,
        resource_id: str,
        processing_status: ProcessingStatus,
        reason_code: str,
        blocked_revision_id: str,
        revisions_processed: int,
        units_inserted: int,
        observed_visibility: str | None,
        ts: datetime,
    ) -> DerivedUnitIngestionResult:
        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)
                    if processing_status is ProcessingStatus.BLOCKED_POLICY:
                        if observed_visibility in (
                            ResourceVisibility.PRIVATE.value,
                            ResourceVisibility.INTERNAL.value,
                        ):
                            cur.execute(
                                """
                                UPDATE public.source_resources
                                SET visibility = %s,
                                    status = 'POLICY_REVIEW_REQUIRED',
                                    last_visibility_checked_at = %s
                                WHERE workspace_id = %s::uuid
                                  AND id = %s::uuid
                                  AND status <> 'DELETED'
                                """,
                                (observed_visibility, ts, ws_uuid, resource_id),
                            )
                        cur.execute(
                            """
                            UPDATE public.content_units cu
                            SET policy_state = 'BLOCKED'
                            FROM public.source_object_revisions rev
                            JOIN public.source_objects so
                              ON so.workspace_id = rev.workspace_id
                             AND so.id = rev.source_object_id
                            WHERE cu.workspace_id = %s::uuid
                              AND rev.workspace_id = cu.workspace_id
                              AND rev.id = cu.source_object_revision_id
                              AND so.source_resource_id = %s::uuid
                              AND cu.policy_state = 'ACTIVE'
                            """,
                            (ws_uuid, resource_id),
                        )
                    self._project_processing_status_in_tx(
                        cur,
                        ws_uuid=ws_uuid,
                        run_uuid=run_uuid,
                        processing_status=processing_status,
                        reason_code=reason_code,
                        ts=ts,
                        extra_meta={"blocked_revision_id": blocked_revision_id},
                    )

        return DerivedUnitIngestionResult(
            executed=True,
            sync_run_id=run_uuid,
            sync_run_status=SyncRunStatus.SUCCESS,
            processing_status=processing_status,
            revisions_processed=revisions_processed,
            content_units_inserted=units_inserted,
            reason_code=reason_code,
            blocked_revision_id=blocked_revision_id,
        )

    @staticmethod
    def _project_processing_status_in_tx(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
        processing_status: ProcessingStatus,
        reason_code: str,
        ts: datetime,
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        # Strictly preserves status = 'SUCCESS' and import_ready = true!
        cur.execute(
            """
            UPDATE public.sync_runs
            SET processing_status = %s,
                updated_at = %s
            WHERE workspace_id = %s::uuid
              AND id = %s::uuid
              AND status = 'SUCCESS'
              AND import_ready = true
            """,
            (processing_status.value, ts, ws_uuid, run_uuid),
        )
        audit_meta: dict[str, Any] = {
            "processing_status": processing_status.value,
            "reason_code": reason_code,
            "tombstone_created": False,
        }
        if extra_meta:
            audit_meta.update(extra_meta)
        assert_no_forbidden_raw_keys(audit_meta, field_name="audit_metadata")
        cur.execute(
            """
            INSERT INTO public.audit_events (
                id, workspace_id, actor_type, actor_id,
                action, target_type, target_id, redacted_metadata, created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s, NULL,
                'SYNC_RUN_PROCESSING_STATUS_PROJECTED', 'SYNC_RUN', %s::uuid, %s::jsonb, %s
            )
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                AuditActorType.WORKER.value,
                run_uuid,
                json.dumps(audit_meta, sort_keys=True),
                ts,
            ),
        )


__all__ = [
    "DerivedUnitIngestionProcessor",
    "DerivedUnitIngestionResult",
]
