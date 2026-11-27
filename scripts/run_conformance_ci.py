#!/usr/bin/env python3
"""Standalone M1 Conformance CI Gate CLI (T-106 + T-106A).

Runs deterministic drift and parity checks across:
1. Committed M0 immutable OpenAPI blob + M1 integrated OpenAPI + 6 AI JSON Schemas + MANIFEST
2. Python StrEnum <-> OpenAPI <-> AI JSON Schema <-> SQL (0010..0040) parity
3. Optional ``--with-postgres``: Real local PostgreSQL 18 Fresh (0010->0040) + Staged Upgrade
   idempotency & catalog snapshot verification (always stops and cleans up temporary cluster).

Never modifies committed documents or snapshots unless ``--write-snapshots`` is explicitly passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow execution directly from repo root or apps/api without editable install assumptions.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.conformance.checks import (  # noqa: E402
    run_all_conformance_checks,
    write_conformance_snapshots,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Career Memory M1 Conformance & Drift CI Gate (T-106 + T-106A)"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root directory (defaults to parent of scripts/).",
    )
    parser.add_argument(
        "--with-postgres",
        action="store_true",
        help="Also run Fresh + Staged Upgrade PostgreSQL 18 catalog snapshot conformance.",
    )
    parser.add_argument(
        "--write-snapshots",
        action="store_true",
        help="Explicitly regenerate committed OpenAPI, AI JSON Schema, and (with --with-postgres) DB snapshots.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.write_snapshots:
        written = write_conformance_snapshots(
            repo_root, with_postgres=args.with_postgres
        )
        print(f"[conformance] Regenerated {len(written)} snapshot artifact(s):")
        for path in written:
            print(f"  - {path.relative_to(repo_root)}")

    violations = run_all_conformance_checks(
        repo_root, with_postgres=args.with_postgres
    )
    if violations:
        print(
            f"[conformance] FAILED: {len(violations)} conformance violation(s) detected.",
            file=sys.stderr,
        )
        for idx, v in enumerate(violations, start=1):
            print(
                f"\n{idx}. [{v.axis}] {v.code} :: {v.target}\n"
                f"   Message:   {v.message}\n"
                f"   Authority: {v.authority}",
                file=sys.stderr,
            )
            if v.remediation:
                print(f"   Action:    {v.remediation}", file=sys.stderr)
            if v.diff:
                print("   Diff:", file=sys.stderr)
                for line in v.diff.splitlines():
                    print(f"     {line}", file=sys.stderr)
        return 1

    mode_label = "static + postgres-18 (fresh & staged upgrade)" if args.with_postgres else "static"
    print(f"[conformance] PASSED ({mode_label}): 0 drift/parity violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
