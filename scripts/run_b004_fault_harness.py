#!/usr/bin/env python3
"""Standalone B-004 Fault Harness CLI (T-005A -> T-005B -> T-005C + T-005D Evidence Verifier).

Boots an ephemeral local PostgreSQL 18 cluster, applies real migrations 0010..0050 with
NOBYPASSRLS roles, executes all 16 B-004 scenarios (S01..S16) against the REAL M2 production
initial-import stack, verifies all 6 global invariants and REF-04/06 0-leak canary audits,
compares strictly READ-ONLY against static expected snapshots, and cleans up the cluster.

STRICT T-005C PROHIBITION:
This CLI intentionally provides NO `--update-snapshots` flag and NEVER writes or overwrites
`contracts/fault-harness/expected_snapshots/*.json`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.fault_harness import (  # noqa: E402
    B004PostgresScenarioRunner,
    EphemeralB004Pg18Cluster,
    SCENARIO_SPECS,
    build_redacted_evidence_bundle,
    compare_logical_snapshots,
)
from career_memory_api.fault_harness.scenario_schema import FaultInjectionPoint  # noqa: E402

IMMUTABLE_OPENAPI_BLOB_SHA = "07594d75425744d6149250a9f7ff95ac24659f80"


def _resolve_openapi_blob_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD:contracts/import-v1/openapi.yaml"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Career Memory B-004 Initial Backfill Fault Harness CLI "
            "(read-only expected snapshot comparison; auto-update prohibited)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root directory (defaults to parent of scripts/).",
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=None,
        help="Optional subset of scenario IDs (e.g. S01 S02). Defaults to all 16 (S01..S16).",
    )
    parser.add_argument(
        "--verify-bundle",
        action="store_true",
        help=(
            "Verify committed contracts/fault-harness/b004_evidence_bundle.v1.json matches "
            "the live PG18 run with zero drift (requires full S01..S16 run)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    snapshots_dir = repo_root / "contracts" / "fault-harness" / "expected_snapshots"
    bundle_path = repo_root / "contracts" / "fault-harness" / "b004_evidence_bundle.v1.json"

    openapi_blob_sha = _resolve_openapi_blob_sha(repo_root)
    if openapi_blob_sha != IMMUTABLE_OPENAPI_BLOB_SHA:
        print(
            f"[b004] FAILED: contracts/import-v1/openapi.yaml blob hash drifted: "
            f"expected {IMMUTABLE_OPENAPI_BLOB_SHA}, got {openapi_blob_sha}",
            file=sys.stderr,
        )
        return 1

    if args.verify_bundle and args.scenarios is not None:
        print(
            "[b004] FAILED: --verify-bundle requires running all 16 scenarios (omit --scenarios).",
            file=sys.stderr,
        )
        return 1

    cluster = EphemeralB004Pg18Cluster(repo_root=repo_root, prefix="cm-b004-cli-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()

        runner = B004PostgresScenarioRunner(
            cluster,
            expected_snapshots_dir=snapshots_dir,
        )

        if args.scenarios is None:
            results = runner.run_all()
        else:
            results = []
            s01_snap = None
            for sid in args.scenarios:
                res = runner.run_scenario(
                    sid,
                    baseline_snapshot=s01_snap if sid.upper() in ("S02", "S03", "S04") else None,
                )
                if sid.upper() == "S01":
                    s01_snap = res.logical_snapshot
                results.append(res)

        failures = 0
        for res in results:
            status_label = "PASSED" if res.passed else "FAILED"
            print(
                f"[b004] {res.spec.scenario_id} {status_label} | "
                f"invariants_viol={len(res.all_violations)} | "
                f"canary_hits={res.canary_audit.canary_hits} | "
                f"planC={res.canary_audit.content_units_plan_c_compliant} | "
                f"s01_equiv={res.baseline_equivalence_matched} | "
                f"core_sha={res.logical_snapshot.core_convergence_sha256[:12]}"
            )
            if not res.passed:
                failures += 1
                for v in res.all_violations:
                    print(
                        f"  [INVARIANT {v.invariant_id} @ {v.phase}] {v.message}",
                        file=sys.stderr,
                    )
                if not res.canary_audit.passed:
                    print(
                        f"  [CANARY AUDIT FAILED] hits={res.canary_audit.canary_hits} "
                        f"locations={res.canary_audit.leak_locations}",
                        file=sys.stderr,
                    )
                if res.comparison is not None and not res.comparison.matched:
                    print(res.comparison.format_report(), file=sys.stderr)
                if res.baseline_equivalence_matched is False:
                    print(
                        f"  [BASELINE EQUIVALENCE FAILED] {res.spec.scenario_id} core hash "
                        f"{res.logical_snapshot.core_convergence_sha256} != S01 baseline",
                        file=sys.stderr,
                    )

        if failures > 0:
            print(
                f"[b004] FAILED: {failures}/{len(results)} scenario(s) failed.",
                file=sys.stderr,
            )
            return 1

        if args.verify_bundle:
            if len(results) != len(SCENARIO_SPECS):
                print(
                    f"[b004] FAILED: expected {len(SCENARIO_SPECS)} scenarios for bundle verification, got {len(results)}.",
                    file=sys.stderr,
                )
                return 1
            if not bundle_path.is_file():
                print(
                    f"[b004] FAILED: committed evidence bundle missing at {bundle_path}.",
                    file=sys.stderr,
                )
                return 1
            expected_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            actual_bundle = build_redacted_evidence_bundle(
                results, openapi_blob_sha=openapi_blob_sha
            )
            bundle_cmp = compare_logical_snapshots(
                scenario_id="EVIDENCE_BUNDLE",
                injection_points=(FaultInjectionPoint.NONE,),
                expected=expected_bundle,
                actual=actual_bundle,
            )
            if not bundle_cmp.matched:
                print(
                    "[b004] FAILED: committed b004_evidence_bundle.v1.json drifted from live PG18 run:",
                    file=sys.stderr,
                )
                print(bundle_cmp.format_report(), file=sys.stderr)
                return 1
            print(
                "[b004] VERIFIED: b004_evidence_bundle.v1.json matches live PG18 run with 0 drift."
            )

        print(
            f"[b004] PASSED: {len(results)}/{len(results)} scenarios passed, "
            "0 invariant violations, 0 canary leaks, 0 snapshot diffs."
        )
        return 0
    finally:
        cluster.stop()


if __name__ == "__main__":
    raise SystemExit(main())
