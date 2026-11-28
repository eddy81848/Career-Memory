"""Frozen 0.4.0-synthetic-golden Single-Entrypoint Loader & Anti-Leakage Verifier (T-407A).

Enforces ADR-018 and T-004B -> T-407A governance strictly:
- Reads ONLY ``evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json``.
- Rejects loading archived ``0.3.0-synthetic-golden``, ``APPROVAL_MANIFEST.json``, or
  unapproved ``CANDIDATE_MANIFEST.json`` directly.
- Executes read-only ``validate_dataset.py`` and ``validate_golden_approval.py`` checks.
- Verifies EVERY SHA-256 recorded in ``GOLDEN_APPROVAL_MANIFEST_v0.4.0.json`` (including
  ``CANDIDATE_MANIFEST.json``, all 9 dataset/schema files, and ``approved_candidate_bundle_sha256``).
- Asserts exact approved counts and zero family/revision/scenario/claim/refetch overlap
  between ``SYNTHETIC_REGRESSION_DEV`` and ``SYNTHETIC_REGRESSION_TEST``.
- Isolates ``TEST_FIXTURE_ONLY_NOT_DOMAIN_API`` labels strictly inside the evaluation harness.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from career_memory_api.claim_eval.models import (
    EXPECTED_APPROVED_BUNDLE_SHA256,
    EXPECTED_APPROVED_COUNTS,
    EXPECTED_APPROVED_DATASET_VERSION,
    EXPECTED_CANDIDATE_MANIFEST_SHA256,
)

GOLDEN_APPROVAL_MANIFEST_REL = "evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json"
FORBIDDEN_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "APPROVAL_MANIFEST.json",
        "CANDIDATE_MANIFEST.json",
    }
)


class GoldenDatasetMisloadError(ValueError):
    """Raised when a caller violates the T-407A single-entrypoint or integrity contract."""


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def golden_approval_manifest_path(repo_root: Path | None = None) -> Path:
    root = (repo_root or default_repo_root()).resolve()
    return root / GOLDEN_APPROVAL_MANIFEST_REL


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def assert_golden_approval_manifest_only(
    manifest_path: Path,
    *,
    repo_root: Path | None = None,
) -> None:
    """Reject any manifest other than evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json."""
    root = (repo_root or default_repo_root()).resolve()
    resolved = manifest_path.resolve()
    expected = golden_approval_manifest_path(root).resolve()

    if resolved.name in FORBIDDEN_MANIFEST_NAMES:
        raise GoldenDatasetMisloadError(
            f"ANTI-MISLOAD GUARD: Loading {resolved.name!r} directly is forbidden for T-407A; "
            f"use ONLY {GOLDEN_APPROVAL_MANIFEST_REL}."
        )
    if resolved != expected:
        raise GoldenDatasetMisloadError(
            f"ANTI-MISLOAD GUARD: Manifest path {resolved} is not the authoritative "
            f"0.4.0-synthetic-golden entrypoint {expected}."
        )


def run_frozen_dataset_validators(repo_root: Path) -> list[str]:
    """Run validate_dataset.py and validate_golden_approval.py strictly read-only (-B)."""
    commands = [
        (
            "validate_dataset",
            [
                sys.executable,
                "-B",
                str(repo_root / "evaluation/claim-draft/tools/validate_dataset.py"),
            ],
        ),
        (
            "validate_golden_approval",
            [
                sys.executable,
                "-B",
                str(repo_root / "evaluation/claim-draft/tools/validate_golden_approval.py"),
            ],
        ),
    ]
    errors: list[str] = []
    for label, cmd in commands:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            errors.append(
                f"{label} failed (rc={proc.returncode}): {(proc.stdout + proc.stderr).strip()}"
            )
    return errors


@dataclass(frozen=True, slots=True)
class LoadedGoldenClaimDataset:
    """Read-only in-memory representation of the frozen 0.4.0-synthetic-golden dataset."""

    dataset_version: str
    approval_manifest_path: Path
    approval_manifest_sha256: str
    candidate_manifest_sha256: str
    approved_candidate_bundle_sha256: str
    approval_manifest: dict[str, Any]
    candidate_manifest: dict[str, Any]
    source_revisions: tuple[dict[str, Any], ...]
    refetch_snapshots: tuple[dict[str, Any], ...]
    content_units: tuple[dict[str, Any], ...]
    stage2_doc: dict[str, Any]
    stage3_doc: dict[str, Any]
    stage4_doc: dict[str, Any]
    expected_oracle: dict[str, Any]
    expected_refetch_oracle: dict[str, Any]
    partitions: dict[str, dict[str, Any]]
    partition_by_scenario: dict[str, str]
    partition_by_claim: dict[str, str]
    partition_by_refetch_case: dict[str, str]
    source_revisions_by_id: dict[str, dict[str, Any]]
    content_units_by_id: dict[str, dict[str, Any]]
    canary_tokens: tuple[str, ...]
    raw_source_texts_by_rev: dict[str, str]


def _verify_partition_isolation(
    *,
    partitions: dict[str, dict[str, Any]],
    expected_oracle: dict[str, Any],
    stage3_doc: dict[str, Any],
    stage4_doc: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    dev = partitions.get("SYNTHETIC_REGRESSION_DEV", {})
    test = partitions.get("SYNTHETIC_REGRESSION_TEST", {})
    if not dev or not test:
        raise GoldenDatasetMisloadError(
            "Both SYNTHETIC_REGRESSION_DEV and SYNTHETIC_REGRESSION_TEST partitions are required."
        )

    # Disjointness across families, scenarios, claims, and refetch cases
    for key in ("family_ids", "scenario_ids", "claim_ids", "refetch_fixture_case_ids"):
        d_set = set(dev.get(key, []))
        t_set = set(test.get(key, []))
        if len(d_set) != len(dev.get(key, [])) or len(t_set) != len(test.get(key, [])):
            raise GoldenDatasetMisloadError(f"Duplicate entries found in partition field {key!r}.")
        overlap = d_set & t_set
        if overlap:
            raise GoldenDatasetMisloadError(
                f"DEV/TEST partition leakage on {key!r}: {sorted(overlap)}"
            )

    part_by_scn: dict[str, str] = {}
    for sid in dev["scenario_ids"]:
        part_by_scn[str(sid)] = "SYNTHETIC_REGRESSION_DEV"
    for sid in test["scenario_ids"]:
        part_by_scn[str(sid)] = "SYNTHETIC_REGRESSION_TEST"

    part_by_claim: dict[str, str] = {}
    for cid in dev["claim_ids"]:
        part_by_claim[str(cid)] = "SYNTHETIC_REGRESSION_DEV"
    for cid in test["claim_ids"]:
        part_by_claim[str(cid)] = "SYNTHETIC_REGRESSION_TEST"

    part_by_ref: dict[str, str] = {}
    for rid in dev["refetch_fixture_case_ids"]:
        part_by_ref[str(rid)] = "SYNTHETIC_REGRESSION_DEV"
    for rid in test["refetch_fixture_case_ids"]:
        part_by_ref[str(rid)] = "SYNTHETIC_REGRESSION_TEST"

    # Zero source_revision_id overlap across DEV and TEST!
    dev_revs: set[str] = set()
    test_revs: set[str] = set()
    for evt in stage3_doc.get("confirmed_events", []):
        sid = str(evt["scenario_id"])
        part = part_by_scn.get(sid)
        if part == "SYNTHETIC_REGRESSION_DEV":
            dev_revs.update(str(r) for r in evt.get("source_revision_ids", []))
        elif part == "SYNTHETIC_REGRESSION_TEST":
            test_revs.update(str(r) for r in evt.get("source_revision_ids", []))
    for chk in stage4_doc.get("refetch_checks", []):
        rcid = str(chk["fixture_case_id"])
        rev = str(chk["baseline_source_revision_id"])
        part = part_by_ref.get(rcid)
        if part == "SYNTHETIC_REGRESSION_DEV":
            dev_revs.add(rev)
        elif part == "SYNTHETIC_REGRESSION_TEST":
            test_revs.add(rev)

    rev_overlap = dev_revs & test_revs
    if rev_overlap:
        raise GoldenDatasetMisloadError(
            f"DEV/TEST source_revision_id overlap detected: {sorted(rev_overlap)}"
        )

    # Verify every scenario's partition & family match oracle_draft.json
    for scn in expected_oracle.get("scenarios", []):
        sid = str(scn["scenario_id"])
        fam = str(scn["evaluation_family_id"])
        part = str(scn["evaluation_partition"])
        if part_by_scn.get(sid) != part:
            raise GoldenDatasetMisloadError(
                f"Scenario {sid} partition mismatch: manifest={part_by_scn.get(sid)} oracle={part}"
            )
        if fam not in partitions[part]["family_ids"]:
            raise GoldenDatasetMisloadError(
                f"Scenario {sid} family {fam} not locked inside partition {part}."
            )

    return part_by_scn, part_by_claim, part_by_ref


def load_frozen_golden_dataset(
    *,
    repo_root: Path | None = None,
    manifest_path: Path | None = None,
    run_subprocess_validators: bool = True,
) -> LoadedGoldenClaimDataset:
    """Validate and load the frozen 0.4.0-synthetic-golden dataset strictly read-only."""
    root = (repo_root or default_repo_root()).resolve()
    mpath = (manifest_path or golden_approval_manifest_path(root)).resolve()
    assert_golden_approval_manifest_only(mpath, repo_root=root)

    if not mpath.is_file():
        raise GoldenDatasetMisloadError(f"Golden approval manifest not found: {mpath}")

    if run_subprocess_validators:
        val_errors = run_frozen_dataset_validators(root)
        if val_errors:
            raise GoldenDatasetMisloadError(
                "Frozen dataset validators failed:\n" + "\n".join(f"  - {e}" for e in val_errors)
            )

    approval_sha = _sha256_file(mpath)
    approval_doc = json.loads(mpath.read_text(encoding="utf-8"))

    if approval_doc.get("dataset_version") != EXPECTED_APPROVED_DATASET_VERSION:
        raise GoldenDatasetMisloadError(
            f"Expected dataset_version={EXPECTED_APPROVED_DATASET_VERSION!r}, "
            f"got {approval_doc.get('dataset_version')!r}."
        )
    if approval_doc.get("approval_status") != "APPROVED_SYNTHETIC_REGRESSION_GOLDEN":
        raise GoldenDatasetMisloadError(
            f"Unexpected approval_status={approval_doc.get('approval_status')!r}."
        )
    if approval_doc.get("approved_candidate_bundle_sha256") != EXPECTED_APPROVED_BUNDLE_SHA256:
        raise GoldenDatasetMisloadError(
            f"approved_candidate_bundle_sha256 mismatch: expected {EXPECTED_APPROVED_BUNDLE_SHA256}, "
            f"got {approval_doc.get('approved_candidate_bundle_sha256')}."
        )
    afc = approval_doc.get("approved_from_candidate", {})
    if afc.get("candidate_manifest_sha256") != EXPECTED_CANDIDATE_MANIFEST_SHA256:
        raise GoldenDatasetMisloadError(
            f"candidate_manifest_sha256 mismatch: expected {EXPECTED_CANDIDATE_MANIFEST_SHA256}, "
            f"got {afc.get('candidate_manifest_sha256')}."
        )

    # Verify every single file SHA-256 on disk
    repo_rel_hashes: dict[str, str] = dict(approval_doc.get("repo_relative_artifact_sha256", {}))
    if len(repo_rel_hashes) != 10:
        raise GoldenDatasetMisloadError(
            f"Expected 10 hashed artifacts (CANDIDATE_MANIFEST + 9 dataset files), got {len(repo_rel_hashes)}."
        )
    for rel_path, expected_digest in sorted(repo_rel_hashes.items()):
        fpath = root / rel_path
        if not fpath.is_file():
            raise GoldenDatasetMisloadError(f"Missing frozen artifact: {rel_path}")
        actual_digest = _sha256_file(fpath)
        if actual_digest != expected_digest:
            raise GoldenDatasetMisloadError(
                f"SHA-256 mismatch on {rel_path}: expected {expected_digest}, got {actual_digest}"
            )

    # Recompute composite candidate bundle SHA-256 over the 9 dataset/schema files (matching validate_dataset.py)
    artifact_hashes: dict[str, str] = dict(approval_doc.get("artifact_sha256", {}))
    bundle_hasher = hashlib.sha256()
    for rel_key in sorted(artifact_hashes.keys()):
        fpath = root / "evaluation" / "claim-draft" / rel_key
        digest = _sha256_file(fpath)
        if digest != artifact_hashes[rel_key]:
            raise GoldenDatasetMisloadError(
                f"Artifact SHA-256 mismatch for {rel_key}: {digest} != {artifact_hashes[rel_key]}"
            )
        bundle_hasher.update(f"{rel_key}  {digest}\n".encode("utf-8"))
    computed_bundle_sha = "sha256:" + bundle_hasher.hexdigest()
    if computed_bundle_sha != EXPECTED_APPROVED_BUNDLE_SHA256:
        raise GoldenDatasetMisloadError(
            f"Recomputed candidate bundle SHA-256 {computed_bundle_sha} != {EXPECTED_APPROVED_BUNDLE_SHA256}"
        )

    # Load all JSON documents strictly via t407a_single_entrypoint.dataset_files
    dataset_files = approval_doc["t407a_single_entrypoint"]["dataset_files"]

    def _read_json(key: str) -> dict[str, Any]:
        return json.loads((root / dataset_files[key]).read_text(encoding="utf-8"))

    cand_manifest = _read_json("candidate_manifest")
    src_doc = _read_json("source_revisions")
    snap_doc = _read_json("refetch_snapshots")
    s1_doc = _read_json("stage1_content_units")
    s2_doc = _read_json("stage2_fact_extraction")
    s3_doc = _read_json("stage3_claim_assembly")
    s4_doc = _read_json("stage4_refetch_checks")
    oracle_doc = _read_json("expected_oracle")
    ref_oracle_doc = _read_json("expected_refetch_oracle")

    # Assert exact approved counts
    partitions = dict(approval_doc.get("evaluation_partitions", {}))
    observed_counts = {
        "source_revisions": len(src_doc["source_revisions"]),
        "content_units": len(s1_doc["content_units"]),
        "fact_candidates": len(s2_doc["fact_candidates"]),
        "fact_support_validations": len(s2_doc["fact_support_validations"]),
        "canonical_facts": len(s2_doc["canonical_facts"]),
        "fact_supports": len(s2_doc["fact_supports"]),
        "confirmed_events": len(s3_doc["confirmed_events"]),
        "scenarios": len(oracle_doc["scenarios"]),
        "claims": len(s3_doc["claims"]),
        "claim_atoms": len(s3_doc["claim_atoms"]),
        "claim_atom_supports": len(s3_doc["claim_atom_supports"]),
        "refetch_cases": len(s4_doc["refetch_checks"]),
        "evaluation_families": (
            len(partitions["SYNTHETIC_REGRESSION_DEV"]["family_ids"])
            + len(partitions["SYNTHETIC_REGRESSION_TEST"]["family_ids"])
        ),
    }
    if observed_counts != EXPECTED_APPROVED_COUNTS:
        raise GoldenDatasetMisloadError(
            f"Approved dataset counts mismatch: observed={observed_counts} expected={EXPECTED_APPROVED_COUNTS}"
        )
    if approval_doc.get("approved_counts") != EXPECTED_APPROVED_COUNTS:
        raise GoldenDatasetMisloadError(
            f"Manifest approved_counts mismatch: {approval_doc.get('approved_counts')}"
        )

    part_by_scn, part_by_claim, part_by_ref = _verify_partition_isolation(
        partitions=partitions,
        expected_oracle=oracle_doc,
        stage3_doc=s3_doc,
        stage4_doc=s4_doc,
    )

    revs = tuple(src_doc["source_revisions"])
    snaps = tuple(snap_doc["refetch_snapshots"])
    units = tuple(s1_doc["content_units"])

    canary_set: set[str] = set()
    for snap in snaps:
        for tok in snap.get("canary_tokens", []):
            if isinstance(tok, str) and tok.strip():
                canary_set.add(tok.strip())

    raw_by_rev = {
        str(r["source_revision_id"]): str(r["raw_text_fixture_only"]) for r in revs
    }

    return LoadedGoldenClaimDataset(
        dataset_version=EXPECTED_APPROVED_DATASET_VERSION,
        approval_manifest_path=mpath,
        approval_manifest_sha256=approval_sha,
        candidate_manifest_sha256=EXPECTED_CANDIDATE_MANIFEST_SHA256,
        approved_candidate_bundle_sha256=EXPECTED_APPROVED_BUNDLE_SHA256,
        approval_manifest=approval_doc,
        candidate_manifest=cand_manifest,
        source_revisions=revs,
        refetch_snapshots=snaps,
        content_units=units,
        stage2_doc=s2_doc,
        stage3_doc=s3_doc,
        stage4_doc=s4_doc,
        expected_oracle=oracle_doc,
        expected_refetch_oracle=ref_oracle_doc,
        partitions=partitions,
        partition_by_scenario=part_by_scn,
        partition_by_claim=part_by_claim,
        partition_by_refetch_case=part_by_ref,
        source_revisions_by_id={str(r["source_revision_id"]): r for r in revs},
        content_units_by_id={str(u["content_unit_id"]): u for u in units},
        canary_tokens=tuple(sorted(canary_set)),
        raw_source_texts_by_rev=raw_by_rev,
    )


__all__ = [
    "FORBIDDEN_MANIFEST_NAMES",
    "GOLDEN_APPROVAL_MANIFEST_REL",
    "GoldenDatasetMisloadError",
    "LoadedGoldenClaimDataset",
    "assert_golden_approval_manifest_only",
    "default_repo_root",
    "golden_approval_manifest_path",
    "load_frozen_golden_dataset",
    "run_frozen_dataset_validators",
]
