#!/usr/bin/env python3
"""Automated format, referential, governance, and Event-domain invariant linter
for Career Memory T-001A Event synthetic dataset draft.

Zero external dependencies (Python 3 standard library only).
Does NOT perform any network, GitHub, Notion, or LLM API calls.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_SCOPES = {"MVP", "POST_MVP"}
ALLOWED_REVIEW_STATUSES = {"PENDING_HUMAN_REVIEW", "REVIEW_REQUIRED"}
ALLOWED_LOOKUP_STATUS = {"COMPLETED", "INCOMPLETE_OR_FAILED"}
ALLOWED_PR_ASSOC = {"ASSOCIATED", "NO_MATCH_OBSERVED", "UNKNOWN"}
ALLOWED_COMMAND_ACTIONS = {"split", "manual_merge", "exclude", "dissolve"}
ALLOWED_LOGICAL_KINDS = {
    "merged_pr_event",
    "direct_commit_event",
    "user_composite_event",
    "split_child_event",
}
ALLOWED_SPEC_EVENT_TYPES = {"USER_COMPOSITE", "UNCONFIRMED"}
ALLOWED_LOGICAL_STATES = {"active", "excluded", "dissolved", "superseded"}
ALLOWED_MERGE_ORIGINS = {"NONE", "USER_MANUAL_MERGE"}
ALLOWED_SPEC_ERROR_CODES = {None, "CANNOT_LINK", "UNCONFIRMED"}
ALLOWED_REJECTION_CATEGORIES = {
    None,
    "cannot_link_non_event_or_unknown",
    "cannot_link_nested_user_composite",
    "cannot_link_excluded_event",
    "cannot_link_cross_repository",
    "version_conflict",
    "workspace_boundary_conflict",
}
ALLOWED_DISPOSITIONS = {
    "hold_classification_unknown_assoc",
    "not_direct_commit_candidate_non_default_branch",
    "deferred_post_mvp",
}
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
    "command_results",
    "claim_side_effects",
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
    "task_ref",
    "approval_status",
    "base_commit_sha",
    "scope_boundary",
    "generation_provenance",
    "reference_docs",
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

    # 2. Scope & B-003 policy
    scope = case.get("scope")
    backlog_ref = case.get("backlog_ref")
    post_mvp_reason = case.get("post_mvp_reason")
    coverage = case.get("coverage") if isinstance(case.get("coverage"), dict) else {}
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

    # 3. Rationale separation (confirmed rules vs unconfirmed tokens vs truly undecided)
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
        elif repo_map[rid].get("workspace_id") != sr.get("workspace_id"):
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
        if ev.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.initial_events[{idx}].workspace_id",
                    f"Unknown workspace_id '{ev.get('workspace_id')}'.",
                )
            )
        rid = ev.get("repo_id")
        if rid not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.initial_events[{idx}].repo_id",
                    f"Unknown repo_id '{rid}'.",
                )
            )
        elif repo_map[rid].get("workspace_id") != ev.get("workspace_id"):
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.initial_events[{idx}]",
                    "initial_event.workspace_id does not match repo.workspace_id.",
                )
            )
        if ev.get("primary_pr_id") is not None and ev.get("primary_pr_id") not in pr_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.synthetic_sources.initial_events[{idx}].primary_pr_id",
                    f"Unknown primary_pr_id '{ev.get('primary_pr_id')}'.",
                )
            )
        for cid in ev.get("source_commit_ids", []):
            if cid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.initial_events[{idx}].source_commit_ids",
                        f"Unknown commit_id '{cid}'.",
                    )
                )
        for tid in ev.get("context_ticket_ids", []):
            if tid not in ticket_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.synthetic_sources.initial_events[{idx}].context_ticket_ids",
                        f"Unknown ticket_id '{tid}'.",
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

    # 5. Validate expected_proposal top-level & DRAFT governance
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

    if "approval_status" in expected and expected.get("approval_status") != "DRAFT":
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                f"{location}.expected_proposal.approval_status",
                f"Expected approval_status='DRAFT' (must never be frozen as golden by agent); got {expected.get('approval_status')!r}.",
            )
        )
    if expected.get("approved_by") is not None:
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                f"{location}.expected_proposal.approved_by",
                "approved_by must be null in T-001A draft.",
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
                    f"Unsupported or guessed spec_error_code {spec_err!r}; only null, 'CANNOT_LINK', or 'UNCONFIRMED' permitted without Notion access.",
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
            if rej_cat is None or spec_err is None or mutated is not False:
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.expected_proposal.command_results[{idx}]",
                        "REJECTED command must have non-null rejection_category, spec_error_code ('CANNOT_LINK' or 'UNCONFIRMED'), and state_mutated=false.",
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

    # Check target references, workspace/repo boundaries, nesting prohibition, and version conflicts
    all_source_entity_ids = set(init_event_map) | set(ticket_map) | set(commit_map) | set(pr_map)
    accepted_manual_merge_cmd_ids: set[str] = set()
    for cid, cmd in cmd_map.items():
        res = cmd_result_map.get(cid, {})
        outcome = res.get("outcome")
        rej_cat = res.get("rejection_category")
        spec_err = res.get("spec_error_code")
        action = cmd.get("command_action")
        if action == "manual_merge" and outcome == "ACCEPTED":
            accepted_manual_merge_cmd_ids.add(cid)

        targets = cmd.get("target_event_ids") if isinstance(cmd.get("target_event_ids"), list) else []
        target_ws: set[str] = set()
        target_repos: set[str] = set()
        version_mismatch = False
        includes_composite_or_subsumed = False
        target_versions = cmd.get("target_versions") if isinstance(cmd.get("target_versions"), dict) else {}

        for tid in targets:
            if tid in init_event_map:
                ev = init_event_map[tid]
                target_ws.add(ev["workspace_id"])
                target_repos.add(ev["repo_id"])
                supplied_v = target_versions.get(tid, cmd.get("expected_version"))
                if isinstance(supplied_v, int) and supplied_v != ev.get("version"):
                    version_mismatch = True
                if ev.get("logical_kind") == "user_composite_event" or ev.get("membership_role") == "subsumed_by_user_composite":
                    includes_composite_or_subsumed = True
            elif outcome == "REJECTED" and spec_err == "CANNOT_LINK" and tid in all_source_entity_ids:
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
        if cross_repo_merge and (outcome != "REJECTED" or res.get("state_mutated") is not False):
            issues.append(
                LintIssue(
                    "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                    f"{location}.expected_proposal.command_results[{cid}]",
                    "Cross-repository manual_merge MUST be REJECTED with state_mutated=false (USER_COMPOSITE is same-repository only).",
                )
            )
        if action == "manual_merge" and includes_composite_or_subsumed:
            if outcome != "REJECTED" or res.get("state_mutated") is not False:
                issues.append(
                    LintIssue(
                        "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                        f"{location}.expected_proposal.command_results[{cid}]",
                        "Nesting USER_COMPOSITE (merging composite root or already-subsumed member) is forbidden and MUST be REJECTED.",
                    )
                )
        if version_mismatch and not ws_mismatch:
            if outcome != "REJECTED" or rej_cat != "version_conflict":
                issues.append(
                    LintIssue(
                        "COMMAND_VERSION_RULES",
                        f"{location}.expected_proposal.command_results[{cid}]",
                        "Stale version command MUST be REJECTED with rejection_category='version_conflict'.",
                    )
                )

    # 7. Validate expected events & all confirmed domain invariants
    exp_events = expected.get("events") if isinstance(expected.get("events"), list) else []
    held_items = expected.get("held_items") if isinstance(expected.get("held_items"), list) else []
    claim_effects = expected.get("claim_side_effects") if isinstance(expected.get("claim_side_effects"), list) else []
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

        if ev.get("workspace_id") not in ws_id_set:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.events[{idx}].workspace_id",
                    f"Unknown workspace_id '{ev.get('workspace_id')}'.",
                )
            )
        ev_repo = ev.get("repo_id")
        if ev_repo not in repo_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.events[{idx}].repo_id",
                    f"Unknown repo_id '{ev_repo}'.",
                )
            )
        elif repo_map[ev_repo].get("workspace_id") != ev.get("workspace_id"):
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.events[{idx}]",
                    "Event workspace_id does not match repo.workspace_id.",
                )
            )

        lkind = ev.get("logical_kind")
        spec_type = ev.get("spec_event_type")
        lstate = ev.get("logical_state")
        spec_status = ev.get("spec_lifecycle_status")

        if lkind in {"issue", "ticket", "context"}:
            issues.append(
                LintIssue(
                    "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                    f"{location}.expected_proposal.events[{idx}].logical_kind",
                    f"Issue/ticket cannot be a standalone Event ({lkind!r}).",
                )
            )
        elif lkind not in ALLOWED_LOGICAL_KINDS:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.events[{idx}].logical_kind",
                    f"Invalid logical_kind {lkind!r}.",
                )
            )

        if spec_type not in ALLOWED_SPEC_EVENT_TYPES:
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.events[{idx}].spec_event_type",
                    f"Guessed spec_event_type {spec_type!r}; must be 'USER_COMPOSITE' or 'UNCONFIRMED'.",
                )
            )
        elif lkind == "user_composite_event" and spec_type != "USER_COMPOSITE":
            issues.append(
                LintIssue(
                    "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                    f"{location}.expected_proposal.events[{idx}].spec_event_type",
                    "user_composite_event must use confirmed spec_event_type='USER_COMPOSITE'.",
                )
            )
        elif lkind != "user_composite_event" and spec_type != "UNCONFIRMED":
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.events[{idx}].spec_event_type",
                    f"Non-composite Event '{eid}' must leave spec_event_type='UNCONFIRMED', got {spec_type!r}.",
                )
            )

        if lstate not in ALLOWED_LOGICAL_STATES:
            issues.append(
                LintIssue(
                    "SCHEMA_REQUIRED_FIELD",
                    f"{location}.expected_proposal.events[{idx}].logical_state",
                    f"Invalid logical_state {lstate!r}.",
                )
            )
        if spec_status != "UNCONFIRMED":
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.events[{idx}].spec_lifecycle_status",
                    f"Unconfirmed lifecycle status must be 'UNCONFIRMED', not guessed as {spec_status!r}.",
                )
            )

        origin_cmd = ev.get("origin_command_id")
        if origin_cmd is not None and origin_cmd not in cmd_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.events[{idx}].origin_command_id",
                    f"Unknown origin_command_id '{origin_cmd}'.",
                )
            )

        ppr = ev.get("primary_pr_id")
        if ppr is not None:
            if ppr not in pr_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.events[{idx}].primary_pr_id",
                        f"Unknown primary_pr_id '{ppr}'.",
                    )
                )
            else:
                if lkind == "merged_pr_event" and pr_map[ppr].get("state") != "MERGED":
                    issues.append(
                        LintIssue(
                            "INVARIANT_MERGED_PR_CANDIDATE",
                            f"{location}.expected_proposal.events[{idx}].primary_pr_id",
                            f"Merged PR candidate Event '{eid}' references non-MERGED PR '{ppr}' (state={pr_map[ppr].get('state')!r}).",
                        )
                    )
                if pr_map[ppr].get("repo_id") != ev_repo:
                    issues.append(
                        LintIssue(
                            "ID_REFERENCE_INTEGRITY",
                            f"{location}.expected_proposal.events[{idx}].repo_id",
                            f"Event repo_id '{ev_repo}' disagrees with primary PR repo_id '{pr_map[ppr].get('repo_id')}'.",
                        )
                    )

        src_commits = ev.get("source_commit_ids", [])
        if lkind == "direct_commit_event" and not src_commits:
            issues.append(
                LintIssue(
                    "INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED",
                    f"{location}.expected_proposal.events[{idx}].source_commit_ids",
                    "direct_commit_event must reference at least one default-branch NO_MATCH_OBSERVED commit.",
                )
            )

        for cid in src_commits:
            if cid not in commit_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.events[{idx}].source_commit_ids",
                        f"Unknown commit_id '{cid}'.",
                    )
                )
            else:
                cmt = commit_map[cid]
                if cid in unknown_commit_ids:
                    issues.append(
                        LintIssue(
                            "INVARIANT_UNKNOWN_NOT_NO_PR",
                            f"{location}.expected_proposal.events[{idx}].source_commit_ids",
                            f"Commit '{cid}' has pr_association_status='UNKNOWN' and MUST NOT be classified into Event '{eid}' ({lkind}).",
                        )
                    )
                elif lkind == "direct_commit_event":
                    if (
                        cmt.get("on_default_branch") is not True
                        or cmt.get("pr_association_lookup_status") != "COMPLETED"
                        or cmt.get("pr_association_status") != "NO_MATCH_OBSERVED"
                    ):
                        issues.append(
                            LintIssue(
                                "INVARIANT_DIRECT_COMMIT_REQUIRES_NO_MATCH_OBSERVED",
                                f"{location}.expected_proposal.events[{idx}].source_commit_ids",
                                f"Direct-commit Event '{eid}' commit '{cid}' requires on_default_branch=true, pr_association_lookup_status='COMPLETED', and pr_association_status='NO_MATCH_OBSERVED'.",
                            )
                        )
                if cmt.get("repo_id") != ev_repo:
                    issues.append(
                        LintIssue(
                            "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                            f"{location}.expected_proposal.events[{idx}].source_commit_ids",
                            f"Commit '{cid}' belongs to repo '{cmt.get('repo_id')}', which differs from Event repo '{ev_repo}'.",
                        )
                    )

        for tid in ev.get("context_ticket_ids", []):
            if tid not in ticket_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.events[{idx}].context_ticket_ids",
                        f"Unknown context_ticket_id '{tid}'.",
                    )
                )

        # No context-only / automatic multi-PR merge invariant
        merge_origin = ev.get("merge_origin")
        if merge_origin not in ALLOWED_MERGE_ORIGINS:
            issues.append(
                LintIssue(
                    "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                    f"{location}.expected_proposal.events[{idx}].merge_origin",
                    f"Forbidden merge_origin {merge_origin!r}; automatic multi-PR or Issue/ticket-link merges are prohibited.",
                )
            )
        if lkind == "user_composite_event":
            if merge_origin != "USER_MANUAL_MERGE":
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                        f"{location}.expected_proposal.events[{idx}].merge_origin",
                        "USER_COMPOSITE MUST have merge_origin='USER_MANUAL_MERGE'.",
                    )
                )
            preexisted_manual = (
                eid in init_event_map
                and init_event_map[eid].get("logical_kind") == "user_composite_event"
                and init_event_map[eid].get("merge_origin") == "USER_MANUAL_MERGE"
            )
            created_by_manual_cmd = origin_cmd in accepted_manual_merge_cmd_ids
            if not preexisted_manual and not created_by_manual_cmd:
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_CONTEXT_AUTO_MERGE",
                        f"{location}.expected_proposal.events[{idx}]",
                        f"USER_COMPOSITE '{eid}' has neither a pre-existing USER_MANUAL_MERGE state nor an accepted manual_merge command.",
                    )
                )

        if lstate in {"excluded", "dissolved", "superseded"}:
            if ev.get("searchable_as_top_level") is not False or ev.get("counted_in_metrics") is not False:
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                        f"{location}.expected_proposal.events[{idx}]",
                        f"Event '{eid}' with logical_state={lstate!r} must have searchable_as_top_level=false and counted_in_metrics=false.",
                    )
                )

    _check_unique(exp_event_ids, "CASE_UNIQUENESS", f"{location}.expected_proposal.events", "event_id", issues)

    # Cross-check member_event_ids, same-repo, no-nesting, and composite double-counting prevention
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
                if ev.get("logical_kind") == "user_composite_event":
                    # Same-workspace and same-repository check
                    if mem.get("workspace_id") != ev.get("workspace_id") or mem.get("repo_id") != ev.get("repo_id"):
                        issues.append(
                            LintIssue(
                                "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                                f"{location}.expected_proposal.events[{idx}].member_event_ids",
                                f"USER_COMPOSITE '{eid}' member '{mid}' has workspace/repo ({mem.get('workspace_id')}/{mem.get('repo_id')}) != ({ev.get('workspace_id')}/{ev.get('repo_id')}).",
                            )
                        )
                    # Nesting prohibition
                    if mem.get("logical_kind") == "user_composite_event" or mem.get("spec_event_type") == "USER_COMPOSITE":
                        issues.append(
                            LintIssue(
                                "INVARIANT_USER_COMPOSITE_SAME_REPO_AND_NO_NESTING",
                                f"{location}.expected_proposal.events[{idx}].member_event_ids",
                                f"USER_COMPOSITE '{eid}' nests another USER_COMPOSITE '{mid}'; nesting is forbidden.",
                            )
                        )

        if ev.get("logical_kind") == "user_composite_event" and ev.get("logical_state") == "active":
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
                    if mem.get("searchable_as_top_level") is not False or mem.get("counted_in_metrics") is not False:
                        issues.append(
                            LintIssue(
                                "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                                f"{location}.expected_proposal.events[{mid}]",
                                f"USER_COMPOSITE '{eid}' member '{mid}' must have searchable_as_top_level=false and counted_in_metrics=false.",
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

    # Check dissolve -> dependent Claims marked STALE
    claim_effect_index: dict[tuple[str, str], dict[str, Any]] = {}
    for idx, ce in enumerate(claim_effects):
        if not isinstance(ce, dict):
            continue
        ce_claim = ce.get("claim_id", "")
        ce_comp = ce.get("source_composite_event_id", "")
        claim_effect_index[(ce_comp, ce_claim)] = ce
        if ce_comp not in exp_event_map:
            issues.append(
                LintIssue(
                    "ID_REFERENCE_INTEGRITY",
                    f"{location}.expected_proposal.claim_side_effects[{idx}].source_composite_event_id",
                    f"Unknown source_composite_event_id '{ce_comp}'.",
                )
            )
        if ce.get("expected_effect") != "STALE":
            issues.append(
                LintIssue(
                    "INVARIANT_DISSOLVE_MARKS_DEPENDENT_CLAIMS_STALE",
                    f"{location}.expected_proposal.claim_side_effects[{idx}].expected_effect",
                    f"Expected 'STALE' for claim side effect, got {ce.get('expected_effect')!r}.",
                )
            )
        if ce.get("spec_claim_status") != "UNCONFIRMED":
            issues.append(
                LintIssue(
                    "NO_GUESSED_UNCONFIRMED_SPEC_TOKENS",
                    f"{location}.expected_proposal.claim_side_effects[{idx}].spec_claim_status",
                    f"Unconfirmed Claim status token must be 'UNCONFIRMED', got {ce.get('spec_claim_status')!r}.",
                )
            )

    for init_id, init_ev in init_event_map.items():
        if init_ev.get("logical_kind") != "user_composite_event":
            continue
        dep_claims = init_ev.get("dependent_claim_ids", [])
        if not dep_claims:
            continue
        exp_ev = exp_event_map.get(init_id)
        if exp_ev and exp_ev.get("logical_state") == "dissolved":
            for clm_id in dep_claims:
                if (init_id, clm_id) not in claim_effect_index:
                    issues.append(
                        LintIssue(
                            "INVARIANT_DISSOLVE_MARKS_DEPENDENT_CLAIMS_STALE",
                            f"{location}.expected_proposal.claim_side_effects",
                            f"Dissolved USER_COMPOSITE '{init_id}' has dependent claim '{clm_id}' that MUST be marked STALE in claim_side_effects.",
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

    # 8. Validate held_items and UNKNOWN commit coverage
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
        elif stype == "OBSERVATION":
            if sid not in obs_map:
                issues.append(
                    LintIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{location}.expected_proposal.held_items[{idx}].source_id",
                        f"Held OBSERVATION references unknown observation_id '{sid}'.",
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
                    f"Commit '{ucid}' has pr_association_status='UNKNOWN' and must appear in held_items.",
                )
            )

    # 9. Validate search_checks & aggregation_check (double-counting guard)
    for idx, sc in enumerate(search_checks):
        if not isinstance(sc, dict):
            continue
        seids = sc.get("expected_event_ids", [])
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
            elif exp_event_map[seid].get("searchable_as_top_level") is not True:
                issues.append(
                    LintIssue(
                        "INVARIANT_NO_COMPOSITE_DOUBLE_COUNT",
                        f"{location}.expected_proposal.search_checks[{idx}].expected_event_ids",
                        f"Non-top-level Event '{seid}' must not appear in top-level search results.",
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
        and ev.get("logical_kind") == "user_composite_event"
        and ev.get("logical_state") == "active"
        and ev.get("counted_in_metrics") is True
    )
    actual_excluded = sum(1 for ev in exp_events if isinstance(ev, dict) and ev.get("logical_state") == "excluded")
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
    if agg.get("total_excluded_events") != actual_excluded:
        issues.append(
            LintIssue(
                "SCHEMA_REQUIRED_FIELD",
                f"{location}.expected_proposal.aggregation_check.total_excluded_events",
                f"Expected total_excluded_events={actual_excluded}, got {agg.get('total_excluded_events')!r}.",
            )
        )

    return issues


def lint_case_file(path: Path) -> list[LintIssue]:
    data, issues = _load_json(path)
    if issues:
        return issues
    return lint_case_data(data, str(path))


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

    if manifest.get("approval_status") != "DRAFT":
        issues.append(
            LintIssue(
                "APPROVAL_STATUS_MUST_BE_DRAFT",
                "manifest.json.approval_status",
                f"Manifest approval_status must be 'DRAFT', got {manifest.get('approval_status')!r}.",
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
        issues.extend(lint_case_data(case_data, rel_path))

        if isinstance(case_data, dict):
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
        description="Lint T-001A Event synthetic dataset draft fixtures and negative variants."
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
    print(f"DATASET LINT PASSED: {case_count} draft cases, coverage matrix, and review queue verified (DRAFT only).")

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
