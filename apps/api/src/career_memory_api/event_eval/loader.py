"""Authoritative read-only loader & anti-misload guards for 1.1.0-mvp-approved (T-306A).

Enforces T-306A-HANDOFF.md §1 strictly:
- Loads ONLY via evaluation/event-draft/releases/1.1.0-mvp-approved/validate_approved_release.py
  and evaluation/event-draft/releases/1.1.0-mvp-approved/manifest.json.
- Verifies approved_21_bundle_sha256 == 91cac742b48ad3bebee2e84aac3f6d3c5d9202c65d2406f3828b8494bf114a4d.
- Rejects loading evaluation/event-draft/manifest.json (1.0.0-mvp-approved) alone.
- Rejects naive directory unions of cases/*.json + candidates/*.json (EV-002 duplication
  and Post-MVP EV-017/EV-018 or NEG-* leakage).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from career_memory_api.event_eval.models import (
    EXPECTED_APPROVED_21_BUNDLE_SHA256,
    EXPECTED_APPROVED_DATASET_VERSION,
    ApprovedCaseEnvelope,
)

FORBIDDEN_CASE_IDS: frozenset[str] = frozenset(
    {
        "EV-017",
        "EV-018",
        "NEG-001",
        "NEG-002",
        "NEG-003",
        "NEG-004",
        "NEG-005",
        "NEG-006",
    }
)
SUPERSEDED_EV002_REL_PATH = "cases/EV-002-default-branch-direct-commit.json"
EXPECTED_DELTA_EV002_REL_PATH = (
    "review-candidates/m0-gate-spec-delta/candidates/"
    "DELTA-EV-002-direct-commit-confirmation-needed.json"
)


class ApprovedReleaseMisloadError(ValueError):
    """Raised when a caller attempts to load an unapproved, baseline-only, or leaked case set."""


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def approved_release_manifest_path(repo_root: Path | None = None) -> Path:
    root = (repo_root or default_repo_root()).resolve()
    return (
        root
        / "evaluation"
        / "event-draft"
        / "releases"
        / "1.1.0-mvp-approved"
        / "manifest.json"
    )


def _load_validate_approved_release_module(repo_root: Path) -> ModuleType:
    module_path = (
        repo_root
        / "evaluation"
        / "event-draft"
        / "releases"
        / "1.1.0-mvp-approved"
        / "validate_approved_release.py"
    )
    if not module_path.is_file():
        raise FileNotFoundError(
            f"Authoritative release validator not found: {module_path}"
        )
    spec = importlib.util.spec_from_file_location(
        "cm_t306a_validate_approved_release",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec from {module_path}")
    module = importlib.util.module_from_spec(spec)
    import sys

    prev_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev_dont_write_bytecode
    return module


def assert_approved_manifest_path_only(
    manifest_path: Path,
    *,
    repo_root: Path | None = None,
) -> None:
    """Reject baseline 1.0.0 manifest or any path other than releases/1.1.0-mvp-approved/manifest.json."""
    root = (repo_root or default_repo_root()).resolve()
    resolved = manifest_path.resolve()
    expected = approved_release_manifest_path(root).resolve()

    if resolved == (root / "evaluation" / "event-draft" / "manifest.json").resolve():
        raise ApprovedReleaseMisloadError(
            "ANTI-MISLOAD GUARD: Loading baseline evaluation/event-draft/manifest.json "
            "(1.0.0-mvp-approved) alone is forbidden for T-306A; use "
            "evaluation/event-draft/releases/1.1.0-mvp-approved/manifest.json."
        )
    if resolved != expected:
        raise ApprovedReleaseMisloadError(
            f"ANTI-MISLOAD GUARD: Manifest path {resolved} is not the authoritative "
            f"1.1.0-mvp-approved entrypoint {expected}."
        )


def validate_loaded_case_composition(cases: list[dict[str, Any]]) -> None:
    """Guard against EV-002 duplication, missing cases, or EV-017/018/NEG-* leakage."""
    seen_ids: set[str] = set()
    for item in cases:
        cid = str(item.get("case_id") or "")
        rel = str(item.get("path") or "")
        if cid in FORBIDDEN_CASE_IDS or cid.startswith("NEG-"):
            raise ApprovedReleaseMisloadError(
                f"ANTI-MISLOAD GUARD: Forbidden excluded case {cid!r} leaked into evaluation set."
            )
        if cid in seen_ids:
            raise ApprovedReleaseMisloadError(
                f"ANTI-MISLOAD GUARD: Duplicate case_id {cid!r} detected (e.g. naive union duplicating EV-002)."
            )
        if rel.endswith(SUPERSEDED_EV002_REL_PATH):
            raise ApprovedReleaseMisloadError(
                "ANTI-MISLOAD GUARD: Superseded baseline EV-002 file loaded instead of DELTA-EV-002."
            )
        seen_ids.add(cid)

    if len(cases) != 21:
        raise ApprovedReleaseMisloadError(
            f"ANTI-MISLOAD GUARD: Expected exactly 21 approved MVP cases, got {len(cases)}."
        )
    ev002_entries = [c for c in cases if c.get("case_id") == "EV-002"]
    if len(ev002_entries) != 1 or ev002_entries[0].get("source_record_id") != "DELTA-EV-002":
        raise ApprovedReleaseMisloadError(
            "ANTI-MISLOAD GUARD: EV-002 must resolve strictly to DELTA-EV-002 once."
        )


def load_approved_21_cases(
    *,
    repo_root: Path | None = None,
    manifest_path: Path | None = None,
) -> tuple[dict[str, Any], tuple[ApprovedCaseEnvelope, ...]]:
    """Validate and load the exact frozen 21-case 1.1.0-mvp-approved release (read-only)."""
    root = (repo_root or default_repo_root()).resolve()
    mpath = (manifest_path or approved_release_manifest_path(root)).resolve()
    assert_approved_manifest_path_only(mpath, repo_root=root)

    dataset_dir = root / "evaluation" / "event-draft"
    validator_mod = _load_validate_approved_release_module(root)

    try:
        raw_loaded: list[dict[str, Any]] = validator_mod.load_approved_21_case_set(
            manifest_path=mpath,
            dataset_dir=dataset_dir,
        )
    except ValueError as exc:
        raise ApprovedReleaseMisloadError(str(exc)) from exc

    validate_loaded_case_composition(raw_loaded)

    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if manifest.get("dataset_version") != EXPECTED_APPROVED_DATASET_VERSION:
        raise ApprovedReleaseMisloadError(
            f"Expected dataset_version={EXPECTED_APPROVED_DATASET_VERSION!r}, "
            f"got {manifest.get('dataset_version')!r}."
        )
    bundle_sha = manifest.get("integrity_hashes", {}).get("approved_21_bundle_sha256")
    if bundle_sha != EXPECTED_APPROVED_21_BUNDLE_SHA256:
        raise ApprovedReleaseMisloadError(
            f"approved_21_bundle_sha256 mismatch: expected {EXPECTED_APPROVED_21_BUNDLE_SHA256}, "
            f"got {bundle_sha}."
        )

    envelopes: list[ApprovedCaseEnvelope] = []
    for item in raw_loaded:
        full_p = Path(item["path"]).resolve()
        rel_p = full_p.relative_to(dataset_dir.resolve()).as_posix()
        envelopes.append(
            ApprovedCaseEnvelope(
                case_id=str(item["case_id"]),
                source_record_id=str(item["source_record_id"]),
                origin=str(item["origin"]),
                rel_path=rel_p,
                file_sha256=str(item["file_sha256"]),
                eval_bundle_id=str(item["eval_bundle_id"]),
                confirmed_api_route_handoff=item.get("confirmed_api_route_handoff"),
                data=dict(item["data"]),
            )
        )

    return manifest, tuple(envelopes)


__all__ = [
    "FORBIDDEN_CASE_IDS",
    "SUPERSEDED_EV002_REL_PATH",
    "ApprovedReleaseMisloadError",
    "approved_release_manifest_path",
    "assert_approved_manifest_path_only",
    "default_repo_root",
    "load_approved_21_cases",
    "validate_loaded_case_composition",
]
