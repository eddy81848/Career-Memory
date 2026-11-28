"""Deterministic Plan C (ADR-013) PostgreSQL 18 Seeder for Frozen 21-Case Event Evaluation.

Maps each approved fixture's synthetic_sources into real PostgreSQL 18 tables
(migrations 0010..0060) using deterministic uuid5 identifiers while strictly
enforcing Plan C raw-text non-persistence:
- Raw PR bodies and multi-line commit messages are NEVER persisted in PostgreSQL.
- Only SHA-256 content hashes, single-line title hints, and safe structural
  metadata are written; every JSONB payload passes assert_no_forbidden_raw_keys.
- Never inspects expected_proposal during seeding (zero oracle leakage).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from career_memory_api.schema_contracts.enums import (
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    SourceObjectType,
    WorkEventAnchorMode,
)
from career_memory_api.schema_contracts.metadata_guard import assert_no_forbidden_raw_keys
from career_memory_api.work_events.relations import canonicalize_event_pair

_SEED_UUID_NAMESPACE = uuid.UUID("9a7e4f8c-3b2d-4c1a-8e5f-6d7c8b9a0e1f")
_FIXED_BASE_TIME = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def deterministic_uuid(case_id: str, entity_kind: str, synthetic_id: str) -> str:
    """Derive a reproducible UUIDv5 for a synthetic fixture identifier."""
    return str(
        uuid.uuid5(
            _SEED_UUID_NAMESPACE,
            f"{case_id}:{entity_kind}:{synthetic_id}",
        )
    )


def _sha256_tag(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso_utc(raw: str | None, *, fallback: datetime = _FIXED_BASE_TIME) -> datetime:
    if not raw or not isinstance(raw, str):
        return fallback
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    parsed = datetime.fromisoformat(cleaned)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _first_line_hint(raw: str | None, *, fallback: str) -> str:
    if isinstance(raw, str):
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:200]
    return fallback


def _map_assoc_status(raw: str | None) -> PrAssociationState | None:
    if raw == "ASSOCIATED":
        return PrAssociationState.MATCHED_PR
    if raw == "NO_MATCH_OBSERVED":
        return PrAssociationState.NO_MATCH_OBSERVED
    if raw == "UNKNOWN":
        return PrAssociationState.UNKNOWN
    return None


@dataclass(slots=True)
class SeedIdRegistry:
    """Bidirectional registry between fixture synthetic IDs and PostgreSQL UUIDs."""

    case_id: str
    workspace_uuids: dict[str, str] = field(default_factory=dict)
    user_uuids: dict[str, str] = field(default_factory=dict)
    workspace_default_actor: dict[str, str] = field(default_factory=dict)
    repo_uuids: dict[str, str] = field(default_factory=dict)
    repo_to_workspace: dict[str, str] = field(default_factory=dict)
    repo_default_branch: dict[str, str] = field(default_factory=dict)
    sync_run_uuids: dict[str, str] = field(default_factory=dict)
    sync_run_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    pr_source_object_uuids: dict[str, str] = field(default_factory=dict)
    commit_source_object_uuids: dict[str, str] = field(default_factory=dict)
    ticket_source_object_uuids: dict[str, str] = field(default_factory=dict)
    content_unit_uuids: dict[str, str] = field(default_factory=dict)
    event_uuids: dict[str, str] = field(default_factory=dict)
    fact_uuids: dict[str, str] = field(default_factory=dict)
    claim_uuids: dict[str, str] = field(default_factory=dict)
    uuid_to_syn_id: dict[str, str] = field(default_factory=dict)
    raw_body_canaries: list[str] = field(default_factory=list)
    seeded_source_counts: dict[str, int] = field(default_factory=dict)

    def register(self, syn_id: str, uuid_str: str) -> str:
        self.uuid_to_syn_id[uuid_str] = syn_id
        return uuid_str

    def resolve_any_target_uuid(self, syn_id: str) -> str:
        for table in (
            self.event_uuids,
            self.ticket_source_object_uuids,
            self.commit_source_object_uuids,
            self.pr_source_object_uuids,
            self.content_unit_uuids,
        ):
            if syn_id in table:
                return table[syn_id]
        return deterministic_uuid(self.case_id, "unknown_target", syn_id)

    def to_syn_id(self, uuid_str: str | None) -> str | None:
        if uuid_str is None:
            return None
        return self.uuid_to_syn_id.get(uuid_str)


def seed_approved_case_into_pg18(
    admin_conn: psycopg.Connection,
    *,
    case_id: str,
    case_data: dict[str, Any],
) -> SeedIdRegistry:
    """Seed one frozen approved case's synthetic_sources into PostgreSQL 18 (Plan C compliant)."""
    ss = case_data.get("synthetic_sources") or {}
    commands = case_data.get("user_commands") or []
    reg = SeedIdRegistry(case_id=case_id)

    workspaces = ss.get("workspaces") or []
    repositories = ss.get("repositories") or []
    sync_runs = ss.get("sync_runs") or []
    pull_requests = ss.get("pull_requests") or []
    commits = ss.get("commits") or []
    issues_tickets = ss.get("issues_tickets") or []
    reviews_comments = ss.get("reviews_comments") or []
    out_of_window_assocs = ss.get("out_of_window_pr_associations") or []
    initial_events = ss.get("initial_events") or []
    event_relations = ss.get("event_relations") or []

    pr_by_id: dict[str, dict[str, Any]] = {pr["pr_id"]: pr for pr in pull_requests}
    commit_by_id: dict[str, dict[str, Any]] = {c["commit_id"]: c for c in commits}
    oow_by_commit_id: dict[str, dict[str, Any]] = {
        obs["observed_default_branch_commit_id"]: obs for obs in out_of_window_assocs
    }

    # Gather text used legitimately in titles/summaries/commands so PR body canaries
    # strictly test that raw PR body text is NEVER persisted anywhere in PostgreSQL.
    allowed_non_raw_strings: list[str] = []
    for pr in pull_requests:
        if isinstance(pr.get("title"), str):
            allowed_non_raw_strings.append(pr["title"])
    for ev in initial_events:
        for k in ("title", "summary"):
            if isinstance(ev.get(k), str):
                allowed_non_raw_strings.append(ev[k])
    for cmd in commands:
        payload = cmd.get("payload") or {}
        for k in ("title", "composite_title", "summary", "reason"):
            if isinstance(payload.get(k), str):
                allowed_non_raw_strings.append(payload[k])

    for pr in pull_requests:
        body = pr.get("body")
        if isinstance(body, str) and len(body.strip()) >= 12:
            stripped_body = body.strip()
            if not any(stripped_body in allowed for allowed in allowed_non_raw_strings):
                reg.raw_body_canaries.append(stripped_body)

    # Collect per-workspace actor IDs from user_commands
    ws_actor_ids: dict[str, list[str]] = {}
    for cmd in commands:
        ws_syn = str(cmd["workspace_id"])
        actor_syn = str(cmd.get("actor_id") or f"owner_{ws_syn}")
        ws_actor_ids.setdefault(ws_syn, [])
        if actor_syn not in ws_actor_ids[ws_syn]:
            ws_actor_ids[ws_syn].append(actor_syn)

    with admin_conn.transaction():
        with admin_conn.cursor() as cur:
            # 1. Workspaces, Users, Workspace Memberships, Source Accounts
            ws_account_uuids: dict[str, str] = {}
            for ws in workspaces:
                ws_syn = str(ws["workspace_id"])
                ws_uuid = reg.register(ws_syn, deterministic_uuid(case_id, "workspace", ws_syn))
                reg.workspace_uuids[ws_syn] = ws_uuid

                actors = ws_actor_ids.get(ws_syn) or [f"usr_default_{ws_syn}"]
                primary_actor_syn = actors[0]
                reg.workspace_default_actor[ws_syn] = primary_actor_syn

                for actor_syn in actors:
                    if actor_syn not in reg.user_uuids:
                        u_uuid = reg.register(
                            actor_syn, deterministic_uuid(case_id, "user", actor_syn)
                        )
                        reg.user_uuids[actor_syn] = u_uuid
                        cur.execute(
                            """
                            INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                            VALUES (%s::uuid, 'https://auth.eval.local/v1', %s, %s, %s)
                            ON CONFLICT (id) DO NOTHING
                            """,
                            (
                                u_uuid,
                                f"sub-{case_id}-{actor_syn}",
                                f"{actor_syn}@{case_id.lower()}.eval.test",
                                f"Synthetic Actor {actor_syn}",
                            ),
                        )

                owner_uuid = reg.user_uuids[primary_actor_syn]
                cur.execute(
                    """
                    INSERT INTO public.workspaces (id, owner_user_id, name)
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (ws_uuid, owner_uuid, str(ws.get("name") or f"Workspace {ws_syn}")),
                )
                for actor_syn in actors:
                    cur.execute(
                        """
                        INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                        VALUES (%s::uuid, %s::uuid, 'OWNER')
                        ON CONFLICT (workspace_id, user_id) DO NOTHING
                        """,
                        (ws_uuid, reg.user_uuids[actor_syn]),
                    )

                acct_uuid = deterministic_uuid(case_id, "source_account", ws_syn)
                ws_account_uuids[ws_syn] = acct_uuid
                cur.execute(
                    """
                    INSERT INTO public.source_accounts (
                        id, workspace_id, provider, external_account_id,
                        token_secret_ref, scopes, status
                    )
                    VALUES (
                        %s::uuid, %s::uuid, 'GITHUB', %s,
                        %s, ARRAY['public_repo'], 'ACTIVE'
                    )
                    """,
                    (
                        acct_uuid,
                        ws_uuid,
                        f"gh-eval-{ws_syn}",
                        f"secret://github/eval/{case_id}/{ws_syn}",
                    ),
                )

            # 2. Source Resources (Repositories)
            for repo in repositories:
                repo_syn = str(repo["repo_id"])
                ws_syn = str(repo["workspace_id"])
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.register(repo_syn, deterministic_uuid(case_id, "repo", repo_syn))
                reg.repo_uuids[repo_syn] = repo_uuid
                reg.repo_to_workspace[repo_syn] = ws_syn
                reg.repo_default_branch[repo_syn] = str(repo["default_branch"])
                full_name = str(repo["full_name"])

                cur.execute(
                    """
                    INSERT INTO public.source_resources (
                        id, workspace_id, source_account_id, provider_resource_id,
                        resource_type, name, canonical_url, visibility,
                        processing_mode, raw_storage_mode, raw_retention_seconds,
                        status, policy_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                        'PUBLIC_CLOUD', 'NONE', NULL,
                        'ACTIVE', 'v1'
                    )
                    """,
                    (
                        repo_uuid,
                        ws_uuid,
                        ws_account_uuids[ws_syn],
                        f"gh-repo-{repo_syn}",
                        full_name,
                        f"https://github.com/{full_name}",
                    ),
                )

            # 3. Sync Runs (+ IMPORT_READY outbox strictly for SUCCESS + promotes_events)
            repo_default_sync_run: dict[str, str] = {}
            for run in sync_runs:
                run_syn = str(run["sync_run_id"])
                ws_syn = str(run["workspace_id"])
                repo_syn = str(run["repo_id"])
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                run_uuid = reg.register(run_syn, deterministic_uuid(case_id, "sync_run", run_syn))
                plan_uuid = deterministic_uuid(case_id, "plan", run_syn)
                reg.sync_run_uuids[run_syn] = run_uuid
                reg.sync_run_meta[run_syn] = dict(run)
                repo_default_sync_run.setdefault(repo_syn, run_syn)

                default_branch = reg.repo_default_branch[repo_syn]
                run_status = str(run["status"])
                promotes_events = bool(run.get("promotes_events", False))
                is_import_ready = run_status == "SUCCESS" and promotes_events
                completed_at = _parse_iso_utc(run.get("completed_at"))

                plan_spec: dict[str, Any] = {
                    "default_branch": default_branch,
                    "mode": str(run.get("run_type") or "INITIAL_BACKFILL"),
                }
                if run.get("import_window_start"):
                    plan_spec["backfill_from"] = str(run["import_window_start"])
                if run.get("import_window_end"):
                    plan_spec["cutoff_at"] = str(run["import_window_end"])
                assert_no_forbidden_raw_keys(plan_spec, field_name="sync_runs.plan_spec")

                failure_reason_code = (
                    "PR_ASSOCIATION_INCOMPLETE"
                    if run_status in ("PARTIAL", "PARTIAL_WITH_LIMIT", "FAILED")
                    else None
                )

                cur.execute(
                    """
                    INSERT INTO public.sync_runs (
                        id, workspace_id, source_resource_id, plan_id, mode,
                        run_version, status, processing_status, import_ready,
                        failure_reason_code,
                        plan_spec, observed_at, created_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, 'INITIAL_BACKFILL',
                        1, %s, 'NOT_STARTED', %s,
                        %s,
                        %s::jsonb, %s, %s, %s
                    )
                    """,
                    (
                        run_uuid,
                        ws_uuid,
                        repo_uuid,
                        plan_uuid,
                        run_status,
                        is_import_ready,
                        failure_reason_code,
                        json.dumps(plan_spec, sort_keys=True),
                        completed_at,
                        completed_at,
                        completed_at,
                    ),
                )

                if is_import_ready:
                    outbox_payload = {
                        "sync_run_id": run_uuid,
                        "source_resource_id": repo_uuid,
                        "plan_id": plan_uuid,
                        "default_branch": default_branch,
                    }
                    assert_no_forbidden_raw_keys(outbox_payload, field_name="outbox_events.payload")
                    cur.execute(
                        """
                        INSERT INTO public.outbox_events (
                            id, workspace_id, event_type, aggregate_type,
                            aggregate_id, dedupe_key, payload, status,
                            next_attempt_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'IMPORT_READY', 'SYNC_RUN',
                            %s::uuid, %s, %s::jsonb, 'PENDING',
                            %s, %s
                        )
                        """,
                        (
                            deterministic_uuid(case_id, "outbox_import_ready", run_syn),
                            ws_uuid,
                            run_uuid,
                            f"import_ready:{run_uuid}",
                            json.dumps(outbox_payload, sort_keys=True),
                            completed_at,
                            completed_at,
                        ),
                    )

            # 4. Pull Requests -> source_objects + source_object_revisions (Plan C)
            revision_by_source_obj_uuid: dict[str, str] = {}
            for pr in pull_requests:
                pr_syn = str(pr["pr_id"])
                repo_syn = str(pr["repo_id"])
                ws_syn = reg.repo_to_workspace[repo_syn]
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                pr_obj_uuid = reg.register(pr_syn, deterministic_uuid(case_id, "pr", pr_syn))
                pr_rev_uuid = deterministic_uuid(case_id, "pr_rev", pr_syn)
                reg.pr_source_object_uuids[pr_syn] = pr_obj_uuid
                revision_by_source_obj_uuid[pr_obj_uuid] = pr_rev_uuid

                merged_at = _parse_iso_utc(pr.get("merged_at"))
                commit_shas = [
                    str(commit_by_id[cid]["sha"]).lower()
                    for cid in (pr.get("commit_ids") or [])
                    if cid in commit_by_id
                ]
                pr_meta: dict[str, Any] = {
                    "provider_number": int(pr["number"]),
                    "title": _first_line_hint(pr.get("title"), fallback=f"PR #{pr['number']}"),
                    "state": str(pr.get("state") or "MERGED"),
                    "merged": str(pr.get("state") or "").upper() == "MERGED",
                    "base_ref": str(pr["base_branch"]),
                    "head_ref": str(pr.get("head_branch") or "feat/synthetic"),
                    "merged_at": pr.get("merged_at"),
                    "author_login": pr.get("author_handle"),
                    "commit_ids": commit_shas,
                    "referenced_ticket_ids": list(pr.get("referenced_ticket_ids") or []),
                }
                if pr.get("merge_strategy"):
                    pr_meta["merge_strategy"] = str(pr["merge_strategy"])
                if "within_import_window" in pr:
                    pr_meta["within_import_window"] = bool(pr["within_import_window"])
                assert_no_forbidden_raw_keys(pr_meta, field_name="source_object_revisions.normalized_metadata")

                # Hash raw body + title into content_hash without persisting raw body!
                content_hash = _sha256_tag(
                    f"pr:{pr_syn}:{pr.get('title','')}:{pr.get('body','')}"
                )
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, pr_association_state,
                        first_seen_at, last_seen_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'ACTIVE', NULL,
                        %s, %s
                    )
                    """,
                    (
                        pr_obj_uuid,
                        ws_uuid,
                        repo_uuid,
                        SourceObjectType.PR.value,
                        str(pr["number"]),
                        merged_at,
                        merged_at,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        provider_updated_at, is_tombstone, normalized_metadata,
                        connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'v1', %s,
                        %s, false, %s::jsonb,
                        'github-eval-v1', 'v1'
                    )
                    """,
                    (
                        pr_rev_uuid,
                        ws_uuid,
                        pr_obj_uuid,
                        f"rev-pr-{pr['number']}",
                        content_hash,
                        merged_at,
                        merged_at,
                        json.dumps(pr_meta, sort_keys=True),
                    ),
                )

            # 5. Commits -> source_objects + source_object_revisions + sync_commit_pr_associations
            for cmt in commits:
                cmt_syn = str(cmt["commit_id"])
                repo_syn = str(cmt["repo_id"])
                ws_syn = reg.repo_to_workspace[repo_syn]
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                sha = str(cmt["sha"]).lower()
                cmt_obj_uuid = reg.register(cmt_syn, deterministic_uuid(case_id, "commit", cmt_syn))
                cmt_rev_uuid = deterministic_uuid(case_id, "commit_rev", cmt_syn)
                reg.commit_source_object_uuids[cmt_syn] = cmt_obj_uuid
                revision_by_source_obj_uuid[cmt_obj_uuid] = cmt_rev_uuid

                committed_at = _parse_iso_utc(cmt.get("committed_at"))
                assoc_state = _map_assoc_status(cmt.get("pr_association_status"))
                assoc_pr_syn = cmt.get("associated_pr_id")
                oow_obs = oow_by_commit_id.get(cmt_syn)

                # Plan C: extract single-line title hint only, never persist raw body/message key
                cmt_meta: dict[str, Any] = {
                    "head_sha": sha,
                    "title": _first_line_hint(cmt.get("message"), fallback=f"Commit {sha[:12]}"),
                    "author_login": cmt.get("author_handle"),
                    "committed_at": cmt.get("committed_at"),
                    "branch_name": cmt.get("branch_name"),
                    "on_default_branch": bool(cmt.get("on_default_branch", False)),
                    "changed_paths": list(cmt.get("changed_paths") or []),
                }
                if "within_import_window" in cmt:
                    cmt_meta["within_import_window"] = bool(cmt["within_import_window"])
                if assoc_pr_syn and assoc_pr_syn in pr_by_id:
                    cmt_meta["associated_pr_number"] = int(pr_by_id[assoc_pr_syn]["number"])
                assert_no_forbidden_raw_keys(cmt_meta, field_name="source_object_revisions.normalized_metadata")

                content_hash = _sha256_tag(f"commit:{cmt_syn}:{sha}:{cmt.get('message','')}")
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, pr_association_state,
                        first_seen_at, last_seen_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'ACTIVE', %s,
                        %s, %s
                    )
                    """,
                    (
                        cmt_obj_uuid,
                        ws_uuid,
                        repo_uuid,
                        SourceObjectType.COMMIT.value,
                        sha,
                        assoc_state.value if assoc_state is not None else None,
                        committed_at,
                        committed_at,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        provider_updated_at, is_tombstone, normalized_metadata,
                        connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'v1', %s,
                        %s, false, %s::jsonb,
                        'github-eval-v1', 'v1'
                    )
                    """,
                    (
                        cmt_rev_uuid,
                        ws_uuid,
                        cmt_obj_uuid,
                        f"rev-commit-{sha[:12]}",
                        content_hash,
                        committed_at,
                        committed_at,
                        json.dumps(cmt_meta, sort_keys=True),
                    ),
                )

                # Seed sync_commit_pr_associations completeness ledger for default-branch / UNKNOWN / out-of-window commits
                run_syn = str(cmt.get("source_sync_run_id") or repo_default_sync_run[repo_syn])
                run_uuid = reg.sync_run_uuids[run_syn]
                default_branch = reg.repo_default_branch[repo_syn]
                on_default = bool(cmt.get("on_default_branch", False))
                verified_start_end = bool(cmt.get("default_branch_verified_at_start_and_end", False))
                lookup_completed = cmt.get("pr_association_lookup_status") == "COMPLETED"

                if on_default or assoc_state is PrAssociationState.UNKNOWN or oow_obs is not None:
                    matched_pr_provider_id: str | None = None
                    matched_pr_in_window: bool | None = None
                    match_mechanism: str | None = None
                    unknown_reason_code: str | None = None
                    obs_meta: dict[str, Any] = {}

                    if oow_obs is not None:
                        matched_pr_provider_id = str(oow_obs["pr_number"])
                        matched_pr_in_window = False
                        match_mechanism = "OUT_OF_WINDOW_PR_API"
                        obs_meta["out_of_window_pr_number"] = int(oow_obs["pr_number"])
                        obs_meta["merge_strategy"] = str(oow_obs.get("merge_strategy") or "REBASE")
                    elif assoc_state is PrAssociationState.MATCHED_PR and assoc_pr_syn in pr_by_id:
                        matched_pr = pr_by_id[assoc_pr_syn]
                        matched_pr_provider_id = str(matched_pr["number"])
                        matched_pr_in_window = bool(matched_pr.get("within_import_window", True))
                        mstrat = str(matched_pr.get("merge_strategy") or "MERGE_COMMIT").upper()
                        if mstrat == "SQUASH":
                            match_mechanism = "SQUASH_COMMIT_SHA"
                        elif mstrat == "REBASE":
                            match_mechanism = "REBASE_COMMIT_SHA"
                        else:
                            match_mechanism = "IN_WINDOW_PR_COMMIT"
                    elif assoc_state is PrAssociationState.UNKNOWN:
                        unknown_reason_code = "PR_ASSOC_INCOMPLETE_OR_FAILED"

                    assert_no_forbidden_raw_keys(
                        obs_meta, field_name="sync_commit_pr_associations.observation_metadata"
                    )
                    cur.execute(
                        """
                        INSERT INTO public.sync_commit_pr_associations (
                            id, workspace_id, sync_run_id, source_resource_id,
                            commit_source_object_id, commit_sha, default_branch_name,
                            pr_association_state, matched_pr_provider_id,
                            matched_pr_in_window, match_mechanism, unknown_reason_code,
                            reachable_at_start, reachable_at_end, lookup_completed,
                            observation_metadata, observed_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s::uuid, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s::jsonb, %s, %s
                        )
                        """,
                        (
                            deterministic_uuid(case_id, "commit_assoc", cmt_syn),
                            ws_uuid,
                            run_uuid,
                            repo_uuid,
                            cmt_obj_uuid,
                            sha,
                            default_branch,
                            (assoc_state or PrAssociationState.UNKNOWN).value,
                            matched_pr_provider_id,
                            matched_pr_in_window,
                            match_mechanism,
                            unknown_reason_code,
                            verified_start_end,
                            verified_start_end,
                            lookup_completed,
                            json.dumps(obs_meta, sort_keys=True),
                            committed_at,
                            committed_at,
                        ),
                    )

                    if assoc_state is PrAssociationState.UNKNOWN:
                        cur.execute(
                            """
                            INSERT INTO public.sync_item_outcomes (
                                id, workspace_id, sync_run_id, source_resource_id,
                                source_object_id, stream_name, object_type,
                                provider_object_id, outcome_kind, reason_code,
                                outcome_metadata, observed_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s::uuid, 'COMMIT_PR_ASSOCIATIONS', 'COMMIT',
                                %s, 'ASSOCIATION_UNKNOWN', 'PR_ASSOC_INCOMPLETE_OR_FAILED',
                                %s::jsonb, %s
                            )
                            """,
                            (
                                deterministic_uuid(case_id, "item_outcome_unknown", cmt_syn),
                                ws_uuid,
                                run_uuid,
                                repo_uuid,
                                cmt_obj_uuid,
                                sha,
                                json.dumps({"commit_sha": sha}, sort_keys=True),
                                committed_at,
                            ),
                        )
                    elif oow_obs is not None:
                        cur.execute(
                            """
                            INSERT INTO public.sync_item_outcomes (
                                id, workspace_id, sync_run_id, source_resource_id,
                                source_object_id, stream_name, object_type,
                                provider_object_id, outcome_kind, reason_code,
                                outcome_metadata, observed_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s::uuid, 'COMMIT_PR_ASSOCIATIONS', 'COMMIT',
                                %s, 'SKIPPED', 'OUT_OF_WINDOW_PR_LINKED',
                                %s::jsonb, %s
                            )
                            """,
                            (
                                deterministic_uuid(case_id, "item_outcome_oow", cmt_syn),
                                ws_uuid,
                                run_uuid,
                                repo_uuid,
                                cmt_obj_uuid,
                                sha,
                                json.dumps(
                                    {
                                        "commit_sha": sha,
                                        "matched_pr_provider_id": str(oow_obs["pr_number"]),
                                    },
                                    sort_keys=True,
                                ),
                                committed_at,
                            ),
                        )

            # 6. Issues / Tickets -> source_objects + source_object_revisions
            for tkt in issues_tickets:
                tkt_syn = str(tkt["ticket_id"])
                repo_syn = str(tkt["repo_id"])
                ws_syn = reg.repo_to_workspace[repo_syn]
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                tkt_obj_uuid = reg.register(tkt_syn, deterministic_uuid(case_id, "ticket", tkt_syn))
                tkt_rev_uuid = deterministic_uuid(case_id, "ticket_rev", tkt_syn)
                reg.ticket_source_object_uuids[tkt_syn] = tkt_obj_uuid
                revision_by_source_obj_uuid[tkt_obj_uuid] = tkt_rev_uuid

                tkt_meta = {
                    "external_key": str(tkt["external_key"]),
                    "title": _first_line_hint(tkt.get("title"), fallback=str(tkt["external_key"])),
                    "relation_role": str(tkt.get("relation_role") or "CONTEXT_ONLY"),
                }
                assert_no_forbidden_raw_keys(tkt_meta, field_name="source_object_revisions.normalized_metadata")
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, pr_association_state,
                        first_seen_at, last_seen_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'ACTIVE', NULL,
                        %s, %s
                    )
                    """,
                    (
                        tkt_obj_uuid,
                        ws_uuid,
                        repo_uuid,
                        SourceObjectType.ISSUE.value,
                        tkt_syn,
                        _FIXED_BASE_TIME,
                        _FIXED_BASE_TIME,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        provider_updated_at, is_tombstone, normalized_metadata,
                        connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'v1', %s,
                        %s, false, %s::jsonb,
                        'github-eval-v1', 'v1'
                    )
                    """,
                    (
                        tkt_rev_uuid,
                        ws_uuid,
                        tkt_obj_uuid,
                        f"rev-issue-{tkt_syn}",
                        _sha256_tag(f"issue:{tkt_syn}:{tkt.get('external_key','')}"),
                        _FIXED_BASE_TIME,
                        _FIXED_BASE_TIME,
                        json.dumps(tkt_meta, sort_keys=True),
                    ),
                )

            # 7. Reviews & Comments (if present in any case)
            for idx, rc in enumerate(reviews_comments):
                rc_syn = str(rc.get("id") or f"rc_{idx}")
                repo_syn = str(rc["repo_id"])
                ws_syn = reg.repo_to_workspace[repo_syn]
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                rc_type = (
                    SourceObjectType.REVIEW
                    if str(rc.get("kind") or "").upper() == "REVIEW"
                    else SourceObjectType.COMMENT
                )
                rc_obj_uuid = reg.register(rc_syn, deterministic_uuid(case_id, "rc", rc_syn))
                rc_rev_uuid = deterministic_uuid(case_id, "rc_rev", rc_syn)
                rc_meta: dict[str, Any] = {
                    "provider_number": rc.get("pr_number"),
                    "author_login": rc.get("author_handle"),
                }
                if rc.get("state"):
                    rc_meta["state"] = rc["state"]
                assert_no_forbidden_raw_keys(rc_meta, field_name="source_object_revisions.normalized_metadata")
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, first_seen_at, last_seen_at
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, 'ACTIVE', %s, %s)
                    """,
                    (
                        rc_obj_uuid,
                        ws_uuid,
                        repo_uuid,
                        rc_type.value,
                        rc_syn,
                        _FIXED_BASE_TIME,
                        _FIXED_BASE_TIME,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        provider_updated_at, is_tombstone, normalized_metadata,
                        connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s, 'v1', %s, %s, false, %s::jsonb,
                        'github-eval-v1', 'v1'
                    )
                    """,
                    (
                        rc_rev_uuid,
                        ws_uuid,
                        rc_obj_uuid,
                        f"rev-rc-{rc_syn}",
                        _sha256_tag(f"rc:{rc_syn}"),
                        _FIXED_BASE_TIME,
                        _FIXED_BASE_TIME,
                        json.dumps(rc_meta, sort_keys=True),
                    ),
                )

            # 8. Content Units referenced by SPLIT evidence_scope (Plan C: text_content/content_ref NULL!)
            for cmd in commands:
                if cmd.get("command_action") != "split":
                    continue
                ws_syn = str(cmd["workspace_id"])
                ws_uuid = reg.workspace_uuids[ws_syn]
                partitions = (cmd.get("payload") or {}).get("partitions") or []
                for p_idx, part in enumerate(partitions):
                    scope = part.get("evidence_scope") or {}
                    part_commits = part.get("source_commit_ids") or []
                    file_paths = scope.get("file_paths") or ["src/synthetic.py"]
                    owner_obj_uuid: str | None = None
                    if part_commits and part_commits[0] in reg.commit_source_object_uuids:
                        owner_obj_uuid = reg.commit_source_object_uuids[part_commits[0]]
                    elif reg.pr_source_object_uuids:
                        owner_obj_uuid = next(iter(reg.pr_source_object_uuids.values()))
                    if owner_obj_uuid is None:
                        continue
                    rev_uuid = revision_by_source_obj_uuid[owner_obj_uuid]
                    for cu_ord, cu_syn in enumerate(scope.get("content_unit_ids") or []):
                        if cu_syn in reg.content_unit_uuids:
                            continue
                        cu_uuid = reg.register(
                            cu_syn, deterministic_uuid(case_id, "content_unit", cu_syn)
                        )
                        reg.content_unit_uuids[cu_syn] = cu_uuid
                        anchor = {
                            "parser_version": "v1",
                            "file_path": str(file_paths[min(cu_ord, len(file_paths) - 1)]),
                            "locator_id": cu_syn,
                        }
                        assert_no_forbidden_raw_keys(anchor, field_name="content_units.anchor")
                        cur.execute(
                            """
                            INSERT INTO public.content_units (
                                id, workspace_id, source_object_revision_id, unit_type,
                                anchor, content_hash, canonicalization_version, ordinal,
                                text_content, content_ref, retention_class, expires_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, 'DIFF_HUNK',
                                %s::jsonb, %s, 'v1', %s,
                                NULL, NULL, 'NONE', NULL
                            )
                            """,
                            (
                                cu_uuid,
                                ws_uuid,
                                rev_uuid,
                                json.dumps(anchor, sort_keys=True),
                                _sha256_tag(f"cu:{case_id}:{cu_syn}"),
                                p_idx * 10 + cu_ord,
                            ),
                        )

            # 9. Initial WorkEvents, Memberships, Immutable Versions, Composite Memberships,
            #    Pairwise CANNOT_LINK Constraints, and Dependent Facts/Claims
            non_composites = [
                ev
                for ev in initial_events
                if str(ev.get("anchor_mode")) != WorkEventAnchorMode.USER_COMPOSITE.value
            ]
            composites = [
                ev
                for ev in initial_events
                if str(ev.get("anchor_mode")) == WorkEventAnchorMode.USER_COMPOSITE.value
            ]
            init_ev_by_id: dict[str, dict[str, Any]] = {
                str(ev["event_id"]): ev for ev in initial_events
            }

            def _derive_event_title(ev: dict[str, Any]) -> str:
                if isinstance(ev.get("title"), str) and ev["title"].strip():
                    return ev["title"].strip()
                pr_syn = ev.get("primary_pr_id")
                if pr_syn and pr_syn in pr_by_id:
                    return str(pr_by_id[pr_syn]["title"])
                cmt_ids = ev.get("source_commit_ids") or []
                if cmt_ids and cmt_ids[0] in commit_by_id:
                    return _first_line_hint(
                        commit_by_id[cmt_ids[0]].get("message"),
                        fallback=f"Event {ev['event_id']}",
                    )
                member_ids = ev.get("member_event_ids") or []
                if member_ids:
                    member_titles: list[str] = []
                    for mid in member_ids:
                        mev = init_ev_by_id.get(mid)
                        if mev is not None:
                            member_titles.append(_derive_event_title(mev))
                    if member_titles:
                        return " + ".join(member_titles)[:200]
                return f"Synthetic Event {ev['event_id']}"

            def _derive_event_times(ev: dict[str, Any]) -> tuple[datetime, datetime]:
                pr_syn = ev.get("primary_pr_id")
                if pr_syn and pr_syn in pr_by_id and pr_by_id[pr_syn].get("merged_at"):
                    end_ts = _parse_iso_utc(pr_by_id[pr_syn]["merged_at"])
                    return end_ts - timedelta(hours=1), end_ts
                cmt_ids = ev.get("source_commit_ids") or []
                ts_list = [
                    _parse_iso_utc(commit_by_id[cid].get("committed_at"))
                    for cid in cmt_ids
                    if cid in commit_by_id and commit_by_id[cid].get("committed_at")
                ]
                if ts_list:
                    return min(ts_list), max(ts_list)
                return _FIXED_BASE_TIME - timedelta(hours=1), _FIXED_BASE_TIME

            for ev in [*non_composites, *composites]:
                ev_syn = str(ev["event_id"])
                ws_syn = str(ev["workspace_id"])
                repo_syn = str(ev["repo_id"])
                ws_uuid = reg.workspace_uuids[ws_syn]
                repo_uuid = reg.repo_uuids[repo_syn]
                ev_uuid = reg.register(ev_syn, deterministic_uuid(case_id, "work_event", ev_syn))
                reg.event_uuids[ev_syn] = ev_uuid

                anchor_mode = str(ev["anchor_mode"])
                status = str(ev["lifecycle_status"])
                version = int(ev["version"])
                title = _derive_event_title(ev)
                summary_text = (
                    str(ev["summary"]).strip()
                    if isinstance(ev.get("summary"), str) and ev["summary"].strip()
                    else None
                )
                start_ts, end_ts = _derive_event_times(ev)

                primary_anchor_uuid: str | None = None
                if anchor_mode in (WorkEventAnchorMode.PR.value, WorkEventAnchorMode.PR_SEGMENT.value):
                    primary_anchor_uuid = reg.pr_source_object_uuids[str(ev["primary_pr_id"])]
                elif anchor_mode == WorkEventAnchorMode.DIRECT_COMMIT.value:
                    cmt_ids = ev.get("source_commit_ids") or []
                    primary_anchor_uuid = reg.commit_source_object_uuids[str(cmt_ids[0])]

                summary_meta: dict[str, Any] = {
                    "source_resource_id": repo_uuid,
                    "merge_origin": str(ev.get("merge_origin") or "NONE"),
                }
                if ev.get("prior_review_status"):
                    summary_meta["prior_review_status"] = str(ev["prior_review_status"])
                assert_no_forbidden_raw_keys(summary_meta, field_name="work_events.summary_metadata")

                created_by = "USER" if anchor_mode == WorkEventAnchorMode.USER_COMPOSITE.value else "RULE"
                cur.execute(
                    """
                    INSERT INTO public.work_events (
                        id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                        anchor_scope, version, title, problem, action, outcome,
                        summary_metadata, status, created_by,
                        occurred_start_at, occurred_end_at, created_at, updated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        NULL, %s, %s, NULL, NULL, %s,
                        %s::jsonb, %s, %s,
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        ev_uuid,
                        ws_uuid,
                        primary_anchor_uuid,
                        anchor_mode,
                        version,
                        title,
                        summary_text,
                        json.dumps(summary_meta, sort_keys=True),
                        status,
                        created_by,
                        start_ts,
                        end_ts,
                        start_ts,
                        end_ts,
                    ),
                )

                # Seed event_memberships ONLY on non-USER_COMPOSITE events (T-304B invariant!)
                if anchor_mode != WorkEventAnchorMode.USER_COMPOSITE.value:
                    if primary_anchor_uuid is not None:
                        cur.execute(
                            """
                            INSERT INTO public.event_memberships (
                                id, workspace_id, event_id, source_object_id,
                                relation_class, relation_type, origin, rationale_code, status, created_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s, 'PRIMARY_ANCHOR', %s, 'SEED_PRIMARY_ANCHOR', 'ACTIVE', %s
                            )
                            ON CONFLICT (event_id, source_object_id, relation_type) DO NOTHING
                            """,
                            (
                                deterministic_uuid(case_id, "em_primary", f"{ev_syn}:{primary_anchor_uuid}"),
                                ws_uuid,
                                ev_uuid,
                                primary_anchor_uuid,
                                EventMembershipRelationClass.MUST_LINK.value,
                                EventMembershipOrigin.SOURCE.value,
                                start_ts,
                            ),
                        )
                    for cmt_syn in ev.get("source_commit_ids") or []:
                        cmt_uuid = reg.commit_source_object_uuids[str(cmt_syn)]
                        if cmt_uuid == primary_anchor_uuid:
                            continue
                        cur.execute(
                            """
                            INSERT INTO public.event_memberships (
                                id, workspace_id, event_id, source_object_id,
                                relation_class, relation_type, origin, rationale_code, status, created_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s, 'PR_COMMIT', %s, 'SEED_PR_COMMIT', 'ACTIVE', %s
                            )
                            ON CONFLICT (event_id, source_object_id, relation_type) DO NOTHING
                            """,
                            (
                                deterministic_uuid(case_id, "em_commit", f"{ev_syn}:{cmt_syn}"),
                                ws_uuid,
                                ev_uuid,
                                cmt_uuid,
                                EventMembershipRelationClass.MUST_LINK.value,
                                EventMembershipOrigin.SOURCE.value,
                                start_ts,
                            ),
                        )
                    for tkt_syn in ev.get("context_ticket_ids") or []:
                        tkt_uuid = reg.ticket_source_object_uuids[str(tkt_syn)]
                        cur.execute(
                            """
                            INSERT INTO public.event_memberships (
                                id, workspace_id, event_id, source_object_id,
                                relation_class, relation_type, origin, rationale_code, status, created_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                                %s, 'ISSUE_REFERENCE', %s, 'SEED_CONTEXT_TICKET', 'ACTIVE', %s
                            )
                            ON CONFLICT (event_id, source_object_id, relation_type) DO NOTHING
                            """,
                            (
                                deterministic_uuid(case_id, "em_ticket", f"{ev_syn}:{tkt_syn}"),
                                ws_uuid,
                                ev_uuid,
                                tkt_uuid,
                                EventMembershipRelationClass.CONTEXT_ONLY.value,
                                EventMembershipOrigin.SOURCE.value,
                                start_ts,
                            ),
                        )

                # Seed immutable work_event_versions v1..vN
                prior_status = str(ev.get("prior_review_status") or "CANDIDATE")
                for ver in range(1, version + 1):
                    ver_status = status if ver == version else prior_status
                    membership_snap: dict[str, Any] = {
                        "anchor_mode": anchor_mode,
                        "must_link_count": (
                            0
                            if anchor_mode == WorkEventAnchorMode.USER_COMPOSITE.value
                            else len(ev.get("source_commit_ids") or []) + (1 if primary_anchor_uuid else 0)
                        ),
                        "context_only_count": len(ev.get("context_ticket_ids") or []),
                        "member_event_ids": [
                            reg.event_uuids[m]
                            for m in (ev.get("member_event_ids") or [])
                            if m in reg.event_uuids
                        ],
                    }
                    assert_no_forbidden_raw_keys(
                        membership_snap, field_name="work_event_versions.membership_snapshot"
                    )
                    cur.execute(
                        """
                        INSERT INTO public.work_event_versions (
                            id, workspace_id, event_id, version, anchor_mode,
                            primary_anchor_source_object_id, anchor_scope,
                            title, problem, action, outcome,
                            summary_metadata, status, created_by,
                            created_by_review_action_id, membership_snapshot,
                            occurred_start_at, occurred_end_at, created_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, %s,
                            %s::uuid, NULL,
                            %s, NULL, NULL, %s,
                            %s::jsonb, %s, %s,
                            NULL, %s::jsonb,
                            %s, %s, %s
                        )
                        """,
                        (
                            deterministic_uuid(case_id, "wev", f"{ev_syn}:v{ver}"),
                            ws_uuid,
                            ev_uuid,
                            ver,
                            anchor_mode,
                            primary_anchor_uuid,
                            title,
                            summary_text,
                            json.dumps(summary_meta, sort_keys=True),
                            ver_status,
                            created_by,
                            json.dumps(membership_snap, sort_keys=True),
                            start_ts,
                            end_ts,
                            start_ts + timedelta(seconds=ver),
                        ),
                    )

                # Seed event_composite_memberships for USER_COMPOSITE initial events
                if anchor_mode == WorkEventAnchorMode.USER_COMPOSITE.value:
                    comp_mem_status = str(ev.get("composite_membership_status") or "ACTIVE")
                    for ordinal, m_syn in enumerate(ev.get("member_event_ids") or []):
                        m_uuid = reg.event_uuids[str(m_syn)]
                        m_ev = init_ev_by_id[str(m_syn)]
                        m_pre_merge_ver = (
                            max(1, int(m_ev["version"]) - 1)
                            if str(m_ev["lifecycle_status"]) == "SUPERSEDED"
                            else int(m_ev["version"])
                        )
                        cur.execute(
                            """
                            INSERT INTO public.event_composite_memberships (
                                id, workspace_id, composite_event_id, composite_event_version,
                                member_event_id, member_event_version, ordinal, status,
                                added_by_review_action_id, created_at, removed_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                %s::uuid, %s, %s, %s,
                                NULL, %s, %s
                            )
                            """,
                            (
                                deterministic_uuid(case_id, "ecm", f"{ev_syn}:{m_syn}"),
                                ws_uuid,
                                ev_uuid,
                                version,
                                m_uuid,
                                m_pre_merge_ver,
                                ordinal,
                                comp_mem_status,
                                end_ts,
                                end_ts if comp_mem_status == "REMOVED" else None,
                            ),
                        )

                # Seed dependent facts & claims (EV-012, CAND-EV-021)
                dep = ev.get("dependent_artifacts") or {}
                for f_syn in dep.get("fact_ids") or []:
                    f_uuid = reg.register(
                        str(f_syn), deterministic_uuid(case_id, "fact", str(f_syn))
                    )
                    reg.fact_uuids[str(f_syn)] = f_uuid
                    cur.execute(
                        """
                        INSERT INTO public.facts (
                            id, workspace_id, event_id, fact_type,
                            predicate, object_text, normalized_value, fact_metadata,
                            status, extractor_version, created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'ACTION',
                            'delivered', %s, NULL, '{}'::jsonb,
                            'SUPPORTED', 'eval-seed-v1', %s, %s
                        )
                        """,
                        (
                            f_uuid,
                            ws_uuid,
                            ev_uuid,
                            f"Synthetic dependent fact {f_syn}",
                            end_ts,
                            end_ts,
                        ),
                    )
                for clm_syn in dep.get("claim_ids") or []:
                    clm_uuid = reg.register(
                        str(clm_syn), deterministic_uuid(case_id, "claim", str(clm_syn))
                    )
                    reg.claim_uuids[str(clm_syn)] = clm_uuid
                    cur.execute(
                        """
                        INSERT INTO public.claims (
                            id, workspace_id, event_id, version,
                            claim_text, status, created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 1,
                            %s, 'APPROVED', %s, %s
                        )
                        """,
                        (
                            clm_uuid,
                            ws_uuid,
                            ev_uuid,
                            f"Synthetic dependent claim {clm_syn}",
                            end_ts,
                            end_ts,
                        ),
                    )

            # 10. Pairwise Event CANNOT_LINK constraints (0060 public.event_constraints, CAND-EV-021)
            for rel in event_relations:
                if str(rel.get("relation_type")) != "CANNOT_LINK":
                    continue
                ws_syn = str(rel["workspace_id"])
                ws_uuid = reg.workspace_uuids[ws_syn]
                a_uuid = reg.event_uuids[str(rel["source_event_id"])]
                b_uuid = reg.event_uuids[str(rel["target_event_id"])]
                left_id, right_id = canonicalize_event_pair(a_uuid, b_uuid)
                rel_meta = {"logical_relation_id": str(rel.get("relation_id") or "seed")}
                assert_no_forbidden_raw_keys(rel_meta, field_name="event_constraints.constraint_metadata")
                cur.execute(
                    """
                    INSERT INTO public.event_constraints (
                        id, workspace_id, left_event_id, right_event_id,
                        constraint_type, origin, status, rationale_code,
                        constraint_metadata, created_by_review_action_id, created_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        'CANNOT_LINK', 'USER', 'ACTIVE', 'EXPLICIT_CANNOT_LINK',
                        %s::jsonb, NULL, %s
                    )
                    """,
                    (
                        deterministic_uuid(case_id, "event_constraint", str(rel.get("relation_id") or f"{left_id}:{right_id}")),
                        ws_uuid,
                        left_id,
                        right_id,
                        json.dumps(rel_meta, sort_keys=True),
                        _FIXED_BASE_TIME,
                    ),
                )

            # Record initial source provenance counts for preservation verification (EV-011)
            for tbl in ("source_objects", "source_object_revisions", "content_units"):
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                row = cur.fetchone()
                reg.seeded_source_counts[tbl] = int(row[0]) if row is not None else 0

    return reg


__all__ = [
    "SeedIdRegistry",
    "deterministic_uuid",
    "seed_approved_case_into_pg18",
]
