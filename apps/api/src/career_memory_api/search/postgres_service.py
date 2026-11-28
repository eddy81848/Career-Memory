"""Permission-Safe PostgreSQL 18 FTS Search Service with Authoritative Pre-Response Recheck
and Cursor Versioning (T-501 / T-501A / OD-008 / SEARCH-01..05).

Key invariants enforced on every `GET /v1/search` request:
1. Workspace-scoped `cm_api_app` transaction with live active `workspace_memberships` check.
2. `OD-008`: `project_id` globally forbidden; explicit `source_resource_id` must belong to the
   authenticated workspace (identical 404 `RESOURCE_NOT_FOUND` on nonexistent or cross-workspace);
   when omitted and exactly 1 active unfenced repository exists, server auto-selects it.
3. Zero-window fence & policy exclusion: fenced, deleted, non-PUBLIC, or account-revoked resources
   never leak hits even when stale `public.search_entries` rows remain behind (index lag).
4. Authoritative pre-response recheck (`T-501A`): NEVER trusts `search_entries` status/version.
   - `WORK_EVENT`: collapsed via `collapse_event_ids_for_search_and_export`, strictly `CONFIRMED`.
   - `FACT`: strictly `SUPPORTED` or `USER_CONFIRMED`, active support & non-conflicted identity.
   - `CLAIM`: strictly `APPROVED` AND live `VerificationCalculator` reports non-UNVERIFIED with 0
     stale/unsupported atoms, 0 identity conflicts, 0 policy blocks, and 0 deletion fences.
5. Deterministic ranking, versioned cursor bound to `(workspace_id, source_resource_id, query_hash,
   index_version, ranking_version)`, degraded-mode authoritative fallback during index rebuild,
   and metadata-only `public.search_runs` logging (zero raw query text).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from career_memory_api.auth.postgres_guard import workspace_scoped_transaction
from career_memory_api.authz import WorkspaceAccessContext, WorkspaceMembershipDeniedError
from career_memory_api.claims.verification import VerificationCalculator
from career_memory_api.contracts_api.models import (
    DEFAULT_SEARCH_INDEX_VERSION,
    DEFAULT_SEARCH_RANKING_VERSION,
    ContractBoundaryError,
    SearchCursorPayload,
    SearchEntityType,
    SearchHit,
    SearchResponse,
    SearchScope,
    assert_no_prohibited_project_id,
    compute_query_hash,
    decode_and_verify_search_cursor,
    encode_search_cursor,
)
from career_memory_api.identities.models import IdentityAttributionBlockReason
from career_memory_api.identities.postgres_service import evaluate_identity_attribution
from career_memory_api.jobs.postgres_runtime import is_deletion_fence_active
from career_memory_api.schema_contracts.enums import ClaimStatus, ClaimVerification, IdentityStatus
from career_memory_api.schema_contracts.metadata_guard import (
    assert_no_forbidden_raw_keys,
    assert_no_forbidden_secret_keys,
)
from career_memory_api.search.indexer import (
    _get_claim_linked_resources,
    _get_fact_linked_resources,
    _get_work_event_linked_resources_and_tokens,
    _resources_all_indexable,
    _truncate_clean,
)
from career_memory_api.search.models import (
    DEFAULT_SEARCH_CANDIDATE_LIMIT,
    MAX_TITLE_OR_SUMMARY_LEN,
    SearchRunRecord,
)
from career_memory_api.work_events.search_collapse import collapse_event_ids_for_search_and_export

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def _parse_uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _escape_like_token(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresSearchService:
    """PostgreSQL 18 permission-safe FTS search service implementing ``ContractBoundaryPort.search``."""

    def __init__(
        self,
        *,
        api_connection_factory: ConnectionFactory,
        index_version: str = DEFAULT_SEARCH_INDEX_VERSION,
        ranking_version: str = DEFAULT_SEARCH_RANKING_VERSION,
        candidate_limit: int = DEFAULT_SEARCH_CANDIDATE_LIMIT,
        force_degraded_mode: bool = False,
        verification_calculator: VerificationCalculator | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be >= 1.")
        self._api_connection_factory = api_connection_factory
        self.index_version = index_version
        self.ranking_version = ranking_version
        self.candidate_limit = candidate_limit
        self.force_degraded_mode = force_degraded_mode
        self._verification = verification_calculator or VerificationCalculator()
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def search(
        self,
        access: WorkspaceAccessContext,
        *,
        q: str,
        source_resource_id: str | None = None,
        entity_types: list[SearchEntityType] | None = None,
        limit: int = 20,
        cursor: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> SearchResponse:
        """Execute permission-safe FTS search with authoritative recheck & metadata-only logging."""
        if extra_params:
            assert_no_prohibited_project_id(extra_params)

        cleaned_q = " ".join(q.strip().split())
        if not cleaned_q:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_SEARCH_QUERY",
                message="Query parameter 'q' must not be blank.",
            )
        if limit < 1 or limit > 100:
            raise ContractBoundaryError(
                status_code=422,
                code="INVALID_SEARCH_LIMIT",
                message="Query parameter 'limit' must be between 1 and 100.",
            )

        normalized_types: list[SearchEntityType] | None = None
        if entity_types:
            seen_t: set[SearchEntityType] = set()
            normalized_types = []
            for et in entity_types:
                et_enum = et if isinstance(et, SearchEntityType) else SearchEntityType(str(et))
                if et_enum not in seen_t:
                    seen_t.add(et_enum)
                    normalized_types.append(et_enum)

        query_hash = compute_query_hash(cleaned_q, normalized_types)
        ts = self._now_fn()

        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)

                # 1. OD-008 Scope Resolution
                effective_source_resource_id, explicit_resource_blocked = (
                    self._resolve_search_scope(
                        cur,
                        workspace_id=access.workspace_id,
                        requested_source_resource_id=source_resource_id,
                    )
                )

                # 2. Verify opaque versioned cursor (bound to workspace, scope, query, index, ranking)
                offset = 0
                if cursor is not None:
                    decoded = decode_and_verify_search_cursor(
                        cursor,
                        expected_workspace_id=access.workspace_id,
                        expected_source_resource_id=effective_source_resource_id,
                        expected_query_hash=query_hash,
                        expected_index_version=self.index_version,
                        expected_ranking_version=self.ranking_version,
                    )
                    offset = decoded.offset

                # 3. Candidate Retrieval (FTS vs Degraded Authoritative Fallback)
                target_types = normalized_types or [
                    SearchEntityType.WORK_EVENT,
                    SearchEntityType.FACT,
                    SearchEntityType.CLAIM,
                ]
                degraded_mode = self._should_use_degraded_mode(
                    cur, workspace_id=access.workspace_id
                )

                if explicit_resource_blocked:
                    raw_candidates: list[tuple[SearchEntityType, str, float]] = []
                    degraded_mode = False
                elif degraded_mode:
                    raw_candidates = self._fetch_degraded_authoritative_candidates(
                        cur,
                        workspace_id=access.workspace_id,
                        query_text=cleaned_q,
                        entity_types=target_types,
                    )
                else:
                    raw_candidates = self._fetch_fts_candidates(
                        cur,
                        workspace_id=access.workspace_id,
                        query_text=cleaned_q,
                        source_resource_id=effective_source_resource_id,
                        entity_types=target_types,
                    )

                raw_candidate_count = len(raw_candidates)

                # 4. Authoritative Pre-Response Recheck (T-501A: NEVER trust search_entries!)
                verified_hits, lag_filtered_count = self._recheck_candidates_authoritatively(
                    cur,
                    workspace_id=access.workspace_id,
                    candidates=raw_candidates,
                    effective_source_resource_id=effective_source_resource_id,
                )

                # 5. Deterministic Ranking & Cursor Pagination
                verified_hits.sort(
                    key=lambda h: (-round(h.score, 6), h.entity_type.value, h.entity_id)
                )
                page_items = verified_hits[offset : offset + limit]
                next_cursor: str | None = None
                if len(verified_hits) > offset + limit:
                    next_cursor = encode_search_cursor(
                        SearchCursorPayload(
                            workspace_id=access.workspace_id,
                            source_resource_id=effective_source_resource_id,
                            query_hash=query_hash,
                            index_version=self.index_version,
                            ranking_version=self.ranking_version,
                            offset=offset + limit,
                        )
                    )

                # 6. Metadata-only Search Run Audit (SEARCH-04: ZERO raw query text!)
                run_metadata = {
                    "explicit_source_resource_id": source_resource_id is not None,
                    "explicit_resource_blocked": explicit_resource_blocked,
                    "verified_total_count": len(verified_hits),
                }
                assert_no_forbidden_raw_keys(run_metadata, field_name="search_runs.run_metadata")
                assert_no_forbidden_secret_keys(run_metadata, field_name="search_runs.run_metadata")
                cur.execute(
                    """
                    INSERT INTO public.search_runs (
                        id, workspace_id, source_resource_id, query_hash,
                        entity_types, index_version, ranking_version,
                        candidate_limit, page_limit, cursor_offset,
                        raw_candidate_count, lag_filtered_count, returned_count,
                        degraded_mode, run_metadata, created_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s::text[], %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        str(uuid.uuid4()),
                        access.workspace_id,
                        effective_source_resource_id,
                        query_hash,
                        [et.value for et in (normalized_types or ())],
                        self.index_version,
                        self.ranking_version,
                        self.candidate_limit,
                        limit,
                        offset,
                        raw_candidate_count,
                        lag_filtered_count,
                        len(page_items),
                        degraded_mode,
                        Jsonb(run_metadata),
                        ts,
                    ),
                )

                return SearchResponse(
                    scope=SearchScope(
                        workspace_id=access.workspace_id,
                        source_resource_id=effective_source_resource_id,
                    ),
                    query_hash=query_hash,
                    index_version=self.index_version,
                    ranking_version=self.ranking_version,
                    items=page_items,
                    next_cursor=next_cursor,
                )

    def list_search_runs(
        self,
        access: WorkspaceAccessContext,
        *,
        limit: int = 20,
    ) -> list[SearchRunRecord]:
        """Read metadata-only search run audit records for the authenticated workspace."""
        with self._api_connection_factory() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                self._assert_live_workspace_membership(cur, access)
                cur.execute(
                    """
                    SELECT
                        id::text,
                        workspace_id::text,
                        source_resource_id::text,
                        query_hash,
                        entity_types,
                        index_version,
                        ranking_version,
                        candidate_limit,
                        page_limit,
                        cursor_offset,
                        raw_candidate_count,
                        lag_filtered_count,
                        returned_count,
                        degraded_mode,
                        run_metadata,
                        created_at
                    FROM public.search_runs
                    WHERE workspace_id = %s::uuid
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (access.workspace_id, limit),
                )
                return [
                    SearchRunRecord(
                        id=str(r[0]),
                        workspace_id=str(r[1]),
                        source_resource_id=str(r[2]) if r[2] is not None else None,
                        query_hash=str(r[3]),
                        entity_types=tuple(str(t) for t in (r[4] or ())),
                        index_version=str(r[5]),
                        ranking_version=str(r[6]),
                        candidate_limit=int(r[7]),
                        page_limit=int(r[8]),
                        cursor_offset=int(r[9]),
                        raw_candidate_count=int(r[10]),
                        lag_filtered_count=int(r[11]),
                        returned_count=int(r[12]),
                        degraded_mode=bool(r[13]),
                        run_metadata=dict(r[14] or {}),
                        created_at=r[15],
                    )
                    for r in cur.fetchall()
                ]

    # =========================================================================
    # Internal Scope, Retrieval & Authoritative Recheck Helpers
    # =========================================================================
    @staticmethod
    def _assert_live_workspace_membership(
        cur: psycopg.Cursor, access: WorkspaceAccessContext
    ) -> None:
        cur.execute(
            """
            SELECT 1
            FROM public.workspace_memberships
            WHERE workspace_id = %s::uuid
              AND user_id = %s::uuid
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (access.workspace_id, access.user_id),
        )
        if cur.fetchone() is None:
            raise WorkspaceMembershipDeniedError("Workspace membership is no longer active.")

    @staticmethod
    def _resolve_search_scope(
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        requested_source_resource_id: str | None,
    ) -> tuple[str | None, bool]:
        if requested_source_resource_id is not None:
            parsed_res_id = _parse_uuid_or_none(requested_source_resource_id)
            if parsed_res_id is None:
                raise ContractBoundaryError(
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message="Requested resource was not found.",
                )
            cur.execute(
                """
                SELECT sr.status, sr.visibility, sa.status
                FROM public.source_resources sr
                INNER JOIN public.source_accounts sa
                    ON sa.workspace_id = sr.workspace_id
                   AND sa.id = sr.source_account_id
                WHERE sr.workspace_id = %s::uuid
                  AND sr.id = %s::uuid
                """,
                (workspace_id, parsed_res_id),
            )
            row = cur.fetchone()
            if row is None:
                # Identical 404 for nonexistent and cross-workspace IDs (FOUNDATION-03 / SEARCH-03)
                raise ContractBoundaryError(
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message="Requested resource was not found.",
                )
            res_status, res_vis, acc_status = str(row[0]), str(row[1]), str(row[2])
            fenced = is_deletion_fence_active(cur, workspace_id, parsed_res_id)
            blocked = (
                fenced
                or res_status != "ACTIVE"
                or res_vis != "PUBLIC"
                or acc_status != "ACTIVE"
            )
            return parsed_res_id, blocked

        # OD-008: Auto-select single active connected repository when source_resource_id is omitted
        cur.execute(
            """
            SELECT sr.id::text
            FROM public.source_resources sr
            INNER JOIN public.source_accounts sa
                ON sa.workspace_id = sr.workspace_id
               AND sa.id = sr.source_account_id
            WHERE sr.workspace_id = %s::uuid
              AND sr.status = 'ACTIVE'
              AND sr.visibility = 'PUBLIC'
              AND sa.status = 'ACTIVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.deletion_jobs dj
                  WHERE dj.workspace_id = sr.workspace_id
                    AND dj.source_resource_id = sr.id
                    AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')
              )
            ORDER BY sr.id ASC
            """,
            (workspace_id,),
        )
        active_ids = [str(r[0]) for r in cur.fetchall()]
        if len(active_ids) == 1:
            return active_ids[0], False
        return None, False

    def _should_use_degraded_mode(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
    ) -> bool:
        if self.force_degraded_mode:
            return True
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE index_version = %s) AS matching_ver,
                COUNT(*) FILTER (WHERE index_version <> %s) AS mismatched_ver
            FROM public.search_entries
            WHERE workspace_id = %s::uuid
            """,
            (self.index_version, self.index_version, workspace_id),
        )
        row = cur.fetchone()
        if row is None:
            return False
        matching_ver, mismatched_ver = int(row[0] or 0), int(row[1] or 0)
        return mismatched_ver > 0 and matching_ver == 0

    def _fetch_fts_candidates(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        query_text: str,
        source_resource_id: str | None,
        entity_types: list[SearchEntityType],
    ) -> list[tuple[SearchEntityType, str, float]]:
        tokens = list(dict.fromkeys(tok for tok in query_text.split() if tok))
        like_clauses: list[str] = []
        like_params: list[Any] = []
        for tok in tokens:
            pat = f"%{_escape_like_token(tok)}%"
            like_clauses.append(
                "(se.title_or_summary ILIKE %s ESCAPE '\\' OR se.search_tokens_text ILIKE %s ESCAPE '\\')"
            )
            like_params.extend([pat, pat])
        like_conjunction = " AND ".join(like_clauses) if like_clauses else "FALSE"

        where_parts = [
            "se.workspace_id = %s::uuid",
            "se.entity_type = ANY(%s::text[])",
            "se.index_version = %s",
            f"""(
                se.search_tsv @@ plainto_tsquery('simple', %s)
                OR se.search_tsv @@ websearch_to_tsquery('english', %s)
                OR ({like_conjunction})
            )""",
        ]
        params: list[Any] = [
            query_text,
            query_text,
            workspace_id,
            [et.value for et in entity_types],
            self.index_version,
            query_text,
            query_text,
            *like_params,
        ]
        if source_resource_id is not None:
            # Include source_resource_id IS NULL so multi-resource composites/claims are checked authoritatively
            where_parts.append("(se.source_resource_id = %s::uuid OR se.source_resource_id IS NULL)")
            params.append(source_resource_id)

        params.append(self.candidate_limit)
        cur.execute(
            f"""
            SELECT
                se.entity_type,
                se.entity_id::text,
                GREATEST(
                    ts_rank_cd(se.search_tsv, plainto_tsquery('simple', %s)),
                    ts_rank_cd(se.search_tsv, websearch_to_tsquery('english', %s)),
                    0.05
                )::double precision AS rank_score
            FROM public.search_entries se
            WHERE {' AND '.join(where_parts)}
            ORDER BY rank_score DESC, se.entity_type ASC, se.entity_id ASC
            LIMIT %s
            """,
            params,
        )
        return [
            (SearchEntityType(str(r[0])), str(r[1]), float(r[2]))
            for r in cur.fetchall()
        ]

    def _fetch_degraded_authoritative_candidates(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        query_text: str,
        entity_types: list[SearchEntityType],
    ) -> list[tuple[SearchEntityType, str, float]]:
        """Authoritative non-raw scan used during index rebuild / degraded mode."""
        tokens = list(dict.fromkeys(tok for tok in query_text.split() if tok))
        if not tokens:
            return []
        candidates: list[tuple[SearchEntityType, str, float]] = []

        if SearchEntityType.WORK_EVENT in entity_types:
            conds = ["we.workspace_id = %s::uuid", "we.status <> 'REJECTED'"]
            params: list[Any] = [workspace_id]
            for tok in tokens:
                pat = f"%{_escape_like_token(tok)}%"
                conds.append(
                    "(we.title ILIKE %s ESCAPE '\\' OR COALESCE(we.problem, '') ILIKE %s ESCAPE '\\' "
                    "OR COALESCE(we.action, '') ILIKE %s ESCAPE '\\' OR COALESCE(we.outcome, '') ILIKE %s ESCAPE '\\')"
                )
                params.extend([pat, pat, pat, pat])
            params.append(self.candidate_limit)
            cur.execute(
                f"""
                SELECT we.id::text
                FROM public.work_events we
                WHERE {' AND '.join(conds)}
                ORDER BY we.id ASC
                LIMIT %s
                """,
                params,
            )
            for row in cur.fetchall():
                candidates.append((SearchEntityType.WORK_EVENT, str(row[0]), 0.5))

        if SearchEntityType.FACT in entity_types:
            conds = [
                "f.workspace_id = %s::uuid",
                "f.status IN ('SUPPORTED', 'USER_CONFIRMED')",
            ]
            params = [workspace_id]
            for tok in tokens:
                pat = f"%{_escape_like_token(tok)}%"
                conds.append(
                    "(f.predicate ILIKE %s ESCAPE '\\' OR f.object_text ILIKE %s ESCAPE '\\')"
                )
                params.extend([pat, pat])
            params.append(self.candidate_limit)
            cur.execute(
                f"""
                SELECT f.id::text
                FROM public.facts f
                WHERE {' AND '.join(conds)}
                ORDER BY f.id ASC
                LIMIT %s
                """,
                params,
            )
            for row in cur.fetchall():
                candidates.append((SearchEntityType.FACT, str(row[0]), 0.4))

        if SearchEntityType.CLAIM in entity_types:
            conds = ["c.workspace_id = %s::uuid", "c.status = 'APPROVED'"]
            params = [workspace_id]
            for tok in tokens:
                pat = f"%{_escape_like_token(tok)}%"
                conds.append("c.claim_text ILIKE %s ESCAPE '\\'")
                params.append(pat)
            params.append(self.candidate_limit)
            cur.execute(
                f"""
                SELECT c.id::text
                FROM public.claims c
                WHERE {' AND '.join(conds)}
                ORDER BY c.id ASC
                LIMIT %s
                """,
                params,
            )
            for row in cur.fetchall():
                candidates.append((SearchEntityType.CLAIM, str(row[0]), 0.6))

        return candidates[: self.candidate_limit]

    def _recheck_candidates_authoritatively(
        self,
        cur: psycopg.Cursor,
        *,
        workspace_id: str,
        candidates: list[tuple[SearchEntityType, str, float]],
        effective_source_resource_id: str | None,
    ) -> tuple[list[SearchHit], int]:
        """Re-verify EVERY candidate against authoritative tables, fences, policies & live verification."""
        if not candidates:
            return [], 0

        verified: list[SearchHit] = []
        lag_filtered = 0

        # Partition by entity type
        we_candidates: list[tuple[str, float]] = []
        fact_candidates: list[tuple[str, float]] = []
        claim_candidates: list[tuple[str, float]] = []
        for etype, eid, score in candidates:
            if etype is SearchEntityType.WORK_EVENT:
                we_candidates.append((eid, score))
            elif etype is SearchEntityType.FACT:
                fact_candidates.append((eid, score))
            elif etype is SearchEntityType.CLAIM:
                claim_candidates.append((eid, score))

        # 1. WORK_EVENT recheck + single-logical composite collapse
        if we_candidates:
            raw_ids = [eid for eid, _ in we_candidates]
            score_by_raw: dict[str, float] = {}
            for eid, sc in we_candidates:
                score_by_raw[eid] = max(score_by_raw.get(eid, 0.0), sc)

            collapsed_ids = collapse_event_ids_for_search_and_export(
                cur, workspace_id=workspace_id, candidate_event_ids=raw_ids
            )
            # Map each raw candidate to its collapsed top-level ID for accurate scoring & lag accounting
            collapsed_scores: dict[str, float] = {}
            for cid in collapsed_ids:
                collapsed_scores[cid] = score_by_raw.get(cid, 0.0)
            if collapsed_ids:
                cur.execute(
                    """
                    SELECT ecm.member_event_id::text, ecm.composite_event_id::text
                    FROM public.event_composite_memberships ecm
                    WHERE ecm.workspace_id = %s::uuid
                      AND ecm.member_event_id = ANY(%s::uuid[])
                      AND ecm.composite_event_id = ANY(%s::uuid[])
                      AND ecm.status = 'ACTIVE'
                    """,
                    (workspace_id, raw_ids, collapsed_ids),
                )
                for member_id, comp_id in cur.fetchall():
                    m_sc = score_by_raw.get(str(member_id), 0.0)
                    c_s = str(comp_id)
                    if c_s in collapsed_scores:
                        collapsed_scores[c_s] = max(collapsed_scores[c_s], m_sc)

            # Count candidates dropped during collapse (REJECTED / dissolved SUPERSEDED / deleted)
            survived_raw = 0
            cur.execute(
                """
                SELECT
                    we.id::text,
                    we.status,
                    active_comp.composite_event_id::text
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
                    LIMIT 1
                ) active_comp ON true
                WHERE we.workspace_id = %s::uuid
                  AND we.id = ANY(%s::uuid[])
                """,
                (workspace_id, list(score_by_raw.keys())),
            )
            raw_to_logical: dict[str, str | None] = {}
            for ev_id, ev_st, comp_id in cur.fetchall():
                if str(ev_st) == "REJECTED":
                    raw_to_logical[str(ev_id)] = None
                elif comp_id is not None:
                    raw_to_logical[str(ev_id)] = str(comp_id)
                elif str(ev_st) in ("CANDIDATE", "CONFIRMED", "STALE"):
                    raw_to_logical[str(ev_id)] = str(ev_id)
                else:
                    raw_to_logical[str(ev_id)] = None

            valid_logical_hits: set[str] = set()
            for logical_id in collapsed_ids:
                cur.execute(
                    """
                    SELECT title, status
                    FROM public.work_events
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                    """,
                    (workspace_id, logical_id),
                )
                wrow = cur.fetchone()
                if wrow is None or str(wrow[1]) != "CONFIRMED":
                    continue
                linked_res, _ = _get_work_event_linked_resources_and_tokens(
                    cur, workspace_id=workspace_id, event_id=logical_id
                )
                ok_res, fenced = _resources_all_indexable(
                    cur, workspace_id=workspace_id, resource_ids=linked_res
                )
                if fenced or not ok_res:
                    continue
                if (
                    effective_source_resource_id is not None
                    and effective_source_resource_id not in linked_res
                ):
                    continue
                valid_logical_hits.add(logical_id)
                hit_res = (
                    effective_source_resource_id
                    if effective_source_resource_id is not None
                    else (linked_res[0] if len(linked_res) == 1 else None)
                )
                verified.append(
                    SearchHit(
                        entity_type=SearchEntityType.WORK_EVENT,
                        entity_id=logical_id,
                        source_resource_id=hit_res,
                        title_or_summary=_truncate_clean(str(wrow[0]), MAX_TITLE_OR_SUMMARY_LEN),
                        score=max(collapsed_scores.get(logical_id, 0.05), 0.0),
                    )
                )

            for raw_id, _ in we_candidates:
                mapped = raw_to_logical.get(raw_id)
                if mapped is not None and mapped in valid_logical_hits:
                    survived_raw += 1
            lag_filtered += len(we_candidates) - survived_raw

        # 2. FACT recheck
        for fid, score in fact_candidates:
            cur.execute(
                """
                SELECT
                    f.event_id::text,
                    f.predicate,
                    f.object_text,
                    f.status,
                    f.subject_identity_id::text,
                    i.display_name
                FROM public.facts f
                LEFT JOIN public.identities i
                    ON i.workspace_id = f.workspace_id
                   AND i.id = f.subject_identity_id
                WHERE f.workspace_id = %s::uuid
                  AND f.id = %s::uuid
                """,
                (workspace_id, fid),
            )
            frow = cur.fetchone()
            if frow is None:
                lag_filtered += 1
                continue
            event_id = str(frow[0])
            predicate = str(frow[1])
            object_text = str(frow[2])
            f_status = str(frow[3])
            subj_ident = str(frow[4]) if frow[4] is not None else None
            subj_name = str(frow[5]) if frow[5] is not None else ""

            if f_status not in ("SUPPORTED", "USER_CONFIRMED"):
                lag_filtered += 1
                continue

            if f_status == "SUPPORTED":
                cur.execute(
                    """
                    SELECT 1
                    FROM public.fact_support fs
                    INNER JOIN public.content_units cu
                        ON cu.workspace_id = fs.workspace_id
                       AND cu.id = fs.content_unit_id
                    INNER JOIN public.source_object_revisions rev
                        ON rev.workspace_id = cu.workspace_id
                       AND rev.id = cu.source_object_revision_id
                    WHERE fs.workspace_id = %s::uuid
                      AND fs.fact_id = %s::uuid
                      AND fs.status = 'ACTIVE'
                      AND fs.support_type = 'DIRECT'
                      AND cu.policy_state = 'ACTIVE'
                      AND NOT rev.is_tombstone
                    LIMIT 1
                    """,
                    (workspace_id, fid),
                )
                if cur.fetchone() is None:
                    lag_filtered += 1
                    continue

            if subj_ident is not None:
                elig = evaluate_identity_attribution(cur, workspace_id, subj_ident)
                if (
                    elig.identity_status is IdentityStatus.REVOKED
                    or elig.active_conflicted_mapping_count > 0
                    or elig.block_reason_code
                    in (
                        IdentityAttributionBlockReason.IDENTITY_CONFLICTED,
                        IdentityAttributionBlockReason.IDENTITY_REVOKED,
                    )
                ):
                    lag_filtered += 1
                    continue

            linked_res = _get_fact_linked_resources(
                cur, workspace_id=workspace_id, fact_id=fid, event_id=event_id
            )
            ok_res, fenced = _resources_all_indexable(
                cur, workspace_id=workspace_id, resource_ids=linked_res
            )
            if fenced or not ok_res:
                lag_filtered += 1
                continue
            if (
                effective_source_resource_id is not None
                and effective_source_resource_id not in linked_res
            ):
                lag_filtered += 1
                continue

            summary_raw = (
                f"{subj_name} {predicate}: {object_text}"
                if subj_name.strip()
                else f"{predicate}: {object_text}"
            )
            hit_res = (
                effective_source_resource_id
                if effective_source_resource_id is not None
                else (linked_res[0] if len(linked_res) == 1 else None)
            )
            verified.append(
                SearchHit(
                    entity_type=SearchEntityType.FACT,
                    entity_id=fid,
                    source_resource_id=hit_res,
                    title_or_summary=_truncate_clean(summary_raw, MAX_TITLE_OR_SUMMARY_LEN),
                    score=max(score, 0.0),
                )
            )

        # 3. CLAIM recheck (live VerificationCalculator — stale/unverified claims NEVER leak!)
        for cid, score in claim_candidates:
            cur.execute(
                """
                SELECT event_id::text, claim_text, status
                FROM public.claims
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                """,
                (workspace_id, cid),
            )
            crow = cur.fetchone()
            if crow is None:
                lag_filtered += 1
                continue
            event_id = str(crow[0])
            claim_text = str(crow[1])
            c_status = str(crow[2])
            if c_status != ClaimStatus.APPROVED.value:
                lag_filtered += 1
                continue

            try:
                evaluation = self._verification.evaluate_claim(
                    cur, workspace_id=workspace_id, claim_id=cid
                )
            except LookupError:
                lag_filtered += 1
                continue

            if (
                evaluation.claim_status is not ClaimStatus.APPROVED
                or evaluation.computed_verification is ClaimVerification.UNVERIFIED
                or evaluation.atom_count == 0
                or evaluation.stale_atom_count > 0
                or evaluation.unsupported_atom_count > 0
                or evaluation.deletion_fenced
                or evaluation.policy_blocked
                or evaluation.has_identity_conflict
            ):
                lag_filtered += 1
                continue

            linked_res = _get_claim_linked_resources(
                cur, workspace_id=workspace_id, claim_id=cid, event_id=event_id
            )
            ok_res, fenced = _resources_all_indexable(
                cur, workspace_id=workspace_id, resource_ids=linked_res
            )
            if fenced or not ok_res:
                lag_filtered += 1
                continue
            if (
                effective_source_resource_id is not None
                and effective_source_resource_id not in linked_res
            ):
                lag_filtered += 1
                continue

            hit_res = (
                effective_source_resource_id
                if effective_source_resource_id is not None
                else (linked_res[0] if len(linked_res) == 1 else None)
            )
            verified.append(
                SearchHit(
                    entity_type=SearchEntityType.CLAIM,
                    entity_id=cid,
                    source_resource_id=hit_res,
                    title_or_summary=_truncate_clean(claim_text, MAX_TITLE_OR_SUMMARY_LEN),
                    score=max(score, 0.0),
                )
            )

        return verified, lag_filtered


__all__ = [
    "ConnectionFactory",
    "PostgresSearchService",
]
