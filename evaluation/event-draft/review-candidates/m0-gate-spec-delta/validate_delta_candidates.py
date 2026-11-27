#!/usr/bin/env python3
"""Deterministic validator for isolated M0-GATE spec-delta review candidates.

Scope:
- Validates ONLY evaluation/event-draft/review-candidates/m0-gate-spec-delta/
  and verifies that the parent 1.0.0-mvp-approved dataset remains untouched.
- Does NOT replace evaluation/event-draft/lint/lint_fixtures.py and does NOT
  constitute human owner approval or M3/EVAL-EVENT runtime evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EXPECTED_BASELINE_MANIFEST_SHA256 = "6aa16ce3bb0eb0a3c79d7bd8efd058e446fff7f9fc05d88cd06764de9a7bd806"
EXPECTED_EV002_BASELINE_SHA256 = "354be261b848463ac0fa4660203817af7890f06c37be5d0f74669e40cc194a6f"

FORBIDDEN_OD007_PATTERNS = [
    re.compile(r"\bCREATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bFOREIGN\s+KEY\b", re.IGNORECASE),
    re.compile(r"\bPRIMARY\s+KEY\b", re.IGNORECASE),
    re.compile(r"\bSELECT\b.+\bFROM\b", re.IGNORECASE),
    re.compile(r"\b(work_events|event_memberships|event_versions|event_relations_tbl|review_actions_tbl)\b", re.IGNORECASE),
    re.compile(r"\b(postgres|postgresql|sqlite|mysql)\b", re.IGNORECASE),
]

INVENTED_UI_API_TOKENS = {"keep", "KEEP", "keep_event", "KEEP_EVENT"}
INVENTED_KEEP_ROUTE_PATTERN = re.compile(r"/actions/keep\b|/keep\b", re.IGNORECASE)
ELIGIBLE_MERGE_ANCHORS = {"PR", "PR_SEGMENT", "DIRECT_COMMIT"}
ACTIVE_LIFECYCLE_STATUSES = {"CANDIDATE", "CONFIRMED"}
OFFICIAL_LIFECYCLE_STATUSES = {"CANDIDATE", "CONFIRMED", "REJECTED", "SUPERSEDED", "STALE"}


@dataclass(frozen=True)
class ValidationIssue:
    rule_code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.rule_code}] {self.location}: {self.message}"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_iso_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _load_json(path: Path) -> tuple[Any, list[ValidationIssue]]:
    if not path.is_file():
        return None, [ValidationIssue("FILE_NOT_FOUND", str(path), "Required file does not exist.")]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as exc:
        return None, [ValidationIssue("JSON_PARSE_ERROR", str(path), f"Invalid JSON: {exc}")]


def _validate_against_schema(obj: Any, schema: dict[str, Any], path: str = "$") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = expected_type if isinstance(expected_type, list) else [expected_type]
        type_ok = False
        for tp in allowed_types:
            if tp == "null" and obj is None:
                type_ok = True
            elif tp == "boolean" and isinstance(obj, bool):
                type_ok = True
            elif tp == "integer" and isinstance(obj, int) and not isinstance(obj, bool):
                type_ok = True
            elif tp == "number" and isinstance(obj, (int, float)) and not isinstance(obj, bool):
                type_ok = True
            elif tp == "string" and isinstance(obj, str):
                type_ok = True
            elif tp == "array" and isinstance(obj, list):
                type_ok = True
            elif tp == "object" and isinstance(obj, dict):
                type_ok = True
        if not type_ok:
            issues.append(
                ValidationIssue("SCHEMA_VALIDATION", path, f"Expected type {allowed_types}, got {type(obj).__name__}.")
            )
            return issues

    if "const" in schema and obj != schema["const"]:
        issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Expected const {schema['const']!r}, got {obj!r}."))
    if "enum" in schema and obj not in schema["enum"]:
        issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Value {obj!r} not in allowed enum {schema['enum']!r}."))
    if isinstance(obj, str):
        if "minLength" in schema and len(obj) < schema["minLength"]:
            issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"String shorter than minLength {schema['minLength']}."))
        if "pattern" in schema and not re.search(schema["pattern"], obj):
            issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"String {obj!r} fails pattern {schema['pattern']!r}."))
    if isinstance(obj, int) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Integer {obj} < minimum {schema['minimum']}."))
    if isinstance(obj, list):
        if "minItems" in schema and len(obj) < schema["minItems"]:
            issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Array length {len(obj)} < minItems {schema['minItems']}."))
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Array length {len(obj)} > maxItems {schema['maxItems']}."))
        if "items" in schema and isinstance(schema["items"], dict):
            for idx, item in enumerate(obj):
                issues.extend(_validate_against_schema(item, schema["items"], f"{path}[{idx}]"))
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                issues.append(ValidationIssue("SCHEMA_VALIDATION", path, f"Missing required property '{req}'."))
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for key, val in obj.items():
            if key in props:
                issues.extend(_validate_against_schema(val, props[key], f"{path}.{key}"))
            elif addl is False:
                issues.append(ValidationIssue("SCHEMA_VALIDATION", f"{path}.{key}", f"Unexpected additional property '{key}'."))
            elif isinstance(addl, dict):
                issues.extend(_validate_against_schema(val, addl, f"{path}.{key}"))
    return issues


def _walk_strings(obj: Any, path: str = "$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{idx}]")


def check_baseline_preservation(dataset_dir: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    manifest_path = dataset_dir / "manifest.json"
    manifest, load_issues = _load_json(manifest_path)
    if load_issues or not isinstance(manifest, dict):
        return load_issues

    actual_manifest_sha = _sha256_file(manifest_path)
    if actual_manifest_sha != EXPECTED_BASELINE_MANIFEST_SHA256:
        issues.append(
            ValidationIssue(
                "BASELINE_PRESERVATION",
                "manifest.json",
                f"Baseline 1.0.0-mvp-approved manifest.json SHA-256 changed: {actual_manifest_sha} != {EXPECTED_BASELINE_MANIFEST_SHA256}.",
            )
        )
    if manifest.get("dataset_version") != "1.0.0-mvp-approved":
        issues.append(
            ValidationIssue(
                "BASELINE_PRESERVATION",
                "manifest.json.dataset_version",
                f"Expected untouched baseline '1.0.0-mvp-approved', got {manifest.get('dataset_version')!r}.",
            )
        )

    ev002_path = dataset_dir / "cases" / "EV-002-default-branch-direct-commit.json"
    if not ev002_path.is_file() or _sha256_file(ev002_path) != EXPECTED_EV002_BASELINE_SHA256:
        issues.append(
            ValidationIssue(
                "BASELINE_PRESERVATION",
                "cases/EV-002-default-branch-direct-commit.json",
                "Baseline EV-002 file must remain untouched.",
            )
        )

    app_rec = manifest.get("approval_record", {})
    if sorted(app_rec.get("unapproved_post_mvp_draft_case_ids", [])) != ["EV-017", "EV-018"]:
        issues.append(
            ValidationIssue(
                "BASELINE_PRESERVATION",
                "manifest.json.approval_record.unapproved_post_mvp_draft_case_ids",
                "EV-017 and EV-018 must remain unapproved Post-MVP drafts.",
            )
        )
    for nentry in manifest.get("negative_variants", []):
        if nentry.get("eval_excluded") is not True or not str(nentry.get("path", "")).startswith("negative-fixtures/"):
            issues.append(
                ValidationIssue(
                    "NEGATIVE_VARIANT_ISOLATION",
                    f"manifest.json.negative_variants[{nentry.get('variant_id')}]",
                    "Negative variants must remain isolated under negative-fixtures/ with eval_excluded=true.",
                )
            )
    return issues


def check_gap1_ev002_confirmation_hint(
    case_data: dict[str, Any], dataset_dir: Path, loc: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    bref = case_data.get("baseline_case_ref")
    if not isinstance(bref, dict):
        issues.append(ValidationIssue("GAP1_EV002_CONFIRM_HINT", loc, "DELTA-EV-002 must include baseline_case_ref."))
        return issues

    ev002_path = dataset_dir / bref.get("baseline_path", "")
    ev002_data, load_issues = _load_json(ev002_path)
    if load_issues or not isinstance(ev002_data, dict):
        issues.extend(load_issues)
        return issues

    if _sha256_file(ev002_path) != bref.get("baseline_file_sha256"):
        issues.append(
            ValidationIssue(
                "GAP1_EV002_CONFIRM_HINT",
                f"{loc}.baseline_case_ref.baseline_file_sha256",
                "baseline_file_sha256 disagrees with actual untouched EV-002 file.",
            )
        )

    old_ss = ev002_data["synthetic_sources"]
    new_ss = case_data["synthetic_sources"]
    for key in ("workspaces", "sync_runs", "pull_requests", "commits", "issues_tickets", "initial_events", "post_sync_observations"):
        if old_ss.get(key) != new_ss.get(key):
            issues.append(
                ValidationIssue(
                    "GAP1_EV002_CONFIRM_HINT",
                    f"{loc}.synthetic_sources.{key}",
                    f"DELTA-EV-002 synthetic_sources.{key} diverged from baseline EV-002.",
                )
            )
    norm_new_repos = [{k: v for k, v in r.items() if k != "default_branch_reported_by_provider"} for r in new_ss.get("repositories", [])]
    if old_ss.get("repositories") != norm_new_repos:
        issues.append(
            ValidationIssue(
                "GAP1_EV002_CONFIRM_HINT",
                f"{loc}.synthetic_sources.repositories",
                "DELTA-EV-002 repository metadata diverged from baseline EV-002.",
            )
        )

    events = case_data.get("expected_proposal", {}).get("events", [])
    if len(events) != 1 or events[0].get("anchor_mode") != "DIRECT_COMMIT":
        issues.append(
            ValidationIssue("GAP1_EV002_CONFIRM_HINT", f"{loc}.expected_proposal.events", "DELTA-EV-002 must emit 1 DIRECT_COMMIT event.")
        )
        return issues

    hint = events[0].get("direct_commit_confirmation_expectation")
    if not isinstance(hint, dict):
        issues.append(
            ValidationIssue(
                "GAP1_EV002_CONFIRM_HINT",
                f"{loc}.expected_proposal.events[0]",
                "Missing direct_commit_confirmation_expectation.",
            )
        )
    else:
        if hint.get("requires_user_association_confirmation") is not True:
            issues.append(
                ValidationIssue(
                    "GAP1_EV002_CONFIRM_HINT",
                    f"{loc}.expected_proposal.events[0].direct_commit_confirmation_expectation",
                    "requires_user_association_confirmation must be true.",
                )
            )
        if hint.get("no_match_observed_means_definitely_no_pr") is not False:
            issues.append(
                ValidationIssue(
                    "GAP1_EV002_CONFIRM_HINT",
                    f"{loc}.expected_proposal.events[0].direct_commit_confirmation_expectation",
                    "no_match_observed_means_definitely_no_pr must be false.",
                )
            )
        if hint.get("spec_api_field_name") != "UNCONFIRMED" or hint.get("spec_badge_text") != "UNCONFIRMED":
            issues.append(
                ValidationIssue(
                    "GAP1_EV002_CONFIRM_HINT",
                    f"{loc}.expected_proposal.events[0].direct_commit_confirmation_expectation",
                    "Unapproved API field name and badge string must remain 'UNCONFIRMED'.",
                )
            )
    return issues


def check_gap2_default_branch_and_pr_dedup(case_data: dict[str, Any], loc: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cid = case_data.get("candidate_id")
    ss = case_data.get("synthetic_sources", {})
    repos = ss.get("repositories", [])
    prs = ss.get("pull_requests", [])
    oow_assocs = ss.get("out_of_window_pr_associations", [])
    commits = ss.get("commits", [])
    sync_runs = ss.get("sync_runs", [])
    ep = case_data.get("expected_proposal", {})
    events = ep.get("events", [])
    held_items = ep.get("held_items", [])
    dedup_checks = ep.get("out_of_window_pr_dedup_checks", [])

    if not repos:
        return issues
    default_branch = repos[0].get("default_branch")
    if default_branch == "main":
        issues.append(
            ValidationIssue(
                "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                f"{loc}.synthetic_sources.repositories[0].default_branch",
                "Gap 2 cases must exercise provider-reported non-'main' default branch ('trunk' or 'master').",
            )
        )
    if repos[0].get("default_branch_reported_by_provider") is not True:
        issues.append(
            ValidationIssue(
                "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                f"{loc}.synthetic_sources.repositories[0].default_branch_reported_by_provider",
                "Gap 2 repositories must declare default_branch_reported_by_provider=true.",
            )
        )

    commit_map = {c["commit_id"]: c for c in commits}

    non_default_main_commits = [c for c in commits if c.get("branch_name") == "main" and c.get("on_default_branch") is False]
    if not non_default_main_commits:
        issues.append(
            ValidationIssue(
                "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                f"{loc}.synthetic_sources.commits",
                "Gap 2 case must include a non-default commit on branch 'main' to verify provider default branch is not hardcoded to 'main'.",
            )
        )

    # No DIRECT_COMMIT event may reference an ASSOCIATED commit or a non-default branch commit
    for idx, ev in enumerate(events):
        if ev.get("anchor_mode") == "DIRECT_COMMIT":
            for cmt_id in ev.get("source_commit_ids", []):
                cmt = commit_map.get(cmt_id, {})
                if cmt.get("pr_association_status") == "ASSOCIATED":
                    issues.append(
                        ValidationIssue(
                            "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                            f"{loc}.expected_proposal.events[{idx}]",
                            f"ASSOCIATED commit '{cmt_id}' must NEVER be re-counted as a DIRECT_COMMIT candidate.",
                        )
                    )
                if cmt.get("on_default_branch") is not True or cmt.get("default_branch_verified_at_start_and_end") is not True:
                    issues.append(
                        ValidationIssue(
                            "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                            f"{loc}.expected_proposal.events[{idx}]",
                            f"Commit '{cmt_id}' is not start/end verified on default branch '{default_branch}'.",
                        )
                    )
            hint = ev.get("direct_commit_confirmation_expectation")
            if not isinstance(hint, dict) or hint.get("requires_user_association_confirmation") is not True:
                issues.append(
                    ValidationIssue(
                        "GAP1_EV002_CONFIRM_HINT",
                        f"{loc}.expected_proposal.events[{idx}]",
                        "Every emitted DIRECT_COMMIT candidate must declare direct_commit_confirmation_expectation.",
                    )
                )

    if cid == "CAND-EV-019":
        strategies = {p.get("merge_strategy") for p in prs}
        if not {"SQUASH", "REBASE"}.issubset(strategies):
            issues.append(
                ValidationIssue(
                    "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                    f"{loc}.synthetic_sources.pull_requests",
                    "CAND-EV-019 must cover both SQUASH and REBASE in-window merge strategies.",
                )
            )
        # In-window pull_requests must be strictly separated from out-of-window PR associations
        for p in prs:
            if p.get("within_import_window") is not True:
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.synthetic_sources.pull_requests[{p['pr_id']}]",
                        "Out-of-window PRs must be modeled in out_of_window_pr_associations, not in-window pull_requests.",
                    )
                )

        if not oow_assocs:
            issues.append(
                ValidationIssue(
                    "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                    f"{loc}.synthetic_sources.out_of_window_pr_associations",
                    "CAND-EV-019 must separate out-of-window provider PR association observation into out_of_window_pr_associations.",
                )
            )

        sr = sync_runs[0] if sync_runs else {}
        win_start = _parse_iso_utc(sr["import_window_start"]) if sr.get("import_window_start") else None
        win_end = _parse_iso_utc(sr["import_window_end"]) if sr.get("import_window_end") else None
        run_completed = _parse_iso_utc(sr["completed_at"]) if sr.get("completed_at") else None

        held_source_ids = {h.get("source_id") for h in held_items}
        dedup_check_by_obs = {d.get("association_observation_id"): d for d in dedup_checks}

        for obs in oow_assocs:
            obs_id = obs.get("association_observation_id", "")
            cmt_id = obs.get("observed_default_branch_commit_id", "")
            ext_pr_id = obs.get("external_pr_id", "")
            cmt = commit_map.get(cmt_id)
            if not cmt:
                issues.append(
                    ValidationIssue(
                        "ID_REFERENCE_INTEGRITY",
                        f"{loc}.synthetic_sources.out_of_window_pr_associations[{obs_id}]",
                        f"Unknown observed_default_branch_commit_id '{cmt_id}'.",
                    )
                )
                continue

            # 1. Fixture input premise & distinction between merge-time head SHA and observed default-branch commit
            if obs.get("provider_association_is_fixture_input_premise") is not True:
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.out_of_window_pr_associations[{obs_id}]",
                        "Must explicitly declare provider_association_is_fixture_input_premise=true.",
                    )
                )
            if obs.get("merge_time_head_commit_sha") == cmt.get("sha"):
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.out_of_window_pr_associations[{obs_id}]",
                        "merge_time_head_commit_sha must be distinguished from the observed default-branch commit SHA.",
                    )
                )

            # 2. Commit MUST NOT already be disqualified by period, branch, or start/end reachability
            if (
                cmt.get("on_default_branch") is not True
                or cmt.get("default_branch_verified_at_start_and_end") is not True
                or cmt.get("within_import_window") is not True
            ):
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.synthetic_sources.commits[{cmt_id}]",
                        "Out-of-window PR dedup test commit must NOT already be disqualified by period, branch, or start/end reachability.",
                    )
                )
            if cmt.get("pr_association_lookup_status") != "COMPLETED" or cmt.get("pr_association_status") != "ASSOCIATED" or cmt.get("associated_pr_id") != ext_pr_id:
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.synthetic_sources.commits[{cmt_id}]",
                        f"Commit '{cmt_id}' must declare COMPLETED + ASSOCIATED -> '{ext_pr_id}'.",
                    )
                )

            # 3. Chronological coherence: window_start <= committed_at <= window_end < pr_merged_at <= run_completed
            if win_start and win_end and run_completed and cmt.get("committed_at") and obs.get("pr_merged_at"):
                cmt_dt = _parse_iso_utc(cmt["committed_at"])
                pr_dt = _parse_iso_utc(obs["pr_merged_at"])
                if not (win_start <= cmt_dt <= win_end):
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.synthetic_sources.commits[{cmt_id}].committed_at",
                            f"Test commit timestamp {cmt['committed_at']} must fall strictly inside import window [{sr['import_window_start']}, {sr['import_window_end']}].",
                        )
                    )
                if win_start <= pr_dt <= win_end:
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.out_of_window_pr_associations[{obs_id}].pr_merged_at",
                            f"Out-of-window PR merged_at {obs['pr_merged_at']} must lie OUTSIDE import window.",
                        )
                    )
                if pr_dt < cmt_dt:
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.out_of_window_pr_associations[{obs_id}].pr_merged_at",
                            f"Chronological contradiction: pr_merged_at ({obs['pr_merged_at']}) precedes observed commit ({cmt['committed_at']}).",
                        )
                    )
                if pr_dt > run_completed:
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.out_of_window_pr_associations[{obs_id}].pr_merged_at",
                            f"Out-of-window PR merged_at ({obs['pr_merged_at']}) cannot occur after sync_run completed_at ({sr['completed_at']}).",
                        )
                    )

            # 4. Expected behavior: 0 new PR Events AND 0 DIRECT_COMMIT Events AND no prescribed persisted held_item
            for ev in events:
                if ev.get("primary_pr_id") == ext_pr_id:
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.expected_proposal.events[{ev['event_id']}]",
                            f"Out-of-window PR '{ext_pr_id}' must NOT create a new PR Event.",
                        )
                    )
                if ev.get("anchor_mode") == "DIRECT_COMMIT" and cmt_id in ev.get("source_commit_ids", []):
                    issues.append(
                        ValidationIssue(
                            "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                            f"{loc}.expected_proposal.events[{ev['event_id']}]",
                            f"Commit '{cmt_id}' linked to out-of-window PR '{ext_pr_id}' must NOT become a DIRECT_COMMIT candidate.",
                        )
                    )
            if cmt_id in held_source_ids:
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.expected_proposal.held_items",
                        f"Do not prescribe an internal persisted diagnostic record in held_items for out-of-window PR commit '{cmt_id}'; use out_of_window_pr_dedup_checks.",
                    )
                )
            dchk = dedup_check_by_obs.get(obs_id)
            if not dchk or dchk.get("commit_id") != cmt_id or dchk.get("external_pr_id") != ext_pr_id or dchk.get("creates_new_pr_event") is not False or dchk.get("creates_direct_commit_event") is not False or dchk.get("diagnostic_persistence_format") != "UNCONFIRMED":
                issues.append(
                    ValidationIssue(
                        "GAP2_OUT_OF_WINDOW_PR_DEDUP",
                        f"{loc}.expected_proposal.out_of_window_pr_dedup_checks",
                        f"Missing or invalid semantic dedup check for observation '{obs_id}'.",
                    )
                )

        # Heuristic similarity decoy commit (NO_MATCH_OBSERVED) must NOT be attached to any PR event
        decoys = [c for c in commits if c.get("heuristic_similarity_decoy_for_pr_id")]
        if not decoys:
            issues.append(
                ValidationIssue(
                    "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                    f"{loc}.synthetic_sources.commits",
                    "CAND-EV-019 must include a timestamp/message heuristic similarity decoy commit.",
                )
            )
        for dec in decoys:
            dec_id = dec["commit_id"]
            if dec.get("pr_association_status") != "NO_MATCH_OBSERVED" or dec.get("associated_pr_id") is not None:
                issues.append(
                    ValidationIssue(
                        "GAP2_DEFAULT_BRANCH_PR_DEDUP",
                        f"{loc}.synthetic_sources.commits[{dec_id}]",
                        "Heuristic decoy commit must have COMPLETED + NO_MATCH_OBSERVED and associated_pr_id=null.",
                    )
                )
            for ev in events:
                if ev.get("anchor_mode") == "PR" and dec_id in ev.get("source_commit_ids", []):
                    issues.append(
                        ValidationIssue(
                            "GAP2_NO_HEURISTIC_LINK",
                            f"{loc}.expected_proposal.events[{ev['event_id']}]",
                            f"Decoy NO_MATCH_OBSERVED commit '{dec_id}' must NEVER be heuristically attached to a PR Event.",
                        )
                    )
            direct_holders = [ev for ev in events if ev.get("anchor_mode") == "DIRECT_COMMIT" and dec_id in ev.get("source_commit_ids", [])]
            if len(direct_holders) != 1:
                issues.append(
                    ValidationIssue(
                        "GAP2_NO_HEURISTIC_LINK",
                        f"{loc}.expected_proposal.events",
                        f"Decoy NO_MATCH_OBSERVED commit '{dec_id}' must produce exactly 1 standalone DIRECT_COMMIT candidate.",
                    )
                )

    if cid == "CAND-EV-020":
        if default_branch != "master":
            issues.append(
                ValidationIssue("GAP2_DEFAULT_BRANCH_PR_DEDUP", f"{loc}.synthetic_sources.repositories[0]", "CAND-EV-020 must use default_branch='master'.")
            )
        if not sync_runs or sync_runs[0].get("status") != "PARTIAL" or sync_runs[0].get("promotes_events") is not False:
            issues.append(
                ValidationIssue("GAP2_UNKNOWN_HOLD", f"{loc}.synthetic_sources.sync_runs[0]", "CAND-EV-020 must have PARTIAL run with promotes_events=false.")
            )
        if events or ep.get("aggregation_check", {}).get("total_countable_events") != 0:
            issues.append(
                ValidationIssue("GAP2_UNKNOWN_HOLD", f"{loc}.expected_proposal.events", "PARTIAL/UNKNOWN run must promote 0 Events.")
            )
        if ep.get("aggregation_check", {}).get("promoted_events_from_partial_runs") != 0:
            issues.append(
                ValidationIssue("GAP2_UNKNOWN_HOLD", f"{loc}.expected_proposal.aggregation_check", "promoted_events_from_partial_runs must be 0.")
            )
        for sc in ep.get("search_checks", []):
            if sc.get("expected_result_count") != 0 or sc.get("expected_event_ids"):
                issues.append(
                    ValidationIssue("GAP2_UNKNOWN_HOLD", f"{loc}.expected_proposal.search_checks", "Default Event search on PARTIAL run must return 0.")
                )
        unknown_held = [h for h in held_items if h.get("logical_disposition") == "hold_classification_unknown_assoc"]
        if not unknown_held or any(h.get("interpreted_as_no_pr") is not False for h in unknown_held):
            issues.append(
                ValidationIssue("GAP2_UNKNOWN_HOLD", f"{loc}.expected_proposal.held_items", "UNKNOWN commit must be held with interpreted_as_no_pr=false.")
            )
    return issues


def check_gap3_active_cannot_link(case_data: dict[str, Any], loc: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ss = case_data.get("synthetic_sources", {})
    init_map = {e["event_id"]: e for e in ss.get("initial_events", [])}
    relations = ss.get("event_relations", [])
    commands = case_data.get("user_commands", [])
    ep = case_data.get("expected_proposal", {})
    cmd_results = ep.get("command_results", [])
    out_map = {e["event_id"]: e for e in ep.get("events", [])}

    if len(commands) != 1 or commands[0].get("command_action") != "manual_merge":
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.user_commands", "CAND-EV-021 must contain 1 manual_merge command."))
        return issues

    cmd = commands[0]
    targets = cmd.get("target_event_ids", [])
    if len(targets) != 2 or len(set(targets)) != 2:
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.user_commands[0].target_event_ids", "Must target 2 distinct active Events."))
        return issues

    t0, t1 = init_map.get(targets[0]), init_map.get(targets[1])
    if not t0 or not t1:
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.user_commands[0]", "Both merge targets must exist in initial_events."))
        return issues

    if t0["workspace_id"] != t1["workspace_id"] or cmd["workspace_id"] != t0["workspace_id"]:
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.user_commands[0]", "Targets and command must share the same workspace_id."))
    if t0["repo_id"] != t1["repo_id"]:
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.user_commands[0]", "Targets must share the same repo_id."))
    for tid, ev in ((targets[0], t0), (targets[1], t1)):
        if ev.get("is_active_top_level") is not True or ev.get("lifecycle_status") not in ACTIVE_LIFECYCLE_STATUSES:
            issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.synthetic_sources.initial_events[{tid}]", "Target Event must be active (CANDIDATE or CONFIRMED)."))
        if ev.get("anchor_mode") not in ELIGIBLE_MERGE_ANCHORS:
            issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.synthetic_sources.initial_events[{tid}]", "Target Event must have eligible non-composite anchor_mode."))
        if cmd.get("target_versions", {}).get(tid) != ev.get("version"):
            issues.append(
                ValidationIssue(
                    "GAP3_ACTIVE_CANNOT_LINK",
                    f"{loc}.user_commands[0].target_versions.{tid}",
                    f"Version mismatch ({cmd.get('target_versions', {}).get(tid)} vs {ev.get('version')}) would mask CANNOT_LINK_CONFLICT.",
                )
            )
        if cmd.get("expected_version") != ev.get("version"):
            issues.append(
                ValidationIssue(
                    "GAP3_ACTIVE_CANNOT_LINK",
                    f"{loc}.user_commands[0].expected_version",
                    f"Command expected_version ({cmd.get('expected_version')}) must match target version ({ev.get('version')}).",
                )
            )
    members_payload = cmd.get("payload", {}).get("members", [])
    member_ver_map = {m.get("event_id"): m.get("expected_version") for m in members_payload if isinstance(m, dict)}
    for tid in targets:
        if member_ver_map.get(tid) != init_map[tid].get("version"):
            issues.append(
                ValidationIssue(
                    "GAP3_ACTIVE_CANNOT_LINK",
                    f"{loc}.user_commands[0].payload.members",
                    f"payload.members expected_version for '{tid}' must match current Event version.",
                )
            )

    target_pair = {targets[0], targets[1]}
    matching_rel = [
        r for r in relations
        if r.get("relation_type") == "CANNOT_LINK"
        and {r.get("source_event_id"), r.get("target_event_id")} == target_pair
        and r.get("workspace_id") == cmd["workspace_id"]
    ]
    if not matching_rel:
        issues.append(
            ValidationIssue(
                "GAP3_ACTIVE_CANNOT_LINK",
                f"{loc}.synthetic_sources.event_relations",
                "Missing explicit CANNOT_LINK relation between the two active merge targets.",
            )
        )

    if len(cmd_results) != 1:
        issues.append(ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.expected_proposal.command_results", "Must have 1 command_result."))
        return issues
    res = cmd_results[0]
    if res.get("outcome") != "REJECTED" or res.get("spec_error_code") != "CANNOT_LINK_CONFLICT":
        issues.append(
            ValidationIssue(
                "GAP3_ACTIVE_CANNOT_LINK",
                f"{loc}.expected_proposal.command_results[0]",
                "Outcome must be REJECTED with spec_error_code='CANNOT_LINK_CONFLICT'.",
            )
        )
    if res.get("state_mutated") is not False or res.get("resulting_event_ids") != []:
        issues.append(
            ValidationIssue(
                "GAP3_ACTIVE_CANNOT_LINK",
                f"{loc}.expected_proposal.command_results[0]",
                "Rejected CANNOT_LINK merge must cause 0 state mutations (state_mutated=false, resulting_event_ids=[]).",
            )
        )
    if "http_status" not in res.get("unconfirmed_fields", []):
        issues.append(
            ValidationIssue(
                "GAP3_ACTIVE_CANNOT_LINK",
                f"{loc}.expected_proposal.command_results[0].unconfirmed_fields",
                "http_status must remain unconfirmed (excluded from truth freeze).",
            )
        )

    if set(out_map.keys()) != set(init_map.keys()):
        issues.append(
            ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.expected_proposal.events", "No new Events or composites may be created on CANNOT_LINK rejection.")
        )
    for tid, init_ev in init_map.items():
        out_ev = out_map.get(tid)
        if not out_ev:
            continue
        for field in ("version", "lifecycle_status", "anchor_mode", "is_active_top_level", "membership_role", "composite_membership_status", "member_event_ids"):
            if out_ev.get(field) != init_ev.get(field):
                issues.append(
                    ValidationIssue(
                        "GAP3_ACTIVE_CANNOT_LINK",
                        f"{loc}.expected_proposal.events[{tid}].{field}",
                        f"Event field '{field}' mutated on CANNOT_LINK rejection ({init_ev.get(field)!r} -> {out_ev.get(field)!r}).",
                    )
                )
        if out_ev.get("dependent_artifacts_unchanged") != init_ev.get("dependent_artifacts"):
            issues.append(
                ValidationIssue(
                    "GAP3_ACTIVE_CANNOT_LINK",
                    f"{loc}.expected_proposal.events[{tid}].dependent_artifacts_unchanged",
                    "Dependent Fact/Claim/Export artifacts must remain unchanged on CANNOT_LINK rejection.",
                )
            )
    if ep.get("downstream_side_effects") != []:
        issues.append(
            ValidationIssue("GAP3_ACTIVE_CANNOT_LINK", f"{loc}.expected_proposal.downstream_side_effects", "downstream_side_effects must be empty on CANNOT_LINK rejection.")
        )
    return issues


def check_gap4_keep_edit_tracking(case_data: dict[str, Any], loc: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ss = case_data.get("synthetic_sources", {})
    init_map = {e["event_id"]: e for e in ss.get("initial_events", [])}
    commands = case_data.get("user_commands", [])
    ep = case_data.get("expected_proposal", {})
    out_map = {e["event_id"]: e for e in ep.get("events", [])}
    cmd_results = ep.get("command_results", [])
    rev_actions = ep.get("review_actions", [])

    if len(commands) != 1 or len(cmd_results) != 1:
        issues.append(ValidationIssue("GAP4_KEEP_EDIT_TRACKING", loc, "Gap 4 case must have 1 user_command and 1 command_result."))
        return issues

    cmd = commands[0]
    action = cmd.get("command_action")
    res = cmd_results[0]
    if action not in {"confirm", "edit"}:
        issues.append(ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.user_commands[0].command_action", f"Expected 'confirm' or 'edit', got {action!r}."))
        return issues
    if res.get("outcome") != "ACCEPTED" or res.get("state_mutated") is not True:
        issues.append(ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.command_results[0]", "Command must be ACCEPTED with state_mutated=true."))

    targets = cmd.get("target_event_ids", [])
    if len(targets) != 1 or targets[0] not in init_map or targets[0] not in out_map:
        issues.append(ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.user_commands[0].target_event_ids", "Must target exactly 1 existing Event."))
        return issues

    tid = targets[0]
    init_ev = init_map[tid]
    out_ev = out_map[tid]

    if init_ev.get("lifecycle_status") not in OFFICIAL_LIFECYCLE_STATUSES or out_ev.get("lifecycle_status") not in OFFICIAL_LIFECYCLE_STATUSES:
        issues.append(
            ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.events[{tid}].lifecycle_status", "Unapproved lifecycle status detected.")
        )

    expected_new_ver = init_ev["version"] + 1
    if out_ev.get("version") != expected_new_ver or out_ev.get("previous_version") != init_ev["version"] or out_ev.get("prior_version_preserved") is not True:
        issues.append(
            ValidationIssue(
                "GAP4_KEEP_EDIT_TRACKING",
                f"{loc}.expected_proposal.events[{tid}]",
                f"Expected new version {expected_new_ver} with previous_version={init_ev['version']} and prior_version_preserved=true.",
            )
        )
    if out_ev.get("anchor_mode") != init_ev.get("anchor_mode") or out_ev.get("merge_origin") != "NONE":
        issues.append(
            ValidationIssue(
                "GAP4_KEEP_EDIT_TRACKING",
                f"{loc}.expected_proposal.events[{tid}]",
                "confirm/edit must NOT change anchor_mode or turn Event into a USER_COMPOSITE merge.",
            )
        )
    if out_ev.get("primary_pr_id") != init_ev.get("primary_pr_id") or out_ev.get("source_commit_ids") != init_ev.get("source_commit_ids"):
        issues.append(
            ValidationIssue(
                "GAP4_KEEP_EDIT_TRACKING",
                f"{loc}.expected_proposal.events[{tid}]",
                "Source provenance (primary_pr_id / source_commit_ids) must be preserved.",
            )
        )
    if out_ev.get("membership_role") != init_ev.get("membership_role") or out_ev.get("member_event_ids") != init_ev.get("member_event_ids"):
        issues.append(
            ValidationIssue(
                "GAP4_KEEP_EDIT_TRACKING",
                f"{loc}.expected_proposal.events[{tid}]",
                "Membership must remain unchanged on confirm/edit.",
            )
        )

    matching_ra = [r for r in rev_actions if r.get("command_id") == cmd["command_id"] and r.get("event_id") == tid]
    if len(matching_ra) != 1:
        issues.append(
            ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.review_actions", f"Expected exactly 1 ReviewAction for command '{cmd['command_id']}'.")
        )
        return issues
    ra = matching_ra[0]
    expected_domain_ra = "CONFIRM" if action == "confirm" else "EDIT"
    if (
        ra.get("logical_action") != action
        or ra.get("domain_review_action") != expected_domain_ra
        or ra.get("external_response_action_token") != "UNCONFIRMED"
        or ra.get("from_version") != init_ev["version"]
        or ra.get("to_version") != expected_new_ver
        or ra.get("creates_new_event_version") is not True
        or ra.get("is_event_merge") is not False
    ):
        issues.append(
            ValidationIssue(
                "GAP4_KEEP_EDIT_TRACKING",
                f"{loc}.expected_proposal.review_actions[0]",
                f"ReviewAction must record domain_review_action='{expected_domain_ra}', external_response_action_token='UNCONFIRMED', exact version transition, creates_new_event_version=true, and is_event_merge=false.",
            )
        )

    if action == "confirm":
        if cmd.get("spec_command_name") != "POST /work-events/{id}/actions/confirm":
            issues.append(
                ValidationIssue(
                    "GAP4_KEEP_EDIT_TRACKING",
                    f"{loc}.user_commands[0].spec_command_name",
                    "Confirm candidate must cite MVP API §5 route 'POST /work-events/{id}/actions/confirm'.",
                )
            )
        if init_ev.get("lifecycle_status") != "CANDIDATE" or out_ev.get("lifecycle_status") != "CONFIRMED":
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.events[{tid}]", "confirm must transition CANDIDATE -> CONFIRMED.")
            )
    elif action == "edit":
        if cmd.get("spec_command_name") != "PATCH /work-events/{id}":
            issues.append(
                ValidationIssue(
                    "GAP4_KEEP_EDIT_TRACKING",
                    f"{loc}.user_commands[0].spec_command_name",
                    "Edit candidate must cite MVP API §5 route 'PATCH /work-events/{id}'.",
                )
            )
        # Event Guide §6: active CANDIDATE edit emits a new CANDIDATE version; do NOT assert CONFIRMED edit retention here
        if init_ev.get("lifecycle_status") != "CANDIDATE" or out_ev.get("lifecycle_status") != "CANDIDATE":
            issues.append(
                ValidationIssue(
                    "GAP4_KEEP_EDIT_TRACKING",
                    f"{loc}.expected_proposal.events[{tid}].lifecycle_status",
                    f"CAND-EV-023 must edit an active CANDIDATE into a new CANDIDATE version (got {init_ev.get('lifecycle_status')!r} -> {out_ev.get('lifecycle_status')!r}); do not assert CONFIRMED edit retention or unapproved statuses.",
                )
            )
        payload = cmd.get("payload", {})
        if not payload.get("title") or not payload.get("summary"):
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.user_commands[0].payload", "edit command must update both title and summary.")
            )
        if out_ev.get("title") != payload.get("title") or out_ev.get("summary") != payload.get("summary"):
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.events[{tid}]", "Edited Event title/summary must match command payload.")
            )
        if out_ev.get("title") == init_ev.get("title") or out_ev.get("summary") == init_ev.get("summary"):
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.events[{tid}]", "Edit must meaningfully change title and summary from initial values.")
            )
        if not {"title", "summary"}.issubset(set(ra.get("changed_fields", []))):
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.review_actions[0].changed_fields", "Edit ReviewAction must list 'title' and 'summary'.")
            )
        siblings = [eid for eid in init_map if eid != tid]
        if not siblings:
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.synthetic_sources.initial_events", "Edit case should include an untouched sibling Event to verify edit != merge.")
            )
        for sid in siblings:
            s_in, s_out = init_map[sid], out_map.get(sid)
            if not s_out:
                issues.append(ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.events", f"Sibling Event '{sid}' missing after edit."))
                continue
            for f in ("version", "lifecycle_status", "anchor_mode", "is_active_top_level", "membership_role", "title", "summary"):
                if s_out.get(f) != s_in.get(f):
                    issues.append(
                        ValidationIssue(
                            "GAP4_KEEP_EDIT_TRACKING",
                            f"{loc}.expected_proposal.events[{sid}].{f}",
                            f"Edit must NOT mutate or supersede sibling Event '{sid}'.",
                        )
                    )
        if ep.get("aggregation_check", {}).get("total_user_composite_events") != 0:
            issues.append(
                ValidationIssue("GAP4_KEEP_EDIT_TRACKING", f"{loc}.expected_proposal.aggregation_check", "Edit must not create USER_COMPOSITE events.")
            )
    return issues


def validate_candidate_data(
    case_data: dict[str, Any],
    schema: dict[str, Any],
    dataset_dir: Path,
    loc: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(_validate_against_schema(case_data, schema, loc))

    for spath, sval in _walk_strings(case_data, loc):
        for pat in FORBIDDEN_OD007_PATTERNS:
            if pat.search(sval):
                issues.append(
                    ValidationIssue("OD007_NEUTRALITY", spath, f"Forbidden OD-007 physical table/SQL pattern {pat.pattern!r} matched.")
                )

    for idx, cmd in enumerate(case_data.get("user_commands", [])):
        if (
            cmd.get("command_action") in INVENTED_UI_API_TOKENS
            or cmd.get("spec_command_name") in INVENTED_UI_API_TOKENS
            or INVENTED_KEEP_ROUTE_PATTERN.search(str(cmd.get("spec_command_name", "")))
        ):
            issues.append(
                ValidationIssue("NO_INVENTED_UI_API_NAME", f"{loc}.user_commands[{idx}]", "UI label 'Keep' must NEVER be invented as a separate API action or route.")
            )
    for idx, ra in enumerate(case_data.get("expected_proposal", {}).get("review_actions", [])):
        if (
            ra.get("logical_action") in INVENTED_UI_API_TOKENS
            or ra.get("domain_review_action") in INVENTED_UI_API_TOKENS
            or ra.get("external_response_action_token") in INVENTED_UI_API_TOKENS
        ):
            issues.append(
                ValidationIssue("NO_INVENTED_UI_API_NAME", f"{loc}.expected_proposal.review_actions[{idx}]", "UI label 'Keep' must NEVER be invented as a ReviewAction token.")
            )

    ss = case_data.get("synthetic_sources", {})
    ws_ids = {w["workspace_id"] for w in ss.get("workspaces", [])}
    repo_map = {r["repo_id"]: r for r in ss.get("repositories", [])}
    sync_map = {s["sync_run_id"]: s for s in ss.get("sync_runs", [])}
    pr_map = {p["pr_id"]: p for p in ss.get("pull_requests", [])}
    oow_pr_ids = {o["external_pr_id"] for o in ss.get("out_of_window_pr_associations", [])}
    all_known_pr_ids = set(pr_map.keys()) | oow_pr_ids
    cmt_map = {c["commit_id"]: c for c in ss.get("commits", [])}
    init_ev_map = {e["event_id"]: e for e in ss.get("initial_events", [])}

    for r in repo_map.values():
        if r.get("workspace_id") not in ws_ids:
            issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"Repo '{r['repo_id']}' references unknown workspace '{r.get('workspace_id')}'."))
    for sr in sync_map.values():
        if sr.get("repo_id") not in repo_map or sr.get("workspace_id") not in ws_ids:
            issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"Sync run '{sr['sync_run_id']}' references unknown repo/workspace."))
        if sr.get("status") == "PARTIAL" and sr.get("promotes_events") is not False:
            issues.append(ValidationIssue("PARTIAL_RUN_NO_PROMOTION", loc, f"PARTIAL sync run '{sr['sync_run_id']}' must have promotes_events=false."))
    for pr in pr_map.values():
        if pr.get("repo_id") not in repo_map:
            issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"PR '{pr['pr_id']}' references unknown repo."))
        for cid in pr.get("commit_ids", []):
            if cid not in cmt_map:
                issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"PR '{pr['pr_id']}' references unknown commit '{cid}'."))
    for c in cmt_map.values():
        repo = repo_map.get(c.get("repo_id"))
        if not repo:
            issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"Commit '{c['commit_id']}' references unknown repo."))
            continue
        expected_on_default = c.get("branch_name") == repo.get("default_branch")
        if c.get("on_default_branch") != expected_on_default:
            issues.append(
                ValidationIssue(
                    "BRANCH_ASSOCIATION_RULES",
                    f"{loc}.synthetic_sources.commits[{c['commit_id']}]",
                    f"on_default_branch={c.get('on_default_branch')} disagrees with branch_name={c.get('branch_name')!r} vs provider default_branch={repo.get('default_branch')!r}.",
                )
            )
        if c.get("default_branch_verified_at_start_and_end") is True and c.get("on_default_branch") is not True:
            issues.append(
                ValidationIssue(
                    "BRANCH_ASSOCIATION_RULES",
                    f"{loc}.synthetic_sources.commits[{c['commit_id']}]",
                    "default_branch_verified_at_start_and_end cannot be true when on_default_branch is false.",
                )
            )
        assoc = c.get("pr_association_status")
        lookup = c.get("pr_association_lookup_status")
        assoc_pr = c.get("associated_pr_id")
        if assoc == "ASSOCIATED":
            if lookup != "COMPLETED" or assoc_pr not in all_known_pr_ids:
                issues.append(ValidationIssue("BRANCH_ASSOCIATION_RULES", f"{loc}.commits[{c['commit_id']}]", "ASSOCIATED requires COMPLETED lookup and valid associated_pr_id."))
        elif assoc == "NO_MATCH_OBSERVED":
            if lookup != "COMPLETED" or assoc_pr is not None:
                issues.append(ValidationIssue("BRANCH_ASSOCIATION_RULES", f"{loc}.commits[{c['commit_id']}]", "NO_MATCH_OBSERVED requires COMPLETED lookup and associated_pr_id=null."))
        elif assoc == "UNKNOWN":
            if lookup != "INCOMPLETE_OR_FAILED" or assoc_pr is not None:
                issues.append(ValidationIssue("BRANCH_ASSOCIATION_RULES", f"{loc}.commits[{c['commit_id']}]", "UNKNOWN requires INCOMPLETE_OR_FAILED lookup and associated_pr_id=null."))

    for rel in ss.get("event_relations", []):
        if rel.get("source_event_id") not in init_ev_map or rel.get("target_event_id") not in init_ev_map:
            issues.append(ValidationIssue("ID_REFERENCE_INTEGRITY", loc, f"Relation '{rel.get('relation_id')}' references unknown initial_event."))

    ep = case_data.get("expected_proposal", {})
    events = ep.get("events", [])
    agg = ep.get("aggregation_check", {})
    countable = [e for e in events if e.get("counted_in_metrics") is True]
    composites = [e for e in countable if e.get("anchor_mode") == "USER_COMPOSITE"]
    directs = [e for e in countable if e.get("anchor_mode") == "DIRECT_COMMIT"]
    rejected = [e for e in events if e.get("lifecycle_status") == "REJECTED"]
    if agg.get("total_countable_events") != len(countable):
        issues.append(ValidationIssue("AGGREGATION_CONSISTENCY", loc, f"total_countable_events={agg.get('total_countable_events')} != {len(countable)}."))
    if agg.get("total_user_composite_events") != len(composites):
        issues.append(ValidationIssue("AGGREGATION_CONSISTENCY", loc, f"total_user_composite_events={agg.get('total_user_composite_events')} != {len(composites)}."))
    if agg.get("total_held_items") != len(ep.get("held_items", [])):
        issues.append(ValidationIssue("AGGREGATION_CONSISTENCY", loc, "total_held_items disagrees with len(held_items)."))
    if agg.get("total_rejected_events") != len(rejected):
        issues.append(ValidationIssue("AGGREGATION_CONSISTENCY", loc, "total_rejected_events disagrees with rejected events count."))
    if "total_direct_commit_events" in agg and agg["total_direct_commit_events"] != len(directs):
        issues.append(ValidationIssue("AGGREGATION_CONSISTENCY", loc, "total_direct_commit_events disagrees with countable DIRECT_COMMIT count."))

    gap_id = case_data.get("spec_delta_trace", {}).get("gap_id")
    if gap_id == "GAP-1-EV002-CONFIRM-HINT":
        issues.extend(check_gap1_ev002_confirmation_hint(case_data, dataset_dir, loc))
    elif gap_id == "GAP-2-DEFAULT-BRANCH-PR-DEDUP":
        issues.extend(check_gap2_default_branch_and_pr_dedup(case_data, loc))
    elif gap_id == "GAP-3-ACTIVE-CANNOT-LINK":
        issues.extend(check_gap3_active_cannot_link(case_data, loc))
    elif gap_id == "GAP-4-KEEP-EDIT-TRACKING":
        issues.extend(check_gap4_keep_edit_tracking(case_data, loc))

    return issues


def validate_delta_pack(delta_dir: Path, dataset_dir: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(check_baseline_preservation(dataset_dir))

    manifest, m_issues = _load_json(delta_dir / "delta-manifest.json")
    schema, s_issues = _load_json(delta_dir / "schemas" / "delta-candidate.schema.json")
    cov_diff, c_issues = _load_json(delta_dir / "coverage-diff.json")
    issues.extend(m_issues + s_issues + c_issues)
    if not isinstance(manifest, dict) or not isinstance(schema, dict):
        return issues

    if manifest.get("approval_status") != "DRAFT_REVIEW_CANDIDATE" or manifest.get("approved_by") is not None:
        issues.append(
            ValidationIssue("DRAFT_GOVERNANCE", "delta-manifest.json", "Delta candidate pack must remain DRAFT_REVIEW_CANDIDATE with approved_by=null.")
        )
    if manifest.get("od_007_physical_table_assumption") is not False or manifest.get("synthetic_only") is not True:
        issues.append(
            ValidationIssue("DRAFT_GOVERNANCE", "delta-manifest.json", "Must declare od_007_physical_table_assumption=false and synthetic_only=true.")
        )

    schema_sha_recorded = manifest.get("integrity_hashes", {}).get("schema_file_sha256")
    actual_schema_sha = _sha256_file(delta_dir / "schemas" / "delta-candidate.schema.json")
    if schema_sha_recorded != actual_schema_sha:
        issues.append(
            ValidationIssue("INTEGRITY_HASH_MISMATCH", "delta-manifest.json.integrity_hashes.schema_file_sha256", f"{schema_sha_recorded} != {actual_schema_sha}.")
        )

    loaded_candidates: dict[str, dict[str, Any]] = {}
    actual_shas: dict[str, str] = {}
    manifest_paths: set[Path] = set()
    for entry in manifest.get("candidates", []):
        cid = entry.get("candidate_id", "")
        rel_path = entry.get("path", "")
        if cid in loaded_candidates:
            issues.append(ValidationIssue("CANDIDATE_UNIQUENESS", "delta-manifest.json", f"Duplicate candidate_id '{cid}'."))
        full_path = (delta_dir / rel_path).resolve()
        manifest_paths.add(full_path)
        cdata, l_issues = _load_json(full_path)
        if l_issues or not isinstance(cdata, dict):
            issues.extend(l_issues)
            continue
        actual_sha = _sha256_file(full_path)
        actual_shas[cid] = actual_sha
        if entry.get("file_sha256") != actual_sha:
            issues.append(
                ValidationIssue("INTEGRITY_HASH_MISMATCH", f"delta-manifest.json.candidates[{cid}].file_sha256", f"{entry.get('file_sha256')} != {actual_sha}.")
            )
        if cdata.get("candidate_id") != cid:
            issues.append(ValidationIssue("CANDIDATE_UNIQUENESS", rel_path, f"Manifest candidate_id '{cid}' != file '{cdata.get('candidate_id')}'."))
        loaded_candidates[cid] = cdata
        issues.extend(validate_candidate_data(cdata, schema, dataset_dir, rel_path))

    for p in sorted((delta_dir / "candidates").glob("*.json")):
        if p.resolve() not in manifest_paths:
            issues.append(ValidationIssue("CANDIDATE_UNIQUENESS", str(p.relative_to(delta_dir)), "Unlisted candidate file in candidates/."))

    payload = "".join(f"{cid}:{actual_shas[cid]}\n" for cid in sorted(actual_shas))
    expected_bundle_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    recorded_bundle_sha = manifest.get("integrity_hashes", {}).get("candidate_bundle_sha256")
    if recorded_bundle_sha != expected_bundle_sha:
        issues.append(
            ValidationIssue("INTEGRITY_HASH_MISMATCH", "delta-manifest.json.integrity_hashes.candidate_bundle_sha256", f"{recorded_bundle_sha} != {expected_bundle_sha}.")
        )

    repo_to_bundle: dict[str, tuple[str, str, str]] = {}
    for cid, cdata in sorted(loaded_candidates.items()):
        eg = cdata.get("eval_grouping", {})
        bid = str(eg.get("eval_bundle_id", ""))
        lgrp = str(eg.get("source_lineage_group", ""))
        for r in cdata.get("synthetic_sources", {}).get("repositories", []):
            rname = str(r.get("full_name", ""))
            if not rname:
                continue
            if rname in repo_to_bundle:
                prev_cid, prev_bid, prev_lgrp = repo_to_bundle[rname]
                if prev_bid != bid or prev_lgrp != lgrp:
                    issues.append(
                        ValidationIssue(
                            "EVAL_BUNDLE_LINEAGE_ISOLATION",
                            f"candidates[{cid}]",
                            f"Candidates '{prev_cid}' and '{cid}' share synthetic repo '{rname}' but have different eval_bundle_id/source_lineage_group ({prev_bid}/{prev_lgrp} vs {bid}/{lgrp}).",
                        )
                    )
            else:
                repo_to_bundle[rname] = (cid, bid, lgrp)

    bundle_seen: set[str] = set()
    for b in manifest.get("evaluation_bundles", []):
        bid = b.get("eval_bundle_id", "")
        for cid in b.get("candidate_ids", []):
            if str(cid).startswith("NEG-"):
                issues.append(ValidationIssue("NEGATIVE_VARIANT_ISOLATION", f"delta-manifest.json.evaluation_bundles[{bid}]", f"Negative variant '{cid}' forbidden in evaluation_bundles."))
            if cid in bundle_seen:
                issues.append(ValidationIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", f"delta-manifest.json.evaluation_bundles[{bid}]", f"Duplicate candidate '{cid}' across bundles."))
            bundle_seen.add(cid)
            if cid in loaded_candidates:
                eg = loaded_candidates[cid].get("eval_grouping", {})
                if eg.get("eval_bundle_id") != bid:
                    issues.append(
                        ValidationIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", f"delta-manifest.json.evaluation_bundles[{bid}]", f"Candidate '{cid}' eval_bundle_id '{eg.get('eval_bundle_id')}' != '{bid}'.")
                    )
    if bundle_seen != set(loaded_candidates.keys()):
        issues.append(
            ValidationIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", "delta-manifest.json.evaluation_bundles", f"Bundle candidates {sorted(bundle_seen)} != manifest candidates {sorted(loaded_candidates.keys())}.")
        )

    if isinstance(cov_diff, dict):
        diff_ids = {d.get("candidate_id") for d in cov_diff.get("deltas", []) if isinstance(d, dict)}
        if diff_ids != set(loaded_candidates.keys()):
            issues.append(
                ValidationIssue("COVERAGE_DIFF_SYNC", "coverage-diff.json", f"coverage-diff candidate IDs {sorted(diff_ids)} != manifest {sorted(loaded_candidates.keys())}.")
            )
        for row in cov_diff.get("deltas", []):
            for req_key in ("notion_clauses", "existing_gap", "synthetic_input", "expected_behavior", "verification_tests"):
                if not row.get(req_key):
                    issues.append(ValidationIssue("COVERAGE_DIFF_SYNC", f"coverage-diff.json[{row.get('candidate_id')}]", f"Missing traceability field '{req_key}'."))

    return issues


def main(argv: list[str] | None = None) -> int:
    default_delta_dir = Path(__file__).resolve().parent
    default_dataset_dir = default_delta_dir.parent.parent
    parser = argparse.ArgumentParser(description="Deterministic validator for isolated M0-GATE spec-delta review candidates.")
    parser.add_argument("--delta-dir", type=Path, default=default_delta_dir)
    parser.add_argument("--dataset-dir", type=Path, default=default_dataset_dir)
    args = parser.parse_args(argv)

    issues = validate_delta_pack(args.delta_dir, args.dataset_dir)
    if issues:
        print(f"DELTA CANDIDATE VALIDATION FAILED ({len(issues)} issue(s)):")
        for issue in issues:
            print("  " + issue.format())
        return 1

    manifest = json.loads((args.delta_dir / "delta-manifest.json").read_text(encoding="utf-8"))
    print(
        f"DELTA CANDIDATE VALIDATION PASSED: {len(manifest.get('candidates', []))} isolated DRAFT candidates verified "
        f"(baseline=1.0.0-mvp-approved untouched, all 4 M0-GATE gaps, same-source bundles & SHA-256 integrity verified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
