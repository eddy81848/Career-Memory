"""PostgreSQL 18 deterministic Event Candidate Builder (M3: T-301 + T-301A).

Builds and idempotently enriches source-anchored ``PR`` and 1:1 ``DIRECT_COMMIT``
candidate ``work_events`` strictly from persisted Plan C metadata tables.

Core invariants:
- Gate: requires ``sync_runs.status = 'SUCCESS'``, ``import_ready = true``,
  ``is_abandoned = false``, persisted ``IMPORT_READY`` outbox event, zero
  ``UNKNOWN`` / missing default-branch commit PR associations, active
  ``PUBLIC`` / ``raw_storage_mode = 'NONE'`` resource, active ``source_account``,
  and zero active ``deletion_jobs`` fences (re-verified pre-commit!).
- ``EVENT-01``: 1 ``PR`` candidate per eligible merged PR targeting provider-reported
  ``default_branch`` within import window (zero automatic multi-PR merging).
- ``EVENT-02`` / ``EVENT-03``: provider-confirmed PR commits (``PR_COMMIT``),
  reviews (``PR_REVIEW``), and PR comments (``PR_COMMENT``) attach strictly as
  ``MUST_LINK`` with ``origin = 'SOURCE'``.
- ``EVENT-04``: referenced issues and shared ticket/label context clues attach
  strictly as ``CONTEXT_ONLY`` (``ISSUE_REFERENCE``), never ``MUST_LINK`` and
  never standalone events or auto-merges.
- ``EVENT-05``: active ``CANNOT_LINK`` rows on ``(event_id, source_object_id)``
  always win and are never overwritten or bypassed by automatic candidate building.
- ``EVENT-07``: NEVER auto-creates ``PR_SEGMENT`` or ``USER_COMPOSITE`` events.
- ``EVENT-10`` / ``SYNC-20`` / ``T-301A``: 1:1 ``DIRECT_COMMIT`` fallback for each
  ``NO_MATCH_OBSERVED`` commit verified reachable on provider-reported
  ``default_branch`` (non-default branches, in-window/out-of-window PR commits,
  squash/rebase/merge SHAs strictly excluded; zero automatic commit clustering).
- User protection: never overwrites existing ``CONFIRMED``, ``REJECTED``,
  ``SUPERSEDED``, ``version > 1``, or ``created_by = 'USER'`` work_events.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from career_memory_api.event_candidates.flags import ReviewFlagInputs, evaluate_review_flags
from career_memory_api.event_candidates.models import (
    DIRECT_COMMIT_ASSOCIATION_NOTE,
    DIRECT_COMMIT_BADGE_CODE,
    BuiltCandidateSummary,
    BuiltMembershipSummary,
    CandidateBuildErrorCode,
    CandidateBuildRequest,
    CandidateBuildResult,
    CandidateBuildStatus,
    EventRelationType,
    ReviewFlagCode,
)
from career_memory_api.jobs.postgres_runtime import (
    bind_worker_workspace_context,
    is_deletion_fence_active,
)
from career_memory_api.schema_contracts.enums import (
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    RecordOrigin,
    ResourceVisibility,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.sync_runs.models import SyncRunStatus

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
BeforeCommitHook = Callable[[psycopg.Cursor, str, str], None]

_PLACEHOLDER_PR_TITLE_RE = re.compile(r"^PR\s+#?\S+$")
_PLACEHOLDER_DC_TITLE_RE = re.compile(r"^Direct commit [0-9a-f]{7,40} on .+$")
_PROTECTED_EVENT_STATUSES: frozenset[str] = frozenset(
    {
        WorkEventStatus.CONFIRMED.value,
        WorkEventStatus.REJECTED.value,
        WorkEventStatus.SUPERSEDED.value,
    }
)


def _normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID.") from exc


def _parse_iso_ts(value: Any) -> datetime | None:
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


def _format_utc_iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    utc_ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return utc_ts.isoformat().replace("+00:00", "Z")


def _extract_first_line_title(raw: Any, *, max_len: int = 200) -> str | None:
    """Extract strictly a single-line non-raw title (never persists multi-line bodies)."""
    if not isinstance(raw, str):
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:max_len]
    return None


def _is_placeholder_title(title: str) -> bool:
    cleaned = title.strip()
    return bool(
        _PLACEHOLDER_PR_TITLE_RE.match(cleaned) or _PLACEHOLDER_DC_TITLE_RE.match(cleaned)
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return []


def _norm_sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned if cleaned else None


def _abbreviate_sha(sha: str) -> str:
    cleaned = sha.strip().lower()
    if len(cleaned) <= 12:
        return cleaned
    return cleaned[:12]


def _token_present_in_text(token: str, text: str) -> bool:
    escaped = re.escape(token.strip())
    if not escaped:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", text, re.IGNORECASE))


@dataclass(frozen=True, slots=True)
class _SourceObjectSnapshot:
    source_object_id: str
    object_type: SourceObjectType
    provider_object_id: str
    lifecycle_state: str
    pr_association_state: PrAssociationState | None
    revision_id: str | None
    provider_updated_at: datetime | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CommitAssocRow:
    commit_source_object_id: str
    commit_sha: str
    default_branch_name: str
    pr_association_state: PrAssociationState
    matched_pr_provider_id: str | None
    matched_pr_in_window: bool | None
    match_mechanism: str | None
    unknown_reason_code: str | None
    reachable_at_start: bool
    reachable_at_end: bool
    lookup_completed: bool
    observation_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ItemOutcomeRow:
    source_object_id: str | None
    stream_name: str
    object_type: str
    provider_object_id: str
    outcome_kind: str
    reason_code: str
    outcome_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _AuthorAmbiguityIndex:
    ambiguous_external_ids: frozenset[str]
    ambiguous_usernames_lower: frozenset[str]

    def is_ambiguous(
        self,
        *,
        external_ids: set[str],
        usernames: set[str],
    ) -> bool:
        if external_ids & self.ambiguous_external_ids:
            return True
        lower_names = {u.strip().lower() for u in usernames if u and u.strip()}
        return bool(lower_names & self.ambiguous_usernames_lower)


class PostgresEventCandidateBuilder:
    """Deterministic PostgreSQL 18 Event Candidate Builder (`T-301` + `T-301A`).

    Operates strictly on persisted Plan C metadata of a completed ``SUCCESS`` /
    ``import_ready = true`` SyncRun. Safe to run standalone on seeded runs OR
    after ``PostgresBackfillPageCommitter.finalize_import_run`` to enrich PR
    reviews/comments, ``CONTEXT_ONLY`` issue links, ``DIRECT_COMMIT`` fallback
    metadata, and deterministic ``review_flags``.
    """

    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        clock: Callable[[], datetime] | None = None,
        before_commit_hook: BeforeCommitHook | None = None,
    ) -> None:
        self._worker_connection_factory = worker_connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._before_commit_hook = before_commit_hook

    def build_candidates(
        self,
        request: CandidateBuildRequest,
        *,
        now: datetime | None = None,
    ) -> CandidateBuildResult:
        """Build or enrich PR & DIRECT_COMMIT candidates from a ``CandidateBuildRequest``."""
        return self.build_candidates_for_sync_run(
            workspace_id=request.workspace_id,
            sync_run_id=request.sync_run_id,
            now=now,
        )

    def build_candidates_for_sync_run(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        now: datetime | None = None,
    ) -> CandidateBuildResult:
        """Atomically verify gates and build/enrich PR + DIRECT_COMMIT event candidates."""
        ts = now or self._clock()
        ws_uuid = _normalize_uuid(workspace_id, "workspace_id")
        run_uuid = _normalize_uuid(sync_run_id, "sync_run_id")

        with self._worker_connection_factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    bind_worker_workspace_context(cur, ws_uuid)

                    # 1. Lock sync_runs row FOR UPDATE
                    cur.execute(
                        """
                        SELECT
                            sr.source_resource_id::text,
                            sr.status,
                            sr.import_ready,
                            sr.is_abandoned,
                            sr.plan_spec
                        FROM public.sync_runs sr
                        WHERE sr.workspace_id = %s::uuid
                          AND sr.id = %s::uuid
                        FOR UPDATE
                        """,
                        (ws_uuid, run_uuid),
                    )
                    run_row = cur.fetchone()
                    if run_row is None:
                        raise ValueError(f"SyncRun {run_uuid} not found in workspace {ws_uuid}.")

                    resource_id = str(run_row[0])
                    run_status = str(run_row[1])
                    import_ready = bool(run_row[2])
                    is_abandoned = bool(run_row[3])
                    plan_spec = dict(run_row[4] or {})

                    # 2. Import-ready & IMPORT_READY outbox gate
                    cur.execute(
                        """
                        SELECT payload
                        FROM public.outbox_events
                        WHERE workspace_id = %s::uuid
                          AND aggregate_type = 'SYNC_RUN'
                          AND aggregate_id = %s::uuid
                          AND event_type = 'IMPORT_READY'
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (ws_uuid, run_uuid),
                    )
                    outbox_row = cur.fetchone()
                    if (
                        run_status != SyncRunStatus.SUCCESS.value
                        or not import_ready
                        or is_abandoned
                        or outbox_row is None
                    ):
                        return CandidateBuildResult(
                            status=CandidateBuildStatus.REJECTED,
                            workspace_id=ws_uuid,
                            sync_run_id=run_uuid,
                            source_resource_id=resource_id,
                            error_code=CandidateBuildErrorCode.SYNC_RUN_NOT_IMPORT_READY,
                            diagnostic_message=(
                                "SyncRun must be status='SUCCESS', import_ready=true, "
                                "not abandoned, with a persisted IMPORT_READY outbox row "
                                f"(found status={run_status}, import_ready={import_ready}, "
                                f"abandoned={is_abandoned}, outbox={outbox_row is not None})."
                            ),
                        )
                    outbox_payload = dict(outbox_row[0] or {})

                    # 3. Lock source_resources FOR UPDATE & verify policy + deletion fence (Start Gate)
                    policy_err = self._check_resource_and_account_policy(
                        cur, ws_uuid=ws_uuid, resource_id=resource_id, lock_resource=True
                    )
                    if policy_err is not None:
                        err_code, msg = policy_err
                        return CandidateBuildResult(
                            status=CandidateBuildStatus.BLOCKED,
                            workspace_id=ws_uuid,
                            sync_run_id=run_uuid,
                            source_resource_id=resource_id,
                            error_code=err_code,
                            diagnostic_message=msg,
                        )

                    # 4. Load persisted metadata snapshot for this run/resource
                    objects_by_id = self._load_active_source_objects(
                        cur, ws_uuid=ws_uuid, resource_id=resource_id
                    )
                    associations = self._load_commit_associations(
                        cur, ws_uuid=ws_uuid, run_uuid=run_uuid
                    )
                    outcomes = self._load_item_outcomes(
                        cur, ws_uuid=ws_uuid, run_uuid=run_uuid
                    )
                    prs_cursor_meta = self._load_prs_checkpoint_metadata(
                        cur, ws_uuid=ws_uuid, run_uuid=run_uuid
                    )
                    unit_paths_by_obj = self._load_content_unit_file_paths(
                        cur, ws_uuid=ws_uuid, resource_id=resource_id
                    )
                    ambiguity_index = self._load_author_ambiguity_index(
                        cur, ws_uuid=ws_uuid
                    )

                    # 5. Resolve provider-reported default_branch (never hardcoded!)
                    default_branch = self._resolve_default_branch(
                        plan_spec=plan_spec,
                        outbox_payload=outbox_payload,
                        associations=associations,
                        objects_by_id=objects_by_id,
                    )
                    if default_branch is None:
                        return CandidateBuildResult(
                            status=CandidateBuildStatus.REJECTED,
                            workspace_id=ws_uuid,
                            sync_run_id=run_uuid,
                            source_resource_id=resource_id,
                            error_code=CandidateBuildErrorCode.DEFAULT_BRANCH_UNRESOLVED,
                            diagnostic_message=(
                                "Provider-reported default_branch is missing from plan_spec, "
                                "IMPORT_READY payload, and commit observations."
                            ),
                        )

                    window_start = _parse_iso_ts(
                        plan_spec.get("backfill_from") or plan_spec.get("import_window_start")
                    )
                    window_end = _parse_iso_ts(
                        plan_spec.get("cutoff_at") or plan_spec.get("import_window_end")
                    )

                    # 6. PR association completeness gate (T-301A, EV-003, CAND-EV-020)
                    assoc_gap_msg = self._verify_pr_association_completeness(
                        default_branch=default_branch,
                        objects_by_id=objects_by_id,
                        associations=associations,
                        outcomes=outcomes,
                    )
                    if assoc_gap_msg is not None:
                        return CandidateBuildResult(
                            status=CandidateBuildStatus.REJECTED,
                            workspace_id=ws_uuid,
                            sync_run_id=run_uuid,
                            source_resource_id=resource_id,
                            default_branch=default_branch,
                            error_code=CandidateBuildErrorCode.PR_ASSOCIATION_INCOMPLETE,
                            diagnostic_message=assoc_gap_msg,
                        )

                    # 7. Enter savepoint before any write so pre-commit fence can roll back cleanly
                    cur.execute("SAVEPOINT sp_event_candidate_writes")

                    # Partition source objects by type
                    pr_objects = [
                        obj for obj in objects_by_id.values() if obj.object_type is SourceObjectType.PR
                    ]
                    commit_objects = [
                        obj
                        for obj in objects_by_id.values()
                        if obj.object_type is SourceObjectType.COMMIT
                    ]
                    review_objects = [
                        obj
                        for obj in objects_by_id.values()
                        if obj.object_type is SourceObjectType.REVIEW
                    ]
                    comment_objects = [
                        obj
                        for obj in objects_by_id.values()
                        if obj.object_type is SourceObjectType.COMMENT
                    ]
                    issue_objects = [
                        obj
                        for obj in objects_by_id.values()
                        if obj.object_type is SourceObjectType.ISSUE
                    ]

                    # Index commits by provider_object_id and normalized SHA
                    commits_by_pid: dict[str, _SourceObjectSnapshot] = {}
                    commits_by_sha: dict[str, _SourceObjectSnapshot] = {}
                    for c_obj in commit_objects:
                        commits_by_pid[c_obj.provider_object_id] = c_obj
                        for candidate_sha in (
                            _norm_sha(c_obj.provider_object_id),
                            _norm_sha(c_obj.metadata.get("head_sha")),
                            _norm_sha(c_obj.metadata.get("commit_sha")),
                            _norm_sha(c_obj.metadata.get("sha")),
                        ):
                            if candidate_sha:
                                commits_by_sha[candidate_sha] = c_obj

                    # Index issues by provider_object_id, provider_number, and external_key
                    issues_by_key: dict[str, _SourceObjectSnapshot] = {}
                    for iss in issue_objects:
                        issues_by_key[iss.provider_object_id.lower()] = iss
                        iss_num = _optional_int(iss.metadata.get("provider_number"))
                        if iss_num is not None:
                            issues_by_key[str(iss_num)] = iss
                        ext_key = iss.metadata.get("external_key")
                        if isinstance(ext_key, str) and ext_key.strip():
                            issues_by_key[ext_key.strip().lower()] = iss

                    # Determine eligible merged PRs targeting default_branch within import window
                    eligible_prs = sorted(
                        (
                            pr
                            for pr in pr_objects
                            if self._is_pr_eligible(
                                pr,
                                default_branch=default_branch,
                                window_start=window_start,
                                window_end=window_end,
                            )
                        ),
                        key=lambda p: (
                            _optional_int(p.metadata.get("provider_number")) or 10**12,
                            p.provider_object_id,
                            p.source_object_id,
                        ),
                    )

                    # Collect ALL PR-linked commit IDs / SHAs (including out-of-window PRs, squash/rebase/merge SHAs)
                    claimed_commit_obj_ids: set[str] = set()
                    claimed_commit_shas: set[str] = set()
                    assoc_by_commit_obj: dict[str, _CommitAssocRow] = {}
                    assoc_by_pr_key: dict[str, list[_CommitAssocRow]] = {}
                    out_of_window_commit_ids: set[str] = set()

                    for assoc in associations:
                        assoc_by_commit_obj[assoc.commit_source_object_id] = assoc
                        if assoc.pr_association_state is PrAssociationState.MATCHED_PR:
                            claimed_commit_obj_ids.add(assoc.commit_source_object_id)
                            claimed_commit_shas.add(assoc.commit_sha.lower())
                            if assoc.matched_pr_provider_id:
                                assoc_by_pr_key.setdefault(
                                    assoc.matched_pr_provider_id, []
                                ).append(assoc)
                            if assoc.matched_pr_in_window is False:
                                out_of_window_commit_ids.add(assoc.commit_source_object_id)

                    for outcome in outcomes:
                        if outcome.reason_code == "OUT_OF_WINDOW_PR_LINKED":
                            if outcome.source_object_id:
                                claimed_commit_obj_ids.add(outcome.source_object_id)
                                out_of_window_commit_ids.add(outcome.source_object_id)
                            o_sha = _norm_sha(outcome.outcome_metadata.get("commit_sha"))
                            if o_sha:
                                claimed_commit_shas.add(o_sha)

                    for pr in pr_objects:
                        for sha_key in ("head_sha", "merge_commit_sha", "squash_commit_sha"):
                            s = _norm_sha(pr.metadata.get(sha_key))
                            if s:
                                claimed_commit_shas.add(s)
                        for list_key in ("commit_shas", "rebase_commit_shas"):
                            for item in _as_str_list(pr.metadata.get(list_key)):
                                s = _norm_sha(item)
                                if s:
                                    claimed_commit_shas.add(s)
                        for cid in _as_str_list(pr.metadata.get("commit_ids")) + _as_str_list(
                            pr.metadata.get("associated_commit_ids")
                        ):
                            matched_c = commits_by_pid.get(cid) or commits_by_sha.get(cid.lower())
                            if matched_c is not None:
                                claimed_commit_obj_ids.add(matched_c.source_object_id)

                    for c_obj in commit_objects:
                        if c_obj.pr_association_state is PrAssociationState.MATCHED_PR:
                            claimed_commit_obj_ids.add(c_obj.source_object_id)
                            s = _norm_sha(c_obj.provider_object_id) or _norm_sha(
                                c_obj.metadata.get("head_sha")
                            )
                            if s:
                                claimed_commit_shas.add(s)

                    # Skipped outcomes for PARTIAL_SOURCE_SKIPPED evaluation
                    skipped_outcomes = [o for o in outcomes if o.outcome_kind == "SKIPPED"]

                    # Load existing promotions for quick authoritative lookup
                    existing_promotions = self._load_existing_promotions(cur, ws_uuid=ws_uuid)

                    built_candidates: list[BuiltCandidateSummary] = []
                    pr_event_ids: list[str] = []
                    direct_event_ids: list[str] = []
                    created_count = 0
                    enriched_count = 0
                    preserved_user_count = 0
                    memberships_inserted_total = 0
                    memberships_upgraded_total = 0
                    preserved_cannot_link_total = 0

                    # =============================================================
                    # 8. Build / Enrich PR Candidates (EVENT-01 .. EVENT-05, EVENT-07)
                    # =============================================================
                    for pr in eligible_prs:
                        pr_num = _optional_int(pr.metadata.get("provider_number")) or _optional_int(
                            pr.provider_object_id
                        )
                        pr_match_keys: set[str] = {pr.provider_object_id}
                        if pr_num is not None:
                            pr_match_keys.add(str(pr_num))

                        # 8a. Provider-confirmed PR commits (EVENT-02)
                        pr_commit_objs = self._resolve_pr_commits(
                            pr=pr,
                            pr_match_keys=pr_match_keys,
                            assoc_by_pr_key=assoc_by_pr_key,
                            commit_objects=commit_objects,
                            commits_by_pid=commits_by_pid,
                            commits_by_sha=commits_by_sha,
                            objects_by_id=objects_by_id,
                        )
                        for c_obj in pr_commit_objs:
                            claimed_commit_obj_ids.add(c_obj.source_object_id)
                            c_sha = _norm_sha(c_obj.metadata.get("head_sha")) or _norm_sha(
                                c_obj.provider_object_id
                            )
                            if c_sha:
                                claimed_commit_shas.add(c_sha)

                        # 8b. Provider-confirmed PR reviews & comments (EVENT-03)
                        pr_review_objs = self._resolve_pr_reviews(
                            pr_match_keys=pr_match_keys,
                            review_objects=review_objects,
                        )
                        pr_comment_objs = self._resolve_pr_comments(
                            pr_match_keys=pr_match_keys,
                            comment_objects=comment_objects,
                        )

                        # 8c. Referenced Issues / Tickets strictly as CONTEXT_ONLY (EVENT-04)
                        pr_title_from_meta = _extract_first_line_title(
                            pr.metadata.get("title")
                        )
                        fallback_pr_title = (
                            f"PR #{pr_num}" if pr_num is not None else f"PR {pr.provider_object_id}"
                        )
                        desired_title = pr_title_from_meta or fallback_pr_title
                        desired_is_placeholder = pr_title_from_meta is None

                        context_issues, issue_flag_keys = self._resolve_pr_context_issues(
                            pr=pr,
                            pr_num=pr_num,
                            pr_match_keys=pr_match_keys,
                            pr_title=desired_title,
                            eligible_pr_count=len(eligible_prs),
                            prs_cursor_meta=prs_cursor_meta,
                            issue_objects=issue_objects,
                            issues_by_key=issues_by_key,
                        )

                        # 8d. Compute deterministic review_flags
                        member_objs_all: list[_SourceObjectSnapshot] = [
                            pr,
                            *pr_commit_objs,
                            *pr_review_objs,
                            *pr_comment_objs,
                            *(iss for iss, _, _ in context_issues),
                        ]
                        pr_changed_paths = tuple(
                            dict.fromkeys(
                                _as_str_list(pr.metadata.get("changed_paths"))
                                + _as_str_list(pr.metadata.get("changed_path_prefixes"))
                                + list(unit_paths_by_obj.get(pr.source_object_id, ()))
                            )
                        )
                        commit_path_groups: list[tuple[str, ...]] = []
                        for c_obj in pr_commit_objs:
                            c_paths = tuple(
                                dict.fromkeys(
                                    _as_str_list(c_obj.metadata.get("changed_paths"))
                                    + _as_str_list(c_obj.metadata.get("changed_path_prefixes"))
                                    + list(unit_paths_by_obj.get(c_obj.source_object_id, ()))
                                )
                            )
                            if c_paths:
                                commit_path_groups.append(c_paths)

                        top_level_dirs = tuple(
                            dict.fromkeys(
                                _as_str_list(pr.metadata.get("top_level_dirs"))
                                + _as_str_list(pr.metadata.get("components"))
                            )
                        )
                        labels = tuple(_as_str_list(pr.metadata.get("labels")))

                        has_skipped = self._has_skipped_outcome_for_event(
                            skipped_outcomes=skipped_outcomes,
                            member_objects=member_objs_all,
                            pr_num=pr_num,
                            pr_pid=pr.provider_object_id,
                            commit_shas={
                                s
                                for c_obj in pr_commit_objs
                                for s in (
                                    _norm_sha(c_obj.provider_object_id),
                                    _norm_sha(c_obj.metadata.get("head_sha")),
                                    _norm_sha(c_obj.metadata.get("sha")),
                                )
                                if s
                            },
                        )
                        has_ambiguous_author = self._has_ambiguous_author(
                            ambiguity_index=ambiguity_index,
                            objects=[pr, *pr_commit_objs],
                        )

                        # Inspect existing event (if any) to respect user title/decisions
                        existing_ev = self._lock_existing_event(
                            cur,
                            ws_uuid=ws_uuid,
                            primary_anchor_source_object_id=pr.source_object_id,
                            anchor_mode=WorkEventAnchorMode.PR,
                            promoted_event_id=existing_promotions.get(
                                (pr.source_object_id, WorkEventAnchorMode.PR.value)
                            ),
                        )
                        effective_title = desired_title
                        effective_is_placeholder = desired_is_placeholder
                        if existing_ev is not None:
                            ex_title = str(existing_ev["title"])
                            if not _is_placeholder_title(ex_title):
                                effective_title = ex_title
                                effective_is_placeholder = False
                            elif pr_title_from_meta is None:
                                effective_title = ex_title
                                effective_is_placeholder = True

                        review_flags = evaluate_review_flags(
                            ReviewFlagInputs(
                                anchor_mode=WorkEventAnchorMode.PR,
                                title=effective_title,
                                is_placeholder_title=effective_is_placeholder,
                                changed_paths=pr_changed_paths,
                                commit_changed_path_groups=tuple(commit_path_groups),
                                top_level_dirs=top_level_dirs,
                                labels=labels,
                                linked_issue_keys=tuple(sorted(issue_flag_keys)),
                                has_partial_source_skipped=has_skipped,
                                has_ambiguous_author_identity=has_ambiguous_author,
                                explicit_mixed_purpose_hint=bool(
                                    pr.metadata.get("mixed_purpose")
                                    or pr.metadata.get("multiple_components")
                                ),
                            )
                        )

                        # Build Plan C non-raw summary_metadata
                        pr_summary_meta: dict[str, Any] = {
                            "anchor_mode": WorkEventAnchorMode.PR.value,
                            "default_branch": default_branch,
                            "provider_object_id": pr.provider_object_id,
                            "review_flags": [f.value for f in review_flags],
                        }
                        if pr_num is not None:
                            pr_summary_meta["pr_number"] = pr_num
                        for safe_key in (
                            "base_ref",
                            "head_ref",
                            "head_sha",
                            "merge_commit_sha",
                            "merge_strategy",
                            "author_login",
                            "author_external_id",
                            "changed_files_count",
                            "commit_count",
                        ):
                            val = pr.metadata.get(safe_key)
                            if val is not None:
                                pr_summary_meta[safe_key] = val
                        if labels:
                            pr_summary_meta["labels"] = sorted(set(labels))
                        if issue_flag_keys:
                            pr_summary_meta["linked_issue_count"] = len(issue_flag_keys)
                        assert_no_forbidden_raw_keys(
                            pr_summary_meta, field_name="work_events.summary_metadata"
                        )

                        # Occurred timestamps (guaranteed end >= start)
                        occurred_start_at, occurred_end_at = self._compute_pr_time_bounds(
                            pr=pr, pr_commits=pr_commit_objs
                        )

                        # Desired memberships for this PR event
                        desired_memberships: list[
                            tuple[
                                _SourceObjectSnapshot,
                                EventMembershipRelationClass,
                                EventRelationType,
                                EventMembershipOrigin,
                                str,
                            ]
                        ] = [
                            (
                                pr,
                                EventMembershipRelationClass.MUST_LINK,
                                EventRelationType.PRIMARY_ANCHOR,
                                EventMembershipOrigin.SOURCE,
                                "PR_ANCHOR",
                            )
                        ]
                        for c_obj in pr_commit_objs:
                            desired_memberships.append(
                                (
                                    c_obj,
                                    EventMembershipRelationClass.MUST_LINK,
                                    EventRelationType.PR_COMMIT,
                                    EventMembershipOrigin.SOURCE,
                                    "PROVIDER_CONFIRMED_PR_COMMIT",
                                )
                            )
                        for r_obj in pr_review_objs:
                            rationale = (
                                "PROVIDER_CONFIRMED_PR_REVIEW"
                                if r_obj.metadata.get("state") is not None
                                else "PROVIDER_CONFIRMED_PR_REVIEW_COMMENT"
                            )
                            desired_memberships.append(
                                (
                                    r_obj,
                                    EventMembershipRelationClass.MUST_LINK,
                                    EventRelationType.PR_REVIEW,
                                    EventMembershipOrigin.SOURCE,
                                    rationale,
                                )
                            )
                        for cm_obj in pr_comment_objs:
                            desired_memberships.append(
                                (
                                    cm_obj,
                                    EventMembershipRelationClass.MUST_LINK,
                                    EventRelationType.PR_COMMENT,
                                    EventMembershipOrigin.SOURCE,
                                    "PROVIDER_CONFIRMED_PR_COMMENT",
                                )
                            )
                        for iss_obj, iss_origin, iss_rationale in context_issues:
                            desired_memberships.append(
                                (
                                    iss_obj,
                                    EventMembershipRelationClass.CONTEXT_ONLY,
                                    EventRelationType.ISSUE_REFERENCE,
                                    iss_origin,
                                    iss_rationale,
                                )
                            )

                        summary, m_ins, m_upg, cl_blocked = self._upsert_candidate_event(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            existing_ev=existing_ev,
                            primary_anchor=pr,
                            anchor_mode=WorkEventAnchorMode.PR,
                            title=effective_title,
                            summary_metadata=pr_summary_meta,
                            occurred_start_at=occurred_start_at,
                            occurred_end_at=occurred_end_at,
                            review_flags=review_flags,
                            desired_memberships=desired_memberships,
                            ts=ts,
                        )
                        built_candidates.append(summary)
                        pr_event_ids.append(summary.event_id)
                        if summary.created:
                            created_count += 1
                        elif summary.enriched:
                            enriched_count += 1
                        elif summary.preserved_user_decision:
                            preserved_user_count += 1
                        memberships_inserted_total += m_ins
                        memberships_upgraded_total += m_upg
                        preserved_cannot_link_total += cl_blocked

                    # =============================================================
                    # 9. Build / Enrich 1:1 DIRECT_COMMIT Fallback Candidates
                    #    (T-301A, EVENT-10, SYNC-20, DELTA-EV-002, EV-004, CAND-EV-019)
                    # =============================================================
                    excluded_non_default_count = 0
                    for c_obj in commit_objects:
                        if c_obj.source_object_id in claimed_commit_obj_ids:
                            continue
                        if self._is_explicitly_non_default_commit(
                            c_obj, default_branch=default_branch
                        ):
                            excluded_non_default_count += 1

                    eligible_direct_assocs = sorted(
                        (
                            assoc
                            for assoc in associations
                            if self._is_direct_commit_eligible(
                                assoc=assoc,
                                default_branch=default_branch,
                                objects_by_id=objects_by_id,
                                claimed_commit_obj_ids=claimed_commit_obj_ids,
                                claimed_commit_shas=claimed_commit_shas,
                            )
                        ),
                        key=lambda a: (a.commit_sha, a.commit_source_object_id),
                    )

                    for assoc in eligible_direct_assocs:
                        c_obj = objects_by_id[assoc.commit_source_object_id]
                        commit_sha = assoc.commit_sha.strip().lower()
                        abbrev_sha = _abbreviate_sha(commit_sha)

                        c_title_from_meta = _extract_first_line_title(
                            c_obj.metadata.get("title") or c_obj.metadata.get("subject")
                        )
                        fallback_dc_title = f"Direct commit {abbrev_sha} on {default_branch}"
                        desired_title = c_title_from_meta or fallback_dc_title
                        desired_is_placeholder = c_title_from_meta is None

                        existing_ev = self._lock_existing_event(
                            cur,
                            ws_uuid=ws_uuid,
                            primary_anchor_source_object_id=c_obj.source_object_id,
                            anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
                            promoted_event_id=existing_promotions.get(
                                (c_obj.source_object_id, WorkEventAnchorMode.DIRECT_COMMIT.value)
                            ),
                        )
                        effective_title = desired_title
                        effective_is_placeholder = desired_is_placeholder
                        if existing_ev is not None:
                            ex_title = str(existing_ev["title"])
                            if not _is_placeholder_title(ex_title):
                                effective_title = ex_title
                                effective_is_placeholder = False
                            elif c_title_from_meta is None:
                                effective_title = ex_title
                                effective_is_placeholder = True

                        parent_count = self._resolve_commit_parent_count(c_obj, assoc)
                        committed_at = (
                            _parse_iso_ts(c_obj.metadata.get("committed_at"))
                            or _parse_iso_ts(c_obj.metadata.get("updated_at"))
                            or c_obj.provider_updated_at
                            or _parse_iso_ts(c_obj.metadata.get("created_at"))
                        )
                        has_skipped = self._has_skipped_outcome_for_event(
                            skipped_outcomes=skipped_outcomes,
                            member_objects=[c_obj],
                            pr_num=None,
                            pr_pid=None,
                            commit_shas={commit_sha},
                        )
                        has_ambiguous_author = self._has_ambiguous_author(
                            ambiguity_index=ambiguity_index,
                            objects=[c_obj],
                        )

                        review_flags = evaluate_review_flags(
                            ReviewFlagInputs(
                                anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
                                title=effective_title,
                                is_placeholder_title=effective_is_placeholder,
                                has_partial_source_skipped=has_skipped,
                                has_ambiguous_author_identity=has_ambiguous_author,
                                parent_count=parent_count,
                                pr_association_state=PrAssociationState.NO_MATCH_OBSERVED,
                            )
                        )

                        dc_summary_meta: dict[str, Any] = {
                            "anchor_mode": WorkEventAnchorMode.DIRECT_COMMIT.value,
                            "default_branch": default_branch,
                            "commit_sha": commit_sha,
                            "abbreviated_sha": abbrev_sha,
                            "default_branch_reachable": True,
                            "pr_association_state": PrAssociationState.NO_MATCH_OBSERVED.value,
                            "no_match_observed_means_definitely_no_pr": False,
                            "requires_user_confirmation": True,
                            "user_review_required": True,
                            "badge_code": DIRECT_COMMIT_BADGE_CODE,
                            "association_note": DIRECT_COMMIT_ASSOCIATION_NOTE,
                            "parent_count": parent_count,
                            "review_flags": [f.value for f in review_flags],
                        }
                        committed_iso = _format_utc_iso(committed_at)
                        if committed_iso is not None:
                            dc_summary_meta["committed_at"] = committed_iso
                        for safe_key in ("author_login", "author_external_id", "committer_external_id"):
                            val = c_obj.metadata.get(safe_key)
                            if val is not None:
                                dc_summary_meta[safe_key] = val
                        assert_no_forbidden_raw_keys(
                            dc_summary_meta, field_name="work_events.summary_metadata"
                        )

                        desired_memberships = [
                            (
                                c_obj,
                                EventMembershipRelationClass.MUST_LINK,
                                EventRelationType.PRIMARY_ANCHOR,
                                EventMembershipOrigin.SOURCE,
                                "DIRECT_COMMIT_ANCHOR",
                            )
                        ]

                        summary, m_ins, m_upg, cl_blocked = self._upsert_candidate_event(
                            cur,
                            ws_uuid=ws_uuid,
                            run_uuid=run_uuid,
                            existing_ev=existing_ev,
                            primary_anchor=c_obj,
                            anchor_mode=WorkEventAnchorMode.DIRECT_COMMIT,
                            title=effective_title,
                            summary_metadata=dc_summary_meta,
                            occurred_start_at=committed_at,
                            occurred_end_at=committed_at,
                            review_flags=review_flags,
                            desired_memberships=desired_memberships,
                            ts=ts,
                        )
                        built_candidates.append(summary)
                        direct_event_ids.append(summary.event_id)
                        if summary.created:
                            created_count += 1
                        elif summary.enriched:
                            enriched_count += 1
                        elif summary.preserved_user_decision:
                            preserved_user_count += 1
                        memberships_inserted_total += m_ins
                        memberships_upgraded_total += m_upg
                        preserved_cannot_link_total += cl_blocked

                    # 10. Pre-commit hook & Pre-Commit Policy + Deletion Fence Re-Verification!
                    if self._before_commit_hook is not None:
                        self._before_commit_hook(cur, ws_uuid, resource_id)

                    post_policy_err = self._check_resource_and_account_policy(
                        cur, ws_uuid=ws_uuid, resource_id=resource_id, lock_resource=False
                    )
                    if post_policy_err is not None:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_event_candidate_writes")
                        cur.execute("RELEASE SAVEPOINT sp_event_candidate_writes")
                        err_code, msg = post_policy_err
                        return CandidateBuildResult(
                            status=CandidateBuildStatus.BLOCKED,
                            workspace_id=ws_uuid,
                            sync_run_id=run_uuid,
                            source_resource_id=resource_id,
                            default_branch=default_branch,
                            error_code=err_code,
                            diagnostic_message=f"Pre-commit re-check blocked promotion: {msg}",
                        )

                    cur.execute("RELEASE SAVEPOINT sp_event_candidate_writes")

        return CandidateBuildResult(
            status=CandidateBuildStatus.SUCCEEDED,
            workspace_id=ws_uuid,
            sync_run_id=run_uuid,
            source_resource_id=resource_id,
            default_branch=default_branch,
            candidates=tuple(built_candidates),
            pr_event_ids=tuple(sorted(pr_event_ids)),
            direct_commit_event_ids=tuple(sorted(direct_event_ids)),
            created_event_count=created_count,
            enriched_event_count=enriched_count,
            preserved_user_event_count=preserved_user_count,
            memberships_inserted_count=memberships_inserted_total,
            memberships_upgraded_count=memberships_upgraded_total,
            preserved_cannot_link_count=preserved_cannot_link_total,
            excluded_non_default_commit_count=excluded_non_default_count,
            excluded_out_of_window_pr_commit_count=len(out_of_window_commit_ids),
        )

    # =========================================================================
    # Gate & Metadata Loaders
    # =========================================================================

    @staticmethod
    def _check_resource_and_account_policy(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        resource_id: str,
        lock_resource: bool,
    ) -> tuple[CandidateBuildErrorCode, str] | None:
        # Use FOR KEY SHARE (not FOR UPDATE) so concurrent deletion_jobs FK
        # inserts on source_resources are never blocked, allowing the pre-commit
        # deletion-fence re-check to observe newly committed fences immediately.
        lock_clause = "FOR KEY SHARE OF res" if lock_resource else ""
        cur.execute(
            f"""
            SELECT
                res.status,
                res.visibility,
                res.raw_storage_mode,
                sa.status
            FROM public.source_resources res
            JOIN public.source_accounts sa
              ON sa.workspace_id = res.workspace_id
             AND sa.id = res.source_account_id
            WHERE res.workspace_id = %s::uuid
              AND res.id = %s::uuid
            {lock_clause}
            """,
            (ws_uuid, resource_id),
        )
        row = cur.fetchone()
        if row is None:
            return (
                CandidateBuildErrorCode.DELETION_FENCE_ACTIVE,
                f"SourceResource {resource_id} not found or deleted.",
            )
        res_status, res_visibility, raw_storage_mode, acct_status = (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
        )
        if res_status == "DELETED" or is_deletion_fence_active(cur, ws_uuid, resource_id):
            return (
                CandidateBuildErrorCode.DELETION_FENCE_ACTIVE,
                "Active deletion fence or deleted source_resource blocks candidate building.",
            )
        if acct_status != "ACTIVE":
            return (
                CandidateBuildErrorCode.SOURCE_ACCOUNT_REVOKED,
                f"SourceAccount status is {acct_status!r}; candidate building blocked.",
            )
        if (
            res_status != "ACTIVE"
            or res_visibility != ResourceVisibility.PUBLIC.value
            or raw_storage_mode != "NONE"
        ):
            return (
                CandidateBuildErrorCode.POLICY_BLOCKED,
                (
                    f"SourceResource policy denied (status={res_status}, "
                    f"visibility={res_visibility}, raw_storage_mode={raw_storage_mode})."
                ),
            )
        return None

    @staticmethod
    def _load_active_source_objects(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        resource_id: str,
    ) -> dict[str, _SourceObjectSnapshot]:
        cur.execute(
            """
            SELECT DISTINCT ON (so.id)
                so.id::text,
                so.object_type,
                so.provider_object_id,
                so.lifecycle_state,
                so.pr_association_state,
                rev.id::text,
                rev.provider_updated_at,
                rev.normalized_metadata
            FROM public.source_objects so
            LEFT JOIN public.source_object_revisions rev
              ON rev.workspace_id = so.workspace_id
             AND rev.source_object_id = so.id
             AND rev.is_tombstone = false
            WHERE so.workspace_id = %s::uuid
              AND so.source_resource_id = %s::uuid
              AND so.lifecycle_state = 'ACTIVE'
            ORDER BY so.id, rev.observed_at DESC NULLS LAST, rev.id DESC NULLS LAST
            """,
            (ws_uuid, resource_id),
        )
        out: dict[str, _SourceObjectSnapshot] = {}
        for row in cur.fetchall():
            obj_id = str(row[0])
            assoc_raw = str(row[4]) if row[4] is not None else None
            out[obj_id] = _SourceObjectSnapshot(
                source_object_id=obj_id,
                object_type=SourceObjectType(str(row[1])),
                provider_object_id=str(row[2]),
                lifecycle_state=str(row[3]),
                pr_association_state=PrAssociationState(assoc_raw) if assoc_raw else None,
                revision_id=str(row[5]) if row[5] is not None else None,
                provider_updated_at=row[6],
                metadata=dict(row[7] or {}),
            )
        return out

    @staticmethod
    def _load_commit_associations(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
    ) -> list[_CommitAssocRow]:
        cur.execute(
            """
            SELECT
                commit_source_object_id::text,
                commit_sha,
                default_branch_name,
                pr_association_state,
                matched_pr_provider_id,
                matched_pr_in_window,
                match_mechanism,
                unknown_reason_code,
                reachable_at_start,
                reachable_at_end,
                lookup_completed,
                observation_metadata
            FROM public.sync_commit_pr_associations
            WHERE workspace_id = %s::uuid
              AND sync_run_id = %s::uuid
            ORDER BY commit_sha ASC, commit_source_object_id ASC
            """,
            (ws_uuid, run_uuid),
        )
        return [
            _CommitAssocRow(
                commit_source_object_id=str(r[0]),
                commit_sha=str(r[1]).strip().lower(),
                default_branch_name=str(r[2]).strip(),
                pr_association_state=PrAssociationState(str(r[3])),
                matched_pr_provider_id=str(r[4]) if r[4] is not None else None,
                matched_pr_in_window=bool(r[5]) if r[5] is not None else None,
                match_mechanism=str(r[6]) if r[6] is not None else None,
                unknown_reason_code=str(r[7]) if r[7] is not None else None,
                reachable_at_start=bool(r[8]),
                reachable_at_end=bool(r[9]),
                lookup_completed=bool(r[10]),
                observation_metadata=dict(r[11] or {}),
            )
            for r in cur.fetchall()
        ]

    @staticmethod
    def _load_item_outcomes(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
    ) -> list[_ItemOutcomeRow]:
        cur.execute(
            """
            SELECT
                source_object_id::text,
                stream_name,
                object_type,
                provider_object_id,
                outcome_kind,
                reason_code,
                outcome_metadata
            FROM public.sync_item_outcomes
            WHERE workspace_id = %s::uuid
              AND sync_run_id = %s::uuid
            """,
            (ws_uuid, run_uuid),
        )
        return [
            _ItemOutcomeRow(
                source_object_id=str(r[0]) if r[0] is not None else None,
                stream_name=str(r[1]),
                object_type=str(r[2]),
                provider_object_id=str(r[3]),
                outcome_kind=str(r[4]),
                reason_code=str(r[5]),
                outcome_metadata=dict(r[6] or {}),
            )
            for r in cur.fetchall()
        ]

    @staticmethod
    def _load_prs_checkpoint_metadata(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
    ) -> dict[str, Any]:
        cur.execute(
            """
            SELECT cursor_metadata
            FROM public.sync_checkpoints
            WHERE workspace_id = %s::uuid
              AND sync_run_id = %s::uuid
              AND stream_name = 'PRS'
            """,
            (ws_uuid, run_uuid),
        )
        row = cur.fetchone()
        return dict(row[0] or {}) if row is not None else {}

    @staticmethod
    def _load_content_unit_file_paths(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        resource_id: str,
    ) -> dict[str, tuple[str, ...]]:
        cur.execute(
            """
            SELECT DISTINCT
                so.id::text,
                cu.anchor ->> 'file_path'
            FROM public.content_units cu
            JOIN public.source_object_revisions rev
              ON rev.workspace_id = cu.workspace_id
             AND rev.id = cu.source_object_revision_id
            JOIN public.source_objects so
              ON so.workspace_id = rev.workspace_id
             AND so.id = rev.source_object_id
            WHERE cu.workspace_id = %s::uuid
              AND so.source_resource_id = %s::uuid
              AND cu.policy_state = 'ACTIVE'
              AND rev.is_tombstone = false
              AND cu.anchor ? 'file_path'
            """,
            (ws_uuid, resource_id),
        )
        grouped: dict[str, list[str]] = {}
        for obj_id, file_path in cur.fetchall():
            if isinstance(file_path, str) and file_path.strip():
                grouped.setdefault(str(obj_id), []).append(file_path.strip())
        return {k: tuple(sorted(set(v))) for k, v in grouped.items()}

    @staticmethod
    def _load_author_ambiguity_index(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
    ) -> _AuthorAmbiguityIndex:
        cur.execute(
            """
            SELECT
                eim.identity_id::text,
                eim.external_user_id,
                eim.username,
                eim.status,
                i.status
            FROM public.external_identity_mappings eim
            JOIN public.identities i
              ON i.workspace_id = eim.workspace_id
             AND i.id = eim.identity_id
            WHERE eim.workspace_id = %s::uuid
              AND eim.status <> 'REVOKED'
            """,
            (ws_uuid,),
        )
        ambig_ext_ids: set[str] = set()
        ambig_usernames: set[str] = set()
        ext_id_to_identities: dict[str, set[str]] = {}
        uname_to_identities: dict[str, set[str]] = {}

        for identity_id, ext_id, username, map_status, id_status in cur.fetchall():
            is_bad = str(map_status) in ("CONFLICTED", "HEURISTIC") or str(id_status) == "AMBIGUOUS"
            if ext_id is not None and str(ext_id).strip():
                ext_clean = str(ext_id).strip()
                ext_id_to_identities.setdefault(ext_clean, set()).add(str(identity_id))
                if is_bad:
                    ambig_ext_ids.add(ext_clean)
            if username is not None and str(username).strip():
                u_clean = str(username).strip().lower()
                uname_to_identities.setdefault(u_clean, set()).add(str(identity_id))
                if is_bad:
                    ambig_usernames.add(u_clean)

        for ext_clean, id_set in ext_id_to_identities.items():
            if len(id_set) >= 2:
                ambig_ext_ids.add(ext_clean)
        for u_clean, id_set in uname_to_identities.items():
            if len(id_set) >= 2:
                ambig_usernames.add(u_clean)

        return _AuthorAmbiguityIndex(
            ambiguous_external_ids=frozenset(ambig_ext_ids),
            ambiguous_usernames_lower=frozenset(ambig_usernames),
        )

    @staticmethod
    def _load_existing_promotions(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
    ) -> dict[tuple[str, str], str]:
        cur.execute(
            """
            SELECT source_object_id::text, anchor_mode, work_event_id::text
            FROM public.sync_run_event_promotions
            WHERE workspace_id = %s::uuid
            """,
            (ws_uuid,),
        )
        return {(str(r[0]), str(r[1])): str(r[2]) for r in cur.fetchall()}

    # =========================================================================
    # Domain Classification & Association Verification Helpers
    # =========================================================================

    @staticmethod
    def _resolve_default_branch(
        *,
        plan_spec: Mapping[str, Any],
        outbox_payload: Mapping[str, Any],
        associations: list[_CommitAssocRow],
        objects_by_id: Mapping[str, _SourceObjectSnapshot],
    ) -> str | None:
        for source in (plan_spec, outbox_payload):
            raw = source.get("default_branch")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()

        assoc_branches = {a.default_branch_name for a in associations if a.default_branch_name}
        if len(assoc_branches) == 1:
            return next(iter(assoc_branches))

        pr_base_refs = {
            str(obj.metadata["base_ref"]).strip()
            for obj in objects_by_id.values()
            if obj.object_type is SourceObjectType.PR
            and isinstance(obj.metadata.get("base_ref"), str)
            and obj.metadata["base_ref"].strip()
        }
        if len(pr_base_refs) == 1:
            return next(iter(pr_base_refs))
        return None

    @classmethod
    def _is_explicitly_non_default_commit(
        cls,
        c_obj: _SourceObjectSnapshot,
        *,
        default_branch: str,
    ) -> bool:
        if c_obj.metadata.get("on_default_branch") is False:
            return True
        branch_name = c_obj.metadata.get("branch_name")
        if isinstance(branch_name, str) and branch_name.strip() and branch_name.strip() != default_branch:
            return True
        return False

    @classmethod
    def _verify_pr_association_completeness(
        cls,
        *,
        default_branch: str,
        objects_by_id: Mapping[str, _SourceObjectSnapshot],
        associations: list[_CommitAssocRow],
        outcomes: list[_ItemOutcomeRow],
    ) -> str | None:
        assoc_by_obj: dict[str, _CommitAssocRow] = {}
        for assoc in associations:
            assoc_by_obj[assoc.commit_source_object_id] = assoc
            if assoc.pr_association_state is PrAssociationState.UNKNOWN:
                return (
                    f"Commit {assoc.commit_sha} remains in UNKNOWN PR association state "
                    f"(reason={assoc.unknown_reason_code})."
                )
            if (
                assoc.pr_association_state is PrAssociationState.NO_MATCH_OBSERVED
                and not assoc.lookup_completed
            ):
                return f"Commit {assoc.commit_sha} has incomplete PR association lookup."

        # Check unresolved ASSOCIATION_UNKNOWN item outcomes
        for outcome in outcomes:
            if outcome.outcome_kind == "ASSOCIATION_UNKNOWN":
                resolved = (
                    outcome.source_object_id is not None
                    and outcome.source_object_id in assoc_by_obj
                    and assoc_by_obj[outcome.source_object_id].pr_association_state
                    in (PrAssociationState.MATCHED_PR, PrAssociationState.NO_MATCH_OBSERVED)
                )
                if not resolved:
                    return (
                        f"Unresolved ASSOCIATION_UNKNOWN outcome for "
                        f"{outcome.provider_object_id!r} (reason={outcome.reason_code})."
                    )

        # Check every active COMMIT source_object for UNKNOWN or missing default-branch observation
        for obj in objects_by_id.values():
            if obj.object_type is not SourceObjectType.COMMIT:
                continue
            if obj.pr_association_state is PrAssociationState.UNKNOWN:
                return (
                    f"Commit source_object {obj.provider_object_id!r} has "
                    "pr_association_state='UNKNOWN'."
                )
            if cls._is_explicitly_non_default_commit(obj, default_branch=default_branch):
                continue
            # Default-branch commit not explicitly matched to a PR MUST have a completeness ledger row
            if (
                obj.source_object_id not in assoc_by_obj
                and obj.pr_association_state is not PrAssociationState.MATCHED_PR
            ):
                return (
                    f"Default-branch commit {obj.provider_object_id!r} is missing a "
                    "sync_commit_pr_associations observation for this sync_run."
                )

        return None

    @staticmethod
    def _is_pr_eligible(
        pr: _SourceObjectSnapshot,
        *,
        default_branch: str,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> bool:
        meta = pr.metadata
        is_merged = (
            meta.get("merged") is True
            or meta.get("merged_at") is not None
            or str(meta.get("state") or "").strip().upper() == "MERGED"
        )
        if not is_merged:
            return False

        base_ref = meta.get("base_ref") or meta.get("base_branch")
        if isinstance(base_ref, str) and base_ref.strip() and base_ref.strip() != default_branch:
            return False

        if meta.get("within_import_window") is False:
            return False

        merged_at = _parse_iso_ts(meta.get("merged_at"))
        if merged_at is not None:
            if window_start is not None and merged_at < window_start:
                return False
            if window_end is not None and merged_at > window_end:
                return False
        return True

    @staticmethod
    def _resolve_pr_commits(
        *,
        pr: _SourceObjectSnapshot,
        pr_match_keys: set[str],
        assoc_by_pr_key: Mapping[str, list[_CommitAssocRow]],
        commit_objects: list[_SourceObjectSnapshot],
        commits_by_pid: Mapping[str, _SourceObjectSnapshot],
        commits_by_sha: Mapping[str, _SourceObjectSnapshot],
        objects_by_id: Mapping[str, _SourceObjectSnapshot],
    ) -> list[_SourceObjectSnapshot]:
        matched: dict[str, _SourceObjectSnapshot] = {}

        # 1. From sync_commit_pr_associations (MATCHED_PR)
        for key in pr_match_keys:
            for assoc in assoc_by_pr_key.get(key, ()):
                c_obj = objects_by_id.get(assoc.commit_source_object_id)
                if c_obj is not None and c_obj.object_type is SourceObjectType.COMMIT:
                    matched[c_obj.source_object_id] = c_obj

        # 2. From PR metadata authoritative SHAs / commit lists
        for sha_key in ("head_sha", "merge_commit_sha", "squash_commit_sha"):
            sha = _norm_sha(pr.metadata.get(sha_key))
            if sha and sha in commits_by_sha:
                c_obj = commits_by_sha[sha]
                matched[c_obj.source_object_id] = c_obj
        for list_key in ("commit_shas", "rebase_commit_shas"):
            for raw_sha in _as_str_list(pr.metadata.get(list_key)):
                sha = _norm_sha(raw_sha)
                if sha and sha in commits_by_sha:
                    c_obj = commits_by_sha[sha]
                    matched[c_obj.source_object_id] = c_obj
        for list_key in ("commit_ids", "associated_commit_ids"):
            for cid in _as_str_list(pr.metadata.get(list_key)):
                c_obj = commits_by_pid.get(cid) or commits_by_sha.get(cid.lower())
                if c_obj is not None:
                    matched[c_obj.source_object_id] = c_obj

        # 3. From COMMIT metadata ONLY when provider confirmed MATCHED_PR
        for c_obj in commit_objects:
            if c_obj.pr_association_state is not PrAssociationState.MATCHED_PR:
                continue
            for ref_key in (
                "associated_pr_id",
                "pr_provider_object_id",
                "associated_pr_number",
                "provider_number",
            ):
                val = c_obj.metadata.get(ref_key)
                if val is not None and str(val).strip() in pr_match_keys:
                    matched[c_obj.source_object_id] = c_obj
                    break

        return sorted(matched.values(), key=lambda c: (c.provider_object_id, c.source_object_id))

    @staticmethod
    def _resolve_pr_reviews(
        *,
        pr_match_keys: set[str],
        review_objects: list[_SourceObjectSnapshot],
    ) -> list[_SourceObjectSnapshot]:
        matched: list[_SourceObjectSnapshot] = []
        for r_obj in review_objects:
            for ref_key in (
                "provider_number",
                "parent_pr_number",
                "parent_pr_id",
                "pr_provider_object_id",
            ):
                val = r_obj.metadata.get(ref_key)
                if val is not None and str(val).strip() in pr_match_keys:
                    matched.append(r_obj)
                    break
        return sorted(matched, key=lambda r: (r.provider_object_id, r.source_object_id))

    @staticmethod
    def _resolve_pr_comments(
        *,
        pr_match_keys: set[str],
        comment_objects: list[_SourceObjectSnapshot],
    ) -> list[_SourceObjectSnapshot]:
        matched: list[_SourceObjectSnapshot] = []
        for cm_obj in comment_objects:
            target_type = str(cm_obj.metadata.get("target_object_type") or "").strip().upper()
            if target_type == "ISSUE":
                continue
            for ref_key in (
                "provider_number",
                "parent_pr_number",
                "parent_pr_id",
                "pr_provider_object_id",
            ):
                val = cm_obj.metadata.get(ref_key)
                if val is not None and str(val).strip() in pr_match_keys:
                    matched.append(cm_obj)
                    break
        return sorted(matched, key=lambda c: (c.provider_object_id, c.source_object_id))

    @staticmethod
    def _resolve_pr_context_issues(
        *,
        pr: _SourceObjectSnapshot,
        pr_num: int | None,
        pr_match_keys: set[str],
        pr_title: str,
        eligible_pr_count: int,
        prs_cursor_meta: Mapping[str, Any],
        issue_objects: list[_SourceObjectSnapshot],
        issues_by_key: Mapping[str, _SourceObjectSnapshot],
    ) -> tuple[list[tuple[_SourceObjectSnapshot, EventMembershipOrigin, str]], set[str]]:
        # Map issue_source_object_id -> (issue_obj, origin, rationale_code)
        # Explicit SOURCE reference takes precedence over RULE clue.
        linked: dict[str, tuple[_SourceObjectSnapshot, EventMembershipOrigin, str]] = {}
        flag_keys: set[str] = set()

        def _register_explicit_ref(raw_ref: str) -> None:
            cleaned = raw_ref.strip()
            if not cleaned:
                return
            iss_obj = issues_by_key.get(cleaned.lower())
            if iss_obj is not None:
                linked[iss_obj.source_object_id] = (
                    iss_obj,
                    EventMembershipOrigin.SOURCE,
                    "PR_REFERENCED_ISSUE",
                )
                flag_keys.add(f"obj:{iss_obj.source_object_id}")
            else:
                flag_keys.add(f"ref:{cleaned.lower()}")

        # 1. Explicit PR metadata references
        for key in (
            "referenced_issue_numbers",
            "referenced_issue_ids",
            "referenced_ticket_ids",
        ):
            for ref in _as_str_list(pr.metadata.get(key)):
                _register_explicit_ref(ref)

        # 2. PRS checkpoint per-PR mapping or single-PR run-level referenced_issue_numbers
        by_pr_map = prs_cursor_meta.get("referenced_issues_by_pr")
        if isinstance(by_pr_map, Mapping):
            for pk in pr_match_keys:
                if pk in by_pr_map:
                    for ref in _as_str_list(by_pr_map[pk]):
                        _register_explicit_ref(ref)
        elif eligible_pr_count == 1:
            for ref in _as_str_list(prs_cursor_meta.get("referenced_issue_numbers")):
                _register_explicit_ref(ref)

        # 3. Issue-side back-references & deterministic RULE clues (ticket key / shared ticket label)
        pr_head_ref = str(pr.metadata.get("head_ref") or "")
        pr_labels = {lbl.lower() for lbl in _as_str_list(pr.metadata.get("labels"))}
        pr_ticket_labels = {lbl for lbl in pr_labels if lbl.startswith("ticket:")}

        for iss in issue_objects:
            back_refs = set(
                _as_str_list(iss.metadata.get("referenced_by_pr_ids"))
                + _as_str_list(iss.metadata.get("referenced_by_pr_numbers"))
            )
            if back_refs & pr_match_keys:
                linked[iss.source_object_id] = (
                    iss,
                    EventMembershipOrigin.SOURCE,
                    "PR_REFERENCED_ISSUE",
                )
                flag_keys.add(f"obj:{iss.source_object_id}")
                continue

            if iss.source_object_id in linked:
                continue

            ext_key = iss.metadata.get("external_key")
            iss_labels = {lbl.lower() for lbl in _as_str_list(iss.metadata.get("labels"))}
            iss_ticket_labels = {lbl for lbl in iss_labels if lbl.startswith("ticket:")}
            matched_by_rule = False
            if isinstance(ext_key, str) and ext_key.strip():
                if _token_present_in_text(ext_key, pr_title) or _token_present_in_text(
                    ext_key, pr_head_ref
                ):
                    matched_by_rule = True
            if not matched_by_rule and (pr_ticket_labels & iss_ticket_labels):
                matched_by_rule = True

            if matched_by_rule:
                linked[iss.source_object_id] = (
                    iss,
                    EventMembershipOrigin.RULE,
                    "SHARED_TICKET_OR_LABEL_CONTEXT",
                )
                flag_keys.add(f"obj:{iss.source_object_id}")

        sorted_linked = sorted(
            linked.values(),
            key=lambda entry: (entry[0].provider_object_id, entry[0].source_object_id),
        )
        return sorted_linked, flag_keys

    @classmethod
    def _is_direct_commit_eligible(
        cls,
        *,
        assoc: _CommitAssocRow,
        default_branch: str,
        objects_by_id: Mapping[str, _SourceObjectSnapshot],
        claimed_commit_obj_ids: set[str],
        claimed_commit_shas: set[str],
    ) -> bool:
        if assoc.pr_association_state is not PrAssociationState.NO_MATCH_OBSERVED:
            return False
        if not (assoc.lookup_completed and assoc.reachable_at_start and assoc.reachable_at_end):
            return False
        if assoc.default_branch_name != default_branch:
            return False
        if assoc.matched_pr_provider_id is not None:
            return False
        if assoc.commit_source_object_id in claimed_commit_obj_ids:
            return False
        if assoc.commit_sha.lower() in claimed_commit_shas:
            return False

        c_obj = objects_by_id.get(assoc.commit_source_object_id)
        if c_obj is None or c_obj.object_type is not SourceObjectType.COMMIT:
            return False
        if c_obj.pr_association_state in (
            PrAssociationState.MATCHED_PR,
            PrAssociationState.UNKNOWN,
        ):
            return False
        if cls._is_explicitly_non_default_commit(c_obj, default_branch=default_branch):
            return False
        if c_obj.metadata.get("within_import_window") is False:
            return False
        return True

    @staticmethod
    def _resolve_commit_parent_count(
        c_obj: _SourceObjectSnapshot, assoc: _CommitAssocRow
    ) -> int:
        for meta in (c_obj.metadata, assoc.observation_metadata):
            pc = _optional_int(meta.get("parent_count"))
            if pc is not None and pc >= 0:
                return pc
            parent_shas = _as_str_list(meta.get("parent_shas"))
            if parent_shas:
                return len(parent_shas)
        return 1

    @staticmethod
    def _has_skipped_outcome_for_event(
        *,
        skipped_outcomes: list[_ItemOutcomeRow],
        member_objects: list[_SourceObjectSnapshot],
        pr_num: int | None,
        pr_pid: str | None,
        commit_shas: set[str],
    ) -> bool:
        if not skipped_outcomes:
            return False
        obj_ids = {o.source_object_id for o in member_objects}
        obj_keys = {(o.object_type.value, o.provider_object_id) for o in member_objects}
        pr_keys = {k for k in (pr_pid, str(pr_num) if pr_num is not None else None) if k}

        for out in skipped_outcomes:
            if out.source_object_id and out.source_object_id in obj_ids:
                return True
            if (out.object_type, out.provider_object_id) in obj_keys:
                return True
            if pr_keys:
                for meta_key in ("pr_number", "parent_pr_number", "pr_provider_object_id"):
                    val = out.outcome_metadata.get(meta_key)
                    if val is not None and str(val).strip() in pr_keys:
                        return True
            if commit_shas:
                o_sha = _norm_sha(out.outcome_metadata.get("commit_sha"))
                if o_sha and o_sha in commit_shas:
                    return True
        return False

    @staticmethod
    def _has_ambiguous_author(
        *,
        ambiguity_index: _AuthorAmbiguityIndex,
        objects: list[_SourceObjectSnapshot],
    ) -> bool:
        ext_ids: set[str] = set()
        usernames: set[str] = set()
        for obj in objects:
            for key in ("author_external_id", "committer_external_id"):
                val = obj.metadata.get(key)
                if val is not None and str(val).strip():
                    ext_ids.add(str(val).strip())
            for key in ("author_login", "author_handle", "committer_login"):
                val = obj.metadata.get(key)
                if val is not None and str(val).strip():
                    usernames.add(str(val).strip())
        return ambiguity_index.is_ambiguous(external_ids=ext_ids, usernames=usernames)

    @staticmethod
    def _compute_pr_time_bounds(
        *,
        pr: _SourceObjectSnapshot,
        pr_commits: list[_SourceObjectSnapshot],
    ) -> tuple[datetime | None, datetime | None]:
        candidates_start: list[datetime] = []
        pr_created = _parse_iso_ts(pr.metadata.get("created_at"))
        if pr_created is not None:
            candidates_start.append(pr_created)
        for c_obj in pr_commits:
            c_ts = (
                _parse_iso_ts(c_obj.metadata.get("committed_at"))
                or _parse_iso_ts(c_obj.metadata.get("created_at"))
                or c_obj.provider_updated_at
            )
            if c_ts is not None:
                candidates_start.append(c_ts)

        pr_merged = _parse_iso_ts(pr.metadata.get("merged_at"))
        pr_updated = _parse_iso_ts(pr.metadata.get("updated_at")) or pr.provider_updated_at
        occurred_end = pr_merged or pr_updated or pr_created
        occurred_start = min(candidates_start) if candidates_start else occurred_end

        if occurred_start is not None and occurred_end is not None and occurred_end < occurred_start:
            occurred_start = occurred_end
        return occurred_start, occurred_end

    # =========================================================================
    # Idempotent Upsert & User-Review / CANNOT_LINK Preservation
    # =========================================================================

    @staticmethod
    def _lock_existing_event(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        primary_anchor_source_object_id: str,
        anchor_mode: WorkEventAnchorMode,
        promoted_event_id: str | None,
    ) -> dict[str, Any] | None:
        if promoted_event_id is not None:
            cur.execute(
                """
                SELECT
                    id::text, version, status, created_by, title,
                    summary_metadata, occurred_start_at, occurred_end_at
                FROM public.work_events
                WHERE workspace_id = %s::uuid
                  AND id = %s::uuid
                FOR UPDATE
                """,
                (ws_uuid, promoted_event_id),
            )
            row = cur.fetchone()
            if row is not None:
                return {
                    "id": str(row[0]),
                    "version": int(row[1]),
                    "status": str(row[2]),
                    "created_by": str(row[3]),
                    "title": str(row[4]),
                    "summary_metadata": dict(row[5] or {}),
                    "occurred_start_at": row[6],
                    "occurred_end_at": row[7],
                }

        cur.execute(
            """
            SELECT
                id::text, version, status, created_by, title,
                summary_metadata, occurred_start_at, occurred_end_at
            FROM public.work_events
            WHERE workspace_id = %s::uuid
              AND primary_anchor_source_object_id = %s::uuid
              AND anchor_mode = %s
            ORDER BY created_at ASC, id ASC
            FOR UPDATE
            """,
            (ws_uuid, primary_anchor_source_object_id, anchor_mode.value),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "version": int(row[1]),
            "status": str(row[2]),
            "created_by": str(row[3]),
            "title": str(row[4]),
            "summary_metadata": dict(row[5] or {}),
            "occurred_start_at": row[6],
            "occurred_end_at": row[7],
        }

    def _upsert_candidate_event(
        self,
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        run_uuid: str,
        existing_ev: dict[str, Any] | None,
        primary_anchor: _SourceObjectSnapshot,
        anchor_mode: WorkEventAnchorMode,
        title: str,
        summary_metadata: dict[str, Any],
        occurred_start_at: datetime | None,
        occurred_end_at: datetime | None,
        review_flags: tuple[ReviewFlagCode, ...],
        desired_memberships: list[
            tuple[
                _SourceObjectSnapshot,
                EventMembershipRelationClass,
                EventRelationType,
                EventMembershipOrigin,
                str,
            ]
        ],
        ts: datetime,
    ) -> tuple[BuiltCandidateSummary, int, int, int]:
        assert_no_forbidden_raw_keys(summary_metadata, field_name="work_events.summary_metadata")

        created = False
        enriched = False
        preserved_user_decision = False

        if existing_ev is None:
            event_id = str(uuid.uuid4())
            clean_title = title.strip() or f"{anchor_mode.value} candidate"
            cur.execute(
                """
                INSERT INTO public.work_events (
                    id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                    anchor_scope, version, title, summary_metadata,
                    status, created_by, occurred_start_at, occurred_end_at,
                    created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s,
                    NULL, 1, %s, %s::jsonb,
                    %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    event_id,
                    ws_uuid,
                    primary_anchor.source_object_id,
                    anchor_mode.value,
                    clean_title,
                    json.dumps(summary_metadata, sort_keys=True),
                    WorkEventStatus.CANDIDATE.value,
                    RecordOrigin.RULE.value,
                    occurred_start_at,
                    occurred_end_at,
                    ts,
                    ts,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.sync_run_event_promotions (
                    id, workspace_id, sync_run_id, source_object_id,
                    anchor_mode, work_event_id, promoted_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s, %s::uuid, %s
                )
                ON CONFLICT (workspace_id, source_object_id, anchor_mode) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    ws_uuid,
                    run_uuid,
                    primary_anchor.source_object_id,
                    anchor_mode.value,
                    event_id,
                    ts,
                ),
            )
            created = True
            final_status = WorkEventStatus.CANDIDATE
            final_version = 1
            final_title = clean_title
        else:
            event_id = existing_ev["id"]
            ex_status = existing_ev["status"]
            ex_version = int(existing_ev["version"])
            ex_created_by = existing_ev["created_by"]
            ex_title = str(existing_ev["title"])
            ex_meta = dict(existing_ev["summary_metadata"])

            is_user_protected = (
                ex_status in _PROTECTED_EVENT_STATUSES
                or ex_version > 1
                or ex_created_by == RecordOrigin.USER.value
            )

            if is_user_protected:
                preserved_user_decision = True
                final_status = WorkEventStatus(ex_status)
                final_version = ex_version
                final_title = ex_title
                existing_flags = tuple(
                    ReviewFlagCode(f)
                    for f in ex_meta.get("review_flags", [])
                    if f in ReviewFlagCode._value2member_map_
                )
                # Preserve user-reviewed event and its memberships untouched.
                active_memberships = self._load_active_event_memberships(
                    cur, ws_uuid=ws_uuid, event_id=event_id
                )
                return (
                    BuiltCandidateSummary(
                        event_id=event_id,
                        workspace_id=ws_uuid,
                        primary_anchor_source_object_id=primary_anchor.source_object_id,
                        anchor_mode=anchor_mode,
                        status=final_status,
                        version=final_version,
                        title=final_title,
                        created=False,
                        enriched=False,
                        preserved_user_decision=True,
                        review_flags=existing_flags,
                        memberships=active_memberships,
                        preserved_cannot_link_object_ids=(),
                    ),
                    0,
                    0,
                    0,
                )

            # Unedited RULE CANDIDATE (version=1): idempotently merge safe metadata & fill gaps
            merged_meta = dict(ex_meta)
            merged_meta.update(summary_metadata)
            assert_no_forbidden_raw_keys(merged_meta, field_name="work_events.summary_metadata")

            new_title = title.strip() or ex_title
            new_start = existing_ev["occurred_start_at"] or occurred_start_at
            new_end = existing_ev["occurred_end_at"] or occurred_end_at
            if new_start is not None and new_end is not None and new_end < new_start:
                new_start = new_end

            if (
                new_title != ex_title
                or merged_meta != ex_meta
                or new_start != existing_ev["occurred_start_at"]
                or new_end != existing_ev["occurred_end_at"]
            ):
                cur.execute(
                    """
                    UPDATE public.work_events
                    SET title = %s,
                        summary_metadata = %s::jsonb,
                        occurred_start_at = %s,
                        occurred_end_at = %s,
                        updated_at = %s
                    WHERE workspace_id = %s::uuid
                      AND id = %s::uuid
                      AND version = 1
                      AND status = 'CANDIDATE'
                      AND created_by = 'RULE'
                    """,
                    (
                        new_title,
                        json.dumps(merged_meta, sort_keys=True),
                        new_start,
                        new_end,
                        ts,
                        ws_uuid,
                        event_id,
                    ),
                )
                enriched = True

            cur.execute(
                """
                INSERT INTO public.sync_run_event_promotions (
                    id, workspace_id, sync_run_id, source_object_id,
                    anchor_mode, work_event_id, promoted_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s, %s::uuid, %s
                )
                ON CONFLICT (workspace_id, source_object_id, anchor_mode) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    ws_uuid,
                    run_uuid,
                    primary_anchor.source_object_id,
                    anchor_mode.value,
                    event_id,
                    ts,
                ),
            )
            final_status = WorkEventStatus(ex_status)
            final_version = ex_version
            final_title = new_title

        # Upsert memberships with strict CANNOT_LINK precedence (EVENT-05)
        m_inserted = 0
        m_upgraded = 0
        cannot_link_blocked_ids: list[str] = []

        for obj, rel_class, rel_type, origin, rationale_code in desired_memberships:
            action = self._upsert_membership_respecting_cannot_link(
                cur,
                ws_uuid=ws_uuid,
                event_id=event_id,
                source_object_id=obj.source_object_id,
                relation_class=rel_class,
                relation_type=rel_type,
                origin=origin,
                rationale_code=rationale_code,
                ts=ts,
            )
            if action == "INSERTED":
                m_inserted += 1
            elif action == "UPGRADED":
                m_upgraded += 1
            elif action == "BLOCKED_CANNOT_LINK":
                cannot_link_blocked_ids.append(obj.source_object_id)

        active_memberships = self._load_active_event_memberships(
            cur, ws_uuid=ws_uuid, event_id=event_id
        )
        return (
            BuiltCandidateSummary(
                event_id=event_id,
                workspace_id=ws_uuid,
                primary_anchor_source_object_id=primary_anchor.source_object_id,
                anchor_mode=anchor_mode,
                status=final_status,
                version=final_version,
                title=final_title,
                created=created,
                enriched=enriched,
                preserved_user_decision=preserved_user_decision,
                review_flags=review_flags,
                memberships=active_memberships,
                preserved_cannot_link_object_ids=tuple(sorted(set(cannot_link_blocked_ids))),
            ),
            m_inserted,
            m_upgraded,
            len(set(cannot_link_blocked_ids)),
        )

    @staticmethod
    def _upsert_membership_respecting_cannot_link(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        event_id: str,
        source_object_id: str,
        relation_class: EventMembershipRelationClass,
        relation_type: EventRelationType,
        origin: EventMembershipOrigin,
        rationale_code: str,
        ts: datetime,
    ) -> str:
        # EVENT-05: Active CANNOT_LINK on (event_id, source_object_id) ALWAYS wins.
        cur.execute(
            """
            SELECT id::text, relation_class, relation_type, origin, status
            FROM public.event_memberships
            WHERE workspace_id = %s::uuid
              AND event_id = %s::uuid
              AND source_object_id = %s::uuid
            FOR UPDATE
            """,
            (ws_uuid, event_id, source_object_id),
        )
        existing_rows = cur.fetchall()
        for _, ex_class, _, _, ex_status in existing_rows:
            if (
                str(ex_class) == EventMembershipRelationClass.CANNOT_LINK.value
                and str(ex_status) == "ACTIVE"
            ):
                return "BLOCKED_CANNOT_LINK"

        for row_id, ex_class, ex_type, ex_origin, ex_status in existing_rows:
            if str(ex_type) == relation_type.value:
                if (
                    str(ex_status) == "ACTIVE"
                    and str(ex_class) == relation_class.value
                    and str(ex_origin) == EventMembershipOrigin.RULE.value
                    and origin is EventMembershipOrigin.SOURCE
                ):
                    cur.execute(
                        """
                        UPDATE public.event_memberships
                        SET origin = %s,
                            rationale_code = %s
                        WHERE workspace_id = %s::uuid
                          AND id = %s::uuid
                        """,
                        (origin.value, rationale_code, ws_uuid, str(row_id)),
                    )
                    return "UPGRADED"
                return "UNCHANGED"

        cur.execute(
            """
            INSERT INTO public.event_memberships (
                id, workspace_id, event_id, source_object_id,
                relation_class, relation_type, origin, rationale_code,
                status, created_at
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s,
                'ACTIVE', %s
            )
            ON CONFLICT (event_id, source_object_id, relation_type) DO NOTHING
            RETURNING id::text
            """,
            (
                str(uuid.uuid4()),
                ws_uuid,
                event_id,
                source_object_id,
                relation_class.value,
                relation_type.value,
                origin.value,
                rationale_code,
                ts,
            ),
        )
        return "INSERTED" if cur.fetchone() is not None else "UNCHANGED"

    @staticmethod
    def _load_active_event_memberships(
        cur: psycopg.Cursor,
        *,
        ws_uuid: str,
        event_id: str,
    ) -> tuple[BuiltMembershipSummary, ...]:
        cur.execute(
            """
            SELECT
                em.source_object_id::text,
                so.object_type,
                so.provider_object_id,
                em.relation_class,
                em.relation_type,
                em.origin,
                em.rationale_code
            FROM public.event_memberships em
            JOIN public.source_objects so
              ON so.workspace_id = em.workspace_id
             AND so.id = em.source_object_id
            WHERE em.workspace_id = %s::uuid
              AND em.event_id = %s::uuid
              AND em.status = 'ACTIVE'
            ORDER BY em.relation_class ASC, em.relation_type ASC, so.provider_object_id ASC
            """,
            (ws_uuid, event_id),
        )
        return tuple(
            BuiltMembershipSummary(
                source_object_id=str(r[0]),
                object_type=SourceObjectType(str(r[1])),
                provider_object_id=str(r[2]),
                relation_class=EventMembershipRelationClass(str(r[3])),
                relation_type=str(r[4]),
                origin=EventMembershipOrigin(str(r[5])),
                rationale_code=str(r[6]) if r[6] is not None else None,
            )
            for r in cur.fetchall()
        )


__all__ = [
    "BeforeCommitHook",
    "ConnectionFactory",
    "PostgresEventCandidateBuilder",
]
