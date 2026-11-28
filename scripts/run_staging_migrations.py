#!/usr/bin/env python3
"""Staging PostgreSQL 18 migration runner, security auditor, and pilot user enrollment CLI (T-106B).

Connects ONLY via ``MIGRATION_DATABASE_URL`` (``:5432`` Session/Direct mode as ``cm_migrator``;
refuses ``:6543`` transaction pooler and refuses runtime roles).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from career_memory_api.hosted_runtime.migration_runner import (  # noqa: E402
    assert_safe_migration_dsn,
    enroll_pilot_user,
    run_post_migration_security_audit,
    run_staging_migrations,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Career Memory Staging Migration Runner & Pilot Enrollment CLI (T-106B)"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL :5432 migration DSN (defaults to MIGRATION_DATABASE_URL env var).",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=_REPO_ROOT / "db" / "migrations",
        help="Directory containing SQL migrations 0010..0080.",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=int,
        default=5,
        help="Bounded PostgreSQL lock_timeout in seconds (default: 5).",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Run the read-only post-migration RLS/role/Data-API security audit only.",
    )
    parser.add_argument(
        "--enroll-pilot-user",
        action="store_true",
        help="Idempotently enroll a closed-pilot owner in users/workspaces/workspace_memberships.",
    )
    parser.add_argument("--auth-issuer", default=None, help="Verified Supabase JWT issuer URL.")
    parser.add_argument("--auth-subject", default=None, help="Verified Supabase auth user UUID (sub).")
    parser.add_argument("--workspace-name", default=None, help="Closed-pilot workspace display name.")
    parser.add_argument("--email", default=None, help="Optional user email.")
    parser.add_argument("--display-name", default=None, help="Optional user display name.")
    parser.add_argument("--github-login", default=None, help="Optional verified GitHub username.")
    parser.add_argument("--github-external-id", default=None, help="Optional GitHub numeric user ID.")
    parser.add_argument(
        "--allow-insecure-local-test",
        action="store_true",
        help="Allow non-TLS connections strictly for ephemeral local PG18 testing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_url = (args.database_url or os.environ.get("MIGRATION_DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: --database-url or MIGRATION_DATABASE_URL is required.", file=sys.stderr)
        return 2

    if args.enroll_pilot_user:
        if not args.auth_issuer or not args.auth_subject or not args.workspace_name:
            print(
                "ERROR: --enroll-pilot-user requires --auth-issuer, --auth-subject, and --workspace-name.",
                file=sys.stderr,
            )
            return 2
        res = enroll_pilot_user(
            db_url,
            auth_issuer=args.auth_issuer,
            auth_subject=args.auth_subject,
            workspace_name=args.workspace_name,
            email=args.email,
            display_name=args.display_name,
            github_login=args.github_login,
            github_external_id=args.github_external_id,
            allow_insecure_local_test=bool(args.allow_insecure_local_test),
        )
        print(json.dumps(res.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.audit_only:
        assert_safe_migration_dsn(
            db_url,
            allow_insecure_local_test=bool(args.allow_insecure_local_test),
        )
        with psycopg.connect(db_url, autocommit=True, connect_timeout=10) as conn:
            audit = run_post_migration_security_audit(conn)
        print(json.dumps(audit.to_dict(), indent=2, sort_keys=True))
        return 0 if audit.passed else 1

    report = run_staging_migrations(
        db_url,
        migrations_dir=args.migrations_dir.resolve(),
        lock_timeout_seconds=args.lock_timeout_seconds,
        allow_insecure_local_test=bool(args.allow_insecure_local_test),
    )
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
