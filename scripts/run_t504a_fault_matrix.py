#!/usr/bin/env python3
"""Standalone T-504 / T-504A Cross-Subsystem Fault & Race Matrix CLI.

Runs all 12 deterministic cross-subsystem fault & threading.Barrier race scenarios
(XFLT-01..XFLT-12) against a real ephemeral PostgreSQL 18 cluster (0010..0080,
NOBYPASSRLS + FORCE RLS, 0 external calls, ₩0 spend).

Usage:
  cd apps/api && uv run python ../../scripts/run_t504a_fault_matrix.py --write-evidence
  cd apps/api && uv run python ../../scripts/run_t504a_fault_matrix.py --verify
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
from career_memory_api.fault_matrix import (  # noqa: E402
    normalize_t504a_bundle_for_drift_check,
    run_t504a_fault_matrix,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Career Memory T-504 / T-504A Cross-Subsystem Fault & Race Matrix CLI"
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
        help="Optional subset of scenario IDs (e.g. XFLT-01 XFLT-04). Defaults to all 12.",
    )
    parser.add_argument(
        "--write-evidence",
        action="store_true",
        help="Write contracts/fault-matrix/t504a_evidence_bundle.v1.json from this run.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run all 12 scenarios on PG18 and assert 0 drift against committed t504a_evidence_bundle.v1.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    evidence_path = repo_root / "contracts" / "fault-matrix" / "t504a_evidence_bundle.v1.json"

    if (args.write_evidence or args.verify) and args.scenarios is not None:
        print(
            "[t504a] FAILED: --write-evidence and --verify require running all 12 scenarios.",
            file=sys.stderr,
        )
        return 1

    bundle = run_t504a_fault_matrix(repo_root=repo_root, scenario_ids=args.scenarios)
    if bundle.openapi_import_v1_blob_sha != M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1:
        print(
            f"[t504a] FAILED: contracts/import-v1/openapi.yaml blob SHA drifted: "
            f"expected {M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1}, got {bundle.openapi_import_v1_blob_sha}",
            file=sys.stderr,
        )
        return 1

    for sc in bundle.scenarios:
        status = "PASSED" if sc.passed else "FAILED"
        print(
            f"[t504a] {sc.scenario_id.value} {status} | "
            f"assertions={sc.assertions_checked - sc.assertions_failed}/{sc.assertions_checked} | "
            f"barrier={sc.barrier_synchronized} | "
            f"planC={sc.plan_c_audit.passed} ({sc.plan_c_audit.audited_table_count} tables)"
        )

    if not bundle.summary["all_passed"]:
        print(
            "[t504a] FAILED: one or more scenarios or invariants failed:\n"
            + render_canonical_json(bundle.model_dump(mode="json")),
            file=sys.stderr,
        )
        return 1

    live_payload = bundle.model_dump(mode="json")

    if args.write_evidence:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(render_canonical_json(live_payload), encoding="utf-8")
        print(f"[t504a] Wrote evidence bundle to {evidence_path.relative_to(repo_root)}")

    if args.verify:
        if not evidence_path.is_file():
            print(
                f"[t504a] FAILED: committed evidence bundle missing at {evidence_path}",
                file=sys.stderr,
            )
            return 1
        committed = json.loads(evidence_path.read_text(encoding="utf-8"))
        expected_norm = render_canonical_json(normalize_t504a_bundle_for_drift_check(committed))
        actual_norm = render_canonical_json(normalize_t504a_bundle_for_drift_check(live_payload))
        if expected_norm != actual_norm:
            diff = "".join(
                difflib.unified_diff(
                    expected_norm.splitlines(keepends=True),
                    actual_norm.splitlines(keepends=True),
                    fromfile="committed/t504a_evidence_bundle.v1.json",
                    tofile="live/t504a_evidence_bundle.v1.json",
                )
            )
            print(
                "[t504a] FAILED: committed t504a_evidence_bundle.v1.json drifted from live PG18 run:\n"
                + diff,
                file=sys.stderr,
            )
            return 1
        print(
            "[t504a] VERIFIED: t504a_evidence_bundle.v1.json matches live PG18 run with 0 drift."
        )

    print(
        f"[t504a] PASSED ({bundle.summary['passed_scenarios']}/{bundle.summary['total_scenarios']} scenarios, "
        f"{bundle.summary['total_assertions_checked']} assertions, "
        f"barrier_races={bundle.summary['barrier_synchronized_scenarios']}, "
        f"planC_violations=0, sha={bundle.deterministic_matrix_sha256[:19]})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
