#!/usr/bin/env python3
"""Standalone T-506 Closed Pilot & 3-Tier Go/No-Go Release Gate CLI.

Programmatically verifies all 7 committed evidence bundles (B-004, T-306A, T-407A,
T-408/INF-08, T-507/INF-07, T-505/INF-10, T-504A), enforces immutable M0 OpenAPI pin
and strict 3-tier release gate honesty (ADR-018, ADR-019, ADR-020).

Usage:
  cd apps/api && uv run python ../../scripts/run_t506_pilot_gate.py --write-evidence
  cd apps/api && uv run python ../../scripts/run_t506_pilot_gate.py --verify
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.conformance.ai_schemas import render_canonical_json  # noqa: E402
from career_memory_api.conformance.checks import M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1  # noqa: E402
from career_memory_api.pilot_gate import (  # noqa: E402
    ReleaseGateDecision,
    ReleaseGateStatusCode,
    ReleaseGateTier,
    assert_strict_three_tier_honesty,
    evaluate_t506_pilot_gate,
    normalize_t506_gate_for_drift_check,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Career Memory T-506 Closed Pilot & 3-Tier Go/No-Go Release Gate CLI"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root directory (defaults to parent of scripts/).",
    )
    parser.add_argument(
        "--write-evidence",
        action="store_true",
        help="Write contracts/pilot-gate/t506_release_gate.v1.json from this evaluation.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify all evidence bundles and assert 0 drift against committed t506_release_gate.v1.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    evidence_path = repo_root / "contracts" / "pilot-gate" / "t506_release_gate.v1.json"

    bundle = evaluate_t506_pilot_gate(repo_root=repo_root)
    if not bundle.openapi_pin_verified or bundle.openapi_import_v1_blob_sha != M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1:
        print(
            f"[t506] FAILED: contracts/import-v1/openapi.yaml blob SHA drifted: "
            f"expected {M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1}, got {bundle.openapi_import_v1_blob_sha}",
            file=sys.stderr,
        )
        return 1

    assert_strict_three_tier_honesty(bundle)

    for b in bundle.evidence_bundles:
        label = "VERIFIED" if b.verified else "FAILED"
        print(
            f"[t506] bundle {b.bundle_id:12s} {label} ({b.checks_passed}/{b.checks_total} checks)"
        )
        for f in b.findings:
            print(f"  [FINDING] {f}", file=sys.stderr)

    gates = {g.tier: g for g in bundle.release_gates}
    local_gate = gates[ReleaseGateTier.LOCAL_DETERMINISTIC_MVP_GATE]
    hosted_gate = gates[ReleaseGateTier.PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE]
    ai_gate = gates[ReleaseGateTier.LIVE_SUBSCRIPTION_AI_PILOT_GATE]

    if local_gate.decision is not ReleaseGateDecision.GO or local_gate.status_code is not ReleaseGateStatusCode.PASSED:
        print("[t506] FAILED: LOCAL_DETERMINISTIC_MVP_GATE did not reach GO/PASSED.", file=sys.stderr)
        return 1
    if hosted_gate.decision is not ReleaseGateDecision.NO_GO or hosted_gate.status_code is not ReleaseGateStatusCode.HELD_PENDING_OD013_OD015:
        print("[t506] FAILED: PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE must be NO-GO/HELD_PENDING_OD013_OD015.", file=sys.stderr)
        return 1
    if ai_gate.decision is not ReleaseGateDecision.NO_GO or ai_gate.status_code is not ReleaseGateStatusCode.HELD_PENDING_OD002_OD014:
        print("[t506] FAILED: LIVE_SUBSCRIPTION_AI_PILOT_GATE must be NO-GO/HELD_PENDING_OD002_OD014.", file=sys.stderr)
        return 1

    live_payload = bundle.model_dump(mode="json")

    if args.write_evidence:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(render_canonical_json(live_payload), encoding="utf-8")
        print(f"[t506] Wrote release gate bundle to {evidence_path.relative_to(repo_root)}")

    if args.verify:
        if not evidence_path.is_file():
            print(
                f"[t506] FAILED: committed release gate bundle missing at {evidence_path}",
                file=sys.stderr,
            )
            return 1
        committed = json.loads(evidence_path.read_text(encoding="utf-8"))
        expected_norm = render_canonical_json(normalize_t506_gate_for_drift_check(committed))
        actual_norm = render_canonical_json(normalize_t506_gate_for_drift_check(live_payload))
        if expected_norm != actual_norm:
            diff = "".join(
                difflib.unified_diff(
                    expected_norm.splitlines(keepends=True),
                    actual_norm.splitlines(keepends=True),
                    fromfile="committed/t506_release_gate.v1.json",
                    tofile="live/t506_release_gate.v1.json",
                )
            )
            print(
                "[t506] FAILED: committed t506_release_gate.v1.json drifted from live evaluation:\n"
                + diff,
                file=sys.stderr,
            )
            return 1
        print("[t506] VERIFIED: t506_release_gate.v1.json matches live evaluation with 0 drift.")

    print(
        f"[t506] PASSED | LOCAL_DETERMINISTIC_MVP_GATE={local_gate.decision.value} ({local_gate.status_code.value}) | "
        f"PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE={hosted_gate.decision.value} ({hosted_gate.status_code.value}) | "
        f"LIVE_SUBSCRIPTION_AI_PILOT_GATE={ai_gate.decision.value} ({ai_gate.status_code.value}) | "
        f"bundles={bundle.summary['bundles_verified']}/{bundle.summary['bundles_total']} | "
        f"invariants={bundle.summary['invariants_passed']}/{bundle.summary['invariants_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
