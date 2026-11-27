"""Ephemeral local PostgreSQL 18 test harness for M1-T105 (T-105 + T-105A)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_0010 = REPO_ROOT / "db" / "migrations" / "0010_identity_workspace_source_auth.sql"
MIGRATION_0040 = REPO_ROOT / "db" / "migrations" / "0040_sync_runs_jobs_outbox_audit.sql"


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pg_bin(name: str) -> str:
    candidate = _PG18_BIN_DIR / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        pytest.fail(
            f"PostgreSQL 18 binary {name!r} not found in {_PG18_BIN_DIR} or PATH."
        )
    return resolved


class EphemeralPg18Cluster:
    """Disposable local PostgreSQL 18 cluster bound strictly to 127.0.0.1."""

    def __init__(self, *, prefix: str = "cm-m1-t105-pg18-") -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_pg_admin"
        self.dbname = "career_memory_t105_test"
        self.api_login = "cm_api_login_t105"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_t105"
        self.worker_password = "local_ephemeral_worker_pw"

    @property
    def admin_dsn(self) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{self.dbname}"

    @property
    def api_dsn(self) -> str:
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @property
    def worker_dsn(self) -> str:
        return (
            f"postgresql://{self.worker_login}:{self.worker_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    @contextmanager
    def api_connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.api_dsn) as conn:
            yield conn

    @contextmanager
    def worker_connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.worker_dsn) as conn:
            yield conn

    @contextmanager
    def admin_connection(self, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.admin_dsn, autocommit=autocommit) as conn:
            yield conn

    def start(self) -> None:
        initdb = _pg_bin("initdb")
        pg_ctl = _pg_bin("pg_ctl")
        createdb = _pg_bin("createdb")

        env = os.environ.copy()
        env["LC_ALL"] = "C"

        subprocess.run(
            [
                initdb,
                "-D",
                str(self._pgdata),
                "-U",
                self.superuser,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        with (self._pgdata / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                f"\nlisten_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self._tmpdir}'\n"
                f"fsync = off\n"
                f"synchronous_commit = off\n"
                f"max_connections = 100\n"
            )

        subprocess.run(
            [
                pg_ctl,
                "-D",
                str(self._pgdata),
                "-l",
                str(self._logfile),
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        subprocess.run(
            [
                createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                self.dbname,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        row = cur.fetchone()
                        assert row is not None and int(row[0]) >= 180000
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PG18 cluster did not become ready.")

    def apply_sql_file(self, path: Path) -> None:
        sql = path.read_text(encoding="utf-8")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def provision_runtime_logins(self) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.api_login}') THEN
                            CREATE ROLE {self.api_login}
                                LOGIN PASSWORD '{self.api_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.worker_login}') THEN
                            CREATE ROLE {self.worker_login}
                                LOGIN PASSWORD '{self.worker_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                    END
                    $$;
                    GRANT cm_api_app TO {self.api_login};
                    GRANT cm_worker_app TO {self.worker_login};
                    """
                )

    def truncate_all(self) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    TRUNCATE TABLE
                        public.outbox_consumed_effects,
                        public.job_side_effect_ledger,
                        public.audit_events,
                        public.outbox_events,
                        public.api_idempotency_records,
                        public.deletion_jobs,
                        public.jobs,
                        public.sync_checkpoints,
                        public.sync_runs,
                        public.source_resources,
                        public.source_accounts,
                        public.workspace_memberships,
                        public.workspaces,
                        public.users
                    CASCADE
                    """
                )

    def stop(self) -> None:
        pg_ctl = _pg_bin("pg_ctl")
        if self._pgdata.exists():
            subprocess.run(
                [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self._tmpdir, ignore_errors=True)


def seed_workspace_with_repo(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_name: str = "Primary Workspace",
    repo_name: str = "acme/career-memory",
    role: str = "OWNER",
    resource_status: str = "ACTIVE",
) -> tuple[WorkspaceAccessContext, str, str]:
    """Seed user, workspace, active membership, source_account, and source_resource.

    Returns ``(access_context, source_account_id, source_resource_id)``.
    """
    user_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
    issuer = "https://auth.career-memory.local/auth/v1"
    subject = f"sub-{user_id}"

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                    VALUES (%s::uuid, %s, %s, %s, %s)
                    """,
                    (user_id, issuer, subject, f"{user_id}@local.test", "Test Owner"),
                )
                cur.execute(
                    """
                    INSERT INTO public.workspaces (id, owner_user_id, name)
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (workspace_id, user_id, workspace_name),
                )
                cur.execute(
                    """
                    INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (workspace_id, user_id, role),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_accounts (
                        id, workspace_id, provider, external_account_id,
                        token_secret_ref, scopes, status
                    )
                    VALUES (%s::uuid, %s::uuid, 'GITHUB', %s, %s, ARRAY['public_repo'], 'ACTIVE')
                    """,
                    (
                        account_id,
                        workspace_id,
                        f"gh-{account_id[:8]}",
                        f"secret://github/accounts/{account_id}",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_resources (
                        id, workspace_id, source_account_id, provider_resource_id,
                        resource_type, name, canonical_url, visibility,
                        processing_mode, raw_storage_mode, raw_retention_seconds,
                        status, policy_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                        'PUBLIC_CLOUD', 'NONE', NULL,
                        %s, 'v1'
                    )
                    """,
                    (
                        resource_id,
                        workspace_id,
                        account_id,
                        f"repo-{resource_id[:8]}",
                        repo_name,
                        f"https://github.com/{repo_name}",
                        resource_status,
                    ),
                )

    access = WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer=issuer, subject=subject),
        ),
        membership=WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
        ),
    )
    return access, account_id, resource_id


def add_repo_to_workspace(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_id: str,
    source_account_id: str,
    repo_name: str,
    status: str = "ACTIVE",
) -> str:
    resource_id = str(uuid.uuid4())
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_resources (
                        id, workspace_id, source_account_id, provider_resource_id,
                        resource_type, name, canonical_url, visibility,
                        processing_mode, raw_storage_mode, raw_retention_seconds,
                        status, policy_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        'GITHUB_REPOSITORY', %s, %s, 'PUBLIC',
                        'PUBLIC_CLOUD', 'NONE', NULL,
                        %s, 'v1'
                    )
                    """,
                    (
                        resource_id,
                        workspace_id,
                        source_account_id,
                        f"repo-{resource_id[:8]}",
                        repo_name,
                        f"https://github.com/{repo_name}",
                        status,
                    ),
                )
    return resource_id


def revoke_membership(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_id: str,
    user_id: str,
    revoked_at: datetime | None = None,
) -> None:
    ts = revoked_at or datetime.now(UTC)
    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.workspace_memberships
                    SET revoked_at = %s
                    WHERE workspace_id = %s::uuid
                      AND user_id = %s::uuid
                    """,
                    (ts, workspace_id, user_id),
                )
