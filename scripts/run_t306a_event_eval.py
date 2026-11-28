#!/usr/bin/env python3
"""T-306 / T-306A Frozen 21-Case Event Evaluation CLI & Redacted Bundle Verifier.

Boots an ephemeral local PostgreSQL 18 cluster, applies migrations 0010..0060 with
NOBYPASSRLS runtime roles (cm_api_app, cm_worker_app), validates all 3 frozen dataset
layers (1.0.0 baseline, 13-file M0-GATE DRAFT candidate pack, 1.1.0-mvp-approved 21-case
release), executes all 21 approved cases against the REAL M3 production stack,
performs strictly READ-ONLY oracle comparison and Plan C (ADR-013) audits, and
either writes or zero-drift verifies `contracts/event-eval/t306a_eval_bundle.v1.json`.

ABSOLUTE PROHIBITION (ADR-018, T-306A Step 5):
This CLI intentionally provides NO oracle overwrite flag and NEVER modifies any
file under `evaluation/event-draft/**`.
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

from career_memory_api.event_eval import (  # noqa: E402
    IMMUTABLE_OPENAPI_BLOB_SHA,
    EphemeralEventEvalPg18Cluster,
    FrozenEventEvalRunner,
    build_redacted_eval_bundle,
    diff_deterministic_bundles,
)


def _resolve_openapi_blob_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD:contracts/import-v1/openapi.yaml"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _verify_frozen_dataset_3_layers(repo_root: Path) -> list[str]:
    """Run all 3 authoritative frozen dataset integrity validators (read-only)."""
    commands = [
        (
            "layer1-baseline-lint",
            [
                sys.executable,
                "-B",
                str(repo_root / "evaluation/event-draft/lint/lint_fixtures.py"),
                "--dataset-dir",
                str(repo_root / "evaluation/event-draft"),
                "--verify-negatives",
            ],
        ),
        (
            "layer2-m0-gate-delta",
            [
                sys.executable,
                "-B",
                str(
                    repo_root
                    / "evaluation/event-draft/review-candidates/m0-gate-spec-delta/validate_delta_candidates.py"
                ),
            ],
        ),
        (
            "layer3-approved-1.1.0",
            [
                sys.executable,
                "-B",
                str(
                    repo_root
                    / "evaluation/event-draft/releases/1.1.0-mvp-approved/validate_approved_release.py"
                ),
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Career Memory M3 T-306 / T-306A Frozen 21-Case Event Evaluation CLI "
            "(read-only oracle comparison; oracle auto-update prohibited)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root directory (defaults to parent of scripts/).",
    )
    parser.add_argument(
        "--bundle-path",
        type=Path,
        default=None,
        help="Override output/verify bundle path (defaults to contracts/event-eval/t306a_eval_bundle.v1.json).",
    )
    parser.add_argument(
        "--write-bundle",
        action="store_true",
        help="Write redacted evaluation evidence bundle to contracts/event-eval/t306a_eval_bundle.v1.json.",
    )
    parser.add_argument(
        "--verify-bundle",
        action="store_true",
        help="Verify committed contracts/event-eval/t306a_eval_bundle.v1.json matches live PG18 run with 0 drift.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    bundle_path = (
        args.bundle_path.resolve()
        if args.bundle_path is not None
        else repo_root / "contracts" / "event-eval" / "t306a_eval_bundle.v1.json"
    )

    # 1. Immutable OpenAPI contract check
    openapi_sha = _resolve_openapi_blob_sha(repo_root)
    if openapi_sha != IMMUTABLE_OPENAPI_BLOB_SHA:
        print(
            f"[t306a] FAILED: contracts/import-v1/openapi.yaml drifted: "
            f"expected {IMMUTABLE_OPENAPI_BLOB_SHA}, got {openapi_sha}",
            file=sys.stderr,
        )
        return 1

    # 2. Frozen 3-layer dataset integrity check
    layer_errors = _verify_frozen_dataset_3_layers(repo_root)
    if layer_errors:
        print("[t306a] FAILED: Frozen 1.1.0-mvp-approved 3-layer integrity check failed:", file=sys.stderr)
        for err in layer_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # 3. Boot ephemeral PG18 (0010..0060, NOBYPASSRLS) and run all 21 cases
    cluster = EphemeralEventEvalPg18Cluster(repo_root=repo_root, prefix="cm-t306a-cli-")
    cluster.start()
    try:
        cluster.apply_all_migrations()
        cluster.provision_runtime_logins()

        runner = FrozenEventEvalRunner(cluster, repo_root=repo_root)
        suite = runner.run_all()

        failures = 0
        for res in suite.case_results:
            status_label = "PASSED" if res.passed else "FAILED"
            print(
                f"[t306a] {res.case_id} ({res.source_record_id}) {status_label} | "
                f"mode={res.execution_mode} | "
                f"assertions={res.comparison.assertions_checked} | "
                f"diffs={len(res.comparison.diffs)} | "
                f"planC={res.plan_c_audit.passed} | "
                f"replay={res.replay_idempotency_passed} | "
                f"state={res.observed_snapshot.logical_state_sha256[:12]}"
            )
            if not res.passed:
                failures += 1
                if not res.comparison.matched:
                    print(res.comparison.format_report(), file=sys.stderr)
                if not res.plan_c_audit.passed:
                    print(
                        f"  [PLAN C FAILED] hits={res.plan_c_audit.canary_hits} "
                        f"jsonb_hits={res.plan_c_audit.jsonb_forbidden_key_hits} "
                        f"locations={res.plan_c_audit.leak_locations}",
                        file=sys.stderr,
                    )
                if res.replay_idempotency_checked and not res.replay_idempotency_passed:
                    print("  [REPLAY IDEMPOTENCY FAILED]", file=sys.stderr)

        if failures > 0 or not suite.all_passed:
            print(
                f"[t306a] FAILED: {suite.passed_count}/{suite.total_count} cases passed.",
                file=sys.stderr,
            )
            return 1

        actual_bundle = build_redacted_eval_bundle(suite, openapi_blob_sha=openapi_sha)

        if args.write_bundle:
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_text(
                json.dumps(actual_bundle, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            print(f"[t306a] WROTE redacted evaluation bundle: {bundle_path.relative_to(repo_root)}")

        if args.verify_bundle:
            if not bundle_path.is_file():
                print(
                    f"[t306a] FAILED: committed eval bundle missing at {bundle_path}",
                    file=sys.stderr,
                )
                return 1
            expected_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle_diffs = diff_deterministic_bundles(expected_bundle, actual_bundle)
            if bundle_diffs:
                print(
                    f"[t306a] FAILED: committed t306a_eval_bundle.v1.json drifted ({len(bundle_diffs)} diff(s)):",
                    file=sys.stderr,
                )
                for d in bundle_diffs[:20]:
                    print(f"  - {d}", file=sys.stderr)
                return 1
            print(
                "[t306a] VERIFIED: contracts/event-eval/t306a_eval_bundle.v1.json "
                "matches live PG18 21-case run with 0 drift."
            )

        print(
            f"[t306a] PASSED: {suite.passed_count}/{suite.total_count} cases passed across "
            f"{len(suite.bundle_summaries)} bundles, 0 oracle diffs, 0 Plan C violations."
        )
        return 0
    finally:
        cluster.stop()


if __name__ == "__main__":
    raise SystemExit(main())
