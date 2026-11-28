#!/usr/bin/env python3
"""Standalone T-505 Encrypted PostgreSQL 18 Backup/Restore & Fence Drill CLI (INF-10 / REL-01).

Runs a real ephemeral PostgreSQL 18 backup (`pg_dump -Fc`), AES-256-GCM encryption + tamper tests,
isolated restore (`pg_restore`) into a brand-new database, and full post-restore invariant checks:
  1. Repo A exact approval/version/identity/export/quota parity
  2. FORCE RLS & NOBYPASSRLS enforcement + cross-workspace isolation
  3. Repo B in-flight PARTIAL deletion fence survival + zero resurrection + clean completion
  4. Plan C audit across all 46 restored tables + encrypted backup metadata
  5. Measured RTO/RPO and honest Supabase Free (₩0, non-PITR) constraints

Usage:
  cd apps/api && uv run python ../../scripts/run_t505_backup_restore_drill.py --write-evidence
  cd apps/api && uv run python ../../scripts/run_t505_backup_restore_drill.py --verify
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

from career_memory_api.backup_restore import (  # noqa: E402
    normalize_evidence_for_drift_check,
    run_t505_backup_restore_drill,
)
from career_memory_api.conformance.ai_schemas import render_canonical_json  # noqa: E402
from career_memory_api.conformance.checks import M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Career Memory T-505 Encrypted PG18 Backup/Restore & Fence Drill CLI"
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
        help="Write redacted contracts/backup-restore/t505_restore_evidence.v1.json from this drill run.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run the live PG18 drill and assert 0 drift against committed t505_restore_evidence.v1.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    evidence_path = repo_root / "contracts" / "backup-restore" / "t505_restore_evidence.v1.json"

    report = run_t505_backup_restore_drill(repo_root=repo_root)
    if report.openapi_import_v1_blob_sha != M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1:
        print(
            f"[t505] FAILED: contracts/import-v1/openapi.yaml blob SHA drifted: "
            f"expected {M0_IMMUTABLE_OPENAPI_GIT_BLOB_SHA1}, got {report.openapi_import_v1_blob_sha}",
            file=sys.stderr,
        )
        return 1

    if not report.all_invariants_passed:
        print(
            "[t505] FAILED: one or more post-restore invariants did not pass:\n"
            + render_canonical_json(report.to_redacted_evidence()),
            file=sys.stderr,
        )
        return 1

    live_evidence = report.to_redacted_evidence()

    if args.write_evidence:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(render_canonical_json(live_evidence), encoding="utf-8")
        print(f"[t505] Wrote redacted restore evidence to {evidence_path.relative_to(repo_root)}")

    if args.verify:
        if not evidence_path.is_file():
            print(
                f"[t505] FAILED: committed evidence file missing at {evidence_path}",
                file=sys.stderr,
            )
            return 1
        committed = json.loads(evidence_path.read_text(encoding="utf-8"))
        committed_metrics = committed.get("measured_rto_metrics", {})
        if not committed_metrics.get("within_rto_budget"):
            print(
                "[t505] FAILED: committed evidence does not satisfy within_rto_budget=true.",
                file=sys.stderr,
            )
            return 1

        expected_norm = render_canonical_json(normalize_evidence_for_drift_check(committed))
        actual_norm = render_canonical_json(normalize_evidence_for_drift_check(live_evidence))
        if expected_norm != actual_norm:
            diff = "".join(
                difflib.unified_diff(
                    expected_norm.splitlines(keepends=True),
                    actual_norm.splitlines(keepends=True),
                    fromfile="committed/t505_restore_evidence.v1.json",
                    tofile="live/t505_restore_evidence.v1.json",
                )
            )
            print(
                "[t505] FAILED: committed t505_restore_evidence.v1.json drifted from live PG18 drill:\n"
                + diff,
                file=sys.stderr,
            )
            return 1

    print(
        f"[t505] PASSED (PG{report.pg_major_version}, {report.plan_c_audit['audited_table_count']} tables, "
        f"backup={report.backup_duration_ms}ms, restore={report.restore_duration_ms}ms, "
        f"total_rto={report.total_rto_ms}ms <= {report.rto_budget_ms}ms, "
        f"resurrected_rows=0, plan_c_violations=0)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
