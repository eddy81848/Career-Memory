#!/usr/bin/env python3
"""Deterministic validator & loader for the approved 21-case MVP regression release (1.1.0-mvp-approved).

Checks:
1. Preserved 1.0.0-mvp-approved baseline files remain byte-for-byte untouched.
2. Preserved 13-file M0-GATE DRAFT candidate pack matches exact reviewed bundle SHA-256.
3. No invented approver name/account or approval timestamp.
4. Exact 21-case composition (15 baseline + 1 DELTA-EV-002 replacing EV-002 + 5 new EV-019..023).
5. Zero EV-002 duplication, zero missing new candidates, zero EV-017/018 or NEG-* leakage.
6. Same-source evaluation bundle isolation across all 21 cases.
7. CAND-EV-021 handoff route (POST /work-events/actions/merge) recorded without mutating CAND-EV-021.
8. SHA-256 integrity of all 21 resolved files and composite approved_21_bundle_sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_BASE_COMMIT_SHA = "140543b983bbdf41584bc5b3a65d1ec67022f951"
EXPECTED_BASELINE_MANIFEST_SHA256 = "6aa16ce3bb0eb0a3c79d7bd8efd058e446fff7f9fc05d88cd06764de9a7bd806"
EXPECTED_SUPERSEDED_EV002_SHA256 = "354be261b848463ac0fa4660203817af7890f06c37be5d0f74669e40cc194a6f"
EXPECTED_CANDIDATE_PACK_FILE_COUNT = 13
EXPECTED_CANDIDATE_BUNDLE_SHA256 = "fa8b380e09ad8891ee45d3d5d0d698323f8418a035a284f6efc34fcbeecf1730"
EXPECTED_CAND_EV021_SHA256 = "6cfdd2360a7ec7be9f79be71b399b3f312edf979115db9fd594eb583952ef68d"

EXPECTED_BASELINE_15_IDS = {f"EV-{i:03d}" for i in range(1, 17) if i != 2}
EXPECTED_NEW_5_IDS = {f"EV-{i:03d}" for i in range(19, 24)}
EXPECTED_ALL_21_IDS = EXPECTED_BASELINE_15_IDS | {"EV-002"} | EXPECTED_NEW_5_IDS
FORBIDDEN_EXCLUDED_IDS = {"EV-017", "EV-018", "NEG-001", "NEG-002", "NEG-003", "NEG-004", "NEG-005", "NEG-006"}

DELTA_EV002_REL_PATH = "review-candidates/m0-gate-spec-delta/candidates/DELTA-EV-002-direct-commit-confirmation-needed.json"
BASELINE_EV002_REL_PATH = "cases/EV-002-default-branch-direct-commit.json"


@dataclass(frozen=True)
class ReleaseIssue:
    rule_code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.rule_code}] {self.location}: {self.message}"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> tuple[Any, list[ReleaseIssue]]:
    if not path.is_file():
        return None, [ReleaseIssue("FILE_NOT_FOUND", str(path), "Required file does not exist.")]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as exc:
        return None, [ReleaseIssue("JSON_PARSE_ERROR", str(path), f"Invalid JSON: {exc}")]


def check_preserved_inputs(dataset_dir: Path) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []

    # 1. Preserved 1.0.0-mvp-approved manifest & EV-002
    base_manifest_path = dataset_dir / "manifest.json"
    if not base_manifest_path.is_file() or _sha256_file(base_manifest_path) != EXPECTED_BASELINE_MANIFEST_SHA256:
        issues.append(
            ReleaseIssue(
                "BASELINE_1_0_0_PRESERVATION",
                "manifest.json",
                "Preserved 1.0.0-mvp-approved manifest.json SHA-256 changed.",
            )
        )
    base_ev002_path = dataset_dir / BASELINE_EV002_REL_PATH
    if not base_ev002_path.is_file() or _sha256_file(base_ev002_path) != EXPECTED_SUPERSEDED_EV002_SHA256:
        issues.append(
            ReleaseIssue(
                "BASELINE_1_0_0_PRESERVATION",
                BASELINE_EV002_REL_PATH,
                "Preserved baseline EV-002 file must remain byte-for-byte untouched.",
            )
        )

    # 2. Preserved 13-file reviewed M0-GATE DRAFT candidate pack
    delta_dir = dataset_dir / "review-candidates" / "m0-gate-spec-delta"
    if not delta_dir.is_dir():
        issues.append(ReleaseIssue("CANDIDATE_PACK_PRESERVATION", str(delta_dir), "Reviewed candidate pack directory missing."))
        return issues

    delta_files = sorted(p for p in delta_dir.rglob("*") if p.is_file())
    if len(delta_files) != EXPECTED_CANDIDATE_PACK_FILE_COUNT:
        issues.append(
            ReleaseIssue(
                "CANDIDATE_PACK_PRESERVATION",
                str(delta_dir),
                f"Expected exactly {EXPECTED_CANDIDATE_PACK_FILE_COUNT} reviewed files, found {len(delta_files)}.",
            )
        )

    cand_shas: dict[str, str] = {}
    for p in sorted((delta_dir / "candidates").glob("*.json")):
        cdata, l_issues = _load_json(p)
        if l_issues or not isinstance(cdata, dict):
            issues.extend(l_issues)
            continue
        if cdata.get("expected_proposal", {}).get("approval_status") != "DRAFT" or cdata.get("expected_proposal", {}).get("approved_by") is not None:
            issues.append(
                ReleaseIssue(
                    "CANDIDATE_PACK_PRESERVATION",
                    str(p.relative_to(dataset_dir)),
                    "Reviewed candidate file must remain preserved as DRAFT with approved_by=null.",
                )
            )
        cand_shas[cdata["candidate_id"]] = _sha256_file(p)
    recomputed_bundle = hashlib.sha256("".join(f"{cid}:{cand_shas[cid]}\n" for cid in sorted(cand_shas)).encode("utf-8")).hexdigest()
    if recomputed_bundle != EXPECTED_CANDIDATE_BUNDLE_SHA256:
        issues.append(
            ReleaseIssue(
                "CANDIDATE_PACK_PRESERVATION",
                "review-candidates/m0-gate-spec-delta",
                f"Reviewed candidate bundle SHA-256 mismatch: {recomputed_bundle} != {EXPECTED_CANDIDATE_BUNDLE_SHA256}.",
            )
        )

    cand_ev021_path = delta_dir / "candidates" / "CAND-EV-021-active-events-cannot-link-conflict.json"
    if not cand_ev021_path.is_file() or _sha256_file(cand_ev021_path) != EXPECTED_CAND_EV021_SHA256:
        issues.append(
            ReleaseIssue(
                "CANDIDATE_PACK_PRESERVATION",
                "review-candidates/m0-gate-spec-delta/candidates/CAND-EV-021-active-events-cannot-link-conflict.json",
                "Preserved CAND-EV-021 draft file must remain untouched (route handoff belongs in release manifest/docs).",
            )
        )
    return issues


def validate_release_manifest(manifest_path: Path, dataset_dir: Path) -> list[ReleaseIssue]:
    issues: list[ReleaseIssue] = []
    issues.extend(check_preserved_inputs(dataset_dir))

    manifest, load_issues = _load_json(manifest_path)
    if load_issues or not isinstance(manifest, dict):
        return issues + load_issues

    # Guard against accidental loading of baseline 1.0.0-mvp-approved manifest as final 21-case release
    if manifest.get("schema_version") != "event-approved-release-manifest.v1" or "approved_cases" not in manifest:
        issues.append(
            ReleaseIssue(
                "BASELINE_ONLY_MISLOAD",
                str(manifest_path),
                f"Expected release manifest schema 'event-approved-release-manifest.v1' with 'approved_cases'; got schema_version={manifest.get('schema_version')!r}.",
            )
        )
        return issues

    if manifest.get("dataset_version") != "1.1.0-mvp-approved":
        issues.append(
            ReleaseIssue("RELEASE_METADATA", "manifest.dataset_version", f"Expected '1.1.0-mvp-approved', got {manifest.get('dataset_version')!r}.")
        )
    if manifest.get("base_commit_sha") != EXPECTED_BASE_COMMIT_SHA:
        issues.append(
            ReleaseIssue("RELEASE_METADATA", "manifest.base_commit_sha", f"Expected {EXPECTED_BASE_COMMIT_SHA}, got {manifest.get('base_commit_sha')!r}.")
        )
    if manifest.get("od_007_physical_table_assumption") is not False or manifest.get("synthetic_only") is not True:
        issues.append(
            ReleaseIssue("RELEASE_METADATA", "manifest", "Must declare od_007_physical_table_assumption=false and synthetic_only=true.")
        )

    # No invented approver name/account or approval timestamp
    app_rec = manifest.get("approval_record", {})
    if app_rec.get("approved_by_name_or_account") is not None or app_rec.get("approved_at_utc") is not None:
        issues.append(
            ReleaseIssue(
                "NO_INVENTED_APPROVAL_METADATA",
                "manifest.approval_record",
                "approved_by_name_or_account and approved_at_utc must remain null (do not invent non-existent approver name/account/timestamp).",
            )
        )
    if app_rec.get("approval_scope_code") != "SYNTHETIC_INPUTS_AND_LOGICAL_EXPECTATIONS_ONLY":
        issues.append(
            ReleaseIssue(
                "RELEASE_METADATA",
                "manifest.approval_record.approval_scope_code",
                "Approval scope must be restricted to SYNTHETIC_INPUTS_AND_LOGICAL_EXPECTATIONS_ONLY.",
            )
        )

    cpol = app_rec.get("comparison_policy", {})
    if cpol.get("enforce_zero_mutation_on_rejection") is not True:
        issues.append(
            ReleaseIssue("RELEASE_METADATA", "manifest.approval_record.comparison_policy", "enforce_zero_mutation_on_rejection must be true.")
        )
    if cpol.get("confirmed_api_routes_for_handoff", {}).get("manual_merge") != "POST /work-events/actions/merge":
        issues.append(
            ReleaseIssue(
                "CAND_EV021_HANDOFF_ROUTE_REQUIRED",
                "manifest.approval_record.comparison_policy.confirmed_api_routes_for_handoff.manual_merge",
                "Must record confirmed MVP API §5 route 'POST /work-events/actions/merge'.",
            )
        )

    # Validate CAND-EV-021 explicit handoff correction entry
    handoff_corrs = app_rec.get("handoff_corrections_without_mutating_draft_candidates", [])
    ev021_corrs = [
        c for c in handoff_corrs
        if c.get("case_id") == "EV-021"
        and c.get("source_record_id") == "CAND-EV-021"
        and c.get("confirmed_api_route") == "POST /work-events/actions/merge"
    ]
    if len(ev021_corrs) != 1:
        issues.append(
            ReleaseIssue(
                "CAND_EV021_HANDOFF_ROUTE_REQUIRED",
                "manifest.approval_record.handoff_corrections_without_mutating_draft_candidates",
                "Must explicitly record CAND-EV-021 handoff correction to POST /work-events/actions/merge.",
            )
        )

    # Validate exact 21 approved cases: 15 baseline + 1 DELTA-EV-002 + 5 new (EV-019..023)
    approved_cases = manifest.get("approved_cases", [])
    if len(approved_cases) != 21:
        issues.append(
            ReleaseIssue(
                "APPROVED_CASE_COUNT_MISMATCH",
                "manifest.approved_cases",
                f"Expected exactly 21 approved cases (15 baseline + 1 DELTA-EV-002 + 5 new), got {len(approved_cases)}.",
            )
        )

    seen_ids: list[str] = []
    seen_paths: set[str] = set()
    origin_counts = {"BASELINE_1_0_0": 0, "REPLACED_FROM_M0_GATE_DELTA": 0, "NEW_FROM_M0_GATE_DELTA": 0}
    resolved_shas: dict[str, str] = {}
    loaded_docs: dict[str, dict[str, Any]] = {}
    case_entry_map: dict[str, dict[str, Any]] = {}

    for idx, entry in enumerate(approved_cases):
        cid = str(entry.get("case_id", ""))
        rel_path = str(entry.get("path", ""))
        origin = str(entry.get("origin", ""))

        if cid in FORBIDDEN_EXCLUDED_IDS or cid.startswith("NEG-"):
            issues.append(
                ReleaseIssue("EXCLUDED_CASE_LEAKED", f"manifest.approved_cases[{idx}]", f"Excluded case '{cid}' MUST NOT appear in approved_cases.")
            )
        if cid in case_entry_map:
            issues.append(
                ReleaseIssue("DUPLICATE_CASE_ID", f"manifest.approved_cases[{idx}]", f"Duplicate case_id '{cid}' in approved_cases.")
            )
        if rel_path == BASELINE_EV002_REL_PATH:
            issues.append(
                ReleaseIssue(
                    "SUPERSEDED_EV002_CO_LOADED",
                    f"manifest.approved_cases[{idx}]",
                    "Superseded baseline cases/EV-002-default-branch-direct-commit.json MUST NOT be loaded in 1.1.0-mvp-approved.",
                )
            )
        if rel_path in seen_paths:
            issues.append(ReleaseIssue("DUPLICATE_CASE_PATH", f"manifest.approved_cases[{idx}]", f"Duplicate file path '{rel_path}'."))

        seen_ids.append(cid)
        seen_paths.add(rel_path)
        case_entry_map[cid] = entry
        if origin in origin_counts:
            origin_counts[origin] += 1
        else:
            issues.append(ReleaseIssue("RELEASE_METADATA", f"manifest.approved_cases[{idx}].origin", f"Unknown origin {origin!r}."))

        if cid == "EV-002":
            if rel_path != DELTA_EV002_REL_PATH or entry.get("source_record_id") != "DELTA-EV-002" or origin != "REPLACED_FROM_M0_GATE_DELTA":
                issues.append(
                    ReleaseIssue(
                        "EV002_NOT_REPLACED_BY_DELTA",
                        f"manifest.approved_cases[{idx}]",
                        f"EV-002 must resolve strictly to DELTA-EV-002 ({DELTA_EV002_REL_PATH}), got {rel_path!r}.",
                    )
                )
        if cid == "EV-021" and entry.get("confirmed_api_route_handoff") != "POST /work-events/actions/merge":
            issues.append(
                ReleaseIssue(
                    "CAND_EV021_HANDOFF_ROUTE_REQUIRED",
                    f"manifest.approved_cases[{idx}].confirmed_api_route_handoff",
                    "EV-021 entry must record confirmed_api_route_handoff='POST /work-events/actions/merge'.",
                )
            )

        full_path = dataset_dir / rel_path
        cdata, l_issues = _load_json(full_path)
        if l_issues or not isinstance(cdata, dict):
            issues.extend(l_issues)
            continue
        actual_sha = _sha256_file(full_path)
        resolved_shas[cid] = actual_sha
        loaded_docs[cid] = cdata

        if entry.get("file_sha256") != actual_sha:
            issues.append(
                ReleaseIssue(
                    "INTEGRITY_HASH_MISMATCH",
                    f"manifest.approved_cases[{cid}].file_sha256",
                    f"Recorded SHA-256 {entry.get('file_sha256')} != actual {actual_sha}.",
                )
            )

        file_cid = cdata.get("target_case_id") or cdata.get("case_id")
        if file_cid != cid:
            issues.append(
                ReleaseIssue("CASE_ID_MISMATCH", f"manifest.approved_cases[{cid}]", f"File case/target ID {file_cid!r} != manifest '{cid}'.")
            )
        if cdata.get("scope") != "MVP":
            issues.append(
                ReleaseIssue("EXCLUDED_CASE_LEAKED", f"manifest.approved_cases[{cid}]", f"Non-MVP case scope {cdata.get('scope')!r} forbidden in approved release.")
            )
        eg = cdata.get("eval_grouping", {})
        if entry.get("eval_bundle_id") != eg.get("eval_bundle_id") or entry.get("source_lineage_group") != eg.get("source_lineage_group"):
            issues.append(
                ReleaseIssue(
                    "EVAL_BUNDLE_LINEAGE_ISOLATION",
                    f"manifest.approved_cases[{cid}]",
                    f"Manifest bundle/lineage ({entry.get('eval_bundle_id')}/{entry.get('source_lineage_group')}) != file ({eg.get('eval_bundle_id')}/{eg.get('source_lineage_group')}).",
                )
            )
        cov = cdata.get("coverage", {})
        for cfield in ("draft_requirement_ids", "draft_test_ids", "official_requirement_refs", "official_test_refs"):
            if entry.get(cfield) != cov.get(cfield):
                issues.append(
                    ReleaseIssue("TRACEABILITY_MISMATCH", f"manifest.approved_cases[{cid}].{cfield}", f"Manifest {cfield} disagrees with file coverage.")
                )

    missing_ids = EXPECTED_ALL_21_IDS - set(seen_ids)
    extra_ids = set(seen_ids) - EXPECTED_ALL_21_IDS
    if missing_ids:
        issues.append(ReleaseIssue("MISSING_APPROVED_CASES", "manifest.approved_cases", f"Missing required approved case IDs: {sorted(missing_ids)}."))
    if extra_ids:
        issues.append(ReleaseIssue("UNEXPECTED_APPROVED_CASES", "manifest.approved_cases", f"Unexpected case IDs in approved_cases: {sorted(extra_ids)}."))
    if origin_counts != {"BASELINE_1_0_0": 15, "REPLACED_FROM_M0_GATE_DELTA": 1, "NEW_FROM_M0_GATE_DELTA": 5}:
        issues.append(
            ReleaseIssue(
                "APPROVED_COMPOSITION_MISMATCH",
                "manifest.approved_cases",
                f"Expected composition 15/1/5, got {origin_counts}.",
            )
        )

    # Verify composite approved_21_bundle_sha256
    ihashes = manifest.get("integrity_hashes", {})
    recomputed_21_sha = hashlib.sha256("".join(f"{cid}:{resolved_shas[cid]}\n" for cid in sorted(resolved_shas)).encode("utf-8")).hexdigest()
    if ihashes.get("approved_21_bundle_sha256") != recomputed_21_sha:
        issues.append(
            ReleaseIssue(
                "INTEGRITY_HASH_MISMATCH",
                "manifest.integrity_hashes.approved_21_bundle_sha256",
                f"Recorded {ihashes.get('approved_21_bundle_sha256')} != recomputed {recomputed_21_sha}.",
            )
        )
    if ihashes.get("candidate_pack_bundle_sha256") != EXPECTED_CANDIDATE_BUNDLE_SHA256:
        issues.append(
            ReleaseIssue(
                "INTEGRITY_HASH_MISMATCH",
                "manifest.integrity_hashes.candidate_pack_bundle_sha256",
                f"Recorded {ihashes.get('candidate_pack_bundle_sha256')} != {EXPECTED_CANDIDATE_BUNDLE_SHA256}.",
            )
        )

    # Verify same-source evaluation bundle grouping across all 21 cases
    repo_to_bundle: dict[str, tuple[str, str, str]] = {}
    for cid in sorted(loaded_docs):
        cdata = loaded_docs[cid]
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
                        ReleaseIssue(
                            "EVAL_BUNDLE_LINEAGE_ISOLATION",
                            f"approved_cases[{cid}]",
                            f"Cases '{prev_cid}' and '{cid}' share synthetic repo '{rname}' across different bundles ({prev_bid}/{prev_lgrp} vs {bid}/{lgrp}).",
                        )
                    )
            else:
                repo_to_bundle[rname] = (cid, bid, lgrp)

    # Verify evaluation_bundles partition the exact 21 cases with zero EV-017/018 or NEG-*
    bundle_seen: set[str] = set()
    for b in manifest.get("evaluation_bundles", []):
        bid = b.get("eval_bundle_id", "")
        for cid in b.get("case_ids", []):
            if cid in FORBIDDEN_EXCLUDED_IDS or str(cid).startswith("NEG-"):
                issues.append(ReleaseIssue("EXCLUDED_CASE_LEAKED", f"manifest.evaluation_bundles[{bid}]", f"Excluded case '{cid}' forbidden in evaluation_bundles."))
            if cid in bundle_seen:
                issues.append(ReleaseIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", f"manifest.evaluation_bundles[{bid}]", f"Case '{cid}' appears in multiple bundles."))
            bundle_seen.add(cid)
            if cid in case_entry_map and case_entry_map[cid].get("eval_bundle_id") != bid:
                issues.append(
                    ReleaseIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", f"manifest.evaluation_bundles[{bid}]", f"Case '{cid}' bundle disagrees with approved_cases entry.")
                )
    if bundle_seen != EXPECTED_ALL_21_IDS:
        issues.append(
            ReleaseIssue("EVAL_BUNDLE_LINEAGE_ISOLATION", "manifest.evaluation_bundles", f"evaluation_bundles cases {sorted(bundle_seen)} != expected 21 cases.")
        )

    return issues


def load_approved_21_case_set(
    manifest_path: Path | None = None,
    dataset_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Deterministic loader for T-306A consumers. Raises ValueError if release validation fails."""
    release_dir = Path(__file__).resolve().parent
    mpath = manifest_path or (release_dir / "manifest.json")
    ddir = dataset_dir or release_dir.parent.parent
    issues = validate_release_manifest(mpath, ddir)
    if issues:
        raise ValueError("Approved 21-case release validation failed:\n" + "\n".join(i.format() for i in issues))
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    loaded: list[dict[str, Any]] = []
    for entry in manifest["approved_cases"]:
        loaded.append(
            {
                "case_id": entry["case_id"],
                "source_record_id": entry["source_record_id"],
                "origin": entry["origin"],
                "path": str(ddir / entry["path"]),
                "file_sha256": entry["file_sha256"],
                "eval_bundle_id": entry["eval_bundle_id"],
                "confirmed_api_route_handoff": entry.get("confirmed_api_route_handoff"),
                "data": json.loads((ddir / entry["path"]).read_text(encoding="utf-8")),
            }
        )
    return loaded


def main(argv: list[str] | None = None) -> int:
    release_dir = Path(__file__).resolve().parent
    default_manifest = release_dir / "manifest.json"
    default_dataset_dir = release_dir.parent.parent
    parser = argparse.ArgumentParser(description="Validate approved 1.1.0-mvp-approved 21-case MVP synthetic regression release.")
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--dataset-dir", type=Path, default=default_dataset_dir)
    args = parser.parse_args(argv)

    issues = validate_release_manifest(args.manifest, args.dataset_dir)
    if issues:
        print(f"APPROVED RELEASE VALIDATION FAILED ({len(issues)} issue(s)):")
        for issue in issues:
            print("  " + issue.format())
        return 1

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(
        f"APPROVED RELEASE VALIDATION PASSED: {len(manifest['approved_cases'])} MVP cases verified "
        f"(version={manifest['dataset_version']}, 15 baseline + 1 DELTA-EV-002 + 5 new EV-019..023, "
        f"16 same-source bundles, baseline 1.0.0 & 13-file DRAFT candidate pack preserved, SHA-256 verified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
