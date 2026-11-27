"""Ephemeral local PostgreSQL 18 test harness applying 0010 -> 0020 -> 0030 -> 0040."""

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
MIGRATIONS_IN_ORDER: tuple[Path, ...] = (
    REPO_ROOT / "db" / "migrations" / "0010_identity_workspace_source_auth.sql",
    REPO_ROOT / "db" / "migrations" / "0020_source_provenance_metadata.sql",
    REPO_ROOT / "db" / "migrations" / "0030_events_facts_claims_contributions.sql",
    REPO_ROOT / "db" / "migrations" / "0040_sync_runs_jobs_outbox_audit.sql",
)


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

    def __init__(self, *, prefix: str = "cm-m2-t201-pg18-") -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_pg_admin"
        self.dbname = "career_memory_t201_test"
        self.api_login = "cm_api_login_t201"
        self.api_password = "local_ephemeral_api_pw"
        self.worker_login = "cm_worker_login_t201"
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
    def admin_connection(
        self, *, autocommit: bool = False
    ) -> Iterator[psycopg.Connection]:
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

    def apply_all_migrations(self) -> None:
        for migration_path in MIGRATIONS_IN_ORDER:
            sql = migration_path.read_text(encoding="utf-8")
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
                cur.execute("SET LOCAL app.allow_revision_purge = 'on'")
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
                        public.content_units,
                        public.source_object_revisions,
                        public.source_objects,
                        public.source_resources,
                        public.orphan_secret_cleanups,
                        public.oauth_connect_states,
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


def seed_workspace_member(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_name: str = "Primary Workspace",
    role: str = "OWNER",
    workspace_id: str | None = None,
) -> WorkspaceAccessContext:
    """Seed user, optional workspace, and active membership."""
    user_id = str(uuid.uuid4())
    ws_id = workspace_id or str(uuid.uuid4())
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
                    (user_id, issuer, subject, f"{user_id}@local.test", "Test User"),
                )
                if workspace_id is None:
                    cur.execute(
                        """
                        INSERT INTO public.workspaces (id, owner_user_id, name)
                        VALUES (%s::uuid, %s::uuid, %s)
                        """,
                        (ws_id, user_id, workspace_name),
                    )
                cur.execute(
                    """
                    INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                    VALUES (%s::uuid, %s::uuid, %s)
                    """,
                    (ws_id, user_id, role),
                )

    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer=issuer, subject=subject),
        ),
        membership=WorkspaceMembership(
            workspace_id=ws_id,
            user_id=user_id,
            role=role,
        ),
    )


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


def seed_provenance_and_active_work(
    cluster: EphemeralPg18Cluster,
    *,
    workspace_id: str,
    source_resource_id: str,
    include_running_job: bool = True,
) -> dict[str, str]:
    """Seed source_object, immutable revision, content_unit, active sync_run, and job."""
    now = datetime.now(UTC)
    obj_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    unit_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    lease_token = str(uuid.uuid4())

    with cluster.admin_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_objects (
                        id, workspace_id, source_resource_id, object_type,
                        provider_object_id, lifecycle_state, first_seen_at, last_seen_at
                    )
                    VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', 'pr-101', 'ACTIVE', %s, %s)
                    """,
                    (obj_id, workspace_id, source_resource_id, now, now),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions (
                        id, workspace_id, source_object_id, provider_revision_id,
                        content_hash, hash_policy_version, observed_at,
                        is_tombstone, normalized_metadata, connector_version, schema_version
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'rev-1',
                        %s, 'v1', %s, false, '{}'::jsonb, 'gh-v1', 'v1'
                    )
                    """,
                    (rev_id, workspace_id, obj_id, "sha256:" + "a" * 64, now),
                )
                cur.execute(
                    """
                    INSERT INTO public.content_units (
                        id, workspace_id, source_object_revision_id, unit_type,
                        anchor, text_content, content_ref, content_hash,
                        canonicalization_version, retention_class, expires_at,
                        ordinal, policy_state
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'PR_TITLE',
                        '{"parser_version": "v1"}'::jsonb, NULL, NULL, %s,
                        'v1', 'NONE', NULL, 0, 'ACTIVE'
                    )
                    """,
                    (unit_id, workspace_id, rev_id, "sha256:" + "b" * 64),
                )
                cur.execute(
                    """
                    INSERT INTO public.sync_runs (
                        id, workspace_id, source_resource_id, mode,
                        status, processing_status, import_ready, started_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s::uuid, 'INITIAL_BACKFILL',
                        'RUNNING', 'NOT_STARTED', false, %s
                    )
                    """,
                    (run_id, workspace_id, source_resource_id, now),
                )
                if include_running_job:
                    cur.execute(
                        """
                        INSERT INTO public.jobs (
                            id, workspace_id, job_type, target_sync_run_id,
                            input_hash, input_manifest, status, attempt, max_attempts,
                            lease_owner, lease_token, lease_until,
                            logical_effect_key
                        )
                        VALUES (
                            %s::uuid, %s::uuid, 'INITIAL_BACKFILL', %s::uuid,
                            %s, %s::jsonb, 'RUNNING', 1, 3,
                            'worker-1', %s::uuid, %s,
                            %s
                        )
                        """,
                        (
                            job_id,
                            workspace_id,
                            run_id,
                            "sha256:" + "c" * 64,
                            f'{{"source_resource_id": "{source_resource_id}"}}',
                            lease_token,
                            now,
                            f"backfill:{run_id}",
                        ),
                    )

    return {
        "source_object_id": obj_id,
        "revision_id": rev_id,
        "content_unit_id": unit_id,
        "sync_run_id": run_id,
        "job_id": job_id,
    }
