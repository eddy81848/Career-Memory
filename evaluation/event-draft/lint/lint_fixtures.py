#!/usr/bin/env python3
"""Automated format, referential, governance, and official Event-domain invariant linter
for Career Memory T-001A Event synthetic dataset draft.

Zero external dependencies (Python 3 standard library only).
Does NOT perform any network, GitHub, Notion, or LLM API calls.
Does NOT represent M3/EVAL-EVENT runtime quality evaluation or T-001B human approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_SCOPES = {"MVP", "POST_MVP"}
ALLOWED_REVIEW_STATUSES = {"PENDING_HUMAN_REVIEW", "REVIEW_REQUIRED", "APPROVED_MVP_REGRESSION"}
ALLOWED_CASE_APPROVAL_STATUSES = {"DRAFT", "APPROVED_MVP_REGRESSION"}
ALLOWED_MANIFEST_APPROVAL_STATUSES = {"DRAFT", "PARTIALLY_APPROVED_MVP_REGRESSION"}
ALLOWED_EVAL_PARTITIONS = {"MVP_SYNTHETIC_REGRESSION", "POST_MVP_DRAFT_EXCLUDED"}
ALLOWED_SYNC_RUN_STATUSES = {"SUCCESS", "PARTIAL", "PENDING"}
ALLOWED_LOOKUP_STATUS = {"COMPLETED", "INCOMPLETE_OR_FAILED"}
ALLOWED_PR_ASSOC = {"ASSOCIATED", "NO_MATCH_OBSERVED", "UNKNOWN"}
ALLOWED_COMMAND_ACTIONS = {"split", "manual_merge", "exclude", "dissolve"}

ALLOWED_ANCHOR_MODES = {"PR", "PR_SEGMENT", "DIRECT_COMMIT", "USER_COMPOSITE"}
ALLOWED_LIFECYCLE_STATUSES = {"CANDIDATE", "CONFIRMED", "REJECTED", "SUPERSEDED", "STALE"}
FORBIDDEN_ARBITRARY_LIFECYCLE_STATUSES = {
    "ACTIVE",
    "EXCLUDED",
    "DISSOLVED",
    "active",
    "excluded",
    "dissolved",
    "superseded",
}
ALLOWED_MERGE_ORIGINS = {"NONE", "USER_MANUAL_MERGE"}
ALLOWED_MEMBERSHIP_ROLES = {
    "standalone",
    "user_composite_root",
    "subsumed_by_user_composite",
    "split_parent",
    "split_child",
}
ALLOWED_COMPOSITE_MEMBERSHIP_STATUSES = {None, "ACTIVE", "REMOVED"}

OFFICIAL_EVENT_API_ERROR_CODES = {
    "EVENT_VERSION_CONFLICT",
    "CROSS_WORKSPACE_MEMBER",
    "CROSS_REPOSITORY_MEMBER",
    "DUPLICATE_COMPOSITE_MEMBER",
    "NESTED_COMPOSITE_NOT_ALLOWED",
    "CANNOT_LINK_CONFLICT",
    "MEMBER_NOT_ACTIVE",
    "MEMBER_ALREADY_COMPOSED",
    "PR_ASSOCIATION_INCOMPLETE",
}
MERGE_ONLY_ERROR_CODES = {
    "CROSS_WORKSPACE_MEMBER",
    "CROSS_REPOSITORY_MEMBER",
    "DUPLICATE_COMPOSITE_MEMBER",
    "NESTED_COMPOSITE_NOT_ALLOWED",
    "MEMBER_ALREADY_COMPOSED",
}
ALLOWED_SPEC_ERROR_CODES = {None, "UNCONFIRMED"} | OFFICIAL_EVENT_API_ERROR_CODES
ALLOWED_REJECTION_CATEGORIES = {
    None,
    "non_event_or_unknown_member_rejected",
    "nested_composite_not_allowed",
    "member_already_composed_or_not_active",
    "member_not_active",
    "version_conflict",
    "cross_repository_member",
    "cross_workspace_merge_rejected",
    "cross_workspace_non_merge_rejected",
}
ALLOWED_DISPOSITIONS = {
    "hold_classification_unknown_assoc",
    "not_direct_commit_candidate_non_default_branch",
}
ALLOWED_HOLD_RECORD_KINDS = {"OBSERVATION_RECORD", "NON_ELIGIBLE_SOURCE_RECORD"}
ALLOWED_ARTIFACT_TYPES = {"FACT", "CLAIM", "EXPORT"}
ALLOWED_OFFICIAL_REQ_REFS = {"FR-006", "FR-007"}
ALLOWED_OFFICIAL_TEST_REFS = {"EVENT-10", "EVENT-11", "EVENT-12"}

FORBIDDEN_OD007_KEYS = {
    "physical_table",
    "table_name",
    "sql_schema",
    "foreign_key_column",
    "db_table",
    "ddl",
}

REQUIRED_CASE_KEYS = {
    "schema_version",
    "case_id",
    "slug",
    "title",
    "scope",
    "backlog_ref",
    "post_mvp_reason",
    "review_status",
    "coverage",
    "synthetic_sources",
    "user_commands",
    "expected_proposal",
    "rationale",
}

REQUIRED_COVERAGE_KEYS = {
    "draft_requirement_ids",
    "draft_test_ids",
    "official_requirement_refs",
    "official_test_refs",
    "scenario_tags",
}

REQUIRED_SOURCE_KEYS = {
    "workspaces",
    "repositories",
    "sync_runs",
    "pull_requests",
    "commits",
    "issues_tickets",
    "initial_events",
    "post_sync_observations",
}

REQUIRED_EXPECTED_KEYS = {
    "approval_status",
    "approved_by",
    "events",
    "held_items",
    "unpersisted_external_observations",
    "command_results",
    "downstream_side_effects",
    "search_checks",
    "aggregation_check",
    "review_required_items",
}

REQUIRED_COMMAND_KEYS = {
    "command_id",
    "command_action",
    "spec_command_name",
    "workspace_id",
    "actor_id",
    "expected_version",
    "target_event_ids",
    "target_versions",
    "payload",
}

REQUIRED_MANIFEST_KEYS = {
    "schema_version",
    "dataset_id",
    "dataset_version",
    "task_ref",
    "approval_status",
    "base_commit_sha",
    "approval_record",
    "scope_boundary",
    "generation_provenance",
    "reference_docs",
    "evaluation_bundles",
    "integrity_hashes",
    "cases",
    "negative_variants",
}

REAL_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/", re.IGNORECASE)
REAL_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class LintIssue:
    rule_code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.rule_code}] {self.location}: {self.message}"


def _load_json(path: Path) -> tuple[Any | None, list[LintIssue]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh), []
    except FileNotFoundError:
        return None, [LintIssue("FILE_NOT_FOUND", str(path), "Required JSON file does not exist.")]
    except json.JSONDecodeError as exc:
        return None, [LintIssue("JSON_PARSE_ERROR", str(path), f"Invalid JSON: {exc}")]


def _walk_keys_and_strings(obj: Any, location: str, issues: list[LintIssue]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_loc = f"{location}.{key}"
            if key in FORBIDDEN_OD007_KEYS:
                issues.append(
                    LintIssue(
                        "OD007_NO_PHYSICAL_TABLE",
                        child_loc,
                        f"Forbidden physical DB assumption key '{key}' violates OD-007 neutrality.",
                    )
                )
            _walk_keys_and_strings(value, child_loc, issues)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _walk_keys_and_strings(item, f"{location}[{idx}]", issues)
    elif isinstance(obj, str):
        if REAL_URL_RE.search(obj):
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    location,
                    "Real github.com URL detected; synthetic fixtures must not reference real repositories.",
                )
            )
        if REAL_EMAIL_RE.search(obj):
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    location,
                    "Non-synthetic email address detected; use @example.invalid if an email is required.",
                )
            )


def _check_unique(values: list[str], rule_code: str, location: str, label: str, issues: list[LintIssue]) -> None:
    seen: set[str] = set()
    for val in values:
        if val in seen:
            issues.append(LintIssue(rule_code, location, f"Duplicate {label}: '{val}'."))
        seen.add(val)


def _scope_token_set(scope_obj: dict[str, Any]) -> set[tuple[str, str]]:
    tokens: set[tuple[str, str]] = set()
    for field in ("file_paths", "diff_hunks", "review_thread_ids", "content_unit_ids"):
        vals = scope_obj.get(field)
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, str) and v:
                    tokens.add((field, v))
    return tokens


def _validate_event_official_tokens(
    ev: dict[str, Any],
    loc: str,
    issues: list[LintIssue],
) -> None:
    anchor = ev.get("anchor_mode")
    status = ev.get("lifecycle_status")
    active_top = ev.get("is_active_top_level")
    role = ev.get("membership_role")

    if anchor in {"issue", "ticket", "context"}:
        issues.append(
            LintIssue(
                "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                f"{loc}.anchor_mode",
                f"Issue/ticket cannot be a standalone Event anchor_mode ({anchor!r}).",
            )
        )
    elif anchor not in ALLOWED_ANCHOR_MODES:
        issues.append(
            LintIssue(
                "OFFICIAL_ANCHOR_MODE",
                f"{loc}.anchor_mode",
                f"Invalid anchor_mode {anchor!r}; official values are {sorted(ALLOWED_ANCHOR_MODES)}.",
            )
        )

    if status in FORBIDDEN_ARBITRARY_LIFECYCLE_STATUSES:
        issues.append(
            LintIssue(
                "OFFICIAL_LIFECYCLE_STATUS",
                f"{loc}.lifecycle_status",
                f"Arbitrary lifecycle_status {status!r} is forbidden; use official {sorted(ALLOWED_LIFECYCLE_STATUSES)} and separate is_active_top_level.",
            )
        )
    elif status not in ALLOWED_LIFECYCLE_STATUSES:
        issues.append(
            LintIssue(
                "OFFICIAL_LIFECYCLE_STATUS",
                f"{loc}.lifecycle_status",
                f"Invalid lifecycle_status {status!r}; official values are {sorted(ALLOWED_LIFECYCLE_STATUSES)}.",
            )
        )

    if not isinstance(active_top, bool):
        issues.append(
            LintIssue(
                "OFFICIAL_LIFECYCLE_STATUS",
                f"{loc}.is_active_top_level",
                "is_active_top_level must be a boolean separate from official lifecycle_status.",
            )
        )

    if role not in ALLOWED_MEMBERSHIP_ROLES:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{loc}.membership_role",
                f"Invalid membership_role {role!r}.",
            )
        )

    comp_mem_status = ev.get("composite_membership_status")
    if comp_mem_status not in ALLOWED_COMPOSITE_MEMBERSHIP_STATUSES:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{loc}.composite_membership_status",
                f"Invalid composite_membership_status {comp_mem_status!r}.",
            )
        )

    if status in {"REJECTED", "SUPERSEDED"}:
        if (
            active_top is not False
            or ev.get("searchable_as_top_level") is not False
            or ev.get("counted_in_metrics") is not False
        ):
            issues.append(
                LintIssue(
                    "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                    loc,
                    f"Event '{ev.get('event_id')}' with lifecycle_status={status!r} must have is_active_top_level=false, searchable_as_top_level=false, and counted_in_metrics=false.",
                )
            )

    if active_top is False and (
        ev.get("searchable_as_top_level") is True or ev.get("counted_in_metrics") is True
    ):
        issues.append(
            LintIssue(
                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                loc,
                f"Non-active Event '{ev.get('event_id')}' cannot be searchable_as_top_level or counted_in_metrics.",
            )
        )


def lint_case_data(case: Any, location: str) -> list[LintIssue]:
    issues: list[LintIssue] = []

    if not isinstance(case, dict):
        return [LintIssue("SCHEMA_REQUIRED_FIELD", location, "Case root must be a JSON object.")]

    # 1. Top-level required keys
    for key in sorted(REQUIRED_CASE_KEYS - set(case.keys())):
        issues.append(
            LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.{key}", f"Missing required top-level field '{key}'.")
        )

    _walk_keys_and_strings(case, location, issues)

    if case.get("schema_version") != "event-draft-case.v1":
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.schema_version",
                f"Expected schema_version 'event-draft-case.v1', got {case.get('schema_version')!r}.",
            )
        )

    # 2. Coverage & Scope / B-003 policy
    scope = case.get("scope")
    backlog_ref = case.get("backlog_ref")
    post_mvp_reason = case.get("post_mvp_reason")
    coverage = case.get("coverage")
    if not isinstance(coverage, dict):
        issues.append(LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.coverage", "coverage must be an object."))
        coverage = {}
    else:
        for ck in sorted(REQUIRED_COVERAGE_KEYS - set(coverage.keys())):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.coverage.{ck}",
                    f"Missing required coverage field '{ck}' (draft local IDs and official refs must be separated).",
                )
            )
        for ref in coverage.get("official_requirement_refs", []):
            if ref not in ALLOWED_OFFICIAL_REQ_REFS:
                issues.append(
                    LintIssue(
                        "SCHEMA_REQUIRED_FIELD",
                        f"{location}.coverage.official_requirement_refs",
                        f"Unsupported official_requirement_ref {ref!r}; allowed: {sorted(ALLOWED_OFFICIAL_REQ_REFS)}.",
                    )
                )
        for tref in coverage.get("official_test_refs", []):
            if tref not in ALLOWED_OFFICIAL_TEST_REFS:
                issues.append(
                    LintIssue(
                        "SCHEMA_REQUIRED_FIELD",
                        f"{location}.coverage.official_test_refs",
                        f"Unsupported official_test_ref {tref!r}; allowed: {sorted(ALLOWED_OFFICIAL_TEST_REFS)}.",
                    )
                )

    scenario_tags = coverage.get("scenario_tags") if isinstance(coverage.get("scenario_tags"), list) else []

    if scope not in ALLOWED_SCOPES:
        issues.append(
            LintIssue("SCOPE_TAG_POLICY", f"{location}.scope", f"Invalid scope {scope!r}; must be MVP or POST_MVP.")
        )
    if case.get("review_status") not in ALLOWED_REVIEW_STATUSES:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.review_status",
                f"Invalid review_status {case.get('review_status')!r}.",
            )
        )

    sources = case.get("synthetic_sources")
    if not isinstance(sources, dict):
        issues.append(LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.synthetic_sources", "Must be an object."))
        sources = {}
    else:
        for key in sorted(REQUIRED_SOURCE_KEYS - set(sources.keys())):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.synthetic_sources.{key}",
                    f"Missing required synthetic_sources field '{key}'.",
                )
            )

    post_obs = sources.get("post_sync_observations") if isinstance(sources.get("post_sync_observations"), list) else []
    mentions_b003 = (
        backlog_ref == "B-003"
        or "b_003" in scenario_tags
        or any(
            isinstance(obs, dict) and isinstance(obs.get("details"), dict) and obs["details"].get("backlog_ref") == "B-003"
            for obs in post_obs
        )
    )
    if mentions_b003 and scope != "POST_MVP":
        issues.append(
            LintIssue(
                "SCOPE_TAG_POLICY",
                f"{location}.scope",
                "B-003 post-success change discovery / late association recovery MUST be tagged scope='POST_MVP'.",
            )
        )
    if scope == "POST_MVP":
        if not backlog_ref:
            issues.append(
                LintIssue("SCOPE_TAG_POLICY", f"{location}.backlog_ref", "POST_MVP case must specify backlog_ref.")
            )
        if not post_mvp_reason:
            issues.append(
                LintIssue(
                    "SCOPE_TAG_POLICY",
                    f"{location}.post_mvp_reason",
                    "POST_MVP case must provide non-empty post_mvp_reason.",
                )
            )

    # 3. Rationale separation
    rationale = case.get("rationale")
    if not isinstance(rationale, dict):
        if "rationale" in case:
            issues.append(LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.rationale", "rationale must be an object."))
    else:
        for rk in ("summary", "confirmed_spec_rules", "unconfirmed_spec_tokens", "truly_undecided_items"):
            if rk not in rationale:
                issues.append(
                    LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.rationale.{rk}", f"Missing rationale field '{rk}'.")
                )

    # 4. Collect and validate synthetic_sources
    workspaces = sources.get("workspaces") if isinstance(sources.get("workspaces"), list) else []
    repos = sources.get("repositories") if isinstance(sources.get("repositories"), list) else []
    sync_runs = sources.get("sync_runs") if isinstance(sources.get("sync_runs"), list) else []
    prs = sources.get("pull_requests") if isinstance(sources.get("pull_requests"), list) else []
    commits = sources.get("commits") if isinstance(sources.get("commits"), list) else []
    tickets = sources.get("issues_tickets") if isinstance(sources.get("issues_tickets"), list) else []
    initial_events = sources.get("initial_events") if isinstance(sources.get("initial_events"), list) else []

    ws_ids = [w["workspace_id"] for w in workspaces if isinstance(w, dict) and "workspace_id" in w]
    repo_map: dict[str, dict[str, Any]] = {}
    sync_run_map: dict[str, dict[str, Any]] = {}
    partial_sync_run_ids: set[str] = set()
    repo_sync_statuses: dict[str, set[str]] = {}
    pr_map: dict[str, dict[str, Any]] = {}
    commit_map: dict[str, dict[str, Any]] = {}
    ticket_map: dict[str, dict[str, Any]] = {}
    init_event_map: dict[str, dict[str, Any]] = {}
    obs_map: dict[str, dict[str, Any]] = {}

    _check_unique(ws_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.workspaces", "workspace_id", issues)
    ws_id_set = set(ws_ids)

    for idx, ws in enumerate(workspaces):
        if isinstance(ws, dict):
            if ws.get("synthetic") is not True:
                issues.append(
                    LintIssue(
                        "SYNTHETIC_PRIVACY_GUARD",
                        f"{location}.synthetic_sources.workspaces[{idx}]",
                        "Workspace must declare synthetic=true.",
                    )
                )
            if not str(ws.get("workspace_id", "")).startswith("ws_syn_"):
                issues.append(
                    LintIssue(
                        "SYNTHETIC_PRIVACY_GUARD",
                        f"{location}.synthetic_sources.workspaces[{idx}].workspace_id",
                        "workspace_id must start with synthetic prefix 'ws_syn_'.",
                    )
                )

    repo_ids: list[str] = []
    for idx, repo in enumerate(repos):
        if not isinstance(repo, dict):
            continue
        rid = repo.get("repo_id")
        if rid:
            repo_ids.append(rid)
            repo_map[rid] = repo
        if repo.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.repositories[{idx}].workspace_id",
                    f"Unknown workspace_id '{repo.get('workspace_id')}'.",
                )
            )
        if repo.get("synthetic") is not True:
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    f"{location}.synthetic_sources.repositories[{idx}]",
                    "Repository must declare synthetic=true.",
                )
            )
        if not str(repo.get("full_name", "")).startswith("acme-syn-"):
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    f"{location}.synthetic_sources.repositories[{idx}].full_name",
                    "Repository full_name must use synthetic org prefix 'acme-syn-'.",
                )
            )
    _check_unique(repo_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.repositories", "repo_id", issues)

    sync_ids: list[str] = []
    for idx, sr in enumerate(sync_runs):
        if not isinstance(sr, dict):
            continue
        sid = sr.get("sync_run_id")
        if sid:
            sync_ids.append(sid)
            sync_run_map[sid] = sr
        sr_status = sr.get("status")
        if sr_status not in ALLOWED_SYNC_RUN_STATUSES:
            issues.append(
                LintIssue(
                    "OFFICIAL_IMPORT_RUN_STATUS",
                    f"{location}.synthetic_sources.sync_runs[{idx}].status",
                    f"Invalid sync_run status {sr_status!r}; official statuses are {sorted(ALLOWED_SYNC_RUN_STATUSES)} (PARTIAL_SUCCESS is forbidden).",
                )
            )
        promotes = sr.get("promotes_events")
        if not isinstance(promotes, bool):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.synthetic_sources.sync_runs[{idx}].promotes_events",
                    "sync_run must declare boolean promotes_events.",
                )
            )
        elif sr_status == "PARTIAL" and promotes is not False:
            issues.append(
                LintIssue(
                    "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                    f"{location}.synthetic_sources.sync_runs[{idx}].promotes_events",
                    "PARTIAL Import Run MUST set promotes_events=false (promotes 0 Events).",
                )
            )
        if sr_status == "PARTIAL" and sid:
            partial_sync_run_ids.add(sid)

        if sr.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.sync_runs[{idx}].workspace_id",
                    f"Unknown workspace_id '{sr.get('workspace_id')}'.",
                )
            )
        rid = sr.get("repo_id")
        if rid not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.sync_runs[{idx}].repo_id",
                    f"Unknown repo_id '{rid}'.",
                )
            )
        else:
            repo_sync_statuses.setdefault(rid, set()).add(str(sr_status))
            if repo_map[rid].get("workspace_id") != sr.get("workspace_id"):
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.sync_runs[{idx}]",
                        "sync_run.workspace_id does not match repo.workspace_id.",
                    )
                )
    _check_unique(sync_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.sync_runs", "sync_run_id", issues)

    ticket_ids: list[str] = []
    for idx, tkt in enumerate(tickets):
        if not isinstance(tkt, dict):
            continue
        tid = tkt.get("ticket_id")
        if tid:
            ticket_ids.append(tid)
            ticket_map[tid] = tkt
        if tkt.get("repo_id") not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.issues_tickets[{idx}].repo_id",
                    f"Unknown repo_id '{tkt.get('repo_id')}'.",
                )
            )
        if tkt.get("relation_role") != "CONTEXT_ONLY":
            issues.append(
                LintIssue(
                    "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                    f"{location}.synthetic_sources.issues_tickets[{idx}].relation_role",
                    "Issues/tickets must have relation_role='CONTEXT_ONLY'.",
                )
            )
    _check_unique(ticket_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.issues_tickets", "ticket_id", issues)

    pr_ids: list[str] = []
    for idx, pr in enumerate(prs):
        if not isinstance(pr, dict):
            continue
        pid = pr.get("pr_id")
        if pid:
            pr_ids.append(pid)
            pr_map[pid] = pr
        if pr.get("repo_id") not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.pull_requests[{idx}].repo_id",
                    f"Unknown repo_id '{pr.get('repo_id')}'.",
                )
            )
        pr_sr = pr.get("source_sync_run_id")
        if pr_sr is not None and pr_sr not in sync_run_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.pull_requests[{idx}].source_sync_run_id",
                    f"Unknown source_sync_run_id '{pr_sr}'.",
                )
            )
        if not str(pr.get("author_handle", "")).startswith("syn-"):
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    f"{location}.synthetic_sources.pull_requests[{idx}].author_handle",
                    "author_handle must start with synthetic prefix 'syn-'.",
                )
            )
    _check_unique(pr_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.pull_requests", "pr_id", issues)

    commit_ids: list[str] = []
    unknown_commit_ids: set[str] = set()
    for idx, cmt in enumerate(commits):
        if not isinstance(cmt, dict):
            continue
        cid = cmt.get("commit_id")
        if cid:
            commit_ids.append(cid)
            commit_map[cid] = cmt
        rid = cmt.get("repo_id")
        if rid not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.commits[{idx}].repo_id",
                    f"Unknown repo_id '{rid}'.",
                )
            )
        else:
            default_branch = repo_map[rid].get("default_branch")
            expected_on_default = cmt.get("branch_name") == default_branch
            if cmt.get("on_default_branch") != expected_on_default:
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.commits[{idx}].on_default_branch",
                        f"on_default_branch={cmt.get('on_default_branch')} disagrees with branch_name={cmt.get('branch_name')!r} vs repo default_branch={default_branch!r}.",
                    )
                )

        if not isinstance(cmt.get("default_branch_verified_at_start_and_end"), bool):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.synthetic_sources.commits[{idx}].default_branch_verified_at_start_and_end",
                    "Commit must declare boolean default_branch_verified_at_start_and_end.",
                )
            )
        elif cmt.get("default_branch_verified_at_start_and_end") is True and cmt.get("on_default_branch") is not True:
            issues.append(
                LintIssue(
                    "BRANCH_ASSOCIATION_RULES",
                    f"{location}.synthetic_sources.commits[{idx}].default_branch_verified_at_start_and_end",
                    "default_branch_verified_at_start_and_end cannot be true when on_default_branch is false.",
                )
            )

        cmt_sr = cmt.get("source_sync_run_id")
        if cmt_sr is not None and cmt_sr not in sync_run_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.commits[{idx}].source_sync_run_id",
                    f"Unknown source_sync_run_id '{cmt_sr}'.",
                )
            )

        lookup_status = cmt.get("pr_association_lookup_status")
        assoc = cmt.get("pr_association_status")
        assoc_pr = cmt.get("associated_pr_id")
        if lookup_status not in ALLOWED_LOOKUP_STATUS:
            issues.append(
                LintIssue(
                    "BRANCH_ASSOCIATION_RULES",
                    f"{location}.synthetic_sources.commits[{idx}].pr_association_lookup_status",
                    f"Invalid pr_association_lookup_status {lookup_status!r}; must be COMPLETED or INCOMPLETE_OR_FAILED.",
                )
            )
        if assoc not in ALLOWED_PR_ASSOC:
            issues.append(
                LintIssue(
                    "BRANCH_ASSOCIATION_RULES",
                    f"{location}.synthetic_sources.commits[{idx}].pr_association_status",
                    f"Invalid pr_association_status {assoc!r}; must be ASSOCIATED, NO_MATCH_OBSERVED, or UNKNOWN.",
                )
            )
        elif assoc == "ASSOCIATED":
            if lookup_status != "COMPLETED":
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.commits[{idx}].pr_association_lookup_status",
                        "ASSOCIATED commit must have pr_association_lookup_status='COMPLETED'.",
                    )
                )
            if not assoc_pr or assoc_pr not in pr_map:
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.commits[{idx}].associated_pr_id",
                        f"ASSOCIATED commit must reference an existing PR; got {assoc_pr!r}.",
                    )
                )
        elif assoc == "NO_MATCH_OBSERVED":
            if lookup_status != "COMPLETED":
                issues.append(
                    LintIssue(
                        "INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED",
                        f"{location}.synthetic_sources.commits[{idx}].pr_association_lookup_status",
                        "NO_MATCH_OBSERVED is only valid when pr_association_lookup_status='COMPLETED'.",
                    )
                )
            if assoc_pr is not None:
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.commits[{idx}].associated_pr_id",
                        "Commit with NO_MATCH_OBSERVED must have associated_pr_id=null.",
                    )
                )
        else:  # UNKNOWN
            if assoc_pr is not None:
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.commits[{idx}].associated_pr_id",
                        "Commit with UNKNOWN must have associated_pr_id=null.",
                    )
                )
            if cid:
                unknown_commit_ids.add(cid)

        if not str(cmt.get("author_handle", "")).startswith("syn-"):
            issues.append(
                LintIssue(
                    "SYNTHETIC_PRIVACY_GUARD",
                    f"{location}.synthetic_sources.commits[{idx}].author_handle",
                    "author_handle must start with synthetic prefix 'syn-'.",
                )
            )
    _check_unique(commit_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.commits", "commit_id", issues)

    for idx, pr in enumerate(prs):
        if not isinstance(pr, dict):
            continue
        for cid in pr.get("commit_ids", []):
            if cid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.pull_requests[{idx}].commit_ids",
                        f"PR references unknown commit_id '{cid}'.",
                    )
                )
            elif commit_map[cid].get("associated_pr_id") != pr.get("pr_id"):
                issues.append(
                    LintIssue(
                        "BRANCH_ASSOCIATION_RULES",
                        f"{location}.synthetic_sources.pull_requests[{idx}].commit_ids",
                        f"Commit '{cid}' in PR '{pr.get('pr_id')}' does not point back via associated_pr_id.",
                    )
                )
        for tid in pr.get("referenced_ticket_ids", []):
            if tid not in ticket_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.pull_requests[{idx}].referenced_ticket_ids",
                        f"PR references unknown ticket_id '{tid}'.",
                    )
                )

    obs_ids: list[str] = []
    for obs in post_obs:
        if isinstance(obs, dict) and obs.get("observation_id"):
            obs_ids.append(obs["observation_id"])
            obs_map[obs["observation_id"]] = obs
    _check_unique(obs_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.post_sync_observations", "observation_id", issues)

    # Initial events
    init_event_ids: list[str] = []
    for idx, ev in enumerate(initial_events):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("event_id")
        if eid:
            init_event_ids.append(eid)
            init_event_map[eid] = ev
        ev_loc = f"{location}.synthetic_sources.initial_events[{idx}]"
        _validate_event_official_tokens(ev, ev_loc, issues)

        if ev.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.workspace_id",
                    f"Unknown workspace_id '{ev.get('workspace_id')}'.",
                )
            )
        rid = ev.get("repo_id")
        if rid not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.repo_id",
                    f"Unknown repo_id '{rid}'.",
                )
            )
        elif repo_map[rid].get("workspace_id") != ev.get("workspace_id"):
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    ev_loc,
                    "initial_event.workspace_id does not match repo.workspace_id.",
                )
            )
        src_sr = ev.get("source_sync_run_id")
        if src_sr is not None:
            if src_sr not in sync_run_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.source_sync_run_id",
                        f"Unknown source_sync_run_id '{src_sr}'.",
                    )
                )
            elif src_sr in partial_sync_run_ids:
                issues.append(
                    LintIssue(
                        "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                        f"{ev_loc}.source_sync_run_id",
                        f"initial_event '{eid}' cannot be promoted from PARTIAL run '{src_sr}'.",
                    )
                )
        elif rid in repo_sync_statuses and repo_sync_statuses[rid] == {"PARTIAL"}:
            issues.append(
                LintIssue(
                    "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                    ev_loc,
                    f"initial_event '{eid}' exists on repo '{rid}' whose only sync_run is PARTIAL; separate an independent SUCCESS precondition run.",
                )
            )

        if ev.get("primary_pr_id") is not None and ev.get("primary_pr_id") not in pr_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.primary_pr_id",
                    f"Unknown primary_pr_id '{ev.get('primary_pr_id')}'.",
                )
            )
        for cid in ev.get("source_commit_ids", []):
            if cid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.source_commit_ids",
                        f"Unknown commit_id '{cid}'.",
                    )
                )
        for tid in ev.get("context_ticket_ids", []):
            if tid not in ticket_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.context_ticket_ids",
                        f"Unknown ticket_id '{tid}'.",
                    )
                )
        dep_arts = ev.get("dependent_artifacts")
        if not isinstance(dep_arts, dict):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{ev_loc}.dependent_artifacts",
                    "initial_event must declare dependent_artifacts object with fact_ids, claim_ids, export_ids.",
                )
            )
        else:
            for ak in ("fact_ids", "claim_ids", "export_ids"):
                if ak not in dep_arts or not isinstance(dep_arts.get(ak), list):
                    issues.append(
                        LintIssue(
                            "SCHEMA_REQUIRED_FIELD",
                            f"{ev_loc}.dependent_artifacts.{ak}",
                            f"Missing list dependent_artifacts.{ak}.",
                        )
                    )
    _check_unique(init_event_ids, "CASE_UNIQUENESS", f"{location}.synthetic_sources.initial_events", "event_id", issues)

    for idx, ev in enumerate(initial_events):
        if not isinstance(ev, dict):
            continue
        for mid in ev.get("member_event_ids", []):
            if mid not in init_event_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.initial_events[{idx}].member_event_ids",
                        f"Unknown member_event_id '{mid}'.",
                    )
                )
            elif ev.get("anchor_mode") == "USER_COMPOSITE" and ev.get("is_active_top_level") is True:
                if init_event_map[mid].get("lifecycle_status") != "SUPERSEDED":
                    issues.append(
                        LintIssue(
                            "INVARIANT_SUPERSEDED_HISTORY",
                            f"{location}.synthetic_sources.initial_events[{mid}].lifecycle_status",
                            f"Active initial USER_COMPOSITE member '{mid}' MUST have lifecycle_status='SUPERSEDED'.",
                        )
                    )

    # 5. Validate eval_grouping, expected_proposal top-level & T-001B approval governance
    case_id_str = str(case.get("case_id", ""))
    eval_grp = case.get("eval_grouping")
    if case_id_str.startswith("EV-"):
        if not isinstance(eval_grp, dict):
            issues.append(
                LintIssue(
                    "EVAL_BUNDLE_LINEAGE_ISOLATION",
                    f"{location}.eval_grouping",
                    "EV-* case must declare eval_grouping (eval_bundle_id, source_lineage_group, eval_partition, derived_from_same_source_cases).",
                )
            )
        else:
            epart = eval_grp.get("eval_partition")
            if epart not in ALLOWED_EVAL_PARTITIONS:
                issues.append(
                    LintIssue(
                        "EVAL_BUNDLE_LINEAGE_ISOLATION",
                        f"{location}.eval_grouping.eval_partition",
                        f"Invalid eval_partition {epart!r}; allowed: {sorted(ALLOWED_EVAL_PARTITIONS)}.",
                    )
                )
            elif scope == "MVP" and epart != "MVP_SYNTHETIC_REGRESSION":
                issues.append(
                    LintIssue(
                        "EVAL_BUNDLE_LINEAGE_ISOLATION",
                        f"{location}.eval_grouping.eval_partition",
                        "MVP case must belong to eval_partition='MVP_SYNTHETIC_REGRESSION'.",
                    )
                )
            elif scope == "POST_MVP" and epart != "POST_MVP_DRAFT_EXCLUDED":
                issues.append(
                    LintIssue(
                        "EVAL_BUNDLE_LINEAGE_ISOLATION",
                        f"{location}.eval_grouping.eval_partition",
                        "POST_MVP case must belong to eval_partition='POST_MVP_DRAFT_EXCLUDED'.",
                    )
                )
            same_src = eval_grp.get("derived_from_same_source_cases")
            if not isinstance(same_src, list) or case_id_str not in same_src:
                issues.append(
                    LintIssue(
                        "EVAL_BUNDLE_LINEAGE_ISOLATION",
                        f"{location}.eval_grouping.derived_from_same_source_cases",
                        f"derived_from_same_source_cases must be a list containing '{case_id_str}'.",
                    )
                )

    expected = case.get("expected_proposal")
    if not isinstance(expected, dict):
        issues.append(LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.expected_proposal", "Must be an object."))
        return issues

    for key in sorted(REQUIRED_EXPECTED_KEYS - set(expected.keys())):
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.expected_proposal.{key}",
                f"Missing required expected_proposal field '{key}'.",
            )
        )

    exp_approval = expected.get("approval_status")
    approved_by = expected.get("approved_by")
    if case_id_str.startswith("NEG-") or scope == "POST_MVP":
        if "approval_status" in expected and exp_approval != "DRAFT":
            issues.append(
                LintIssue(
                    "APPROVAL_STATUS_MUST_BE_DRAFT",
                    f"{location}.expected_proposal.approval_status",
                    f"POST_MVP drafts and NEG-* variants MUST remain approval_status='DRAFT'; got {exp_approval!r}.",
                )
            )
        if approved_by is not None:
            issues.append(
                LintIssue(
                    "APPROVAL_STATUS_MUST_BE_DRAFT",
                    f"{location}.expected_proposal.approved_by",
                    "POST_MVP drafts and NEG-* variants MUST keep approved_by=null.",
                )
            )
    else:
        if "approval_status" in expected and exp_approval not in ALLOWED_CASE_APPROVAL_STATUSES:
            issues.append(
                LintIssue(
                    "APPROVAL_STATUS_MUST_BE_DRAFT",
                    f"{location}.expected_proposal.approval_status",
                    f"Invalid approval_status {exp_approval!r}; allowed: {sorted(ALLOWED_CASE_APPROVAL_STATUSES)}.",
                )
            )
        elif exp_approval == "DRAFT":
            if approved_by is not None:
                issues.append(
                    LintIssue(
                        "APPROVAL_STATUS_MUST_BE_DRAFT",
                        f"{location}.expected_proposal.approved_by",
                        "DRAFT case must have approved_by=null.",
                    )
                )
        elif exp_approval == "APPROVED_MVP_REGRESSION":
            if (
                not isinstance(approved_by, str)
                or not approved_by.startswith("human-reviewer")
                or "agent" in approved_by.lower()
            ):
                issues.append(
                    LintIssue(
                        "APPROVAL_STATUS_MUST_BE_DRAFT",
                        f"{location}.expected_proposal.approved_by",
                        f"APPROVED_MVP_REGRESSION requires explicit human reviewer attribution (not agent self-approval); got {approved_by!r}.",
                    )
                )
            ameta = expected.get("approval_metadata")
            if not isinstance(ameta, dict):
                issues.append(
                    LintIssue(
                        "APPROVAL_STATUS_MUST_BE_DRAFT",
                        f"{location}.expected_proposal.approval_metadata",
                        "APPROVED_MVP_REGRESSION case must declare approval_metadata with baseline SHA, version, and comparison_policy.",
                    )
                )
            else:
                cpol = ameta.get("comparison_policy")
                if not isinstance(cpol, dict):
                    issues.append(
                        LintIssue(
                            "APPROVAL_STATUS_MUST_BE_DRAFT",
                            f"{location}.expected_proposal.approval_metadata.comparison_policy",
                            "approval_metadata must include comparison_policy.",
                        )
                    )
                else:
                    if cpol.get("compare_confirmed_logical_expectations") is not True:
                        issues.append(
                            LintIssue(
                                "APPROVAL_STATUS_MUST_BE_DRAFT",
                                f"{location}.expected_proposal.approval_metadata.comparison_policy.compare_confirmed_logical_expectations",
                                "compare_confirmed_logical_expectations must be true.",
                            )
                        )
                    if cpol.get("enforce_zero_mutation_on_rejection") is not True:
                        issues.append(
                            LintIssue(
                                "APPROVAL_STATUS_MUST_BE_DRAFT",
                                f"{location}.expected_proposal.approval_metadata.comparison_policy.enforce_zero_mutation_on_rejection",
                                "enforce_zero_mutation_on_rejection MUST stay true on approved MVP cases.",
                            )
                        )
                    excl_fields = cpol.get("excluded_unconfirmed_fields")
                    if not isinstance(excl_fields, list) or not any(
                        isinstance(f, str) and "http_status" in f for f in excl_fields
                    ):
                        issues.append(
                            LintIssue(
                                "APPROVAL_STATUS_MUST_BE_DRAFT",
                                f"{location}.expected_proposal.approval_metadata.comparison_policy.excluded_unconfirmed_fields",
                                "excluded_unconfirmed_fields must explicitly exclude unconfirmed http_status from ground-truth comparison.",
                            )
                        )

    # 6. Validate user_commands & command_results
    commands = case.get("user_commands") if isinstance(case.get("user_commands"), list) else []
    cmd_results = expected.get("command_results") if isinstance(expected.get("command_results"), list) else []

    cmd_map: dict[str, dict[str, Any]] = {}
    cmd_ids: list[str] = []
    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            issues.append(LintIssue("SCHEMA_REQUIRED_FIELD", f"{location}.user_commands[{idx}]", "Command must be object."))
            continue
        for key in sorted(REQUIRED_COMMAND_KEYS - set(cmd.keys())):
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.user_commands[{idx}].{key}",
                    f"Missing required command field '{key}'.",
                )
            )
        cid = cmd.get("command_id")
        if cid:
            cmd_ids.append(cid)
            cmd_map[cid] = cmd
        action = cmd.get("command_action")
        if action not in ALLOWED_COMMAND_ACTIONS:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.user_commands[{idx}].command_action",
                    f"Invalid command_action {action!r}.",
                )
            )
        if "spec_command_name" in cmd and cmd.get("spec_command_name") != "UNCONFIRMED":
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.user_commands[{idx}].spec_command_name",
                    f"Unconfirmed command wire name must be 'UNCONFIRMED', not guessed as {cmd.get('spec_command_name')!r}.",
                )
            )
        if cmd.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.user_commands[{idx}].workspace_id",
                    f"Unknown workspace_id '{cmd.get('workspace_id')}'.",
                )
            )
        exp_ver = cmd.get("expected_version")
        if "expected_version" in cmd and (not isinstance(exp_ver, int) or isinstance(exp_ver, bool) or exp_ver < 1):
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.user_commands[{idx}].expected_version",
                    f"expected_version must be integer >= 1; got {exp_ver!r}.",
                )
            )
        targets = cmd.get("target_event_ids") if isinstance(cmd.get("target_event_ids"), list) else []
        if not targets:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.user_commands[{idx}].target_event_ids",
                    "target_event_ids must contain at least one ID.",
                )
            )
        if action == "manual_merge" and len(set(targets)) < 2:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.user_commands[{idx}].target_event_ids",
                    "manual_merge requires at least 2 distinct target_event_ids.",
                )
            )
        if action == "split":
            payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
            parts = payload.get("partitions") if isinstance(payload.get("partitions"), list) else []
            if len(parts) < 2:
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.user_commands[{idx}].payload.partitions",
                        "split command payload must specify at least 2 partitions.",
                    )
                )
        if action == "dissolve":
            payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
            if not isinstance(payload.get("reason"), str) or not payload.get("reason", "").strip():
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.user_commands[{idx}].payload.reason",
                        "dissolve command MUST specify a non-empty reason.",
                    )
                )
            if not isinstance(cmd.get("idempotency_key"), str) or not cmd.get("idempotency_key", "").strip():
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.user_commands[{idx}].idempotency_key",
                        "dissolve command MUST specify a non-empty idempotency_key.",
                    )
                )
    _check_unique(cmd_ids, "CASE_UNIQUENESS", f"{location}.user_commands", "command_id", issues)

    cmd_result_map: dict[str, dict[str, Any]] = {}
    res_cmd_ids: list[str] = []
    for idx, res in enumerate(cmd_results):
        if not isinstance(res, dict):
            continue
        rcid = res.get("command_id")
        if rcid:
            res_cmd_ids.append(rcid)
            cmd_result_map[rcid] = res
        if rcid not in cmd_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.command_results[{idx}].command_id",
                    f"command_result references unknown command_id '{rcid}'.",
                )
            )
        outcome = res.get("outcome")
        rej_cat = res.get("rejection_category")
        spec_err = res.get("spec_error_code")
        internal_pol = res.get("internal_policy_code")
        mutated = res.get("state_mutated")

        if rej_cat not in ALLOWED_REJECTION_CATEGORIES:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.expected_proposal.command_results[{idx}].rejection_category",
                    f"Invalid rejection_category {rej_cat!r}.",
                )
            )
        if spec_err not in ALLOWED_SPEC_ERROR_CODES:
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.command_results[{idx}].spec_error_code",
                    f"Unsupported spec_error_code {spec_err!r}; only official Event API codes, 'UNCONFIRMED', or null are permitted (catch-all 'CANNOT_LINK' is forbidden).",
                )
            )
        if internal_pol is not None and internal_pol not in OFFICIAL_EVENT_API_ERROR_CODES:
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.command_results[{idx}].internal_policy_code",
                    f"Invalid internal_policy_code {internal_pol!r}.",
                )
            )
        if outcome == "ACCEPTED":
            if rej_cat is not None or spec_err is not None or mutated is not True:
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.expected_proposal.command_results[{idx}]",
                        "ACCEPTED command must have rejection_category=null, spec_error_code=null, and state_mutated=true.",
                    )
                )
        elif outcome == "REJECTED":
            if (
                rej_cat is None
                or spec_err is None
                or mutated is not False
                or res.get("resulting_event_ids") != []
            ):
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.expected_proposal.command_results[{idx}]",
                        "REJECTED command must have non-null rejection_category, spec_error_code (official code or 'UNCONFIRMED'), state_mutated=false, and resulting_event_ids=[].",
                    )
                )
        else:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.expected_proposal.command_results[{idx}].outcome",
                    f"Invalid command outcome {outcome!r}.",
                )
            )
    _check_unique(res_cmd_ids, "CASE_UNIQUENESS", f"{location}.expected_proposal.command_results", "command_id", issues)

    for cid in cmd_map:
        if cid not in cmd_result_map:
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.expected_proposal.command_results",
                    f"Missing command_result entry for command_id '{cid}'.",
                )
            )

    # Check target references, workspace/repo boundaries, nesting prohibition, split scope, and version conflicts
    all_source_entity_ids = set(init_event_map) | set(ticket_map) | set(commit_map) | set(pr_map)
    accepted_manual_merge_cmd_ids: set[str] = set()
    accepted_split_cmds: list[dict[str, Any]] = []
    accepted_dissolve_cmds: list[dict[str, Any]] = []

    for cid, cmd in cmd_map.items():
        res = cmd_result_map.get(cid, {})
        outcome = res.get("outcome")
        rej_cat = res.get("rejection_category")
        spec_err = res.get("spec_error_code")
        internal_pol = res.get("internal_policy_code")
        action = cmd.get("command_action")

        if action == "manual_merge" and outcome == "ACCEPTED":
            accepted_manual_merge_cmd_ids.add(cid)
        elif action == "split" and outcome == "ACCEPTED":
            accepted_split_cmds.append(cmd)
        elif action == "dissolve" and outcome == "ACCEPTED":
            accepted_dissolve_cmds.append(cmd)

        targets = cmd.get("target_event_ids") if isinstance(cmd.get("target_event_ids"), list) else []
        target_ws: set[str] = set()
        target_repos: set[str] = set()
        version_mismatch = False
        includes_composite_or_subsumed = False
        includes_inactive_member = False
        target_versions = cmd.get("target_versions") if isinstance(cmd.get("target_versions"), dict) else {}

        for tid in targets:
            if tid in init_event_map:
                ev = init_event_map[tid]
                target_ws.add(ev["workspace_id"])
                target_repos.add(ev["repo_id"])
                supplied_v = target_versions.get(tid, cmd.get("expected_version"))
                if isinstance(supplied_v, int) and supplied_v != ev.get("version"):
                    version_mismatch = True
                if action == "manual_merge" and outcome == "ACCEPTED" and tid not in target_versions:
                    issues.append(
                        LintIssue(
                            "COMMAND_VERSION_RULES",
                            f"{location}.user_commands[{cid}].target_versions",
                            f"Accepted manual_merge must verify per-Event version for every member; missing '{tid}'.",
                        )
                    )
                if ev.get("anchor_mode") == "USER_COMPOSITE" or ev.get("membership_role") == "subsumed_by_user_composite":
                    includes_composite_or_subsumed = True
                if ev.get("lifecycle_status") in {"REJECTED", "SUPERSEDED"} or ev.get("is_active_top_level") is False:
                    includes_inactive_member = True
            elif outcome == "REJECTED" and rej_cat == "non_event_or_unknown_member_rejected" and tid in all_source_entity_ids:
                continue
            else:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.user_commands[{cid}].target_event_ids",
                        f"Unknown target_event_id '{tid}'.",
                    )
                )

        ws_mismatch = bool(target_ws) and (target_ws != {cmd.get("workspace_id")})
        cross_repo_merge = action == "manual_merge" and len(target_repos) > 1

        if ws_mismatch and (outcome != "REJECTED" or res.get("state_mutated") is not False):
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.expected_proposal.command_results[{cid}]",
                    "Cross-workspace command MUST be REJECTED with state_mutated=false.",
                )
            )
        if action != "manual_merge" and (
            spec_err in MERGE_ONLY_ERROR_CODES or internal_pol in MERGE_ONLY_ERROR_CODES
        ):
            issues.append(
                LintIssue(
                    "COMMAND_VERSION_RULES",
                    f"{location}.expected_proposal.command_results[{cid}]",
                    f"Non-merge command '{cid}' ({action}) must NOT use merge-only error code ({spec_err!r} / {internal_pol!r}).",
                )
            )
        if cross_repo_merge and (outcome != "REJECTED" or res.get("state_mutated") is not False):
            issues.append(
                LintIssue(
                    "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                    f"{location}.expected_proposal.command_results[{cid}]",
                    "Cross-repository manual_merge MUST be REJECTED with state_mutated=false (CROSS_REPOSITORY_MEMBER).",
                )
            )
        if action == "manual_merge" and (includes_composite_or_subsumed or includes_inactive_member):
            if outcome != "REJECTED" or res.get("state_mutated") is not False:
                issues.append(
                    LintIssue(
                        "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                        f"{location}.expected_proposal.command_results[{cid}]",
                        "Nesting USER_COMPOSITE or merging inactive/SUPERSEDED/REJECTED member MUST be REJECTED with state_mutated=false.",
                    )
                )
        if version_mismatch and not ws_mismatch:
            if outcome != "REJECTED" or rej_cat != "version_conflict" or spec_err != "EVENT_VERSION_CONFLICT":
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.expected_proposal.command_results[{cid}]",
                        "Stale per-Event version command MUST be REJECTED with rejection_category='version_conflict' and spec_error_code='EVENT_VERSION_CONFLICT'.",
                    )
                )

        if action == "split" and outcome == "ACCEPTED":
            payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
            parts = payload.get("partitions") if isinstance(payload.get("partitions"), list) else []
            seen_scope_tokens: set[tuple[str, str]] = set()
            for pidx, part in enumerate(parts):
                if not isinstance(part, dict) or not isinstance(part.get("evidence_scope"), dict):
                    issues.append(
                        LintIssue(
                            "INVARIANT_SPLIT_DISJOINT_SCOPE",
                            f"{location}.user_commands[{cid}].payload.partitions[{pidx}]",
                            "Accepted split partition must specify concrete evidence_scope.",
                        )
                    )
                    continue
                ptoks = _scope_token_set(part["evidence_scope"])
                if not ptoks:
                    issues.append(
                        LintIssue(
                            "INVARIANT_SPLIT_DISJOINT_SCOPE",
                            f"{location}.user_commands[{cid}].payload.partitions[{pidx}].evidence_scope",
                            "Accepted split partition evidence_scope must be non-empty.",
                        )
                    )
                overlap = seen_scope_tokens & ptoks
                if overlap:
                    issues.append(
                        LintIssue(
                            "INVARIANT_SPLIT_DISJOINT_SCOPE",
                            f"{location}.user_commands[{cid}].payload.partitions[{pidx}].evidence_scope",
                            f"Overlapping split evidence_scope tokens are forbidden: {sorted(overlap)}.",
                        )
                    )
                seen_scope_tokens |= ptoks

    # 7. Validate expected events & all confirmed official domain invariants
    exp_events = expected.get("events") if isinstance(expected.get("events"), list) else []
    held_items = expected.get("held_items") if isinstance(expected.get("held_items"), list) else []
    unpersisted_obs = (
        expected.get("unpersisted_external_observations")
        if isinstance(expected.get("unpersisted_external_observations"), list)
        else []
    )
    downstream_effects = (
        expected.get("downstream_side_effects") if isinstance(expected.get("downstream_side_effects"), list) else []
    )
    search_checks = expected.get("search_checks") if isinstance(expected.get("search_checks"), list) else []
    agg = expected.get("aggregation_check") if isinstance(expected.get("aggregation_check"), dict) else {}

    exp_event_map: dict[str, dict[str, Any]] = {}
    exp_event_ids: list[str] = []
    for idx, ev in enumerate(exp_events):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("event_id")
        if eid:
            exp_event_ids.append(eid)
            exp_event_map[eid] = ev
        ev_loc = f"{location}.expected_proposal.events[{idx}]"
        _validate_event_official_tokens(ev, ev_loc, issues)

        if ev.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.workspace_id",
                    f"Unknown workspace_id '{ev.get('workspace_id')}'.",
                )
            )
        ev_repo = ev.get("repo_id")
        if ev_repo not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.repo_id",
                    f"Unknown repo_id '{ev_repo}'.",
                )
            )
        elif repo_map[ev_repo].get("workspace_id") != ev.get("workspace_id"):
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    ev_loc,
                    "Event workspace_id does not match repo.workspace_id.",
                )
            )

        anchor = ev.get("anchor_mode")
        origin_cmd = ev.get("origin_command_id")
        if origin_cmd is not None and origin_cmd not in cmd_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{ev_loc}.origin_command_id",
                    f"Unknown origin_command_id '{origin_cmd}'.",
                )
            )

        # PARTIAL run must promote 0 new Events
        if eid not in init_event_map and ev_repo in repo_sync_statuses and repo_sync_statuses[ev_repo] == {"PARTIAL"}:
            issues.append(
                LintIssue(
                    "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                    ev_loc,
                    f"Event '{eid}' cannot be promoted from a PARTIAL Import Run.",
                )
            )

        ppr = ev.get("primary_pr_id")
        if ppr is not None:
            if ppr not in pr_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.primary_pr_id",
                        f"Unknown primary_pr_id '{ppr}'.",
                    )
                )
            else:
                pr_obj = pr_map[ppr]
                if anchor in {"PR", "PR_SEGMENT"} and pr_obj.get("state") != "MERGED":
                    issues.append(
                        LintIssue(
                            "INVARIANT_MERGED_PR_CANDIDATE",
                            f"{ev_loc}.primary_pr_id",
                            f"{anchor} Event '{eid}' references non-MERGED PR '{ppr}' (state={pr_obj.get('state')!r}).",
                        )
                    )
                if pr_obj.get("repo_id") != ev_repo:
                    issues.append(
                        LintIssue(
                            "ID_REFERENCE_INTEGRITY",
                            f"{ev_loc}.repo_id",
                            f"Event repo_id '{ev_repo}' disagrees with primary PR repo_id '{pr_obj.get('repo_id')}'.",
                        )
                    )
                if eid not in init_event_map and pr_obj.get("source_sync_run_id") in partial_sync_run_ids:
                    issues.append(
                        LintIssue(
                            "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                            f"{ev_loc}.primary_pr_id",
                            f"Event '{eid}' references PR '{ppr}' from PARTIAL run '{pr_obj.get('source_sync_run_id')}'.",
                        )
                    )
        elif anchor in {"PR", "PR_SEGMENT"}:
            issues.append(
                LintIssue(
                    "INVARIANT_MERGED_PR_CANDIDATE",
                    f"{ev_loc}.primary_pr_id",
                    f"{anchor} Event '{eid}' must reference a MERGED primary_pr_id.",
                )
            )

        src_commits = ev.get("source_commit_ids", [])
        if anchor == "DIRECT_COMMIT" and not src_commits:
            issues.append(
                LintIssue(
                    "INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED",
                    f"{ev_loc}.source_commit_ids",
                    "DIRECT_COMMIT Event must reference at least one default-branch NO_MATCH_OBSERVED commit.",
                )
            )

        for cid in src_commits:
            if cid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.source_commit_ids",
                        f"Unknown commit_id '{cid}'.",
                    )
                )
            else:
                cmt = commit_map[cid]
                if cid in unknown_commit_ids:
                    issues.append(
                        LintIssue(
                            "INVARIANT_UNKNOWN_NOT_NO_PR",
                            f"{ev_loc}.source_commit_ids",
                            f"Commit '{cid}' has pr_association_status='UNKNOWN' and MUST NOT be classified into Event '{eid}' ({anchor}).",
                        )
                    )
                elif anchor == "DIRECT_COMMIT":
                    if (
                        cmt.get("on_default_branch") is not True
                        or cmt.get("default_branch_verified_at_start_and_end") is not True
                        or cmt.get("pr_association_lookup_status") != "COMPLETED"
                        or cmt.get("pr_association_status") != "NO_MATCH_OBSERVED"
                    ):
                        issues.append(
                            LintIssue(
                                "INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED",
                                f"{ev_loc}.source_commit_ids",
                                f"DIRECT_COMMIT Event '{eid}' commit '{cid}' requires on_default_branch=true, default_branch_verified_at_start_and_end=true, pr_association_lookup_status='COMPLETED', and pr_association_status='NO_MATCH_OBSERVED'.",
                            )
                        )
                if eid not in init_event_map and cmt.get("source_sync_run_id") in partial_sync_run_ids:
                    issues.append(
                        LintIssue(
                            "INVARIANT_PARTIAL_RUN_NO_PROMOTION",
                            f"{ev_loc}.source_commit_ids",
                            f"Event '{eid}' references commit '{cid}' from PARTIAL run '{cmt.get('source_sync_run_id')}'.",
                        )
                    )
                if cmt.get("repo_id") != ev_repo:
                    issues.append(
                        LintIssue(
                            "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                            f"{ev_loc}.source_commit_ids",
                            f"Commit '{cid}' belongs to repo '{cmt.get('repo_id')}', which differs from Event repo '{ev_repo}'.",
                        )
                    )

        for tid in ev.get("context_ticket_ids", []):
            if tid not in ticket_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{ev_loc}.context_ticket_ids",
                        f"Unknown context_ticket_id '{tid}'.",
                    )
                )

        # No context-only / automatic multi-PR merge invariant
        merge_origin = ev.get("merge_origin")
        if merge_origin not in ALLOWED_MERGE_ORIGINS:
            issues.append(
                LintIssue(
                    "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                    f"{ev_loc}.merge_origin",
                    f"Forbidden merge_origin {merge_origin!r}; automatic multi-PR or Issue/ticket-link merges are prohibited.",
                )
            )
        if anchor == "USER_COMPOSITE":
            if merge_origin != "USER_MANUAL_MERGE":
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                        f"{ev_loc}.merge_origin",
                        "USER_COMPOSITE MUST have merge_origin='USER_MANUAL_MERGE'.",
                    )
                )
            preexisted_manual = (
                eid in init_event_map
                and init_event_map[eid].get("anchor_mode") == "USER_COMPOSITE"
                and init_event_map[eid].get("merge_origin") == "USER_MANUAL_MERGE"
            )
            created_by_manual_cmd = origin_cmd in accepted_manual_merge_cmd_ids
            if not preexisted_manual and not created_by_manual_cmd:
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                        ev_loc,
                        f"USER_COMPOSITE '{eid}' has neither a pre-existing USER_MANUAL_MERGE state nor an accepted manual_merge command.",
                    )
                )

    _check_unique(exp_event_ids, "CASE_UNIQUENESS", f"{location}.expected_proposal.events", "event_id", issues)

    # Cross-check split children, member_event_ids, same-repo, no-nesting, SUPERSEDED history, and double-counting
    active_composite_members: dict[str, set[str]] = {}
    member_to_active_composite: dict[str, str] = {}
    for idx, ev in enumerate(exp_events):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("event_id", "")
        parent_id = ev.get("parent_event_id")
        if parent_id is not None and parent_id not in exp_event_map and parent_id not in init_event_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.events[{idx}].parent_event_id",
                    f"Unknown parent_event_id '{parent_id}'.",
                )
            )
        members = ev.get("member_event_ids", [])
        for mid in members:
            if mid not in exp_event_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.events[{idx}].member_event_ids",
                        f"Unknown member_event_id '{mid}'.",
                    )
                )
            else:
                mem = exp_event_map[mid]
                if ev.get("anchor_mode") == "USER_COMPOSITE":
                    if mem.get("workspace_id") != ev.get("workspace_id") or mem.get("repo_id") != ev.get("repo_id"):
                        issues.append(
                            LintIssue(
                                "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                                f"{location}.expected_proposal.events[{idx}].member_event_ids",
                                f"USER_COMPOSITE '{eid}' member '{mid}' has workspace/repo ({mem.get('workspace_id')}/{mem.get('repo_id')}) != ({ev.get('workspace_id')}/{ev.get('repo_id')}).",
                            )
                        )
                    if mem.get("anchor_mode") == "USER_COMPOSITE":
                        issues.append(
                            LintIssue(
                                "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                                f"{location}.expected_proposal.events[{idx}].member_event_ids",
                                f"USER_COMPOSITE '{eid}' nests another USER_COMPOSITE '{mid}'; nesting is forbidden.",
                            )
                        )

        if ev.get("anchor_mode") == "USER_COMPOSITE" and ev.get("is_active_top_level") is True:
            if ev.get("composite_membership_status") != "ACTIVE":
                issues.append(
                    LintIssue(
                        "SCHEMA_REQUIRED_FIELD",
                        f"{location}.expected_proposal.events[{idx}].composite_membership_status",
                        f"Active USER_COMPOSITE '{eid}' must declare composite_membership_status='ACTIVE'.",
                    )
                )
            if ev.get("member_snapshots_preserved") is not True:
                issues.append(
                    LintIssue(
                        "SCHEMA_REQUIRED_FIELD",
                        f"{location}.expected_proposal.events[{idx}].member_snapshots_preserved",
                        f"Active USER_COMPOSITE '{eid}' must declare member_snapshots_preserved=true.",
                    )
                )
            active_composite_members[eid] = set(members)
            for mid in members:
                if mid in member_to_active_composite:
                    issues.append(
                        LintIssue(
                            "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                            f"{location}.expected_proposal.events[{idx}].member_event_ids",
                            f"Member '{mid}' belongs to multiple active USER_COMPOSITEs ('{member_to_active_composite[mid]}' and '{eid}').",
                        )
                    )
                member_to_active_composite[mid] = eid
                if mid in exp_event_map:
                    mem = exp_event_map[mid]
                    if mem.get("lifecycle_status") != "SUPERSEDED":
                        issues.append(
                            LintIssue(
                                "INVARIANT_SUPERSEDED_HISTORY",
                                f"{location}.expected_proposal.events[{mid}].lifecycle_status",
                                f"Active USER_COMPOSITE '{eid}' member '{mid}' MUST have lifecycle_status='SUPERSEDED', got {mem.get('lifecycle_status')!r}.",
                            )
                        )
                    if (
                        mem.get("is_active_top_level") is not False
                        or mem.get("searchable_as_top_level") is not False
                        or mem.get("counted_in_metrics") is not False
                    ):
                        issues.append(
                            LintIssue(
                                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                                f"{location}.expected_proposal.events[{mid}]",
                                f"USER_COMPOSITE '{eid}' member '{mid}' must have is_active_top_level=false, searchable_as_top_level=false, and counted_in_metrics=false.",
                            )
                        )
                    if mem.get("membership_role") != "subsumed_by_user_composite":
                        issues.append(
                            LintIssue(
                                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                                f"{location}.expected_proposal.events[{mid}].membership_role",
                                f"Active USER_COMPOSITE member '{mid}' must have membership_role='subsumed_by_user_composite'.",
                            )
                        )

    # Validate accepted split results: parent SUPERSEDED, children PR_SEGMENT/DIRECT_COMMIT with disjoint evidence_scope
    for scmd in accepted_split_cmds:
        scid = scmd["command_id"]
        parent_id = scmd["target_event_ids"][0]
        parent_init = init_event_map.get(parent_id, {})
        parent_exp = exp_event_map.get(parent_id)
        if not parent_exp or parent_exp.get("lifecycle_status") != "SUPERSEDED":
            issues.append(
                LintIssue(
                    "INVARIANT_SUPERSEDED_HISTORY",
                    f"{location}.expected_proposal.events[{parent_id}]",
                    f"Split parent '{parent_id}' MUST transition to lifecycle_status='SUPERSEDED'.",
                )
            )
        children = [
            ev
            for ev in exp_events
            if isinstance(ev, dict) and ev.get("parent_event_id") == parent_id and ev.get("membership_role") == "split_child"
        ]
        if len(children) < 2:
            issues.append(
                LintIssue(
                    "INVARIANT_SPLIT_DISJOINT_SCOPE",
                    f"{location}.expected_proposal.events",
                    f"Accepted split command '{scid}' must produce at least 2 split_child Events.",
                )
            )
        expected_child_anchor = "PR_SEGMENT" if parent_init.get("anchor_mode") == "PR" else "DIRECT_COMMIT"
        child_scope_tokens: set[tuple[str, str]] = set()
        for child in children:
            ceid = child.get("event_id")
            if child.get("anchor_mode") != expected_child_anchor:
                issues.append(
                    LintIssue(
                        "OFFICIAL_ANCHOR_MODE",
                        f"{location}.expected_proposal.events[{ceid}].anchor_mode",
                        f"Split child '{ceid}' of {parent_init.get('anchor_mode')!r} must have anchor_mode={expected_child_anchor!r}, got {child.get('anchor_mode')!r}.",
                    )
                )
            escope = child.get("evidence_scope")
            if not isinstance(escope, dict):
                issues.append(
                    LintIssue(
                        "INVARIANT_SPLIT_DISJOINT_SCOPE",
                        f"{location}.expected_proposal.events[{ceid}].evidence_scope",
                        f"Split child '{ceid}' must define concrete evidence_scope.",
                    )
                )
                continue
            ctoks = _scope_token_set(escope)
            if not ctoks:
                issues.append(
                    LintIssue(
                        "INVARIANT_SPLIT_DISJOINT_SCOPE",
                        f"{location}.expected_proposal.events[{ceid}].evidence_scope",
                        f"Split child '{ceid}' evidence_scope must be non-empty.",
                    )
                )
            overlap = child_scope_tokens & ctoks
            if overlap:
                issues.append(
                    LintIssue(
                        "INVARIANT_SPLIT_DISJOINT_SCOPE",
                        f"{location}.expected_proposal.events[{ceid}].evidence_scope",
                        f"Split child '{ceid}' overlaps sibling evidence_scope tokens: {sorted(overlap)}.",
                    )
                )
            child_scope_tokens |= ctoks

    # Check dissolve -> Composite SUPERSEDED + membership REMOVED + dependent FACT/CLAIM/EXPORT STALE
    effect_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for idx, eff in enumerate(downstream_effects):
        if not isinstance(eff, dict):
            continue
        atype = eff.get("artifact_type", "")
        aid = eff.get("artifact_id", "")
        comp_id = eff.get("source_composite_event_id", "")
        effect_index[(comp_id, atype, aid)] = eff
        if atype not in ALLOWED_ARTIFACT_TYPES:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.downstream_side_effects[{idx}].artifact_type",
                    f"Invalid artifact_type {atype!r}; must be FACT, CLAIM, or EXPORT.",
                )
            )
        if comp_id not in exp_event_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.downstream_side_effects[{idx}].source_composite_event_id",
                    f"Unknown source_composite_event_id '{comp_id}'.",
                )
            )
        if eff.get("expected_status") != "STALE":
            issues.append(
                LintIssue(
                    "INVARIANT_DISSOLVE_MARKS_DEPENDENT_ARTIFACTS_STALE",
                    f"{location}.expected_proposal.downstream_side_effects[{idx}].expected_status",
                    f"Expected 'STALE' for dissolved composite artifact side effect, got {eff.get('expected_status')!r}.",
                )
            )
        if eff.get("retroactively_deleted") is not False or eff.get("auto_transferred_to_members") is not False:
            issues.append(
                LintIssue(
                    "INVARIANT_DISSOLVE_MARKS_DEPENDENT_ARTIFACTS_STALE",
                    f"{location}.expected_proposal.downstream_side_effects[{idx}]",
                    "Dissolved composite artifacts must NEVER be retroactively deleted or auto-transferred to members.",
                )
            )

    for dcmd in accepted_dissolve_cmds:
        comp_id = dcmd["target_event_ids"][0]
        init_ev = init_event_map.get(comp_id, {})
        exp_ev = exp_event_map.get(comp_id)
        if not exp_ev:
            issues.append(
                LintIssue(
                    "INVARIANT_SUPERSEDED_HISTORY",
                    f"{location}.expected_proposal.events",
                    f"Dissolved USER_COMPOSITE '{comp_id}' must remain in expected_proposal.events as SUPERSEDED history.",
                )
            )
        else:
            if exp_ev.get("lifecycle_status") != "SUPERSEDED":
                issues.append(
                    LintIssue(
                        "INVARIANT_SUPERSEDED_HISTORY",
                        f"{location}.expected_proposal.events[{comp_id}].lifecycle_status",
                        f"Dissolved USER_COMPOSITE '{comp_id}' MUST have lifecycle_status='SUPERSEDED', got {exp_ev.get('lifecycle_status')!r}.",
                    )
                )
            if exp_ev.get("composite_membership_status") != "REMOVED":
                issues.append(
                    LintIssue(
                        "INVARIANT_SUPERSEDED_HISTORY",
                        f"{location}.expected_proposal.events[{comp_id}].composite_membership_status",
                        f"Dissolved USER_COMPOSITE '{comp_id}' MUST set composite_membership_status='REMOVED'.",
                    )
                )
            if isinstance(init_ev.get("version"), int) and isinstance(exp_ev.get("version"), int):
                if exp_ev["version"] <= init_ev["version"]:
                    issues.append(
                        LintIssue(
                            "COMMAND_VERSION_RULES",
                            f"{location}.expected_proposal.events[{comp_id}].version",
                            f"Dissolved USER_COMPOSITE '{comp_id}' must increment version above {init_ev['version']}.",
                        )
                    )
        dep_arts = init_ev.get("dependent_artifacts") if isinstance(init_ev.get("dependent_artifacts"), dict) else {}
        for atype, key in (("FACT", "fact_ids"), ("CLAIM", "claim_ids"), ("EXPORT", "export_ids")):
            for aid in dep_arts.get(key, []):
                if (comp_id, atype, aid) not in effect_index:
                    issues.append(
                        LintIssue(
                            "INVARIANT_DISSOLVE_MARKS_DEPENDENT_ARTIFACTS_STALE",
                            f"{location}.expected_proposal.downstream_side_effects",
                            f"Dissolved USER_COMPOSITE '{comp_id}' has dependent {atype} '{aid}' that MUST be marked STALE in downstream_side_effects.",
                        )
                    )

    # Check command_results resulting_event_ids
    for idx, res in enumerate(cmd_results):
        if not isinstance(res, dict):
            continue
        for reid in res.get("resulting_event_ids", []):
            if reid not in exp_event_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.command_results[{idx}].resulting_event_ids",
                        f"Unknown resulting_event_id '{reid}'.",
                    )
                )

    # 8. Validate held_items, unpersisted_external_observations, and UNKNOWN commit coverage
    held_ids: list[str] = []
    held_commit_ids: set[str] = set()
    for idx, item in enumerate(held_items):
        if not isinstance(item, dict):
            continue
        hid = item.get("held_id")
        if hid:
            held_ids.append(hid)
        stype = item.get("source_type")
        sid = item.get("source_id")
        rkind = item.get("record_kind")
        if rkind not in ALLOWED_HOLD_RECORD_KINDS:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.held_items[{idx}].record_kind",
                    f"Invalid record_kind {rkind!r}; expected OBSERVATION_RECORD or NON_ELIGIBLE_SOURCE_RECORD.",
                )
            )
        if item.get("logical_disposition") not in ALLOWED_DISPOSITIONS:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.held_items[{idx}].logical_disposition",
                    f"Invalid logical_disposition {item.get('logical_disposition')!r}.",
                )
            )
        if item.get("spec_hold_status") != "UNCONFIRMED":
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.held_items[{idx}].spec_hold_status",
                    f"Unconfirmed hold status must be 'UNCONFIRMED', not guessed as {item.get('spec_hold_status')!r}.",
                )
            )
        if stype == "COMMIT":
            if sid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.held_items[{idx}].source_id",
                        f"Held COMMIT references unknown commit_id '{sid}'.",
                    )
                )
            else:
                held_commit_ids.add(sid)
        elif stype == "PR":
            if sid not in pr_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.held_items[{idx}].source_id",
                        f"Held PR references unknown pr_id '{sid}'.",
                    )
                )
        else:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.held_items[{idx}].source_type",
                    f"held_items source_type must be COMMIT or PR (unpersisted B-003 observations belong in unpersisted_external_observations), got {stype!r}.",
                )
            )
        if item.get("interpreted_as_no_pr") is not False:
            issues.append(
                LintIssue(
                    "INVARIANT_UNKNOWN_NOT_NO_PR",
                    f"{location}.expected_proposal.held_items[{idx}].interpreted_as_no_pr",
                    "held_item.interpreted_as_no_pr must be false.",
                )
            )
    _check_unique(held_ids, "CASE_UNIQUENESS", f"{location}.expected_proposal.held_items", "held_id", issues)

    for ucid in sorted(unknown_commit_ids):
        if ucid not in held_commit_ids:
            issues.append(
                LintIssue(
                    "INVARIANT_UNKNOWN_NOT_NO_PR",
                    f"{location}.expected_proposal.held_items",
                    f"Commit '{ucid}' has pr_association_status='UNKNOWN' and must appear in held_items as an observation record.",
                )
            )

    for idx, uobs in enumerate(unpersisted_obs):
        if not isinstance(uobs, dict):
            continue
        oid = uobs.get("observation_id")
        if oid not in obs_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.unpersisted_external_observations[{idx}].observation_id",
                    f"Unknown observation_id '{oid}'.",
                )
            )
        if uobs.get("persisted_in_mvp") is not False:
            issues.append(
                LintIssue(
                    "SCOPE_TAG_POLICY",
                    f"{location}.expected_proposal.unpersisted_external_observations[{idx}].persisted_in_mvp",
                    "unpersisted_external_observations must declare persisted_in_mvp=false.",
                )
            )

    # 9. Validate search_checks & aggregation_check (double-counting & workspace isolation guard)
    for idx, sc in enumerate(search_checks):
        if not isinstance(sc, dict):
            continue
        seids = sc.get("expected_event_ids", [])
        sc_ws = sc.get("workspace_id")
        if sc_ws is not None and sc_ws not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.search_checks[{idx}].workspace_id",
                    f"Unknown workspace_id '{sc_ws}'.",
                )
            )
        if len(seids) != sc.get("expected_result_count"):
            issues.append(
                LintIssue(
                    "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                    f"{location}.expected_proposal.search_checks[{idx}]",
                    f"expected_result_count={sc.get('expected_result_count')} does not match len(expected_event_ids)={len(seids)}.",
                )
            )
        if len(set(seids)) != len(seids):
            issues.append(
                LintIssue(
                    "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                    f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                    "Duplicate event_id in search_check results.",
                )
            )
        seid_set = set(seids)
        for seid in seids:
            if seid not in exp_event_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                        f"Unknown expected_event_id '{seid}'.",
                    )
                )
            else:
                if exp_event_map[seid].get("searchable_as_top_level") is not True:
                    issues.append(
                        LintIssue(
                            "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                            f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                            f"Non-top-level Event '{seid}' must not appear in top-level search results.",
                        )
                    )
                if sc_ws is not None and exp_event_map[seid].get("workspace_id") != sc_ws:
                    issues.append(
                        LintIssue(
                            "WORKSPACE_AGGREGATION_SEPARATION",
                            f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                            f"Workspace-scoped search for '{sc_ws}' returned foreign-workspace Event '{seid}'.",
                        )
                    )
        for comp_id, members in active_composite_members.items():
            overlap = seid_set & members
            if comp_id in seid_set and overlap:
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                        f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                        f"Search check double-counts USER_COMPOSITE '{comp_id}' alongside its member(s) {sorted(overlap)}.",
                    )
                )

    actual_countable = sum(1 for ev in exp_events if isinstance(ev, dict) and ev.get("counted_in_metrics") is True)
    actual_composites = sum(
        1
        for ev in exp_events
        if isinstance(ev, dict)
        and ev.get("anchor_mode") == "USER_COMPOSITE"
        and ev.get("is_active_top_level") is True
        and ev.get("counted_in_metrics") is True
    )
    actual_rejected = sum(1 for ev in exp_events if isinstance(ev, dict) and ev.get("lifecycle_status") == "REJECTED")
    actual_held = len(held_items)

    if agg.get("total_countable_events") != actual_countable:
        issues.append(
            LintIssue(
                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                f"{location}.expected_proposal.aggregation_check.total_countable_events",
                f"Expected total_countable_events={actual_countable}, got {agg.get('total_countable_events')!r}.",
            )
        )
    if agg.get("total_user_composite_events") != actual_composites:
        issues.append(
            LintIssue(
                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                f"{location}.expected_proposal.aggregation_check.total_user_composite_events",
                f"Expected total_user_composite_events={actual_composites}, got {agg.get('total_user_composite_events')!r}.",
            )
        )
    if agg.get("total_held_items") != actual_held:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.expected_proposal.aggregation_check.total_held_items",
                f"Expected total_held_items={actual_held}, got {agg.get('total_held_items')!r}.",
            )
        )
    if agg.get("total_rejected_events") != actual_rejected:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.expected_proposal.aggregation_check.total_rejected_events",
                f"Expected total_rejected_events={actual_rejected}, got {agg.get('total_rejected_events')!r}.",
            )
        )

    if len(ws_id_set) > 1:
        per_ws = agg.get("per_workspace_countable_events")
        if not isinstance(per_ws, dict):
            issues.append(
                LintIssue(
                    "WORKSPACE_AGGREGATION_SEPARATION",
                    f"{location}.expected_proposal.aggregation_check.per_workspace_countable_events",
                    "Multi-workspace case must separate per_workspace_countable_events from internal fixture-wide total_countable_events.",
                )
            )
        else:
            expected_per_ws = {
                ws: sum(
                    1
                    for ev in exp_events
                    if isinstance(ev, dict) and ev.get("workspace_id") == ws and ev.get("counted_in_metrics") is True
                )
                for ws in sorted(ws_id_set)
            }
            if per_ws != expected_per_ws:
                issues.append(
                    LintIssue(
                        "WORKSPACE_AGGREGATION_SEPARATION",
                        f"{location}.expected_proposal.aggregation_check.per_workspace_countable_events",
                        f"per_workspace_countable_events={per_ws!r} disagrees with actual workspace counts {expected_per_ws!r}.",
                    )
                )

    return issues


def lint_case_file(path: Path) -> list[LintIssue]:
    data, issues = _load_json(path)
    if issues:
        return issues
    return lint_case_data(data, str(path))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lint_dataset(dataset_dir: Path) -> list[LintIssue]:
    issues: list[LintIssue] = []
    manifest_path = dataset_dir / "manifest.json"
    manifest, m_issues = _load_json(manifest_path)
    issues.extend(m_issues)
    if manifest is None or not isinstance(manifest, dict):
        return issues

    for key in sorted(REQUIRED_MANIFEST_KEYS - set(manifest.keys())):
        issues.append(
            LintIssue("SCHEMA_REQUIRED_FIELD", f"manifest.json.{key}", f"Missing required manifest field '{key}'.")
        )
    _walk_keys_and_strings(manifest, "manifest.json", issues)

    m_approval = manifest.get("approval_status")
    if m_approval not in ALLOWED_MANIFEST_APPROVAL_STATUSES:
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                "manifest.json.approval_status",
                f"Manifest approval_status must be in {sorted(ALLOWED_MANIFEST_APPROVAL_STATUSES)}, got {m_approval!r}.",
            )
        )

    scope_boundary = manifest.get("scope_boundary") if isinstance(manifest.get("scope_boundary"), dict) else {}
    if scope_boundary.get("od_007_physical_table_assumption") is not False:
        issues.append(
            LintIssue(
                "OD007_NO_PHYSICAL_TABLE",
                "manifest.json.scope_boundary.od_007_physical_table_assumption",
                "Manifest must declare od_007_physical_table_assumption=false.",
            )
        )

    cases_entries = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    if len(cases_entries) < 16:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                "manifest.json.cases",
                f"T-001A requires at least 16 cases; found {len(cases_entries)}.",
            )
        )

    seen_case_ids: set[str] = set()
    seen_slugs: set[str] = set()
    manifest_case_paths: set[Path] = set()
    manifest_case_map: dict[str, dict[str, Any]] = {}
    loaded_cases: dict[str, dict[str, Any]] = {}
    actual_case_sha256: dict[str, str] = {}

    for idx, entry in enumerate(cases_entries):
        if not isinstance(entry, dict):
            continue
        cid = entry.get("case_id", "")
        rel_path = entry.get("path", "")
        if cid in seen_case_ids:
            issues.append(LintIssue("CASE_UNIQUENESS", f"manifest.json.cases[{idx}]", f"Duplicate case_id '{cid}'."))
        seen_case_ids.add(cid)
        manifest_case_map[cid] = entry

        full_path = (dataset_dir / rel_path).resolve()
        manifest_case_paths.add(full_path)
        case_data, load_issues = _load_json(full_path)
        if load_issues:
            issues.extend(load_issues)
            continue
        actual_sha = _sha256_file(full_path)
        actual_case_sha256[cid] = actual_sha
        issues.extend(lint_case_data(case_data, rel_path))

        if isinstance(case_data, dict):
            loaded_cases[cid] = case_data
            if case_data.get("case_id") != cid:
                issues.append(
                    LintIssue(
                        "CASE_UNIQUENESS",
                        rel_path,
                        f"Manifest case_id '{cid}' disagrees with file case_id '{case_data.get('case_id')}'.",
                    )
                )
            slug = case_data.get("slug", "")
            if slug in seen_slugs:
                issues.append(LintIssue("CASE_UNIQUENESS", rel_path, f"Duplicate slug '{slug}'."))
            seen_slugs.add(slug)

            if case_data.get("scope") != entry.get("scope"):
                issues.append(
                    LintIssue(
                        "SCOPE_TAG_POLICY",
                        rel_path,
                        f"Manifest scope '{entry.get('scope')}' disagrees with file scope '{case_data.get('scope')}'.",
                    )
                )
            if case_data.get("backlog_ref") != entry.get("backlog_ref"):
                issues.append(
                    LintIssue(
                        "SCOPE_TAG_POLICY",
                        rel_path,
                        f"Manifest backlog_ref {entry.get('backlog_ref')!r} disagrees with file backlog_ref {case_data.get('backlog_ref')!r}.",
                    )
                )

            ep = case_data.get("expected_proposal") if isinstance(case_data.get("expected_proposal"), dict) else {}
            if entry.get("approval_status") != ep.get("approval_status"):
                issues.append(
                    LintIssue(
                        "APPROVAL_STATUS_MUST_BE_DRAFT",
                        f"manifest.json.cases[{cid}].approval_status",
                        f"Manifest approval_status {entry.get('approval_status')!r} disagrees with case file {ep.get('approval_status')!r}.",
                    )
                )
            if entry.get("review_status") != case_data.get("review_status"):
                issues.append(
                    LintIssue(
                        "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                        f"manifest.json.cases[{cid}].review_status",
                        f"Manifest review_status {entry.get('review_status')!r} disagrees with case file {case_data.get('review_status')!r}.",
                    )
                )
            if entry.get("file_sha256") != actual_sha:
                issues.append(
                    LintIssue(
                        "INTEGRITY_HASH_MISMATCH",
                        f"manifest.json.cases[{cid}].file_sha256",
                        f"Manifest file_sha256={entry.get('file_sha256')!r} does not match actual SHA-256 {actual_sha!r}.",
                    )
                )

            eg = case_data.get("eval_grouping") if isinstance(case_data.get("eval_grouping"), dict) else {}
            for gfield in ("eval_bundle_id", "source_lineage_group", "eval_partition"):
                if entry.get(gfield) != eg.get(gfield):
                    issues.append(
                        LintIssue(
                            "EVAL_BUNDLE_LINEAGE_ISOLATION",
                            f"manifest.json.cases[{cid}].{gfield}",
                            f"Manifest {gfield}={entry.get(gfield)!r} disagrees with case eval_grouping {eg.get(gfield)!r}.",
                        )
                    )

            cov = case_data.get("coverage") if isinstance(case_data.get("coverage"), dict) else {}
            for field in (
                "draft_requirement_ids",
                "draft_test_ids",
                "official_requirement_refs",
                "official_test_refs",
            ):
                if entry.get(field) != cov.get(field):
                    issues.append(
                        LintIssue(
                            "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                            f"manifest.json.cases[{cid}].{field}",
                            f"Manifest {field}={entry.get(field)!r} disagrees with case coverage {cov.get(field)!r}.",
                        )
                    )

    cases_dir = dataset_dir / "cases"
    if cases_dir.is_dir():
        for json_file in sorted(cases_dir.glob("*.json")):
            if json_file.resolve() not in manifest_case_paths:
                issues.append(
                    LintIssue(
                        "CASE_UNIQUENESS",
                        str(json_file.relative_to(dataset_dir)),
                        "Case file exists in cases/ but is not listed in manifest.json.",
                    )
                )

    # Validate approval_record consistency
    approval_rec = manifest.get("approval_record") if isinstance(manifest.get("approval_record"), dict) else {}
    approved_ids_list = approval_rec.get("approved_case_ids") if isinstance(approval_rec.get("approved_case_ids"), list) else []
    post_mvp_ids_list = (
        approval_rec.get("unapproved_post_mvp_draft_case_ids")
        if isinstance(approval_rec.get("unapproved_post_mvp_draft_case_ids"), list)
        else []
    )
    actual_approved_ids = sorted(
        cid
        for cid, cdata in loaded_cases.items()
        if isinstance(cdata.get("expected_proposal"), dict)
        and cdata["expected_proposal"].get("approval_status") == "APPROVED_MVP_REGRESSION"
    )
    actual_post_mvp_draft_ids = sorted(
        cid
        for cid, cdata in loaded_cases.items()
        if cdata.get("scope") == "POST_MVP"
        and isinstance(cdata.get("expected_proposal"), dict)
        and cdata["expected_proposal"].get("approval_status") == "DRAFT"
    )
    if sorted(approved_ids_list) != actual_approved_ids:
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                "manifest.json.approval_record.approved_case_ids",
                f"approval_record.approved_case_ids={sorted(approved_ids_list)!r} disagrees with approved MVP cases {actual_approved_ids!r}.",
            )
        )
    if sorted(post_mvp_ids_list) != actual_post_mvp_draft_ids:
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                "manifest.json.approval_record.unapproved_post_mvp_draft_case_ids",
                f"approval_record.unapproved_post_mvp_draft_case_ids={sorted(post_mvp_ids_list)!r} disagrees with Post-MVP drafts {actual_post_mvp_draft_ids!r}.",
            )
        )
    cpol = approval_rec.get("comparison_policy") if isinstance(approval_rec.get("comparison_policy"), dict) else {}
    if cpol.get("enforce_zero_mutation_on_rejection") is not True:
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                "manifest.json.approval_record.comparison_policy.enforce_zero_mutation_on_rejection",
                "Manifest comparison_policy MUST enforce zero mutation on rejection.",
            )
        )

    # Validate same-source evaluation bundles & lineage isolation
    eval_bundles = manifest.get("evaluation_bundles") if isinstance(manifest.get("evaluation_bundles"), list) else []
    bundle_case_seen: set[str] = set()
    bundle_map: dict[str, dict[str, Any]] = {}
    for idx, bundle in enumerate(eval_bundles):
        if not isinstance(bundle, dict):
            continue
        bid = bundle.get("eval_bundle_id", "")
        bpart = bundle.get("eval_partition", "")
        if bid in bundle_map:
            issues.append(
                LintIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", f"manifest.json.evaluation_bundles[{idx}]", f"Duplicate eval_bundle_id '{bid}'.")
            )
        bundle_map[bid] = bundle
        for cid in bundle.get("case_ids", []):
            if str(cid).startswith("NEG-"):
                issues.append(
                    LintIssue(
                        "NEGATIVE_VARIANT_ISOLATION",
                        f"manifest.json.evaluation_bundles[{bid}].case_ids",
                        f"Negative variant '{cid}' MUST NEVER appear in evaluation_bundles.",
                    )
                )
            if cid in bundle_case_seen:
                issues.append(
                    LintIssue(
                        "EVAL_BUNDLE_LINEAGE_ISOLATION",
                        f"manifest.json.evaluation_bundles[{bid}].case_ids",
                        f"Case '{cid}' appears in multiple evaluation bundles.",
                    )
                )
            bundle_case_seen.add(cid)
            if cid in loaded_cases:
                eg = loaded_cases[cid].get("eval_grouping") or {}
                if eg.get("eval_bundle_id") != bid or eg.get("eval_partition") != bpart:
                    issues.append(
                        LintIssue(
                            "EVAL_BUNDLE_LINEAGE_ISOLATION",
                            f"manifest.json.evaluation_bundles[{bid}]",
                            f"Case '{cid}' eval_grouping ({eg.get('eval_bundle_id')}/{eg.get('eval_partition')}) disagrees with bundle ({bid}/{bpart}).",
                        )
                    )
    if bundle_case_seen != seen_case_ids:
        issues.append(
            LintIssue(
                "EVAL_BUNDLE_LINEAGE_ISOLATION",
                "manifest.json.evaluation_bundles",
                f"evaluation_bundles cases {sorted(bundle_case_seen)} do not match manifest cases {sorted(seen_case_ids)}.",
            )
        )

    # Same-source invariant within approved MVP evaluation partition:
    # Any two MVP cases sharing a synthetic repository full_name MUST share source_lineage_group and eval_bundle_id.
    repo_to_mvp_bundle: dict[str, tuple[str, str, str]] = {}
    for cid in actual_approved_ids:
        cdata = loaded_cases[cid]
        eg = cdata.get("eval_grouping") if isinstance(cdata.get("eval_grouping"), dict) else {}
        bid = str(eg.get("eval_bundle_id", ""))
        lgrp = str(eg.get("source_lineage_group", ""))
        repos = cdata.get("synthetic_sources", {}).get("repositories", [])
        for r in repos:
            if not isinstance(r, dict):
                continue
            rname = str(r.get("full_name", ""))
            if not rname:
                continue
            if rname in repo_to_mvp_bundle:
                prev_cid, prev_bid, prev_lgrp = repo_to_mvp_bundle[rname]
                if prev_bid != bid or prev_lgrp != lgrp:
                    issues.append(
                        LintIssue(
                            "EVAL_BUNDLE_LINEAGE_ISOLATION",
                            f"cases[{cid}]",
                            f"Cases '{prev_cid}' and '{cid}' share synthetic source repository '{rname}' but have different eval_bundle_id/source_lineage_group ({prev_bid}/{prev_lgrp} vs {bid}/{lgrp}).",
                        )
                    )
            else:
                repo_to_mvp_bundle[rname] = (cid, bid, lgrp)

    # Validate SHA-256 integrity hashes
    ihashes = manifest.get("integrity_hashes") if isinstance(manifest.get("integrity_hashes"), dict) else {}
    if ihashes:
        if ihashes.get("hash_algorithm") != "sha256":
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.hash_algorithm",
                    "integrity_hashes.hash_algorithm must be 'sha256'.",
                )
            )
        app_map = ihashes.get("approved_mvp_cases_sha256") if isinstance(ihashes.get("approved_mvp_cases_sha256"), dict) else {}
        post_map = ihashes.get("post_mvp_draft_cases_sha256") if isinstance(ihashes.get("post_mvp_draft_cases_sha256"), dict) else {}
        if sorted(app_map.keys()) != actual_approved_ids:
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.approved_mvp_cases_sha256",
                    f"Keys {sorted(app_map.keys())} do not match approved MVP cases {actual_approved_ids}.",
                )
            )
        if sorted(post_map.keys()) != actual_post_mvp_draft_ids:
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.post_mvp_draft_cases_sha256",
                    f"Keys {sorted(post_map.keys())} do not match Post-MVP draft cases {actual_post_mvp_draft_ids}.",
                )
            )
        for label, hmap in (("approved_mvp_cases_sha256", app_map), ("post_mvp_draft_cases_sha256", post_map)):
            for cid, rec in hmap.items():
                if not isinstance(rec, dict):
                    continue
                if cid in actual_case_sha256 and rec.get("file_sha256") != actual_case_sha256[cid]:
                    issues.append(
                        LintIssue(
                            "INTEGRITY_HASH_MISMATCH",
                            f"manifest.json.integrity_hashes.{label}.{cid}.file_sha256",
                            f"Recorded SHA-256 {rec.get('file_sha256')!r} != actual {actual_case_sha256[cid]!r}.",
                        )
                    )

        def _recompute_bundle_sha(hmap: dict[str, Any], key_name: str) -> str:
            payload = "".join(f"{cid}:{hmap[cid].get(key_name, '')}\n" for cid in sorted(hmap))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        exp_mvp_bundle_sha = _recompute_bundle_sha(app_map, "file_sha256")
        if ihashes.get("approved_mvp_bundle_sha256") != exp_mvp_bundle_sha:
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.approved_mvp_bundle_sha256",
                    f"Recorded approved_mvp_bundle_sha256 {ihashes.get('approved_mvp_bundle_sha256')!r} != recomputed {exp_mvp_bundle_sha!r}.",
                )
            )
        exp_base_bundle_sha = _recompute_bundle_sha(app_map, "baseline_sha256_at_0773ac5")
        if ihashes.get("approved_mvp_baseline_bundle_sha256") != exp_base_bundle_sha:
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.approved_mvp_baseline_bundle_sha256",
                    f"Recorded approved_mvp_baseline_bundle_sha256 {ihashes.get('approved_mvp_baseline_bundle_sha256')!r} != recomputed {exp_base_bundle_sha!r}.",
                )
            )
        exp_post_bundle_sha = _recompute_bundle_sha(post_map, "file_sha256")
        if ihashes.get("post_mvp_draft_bundle_sha256") != exp_post_bundle_sha:
            issues.append(
                LintIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    "manifest.json.integrity_hashes.post_mvp_draft_bundle_sha256",
                    f"Recorded post_mvp_draft_bundle_sha256 {ihashes.get('post_mvp_draft_bundle_sha256')!r} != recomputed {exp_post_bundle_sha!r}.",
                )
            )
        schema_hashes = ihashes.get("schema_files_sha256") if isinstance(ihashes.get("schema_files_sha256"), dict) else {}
        for srel, ssha in schema_hashes.items():
            spath = dataset_dir / srel
            if not spath.is_file():
                issues.append(LintIssue("FILE_NOT_FOUND", srel, "Schema file listed in integrity_hashes does not exist."))
            elif _sha256_file(spath) != ssha:
                issues.append(
                    LintIssue(
                        "INTEGRITY_HASH_MISMATCH",
                        f"manifest.json.integrity_hashes.schema_files_sha256.{srel}",
                        f"Schema SHA-256 {ssha!r} != actual {_sha256_file(spath)!r}.",
                    )
                )

    neg_entries = manifest.get("negative_variants") if isinstance(manifest.get("negative_variants"), list) else []
    for idx, nentry in enumerate(neg_entries):
        if not isinstance(nentry, dict):
            continue
        rel_path = nentry.get("path", "")
        if not rel_path.startswith("negative-fixtures/"):
            issues.append(
                LintIssue(
                    "NEGATIVE_VARIANT_ISOLATION",
                    f"manifest.json.negative_variants[{idx}].path",
                    f"Negative variant must live under negative-fixtures/, got '{rel_path}'.",
                )
            )
        if nentry.get("eval_excluded") is not True:
            issues.append(
                LintIssue(
                    "NEGATIVE_VARIANT_ISOLATION",
                    f"manifest.json.negative_variants[{idx}].eval_excluded",
                    "Negative variant must declare eval_excluded=true (isolated from evaluation dataset).",
                )
            )
        if not (dataset_dir / rel_path).is_file():
            issues.append(
                LintIssue(
                    "FILE_NOT_FOUND",
                    rel_path,
                    "Negative variant listed in manifest does not exist.",
                )
            )

    cov_data, cov_issues = _load_json(dataset_dir / "coverage-matrix.json")
    issues.extend(cov_issues)
    if isinstance(cov_data, dict):
        covered_cases: set[str] = set()
        for row in cov_data.get("matrix", []):
            if isinstance(row, dict):
                covered_cases.update(row.get("input_cases", []))
        for cid in sorted(seen_case_ids - covered_cases):
            issues.append(
                LintIssue(
                    "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                    "coverage-matrix.json",
                    f"Case '{cid}' is missing from coverage-matrix.json.",
                )
            )
        for cid in sorted(covered_cases - seen_case_ids):
            issues.append(
                LintIssue(
                    "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                    "coverage-matrix.json",
                    f"coverage-matrix.json references unknown case '{cid}'.",
                )
            )

    rq_data, rq_issues = _load_json(dataset_dir / "review-queue.json")
    issues.extend(rq_issues)
    if isinstance(rq_data, dict):
        queued_cases: set[str] = set()
        for item in rq_data.get("queue", []):
            if isinstance(item, dict):
                cid = item.get("case_id", "")
                queued_cases.add(cid)
                if cid in manifest_case_map:
                    if item.get("case_path") != manifest_case_map[cid].get("path"):
                        issues.append(
                            LintIssue(
                                "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                                f"review-queue.json[{cid}]",
                                "case_path in review-queue.json does not match manifest.json.",
                            )
                        )
                    if item.get("scope") != manifest_case_map[cid].get("scope"):
                        issues.append(
                            LintIssue(
                                "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                                f"review-queue.json[{cid}]",
                                "scope in review-queue.json does not match manifest.json.",
                            )
                        )
        for cid in sorted(seen_case_ids - queued_cases):
            issues.append(
                LintIssue(
                    "COVERAGE_AND_REVIEW_QUEUE_SYNC",
                    "review-queue.json",
                    f"Case '{cid}' is missing from review-queue.json.",
                )
            )

    return issues


def verify_negative_variants(dataset_dir: Path) -> tuple[bool, list[str]]:
    manifest, m_issues = _load_json(dataset_dir / "manifest.json")
    if m_issues or not isinstance(manifest, dict):
        return False, [i.format() for i in m_issues]

    report_lines: list[str] = []
    all_ok = True
    for nentry in manifest.get("negative_variants", []):
        vid = nentry["variant_id"]
        path = dataset_dir / nentry["path"]
        expected_codes = set(nentry["expected_rule_codes"])
        found_issues = lint_case_file(path)
        found_codes = {i.rule_code for i in found_issues}
        missing_codes = expected_codes - found_codes
        if not found_issues or missing_codes:
            all_ok = False
            report_lines.append(
                f"FAIL {vid} ({nentry['path']}): expected {sorted(expected_codes)}, got {sorted(found_codes)}"
            )
        else:
            report_lines.append(
                f"CAUGHT {vid} ({nentry['path']}): {sorted(found_codes)} ({len(found_issues)} issue(s))"
            )
    return all_ok, report_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint Event synthetic dataset fixtures (MVP approved + Post-MVP draft) and negative variants."
    )
    parser.add_argument("--dataset-dir", type=Path, help="Path to evaluation/event-draft directory.")
    parser.add_argument("--case-file", type=Path, help="Lint a single case JSON file.")
    parser.add_argument(
        "--verify-negatives",
        action="store_true",
        help="Also run all negative_variants from manifest.json and assert each triggers its expected rule codes.",
    )
    args = parser.parse_args(argv)

    if not args.dataset_dir and not args.case_file:
        parser.error("Provide --dataset-dir or --case-file.")

    if args.case_file:
        issues = lint_case_file(args.case_file)
        if issues:
            print(f"LINT FAILED ({len(issues)} issue(s)) for {args.case_file}:")
            for issue in issues:
                print("  " + issue.format())
            return 1
        print(f"LINT PASSED: {args.case_file}")
        return 0

    issues = lint_dataset(args.dataset_dir)
    if issues:
        print(f"DATASET LINT FAILED ({len(issues)} issue(s)):")
        for issue in issues:
            print("  " + issue.format())
        return 1

    manifest_data, _ = _load_json(args.dataset_dir / "manifest.json")
    case_count = len(manifest_data.get("cases", [])) if isinstance(manifest_data, dict) else 0
    ver = manifest_data.get("dataset_version", "unknown") if isinstance(manifest_data, dict) else "unknown"
    app_rec = manifest_data.get("approval_record", {}) if isinstance(manifest_data, dict) else {}
    n_app = len(app_rec.get("approved_case_ids", []))
    n_post = len(app_rec.get("unapproved_post_mvp_draft_case_ids", []))
    print(
        f"DATASET LINT PASSED: {case_count} cases verified "
        f"(version={ver}, {n_app} MVP approved, {n_post} Post-MVP DRAFT, same-source bundles & SHA-256 integrity verified)."
    )

    if args.verify_negatives:
        ok, lines = verify_negative_variants(args.dataset_dir)
        for line in lines:
            print("  " + line)
        if not ok:
            print("NEGATIVE VARIANT VERIFICATION FAILED.")
            return 1
        print(f"NEGATIVE VARIANT VERIFICATION PASSED: {len(lines)} isolated broken variants caught as expected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
