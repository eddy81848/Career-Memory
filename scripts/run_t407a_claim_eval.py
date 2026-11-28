#!/usr/bin/env python3
"""Deterministic CLI Verifier for M4 Wave 4 T-407A Grounded Claim & Refetch Evaluation.

Runs the frozen 0.4.0-synthetic-golden dataset (16 scenarios, 29 claims, 50 atoms, 27 refetch cases)
against an ephemeral local PostgreSQL 18 cluster (migrations 0010..0070, NOBYPASSRLS roles) and
the real M4 FastAPI + service stack.

Strict governance invariants:
- Single entrypoint: evaluation/claim-draft/GOLDEN_APPROVAL_MANIFEST_v0.4.0.json ONLY.
- Zero oracle auto-overwrite flags (frozen fixtures are strictly read-only).
- Zero live external LLM or network calls (OD-002 / OD-014 hard OFF).
- Multi-surface Plan C (ADR-013 / REF-04) zero-raw-persistence audit.
- Immutable contracts/import-v1/openapi.yaml Git blob SHA verification.

Usage:
    cd apps/api && uv run python ../../scripts/run_t407a_claim_eval.py --write-bundle
    cd apps/api && uv run python ../../scripts/run_t407a_claim_eval.py --verify-bundle
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from career_memory_api.claim_eval import (
    IMMUTABLE_OPENAPI_BLOB_SHA,
    build_redacted_claim_eval_bundle,
    default_repo_root,
    diff_deterministic_bundles,
    run_frozen_t407a_evaluation,
)

DEFAULT_BUNDLE_REL = Path("contracts/claim-eval/t407a_eval_bundle.v1.json")


def _compute_git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324


def _verify_immutable_boundaries(repo_root: Path) -> str:
    openapi_path = repo_root / "contracts/import-v1/openapi.yaml"
    blob_sha = _compute_git_blob_sha(openapi_path)
    if blob_sha != IMMUTABLE_OPENAPI_BLOB_SHA:
        raise RuntimeError(
            f"Immutable contracts/import-v1/openapi.yaml blob SHA mismatch: "
            f"actual={blob_sha} expected={IMMUTABLE_OPENAPI_BLOB_SHA}"
        )

    for frozen_dir in ("evaluation/claim-draft", "evaluation/event-draft"):
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", frozen_dir],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raise RuntimeError(
                f"Frozen dataset directory {frozen_dir!r} has uncommitted modifications:\n"
                f"{proc.stdout.strip()}"
            )
    return blob_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run frozen 0.4.0-synthetic-golden T-407A Claim & Refetch Evaluation on PG18 "
            "(read-only oracles; zero live LLM calls)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write-bundle",
        action="store_true",
        help="Write the redacted evaluation bundle to contracts/claim-eval/t407a_eval_bundle.v1.json.",
    )
    mode.add_argument(
        "--verify-bundle",
        action="store_true",
        help="Verify zero deterministic drift against contracts/claim-eval/t407a_eval_bundle.v1.json.",
    )
    parser.add_argument(
        "--bundle-path",
        type=Path,
        default=None,
        help="Optional custom path for the redacted evaluation bundle JSON.",
    )
    args = parser.parse_args(argv)

    repo_root = default_repo_root()
    bundle_path = (args.bundle_path or (repo_root / DEFAULT_BUNDLE_REL)).resolve()

    openapi_blob_sha = _verify_immutable_boundaries(repo_root)
    dataset, suite = run_frozen_t407a_evaluation(
        repo_root=repo_root,
        run_subprocess_validators=True,
    )
    bundle = build_redacted_claim_eval_bundle(
        suite,
        openapi_blob_sha=openapi_blob_sha,
        canary_tokens=dataset.canary_tokens,
        raw_source_texts=list(dataset.raw_source_texts_by_rev.values()),
    )

    print(
        f"[T-407A] dataset={suite.dataset_version} "
        f"scenarios={suite.passed_scenarios}/{suite.total_scenarios} "
        f"claims={suite.passed_claims}/{suite.total_claims} "
        f"atoms={suite.passed_atoms}/{suite.total_atoms} "
        f"refetch={suite.passed_refetch_cases}/{suite.total_refetch_cases} "
        f"plan_c_leaks={suite.total_plan_c_leaks} "
        f"external_live_llm_calls={suite.external_live_llm_calls}"
    )
    print(
        f"[T-407A] SYNTHETIC_CONTRACT_AND_SAFETY_GATE="
        f"{bundle['governance_gates']['SYNTHETIC_CONTRACT_AND_SAFETY_GATE']} | "
        f"LIVE_LLM_SEMANTIC_EVAL="
        f"{bundle['governance_gates']['LIVE_LLM_SEMANTIC_EVAL']}"
    )

    if not suite.all_passed:
        print("[T-407A] FAILURES DETECTED:", file=sys.stderr)
        for s in suite.scenario_results:
            if not s.passed:
                print(
                    f"  Scenario {s.scenario_id}: diffs={[d.to_dict() for d in s.diffs]} "
                    f"plan_c_leaks={s.plan_c_audit.leak_locations}",
                    file=sys.stderr,
                )
                for f in s.fact_results:
                    if not f.matched:
                        print(f"    Fact {f.fact_candidate_id}: {[d.to_dict() for d in f.diffs]}", file=sys.stderr)
                for c in s.claim_results:
                    if not c.matched:
                        print(f"    Claim {c.claim_id}: {[d.to_dict() for d in c.diffs]}", file=sys.stderr)
                        for a in c.atom_results:
                            if not a.matched:
                                print(f"      Atom {a.claim_atom_id}: {[d.to_dict() for d in a.diffs]}", file=sys.stderr)
        for r in suite.refetch_results:
            if not r.passed:
                print(
                    f"  Refetch {r.fixture_case_id} ({r.official_ref_id}): "
                    f"diffs={[d.to_dict() for d in r.diffs]} "
                    f"plan_c_leaks={r.plan_c_audit.leak_locations}",
                    file=sys.stderr,
                )
        return 1

    if args.write_bundle:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            f"[T-407A] Wrote redacted bundle -> {bundle_path} "
            f"(sha={bundle['deterministic_bundle_sha256']})"
        )
        return 0

    if args.verify_bundle:
        if not bundle_path.is_file():
            print(f"[T-407A] ERROR: Expected bundle not found: {bundle_path}", file=sys.stderr)
            return 1
        expected = json.loads(bundle_path.read_text(encoding="utf-8"))
        diffs = diff_deterministic_bundles(expected, bundle)
        if diffs:
            print(f"[T-407A] BUNDLE DRIFT DETECTED ({len(diffs)} diffs):", file=sys.stderr)
            for line in diffs[:40]:
                print(f"  - {line}", file=sys.stderr)
            return 1
        print(
            f"[T-407A] ZERO-DRIFT BUNDLE VERIFIED: {bundle_path} "
            f"(sha={bundle['deterministic_bundle_sha256']})"
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
