"""PostgreSQL 18 Read Service for M3 WorkEvent Inbox & Detail API (T-303).

Executes strictly inside ``workspace_scoped_transaction`` under the least-privilege
``cm_api_app`` role (NOBYPASSRLS + FORCE RLS), never calls external GitHub APIs,
never exposes raw source text (Plan C), conceals missing/cross-workspace IDs as
404 ``RESOURCE_NOT_FOUND``, and projects live ``source_resources`` policy &
``deletion_jobs`` fence status in the same read transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.schema_contracts.enums import (
    CompositeMembershipStatus,
    ContributionRole,
    PrAssociationState,
    RecordOrigin,
    ResourceVisibility,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.work_events.history import (
    list_event_version_history,
    list_work_event_review_actions,
)
from career_memory_api.work_events.models import (
    _DEFAULT_WORK_EVENTS_CURSOR_SECRET,
    CompositeDetail,
    CompositeMemberSnapshotItem,
    CompositeSummary,
    DirectCommitSummary,
    DownstreamImpactSummary,
    WorkEventCounts,
    WorkEventCursorPayload,
    WorkEventDetailResponse,
    WorkEventError,
    WorkEventListResponse,
    WorkEventPageInfo,
    WorkEventPolicyState,
    WorkEventStatusCounts,
    WorkEventSummaryItem,
    compute_allowed_actions,
    compute_work_events_filter_hash,
    decode_and_verify_work_event_cursor,
    encode_work_event_cursor,
    format_work_event_etag,
)
from career_memory_api.work_events.relations import (
    list_active_context_relations,
    list_active_evidence_memberships,
    list_cannot_link_summary,
)
from career_memory_api.workspace_resources import WorkspaceResourceNotFoundError

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _extract_review_flags(summary_metadata: dict[str, Any]) -> list[str]:
    raw_flags = summary_metadata.get("review_flags")
    if not isinstance(raw_flags, list):
        return []
    return [str(flag) for flag in raw_flags if isinstance(flag, str) and flag.strip()]


class PostgresWorkEventsReadService:
    """Workspace-scoped PostgreSQL 18 read service for ``GET /v1/work-events*``."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        cursor_secret: str = _DEFAULT_WORK_EVENTS_CURSOR_SECRET,
    ) -> None:
        self._api_connection_factory = api_connection_factory
        self._cursor_secret = cursor_secret

    def list_work_events(
        self,
        access: WorkspaceAccessContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: WorkEventStatus | None = None,
        source_resource_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        role: ContributionRole | None = None,
    ) -> WorkEventListResponse:
        """List workspace-scoped WorkEvents with HMAC cursor pagination and zero double-counting."""
        if limit < 1 or limit > 100:
            raise WorkEventError(
                status_code=422,
                code="INVALID_PAGINATION_LIMIT",
                message="limit must be between 1 and 100.",
            )
        if from_date is not None and to_date is not None and from_date > to_date:
            raise WorkEventError(
                status_code=422,
                code="INVALID_DATE_RANGE",
                message="'from' date must be on or before 'to' date.",
            )

        normalized_resource_id: str | None = None
        if source_resource_id is not None:
            normalized_resource_id = _parse_uuid_or_none(source_resource_id)
            if normalized_resource_id is None:
                raise WorkspaceResourceNotFoundError()

        filter_hash = compute_work_events_filter_hash(
            status=status,
            source_resource_id=normalized_resource_id,
            from_date=from_date,
            to_date=to_date,
            role=role,
        )
        decoded_cursor: WorkEventCursorPayload | None = None
        if cursor is not None:
            decoded_cursor = decode_and_verify_work_event_cursor(
                cursor,
                expected_workspace_id=access.workspace_id,
                expected_filter_hash=filter_hash,
                secret=self._cursor_secret,
            )

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                # Conceal non-existent or cross-workspace source_resource_id as 404
                if normalized_resource_id is not None:
                    cur.execute(
                        """
                        SELECT 1
                        FROM public.source_resources
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (access.workspace_id, normalized_resource_id),
                    )
                    if cur.fetchone() is None:
                        raise WorkspaceResourceNotFoundError()

                base_where, base_params = self._build_filter_clauses(
                    workspace_id=access.workspace_id,
                    source_resource_id=normalized_resource_id,
                    from_date=from_date,
                    to_date=to_date,
                    role=role,
                )

                # Active top-level predicate (EV-010: zero double counting)
                active_top_level_sql = """
                    we.status IN ('CANDIDATE', 'CONFIRMED', 'STALE')
                    AND NOT EXISTS (
                        SELECT 1
                        FROM public.event_composite_memberships ecm_top
                        WHERE ecm_top.workspace_id = we.workspace_id
                          AND ecm_top.member_event_id = we.id
                          AND ecm_top.status = 'ACTIVE'
                    )
                """

                # 1. Counts for active top-level events under current scope/date/role filters
                counts_sql = f"""
                    SELECT we.status, COUNT(*)::integer
                    FROM public.work_events we
                    WHERE {' AND '.join(base_where)}
                      AND ({active_top_level_sql})
                    GROUP BY we.status
                """
                cur.execute(counts_sql, tuple(base_params))
                status_counts_map: dict[str, int] = {
                    WorkEventStatus.CANDIDATE.value: 0,
                    WorkEventStatus.CONFIRMED.value: 0,
                    WorkEventStatus.STALE.value: 0,
                }
                for st_val, cnt_val in cur.fetchall():
                    if str(st_val) in status_counts_map:
                        status_counts_map[str(st_val)] = int(cnt_val)
                counts = WorkEventCounts(
                    active_top_level_total=sum(status_counts_map.values()),
                    by_status=WorkEventStatusCounts(
                        CANDIDATE=status_counts_map[WorkEventStatus.CANDIDATE.value],
                        CONFIRMED=status_counts_map[WorkEventStatus.CONFIRMED.value],
                        STALE=status_counts_map[WorkEventStatus.STALE.value],
                    ),
                )

                # 2. Fetch page of events (+1 sentinel row for has_more)
                list_where = list(base_where)
                list_params = list(base_params)
                if status is None:
                    list_where.append(f"({active_top_level_sql})")
                else:
                    list_where.append("we.status = %s")
                    list_params.append(status.value)
                    if status in (
                        WorkEventStatus.CANDIDATE,
                        WorkEventStatus.CONFIRMED,
                        WorkEventStatus.STALE,
                    ):
                        list_where.append(
                            """
                            NOT EXISTS (
                                SELECT 1
                                FROM public.event_composite_memberships ecm_mem
                                WHERE ecm_mem.workspace_id = we.workspace_id
                                  AND ecm_mem.member_event_id = we.id
                                  AND ecm_mem.status = 'ACTIVE'
                            )
                            """
                        )

                sort_expr = "COALESCE(we.occurred_end_at, we.occurred_start_at, we.created_at)"
                if decoded_cursor is not None:
                    list_where.append(f"({sort_expr}, we.id) < (%s::timestamptz, %s::uuid)")
                    list_params.extend([decoded_cursor.sort_ts, decoded_cursor.last_event_id])

                list_sql = f"""
                    SELECT
                        we.id::text,
                        we.workspace_id::text,
                        we.primary_anchor_source_object_id::text,
                        we.anchor_mode,
                        we.status,
                        we.version,
                        we.title,
                        we.problem,
                        we.action,
                        we.outcome,
                        we.summary_metadata,
                        we.created_by,
                        we.occurred_start_at,
                        we.occurred_end_at,
                        we.reviewed_at,
                        we.updated_at,
                        ({active_top_level_sql}) AS is_active_top_level,
                        {sort_expr} AS sort_ts
                    FROM public.work_events we
                    WHERE {' AND '.join(list_where)}
                    ORDER BY {sort_expr} DESC, we.id DESC
                    LIMIT %s
                """
                list_params.append(limit + 1)
                cur.execute(list_sql, tuple(list_params))
                fetched_rows = cur.fetchall()

                has_more = len(fetched_rows) > limit
                page_rows = fetched_rows[:limit]

                items = [
                    self._hydrate_summary_item(cur, workspace_id=access.workspace_id, row=row)
                    for row in page_rows
                ]

                next_cursor: str | None = None
                if has_more and page_rows:
                    last_row = page_rows[-1]
                    last_sort_ts = last_row[17]
                    next_cursor = encode_work_event_cursor(
                        WorkEventCursorPayload(
                            workspace_id=access.workspace_id,
                            filter_hash=filter_hash,
                            sort_ts=last_sort_ts,
                            last_event_id=str(last_row[0]),
                        ),
                        secret=self._cursor_secret,
                    )

        return WorkEventListResponse(
            items=items,
            page=WorkEventPageInfo(next_cursor=next_cursor, has_more=has_more),
            counts=counts,
        )

    def get_work_event_detail(
        self,
        access: WorkspaceAccessContext,
        *,
        event_id: str,
    ) -> WorkEventDetailResponse:
        """Return full WorkEvent detail (including SUPERSEDED & REJECTED historical records)."""
        normalized_event_id = _parse_uuid_or_none(event_id)
        if normalized_event_id is None:
            raise WorkspaceResourceNotFoundError()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                cur.execute(
                    """
                    SELECT
                        we.id::text,
                        we.workspace_id::text,
                        we.primary_anchor_source_object_id::text,
                        we.anchor_mode,
                        we.status,
                        we.version,
                        we.title,
                        we.problem,
                        we.action,
                        we.outcome,
                        we.summary_metadata,
                        we.created_by,
                        we.occurred_start_at,
                        we.occurred_end_at,
                        we.reviewed_at,
                        we.updated_at,
                        (
                            we.status IN ('CANDIDATE', 'CONFIRMED', 'STALE')
                            AND NOT EXISTS (
                                SELECT 1
                                FROM public.event_composite_memberships ecm_top
                                WHERE ecm_top.workspace_id = we.workspace_id
                                  AND ecm_top.member_event_id = we.id
                                  AND ecm_top.status = 'ACTIVE'
                            )
                        ) AS is_active_top_level,
                        we.anchor_scope
                    FROM public.work_events we
                    WHERE we.workspace_id = %s::uuid
                      AND we.id = %s::uuid
                    """,
                    (access.workspace_id, normalized_event_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceResourceNotFoundError()

                summary = self._hydrate_summary_item(cur, workspace_id=access.workspace_id, row=row)
                primary_anchor_id = str(row[2]) if row[2] is not None else None
                anchor_scope = dict(row[17]) if row[17] is not None else None
                summary_metadata = dict(row[10] or {})

                evidence_memberships = list_active_evidence_memberships(
                    cur, workspace_id=access.workspace_id, event_id=normalized_event_id
                )
                context_relations = list_active_context_relations(
                    cur, workspace_id=access.workspace_id, event_id=normalized_event_id
                )
                cannot_link_constraints = list_cannot_link_summary(
                    cur, workspace_id=access.workspace_id, event_id=normalized_event_id
                )
                composite_details = self._load_composite_details(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=normalized_event_id,
                    anchor_mode=summary.anchor_mode,
                    event_version=summary.version,
                    summary_metadata=summary_metadata,
                )
                downstream_impact = self._load_downstream_impact(
                    cur,
                    workspace_id=access.workspace_id,
                    event_id=normalized_event_id,
                    event_status=summary.status,
                    policy_state=summary.policy_state,
                )
                version_history = list_event_version_history(
                    cur, workspace_id=access.workspace_id, event_id=normalized_event_id
                )
                review_actions = list_work_event_review_actions(
                    cur, workspace_id=access.workspace_id, event_id=normalized_event_id
                )

        return WorkEventDetailResponse(
            **summary.model_dump(),
            primary_anchor_source_object_id=primary_anchor_id,
            anchor_scope=anchor_scope,
            evidence_memberships=evidence_memberships,
            context_relations=context_relations,
            cannot_link_constraints=cannot_link_constraints,
            composite_details=composite_details,
            downstream_impact=downstream_impact,
            version_history=version_history,
            review_actions=review_actions,
        )

    # =========================================================================
    # Internal Hydration & Policy Projection Helpers
    # =========================================================================
    @staticmethod
    def _build_filter_clauses(
        *,
        workspace_id: str,
        source_resource_id: str | None,
        from_date: date | None,
        to_date: date | None,
        role: ContributionRole | None,
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = ["we.workspace_id = %s::uuid"]
        params: list[Any] = [workspace_id]

        if source_resource_id is not None:
            where.append(
                """
                (
                    EXISTS (
                        SELECT 1
                        FROM public.source_objects so_pa
                        WHERE so_pa.workspace_id = we.workspace_id
                          AND so_pa.id = we.primary_anchor_source_object_id
                          AND so_pa.source_resource_id = %s::uuid
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM public.event_memberships em_sr
                        INNER JOIN public.source_objects so_em
                            ON so_em.workspace_id = em_sr.workspace_id
                           AND so_em.id = em_sr.source_object_id
                        WHERE em_sr.workspace_id = we.workspace_id
                          AND em_sr.event_id = we.id
                          AND em_sr.status = 'ACTIVE'
                          AND em_sr.relation_class = 'MUST_LINK'
                          AND so_em.source_resource_id = %s::uuid
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM public.event_composite_memberships ecm_sr
                        INNER JOIN public.work_events m_we
                            ON m_we.workspace_id = ecm_sr.workspace_id
                           AND m_we.id = ecm_sr.member_event_id
                        INNER JOIN public.source_objects so_m
                            ON so_m.workspace_id = m_we.workspace_id
                           AND so_m.id = m_we.primary_anchor_source_object_id
                        WHERE ecm_sr.workspace_id = we.workspace_id
                          AND ecm_sr.composite_event_id = we.id
                          AND ecm_sr.status = 'ACTIVE'
                          AND so_m.source_resource_id = %s::uuid
                    )
                )
                """
            )
            params.extend([source_resource_id, source_resource_id, source_resource_id])

        if from_date is not None:
            from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
            where.append(
                "COALESCE(we.occurred_end_at, we.occurred_start_at, we.created_at) >= %s::timestamptz"
            )
            params.append(from_dt)

        if to_date is not None:
            next_day = to_date + timedelta(days=1)
            to_exclusive_dt = datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC)
            where.append(
                "COALESCE(we.occurred_start_at, we.occurred_end_at, we.created_at) < %s::timestamptz"
            )
            params.append(to_exclusive_dt)

        if role is not None:
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM public.contributions c
                    WHERE c.workspace_id = we.workspace_id
                      AND c.event_id = we.id
                      AND c.role = %s
                      AND c.status <> 'REJECTED'
                )
                """
            )
            params.append(role.value)

        return where, params

    def _hydrate_summary_item(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        row: tuple[Any, ...],
    ) -> WorkEventSummaryItem:
        event_id = str(row[0])
        primary_anchor_id = str(row[2]) if row[2] is not None else None
        anchor_mode = WorkEventAnchorMode(str(row[3]))
        status = WorkEventStatus(str(row[4]))
        version = int(row[5])
        title = str(row[6])
        problem = str(row[7]) if row[7] is not None else None
        action_text = str(row[8]) if row[8] is not None else None
        outcome = str(row[9]) if row[9] is not None else None
        summary_metadata = dict(row[10] or {})
        created_by = RecordOrigin(str(row[11]))
        occurred_start_at = row[12]
        occurred_end_at = row[13]
        reviewed_at = row[14]
        updated_at = row[15]
        is_active_top_level = bool(row[16])

        # Evidence count (STRICTLY MUST_LINK + ACTIVE) & Context count (STRICTLY CONTEXT_ONLY + ACTIVE)
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE relation_class = 'MUST_LINK')::integer AS evidence_count,
                COUNT(*) FILTER (WHERE relation_class = 'CONTEXT_ONLY')::integer AS context_count
            FROM public.event_memberships
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND status = 'ACTIVE'
            """,
            (workspace_id, event_id),
        )
        cnt_row = cur.fetchone()
        evidence_count = int(cnt_row[0]) if cnt_row is not None else 0
        context_count = int(cnt_row[1]) if cnt_row is not None else 0

        # Resolve linked source_resource_ids & real-time policy/deletion state
        linked_resource_ids = self._resolve_linked_source_resource_ids(
            cur,
            workspace_id=workspace_id,
            event_id=event_id,
            primary_anchor_source_object_id=primary_anchor_id,
        )
        primary_resource_id = linked_resource_ids[0] if linked_resource_ids else None
        policy_state = self._evaluate_live_policy_state(
            cur,
            workspace_id=workspace_id,
            resource_ids=linked_resource_ids,
        )

        # DIRECT_COMMIT badge
        direct_commit_badge: DirectCommitSummary | None = None
        if anchor_mode is WorkEventAnchorMode.DIRECT_COMMIT:
            direct_commit_badge = self._build_direct_commit_badge(
                cur,
                workspace_id=workspace_id,
                primary_anchor_source_object_id=primary_anchor_id,
                summary_metadata=summary_metadata,
                occurred_start_at=occurred_start_at,
            )

        # USER_COMPOSITE summary
        composite_summary: CompositeSummary | None = None
        if anchor_mode is WorkEventAnchorMode.USER_COMPOSITE:
            cur.execute(
                """
                SELECT member_event_id::text
                FROM public.event_composite_memberships
                WHERE workspace_id = %s::uuid
                  AND composite_event_id = %s::uuid
                  AND status = 'ACTIVE'
                ORDER BY ordinal ASC, member_event_id ASC
                """,
                (workspace_id, event_id),
            )
            member_ids = [str(r[0]) for r in cur.fetchall()]
            raw_merge_reason = summary_metadata.get("merge_reason")
            raw_regen_status = summary_metadata.get("regeneration_status")
            composite_summary = CompositeSummary(
                member_count=len(member_ids),
                merge_reason=str(raw_merge_reason) if raw_merge_reason is not None else None,
                regeneration_status=str(raw_regen_status) if raw_regen_status is not None else None,
                member_event_ids=member_ids,
            )

        allowed_actions = compute_allowed_actions(
            anchor_mode=anchor_mode,
            status=status,
            policy_state=policy_state,
            is_active_top_level=is_active_top_level,
        )

        return WorkEventSummaryItem(
            event_id=event_id,
            workspace_id=workspace_id,
            source_resource_id=primary_resource_id,
            anchor_mode=anchor_mode,
            status=status,
            version=version,
            etag=format_work_event_etag(version),
            is_active_top_level=is_active_top_level,
            title=title,
            problem=problem,
            action=action_text,
            outcome=outcome,
            created_by=created_by,
            occurred_start_at=occurred_start_at,
            occurred_end_at=occurred_end_at,
            reviewed_at=reviewed_at,
            updated_at=updated_at,
            review_flags=_extract_review_flags(summary_metadata),
            allowed_actions=allowed_actions,
            evidence_count=evidence_count,
            context_count=context_count,
            direct_commit_badge=direct_commit_badge,
            composite_summary=composite_summary,
            policy_state=policy_state,
        )

    @staticmethod
    def _resolve_linked_source_resource_ids(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        primary_anchor_source_object_id: str | None,
    ) -> list[str]:
        resource_ids: list[str] = []

        if primary_anchor_source_object_id is not None:
            cur.execute(
                """
                SELECT source_resource_id::text
                FROM public.source_objects
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (workspace_id, primary_anchor_source_object_id),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                resource_ids.append(str(row[0]))

        cur.execute(
            """
            SELECT DISTINCT so.source_resource_id::text
            FROM public.event_memberships em
            INNER JOIN public.source_objects so
                ON so.workspace_id = em.workspace_id
               AND so.id = em.source_object_id
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.status = 'ACTIVE'
              AND em.relation_class = 'MUST_LINK'
            ORDER BY so.source_resource_id::text ASC
            """,
            (workspace_id, event_id),
        )
        for (rid,) in cur.fetchall():
            rid_str = str(rid)
            if rid_str not in resource_ids:
                resource_ids.append(rid_str)

        # For USER_COMPOSITE (or any event with composite members), inspect member events' resources
        cur.execute(
            """
            SELECT DISTINCT so.source_resource_id::text
            FROM public.event_composite_memberships ecm
            INNER JOIN public.work_events m_we
                ON m_we.workspace_id = ecm.workspace_id
               AND m_we.id = ecm.member_event_id
            INNER JOIN public.source_objects so
                ON so.workspace_id = m_we.workspace_id
               AND so.id = m_we.primary_anchor_source_object_id
            WHERE ecm.workspace_id = %s::uuid
              AND ecm.composite_event_id = %s::uuid
            ORDER BY so.source_resource_id::text ASC
            """,
            (workspace_id, event_id),
        )
        for (rid,) in cur.fetchall():
            rid_str = str(rid)
            if rid_str not in resource_ids:
                resource_ids.append(rid_str)

        return resource_ids

    @staticmethod
    def _evaluate_live_policy_state(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        resource_ids: list[str],
    ) -> WorkEventPolicyState:
        if not resource_ids:
            return WorkEventPolicyState.ALLOWED

        cur.execute(
            """
            SELECT
                sr.id::text,
                sr.visibility,
                sr.status,
                sa.status AS account_status,
                EXISTS (
                    SELECT 1
                    FROM public.deletion_jobs dj
                    WHERE dj.workspace_id = sr.workspace_id
                      AND dj.source_resource_id = sr.id
                      AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
                ) AS has_deletion_fence
            FROM public.source_resources sr
            INNER JOIN public.source_accounts sa
                ON sa.workspace_id = sr.workspace_id
               AND sa.id = sr.source_account_id
            WHERE sr.workspace_id = %s::uuid
              AND sr.id = ANY(%s::uuid[])
            """,
            (workspace_id, resource_ids),
        )
        rows = cur.fetchall()
        if len(rows) < len(resource_ids):
            # Missing resource row implies deleted/purged
            return WorkEventPolicyState.DELETION_PENDING

        policy_blocked = False
        for _, visibility, res_status, acct_status, has_fence in rows:
            if str(res_status) == "DELETED" or bool(has_fence):
                return WorkEventPolicyState.DELETION_PENDING
            if (
                str(visibility) != ResourceVisibility.PUBLIC.value
                or str(res_status) in ("PAUSED", "POLICY_REVIEW_REQUIRED")
                or str(acct_status) != "ACTIVE"
            ):
                policy_blocked = True

        return (
            WorkEventPolicyState.POLICY_BLOCKED
            if policy_blocked
            else WorkEventPolicyState.ALLOWED
        )

    @staticmethod
    def _build_direct_commit_badge(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        primary_anchor_source_object_id: str | None,
        summary_metadata: dict[str, Any],
        occurred_start_at: datetime | None,
    ) -> DirectCommitSummary:
        default_branch = str(summary_metadata.get("default_branch") or "").strip()
        abbreviated_sha = str(summary_metadata.get("abbreviated_sha") or "").strip()
        commit_sha = str(summary_metadata.get("commit_sha") or "").strip()
        committed_at = _parse_iso_datetime(summary_metadata.get("committed_at")) or occurred_start_at
        reachable = summary_metadata.get("default_branch_reachable")
        raw_assoc_state = summary_metadata.get("pr_association_state")
        requires_conf = summary_metadata.get("requires_user_confirmation")

        # Fall back to persisted source_objects / sync_commit_pr_associations if any field missing
        if primary_anchor_source_object_id is not None and (
            not default_branch or not abbreviated_sha or raw_assoc_state is None
        ):
            cur.execute(
                """
                SELECT
                    so.provider_object_id,
                    so.pr_association_state,
                    scpa.default_branch_name,
                    scpa.commit_sha,
                    scpa.reachable_at_start,
                    scpa.reachable_at_end,
                    scpa.pr_association_state
                FROM public.source_objects so
                LEFT JOIN public.sync_commit_pr_associations scpa
                    ON scpa.workspace_id = so.workspace_id
                   AND scpa.commit_source_object_id = so.id
                WHERE so.workspace_id = %s::uuid
                  AND so.id = %s::uuid
                ORDER BY scpa.observed_at DESC NULLS LAST
                LIMIT 1
                """,
                (workspace_id, primary_anchor_source_object_id),
            )
            fb_row = cur.fetchone()
            if fb_row is not None:
                if not default_branch and fb_row[2]:
                    default_branch = str(fb_row[2]).strip()
                if not commit_sha:
                    commit_sha = str(fb_row[3] or fb_row[0] or "").strip().lower()
                if not abbreviated_sha and commit_sha:
                    abbreviated_sha = commit_sha[:12]
                if raw_assoc_state is None:
                    raw_assoc_state = fb_row[6] or fb_row[1]
                if reachable is None and fb_row[4] is not None and fb_row[5] is not None:
                    reachable = bool(fb_row[4]) and bool(fb_row[5])

        if not abbreviated_sha:
            abbreviated_sha = (commit_sha[:12] if commit_sha else "0000000")
        if len(abbreviated_sha) < 7:
            abbreviated_sha = abbreviated_sha.ljust(7, "0")
        if not default_branch:
            default_branch = "unknown"

        assoc_state = (
            PrAssociationState(str(raw_assoc_state))
            if raw_assoc_state in PrAssociationState._value2member_map_
            else PrAssociationState.NO_MATCH_OBSERVED
        )
        return DirectCommitSummary(
            default_branch=default_branch,
            abbreviated_sha=abbreviated_sha,
            committed_at=committed_at,
            default_branch_reachable=True if reachable is None else bool(reachable),
            pr_association_state=assoc_state,
            requires_user_confirmation=True if requires_conf is None else bool(requires_conf),
        )

    @staticmethod
    def _load_composite_details(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        anchor_mode: WorkEventAnchorMode,
        event_version: int,
        summary_metadata: dict[str, Any],
    ) -> CompositeDetail | None:
        if anchor_mode is WorkEventAnchorMode.USER_COMPOSITE:
            cur.execute(
                """
                SELECT
                    ecm.member_event_id::text,
                    ecm.member_event_version,
                    ecm.ordinal,
                    ecm.status,
                    wev.title,
                    wev.anchor_mode,
                    wev.status AS status_at_version,
                    wev.occurred_start_at,
                    wev.occurred_end_at,
                    wev.membership_snapshot,
                    we.status AS current_status,
                    we.title AS current_title,
                    we.anchor_mode AS current_anchor_mode
                FROM public.event_composite_memberships ecm
                LEFT JOIN public.work_event_versions wev
                    ON wev.workspace_id = ecm.workspace_id
                   AND wev.event_id = ecm.member_event_id
                   AND wev.version = ecm.member_event_version
                LEFT JOIN public.work_events we
                    ON we.workspace_id = ecm.workspace_id
                   AND we.id = ecm.member_event_id
                WHERE ecm.workspace_id = %s::uuid
                  AND ecm.composite_event_id = %s::uuid
                  AND ecm.composite_event_version = (
                      SELECT COALESCE(MAX(ecm2.composite_event_version), %s)
                      FROM public.event_composite_memberships ecm2
                      WHERE ecm2.workspace_id = %s::uuid
                        AND ecm2.composite_event_id = %s::uuid
                  )
                ORDER BY ecm.ordinal ASC, ecm.member_event_id ASC
                """,
                (workspace_id, event_id, event_version, workspace_id, event_id),
            )
            member_rows = cur.fetchall()
            members: list[CompositeMemberSnapshotItem] = []
            for r in member_rows:
                snap_available = r[4] is not None
                snap_dict = dict(r[9] or {}) if snap_available else {}
                must_ids = snap_dict.get("must_link_source_object_ids")
                ev_cnt = len(must_ids) if isinstance(must_ids, list) else 0
                members.append(
                    CompositeMemberSnapshotItem(
                        member_event_id=str(r[0]),
                        member_event_version=int(r[1]),
                        ordinal=int(r[2]),
                        membership_status=CompositeMembershipStatus(str(r[3])),
                        title=str(r[4]) if r[4] is not None else (str(r[11]) if r[11] is not None else None),
                        anchor_mode=(
                            WorkEventAnchorMode(str(r[5]))
                            if r[5] is not None
                            else (WorkEventAnchorMode(str(r[12])) if r[12] is not None else None)
                        ),
                        status_at_version=WorkEventStatus(str(r[6])) if r[6] is not None else None,
                        current_status=WorkEventStatus(str(r[10])) if r[10] is not None else None,
                        occurred_start_at=r[7],
                        occurred_end_at=r[8],
                        evidence_count=ev_cnt,
                        snapshot_available=snap_available,
                    )
                )
            active_count = sum(
                1 for m in members if m.membership_status is CompositeMembershipStatus.ACTIVE
            )
            raw_merge_reason = summary_metadata.get("merge_reason")
            raw_regen_status = summary_metadata.get("regeneration_status")
            return CompositeDetail(
                member_count=active_count,
                merge_reason=str(raw_merge_reason) if raw_merge_reason is not None else None,
                regeneration_status=str(raw_regen_status) if raw_regen_status is not None else None,
                superseded_by_composite_id=None,
                members=members,
            )

        # For a non-composite event that is currently superseded by an active USER_COMPOSITE,
        # expose the superseding composite ID for traceability (EV-010 / EV-011).
        cur.execute(
            """
            SELECT composite_event_id::text
            FROM public.event_composite_memberships
            WHERE workspace_id = %s::uuid
              AND member_event_id = %s::uuid
              AND status = 'ACTIVE'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (workspace_id, event_id),
        )
        sup_row = cur.fetchone()
        if sup_row is not None:
            return CompositeDetail(
                member_count=0,
                merge_reason=None,
                regeneration_status=None,
                superseded_by_composite_id=str(sup_row[0]),
                members=[],
            )
        return None

    @staticmethod
    def _load_downstream_impact(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        event_id: str,
        event_status: WorkEventStatus,
        policy_state: WorkEventPolicyState,
    ) -> DownstreamImpactSummary:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status <> 'REJECTED')::integer,
                COUNT(*) FILTER (WHERE status = 'STALE')::integer
            FROM public.facts
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        f_row = cur.fetchone()
        fact_count = int(f_row[0]) if f_row is not None else 0
        stale_fact_count = int(f_row[1]) if f_row is not None else 0

        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status NOT IN ('REJECTED', 'SUPERSEDED'))::integer,
                COUNT(*) FILTER (WHERE status = 'STALE')::integer
            FROM public.claims
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
            """,
            (workspace_id, event_id),
        )
        c_row = cur.fetchone()
        claim_count = int(c_row[0]) if c_row is not None else 0
        stale_claim_count = int(c_row[1]) if c_row is not None else 0

        if policy_state is WorkEventPolicyState.DELETION_PENDING:
            processing_status = "DELETION_PENDING"
        elif policy_state is WorkEventPolicyState.POLICY_BLOCKED:
            processing_status = "POLICY_BLOCKED"
        elif (
            event_status is WorkEventStatus.STALE
            or stale_fact_count > 0
            or stale_claim_count > 0
        ):
            processing_status = "STALE_REGENERATION_REQUIRED"
        elif fact_count > 0 or claim_count > 0:
            processing_status = "READY"
        else:
            processing_status = "NOT_STARTED"

        return DownstreamImpactSummary(
            fact_count=fact_count,
            claim_count=claim_count,
            export_count=0,
            stale_fact_count=stale_fact_count,
            stale_claim_count=stale_claim_count,
            processing_status=processing_status,
        )


__all__ = [
    "ConnectionFactory",
    "PostgresWorkEventsReadService",
]
