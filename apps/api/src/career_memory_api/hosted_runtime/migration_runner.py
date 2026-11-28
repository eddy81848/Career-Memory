"""CI/Operator migration runner, post-migration security audit, and pilot enrollment (T-106B / OD-006).

Key invariants:
- Connects ONLY via ``MIGRATION_DATABASE_URL`` (``:5432`` Session/Direct mode; refuses ``:6543``
  transaction pooler and refuses runtime roles ``cm_api_app`` / ``cm_worker_app``).
- Acquires a session-level PostgreSQL advisory lock + bounded ``lock_timeout = '5s'``.
- Tracks applied migrations (``0010..0080``) with SHA-256 checksums in
  ``auth_ctx.schema_migration_ledger`` (in ``auth_ctx`` schema, NOT ``public``, so ``public``
  remains at 46 tables with ZERO drift against ``expected_db_catalog_snapshot.json``!).
- Revokes all ``public`` schema table/sequence/function privileges from Supabase ``anon`` and
  ``authenticated`` roles when those roles exist (OD006-5 Data API bypass prevention).
- Immediately runs a post-migration security audit verifying 46/46 FORCE RLS, NOBYPASSRLS runtime
  roles owning 0 tables, and 0 ``anon``/``authenticated`` grants on ``public`` tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import psycopg

from career_memory_api.conformance.db_catalog import FULL_M1_MIGRATIONS
from career_memory_api.hosted_runtime.models import (
    ALLOWED_TLS_SSLMODES,
    FORBIDDEN_MIGRATION_DB_ROLES,
    HostedConfigurationError,
    HostedRoleSecurityError,
    extract_base_role_name,
    extract_dsn_username,
)

# Stable 64-bit signed advisory lock key ('CMMIGR01')
MIGRATION_ADVISORY_LOCK_KEY = 0x434D_4D49_4752_3031
EXPECTED_PUBLIC_TABLE_COUNT = 46


class MigrationLockError(RuntimeError):
    """Raised when another migration deployment currently holds the advisory lock."""


class MigrationChecksumDriftError(RuntimeError):
    """Raised when a previously applied migration file checksum no longer matches the ledger."""


class PostMigrationAuditError(RuntimeError):
    """Raised when the post-migration security audit detects RLS, role, or Data API drift."""


@dataclass(frozen=True, slots=True)
class PostMigrationSecurityAudit:
    """Result of the mandatory post-migration security audit."""

    passed: bool
    public_table_count: int
    tables_missing_force_rls: tuple[str, ...]
    insecure_runtime_roles: tuple[str, ...]
    runtime_owned_tables: tuple[str, ...]
    supabase_data_api_leaks: tuple[str, ...]
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "public_table_count": self.public_table_count,
            "tables_missing_force_rls": list(self.tables_missing_force_rls),
            "insecure_runtime_roles": list(self.insecure_runtime_roles),
            "runtime_owned_tables": list(self.runtime_owned_tables),
            "supabase_data_api_leaks": list(self.supabase_data_api_leaks),
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class MigrationRunReport:
    """Structured report emitted by ``run_staging_migrations``."""

    applied_migrations: tuple[str, ...]
    skipped_already_applied: tuple[str, ...]
    checksums: dict[str, str]
    revoked_supabase_data_api_roles: tuple[str, ...]
    audit: PostMigrationSecurityAudit

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied_migrations": list(self.applied_migrations),
            "skipped_already_applied": list(self.skipped_already_applied),
            "checksums": dict(self.checksums),
            "revoked_supabase_data_api_roles": list(self.revoked_supabase_data_api_roles),
            "audit": self.audit.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PilotEnrollmentResult:
    """Result of idempotently enrolling a closed-pilot user and workspace via ``cm_migrator``."""

    user_id: str
    workspace_id: str
    workspace_name: str
    auth_issuer: str
    auth_subject: str
    role: str
    user_created: bool
    workspace_created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "auth_issuer": self.auth_issuer,
            "auth_subject": self.auth_subject,
            "role": self.role,
            "user_created": self.user_created,
            "workspace_created": self.workspace_created,
        }


def assert_safe_migration_dsn(
    database_url: str,
    *,
    allow_transaction_pooler_for_tests: bool = False,
    allow_insecure_local_test: bool = False,
) -> None:
    """Ensure migrations run via :5432 Session/Direct mode and NEVER as a runtime role."""
    if not isinstance(database_url, str) or not database_url.strip():
        raise HostedConfigurationError("MIGRATION_DATABASE_URL must be a non-empty string.")

    username = extract_dsn_username(database_url)
    base_role = extract_base_role_name(username)
    if (
        username.lower() in FORBIDDEN_MIGRATION_DB_ROLES
        or base_role in FORBIDDEN_MIGRATION_DB_ROLES
    ):
        raise HostedRoleSecurityError(
            f"Migration runner refuses runtime/unprivileged role {username!r} (OD-006-6)."
        )

    parsed = urlsplit(database_url.strip())
    if parsed.port == 6543 and not allow_transaction_pooler_for_tests:
        raise HostedConfigurationError(
            "Migration runner refuses Supavisor Transaction Pooler (:6543); "
            "use Session Pooler (:5432) or Direct connection (OD-006)."
        )

    if not allow_insecure_local_test:
        query_params = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
        sslmodes = [m.strip().lower() for m in query_params.get("sslmode", []) if m.strip()]
        if not sslmodes or sslmodes[-1] not in ALLOWED_TLS_SSLMODES:
            raise HostedConfigurationError(
                "MIGRATION_DATABASE_URL requires TLS sslmode in {'require', 'verify-ca', 'verify-full'}."
            )


def _compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_ledger_table(conn: psycopg.Connection) -> None:
    """Ensure ``auth_ctx.schema_migration_ledger`` exists (outside ``public`` for 0 catalog drift)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_ctx.schema_migration_ledger (
                migration_name text PRIMARY KEY,
                sha256_checksum text NOT NULL CHECK (sha256_checksum ~ '^[0-9a-f]{64}$'),
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            REVOKE ALL ON TABLE auth_ctx.schema_migration_ledger
                FROM PUBLIC, cm_api_app, cm_worker_app;
            """
        )


def _revoke_supabase_data_api_grants(conn: psycopg.Connection) -> tuple[str, ...]:
    """Revoke all ``public`` privileges from ``anon``/``authenticated`` when present (OD006-5)."""
    revoked_roles: list[str] = []
    with conn.cursor() as cur:
        for role_name in ("anon", "authenticated"):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
            if cur.fetchone() is not None:
                cur.execute(
                    f"""
                    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name};
                    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role_name};
                    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role_name};
                    """
                )
                revoked_roles.append(role_name)
    return tuple(revoked_roles)


def run_post_migration_security_audit(
    conn: psycopg.Connection,
    *,
    expected_public_table_count: int = EXPECTED_PUBLIC_TABLE_COUNT,
) -> PostMigrationSecurityAudit:
    """Verify 46/46 FORCE RLS, NOBYPASSRLS runtime roles owning 0 tables, and 0 Data API grants."""
    findings: list[str] = []
    with conn.cursor() as cur:
        # 1. All public tables have ENABLE + FORCE RLS
        cur.execute(
            """
            SELECT
                t.tablename,
                t.tableowner,
                c.relrowsecurity,
                c.relforcerowsecurity
            FROM pg_tables t
            JOIN pg_namespace n ON n.nspname = t.schemaname
            JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = t.tablename
            WHERE t.schemaname = 'public'
            ORDER BY t.tablename ASC
            """
        )
        rows = cur.fetchall()
        public_table_count = len(rows)
        missing_force_rls: list[str] = []
        runtime_owned_tables: list[str] = []

        for tablename, owner, rls, force_rls in rows:
            if not bool(rls) or not bool(force_rls):
                missing_force_rls.append(str(tablename))
            if str(owner) in ("cm_api_app", "cm_worker_app"):
                runtime_owned_tables.append(f"{tablename} (owner={owner})")

        if public_table_count != expected_public_table_count:
            findings.append(
                f"Expected {expected_public_table_count} public tables, found {public_table_count}."
            )
        if missing_force_rls:
            findings.append(
                f"Public tables missing ENABLE+FORCE RLS: {', '.join(missing_force_rls)}."
            )
        if runtime_owned_tables:
            findings.append(
                f"Runtime roles must own 0 public tables; found: {', '.join(runtime_owned_tables)}."
            )

        # 2. cm_api_app & cm_worker_app must be NOSUPERUSER + NOBYPASSRLS
        cur.execute(
            """
            SELECT rolname, rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname IN ('cm_api_app', 'cm_worker_app')
            ORDER BY rolname ASC
            """
        )
        role_rows = {str(r[0]): (bool(r[1]), bool(r[2])) for r in cur.fetchall()}
        insecure_roles: list[str] = []
        for req_role in ("cm_api_app", "cm_worker_app"):
            if req_role not in role_rows:
                insecure_roles.append(f"{req_role}:missing")
            else:
                rolsuper, rolbypassrls = role_rows[req_role]
                if rolsuper or rolbypassrls:
                    insecure_roles.append(
                        f"{req_role}(rolsuper={rolsuper},rolbypassrls={rolbypassrls})"
                    )
        if insecure_roles:
            findings.append(
                f"Insecure or missing runtime DB roles: {', '.join(insecure_roles)}."
            )

        # 3. anon & authenticated (if present on Supabase) must have 0 privileges on public tables
        cur.execute(
            """
            SELECT grantee, table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_schema = 'public'
              AND grantee IN ('anon', 'authenticated')
            ORDER BY grantee, table_name, privilege_type
            """
        )
        data_api_leaks = [
            f"{r[0]}:{r[1]}:{r[2]}" for r in cur.fetchall()
        ]
        if data_api_leaks:
            findings.append(
                f"Supabase Data API roles retain grants on public tables: {', '.join(data_api_leaks[:10])}."
            )

    return PostMigrationSecurityAudit(
        passed=len(findings) == 0,
        public_table_count=public_table_count,
        tables_missing_force_rls=tuple(missing_force_rls),
        insecure_runtime_roles=tuple(insecure_roles),
        runtime_owned_tables=tuple(runtime_owned_tables),
        supabase_data_api_leaks=tuple(data_api_leaks),
        findings=tuple(findings),
    )


def run_staging_migrations(
    database_url: str,
    *,
    migrations_dir: Path,
    lock_timeout_seconds: int = 5,
    allow_transaction_pooler_for_tests: bool = False,
    allow_insecure_local_test: bool = False,
) -> MigrationRunReport:
    """Apply migrations ``0010..0080`` idempotently under an advisory lock + security audit."""
    assert_safe_migration_dsn(
        database_url,
        allow_transaction_pooler_for_tests=allow_transaction_pooler_for_tests,
        allow_insecure_local_test=allow_insecure_local_test,
    )

    migration_files = [(name, migrations_dir / name) for name in FULL_M1_MIGRATIONS]
    for name, path in migration_files:
        if not path.is_file():
            raise FileNotFoundError(f"Required migration file missing: {path} ({name})")

    applied: list[str] = []
    skipped: list[str] = []
    checksums: dict[str, str] = {}

    with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
        locked = False
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET lock_timeout = '{max(1, int(lock_timeout_seconds))}s'")
                cur.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
                locked = bool((cur.fetchone() or (False,))[0])
                if not locked:
                    raise MigrationLockError(
                        "Another deployment holds the Career Memory migration advisory lock."
                    )

            # Ensure managed non-superuser CREATEROLE accounts (e.g. Supabase PostgreSQL 17 'postgres')
            # hold membership in cm_migrator before 0010 runs AUTHORIZATION / SET ROLE cm_migrator.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cm_migrator') THEN
                            CREATE ROLE cm_migrator
                                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                        END IF;
                        EXECUTE format('GRANT cm_migrator TO %I', current_user);
                    END
                    $$;
                    """
                )

            # Check whether auth_ctx.schema_migration_ledger already exists
            recorded: dict[str, str] = {}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('auth_ctx.schema_migration_ledger') IS NOT NULL"
                )
                ledger_exists = bool((cur.fetchone() or (False,))[0])
                if ledger_exists:
                    cur.execute(
                        "SELECT migration_name, sha256_checksum FROM auth_ctx.schema_migration_ledger"
                    )
                    recorded = {str(r[0]): str(r[1]) for r in cur.fetchall()}

            for name, path in migration_files:
                sql = path.read_text(encoding="utf-8")
                digest = _compute_sha256(sql)
                checksums[name] = digest

                if name in recorded:
                    if recorded[name] != digest:
                        raise MigrationChecksumDriftError(
                            f"Migration {name!r} checksum mismatch: recorded={recorded[name]} != current={digest}."
                        )
                    skipped.append(name)
                    continue

                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute("RESET ROLE")

                # 0010 creates auth_ctx schema; ensure ledger exists immediately after first migration
                _ensure_ledger_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO auth_ctx.schema_migration_ledger (migration_name, sha256_checksum)
                        VALUES (%s, %s)
                        ON CONFLICT (migration_name) DO NOTHING
                        """,
                        (name, digest),
                    )
                applied.append(name)

            # Ensure ledger exists even on pure re-apply runs
            _ensure_ledger_table(conn)

            # Revoke Supabase Data API roles (anon/authenticated) if present (OD006-5)
            revoked_roles = _revoke_supabase_data_api_grants(conn)

            # Mandatory post-migration security audit
            audit = run_post_migration_security_audit(conn)
            if not audit.passed:
                raise PostMigrationAuditError(
                    "Post-migration security audit FAILED: " + "; ".join(audit.findings)
                )

            return MigrationRunReport(
                applied_migrations=tuple(applied),
                skipped_already_applied=tuple(skipped),
                checksums=checksums,
                revoked_supabase_data_api_roles=revoked_roles,
                audit=audit,
            )
        finally:
            if locked and not conn.closed:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,)
                        )
                except Exception:  # noqa: BLE001
                    pass


def enroll_pilot_user(
    database_url: str,
    *,
    auth_issuer: str,
    auth_subject: str,
    workspace_name: str,
    email: str | None = None,
    display_name: str | None = None,
    github_login: str | None = None,
    github_external_id: str | None = None,
    allow_transaction_pooler_for_tests: bool = False,
    allow_insecure_local_test: bool = False,
) -> PilotEnrollmentResult:
    """Idempotently enroll a closed-pilot owner in users/workspaces/workspace_memberships."""
    assert_safe_migration_dsn(
        database_url,
        allow_transaction_pooler_for_tests=allow_transaction_pooler_for_tests,
        allow_insecure_local_test=allow_insecure_local_test,
    )
    clean_issuer = auth_issuer.strip()
    clean_subject = auth_subject.strip()
    clean_ws_name = workspace_name.strip()
    if not clean_issuer or not clean_subject or not clean_ws_name:
        raise HostedConfigurationError(
            "auth_issuer, auth_subject, and workspace_name must all be non-empty."
        )

    now = datetime.now(UTC)
    user_created = False
    workspace_created = False

    with psycopg.connect(database_url, autocommit=False, connect_timeout=10) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text
                    FROM public.users
                    WHERE auth_issuer = %s AND auth_subject = %s
                    """,
                    (clean_issuer, clean_subject),
                )
                row = cur.fetchone()
                if row is not None:
                    user_id = str(row[0])
                else:
                    user_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                        VALUES (%s::uuid, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            clean_issuer,
                            clean_subject,
                            email.strip() if email else None,
                            display_name.strip() if display_name else None,
                        ),
                    )
                    user_created = True

                cur.execute(
                    """
                    SELECT id::text, name
                    FROM public.workspaces
                    WHERE owner_user_id = %s::uuid
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                ws_row = cur.fetchone()
                if ws_row is not None:
                    ws_id = str(ws_row[0])
                    resolved_ws_name = str(ws_row[1])
                else:
                    ws_id = str(uuid.uuid4())
                    resolved_ws_name = clean_ws_name
                    cur.execute(
                        """
                        INSERT INTO public.workspaces (id, owner_user_id, name)
                        VALUES (%s::uuid, %s::uuid, %s)
                        """,
                        (ws_id, user_id, resolved_ws_name),
                    )
                    workspace_created = True

                cur.execute(
                    """
                    INSERT INTO public.workspace_memberships (workspace_id, user_id, role, revoked_at)
                    VALUES (%s::uuid, %s::uuid, 'OWNER', NULL)
                    ON CONFLICT (workspace_id, user_id)
                    DO UPDATE SET role = 'OWNER', revoked_at = NULL
                    """,
                    (ws_id, user_id),
                )

                if github_login and github_login.strip():
                    gh_login = github_login.strip()
                    gh_ext_id = (github_external_id or gh_login).strip()
                    cur.execute(
                        """
                        SELECT id::text
                        FROM public.identities
                        WHERE workspace_id = %s::uuid
                          AND canonical_user_id = %s::uuid
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (ws_id, user_id),
                    )
                    id_row = cur.fetchone()
                    if id_row is not None:
                        ident_id = str(id_row[0])
                    else:
                        ident_id = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO public.identities (
                                id, workspace_id, display_name, identity_type,
                                canonical_user_id, status
                            )
                            VALUES (%s::uuid, %s::uuid, %s, 'PERSON', %s::uuid, 'ACTIVE')
                            """,
                            (
                                ident_id,
                                ws_id,
                                (display_name or gh_login).strip(),
                                user_id,
                            ),
                        )
                    cur.execute(
                        """
                        SELECT 1
                        FROM public.external_identity_mappings
                        WHERE workspace_id = %s::uuid
                          AND provider = 'GITHUB'
                          AND external_user_id = %s
                          AND status = 'VERIFIED'
                        """,
                        (ws_id, gh_ext_id),
                    )
                    if cur.fetchone() is None:
                        cur.execute(
                            """
                            INSERT INTO public.external_identity_mappings (
                                id, workspace_id, identity_id, provider, external_user_id,
                                username, verification_method, status, verified_at
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, 'GITHUB', %s,
                                %s, 'OAUTH_ACCOUNT', 'VERIFIED', %s
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_id,
                                ident_id,
                                gh_ext_id,
                                gh_login,
                                now,
                            ),
                        )

    return PilotEnrollmentResult(
        user_id=user_id,
        workspace_id=ws_id,
        workspace_name=resolved_ws_name,
        auth_issuer=clean_issuer,
        auth_subject=clean_subject,
        role="OWNER",
        user_created=user_created,
        workspace_created=workspace_created,
    )


def build_runtime_pooler_dsn(
    migration_database_url: str,
    *,
    base_login_role: str,
    password: str,
    target_port: int | None = 6543,
) -> str:
    """Derive a Supavisor :6543 Transaction Pooler DSN preserving '.<project_ref>' suffixes."""
    parsed = urlsplit(migration_database_url.strip())
    mig_user = (parsed.username or "").strip()
    project_suffix = ""
    if "." in mig_user and (parsed.hostname or "").lower().endswith(".pooler.supabase.com"):
        project_suffix = "." + mig_user.split(".", 1)[1]

    runtime_user = f"{base_login_role}{project_suffix}"
    encoded_user = quote(runtime_user, safe=".")
    encoded_pw = quote(password, safe="")
    host = parsed.hostname or "127.0.0.1"
    effective_port = target_port if target_port is not None else parsed.port
    port_part = f":{effective_port}" if effective_port else ""
    netloc = f"{encoded_user}:{encoded_pw}@{host}{port_part}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _sync_single_gcp_secret_version(
    *,
    gcp_project_id: str,
    secret_name: str,
    secret_value: str,
) -> dict[str, Any]:
    """Add a new Secret Manager version via stdin and destroy prior enabled versions (<= 4 cap)."""
    env = os.environ.copy()
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    existing_metric = env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
    env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
        f"{existing_metric} datacloud.antigravity" if existing_metric else "datacloud.antigravity"
    )

    prior_raw = subprocess.check_output(
        [
            "gcloud",
            "--quiet",
            "secrets",
            "versions",
            "list",
            secret_name,
            "--filter=state=ENABLED",
            f"--project={gcp_project_id}",
            "--format=json",
        ],
        text=True,
        env=env,
    ).strip()
    prior_items = json.loads(prior_raw) if prior_raw else []
    prior_ids = [
        str(item.get("name", "")).rsplit("/", 1)[-1]
        for item in (prior_items if isinstance(prior_items, list) else [])
        if str(item.get("name", "")).rsplit("/", 1)[-1]
    ]

    add_out = subprocess.check_output(
        [
            "gcloud",
            "--quiet",
            "secrets",
            "versions",
            "add",
            secret_name,
            "--data-file=-",
            f"--project={gcp_project_id}",
            "--format=json",
        ],
        input=secret_value,
        text=True,
        env=env,
    ).strip()
    added_obj = json.loads(add_out) if add_out else {}
    new_version_id = str(added_obj.get("name", "")).rsplit("/", 1)[-1]

    destroyed_ids: list[str] = []
    for old_id in prior_ids:
        if old_id and old_id != new_version_id:
            subprocess.run(
                [
                    "gcloud",
                    "--quiet",
                    "secrets",
                    "versions",
                    "destroy",
                    old_id,
                    f"--secret={secret_name}",
                    f"--project={gcp_project_id}",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            destroyed_ids.append(old_id)

    return {
        "secret_name": secret_name,
        "active_version": new_version_id,
        "destroyed_prior_versions": destroyed_ids,
    }


def provision_and_sync_runtime_db_secrets(
    migration_database_url: str,
    *,
    gcp_project_id: str | None = None,
    api_login_role: str = "cm_staging_api_login",
    worker_login_role: str = "cm_staging_worker_login",
    target_pooler_port: int | None = None,
    allow_insecure_local_test: bool = False,
) -> dict[str, Any]:
    """Create/rotate least-privilege runtime LOGIN roles, verify :6543 pooler, & sync Secret Manager."""
    assert_safe_migration_dsn(
        migration_database_url,
        allow_insecure_local_test=allow_insecure_local_test,
    )
    api_pw = secrets.token_hex(24)
    worker_pw = secrets.token_hex(24)

    with psycopg.connect(migration_database_url, autocommit=True, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for login_role, pw, group_role in (
                (api_login_role, api_pw, "cm_api_app"),
                (worker_login_role, worker_pw, "cm_worker_app"),
            ):
                if not login_role.replace("_", "").isalnum():
                    raise HostedConfigurationError(f"Invalid role identifier: {login_role!r}")
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login_role,))
                if cur.fetchone() is None:
                    cur.execute(
                        f"CREATE ROLE {login_role} LOGIN PASSWORD '{pw}' "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS"
                    )
                else:
                    cur.execute(
                        f"ALTER ROLE {login_role} WITH LOGIN PASSWORD '{pw}' "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS"
                    )
                cur.execute(f"GRANT {group_role} TO {login_role}")

    parsed = urlsplit(migration_database_url.strip())
    is_supabase_pooler = (parsed.hostname or "").lower().endswith(".pooler.supabase.com")
    effective_port = (
        target_pooler_port
        if target_pooler_port is not None
        else (6543 if is_supabase_pooler else parsed.port)
    )

    api_dsn = build_runtime_pooler_dsn(
        migration_database_url,
        base_login_role=api_login_role,
        password=api_pw,
        target_port=effective_port,
    )
    worker_dsn = build_runtime_pooler_dsn(
        migration_database_url,
        base_login_role=worker_login_role,
        password=worker_pw,
        target_port=effective_port,
    )

    # Verify live Transaction Pooler (:6543) connectivity, prepare_threshold=None, and fail-closed RLS!
    pooler_checks: list[dict[str, Any]] = []
    for label, dsn in (("api_pooler", api_dsn), ("worker_pooler", worker_dsn)):
        with psycopg.connect(dsn, autocommit=False, prepare_threshold=None, connect_timeout=10) as rconn:
            with rconn.transaction():
                with rconn.cursor() as rcur:
                    rcur.execute("SELECT count(*) FROM public.work_events")
                    unscoped_rows = int((rcur.fetchone() or (0,))[0])
                    rcur.execute("SELECT count(*) FROM pg_prepared_statements")
                    prepared_cnt = int((rcur.fetchone() or (0,))[0])
            pooler_checks.append(
                {
                    "role_check": label,
                    "port": effective_port,
                    "unscoped_work_events_rows": unscoped_rows,
                    "prepared_statements_count": prepared_cnt,
                    "passed": unscoped_rows == 0 and prepared_cnt == 0,
                }
            )
            if unscoped_rows != 0 or prepared_cnt != 0:
                raise PostMigrationAuditError(
                    f"Runtime pooler check {label} failed: unscoped={unscoped_rows}, prepared={prepared_cnt}"
                )

    synced_secrets: list[dict[str, Any]] = []
    if gcp_project_id and gcp_project_id.strip():
        proj = gcp_project_id.strip()
        synced_secrets.append(
            _sync_single_gcp_secret_version(
                gcp_project_id=proj,
                secret_name="cm-api-database-url",
                secret_value=api_dsn,
            )
        )
        synced_secrets.append(
            _sync_single_gcp_secret_version(
                gcp_project_id=proj,
                secret_name="cm-worker-database-url",
                secret_value=worker_dsn,
            )
        )

    return {
        "api_login_role": api_login_role,
        "worker_login_role": worker_login_role,
        "runtime_pooler_port": effective_port,
        "pooler_verification": pooler_checks,
        "gcp_secret_manager_synced": synced_secrets,
    }


__all__ = [
    "EXPECTED_PUBLIC_TABLE_COUNT",
    "MIGRATION_ADVISORY_LOCK_KEY",
    "MigrationChecksumDriftError",
    "MigrationLockError",
    "MigrationRunReport",
    "PilotEnrollmentResult",
    "PostMigrationAuditError",
    "PostMigrationSecurityAudit",
    "assert_safe_migration_dsn",
    "build_runtime_pooler_dsn",
    "enroll_pilot_user",
    "provision_and_sync_runtime_db_secrets",
    "run_post_migration_security_audit",
    "run_staging_migrations",
]
